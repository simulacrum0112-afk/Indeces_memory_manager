"""Offline regressions for the single bounded no-new-evidence final reply.

Every observation, question and answer here is a synthetic fixture. The native
Runtime and durable turn adapter run against a temporary Store and mock HTTP;
the inherited transport guards prohibit real network access.
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from indeces.active_requery import ActiveRequery, TurnBudgetAdapter
from indeces.contracts import GovernedError
from indeces import requery_records
from indeces.requery_records import bundle_model_materials, verify_active_turn
from indeces.run_records import digest, freeze_retrieval, verify_runs
from indeces.scratch import CanonicalSnapshot
from indeces.user_notices import NOTICE_POLICY
from tests import test_active_requery as legacy
from tests import test_requery_records as record_fixtures


class NoNewEvidenceFinalTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup/helpers without inheriting and rerunning the legacy test suite.
    setUp = legacy.ActiveConsoleTests.setUp
    asyncTearDown = legacy.ActiveConsoleTests.asyncTearDown
    deliver = legacy.ActiveConsoleTests.deliver
    entries = legacy.ActiveConsoleTests.entries
    event = legacy.ActiveConsoleTests.event
    run_turn = legacy.ActiveConsoleTests.run_turn
    saved_ledger = legacy.ActiveConsoleTests.saved_ledger

    async def transport(self, path, payload):
        response = await legacy.ActiveConsoleTests.transport(self, path, payload)
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        if path == "/responses" and stage == "reply":
            text = ("Synthetic observations [M1] and [M2] are available, but the "
                    "evidence is insufficient to establish the requested conclusion.")
            if payload.get("text", {}).get("format", {}).get("name") == "reply":
                text = json.dumps({"action": "reply", "text": text})
            response["output"][0]["content"][0]["text"] = text
        return response

    def prepare_no_new(self):
        # M1 is frozen initially; query one adds M2; query two retrieves M1
        # without adding evidence. Both queries preserve the synthetic anchor.
        self.message = replace(self.message, text="amber")
        self.config.runtime = replace(self.config.runtime, model_selection_enabled=False)
        self.runtime = legacy.create_runtime(self.config, self.store, self.adapter, self.scratch)
        self.outputs = [legacy.action("cobalt", "amber"),
                        legacy.action("amber", "amber")]

    def generation_stages(self):
        return [stage for path, stage, _ in self.requests if path == "/responses"]

    def trace_events(self):
        trace_id = self.event("turn_start")[0]["trace_id"]
        return [(row["event"], deepcopy(row["fields"])) for row in self.entries()[1]
                if row["fields"].get("trace_id") == trace_id]

    def assert_complete(self, status="complete"):
        report = verify_runs([self.entries()[0]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"][status], 1)

    def assert_policy(self, fields, mode, reason=None):
        self.assertEqual(fields["requery_final_policy"], requery_records.FINALIZATION_POLICY)
        self.assertEqual(fields["final_reply_mode"], mode)
        if reason is not None:
            self.assertEqual(fields["retrieval_stop_reason"], reason)

    async def test_no_new_replies_once_with_all_frozen_quotes_and_explicit_limitation(self):
        self.prepare_no_new()
        original_weights = list(self.store.db.execute("SELECT * FROM memory_static"))
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        stop = self.event("requery_stop")[0]
        self.assertEqual(stop["reason"], "no_new_evidence")
        self.assertFalse(stop["fallback"])
        self.assertNotIn("notice_policy", stop)
        self.assert_policy(stop, "no_new_evidence_consolidation")
        self.assertEqual(stop["ledger"]["phase"], "open")
        self.assertIsNone(stop["ledger"]["halted"])
        self.assertTrue(stop["ledger"]["usage_complete"])
        self.assertEqual(stop["ledger"]["unknown_generation_count"], 0)
        bundle = self.event("requery_evidence")[0]["bundle"]
        self.assertEqual(len(bundle["rounds"]), 3)
        materials = bundle_model_materials(bundle)
        self.assertEqual([(m["citation_id"], m["source_id"]) for m in materials],
                         [("M1", "synthetic:amber"), ("M2", "synthetic:cobalt")])
        reply = self.event("reply_context")[0]
        self.assert_policy(reply, "no_new_evidence_consolidation", "no_new_evidence")
        self.assertFalse(reply["no_provider_reply"])
        historical = json.loads(reply["messages"][0]["content"].split("\n", 1)[1])
        self.assertEqual(historical["memory_citations"], materials)
        self.assertEqual(historical["context_limitation"], requery_records.REPLY_LIMITATION)
        self.assertNotIn("memory_citations_ref", historical)
        for material in materials:
            self.assertEqual(material["quote"],
                f"Synthetic {material['source_id'].split(':')[1]} observation is a mock fixture.")
        request = next(payload for path, stage, payload in self.requests
                       if path == "/responses" and stage == "reply")
        self.assertEqual(request["instructions"], reply["instructions"])
        self.assertTrue(reply["instructions"].endswith(requery_records.FINAL_REPLY_INSTRUCTIONS))
        self.assertEqual(request["input"], reply["messages"])
        generated = self.event("answer_generated")[0]
        delivered = self.event("answer_delivered")[0]
        for fields in (generated, delivered):
            self.assert_policy(fields, "no_new_evidence_consolidation", "no_new_evidence")
            self.assertEqual(fields["bundle_sha256"], digest(bundle))
            self.assertEqual(fields["record"]["unresolved_markers"], [])
            self.assertEqual([c["citation_id"] for c in fields["record"]["citations"]],
                             ["M1", "M2"])
        self.assertIsNotNone(generated["model_result"])
        self.assertIn("evidence is insufficient", self.deliveries[0])
        self.assertEqual(len(self.deliveries), 1)
        end = self.event("turn_end")[0]
        self.assert_policy(end, "no_new_evidence_consolidation", "no_new_evidence")
        self.assertEqual(end["final_status"], "model_reply_delivered")
        ledger = self.saved_ledger()
        self.assertEqual((ledger["phase"], ledger["halted"], ledger["automatic_retries"]),
                         ("completed", None, 0))
        self.assertEqual(list(self.store.db.execute("SELECT * FROM memory_static")), original_weights)
        self.assert_complete()

    async def test_bot_uses_existing_reply_schema_for_limited_final_answer(self):
        self.prepare_no_new()
        self.message = replace(self.message, author_is_bot=True)
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        decision = self.event("bot_reply_decision")[0]
        self.assertEqual(decision["action"], "reply")
        self.assertIn("evidence is insufficient", decision["text"])
        self.assertEqual(self.deliveries, [decision["text"]])
        self.assertEqual(self.event("turn_end")[0]["final_status"], "model_reply_delivered")
        self.assert_complete()

    async def test_bot_final_skip_remains_silent_without_second_call(self):
        self.prepare_no_new()
        self.message = replace(self.message, author_is_bot=True)
        native = self.transport
        async def closing_reply(path, payload):
            response = await native(path, payload)
            if path == "/responses" and payload.get("text", {}).get("format", {}).get("name") == "reply":
                response["output"][0]["content"][0]["text"] = json.dumps({"action": "skip", "text": ""})
            return response
        self.adapter._request_override = closing_reply
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        self.assertEqual(self.event("bot_reply_decision")[0]["action"], "skip")
        self.assertEqual(self.deliveries, [])
        self.assertEqual(self.event("answer_generated"), [])
        self.assertEqual(self.event("turn_end")[0]["final_status"], "model_skip")
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)
        self.assert_complete("skipped")

    async def test_ordinary_answer_keeps_existing_policy_without_insufficiency_limit(self):
        self.outputs = [legacy.done()]
        await self.run_turn()
        self.assert_policy(self.event("requery_stop")[0], "ordinary_answer")
        reply = self.event("reply_context")[0]
        self.assert_policy(reply, "ordinary_answer", "evidence_sufficient")
        historical = json.loads(reply["messages"][0]["content"].split("\n", 1)[1])
        self.assertNotIn("context_limitation", historical)
        self.assertEqual(self.generation_stages(), ["query", "reply"])
        self.assert_complete()

    async def assert_host_stop(self, reason):
        await self.run_turn()
        stop = self.event("requery_stop")[0]
        self.assertEqual(stop["reason"], reason)
        self.assertTrue(stop["fallback"])
        self.assert_policy(stop, "host_notice")
        self.assertFalse(any(s == "reply" for s in self.generation_stages()))
        self.assertIsNone(self.event("answer_generated")[0]["model_result"])
        self.assertEqual(self.event("turn_end")[0]["final_status"], "host_notice_delivered")
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)
        self.assert_complete()

    async def test_empty_results_with_existing_evidence_remains_host_only(self):
        self.outputs = [legacy.action(), legacy.action("not-present")]
        await self.assert_host_stop("empty_results")
        self.assertTrue(bundle_model_materials(self.event("requery_evidence")[0]["bundle"]))

    async def test_duplicate_query_remains_host_only(self):
        self.outputs = [legacy.action(), legacy.action()]
        await self.assert_host_stop("duplicate_query")

    async def test_round_exhaustion_remains_host_only(self):
        self.config.runtime = replace(self.config.runtime, active_requery_max_rounds=1)
        await self.assert_host_stop("query_round_budget")

    async def test_model_stop_remains_host_only(self):
        self.outputs = [dict(legacy.done(), action="stop", stop_reason="")]
        await self.assert_host_stop("model_stop")

    async def test_unknown_provider_usage_cannot_trigger_final_reply_or_retry(self):
        self.prepare_no_new()
        self.query_error = GovernedError("synthetic_network_loss")
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query"])
        ledger = self.saved_ledger()
        self.assertEqual((ledger["phase"], ledger["halted"]), ("halted", "requery_usage_unknown"))
        self.assertEqual(ledger["unknown_generation_count"], 1)
        self.assertFalse(ledger["usage_complete"])
        self.assertEqual(ledger["automatic_retries"], 0)
        self.assert_policy(self.event("requery_stop")[0], "host_notice")
        self.assert_complete()

    async def test_final_generation_unknown_preserves_reservation_and_original_stop_without_retry(self):
        self.prepare_no_new()
        native = self.transport
        async def lost_final_response(path, payload):
            if path == "/responses" and "format" not in payload.get("text", {}):
                self.requests.append((path, "reply", deepcopy(payload)))
                raise GovernedError("synthetic_final_transport_loss")
            return await native(path, payload)
        self.adapter._request_override = lost_final_response
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        stop = self.event("requery_stop")[0]
        self.assertEqual(stop["reason"], "no_new_evidence")
        self.assertFalse(stop["fallback"])
        self.assertEqual((stop["ledger"]["phase"], stop["ledger"]["halted"]), ("open", None))
        ledger = self.saved_ledger()
        self.assertEqual((ledger["phase"], ledger["halted"]), ("halted", "requery_usage_unknown"))
        self.assertEqual((ledger["unknown_generation_count"], ledger["automatic_retries"]), (1, 0))
        self.assertFalse(ledger["usage_complete"])
        self.assertEqual([call["stage"] for call in ledger["calls"]], ["query", "query", "reply"])
        final = ledger["calls"][-1]
        self.assertTrue(final["generation_started"])
        self.assertIsNone(final["usage"])
        self.assertEqual(final["reserved_output_tokens"], self.config.adapter.budgets["reply"].output_tokens)
        self.assertGreater(final["reserved_input_tokens"], 0)
        self.assertEqual(final["transport_intents"], ["/responses/input_tokens", "/responses"])
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (128, 64))
        self.assertEqual(self.event("answer_generated"), [])
        self.assertEqual(self.event("turn_end")[0]["final_status"], "failed")
        self.assertEqual(self.event("turn_end")[0]["retrieval_stop_reason"], "no_new_evidence")
        self.assert_complete("failed")

    async def test_final_actual_input_gate_rejects_without_generation_retry(self):
        self.prepare_no_new()
        native = self.transport
        async def limited_reply(path, payload):
            if path == "/responses/input_tokens" and "format" not in payload.get("text", {}):
                self.requests.append((path, "reply", deepcopy(payload)))
                return {"input_tokens": 1000000}
            return await native(path, payload)
        self.adapter._request_override = limited_reply
        await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        reply_counts = [r for r in self.requests if r[:2] == ("/responses/input_tokens", "reply")]
        self.assertEqual(len(reply_counts), 1)
        ledger = self.saved_ledger()
        self.assertEqual((ledger["phase"], ledger["halted"], ledger["automatic_retries"]),
                         ("stopped", "input_token_limit", 0))
        self.assertEqual(self.event("turn_end")[0]["final_status"], "failed")
        self.assertEqual(self.event("answer_generated"), [])
        self.assert_complete("failed")

    async def test_final_cost_gate_rejects_before_transport_without_retry(self):
        self.prepare_no_new()
        native = TurnBudgetAdapter.call
        async def costly_reply(adapter, stage, *args, **kwargs):
            if stage == "reply":
                # Only exercise the admission formula; no real pricing/request.
                adapter.input_price = Decimal("1")
            return await native(adapter, stage, *args, **kwargs)
        with patch.object(TurnBudgetAdapter, "call", costly_reply):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))
        ledger = self.saved_ledger()
        self.assertEqual((ledger["halted"], ledger["automatic_retries"]), ("requery_cost_budget", 0))
        self.assertEqual(len(ledger["calls"]), 2)
        self.assertEqual(self.event("turn_end")[0]["final_status"], "failed")
        self.assert_complete("failed")

    async def test_final_time_gate_rejects_before_transport_without_retry(self):
        self.prepare_no_new()
        native = TurnBudgetAdapter.call
        async def expired_reply(adapter, stage, *args, **kwargs):
            if stage == "reply":
                adapter.deadline = 0
            return await native(adapter, stage, *args, **kwargs)
        with patch.object(TurnBudgetAdapter, "call", expired_reply):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))
        ledger = self.saved_ledger()
        self.assertEqual((ledger["halted"], ledger["automatic_retries"]), ("requery_time_budget", 0))
        self.assertEqual(len(ledger["calls"]), 2)
        self.assertEqual(self.event("turn_end")[0]["final_status"], "failed")
        self.assert_complete("failed")

    async def test_closed_ledger_cannot_be_reopened_for_no_new_final(self):
        self.prepare_no_new()
        native = ActiveRequery.append
        closed = {}
        def close_after_frozen(controller, retrieval, request_id, planning_call_id=None):
            native(controller, retrieval, request_id, planning_call_id)
            if request_id.endswith(":query:2"):
                controller.adapter.finish()
                closed["bytes"] = controller.adapter.path.read_bytes()
                closed["path"] = controller.adapter.path
                closed["halted"] = controller.adapter.halted
        with patch.object(ActiveRequery, "append", close_after_frozen):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        ledger = self.saved_ledger()
        self.assertEqual(ledger["phase"], "completed")
        self.assertIsNone(closed["halted"])
        self.assertIsNone(ledger["halted"])
        self.assertEqual(closed["path"].read_bytes(), closed["bytes"])
        self.assertEqual(ledger["automatic_retries"], 0)
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))

    async def test_empty_frozen_bundle_cannot_admit_no_new_final(self):
        wrapper = TurnBudgetAdapter(self.adapter, self.config, self.scratch,
                                    "synthetic-empty-final", "synthetic-empty-final", "10:20")
        controller = ActiveRequery(self.runtime, wrapper, "synthetic-empty-final", self.message)
        audit = {}
        records = controller.query_graph.retrieve(self.runtime.knowledge_scope, [], "mystery", time.time(),
            event_id="synthetic-empty-final:initial", audit=audit, ranking_mode="static", context_query="")
        self.assertEqual(records, [])
        frozen = freeze_retrieval(self.store.db, self.runtime.knowledge_scope,
            "synthetic-empty-final:initial", "mystery", records, CanonicalSnapshot(audit))
        controller.append(frozen, "synthetic-empty-final:initial")
        with self.assertRaisesRegex(GovernedError, "requery_finalization_not_eligible"):
            controller.stop("no_new_evidence", fallback=False)
        self.assertEqual(self.requests, [])
        self.assertEqual((wrapper.phase, wrapper.halted), ("open", None))
        wrapper.finish()

    async def test_ledger_io_failure_cannot_trigger_no_new_final(self):
        self.prepare_no_new()
        native = ActiveRequery.append
        def fail_after_frozen(controller, retrieval, request_id, planning_call_id=None):
            native(controller, retrieval, request_id, planning_call_id)
            if request_id.endswith(":query:2"):
                controller.adapter.ledger_io_failure = {"operation": "replace", "errno": 13, "winerror": 5}
        with patch.object(ActiveRequery, "append", fail_after_frozen):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)

    async def test_ledger_ownership_loss_cannot_trigger_no_new_final(self):
        self.prepare_no_new()
        native = ActiveRequery.append
        def lose_after_frozen(controller, retrieval, request_id, planning_call_id=None):
            native(controller, retrieval, request_id, planning_call_id)
            if request_id.endswith(":query:2"):
                error = GovernedError("requery_ledger_identity_changed", remote_usage_unknown=False)
                error.ledger_ownership_lost = True
                controller.adapter._ledger_ownership_error = error
        with patch.object(ActiveRequery, "append", lose_after_frozen):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)

    async def test_inflight_call_cannot_trigger_no_new_final(self):
        self.prepare_no_new()
        native = ActiveRequery.append
        def active_after_frozen(controller, retrieval, request_id, planning_call_id=None):
            native(controller, retrieval, request_id, planning_call_id)
            if request_id.endswith(":query:2"):
                controller.adapter._call_active = True
        with patch.object(ActiveRequery, "append", active_after_frozen):
            await self.run_turn()
        self.assertEqual(self.generation_stages(), ["query", "query"])
        self.assertFalse(any(stage == "reply" for _, stage, _ in self.requests))
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)

    async def test_final_delivery_exception_is_unknown_without_second_send(self):
        self.prepare_no_new()
        async def unknown_delivery(value):
            self.deliveries.append(value)
            raise RuntimeError("synthetic_delivery_loss")
        await self.runtime.process(self.message, unknown_delivery)
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(len(self.event("answer_generated")), 1)
        self.assertEqual(self.event("answer_delivered"), [])
        self.assertEqual(self.event("failure_notice_delivered"), [])
        end = self.event("turn_end")[0]
        self.assertEqual(end["status"], "failed")
        self.assertTrue(end["code"].startswith("delivery_unknown:"))
        self.assertEqual(end["final_status"], "delivery_unknown")
        self.assertEqual(end["retrieval_stop_reason"], "no_new_evidence")
        self.assertEqual(self.saved_ledger()["automatic_retries"], 0)
        self.assert_complete("failed")

    async def test_final_delivery_cancellation_is_unknown_without_second_send(self):
        self.prepare_no_new()
        async def cancelled_delivery(value):
            self.deliveries.append(value)
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.runtime.process(self.message, cancelled_delivery)
        self.assertEqual(self.generation_stages(), ["query", "query", "reply"])
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.event("answer_delivered"), [])
        self.assertEqual(self.event("failure_notice_delivered"), [])
        end = self.event("turn_end")[0]
        self.assertEqual((end["status"], end["final_status"]), ("delivery_unknown", "delivery_unknown"))
        self.assertEqual(end["retrieval_stop_reason"], "no_new_evidence")
        ledger = self.saved_ledger()
        self.assertEqual(ledger["halted"], "delivery_unknown")
        self.assertEqual(ledger["automatic_retries"], 0)
        self.assertEqual(verify_runs([self.entries()[0]])["issues"], [])

    async def test_retained_final_suffix_remains_partial_and_checks_policy_consistency(self):
        self.prepare_no_new()
        await self.run_turn()
        retained = {"reply_context", "answer_generated", "delivery_start", "answer_delivered", "turn_end"}
        events = [(event, fields) for event, fields in self.trace_events() if event in retained]
        self.assertFalse(any(event in {"turn_start", "requery_stop"} for event, _ in events))
        self.assertEqual(verify_active_turn(events, partial=True)["status"], "retention_partial")
        mutations = [
            ("reply_context", "requery_final_policy", "synthetic_unknown_policy"),
            ("answer_generated", "final_reply_mode", "ordinary_answer"),
            ("answer_delivered", "retrieval_stop_reason", "duplicate_query"),
            ("turn_end", "final_status", "host_notice_delivered"),
        ]
        for event, key, value in mutations:
            with self.subTest(event=event, key=key):
                changed = deepcopy(events)
                next(fields for name, fields in changed if name == event)[key] = value
                with self.assertRaises(ValueError):
                    verify_active_turn(changed, partial=True)
        changed = deepcopy(events)
        next(fields for event, fields in changed if event == "reply_context").pop("requery_final_policy")
        with self.assertRaises(ValueError):
            verify_active_turn(changed, partial=True)
        end_only = [(event, deepcopy(fields)) for event, fields in events if event == "turn_end"]
        self.assertEqual(verify_active_turn(end_only, partial=True)["status"], "retention_partial")
        end_only[0][1].pop("requery_final_policy")
        self.assertIn("final_reply_mode", end_only[0][1])
        self.assertIn("retrieval_stop_reason", end_only[0][1])
        self.assertIn("final_status", end_only[0][1])
        with self.assertRaises(ValueError):
            verify_active_turn(end_only, partial=True)
        end_only = [(event, deepcopy(fields)) for event, fields in events if event == "turn_end"]
        end_only[0][1]["status"] = "synthetic_unknown_status"
        end_only[0][1]["final_status"] = "synthetic_unknown_status"
        with self.assertRaises(ValueError):
            verify_active_turn(end_only, partial=True)

    async def test_new_final_policy_cannot_be_removed_or_rewritten(self):
        self.prepare_no_new()
        await self.run_turn()
        original = self.trace_events()
        start = next(f for e, f in original if e == "turn_start")
        self.assertEqual(verify_active_turn(original, start)["status"], "complete")
        for event in ("turn_start", "requery_stop", "reply_context", "answer_generated",
                      "answer_delivered", "turn_end"):
            for value in (None, "synthetic_unknown_policy"):
                with self.subTest(event=event, value=value):
                    events = deepcopy(original)
                    fields = next(f for e, f in events if e == event)
                    if value is None:
                        fields.pop("requery_final_policy")
                    else:
                        fields["requery_final_policy"] = value
                    altered_start = next(f for e, f in events if e == "turn_start")
                    with self.assertRaises(ValueError):
                        verify_active_turn(events, altered_start)
                    with self.assertRaises(ValueError):
                        verify_active_turn(events, altered_start, partial=True)

    async def test_no_new_reason_mode_status_and_context_tampering_is_rejected(self):
        self.prepare_no_new()
        await self.run_turn()
        original = self.trace_events()
        mutations = [
            ("requery_stop", "reason", "duplicate_query"),
            ("requery_stop", "fallback", True),
            ("requery_stop", "final_reply_mode", "ordinary_answer"),
            ("reply_context", "retrieval_stop_reason", "evidence_sufficient"),
            ("answer_generated", "final_reply_mode", "host_notice"),
            ("answer_delivered", "retrieval_stop_reason", "empty_results"),
            ("turn_end", "final_status", "host_notice_delivered"),
        ]
        for event, key, value in mutations:
            with self.subTest(event=event, key=key):
                events = deepcopy(original)
                next(f for e, f in events if e == event)[key] = value
                with self.assertRaises(ValueError):
                    verify_active_turn(events, next(f for e, f in events if e == "turn_start"))
        for remove in ("limitation", "quote", "instructions"):
            with self.subTest(remove=remove):
                events = deepcopy(original)
                reply = next(f for e, f in events if e == "reply_context")
                if remove == "instructions":
                    reply["instructions"] = "Synthetic ordinary instructions."
                else:
                    data = json.loads(reply["messages"][0]["content"].split("\n", 1)[1])
                    if remove == "limitation":
                        data.pop("context_limitation")
                    else:
                        data["memory_citations"][0]["quote"] = "Synthetic altered quote."
                    reply["messages"][0]["content"] = "Historical context data:\n" + json.dumps(data)
                with self.assertRaises(ValueError):
                    verify_active_turn(events, next(f for e, f in events if e == "turn_start"))


class HistoricalHostCompatibilityTests(unittest.TestCase):
    def test_unmarked_historical_and_prior_notice_policy_host_receipts_remain_valid(self):
        fixture = record_fixtures.ActiveTurnVerificationTests("runTest")
        fixture.setUp()
        try:
            for policy in (None, NOTICE_POLICY):
                with self.subTest(policy=policy):
                    scratch = record_fixtures.Scratch()
                    start = fixture.start(scratch, notice_policy=policy)
                    bundle = fixture.initial_events(scratch)
                    fixture.host_finish(scratch, bundle, reason="no_new_evidence", notice_policy=policy)
                    self.assertNotIn("requery_final_policy", start)
                    self.assertEqual(verify_active_turn(scratch.events, start)["status"], "complete")
                    self.assertFalse(any(f.get("stage") == "reply" for _, f in scratch.events))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
