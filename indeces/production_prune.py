"""Internal, bounded question service for the existing Discord Runtime."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .contracts import GovernedError, ModelResult
from .console_prune import _sidecar, _adapter_signature, _MetadataScratch, _SYSTEM, _display_text
from .claude_prune_budget import PruneBudget, NATIVE_INPUT_RESERVATION
from .claude_prune_loop import PruneToolLoop
from .prune_native_plan import select_native_plan
from .prune_question import prepare_grounding, resolve_grounded_plan
from .richard_chat_selection import strict_json
from ._vendor.npmi_prune import Graph, Session


class ClosedStageAdapter:
    """Business services cannot bypass the question's native budget bridge."""
    def __init__(self, config, service):
        self.config, self._service = config, service

    async def call(self, *args, **kwargs):
        self._service.halt("prune_ordinary_stage_forbidden")
        raise GovernedError("prune_ordinary_stage_forbidden", remote_usage_unknown=False)


class PruneQuestionService:
    def __init__(self, config, config_path, adapter, *, budget=None, graph=None, live=True):
        self.config, self.adapter, self.live = config, adapter, live
        self._signature = _adapter_signature(config)
        self._owned_budget = budget is None
        self._owned_client = False
        self._closed = self._answering = False
        self.last_receipt = None
        if budget is None or graph is None:
            graph_dir, evidence_dir, binding, price = _sidecar(Path(config_path))
            if graph is None:
                graph = Graph(str(graph_dir), str(evidence_dir), verify_hashes=True)
            if budget is None:
                budget = PruneBudget.bind_existing_grant(binding, price=price, offline_accepted=True)
        self.graph, self.budget = graph, budget
        try:
            opening = budget.snapshot()
            reply = config.adapter.budgets["reply"]
            if opening["halted"] is not None or opening["pending_nusd"]:
                raise GovernedError("prune_chat_budget_not_ready")
            if opening["remaining_nusd"] < budget._price.cost(NATIVE_INPUT_RESERVATION, min(1024, reply.output_tokens)):
                raise GovernedError("prune_chat_budget_not_ready")
            # Native continuation retains signed blocks only in memory. Runtime
            # records the visible answer and safe provenance separately.
            adapter.scratch = _MetadataScratch()
            if live and adapter._session is None:
                import aiohttp
                adapter._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None), trust_env=False)
                adapter._session._retry_connection = False
                self._owned_client = True
        except BaseException:
            if self._owned_budget:
                try:
                    budget.close()
                except BaseException:
                    pass
            raise

    def session_stop_error(self):
        if self._closed:
            return GovernedError("prune_chat_closed", remote_usage_unknown=False)
        state = self.budget.snapshot()
        if state["halted"] is not None or (state["pending_nusd"] and not self._answering):
            return GovernedError("prune_chat_halted", remote_usage_unknown=bool(state["pending_nusd"]))
        return None

    def halt(self, code):
        self.budget.halt(code)

    async def answer(self, message, trace_id, *, seconds):
        if self._answering:
            raise GovernedError("prune_chat_busy", remote_usage_unknown=False)
        error = self.session_stop_error()
        if error is not None:
            raise error
        if _adapter_signature(self.config) != self._signature:
            raise GovernedError("prune_chat_configuration_changed", remote_usage_unknown=False)
        started = time.monotonic()
        reply = self.config.adapter.budgets["reply"]
        deadline = started + min(seconds, reply.seconds, 90)
        question = message.text
        try:
            grounding = prepare_grounding(self.graph, question)
        except GovernedError as error:
            # A local absence is an honest answer, not a fabricated graph plan
            # or a paid attempt. No budget reservation is created here.
            text = "I cannot ground this question in the fixed graph: " + error.code + ". Please clarify the concept and the kind of relation you want."
            if message.author_is_bot:
                text = json.dumps({"action": "reply", "text": text}, ensure_ascii=False)
            self.last_receipt = {"schema": "production_prune_v1", "status": "local_decline", "code": error.code,
                                 "model_calls": 0, "graph_sha256": self.graph.hashes}
            return ModelResult(text, 0, 0, "local-grounding-decline", time.monotonic()-started)
        self._answering = True
        try:
            selection = self.config.adapter.budgets["selection"]
            plan_result = await select_native_plan(self.adapter, self.budget, grounding.instructions,
                grounding.user, grounding.schema, trace_id + "-plan",
                input_cap=min(selection.input_tokens, reply.input_tokens, 65536),
                output_cap=min(selection.output_tokens, reply.output_tokens, 1024),
                seconds=min(selection.seconds, max(0.001, deadline-time.monotonic())), live=self.live)
            plan = resolve_grounded_plan(self.graph, question, strict_json(plan_result.text), grounding)
            remaining_input = min(65536, reply.input_tokens)-plan_result.input_tokens
            remaining_output = min(4096, reply.output_tokens)-plan_result.output_tokens
            remaining_seconds = deadline-time.monotonic()
            if remaining_input <= 0 or remaining_output <= 0 or remaining_seconds <= 0:
                raise GovernedError("prune_question_total_limit", remote_usage_unknown=False)
            session = Session(self.graph, trace_id, plan.seeds_A, plan.seeds_B, plan.target,
                              level=plan.level, text_provider=None, log_path=None)
            instructions = _SYSTEM
            if message.author_is_bot:
                instructions += (' The sender is a bot in a bounded conversation. Your final text must be ONLY JSON '
                    'with exactly action and text: action is reply with nonempty text, or skip with empty text. '
                    'Use skip if no useful new reply is warranted. Complete answer(none) before skipping.')
            user = json.dumps({"question": question, "seeds_A_cid": session.A, "seeds_B_cid": session.B,
                               "target": plan.target, "level": plan.level}, ensure_ascii=False, allow_nan=False)
            loop = PruneToolLoop(self.adapter, session, self.budget, max_rounds=8, live=self.live)
            result = await loop.run(instructions, user, trace_id + "-tools", input_cap=remaining_input,
                output_cap=min(1024, remaining_output), seconds=remaining_seconds,
                total_input_cap=remaining_input, total_output_cap=remaining_output)
            text = _display_text(result.text)
            if message.author_is_bot:
                from .runtime import bot_reply_data
                decision = bot_reply_data(text)
                text = json.dumps(decision, ensure_ascii=False)
            state = self.budget.snapshot()
            self.last_receipt = {"schema": "production_prune_v1", "status": "answered",
                "graph_sha256": self.graph.hashes, "seeds_A_original": list(plan.seeds_A),
                "seeds_B_original": list(plan.seeds_B), "target": plan.target, "level": plan.level,
                "tools": [row["action"] for row in session.log], "answer_receipt": result.answer_receipt,
                "spent_nusd": state["spent_nusd"], "remaining_nusd": state["remaining_nusd"],
                "pending_nusd": state["pending_nusd"], "model_rounds": result.rounds+1,
                "evidence_text_available": False, "scientific_certification": False}
            return ModelResult(text, result.input_tokens+plan_result.input_tokens,
                result.output_tokens+plan_result.output_tokens, result.response_id, time.monotonic()-started)
        except BaseException as error:
            try:
                self.budget.halt(getattr(error, "code", "prune_question_failure"))
            except BaseException:
                pass
            raise
        finally:
            self._answering = False

    async def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._owned_client and self.adapter._session is not None and not self.adapter._session.closed:
                await self.adapter._session.close()
        finally:
            if self._owned_budget:
                self.budget.close()
