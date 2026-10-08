from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import asdict

from .adapter import reservation
from .context import compaction_prefix, encode, groups, history_cost, history_data, raw_capacity, reply_messages
from .contracts import FailureNotice, GovernedError
from .memory import MemoryGraph
from .run_records import answer_record, digest, freeze_retrieval
from .scratch import CanonicalSnapshot
from .selection_policy import policy_identity
from . import prompts


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def label_data(text):
    try:
        data = strict_json(text)
        if not isinstance(data, dict) or set(data) != {"marks"}:
            raise ValueError()
        marks = data["marks"]
        if not isinstance(marks, list) or not 1 <= len(marks) <= 8:
            raise ValueError()
        def valid_mark(mark):
            return isinstance(mark, str) and 0 < len(mark.strip()) <= 40
        if any(not valid_mark(mark) for mark in marks):
            raise ValueError()
        return sorted({mark.strip() for mark in marks})
    except (ValueError, TypeError, KeyError):
        raise GovernedError("invalid_labels") from None


def bot_reply_data(text):
    """Validate the decision as data; silence is never a transport placeholder."""
    try:
        data = strict_json(text)
        if not isinstance(data, dict) or set(data) != {"action", "text"}:
            raise ValueError()
        if not isinstance(data["text"], str):
            raise ValueError()
        if data["action"] == "skip" and data["text"] == "":
            return data
        if data["action"] == "reply" and data["text"].strip():
            return data
        raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise GovernedError("invalid_bot_reply_decision") from None


class Runtime:
    def __init__(self, config, store, adapter, scratch, *, selector=None):
        self.config, self.store, self.adapter, self.scratch = config, store, adapter, scratch
        self.graph = MemoryGraph(store.db, self_marks=(config.name,),
                                 retrieval_policy=config.runtime.retrieval_policy,
                                 selector=selector)
        self.model_selector = selector if callable(getattr(selector, "choose", None)) else None
        self.knowledge_scope = f"{config.discord.guild_id}:knowledge"
        self._serial = asyncio.Lock()

    async def _summary(self, message, knowledge, trace_id, *, adapter_override=None):
        checkpoint = self.store.checkpoint(message.scope)
        rows = self.store.history(message.scope, checkpoint["through_seq"], exclude_turn=message.message_id)
        instructions = (prompts.bot_reply_instructions(self.config.name) if message.author_is_bot
                        else prompts.reply_instructions(self.config.name))
        schema = prompts.BOT_REPLY_SCHEMA if message.author_is_bot else None
        capacity = raw_capacity(self.config, instructions, message, knowledge, schema)
        prefix = compaction_prefix(rows, capacity, self.config.runtime.low_watermark)
        self.scratch.write("context_watermark", trace_id=trace_id, raw_reservation=history_cost(rows),
                           raw_capacity=capacity, low_watermark=self.config.runtime.low_watermark,
                           checkpoint_id=checkpoint["id"], planned_source_seqs=[r["seq"] for r in prefix])
        if prefix:
            admitted = []
            budget = self.config.adapter.budgets["summary"]
            summary_schema = prompts.summary_schema(self.config.runtime.summary_max_bytes)
            def summary_input(selected):
                return [{"role": "user", "content": encode({"previous_summary": checkpoint["summary"],
                    "source_prefix": history_data(selected), "summary_max_utf8_bytes": self.config.runtime.summary_max_bytes,
                    "summary_target_characters": self.config.runtime.summary_max_bytes // 4 * 3 // 4})}]
            for interaction in groups(prefix):
                proposed = admitted + interaction
                if reservation(prompts.SUMMARY, summary_input(proposed), summary_schema) > budget.input_tokens:
                    break
                admitted = proposed
            if not admitted:
                raise GovernedError("summary_prefix_too_large")
            result = await (adapter_override if adapter_override is not None else self.adapter).call(
                "summary", prompts.SUMMARY, summary_input(admitted), trace_id, summary_schema)
            try:
                data = strict_json(result.text)
                if not isinstance(data, dict) or set(data) != {"summary"} or not isinstance(data["summary"], str):
                    raise ValueError()
                summary = data["summary"].strip()
                if not summary:
                    raise ValueError()
            except (ValueError, TypeError):
                raise GovernedError("invalid_summary") from None
            if (len(summary.encode('utf-8')) > self.config.runtime.summary_max_bytes
                    or len(summary) >= summary_schema['properties']['summary']['maxLength']):
                # Do not acknowledge source coverage with a truncated summary.
                # Keep the checkpoint and all original rows. Fit a contiguous
                # tail of whole interactions, explicitly declaring the gap.
                tail = []
                for interaction in reversed(groups(rows)):
                    proposed = interaction + tail
                    if history_cost(proposed) > capacity - 512:
                        break
                    tail = proposed
                warning = 'Continuity is incomplete: summary overflow; older unsummarized observations omitted. Do not infer their contents.'
                self.scratch.write('summary_overflow_fallback', trace_id=trace_id,
                    actual_utf8_bytes=len(summary.encode('utf-8')), limit=self.config.runtime.summary_max_bytes,
                    actual_characters=len(summary), schema_characters=summary_schema['properties']['summary']['maxLength'],
                    checkpoint_updated=False, automatic_retries=0,
                    retained_source_seqs=[r['seq'] for r in tail],
                    omitted_source_seqs=[r['seq'] for r in rows if r not in tail])
                return reply_messages(checkpoint['summary'], tail, knowledge, message, limitation=warning)
            checkpoint = self.store.save_checkpoint(message.scope, admitted, summary, trace_id)
            self.scratch.write("checkpoint_saved", trace_id=trace_id, checkpoint=checkpoint)
            rows = self.store.history(message.scope, checkpoint["through_seq"], exclude_turn=message.message_id)
            if history_cost(rows) > capacity:
                raise GovernedError("context_backlog_requires_next_turn")
        return reply_messages(checkpoint["summary"], rows, knowledge, message)

    async def process(self, message, deliver):
        # Discord bridge also serializes. This lock preserves the invariant for
        # direct callers/tests without adding another conversation surface.
        async with self._serial:
            active_enabled = getattr(self.config.runtime, "active_requery_enabled", False)
            turn_seconds = (min(self.config.runtime.turn_seconds, self.config.runtime.active_requery_seconds)
                            if active_enabled else self.config.runtime.turn_seconds)
            turn_deadline = time.monotonic() + turn_seconds
            trace_id = uuid.uuid4().hex
            if not self.store.begin(message):
                self.scratch.write("duplicate_ignored", trace_id=trace_id, message_id=message.message_id)
                return
            input_record = {"text": message.text, "raw_text": message.raw_text, "author_id": message.author_id}
            if message.author_is_bot:
                input_record["author_is_bot"] = True
            self.scratch.write("turn_start", trace_id=trace_id, message_id=message.message_id, scope=message.scope,
                               input=input_record,
                               deadline_seconds=turn_seconds,
                               run_record_version=3 if getattr(self.config.runtime, "active_requery_enabled", False) else 1,
                               knowledge_scope=self.knowledge_scope,
                               **({"model_selection_policy": policy_identity(self.model_selector)}
                                  if self.model_selector is not None else {}))
            delivered = False
            confirmed = False
            local_phase = None
            active = None
            turn_adapter = None
            def halt_budget(code):
                if turn_adapter is not None:
                    try:
                        turn_adapter.halt(code)
                    except Exception as ledger_error:
                        print(f"[{self.config.name}] query budget stop persistence failed: {type(ledger_error).__name__}; trace={trace_id}", flush=True)
            try:
                async with asyncio.timeout(turn_seconds):
                    if active_enabled:
                        from .active_requery import ActiveRequery, TurnBudgetAdapter
                        turn_adapter = TurnBudgetAdapter(self.adapter, self.config, self.scratch,
                                                         trace_id, message.message_id, message.scope)
                        active = ActiveRequery(self, turn_adapter, trace_id, message)
                    started = time.monotonic()
                    # sqlite is cooperatively interrupted during expensive scans;
                    # graph methods also bound their lookup/expansion outputs.
                    local_deadline_observed = False

                    def local_progress():
                        nonlocal local_deadline_observed
                        local_deadline_observed |= time.monotonic() - started > self.config.runtime.local_seconds
                        return int(local_deadline_observed)

                    self.store.db.set_progress_handler(local_progress, 1000)
                    try:
                        local_phase = "retrieval"
                        graph_audit = {}
                        previous = self.store.db.execute("SELECT content FROM messages WHERE scope=? AND role='user' AND turn_id!=? ORDER BY seq DESC LIMIT 1", (message.scope, message.message_id)).fetchone()
                        if self.model_selector is None:
                            records = self.graph.retrieve(self.knowledge_scope, [], message.text, time.time(),
                                                          event_id=message.message_id, audit=graph_audit,
                                                          ranking_mode="static", context_query=previous[0] if previous else '')
                        else:
                            prepared = self.graph.prepare_retrieval(self.knowledge_scope, [], message.text,
                                time.time(), event_id=message.message_id,
                                context_query=previous[0] if previous else '')
                            if time.monotonic() - started > self.config.runtime.local_seconds:
                                raise GovernedError("local_memory_timeout")
                            # No SQLite transaction or local SQL deadline crosses
                            # the separately bounded, serial model selection call.
                            self.store.db.set_progress_handler(None, 0)
                            local_phase = "model_selection"
                            selection_started = time.monotonic()
                            try:
                                if active is None:
                                    decision = await self.model_selector.choose(prepared.request, trace_id)
                                else:
                                    decision = await self.model_selector.choose(prepared.request, trace_id,
                                                                               adapter_override=turn_adapter)
                            finally:
                                started += time.monotonic() - selection_started
                                self.store.db.set_progress_handler(local_progress, 1000)
                            local_phase = "selection_commit"
                            records = self.graph.finish_retrieval(prepared, decision, audit=graph_audit)
                        local_phase = "memory_observation"
                        # One immutable JSON snapshot binds the committed graph
                        # receipt to both scratch and the recalled materials.
                        # Reuse its bytes, never a hash cached on a mutable dict.
                        audit_snapshot = CanonicalSnapshot(graph_audit)
                        snapshot_writer = getattr(self.scratch, "write_snapshot", None)
                        if callable(snapshot_writer):
                            snapshot_writer("memory_observation", trace_id=trace_id,
                                            snapshots={"audit": audit_snapshot},
                                            audit_sha256=audit_snapshot.sha256)
                        else:
                            self.scratch.write("memory_observation", trace_id=trace_id, audit=graph_audit,
                                               audit_sha256=audit_snapshot.sha256)
                        local_phase = "freeze_retrieval"
                        retrieval = freeze_retrieval(self.store.db, self.knowledge_scope, message.message_id,
                                                     message.text, records, audit_snapshot)
                        knowledge = retrieval["model_materials"]
                    except sqlite3.OperationalError as error:
                        # Only this deadline's actual VM interruption is a local
                        # timeout. Locks, malformed SQL and external interrupts
                        # must retain their distinct diagnostic code.
                        sqlite_code = getattr(error, "sqlite_errorcode", None)
                        if local_deadline_observed and type(sqlite_code) is int and sqlite_code == sqlite3.SQLITE_INTERRUPT:
                            raise GovernedError("local_memory_timeout") from error
                        raise
                    finally:
                        self.store.db.set_progress_handler(None, 0)
                    if time.monotonic() - started > self.config.runtime.local_seconds:
                        raise GovernedError("local_memory_timeout")
                    local_phase = None
                    retrieval_hash = digest(retrieval)
                    self.scratch.write("retrieval_record", trace_id=trace_id, record=retrieval, record_sha256=retrieval_hash)
                    self.scratch.write("knowledge_retrieved", trace_id=trace_id, input_marks=[],
                                       query=message.text, records=knowledge, retrieval_sha256=retrieval_hash,
                                       elapsed_seconds=time.monotonic() - started)
                    if active is None:
                        messages = await self._summary(message, knowledge, trace_id)
                        record_answer = lambda text: answer_record(text, retrieval)
                        evidence_link = {"retrieval_sha256": retrieval_hash}
                        final_fields = {}
                    else:
                        from .active_requery import context_with_bundle
                        from .requery_records import answer_record_bundle
                        active.append(retrieval, f"{message.message_id}:initial")
                        messages = await self._summary(message, knowledge, trace_id, adapter_override=turn_adapter)
                        bundle = await active.gather(messages)
                        messages = context_with_bundle(messages, bundle)
                        record_answer = lambda text: answer_record_bundle(text, bundle)
                        evidence_link = {"bundle_sha256": digest(bundle)}
                        final_fields = {"final_origin": "bounded_stop" if active.fallback_text else "model",
                                        "simulation": turn_adapter.mock}
                    if getattr(self.config.runtime, "communicability_enabled", False):
                        # Independent frozen-data diagnostic. Never feed scores
                        # into selection, model context, or base graph weights.
                        from .communicability import CommunicabilityConfig, analyze_frozen_graph
                        diagnostic_input = bundle if active is not None else retrieval
                        diagnostic_config = CommunicabilityConfig(enabled=True)
                        try:
                            diagnostic = analyze_frozen_graph(diagnostic_input, config=diagnostic_config,
                                deadline=min(turn_deadline, time.monotonic() + diagnostic_config.max_seconds))
                        except Exception as diagnostic_error:
                            diagnostic = {"schema": "communicability_offline_v1", "status": "unknown",
                                          "reason": "diagnostic_error", "error_type": type(diagnostic_error).__name__,
                                          "proof": False, "changes_selection": False, "changes_base_weights": False}
                        self.scratch.write("communicability_diagnostic", trace_id=trace_id,
                                           evidence_kind="bundle" if active is not None else "retrieval",
                                           evidence_sha256=digest(diagnostic_input), receipt=diagnostic,
                                           independent_diagnostic=True)
                    instructions = (prompts.bot_reply_instructions(self.config.name) if message.author_is_bot
                                    else prompts.reply_instructions(self.config.name))
                    schema_fields = {"response_schema": prompts.BOT_REPLY_SCHEMA} if message.author_is_bot else {}
                    self.scratch.write("reply_context", trace_id=trace_id, **evidence_link,
                                       instructions=instructions, messages=messages, **schema_fields, **final_fields,
                                       **({"no_provider_reply": bool(active.fallback_text)} if active else {}))
                    reply_adapter = turn_adapter if active is not None else self.adapter
                    if active is not None and active.fallback_text:
                        result = None
                        answer = active.fallback_text
                    elif message.author_is_bot:
                        result = await reply_adapter.call("reply", instructions, messages, trace_id, prompts.BOT_REPLY_SCHEMA)
                        decision = bot_reply_data(result.text)
                        self.scratch.write("bot_reply_decision", trace_id=trace_id, **evidence_link,
                                           **decision, model_result=asdict(result))
                        if decision["action"] == "skip":
                            self.store.fail(message.message_id, "skipped")
                            self.scratch.write("turn_end", trace_id=trace_id, status="skipped", reason="bot_reply_skipped")
                            return
                        answer = decision["text"]
                    else:
                        result = await reply_adapter.call("reply", instructions, messages, trace_id)
                        answer = result.text
                    self.scratch.write("answer_generated", trace_id=trace_id, **evidence_link, **final_fields,
                                       record=record_answer(answer), model_result=asdict(result) if result else None)
                    self.store.generated(message.message_id, answer)
                    self.scratch.write("delivery_start", trace_id=trace_id, message_id=message.message_id, text=answer)
                    # Mark attempt before crossing Discord boundary. A timeout
                    # can mean delivered remotely; never automatically retry.
                    delivered = True
                    async with asyncio.timeout(self.config.discord.delivery_seconds):
                        receipt = await deliver(FailureNotice(answer) if active is not None and active.fallback_text
                                                and message.author_is_bot else answer)
                    confirmed = True
                    self.store.finish(message, receipt)
                    self.scratch.write("answer_delivered", trace_id=trace_id, **evidence_link, **final_fields,
                                       record=record_answer(receipt.text), receipt_ids=receipt.message_ids)
                    self.scratch.write("turn_end", trace_id=trace_id, status="delivered", receipt={"ids": receipt.message_ids, "text": receipt.text})
                    return
            except asyncio.CancelledError:
                halt_budget("post_delivery_record_cancelled" if confirmed else "delivery_unknown" if delivered else "cancelled")
                if confirmed:
                    print(f"[{self.config.name}] Discord delivery confirmed; post-delivery recording interrupted; trace={trace_id}", flush=True)
                    raise
                code = "delivery_unknown" if delivered else "cancelled"
                self.store.fail(message.message_id, code)
                self.scratch.write("turn_end", trace_id=trace_id, status=code)
                raise
            except Exception as error:
                if confirmed:
                    halt_budget("post_delivery_record_failed")
                    # A local audit failure cannot erase a confirmed Discord
                    # receipt or trigger another delivery. Preserve history.
                    print(f"[{self.config.name}] Discord delivery confirmed; post-delivery storage/audit failed: {type(error).__name__}; trace={trace_id}; inspect records before restarting.", flush=True)
                    return
                code = error.code if isinstance(error, GovernedError) else "turn_timeout" if isinstance(error, TimeoutError) else type(error).__name__
                if delivered:
                    code = "delivery_unknown:" + code
                halt_budget(code)
                self.store.fail(message.message_id, code)
                diagnostic = error.__cause__ if isinstance(error.__cause__, sqlite3.OperationalError) else error
                failure_fields = {"error_type": type(diagnostic).__name__}
                if isinstance(diagnostic, sqlite3.Error):
                    sqlite_code = getattr(diagnostic, "sqlite_errorcode", None)
                    sqlite_name = getattr(diagnostic, "sqlite_errorname", None)
                    if type(sqlite_code) is int:
                        failure_fields["sqlite_errorcode"] = sqlite_code
                    if isinstance(sqlite_name, str) and sqlite_name.startswith("SQLITE_") and sqlite_name.isascii() and sqlite_name.replace("_", "").isalnum():
                        failure_fields["sqlite_errorname"] = sqlite_name
                if local_phase is not None:
                    failure_fields.update(phase=local_phase, local_seconds=self.config.runtime.local_seconds)
                self.scratch.write("turn_end", trace_id=trace_id, status="failed", code=code, **failure_fields)
                print(f"[{self.config.name}] turn failed: {code}; trace={trace_id}", flush=True)
                if not delivered:
                    # A fixed failure receipt adds no model request. It is traced
                    # and kept separate from assistant conversational history.
                    try:
                        remaining = turn_deadline - time.monotonic()
                        if remaining <= 0:
                            self.scratch.write("failure_notice_skipped", trace_id=trace_id, reason="turn_time_exhausted")
                            return
                        notice = f"本轮未完成（{code}）。审计编号：{trace_id[:12]}"
                        async with asyncio.timeout(min(self.config.discord.delivery_seconds, remaining)):
                            receipt = await deliver(FailureNotice(notice) if message.author_is_bot else notice)
                        self.scratch.write("failure_notice_delivered", trace_id=trace_id, receipt={"ids": receipt.message_ids, "text": receipt.text})
                    except Exception as error:
                        self.scratch.write("failure_notice_unknown", trace_id=trace_id, error_type=type(error).__name__)
            finally:
                if turn_adapter is not None:
                    try:
                        turn_adapter.finish()
                    except Exception as error:
                        # Never convert a confirmed delivery into a second send.
                        print(f"[{self.config.name}] query budget ledger close failed: {type(error).__name__}; trace={trace_id}", flush=True)
