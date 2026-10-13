"""Isolated native Claude continuation for the unchanged npmi_prune v0.1.2.

Offline injection is the default. Explicit live mode binds an existing grant and
a precreated no-retry client; it never changes
ClaudeAdapter.call, the OpenAI path, the graph algorithm, or application wiring.
The supplied budget owns durable cumulative accounting; tool-output estimates
in Session are a separate limit. A loop instance and its Session are single-use.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from .claude_adapter import (
    CLAUDE_BASE_URL, ClaudeAdapter, _ClaudeProgress, _COUNT_PATH,
    _GENERATE_PATH, _json_bytes, _validate_json,
)
from .contracts import GovernedError
from .claude_prune_budget import NATIVE_INPUT_RESERVATION, PruneBudget
from ._vendor.npmi_prune import Graph, Session, BudgetExceeded, __version__
from ._vendor.npmi_prune.core import PINNED_INDECES, PINNED_V302


_MODEL = "claude-opus-5-5"
_CONTEXT = 1_048_576
_TOOL_NAME = "npmi_prune"
_MAX_TOOL_INPUT_BYTES = 64 * 1024
_MAX_TOOL_RESULT_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def _copy(value):
    _validate_json(value)
    return json.loads(_json_bytes(value))


def _tool_definition():
    """One native client tool, exposing only the frozen three Session actions."""
    integer = {"type": "integer"}
    ids = {"type": "array", "items": integer}
    return {
        "name": _TOOL_NAME,
        "description": (
            "Operate this question's fixed-seed, subtraction-only graph draft. "
            "view returns top-10 and optional details for at most three current "
            "candidate concept IDs. prune requires nonempty removals with node_id, "
            "nonempty reason, and kind evidence or logic; evidence also requires "
            "existing cited_record_ids. Seeds cannot be removed. At most three "
            "prune rounds; a tool-output budget rejection allows answer only. "
            "answer chooses a current top-10 integer concept ID or the string none, "
            "with optional citations {edge:[cid,cid],record_id:int}. You must "
            "successfully call answer before giving a final answer; no tools after "
            "answer. Unverified citations fail host acceptance. No seed addition."
        ),
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {"type": "string", "enum": ["view", "prune", "answer"]},
                "details": {**ids, "maxItems": 3},
                "removals": {
                    "type": "array", "minItems": 1,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["node_id", "reason", "kind"],
                        "properties": {
                            "node_id": integer,
                            "reason": {"type": "string", "minLength": 1},
                            "kind": {"type": "string", "enum": ["evidence", "logic"]},
                            "cited_record_ids": ids,
                        },
                    },
                },
                "choice": {"anyOf": [integer, {"type": "string", "enum": ["none"]}]},
                "citations": {
                    "type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["edge", "record_id"],
                        "properties": {
                            "edge": {**ids, "minItems": 2, "maxItems": 2},
                            "record_id": integer,
                        },
                    },
                },
            },
        },
    }


def _integers(value):
    return isinstance(value, list) and all(type(item) is int for item in value)


def _tool_input(value):
    """Validate conditional action shape before passing anything to Session."""
    try:
        value = _copy(value)
        if not isinstance(value, dict) or len(_json_bytes(value)) > _MAX_TOOL_INPUT_BYTES:
            raise ValueError()
        action = value.get("action")
        allowed = {
            "view": {"action", "details"},
            "prune": {"action", "details", "removals"},
            "answer": {"action", "choice", "citations"},
        }
        if not isinstance(action, str) or action not in allowed or set(value) - allowed[action]:
            raise ValueError()
        if "details" in value and (not _integers(value["details"]) or len(value["details"]) > 3):
            raise ValueError()
        if action == "prune":
            removals = value.get("removals")
            if not isinstance(removals, list) or not removals:
                raise ValueError()
            for item in removals:
                if (not isinstance(item, dict)
                        or set(item) - {"node_id", "reason", "kind", "cited_record_ids"}
                        or type(item.get("node_id")) is not int
                        or not isinstance(item.get("reason"), str) or not item["reason"].strip()
                        or item.get("kind") not in {"evidence", "logic"}
                        or ("cited_record_ids" in item and not _integers(item["cited_record_ids"]))
                        or (item["kind"] == "evidence" and not item.get("cited_record_ids"))):
                    raise ValueError()
        elif action == "answer":
            if "choice" not in value or not (type(value["choice"]) is int or value["choice"] == "none"):
                raise ValueError()
            citations = value.get("citations", [])
            if not isinstance(citations, list):
                raise ValueError()
            for item in citations:
                if (not isinstance(item, dict) or set(item) != {"edge", "record_id"}
                        or not _integers(item["edge"]) or len(item["edge"]) != 2
                        or type(item["record_id"]) is not int):
                    raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise GovernedError("prune_tool_invalid_input") from None


@dataclass
class _PruneProgress(_ClaudeProgress):
    response_model: object = None

    def received(self, path, data):
        super().received(path, data)
        if path == _GENERATE_PATH:
            # This actual response provenance is captured before native audit,
            # scratch, or decoded-content validation can fail. Never infer it
            # from the requested model or normalized token counters.
            self.response_model = data.get("model") if isinstance(data, dict) else None


@dataclass(frozen=True, repr=False)
class _NativePostView:
    """Seal the existing native post function's captured transport dependencies.

    Audit/scratch callbacks may mutate the original adapter. The native function
    receives immutable driver references. Offline mode has a callable, an empty
    key and no HTTP session; live mode retains its pre-admitted key and session.
    """
    _request_override: object
    _key: str
    _session: object
    scratch: object
    _audit: object


@dataclass(frozen=True)
class PruneLoopResult:
    text: str
    answer_receipt: dict
    rounds: int
    input_tokens: int
    output_tokens: int
    response_id: str
    elapsed_seconds: float
    budget_snapshot: dict


class PruneToolLoop:
    def __init__(self, adapter, session, budget, max_rounds=8, *, live=False):
        if type(max_rounds) is not int or not 1 <= max_rounds <= 8:
            raise GovernedError("prune_invalid_round_limit")
        if type(adapter) is not ClaudeAdapter:
            raise GovernedError("prune_native_adapter_required")
        if type(live) is not bool:
            raise GovernedError("prune_invalid_activation")
        if live:
            if (type(budget) is not PruneBudget or budget.existing_grant_bound is not True
                    or budget.path.resolve() != budget.bound_path):
                raise GovernedError("prune_live_existing_grant_required")
            opening = budget.snapshot()
            if (opening["halted"] is not None or opening["pending_nusd"] != 0
                    or type(opening["remaining_nusd"]) is not int or opening["remaining_nusd"] <= 0):
                raise GovernedError("prune_live_budget_not_ready")
            if adapter._request_override is not None:
                raise GovernedError("prune_live_native_transport_required")
            self._check_live_session(adapter._session)
            if not isinstance(adapter._key, str) or not adapter._key:
                raise GovernedError("prune_live_key_required")
        elif not callable(getattr(adapter, "_request_override", None)):
            raise GovernedError("prune_live_not_authorized")
        if __version__ != "0.1.2" or type(session) is not Session or type(session.g) is not Graph:
            raise GovernedError("prune_frozen_session_required")
        if session.closed:
            raise GovernedError("prune_session_closed")
        expected = {**PINNED_V302, **PINNED_INDECES}
        if session.g.hashes != expected:
            raise GovernedError("prune_graph_pin_mismatch")
        for directory, pins in ((session.g.graph_dir, PINNED_V302), (session.g.indeces_dir, PINNED_INDECES)):
            for name, digest in pins.items():
                h = hashlib.sha256()
                with (Path(directory) / name).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                if h.hexdigest() != digest:
                    raise GovernedError("prune_graph_pin_mismatch")
        if any(not callable(getattr(budget, name, None)) for name in
               ("reserve", "observe", "complete", "fail", "halt", "snapshot")):
            raise GovernedError("prune_budget_required")
        self.adapter, self.session, self.budget = adapter, session, budget
        self.max_rounds = max_rounds
        self._live = live
        self._config = adapter.config
        self._transport = adapter._request_override
        self._native_post = ClaudeAdapter._post
        self._post_view = _NativePostView(self._transport, adapter._key if live else "",
                                         adapter._session if live else None, adapter.scratch, adapter._audit)
        self._used = False
        self._check_adapter()

    def _check_adapter(self):
        adapter = self.adapter
        if (adapter.config.model != _MODEL
                or adapter.config.base_url.rstrip("/") != CLAUDE_BASE_URL
                or getattr(adapter.config, "provider", "anthropic") != "anthropic"
                or ClaudeAdapter._post is not self._native_post
                or getattr(adapter._post, "__func__", None) is not self._native_post
                or adapter.scratch is not self._post_view.scratch
                or adapter._audit is not self._post_view._audit):
            raise GovernedError("prune_native_adapter_changed")
        if self._live:
            if (type(self.budget) is not PruneBudget or self.budget.existing_grant_bound is not True
                    or self.budget.path.resolve() != self.budget.bound_path
                    or adapter.config is not self._config or adapter._request_override is not None
                    or adapter._key is not self._post_view._key
                    or adapter._session is not self._post_view._session):
                raise GovernedError("prune_native_adapter_changed")
            self._check_live_session(self._post_view._session)
        elif adapter._request_override is not self._transport or not callable(self._transport):
            raise GovernedError("prune_native_adapter_changed")

    @staticmethod
    def _check_live_session(session):
        # The owner creates this session only after existing-ledger admission.
        # The loop never resolves credentials or constructs a fallback session.
        import aiohttp
        if type(session) is not aiohttp.ClientSession:
            raise GovernedError("prune_live_session_required")
        if (session.closed is not False or session.trust_env is not False
                or getattr(session, "_retry_connection", True) is not False):
            raise GovernedError("prune_live_session_unsafe")

    def _dispatch(self, request):
        action = request["action"]
        if self.session.closed:
            raise GovernedError("prune_session_closed")
        if action == "view":
            return self.session.view(details=request.get("details"))
        if action == "prune":
            return self.session.prune(request["removals"], details=request.get("details"))
        return self.session.answer(request["choice"], citations=request.get("citations"))

    def _content(self, data):
        if (data.get("type") != "message" or data.get("role") != "assistant"
                or data.get("model") != _MODEL or not isinstance(data.get("id"), str) or not data["id"]
                or data.get("stop_sequence") is not None or not isinstance(data.get("stop_reason"), str)):
            raise GovernedError("invalid_provider_response")
        details = data.get("stop_details")
        if details is not None and not isinstance(details, dict):
            raise GovernedError("invalid_provider_response")
        if data.get("stop_reason") == "refusal" or (isinstance(details, dict) and details.get("type") == "refusal"):
            raise GovernedError("provider_refusal")
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise GovernedError("invalid_output")
        tools, texts = [], []
        for block in blocks:
            if not isinstance(block, dict):
                raise GovernedError("invalid_output")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str) and isinstance(block.get("signature"), str) and block["signature"]:
                pass
            elif kind == "redacted_thinking" and isinstance(block.get("data"), str) and block["data"]:
                pass
            elif kind == "tool_use":
                if (not isinstance(block.get("id"), str) or not _ID.fullmatch(block["id"])
                        or block.get("name") != _TOOL_NAME):
                    raise GovernedError("prune_tool_not_allowed")
                tools.append(block)
            elif kind == "refusal":
                raise GovernedError("provider_refusal")
            else:
                raise GovernedError("unexpected_provider_item")
        if len(tools) > 1:
            raise GovernedError("prune_parallel_tools")
        stop = data.get("stop_reason")
        if (stop == "tool_use" and len(tools) != 1) or (stop == "end_turn" and tools):
            raise GovernedError("invalid_provider_response")
        if stop not in {"tool_use", "end_turn"}:
            raise GovernedError("incomplete_response")
        return blocks, tools, "".join(texts)

    async def run(self, system, user, trace_id, *, input_cap=1_048_576, output_cap=512, seconds=60.0,
                  total_input_cap=65_536, total_output_cap=4096):
        if self._used:
            raise GovernedError("prune_loop_already_used")
        self._used = True
        started = time.monotonic()
        progress = None
        permit = None
        call_id = None
        completed = False
        terminal_attempted = False
        try:
            if (not isinstance(system, str) or not isinstance(user, str) or not user.strip()
                    or not isinstance(trace_id, str) or not trace_id.strip() or len(trace_id) > 240
                    or type(input_cap) is not int or not 0 < input_cap <= _CONTEXT
                    or type(output_cap) is not int or not 0 < output_cap < _CONTEXT
                    or type(total_input_cap) is not int or not 0 < total_input_cap <= _CONTEXT
                    or type(total_output_cap) is not int or not 0 < total_output_cap <= 131_072
                    or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
                raise GovernedError("prune_invalid_input")
            try:
                system, user, trace_id = _copy([system, user, trace_id])
            except (ValueError, UnicodeError, RecursionError):
                raise GovernedError("prune_invalid_input") from None
            deadline = started + seconds
            history = [{"role": "user", "content": user}]
            definition = _copy(_tool_definition())
            answer = None
            seen_ids = set()
            total_input = total_output = 0
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                async with self.adapter._lock:
                    for round_no in range(1, self.max_rounds + 1):
                        permit = progress = call_id = None
                        completed = False
                        terminal_attempted = False
                        self._check_adapter()
                        if time.monotonic() >= deadline:
                            raise GovernedError("stage_timeout")
                        if self.session.closed and answer is None:
                            raise GovernedError("prune_session_closed")
                        remaining_input = total_input_cap - total_input
                        remaining_output = total_output_cap - total_output
                        if remaining_input <= 0 or remaining_output <= 0:
                            raise GovernedError("prune_total_token_limit")
                        effective_output = min(output_cap, remaining_output)
                        effective_input = min(input_cap, remaining_input, _CONTEXT - effective_output)
                        # Count estimates bound execution, not liability. Hold
                        # the native context ceiling so a larger actual input
                        # receipt can be settled before local-limit rejection.
                        permit = self.budget.reserve(f"{trace_id}:{round_no}",
                                                     NATIVE_INPUT_RESERVATION, effective_output)
                        call_id = uuid.uuid4().hex
                        progress = _PruneProgress(started, deadline=deadline)
                        progress.phase = "slot_acquired"
                        progress.slot_wait_seconds = time.monotonic() - started

                        def audit(event, **fields):
                            if event == "response_received" and fields.get("path") == _GENERATE_PATH:
                                fields = {**fields, "response_model": progress.response_model}
                            self.budget.observe(permit, event, fields)

                        common = _copy({
                            "model": _MODEL, "system": system, "messages": history,
                            "output_config": {"effort": "high"}, "thinking": {"type": "adaptive"},
                            "tools": [definition],
                            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
                        })
                        self.adapter._audit(audit, "call_start", progress, trace_id=trace_id,
                                            call_id=call_id, stage="npmi_prune", model=_MODEL,
                                            input_cap=effective_input, output_cap=effective_output,
                                            provider="anthropic", protocol="messages", base_url=CLAUDE_BASE_URL)
                        counted = await self._native_post(self._post_view, _COUNT_PATH, common,
                                                          trace_id, call_id, progress, audit)
                        tokens = counted.get("input_tokens")
                        if type(tokens) is not int or tokens < 0:
                            raise GovernedError("invalid_input_token_count")
                        if tokens > effective_input:
                            raise GovernedError("input_token_limit")
                        self._check_adapter()
                        if time.monotonic() >= deadline:
                            raise GovernedError("stage_timeout")
                        payload = _copy({**common, "max_tokens": effective_output, "stream": False,
                                         "service_tier": "standard_only", "inference_geo": "global"})
                        data = await self._native_post(self._post_view, _GENERATE_PATH, payload,
                                                       trace_id, call_id, progress, audit)
                        self._check_adapter()
                        # Native audit settles the actual receipt before semantic
                        # rejection. A sink failure above follows the same fail()
                        # path with progress already carrying confirmed usage.
                        if progress.known_usage is None:
                            raise GovernedError("missing_usage" if not isinstance(data.get("usage"), dict) else "invalid_usage")
                        terminal_attempted = True
                        self.adapter._record_end(progress, audit, trace_id=trace_id, call_id=call_id,
                                                 stage="npmi_prune", status="completed",
                                                 elapsed_seconds=time.monotonic() - started)
                        self.budget.complete(permit)
                        completed = True
                        if self.budget.snapshot()["halted"] is not None:
                            raise GovernedError("prune_budget_halted")
                        data = _copy(data)
                        inp, out = progress.known_usage["input_tokens"], progress.known_usage["output_tokens"]
                        total_input += inp
                        total_output += out
                        if inp > effective_input or out > effective_output:
                            raise GovernedError("provider_token_limit_breach")
                        if any((progress.native_usage.get("server_tool_use") or {}).values()):
                            raise GovernedError("unexpected_provider_item")
                        blocks, tools, text = self._content(data)
                        if time.monotonic() >= deadline:
                            raise GovernedError("stage_timeout")
                        if not tools:
                            if answer is None:
                                raise GovernedError("prune_answer_required")
                            if not text.strip():
                                raise GovernedError("empty_output")
                            return PruneLoopResult(text, _copy(answer), round_no, total_input, total_output,
                                                   data["id"], time.monotonic() - started,
                                                   _copy(self.budget.snapshot()))
                        if answer is not None or self.session.closed:
                            raise GovernedError("prune_session_closed")
                        block = tools[0]
                        if block["id"] in seen_ids:
                            raise GovernedError("prune_duplicate_tool_id")
                        seen_ids.add(block["id"])
                        request = _tool_input(block.get("input"))
                        try:
                            result = self._dispatch(request)
                            is_error = False
                        except BudgetExceeded:
                            # Frozen Session logged the rejection and locked its
                            # draft to answer-only. No rejected output is leaked.
                            result = {"error": "npmi_prune_output_budget_exhausted", "answer_only": True}
                            is_error = True
                        result = _copy(result)
                        result_bytes = _json_bytes(result)
                        if len(result_bytes) > _MAX_TOOL_RESULT_BYTES:
                            raise GovernedError("prune_tool_result_too_large")
                        if request["action"] == "answer" and not is_error:
                            if (not self.session.closed or not isinstance(result, dict)
                                    or result.get("question_id") != self.session.qid
                                    or result.get("choice") != request["choice"]
                                    or type(result.get("unverified_citations")) is not int):
                                raise GovernedError("prune_answer_receipt_invalid")
                            if result["unverified_citations"] != 0:
                                raise GovernedError("prune_answer_unverified")
                            answer = _copy(result)
                        if time.monotonic() >= deadline:
                            raise GovernedError("stage_timeout")
                        # Preserve ALL native assistant blocks, including signed
                        # thinking and redacted thinking, without rewriting them.
                        history.append({"role": "assistant", "content": _copy(blocks)})
                        result_block = {"type": "tool_result", "tool_use_id": block["id"],
                                        "content": result_bytes.decode("utf-8")}
                        if is_error:
                            result_block["is_error"] = True
                        history.append({"role": "user", "content": [result_block]})
                    raise GovernedError("prune_max_rounds")
        except BaseException as error:
            if isinstance(error, TimeoutError):
                error = GovernedError("stage_timeout")
            if progress is not None:
                progress.annotate_error(error)
                if not completed and not terminal_attempted:
                    try:
                        def failed_audit(event, **fields):
                            self.budget.observe(permit, event, fields)
                        self.adapter._record_end(progress, failed_audit, trace_id=trace_id,
                                                 call_id=call_id, stage="npmi_prune", status="failed",
                                                 code=getattr(error, "code", "prune_local_failure"),
                                                 elapsed_seconds=time.monotonic() - started)
                    except BaseException:
                        pass
            if permit is not None:
                try:
                    self.budget.fail(permit, error)
                except BaseException:
                    pass
            try:
                self.budget.halt(getattr(error, "code", "prune_local_failure"))
            except BaseException:
                pass
            # Cancellation and local primary exceptions survive any secondary
            # terminal audit/ledger failure. No HTTP or sink is ever retried.
            raise error
