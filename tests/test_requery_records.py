"""New synthetic graph fixtures only; no external transport or stored questions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, RuntimeConfig
from indeces.context import encode
from indeces.contracts import GovernedError
from indeces.memory import MemoryGraph
from indeces.requery_records import (answer_record_bundle, append_retrieval,
    bundle_model_materials, validate_answer_bundle, validate_bundle, verify_active_turn)
from indeces.run_records import answer_record, digest, freeze_retrieval, validate_answer, verify_runs
from indeces.scratch import ScratchLog
from indeces.store import Store
from indeces.user_notices import NOTICE_POLICY, user_notice


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class SyntheticFixture(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.store = Store(self.root / "state")
        self.graph = MemoryGraph(self.store.db)
        self.scope = "synthetic:knowledge"
        self.graph.add(self.scope, "synthetic:alpha", "synthetic-source-alpha", [
            {"text": "Synthetic amber sensor observes a positive signal at time one.",
             "quote": "Synthetic amber sensor observes a positive signal at time one.",
             "marks": ["amber", "sensor"]}], 1.0)
        self.graph.add(self.scope, "synthetic:beta", "synthetic-source-beta", [
            {"text": "Synthetic violet valve does not open before time two.",
             "quote": "Synthetic violet valve does not open before time two.",
             "marks": ["violet", "valve"]}], 1.0)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def retrieval(self, query="amber sensor", event_id="synthetic-message"):
        audit = {}
        records = self.graph.retrieve(self.scope, [], query, 2.0, event_id=event_id,
                                      audit=audit, ranking_mode="static")
        return freeze_retrieval(self.store.db, self.scope, event_id, query, records, audit)

    def initial(self, query="amber sensor"):
        return append_retrieval(None, self.retrieval(query), round_index=0,
                                request_id="synthetic-message:initial", planning_call_id=None)


class EvidenceBundleTests(SyntheticFixture):
    def test_planning_context_references_complete_materials_preserving_history_and_question(self):
        from indeces.active_requery import context_with_bundle
        bundle = self.initial()
        materials = bundle_model_materials(bundle)
        historical = {"continuity_summary": "Synthetic preceding summary.",
                      "recent_messages": [{"role": "user", "content": "Synthetic previous question."}],
                      "memory_citations": [{"quote": "Synthetic stale context material."}]}
        messages = [{"role": "user", "content": "Historical context data:\n" + encode(historical)},
                    {"role": "user", "content": "Synthetic immutable current question."}]
        before = deepcopy(messages)
        planning = context_with_bundle(messages, bundle, evidence_ref=True)
        context = json.loads(planning[0]["content"].split("\n", 1)[1])
        self.assertEqual(context, {
            "continuity_summary": historical["continuity_summary"],
            "recent_messages": historical["recent_messages"],
            "memory_citations_ref": {"schema": "active_requery_planning_evidence_ref_v1",
                "version": 1, "field": "evidence", "sha256": digest(materials)}})
        self.assertEqual(planning[1:], messages[1:])
        self.assertEqual(messages, before)
        body = json.dumps({"evidence": materials, "history_context": planning}, ensure_ascii=False)
        for material in materials:
            self.assertEqual(body.count('"quote": ' + json.dumps(material["quote"], ensure_ascii=False)), 1)
        reply = context_with_bundle(messages, bundle)
        final_context = json.loads(reply[0]["content"].split("\n", 1)[1])
        self.assertEqual(final_context, {**historical, "memory_citations": materials})
        self.assertNotIn("memory_citations_ref", final_context)
        self.assertEqual(reply[1:], before[1:])

    def test_two_rounds_keep_complete_quotes_and_stable_global_citations(self):
        initial = self.initial()
        before = deepcopy(initial)
        retrieval = self.retrieval("violet valve", "synthetic-message:query:1")
        native_before = deepcopy(retrieval)
        extended = append_retrieval(initial, retrieval, round_index=1,
                                   request_id="synthetic-message:query:1", planning_call_id="synthetic-plan-1")
        self.assertEqual(initial, before)
        self.assertEqual(retrieval, native_before)
        self.assertEqual(extended["rounds"][1]["parent_evidence_sha256"], digest(initial))
        materials = bundle_model_materials(extended)
        self.assertEqual([m["citation_id"] for m in materials], ["M1", "M2"])
        self.assertEqual(materials[1]["local_citation_id"], "M1")
        self.assertEqual(materials[1]["source_id"], "synthetic:beta")
        self.assertEqual(materials[1]["quote"], retrieval["materials"][0]["quote"])
        self.assertEqual(len(retrieval["materials"]), 1)
        validate_bundle(extended)
        record = answer_record_bundle("Synthetic statement [M1].\n\nSynthetic denial [M2]. [M9]", extended)
        validate_answer_bundle(record, extended)
        self.assertEqual(record["version"], 3)
        self.assertEqual(record["cited_material_ids"], ["M1", "M2"])
        self.assertEqual(record["unresolved_markers"], ["[M9]"])
        self.assertEqual(record["citations"][1]["request_id"], "synthetic-message:query:1")
        self.assertEqual(record["semantic_support"], "not_evaluated")

    def test_repeated_material_reuses_first_version_without_overwrite(self):
        initial = self.initial()
        repeat = self.retrieval("amber", "synthetic-message:query:1")
        repeated = append_retrieval(initial, repeat, round_index=1,
                                   request_id="synthetic-message:query:1", planning_call_id="synthetic-plan-1")
        self.assertEqual(len(repeated["materials"]), 1)
        self.assertEqual(repeated["materials"], initial["materials"])
        self.assertEqual(repeated["rounds"][1]["bindings"][0]["citation_id"], "M1")
        self.assertEqual(bundle_model_materials(repeated), bundle_model_materials(initial))
        validate_bundle(repeated)

    def test_source_version_digest_keeps_same_record_identity_distinct(self):
        initial_retrieval = self.retrieval()
        initial_retrieval["sources"]["synthetic:alpha"]["original_file_bytes_sha256"] = "a" * 64
        initial = append_retrieval(None, initial_retrieval, round_index=0,
                                  request_id="synthetic-message:initial", planning_call_id=None)
        changed = self.retrieval("amber", "synthetic-message:query:1")
        changed["sources"]["synthetic:alpha"]["original_file_bytes_sha256"] = "b" * 64
        extended = append_retrieval(initial, changed, round_index=1,
                                   request_id="synthetic-message:query:1", planning_call_id="synthetic-plan-1")
        self.assertEqual(len(extended["materials"]), 2)
        self.assertEqual(extended["materials"][0]["material"]["record_id"],
                         extended["materials"][1]["material"]["record_id"])
        self.assertNotEqual(extended["materials"][0]["evidence_uid"], extended["materials"][1]["evidence_uid"])
        validate_bundle(extended)

    def test_empty_results_are_recorded_without_erasing_earlier_evidence(self):
        initial = self.initial()
        empty = self.retrieval("synthetic-nothing", "synthetic-message:query:1")
        self.assertEqual(empty["materials"], [])
        extended = append_retrieval(initial, empty, round_index=1,
                                   request_id="synthetic-message:query:1", planning_call_id="synthetic-plan-1")
        self.assertEqual(extended["materials"], initial["materials"])
        self.assertEqual(extended["rounds"][1]["bindings"], [])
        validate_bundle(extended)
        validate_bundle(append_retrieval(None, empty, round_index=0,
                                        request_id="synthetic-empty:initial", planning_call_id=None))

    def test_tampered_parent_mapping_and_quote_are_rejected(self):
        initial = self.initial()
        extended = append_retrieval(initial, self.retrieval("violet", "synthetic-message:query:1"),
                                   round_index=1, request_id="synthetic-message:query:1",
                                   planning_call_id="synthetic-plan-1")
        for change in ("parent", "global", "quote", "record_digest"):
            damaged = deepcopy(extended)
            if change == "parent":
                damaged["rounds"][1]["parent_evidence_sha256"] = "0" * 64
            elif change == "global":
                damaged["materials"][0]["citation_id"] = "M8"
            elif change == "quote":
                damaged["materials"][0]["material"]["quote"] = "synthetic replacement"
            else:
                damaged["rounds"][0]["record_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_bundle(damaged)

    def test_invalid_round_request_call_and_native_audit_are_rejected(self):
        initial = self.initial()
        native = self.retrieval("violet", "synthetic-message:query:1")
        for kwargs in (
            {"round_index": 2, "request_id": "synthetic-message:query:1", "planning_call_id": "plan"},
            {"round_index": True, "request_id": "synthetic-message:query:1", "planning_call_id": "plan"},
            {"round_index": 1, "request_id": "synthetic-message:initial", "planning_call_id": "plan"},
            {"round_index": 1, "request_id": "synthetic-message:query:1", "planning_call_id": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                append_retrieval(initial, native, **kwargs)
        damaged = deepcopy(native)
        damaged["graph_audit_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            append_retrieval(initial, damaged, round_index=1,
                             request_id="synthetic-message:query:1", planning_call_id="plan")

    def test_old_single_round_contract_remains_valid(self):
        native = self.retrieval()
        old = answer_record("Synthetic [M1]", native)
        validate_answer(old, native)
        self.assertEqual(old["version"], 1)
        self.assertNotIn("bundle_sha256", old)
        with self.assertRaises(ValueError):
            validate_answer_bundle(old, self.initial())


class ActiveTurnVerificationTests(SyntheticFixture):
    def synthetic_ledger(self, scratch):
        calls = []
        for event, start in scratch.events:
            if event != "call_start":
                continue
            end = next((f for e, f in scratch.events if e in {"call_end", "call_rejected"}
                        and f["call_id"] == start["call_id"]), {})
            paths = [f["path"] for e, f in scratch.events if e == "http_request"
                     and f["call_id"] == start["call_id"]]
            calls.append({"call_id": start["call_id"], "stage": start["stage"],
                          "status": end.get("status", "admitted"),
                          "reserved_input_tokens": start["budget"]["input_tokens"],
                          "reserved_output_tokens": start["budget"]["output_tokens"],
                          "reserved_cost_usd": "0", "generation_started": "/responses" in paths,
                          "usage": end.get("known_usage"), "transport_intents": paths})
        return {"schema": "active_requery_budget_v1", "trace_id": "synthetic-trace", "mode": "mock",
                "phase": "open", "limits": {"calls": 6, "input_tokens": 65536,
                    "output_tokens": 8192, "seconds": 60, "cost_usd": 0}, "calls": calls,
                "input_tokens": sum(c["usage"]["input_tokens"] for c in calls if c["usage"]),
                "output_tokens": sum(c["usage"]["output_tokens"] for c in calls if c["usage"]),
                "estimated_cost_usd": "0", "cost_basis": "synthetic_zero", "elapsed_seconds": 0,
                "halted": "requery_usage_unknown" if any(c["generation_started"] and c["usage"] is None for c in calls) else None,
                "automatic_retries": 0}

    def start(self, scratch, *, bot=False, notice_policy=None):
        fields = {"trace_id": "synthetic-trace", "message_id": "synthetic-message",
                  "run_record_version": 3, "knowledge_scope": self.scope,
                  "input": {"text": "amber sensor", "author_id": "synthetic-author"}}
        if bot:
            fields["input"]["author_is_bot"] = True
        if notice_policy is not None:
            fields["notice_policy"] = notice_policy
        scratch.write("turn_start", **fields)
        return fields

    def initial_events(self, scratch):
        native = self.retrieval()
        scratch.write("memory_observation", trace_id="synthetic-trace", audit=native["graph_audit"],
                      audit_sha256=native["graph_audit_sha256"])
        scratch.write("retrieval_record", trace_id="synthetic-trace", record=native, record_sha256=digest(native))
        bundle = append_retrieval(None, native, round_index=0,
                                  request_id="synthetic-message:initial", planning_call_id=None)
        scratch.write("requery_retrieval", trace_id="synthetic-trace", round_index=0,
                      request_id="synthetic-message:initial", planning_call_id=None, record=native,
                      record_sha256=digest(native), parent_evidence_sha256=None)
        scratch.write("requery_evidence", trace_id="synthetic-trace", bundle=bundle, bundle_sha256=digest(bundle))
        return bundle

    def context(self, bundle):
        return [{"role": "user", "content": "Historical context data:\n" + encode({
            "memory_citations": bundle_model_materials(bundle)})}]

    def host_finish(self, scratch, bundle, *, reason="query_budget", bot=False, ledger=None,
                    notice_policy=None):
        stop_ledger = self.synthetic_ledger(scratch) if ledger is None else ledger
        notice_fields = {}
        if notice_policy is not None:
            stop_ledger = deepcopy(stop_ledger)
            if stop_ledger["halted"] is None:
                stop_ledger["halted"] = reason
            notice_fields = {"fallback": True, "notice_policy": notice_policy}
        scratch.write("requery_stop", trace_id="synthetic-trace", reason=reason,
                      ledger=stop_ledger, **notice_fields)
        scratch.write("reply_context", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      instructions="Synthetic instruction", messages=self.context(bundle),
                      no_provider_reply=True, final_origin="bounded_stop")
        answer = user_notice(reason) if notice_policy is not None else "Synthetic bounded stop. [M1]"
        record = answer_record_bundle(answer, bundle)
        scratch.write("answer_generated", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      record=record, model_result=None, final_origin="bounded_stop")
        scratch.write("delivery_start", trace_id="synthetic-trace", text=answer)
        scratch.write("answer_delivered", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      record=record, receipt_ids=["synthetic-receipt"], final_origin="bounded_stop")
        scratch.write("turn_end", trace_id="synthetic-trace", status="delivered",
                      receipt={"text": answer, "ids": ["synthetic-receipt"]})

    async def model_call(self, scratch, stage, output, messages, schema=None):
        async def mock_request(path, payload):
            if path == "/responses/input_tokens":
                return {"input_tokens": 20}
            self.assertEqual(path, "/responses")
            return {"id": "synthetic-provider-result", "status": "completed",
                    "usage": {"input_tokens": 20, "output_tokens": 20}, "output": [
                        {"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": output}]}]}
        config = AdapterConfig("gpt-6.1-sol", "https://synthetic.invalid", {
            item: Budget(65536, 2048, 5, reasoning="medium") for item in ("query", "reply", "summary", "selection")})
        adapter = OpenAIAdapter(config, scratch, request=mock_request)
        try:
            if stage == "query":
                from indeces.active_requery import ACTION_SCHEMA, INSTRUCTIONS
                return await adapter.call(stage, INSTRUCTIONS, messages, "synthetic-trace", ACTION_SCHEMA)
            return await adapter.call(stage, "Synthetic instruction", messages, "synthetic-trace", schema)
        finally:
            await adapter.close()

    def query_round(self, scratch, bundle, *, planning_context=None, planning_materials=None):
        action = {"action": "query", "query": {
            "entities": [{"id": "e1", "surface": "amber", "canonical": "violet", "span": [0, 5]}],
            "relations": [], "negations": [], "time": None, "scope": None,
            "canonical_terms": ["violet", "valve"], "ambiguities": []},
            "missing_evidence": ["Synthetic follow-up evidence"], "clarification": "", "stop_reason": ""}
        payload = {"original_text": "amber sensor", "round_index": 0,
                   "evidence": bundle_model_materials(bundle) if planning_materials is None else planning_materials,
                   "remaining_query_rounds": 2,
                   "history_context": self.context(bundle) if planning_context is None else planning_context}
        asyncio.run(self.model_call(scratch, "query", json.dumps(action), [
            {"role": "user", "content": json.dumps(payload)}]))
        call_id = next(f["call_id"] for e, f in reversed(scratch.events) if e == "call_end")
        scratch.write("requery_action", trace_id="synthetic-trace", round_index=0,
                      planning_call_id=call_id, action=action, action_sha256=digest(action))
        query_text = "amber sensor\nCanonical query data:\n" + json.dumps(action["query"], ensure_ascii=False, sort_keys=True)
        native = self.retrieval(query_text, "synthetic-message:query:1")
        extended = append_retrieval(bundle, native, round_index=1,
                                   request_id="synthetic-message:query:1", planning_call_id=call_id)
        scratch.write("requery_retrieval", trace_id="synthetic-trace", round_index=1,
                      request_id="synthetic-message:query:1", planning_call_id=call_id,
                      record=native, record_sha256=digest(native), parent_evidence_sha256=digest(bundle))
        scratch.write("requery_evidence", trace_id="synthetic-trace", bundle=extended, bundle_sha256=digest(extended))
        return extended

    def planning_reference_context(self, bundle):
        data = {"memory_citations_ref": {
            "schema": "active_requery_planning_evidence_ref_v1", "version": 1,
            "field": "evidence", "sha256": digest(bundle_model_materials(bundle))},
            "continuity_summary": "Synthetic continuity is preserved.",
            "recent_messages": [{"role": "user", "content": "Synthetic earlier turn."}]}
        return [{"role": "user", "content": "Historical context data:\n" + encode(data)},
                {"role": "user", "content": "Synthetic original question remains present."}]

    def test_reference_planning_chain_and_legacy_duplicate_chain_both_verify(self):
        for reference in (False, True):
            with self.subTest(reference=reference):
                scratch = Scratch()
                start = self.start(scratch)
                initial = self.initial_events(scratch)
                context = self.planning_reference_context(initial) if reference else self.context(initial)
                bundle = self.query_round(scratch, initial, planning_context=context)
                self.host_finish(scratch, bundle)
                self.assertEqual(verify_active_turn(scratch.events, start)["status"], "complete")
                request = next(f["payload"] for e, f in scratch.events
                               if e == "http_request" and f["path"] == "/responses")
                data = json.loads(request["input"][0]["content"])
                self.assertEqual(data["original_text"], start["input"]["text"])
                self.assertEqual(data["history_context"], context)
                self.assertEqual(data["evidence"], bundle_model_materials(initial))

    def test_planning_reference_missing_wrong_or_mixed_bindings_are_rejected(self):
        for change in ("missing", "digest", "schema", "version", "bool-version", "field",
                       "extra-key", "mixed", "wrong-type", "wrong-history-type", "wrong-history-role"):
            with self.subTest(change=change):
                scratch = Scratch()
                start = self.start(scratch)
                initial = self.initial_events(scratch)
                context = self.planning_reference_context(initial)
                data = json.loads(context[0]["content"].split("\n", 1)[1])
                ref = data["memory_citations_ref"]
                if change == "missing":
                    del data["memory_citations_ref"]
                elif change == "mixed":
                    data["memory_citations"] = bundle_model_materials(initial)
                elif change == "wrong-type":
                    data["memory_citations_ref"] = []
                elif change == "extra-key":
                    ref["unbound"] = "Synthetic field"
                elif change == "bool-version":
                    ref["version"] = True
                elif change not in {"wrong-history-type", "wrong-history-role"}:
                    ref[change] = "0" * 64 if change == "digest" else "synthetic invalid"
                    if change == "digest":
                        ref["sha256"] = ref.pop("digest")
                context[0]["content"] = "Historical context data:\n" + encode(data)
                if change == "wrong-history-type":
                    context = {}
                elif change == "wrong-history-role":
                    context[0]["role"] = "assistant"
                bundle = self.query_round(scratch, initial, planning_context=context)
                self.host_finish(scratch, bundle)
                with self.assertRaisesRegex(ValueError, "active planning history (evidence|context) mismatch"):
                    verify_active_turn(scratch.events, start)

    def test_planning_reference_cannot_rebind_a_replaced_quote(self):
        scratch = Scratch()
        start = self.start(scratch)
        initial = self.initial_events(scratch)
        materials = bundle_model_materials(initial)
        materials[0]["quote"] = "Synthetic replacement must not supersede the frozen quote."
        context = self.planning_reference_context(initial)
        data = json.loads(context[0]["content"].split("\n", 1)[1])
        data["memory_citations_ref"]["sha256"] = digest(materials)
        context[0]["content"] = "Historical context data:\n" + encode(data)
        bundle = self.query_round(scratch, initial, planning_context=context, planning_materials=materials)
        self.host_finish(scratch, bundle)
        with self.assertRaisesRegex(ValueError, "active planning question/round/evidence binding mismatch"):
            verify_active_turn(scratch.events, start)

    def test_budget_stop_host_delivery_is_not_misclassified_as_model_reply(self):
        for bot in (False, True):
            scratch = Scratch()
            start = self.start(scratch, bot=bot)
            bundle = self.initial_events(scratch)
            self.host_finish(scratch, bundle, bot=bot)
            report = verify_active_turn(scratch.events, start)
            self.assertEqual(report["status"], "complete")
            self.assertFalse(any(f.get("stage") == "reply" for _, f in scratch.events))

    def test_natural_notice_and_unmarked_historical_host_receipts_both_verify(self):
        for policy in (None, NOTICE_POLICY):
            with self.subTest(policy=policy):
                scratch = Scratch()
                start = self.start(scratch, notice_policy=policy)
                bundle = self.initial_events(scratch)
                self.host_finish(scratch, bundle, reason="input_token_limit", notice_policy=policy)
                self.assertEqual(verify_active_turn(scratch.events, start)["status"], "complete")
                stop = next(f for e, f in scratch.events if e == "requery_stop")
                self.assertEqual(stop["reason"], "input_token_limit")
                self.assertEqual("notice_policy" in stop, policy is not None)

    def test_natural_notice_policy_cannot_be_changed_or_removed_from_new_turn(self):
        scratch = Scratch()
        start = self.start(scratch, notice_policy=NOTICE_POLICY)
        bundle = self.initial_events(scratch)
        self.host_finish(scratch, bundle, reason="input_token_limit", notice_policy=NOTICE_POLICY)
        for value in (None, "unknown_policy", True, "removed"):
            with self.subTest(policy=value):
                damaged = deepcopy(scratch.events)
                stop = next(f for e, f in damaged if e == "requery_stop")
                if value == "removed":
                    del stop["notice_policy"]
                else:
                    stop["notice_policy"] = value
                with self.assertRaisesRegex(ValueError, "notice policy"):
                    verify_active_turn(damaged, start)
                with self.assertRaisesRegex(ValueError, "notice policy"):
                    verify_active_turn(damaged, partial=True)

    def test_new_host_policy_binds_the_recorded_start_and_retained_suffix_is_allowed(self):
        scratch = Scratch()
        start = self.start(scratch, notice_policy=NOTICE_POLICY)
        bundle = self.initial_events(scratch)
        self.host_finish(scratch, bundle, reason="input_token_limit", notice_policy=NOTICE_POLICY)
        for supplied_start_changed in (False, True):
            with self.subTest(supplied_start_changed=supplied_start_changed):
                damaged = deepcopy(scratch.events)
                del next(f for e, f in damaged if e == "turn_start")["notice_policy"]
                supplied = deepcopy(start)
                if supplied_start_changed:
                    del supplied["notice_policy"]
                with self.assertRaisesRegex(ValueError, "notice turn policy missing or downgraded"):
                    verify_active_turn(damaged, supplied)
        suffix = [(event, fields) for event, fields in scratch.events if event != "turn_start"]
        self.assertEqual(verify_active_turn(suffix, partial=True)["status"], "retention_partial")

    def test_self_consistent_forged_notice_and_missing_stop_cause_are_rejected(self):
        scratch = Scratch()
        start = self.start(scratch, notice_policy=NOTICE_POLICY)
        bundle = self.initial_events(scratch)
        self.host_finish(scratch, bundle, reason="input_token_limit", notice_policy=NOTICE_POLICY)
        forged = deepcopy(scratch.events)
        for event, fields in forged:
            if event in {"answer_generated", "answer_delivered"}:
                fields["record"] = answer_record_bundle("Synthetic unsupported answer.", bundle)
            elif event == "delivery_start":
                fields["text"] = "Synthetic unsupported answer."
            elif event == "turn_end":
                fields["receipt"]["text"] = "Synthetic unsupported answer."
        with self.assertRaisesRegex(ValueError, "notice template mismatch"):
            verify_active_turn(forged, start)
        for changed in ("reason", "halt"):
            with self.subTest(cause=changed):
                damaged = deepcopy(scratch.events)
                stop = next(f for e, f in damaged if e == "requery_stop")
                if changed == "reason":
                    del stop["reason"]
                else:
                    stop["ledger"]["halted"] = None
                with self.assertRaisesRegex(ValueError, "notice lost stop reason or halt"):
                    verify_active_turn(damaged, start)

    def test_retained_natural_notice_checks_template_without_full_prefix(self):
        scratch = Scratch()
        start = self.start(scratch, notice_policy=NOTICE_POLICY)
        bundle = self.initial_events(scratch)
        self.host_finish(scratch, bundle, reason="input_token_limit", notice_policy=NOTICE_POLICY)
        suffix = [(e, f) for e, f in scratch.events if e != "turn_start"]
        self.assertEqual(verify_active_turn(suffix, partial=True)["status"], "retention_partial")
        damaged = deepcopy(suffix)
        next(f for e, f in damaged if e == "answer_generated")["record"]["text"] = "Synthetic forgery"
        with self.assertRaisesRegex(ValueError, "notice template mismatch"):
            verify_active_turn(damaged, partial=True)

    def test_two_rounds_and_real_mock_transport_reply_verify_through_public_reader(self):
        scratch = Scratch()
        start = self.start(scratch)
        bundle = self.query_round(scratch, self.initial_events(scratch))
        scratch.write("requery_stop", trace_id="synthetic-trace", reason="answer", ledger=self.synthetic_ledger(scratch))
        messages = self.context(bundle)
        scratch.write("reply_context", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      instructions="Synthetic instruction", messages=messages)
        result = asyncio.run(self.model_call(scratch, "reply", "Synthetic answer [M1] and [M2].", messages))
        scratch.write("requery_budget_event", trace_id="synthetic-trace", budget_event="call_end",
                      provider_call_id=next(f["call_id"] for e, f in reversed(scratch.events) if e == "call_end"),
                      ledger=self.synthetic_ledger(scratch))
        answer = answer_record_bundle(result.text, bundle)
        scratch.write("answer_generated", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      record=answer, model_result=asdict(result))
        scratch.write("delivery_start", trace_id="synthetic-trace", text=result.text)
        scratch.write("answer_delivered", trace_id="synthetic-trace", bundle_sha256=digest(bundle),
                      record=answer, receipt_ids=["synthetic-receipt"])
        scratch.write("turn_end", trace_id="synthetic-trace", status="delivered",
                      receipt={"text": result.text, "ids": ["synthetic-receipt"]})
        self.assertEqual(verify_active_turn(scratch.events, start)["status"], "complete")
        disk = ScratchLog(self.root / "synthetic-scratch")
        for event, fields in scratch.events:
            disk.write(event, **fields)
        disk.close()
        report = verify_runs(list((self.root / "synthetic-scratch").glob("*.jsonl")))
        self.assertEqual(report["counts"]["complete"], 1)
        self.assertEqual(report["call_counts"]["complete"], 2)
        self.assertEqual(report["issues"], [])

    def test_failed_query_unknown_usage_is_visible_and_does_not_hide_delivery(self):
        scratch = Scratch()
        start = self.start(scratch)
        bundle = self.initial_events(scratch)
        async def failure():
            async def mock_request(path, payload):
                if path == "/responses/input_tokens":
                    return {"input_tokens": 20}
                raise GovernedError("synthetic_transport_failure")
            adapter = OpenAIAdapter(AdapterConfig("gpt-6.1-sol", "https://synthetic.invalid", {
                "query": Budget(4096, 512, 5, reasoning="medium")}), scratch, request=mock_request)
            try:
                await adapter.call("query", "Synthetic instruction", [{"role": "user", "content": "Synthetic"}], "synthetic-trace")
            except GovernedError:
                pass
            finally:
                await adapter.close()
        asyncio.run(failure())
        self.host_finish(scratch, bundle, reason="unknown_usage")
        report = verify_active_turn(scratch.events, start)
        self.assertEqual(report["status"], "complete")
        self.assertIn("remote_generation_usage_unknown", report["warnings"])
        self.assertIn("active_query_or_other_call_failed", report["warnings"])
        damaged = deepcopy(scratch.events)
        next(f["ledger"] for e, f in damaged if e == "requery_stop")["halted"] = None
        with self.assertRaisesRegex(ValueError, "unknown generation usage"):
            verify_active_turn(damaged, start)
        hidden_intent = deepcopy(scratch.events)
        hidden_ledger = next(f["ledger"] for e, f in hidden_intent if e == "requery_stop")
        hidden_ledger["halted"] = None
        hidden_ledger["calls"][0]["generation_started"] = False
        hidden_ledger["calls"][0]["transport_intents"] = []
        with self.assertRaisesRegex(ValueError, "generation intent mismatch"):
            verify_active_turn(hidden_intent, start)

    def test_failed_before_initial_retrieval_is_valid_failed_turn(self):
        scratch = Scratch()
        start = self.start(scratch)
        scratch.write("turn_end", trace_id="synthetic-trace", status="failed", code="synthetic_failure")
        self.assertEqual(verify_active_turn(scratch.events, start)["status"], "failed")

    def test_retained_suffix_is_partial_even_with_self_contained_bundle(self):
        scratch = Scratch()
        self.start(scratch)
        bundle = self.initial_events(scratch)
        self.host_finish(scratch, bundle)
        suffix = [(e, f) for e, f in scratch.events if e not in {"turn_start", "memory_observation", "retrieval_record", "requery_retrieval"}]
        report = verify_active_turn(suffix, partial=True)
        self.assertEqual(report["status"], "retention_partial")
        self.assertIn("retained_active_query_contract", report["warnings"])
        damaged = deepcopy(suffix)
        evidence = next(f for e, f in damaged if e == "requery_evidence")
        evidence["bundle_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            verify_active_turn(damaged, partial=True)

    def test_action_call_and_append_order_tampering_are_rejected(self):
        scratch = Scratch()
        start = self.start(scratch)
        bundle = self.query_round(scratch, self.initial_events(scratch))
        self.host_finish(scratch, bundle, reason="synthetic-stop")
        for change in ("call", "action", "parent", "ordering", "host-origin", "early-stop", "early-delivery"):
            damaged = deepcopy(scratch.events)
            if change == "call":
                next(f for e, f in damaged if e == "requery_action")["planning_call_id"] = "missing"
            elif change == "action":
                fields = next(f for e, f in damaged if e == "requery_action")
                fields["action"]["query"]["canonical_terms"] = ["synthetic different"]
                fields["action_sha256"] = digest(fields["action"])
            elif change == "parent":
                next(f for e, f in damaged if e == "requery_retrieval" and f["round_index"] == 1)["parent_evidence_sha256"] = "0" * 64
            elif change == "ordering":
                position = next(i for i, (e, _) in enumerate(damaged) if e == "requery_action")
                damaged[position], damaged[position + 1] = damaged[position + 1], damaged[position]
            elif change == "early-stop":
                stop = next(item for item in damaged if item[0] == "requery_stop")
                damaged.remove(stop)
                position = next(i for i, (e, _) in enumerate(damaged) if e == "requery_action")
                damaged.insert(position, stop)
            elif change == "early-delivery":
                delivery = next(item for item in damaged if item[0] == "answer_delivered")
                damaged.remove(delivery)
                position = next(i for i, (e, _) in enumerate(damaged) if e == "answer_generated")
                damaged.insert(position, delivery)
            else:
                next(f for e, f in damaged if e == "answer_generated")["final_origin"] = "provider"
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_active_turn(damaged, start)

    def wrapped_query_fixture(self):
        from indeces.active_requery import ACTION_SCHEMA, INSTRUCTIONS, TurnBudgetAdapter
        scratch = Scratch()
        start = self.start(scratch)
        bundle = self.initial_events(scratch)
        async def execute():
            async def mock_request(path, payload):
                if path == "/responses/input_tokens":
                    return {"input_tokens": 20}
                return {"id": "synthetic-wrapped-result", "status": "completed",
                        "usage": {"input_tokens": 20, "output_tokens": 20}, "output": [
                            {"type": "message", "role": "assistant", "content": [
                                {"type": "output_text", "text": "Synthetic invalid action"}]}]}
            adapter = OpenAIAdapter(AdapterConfig("gpt-6.1-sol", "https://synthetic.invalid", {
                "query": Budget(4096, 512, 5, reasoning="medium")}), scratch, request=mock_request)
            adapter.offline_mock = True
            config = SimpleNamespace(runtime=RuntimeConfig(active_requery_enabled=True),
                                     state_dir=self.root / "synthetic-wrapper-state")
            wrapper = TurnBudgetAdapter(adapter, config, scratch, "synthetic-trace",
                                        "synthetic-message", "synthetic-conversation")
            try:
                await wrapper.call("query", INSTRUCTIONS, [{"role": "user", "content": "Synthetic"}],
                                   "synthetic-trace", ACTION_SCHEMA)
                wrapper.halt("invalid_query_action")
                return wrapper
            finally:
                await adapter.close()
        wrapper = asyncio.run(execute())
        self.host_finish(scratch, bundle, reason="invalid_query_action", ledger=wrapper.snapshot())
        wrapper.finish()
        return scratch, start

    def test_durable_mock_budget_receipts_bind_usage_calls_and_admission(self):
        scratch, start = self.wrapped_query_fixture()
        report = verify_active_turn(scratch.events, start)
        self.assertEqual(report["status"], "complete")
        self.assertNotIn("active_cumulative_budget_evidence_unavailable", report["warnings"])
        ledgers = [f["ledger"] for e, f in scratch.events if e == "requery_budget_event"]
        self.assertGreater(len(ledgers), 3)
        self.assertEqual(ledgers[-1]["input_tokens"], 20)
        self.assertEqual(ledgers[-1]["output_tokens"], 20)
        self.assertEqual(ledgers[-1]["estimated_cost_usd"], "0")
        self.assertEqual(ledgers[-1]["mode"], "mock")

    def test_durable_ledger_cannot_rewrite_usage_reservations_or_hide_calls(self):
        scratch, start = self.wrapped_query_fixture()
        for change in ("usage", "reservation", "hidden-call", "changed-cap", "fake-simulation", "pretend-pending"):
            damaged = deepcopy(scratch.events)
            ledger = next(f["ledger"] for e, f in damaged if e == "requery_stop")
            if change == "usage":
                ledger["calls"][0]["usage"]["input_tokens"] += 1
                ledger["input_tokens"] += 1
            elif change == "reservation":
                ledger["calls"][0]["reserved_input_tokens"] += 1
            elif change == "hidden-call":
                ledger["calls"] = []
                ledger["input_tokens"] = ledger["output_tokens"] = 0
            elif change == "changed-cap":
                ledger["limits"]["input_tokens"] += 1
            elif change == "pretend-pending":
                for event, fields in damaged:
                    if event not in {"requery_stop", "requery_budget_event"}:
                        continue
                    pending = fields["ledger"]
                    for entry in pending["calls"]:
                        entry["status"] = "admitted"
                        entry["usage"] = None
                    pending["input_tokens"] = pending["output_tokens"] = 0
            else:
                fields = next(f for e, f in damaged if e == "requery_stop")
                fields["ledger"] = {"simulated": True}
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_active_turn(damaged, start)

    def stopped_budget_fixture(self):
        from indeces.active_requery import ACTION_SCHEMA, INSTRUCTIONS, TurnBudgetAdapter
        scratch = Scratch()
        start = self.start(scratch)
        bundle = self.initial_events(scratch)
        requests = []
        async def execute():
            async def forbidden_mock_transport(path, payload):
                requests.append(path)
                raise AssertionError("budget rejection must precede mock transport")
            adapter = OpenAIAdapter(AdapterConfig("gpt-6.1-sol", "https://synthetic.invalid", {
                "query": Budget(4096, 512, 5, reasoning="medium")}), scratch,
                request=forbidden_mock_transport)
            adapter.offline_mock = True
            config = SimpleNamespace(runtime=RuntimeConfig(active_requery_enabled=True,
                active_requery_max_calls=1), state_dir=self.root / "synthetic-stopped-state")
            wrapper = TurnBudgetAdapter(adapter, config, scratch, "synthetic-trace",
                                        "synthetic-message", "synthetic-conversation")
            try:
                with self.assertRaises(GovernedError) as captured:
                    await wrapper.call("query", INSTRUCTIONS,
                        [{"role": "user", "content": "Synthetic budget stop"}], "synthetic-trace", ACTION_SCHEMA)
                self.assertEqual(captured.exception.code, "requery_call_budget")
                self.assertEqual(wrapper.halted, "requery_call_budget")
                return wrapper
            finally:
                await adapter.close()
        wrapper = asyncio.run(execute())
        self.host_finish(scratch, bundle, reason="requery_call_budget", ledger=wrapper.snapshot())
        wrapper.finish()
        persisted = json.loads(wrapper.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["phase"], "stopped")
        self.assertEqual(persisted["halted"], "requery_call_budget")
        self.assertEqual(persisted["calls"], [])
        self.assertEqual(requests, [])
        return scratch, start

    def test_real_mock_budget_reject_persists_stopped_phase_and_verifies_after_turn_end(self):
        scratch, start = self.stopped_budget_fixture()
        report = verify_active_turn(scratch.events, start)
        self.assertEqual(report["status"], "complete")
        terminal = scratch.events[-1]
        self.assertEqual(terminal[0], "requery_budget_event")
        self.assertEqual(terminal[1]["budget_event"], "turn_end")
        self.assertIsNone(terminal[1]["provider_call_id"])
        self.assertEqual(terminal[1]["ledger"]["phase"], "stopped")
        disk = ScratchLog(self.root / "synthetic-stopped-scratch")
        for event, fields in scratch.events:
            disk.write(event, **fields)
        disk.close()
        result = verify_runs(list((self.root / "synthetic-stopped-scratch").glob("*.jsonl")))
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["counts"]["complete"], 1)
        self.assertEqual(result["calls"], [])

    def test_stopped_phase_needs_cause_and_rejects_reopen_provider_query_or_late_delivery(self):
        scratch, start = self.stopped_budget_fixture()
        for change in ("missing-cause", "fake-completed", "reopen", "provider", "query", "late-delivery", "bad-terminal-marker"):
            damaged = deepcopy(scratch.events)
            terminal = damaged[-1][1]
            if change == "missing-cause":
                terminal["ledger"]["halted"] = None
            elif change == "fake-completed":
                terminal["ledger"]["phase"] = "completed"
            elif change == "reopen":
                reopened = deepcopy(terminal)
                reopened["ledger"]["phase"] = "open"
                damaged.append(("requery_budget_event", reopened))
            elif change == "provider":
                damaged.append(("http_request", {"trace_id": "synthetic-trace",
                    "path": "/responses", "payload": {}}))
            elif change == "query":
                damaged.append(("requery_action", {"trace_id": "synthetic-trace", "round_index": 0,
                    "planning_call_id": "synthetic-new-plan", "action": {}, "action_sha256": digest({})}))
            elif change == "late-delivery":
                damaged.append(deepcopy(next(item for item in damaged if item[0] == "delivery_start")))
            else:
                terminal["budget_event"] = "call_end"
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_active_turn(damaged, start)
        suffix = [item for item in scratch.events if item[0] not in {
            "turn_start", "memory_observation", "retrieval_record", "requery_retrieval"}]
        self.assertEqual(verify_active_turn(suffix, partial=True)["status"], "retention_partial")
        suffix.append(("http_request", {"trace_id": "synthetic-trace", "path": "/responses", "payload": {}}))
        with self.assertRaises(ValueError):
            verify_active_turn(suffix, partial=True)
