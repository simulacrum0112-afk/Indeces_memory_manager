from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import sys
import time
import uuid
from copy import deepcopy
from dataclasses import asdict

from .adapter import reservation
from .config import controlled_session_enabled
from .context import compaction_prefix, encode, groups, history_cost, history_data, raw_capacity, reply_messages
from .contracts import ConfirmedDeliveryPersistenceError, FailureNotice, GovernedError
from .memory import MemoryGraph
from .npmi_telemetry import NPMIRetrievalTelemetry
from .run_records import answer_record, digest, freeze_retrieval, validate_ledger_io_failure
from .scratch import CanonicalSnapshot
from .selection_policy import policy_identity
from .user_notices import NOTICE_POLICY, user_notice
from .requery_records import FINALIZATION_POLICY, REPLY_LIMITATION, FINAL_REPLY_INSTRUCTIONS
from . import prompts
from .session_budget import SessionBudgetAdapter


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
        if (getattr(config.adapter, "provider", "openai") == "anthropic" and
                any(getattr(config.runtime, key, False) for key in
                    ("active_requery_enabled", "path_requery_loop_enabled"))):
            raise GovernedError("claude_requery_unsupported", remote_usage_unknown=False)
        if controlled_session_enabled(config) and type(adapter) is not SessionBudgetAdapter:
            raise GovernedError("session_adapter_required")
        if type(adapter) is SessionBudgetAdapter and any(getattr(config.runtime, key, False)
                for key in ("active_requery_enabled", "path_requery_loop_enabled", "communicability_enabled", "path_hypotheses_enabled")):
            raise GovernedError("session_extra_model_stages_forbidden", remote_usage_unknown=False)
        if getattr(config.runtime, "path_requery_loop_enabled", False):
            if not config.runtime.active_requery_enabled:
                raise GovernedError("path_requery_requires_active_query", remote_usage_unknown=False)
            from .active_requery import _require_provider_authorization
            _require_provider_authorization(adapter, config.runtime)
        self.config, self.store, self.adapter, self.scratch = config, store, adapter, scratch
        self.graph = MemoryGraph(store.db, self_marks=(config.name,),
                                 retrieval_policy=config.runtime.retrieval_policy,
                                 selector=selector)
        self.model_selector = selector if callable(getattr(selector, "choose", None)) else None
        self.knowledge_scope = f"{config.discord.guild_id}:knowledge"
        self._serial = asyncio.Lock()
        self._post_delivery_failure = None

    def session_stop_error(self):
        """Expose terminal shared-session state without admitting a new turn."""
        if type(self.adapter) is not SessionBudgetAdapter:
            return None
        self.session_usage_unknown = None
        try:
            state = self.adapter.ledger.snapshot()
            unknown = any(call["status"] == "unknown" or
                          (call["generation_intents"] and call["usage"] is None)
                          for call in state["calls"])
            unknown = unknown or state["halted"] in {"session_usage_unknown", "session_recovery_pending"}
            self.session_usage_unknown = unknown
            if state["halted"]:
                return GovernedError("session_halted", remote_usage_unknown=unknown)
            self.adapter.check_config()
        except BaseException as error:
            # Preserve the primary check exception. None means that a durable
            # ledger read did not establish aggregate usage certainty.
            return error
        return None

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
            admitted_messages = None
            budget = self.config.adapter.budgets["summary"]
            summary_schema = prompts.summary_schema(self.config.runtime.summary_max_bytes)
            def summary_input(selected):
                return [{"role": "user", "content": encode({"previous_summary": checkpoint["summary"],
                    "source_prefix": history_data(selected), "summary_max_utf8_bytes": self.config.runtime.summary_max_bytes,
                    "summary_target_characters": self.config.runtime.summary_max_bytes // 4 * 3 // 4})}]
            for interaction in groups(prefix):
                proposed = admitted + interaction
                proposed_messages = summary_input(proposed)
                if reservation(prompts.SUMMARY, proposed_messages, summary_schema) > budget.input_tokens:
                    del proposed_messages
                    break
                admitted = proposed
                admitted_messages = proposed_messages
            if not admitted:
                raise GovernedError("summary_prefix_too_large")
            result = await (adapter_override if adapter_override is not None else self.adapter).call(
                "summary", prompts.SUMMARY, admitted_messages, trace_id, summary_schema)
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
            if getattr(self, "_post_delivery_failure", None) is not None:
                failed_message_id, receipt_ids = self._post_delivery_failure
                raise ConfirmedDeliveryPersistenceError(failed_message_id, receipt_ids)
            active_enabled = getattr(self.config.runtime, "active_requery_enabled", False)
            path_provider_fields = {}
            if getattr(self.config.runtime, "path_requery_loop_enabled", False):
                from .active_requery import _path_requery_provider_fields
                path_provider_fields = _path_requery_provider_fields(self.adapter, self.config.runtime)
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
                               notice_policy=NOTICE_POLICY,
                               run_record_version=3 if getattr(self.config.runtime, "active_requery_enabled", False) else 1,
                               knowledge_scope=self.knowledge_scope,
                                npmi_telemetry_version=1,
                               **({"requery_final_policy": FINALIZATION_POLICY} if active_enabled else {}),
                                **path_provider_fields,
                               **({"model_selection_policy": policy_identity(self.model_selector)}
                                  if self.model_selector is not None else {}))
            delivered = False
            confirmed = False
            local_phase = None
            active = None
            turn_adapter = None
            npmi_telemetry = NPMIRetrievalTelemetry()
            npmi_recorded = False

            def record_initial_npmi(error=None):
                nonlocal npmi_recorded
                if npmi_recorded:
                    return
                if npmi_telemetry.status == "not_started":
                    # Ledger/provider preparation can fail before any local
                    # retrieval begins. Preserve that absence without timing it.
                    npmi_telemetry.status = "failed"
                    npmi_telemetry.error_type = type(error).__name__
                npmi_recorded = True
                try:
                    self.scratch.write("npmi_retrieval", trace_id=trace_id, event_id=message.message_id,
                                       telemetry=npmi_telemetry.receipt())
                except Exception as telemetry_error:
                    if error is None:
                        raise
                    # Secondary diagnostic I/O cannot bypass the original
                    # failure/unknown-usage handling. Do not retry the write.
                    print(f"[{self.config.name}] initial retrieval telemetry recording failed: {type(telemetry_error).__name__}; trace={trace_id}", flush=True)

            def final_state_fields(status):
                return active.final_fields(status) if active is not None and active.stop_reason else {}

            def halt_budget(code):
                if turn_adapter is not None:
                    try:
                        turn_adapter.halt(code)
                    except Exception as ledger_error:
                        print(f"[{self.config.name}] query budget stop persistence failed: {type(ledger_error).__name__}; trace={trace_id}", flush=True)

            def confirmed_persistence_failure():
                # Do not retain an exception traceback (and its prompt locals)
                # in the long-lived Runtime. The current caller gets the actual
                # receipt; future calls need only the confirmed identity latch.
                self._post_delivery_failure = (message.message_id, tuple(receipt.message_ids))
                return ConfirmedDeliveryPersistenceError(message.message_id, tuple(receipt.message_ids), receipt=receipt)
            try:
                async with asyncio.timeout(turn_seconds):
                    if type(self.adapter) is SessionBudgetAdapter:
                        turn_identity = hashlib.sha256((message.scope + "\0" + message.message_id).encode("utf-8")).hexdigest()
                        turn_adapter = self.adapter.begin_turn(turn_identity)
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
                        try:
                            with npmi_telemetry:
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
                                    primary_selection_error = None
                                    try:
                                        deadline_setter = getattr(self.model_selector, "set_deadlines", None)
                                        if deadline_setter is not None:
                                            if not callable(deadline_setter):
                                                raise GovernedError("invalid_selection_deadline_contract", remote_usage_unknown=False)
                                            deadline_setter(local_deadline=started + self.config.runtime.local_seconds,
                                                            turn_deadline=turn_deadline)
                                        with npmi_telemetry.suspended():
                                            if turn_adapter is None:
                                                decision = await self.model_selector.choose(prepared.request, trace_id)
                                            else:
                                                decision = await self.model_selector.choose(prepared.request, trace_id,
                                                                                           adapter_override=turn_adapter)
                                    except BaseException as selection_error:
                                        primary_selection_error = selection_error
                                        raise
                                    finally:
                                        try:
                                            selection_elapsed = time.monotonic() - selection_started
                                            local_work = getattr(self.model_selector, "last_local_seconds", 0.0)
                                            if (type(local_work) not in (int, float) or not math.isfinite(local_work)
                                                    or local_work < 0):
                                                raise GovernedError("invalid_selection_local_timing", remote_usage_unknown=False)
                                            local_work = min(local_work, selection_elapsed)
                                            started += selection_elapsed - local_work
                                            self.store.db.set_progress_handler(local_progress, 1000)
                                            if hasattr(self.model_selector, "last_local_seconds"):
                                                self.scratch.write("selection_local_work", trace_id=trace_id,
                                                    elapsed_seconds=selection_elapsed, local_work_seconds=local_work,
                                                    model_wait_seconds=selection_elapsed - local_work,
                                                    local_seconds=self.config.runtime.local_seconds)
                                        except Exception as timing_error:
                                            if primary_selection_error is None:
                                                raise
                                            # Secondary timing/audit errors cannot replace the
                                            # original failure, unknown usage or cancellation.
                                            try:
                                                print(f"[{self.config.name}] selection timing recording failed: {type(timing_error).__name__}; trace={trace_id}", flush=True)
                                            except Exception:
                                                pass
                                    if time.monotonic() - started > self.config.runtime.local_seconds:
                                        raise GovernedError("local_memory_timeout")
                                    local_phase = "selection_commit"
                                    records = self.graph.finish_retrieval(prepared, decision, audit=graph_audit)
                        finally:
                            record_initial_npmi(sys.exc_info()[1])
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
                        messages = await self._summary(message, knowledge, trace_id, adapter_override=turn_adapter)
                        record_answer = lambda text: answer_record(text, retrieval)
                        evidence_link = {"retrieval_sha256": retrieval_hash}
                        final_fields = {}
                    else:
                        from .active_requery import context_with_bundle
                        from .requery_records import answer_record_bundle
                        active.append(retrieval, f"{message.message_id}:initial")
                        messages = await self._summary(message, knowledge, trace_id, adapter_override=turn_adapter)
                        bundle = await active.gather(messages)
                        messages = context_with_bundle(messages, bundle,
                            limitation=REPLY_LIMITATION if active.final_reply_mode == "no_new_evidence_consolidation" else None)
                        record_answer = lambda text: answer_record_bundle(text, bundle)
                        evidence_link = {"bundle_sha256": digest(bundle)}
                        final_fields = {"final_origin": "bounded_stop" if active.fallback_text else "model",
                                        "simulation": turn_adapter.mock, **active.final_fields()}
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
                    if getattr(self.config.runtime, "path_hypotheses_enabled", False):
                        from .path_hypotheses import PathHypothesesConfig, analyze_path_hypotheses
                        path_input = bundle if active is not None else retrieval
                        path_actions = active.query_actions if active is not None else []
                        path_config = PathHypothesesConfig(enabled=True)
                        try:
                            path_receipt = analyze_path_hypotheses(path_input, path_actions, config=path_config,
                                deadline=min(turn_deadline, time.monotonic() + path_config.max_seconds))
                        except Exception as path_error:
                            path_receipt = {"schema": "automatic_path_hypotheses_v1", "status": "unknown",
                                            "reason": "diagnostic_error", "error_type": type(path_error).__name__,
                                            "proof": False, "changes_selection": False,
                                            "changes_base_weights": False}
                        self.scratch.write("path_hypotheses_diagnostic", trace_id=trace_id,
                            evidence_kind="bundle" if active is not None else "retrieval",
                            evidence_sha256=digest(path_input), query_actions=deepcopy(path_actions),
                            query_actions_sha256=digest(path_actions), receipt=path_receipt,
                            independent_diagnostic=True)
                    instructions = (prompts.bot_reply_instructions(self.config.name) if message.author_is_bot
                                    else prompts.reply_instructions(self.config.name))
                    if active is not None and active.final_reply_mode == "no_new_evidence_consolidation":
                        instructions += FINAL_REPLY_INSTRUCTIONS
                    schema_fields = {"response_schema": prompts.BOT_REPLY_SCHEMA} if message.author_is_bot else {}
                    self.scratch.write("reply_context", trace_id=trace_id, **evidence_link,
                                       instructions=instructions, messages=messages, **schema_fields, **final_fields,
                                       **({"no_provider_reply": bool(active.fallback_text)} if active else {}))
                    reply_adapter = turn_adapter if turn_adapter is not None else self.adapter
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
                            self.scratch.write("turn_end", trace_id=trace_id, status="skipped", reason="bot_reply_skipped",
                                               **final_state_fields("model_skip"))
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
                    self.scratch.write("turn_end", trace_id=trace_id, status="delivered", receipt={"ids": receipt.message_ids, "text": receipt.text},
                                       **final_state_fields("host_notice_delivered" if active and active.fallback_text else "model_reply_delivered"))
                    return
            except asyncio.CancelledError as error:
                record_initial_npmi(error)
                if confirmed:
                    confirmed_persistence_failure()
                    error.confirmed_delivery_receipt = receipt
                    error.message_id = message.message_id
                    try:
                        halt_budget("post_delivery_record_cancelled")
                        print(f"[{self.config.name}] Discord delivery confirmed; post-delivery recording interrupted; trace={trace_id}", flush=True)
                    except Exception:
                        pass
                    raise
                halt_budget("delivery_unknown" if delivered else "cancelled")
                code = "delivery_unknown" if delivered else "cancelled"
                self.store.fail(message.message_id, code)
                self.scratch.write("turn_end", trace_id=trace_id, status=code, **final_state_fields(code))
                raise
            except Exception as error:
                record_initial_npmi(error)
                if confirmed:
                    failure = confirmed_persistence_failure()
                    # A local audit failure cannot erase a confirmed Discord
                    # receipt or trigger another delivery. Preserve history.
                    try:
                        halt_budget("post_delivery_record_failed")
                        print(f"[{self.config.name}] Discord delivery confirmed; post-delivery storage/audit failed: {type(error).__name__}; trace={trace_id}; inspect records before restarting.", flush=True)
                    except Exception:
                        pass
                    raise failure from error
                code = error.code if isinstance(error, GovernedError) else "turn_timeout" if isinstance(error, TimeoutError) else type(error).__name__
                if delivered:
                    code = "delivery_unknown:" + code
                halt_budget(code)
                self.store.fail(message.message_id, code)
                diagnostic = error.__cause__ if isinstance(error.__cause__, sqlite3.OperationalError) else error
                failure_fields = {"error_type": type(diagnostic).__name__}
                if isinstance(error, GovernedError) and error.code == "requery_ledger_write_failed":
                    failure = (getattr(turn_adapter, "ledger_io_failure", None)
                               if turn_adapter is not None else None)
                    if failure is None:
                        failure = getattr(error, "ledger_io_failure", None)
                    if failure is not None:
                        try:
                            failure_fields["ledger_io_failure"] = validate_ledger_io_failure(failure)
                        except ValueError:
                            pass
                if isinstance(diagnostic, sqlite3.Error):
                    sqlite_code = getattr(diagnostic, "sqlite_errorcode", None)
                    sqlite_name = getattr(diagnostic, "sqlite_errorname", None)
                    if type(sqlite_code) is int:
                        failure_fields["sqlite_errorcode"] = sqlite_code
                    if isinstance(sqlite_name, str) and sqlite_name.startswith("SQLITE_") and sqlite_name.isascii() and sqlite_name.replace("_", "").isalnum():
                        failure_fields["sqlite_errorname"] = sqlite_name
                if local_phase is not None:
                    failure_fields.update(phase=local_phase, local_seconds=self.config.runtime.local_seconds)
                self.scratch.write("turn_end", trace_id=trace_id, status="failed", code=code,
                                   **final_state_fields("delivery_unknown" if delivered else "failed"), **failure_fields)
                print(f"[{self.config.name}] turn failed: {code}; trace={trace_id}", flush=True)
                if not delivered:
                    # A fixed failure receipt adds no model request. It is traced
                    # and kept separate from assistant conversational history.
                    try:
                        remaining = turn_deadline - time.monotonic()
                        if remaining <= 0:
                            self.scratch.write("failure_notice_skipped", trace_id=trace_id, reason="turn_time_exhausted")
                            return
                        notice = user_notice(code)
                        async with asyncio.timeout(min(self.config.discord.delivery_seconds, remaining)):
                            receipt = await deliver(FailureNotice(notice) if message.author_is_bot else notice)
                        confirmed = True
                        self.scratch.write("failure_notice_delivered", trace_id=trace_id,
                                           notice_policy=NOTICE_POLICY, reason=code,
                                           receipt={"ids": receipt.message_ids, "text": receipt.text})
                    except asyncio.CancelledError as cancellation:
                        if confirmed:
                            confirmed_persistence_failure()
                            cancellation.confirmed_delivery_receipt = receipt
                            cancellation.message_id = message.message_id
                        raise
                    except Exception as error:
                        if confirmed:
                            raise confirmed_persistence_failure() from error
                        self.scratch.write("failure_notice_unknown", trace_id=trace_id, error_type=type(error).__name__)
            finally:
                if turn_adapter is not None:
                    try:
                        turn_adapter.finish()
                    except Exception as error:
                        # Never convert a confirmed delivery into a second send.
                        if confirmed and getattr(self, "_post_delivery_failure", None) is None:
                            raise confirmed_persistence_failure() from error
                        if confirmed:
                            try:
                                print(f"[{self.config.name}] query budget ledger close failed: {type(error).__name__}; trace={trace_id}", flush=True)
                            except Exception:
                                pass
                        else:
                            print(f"[{self.config.name}] query budget ledger close failed: {type(error).__name__}; trace={trace_id}", flush=True)
