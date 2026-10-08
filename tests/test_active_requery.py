"""New synthetic questions and HTTP-shaped mocks; never access user data."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.active_requery import (ACTION_SCHEMA, TurnBudgetAdapter, parse_action,
                                    query_identity)
from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
from indeces.console import create_runtime
from indeces.contracts import DeliveryReceipt, FailureNotice, GovernedError, IncomingMessage
from indeces.memory import MemoryGraph
from indeces.requery_records import bundle_model_materials, validate_bundle
from indeces.run_records import verify_runs
from indeces.scratch import ScratchLog
from indeces.store import Store


def action(term="amber", original="mystery"):
    return {"action": "query", "query": {
        "entities": [{"id": "e1", "surface": original, "canonical": term, "span": [0, len(original)]}],
        "relations": [], "negations": [], "time": None, "scope": None,
        "canonical_terms": [term], "ambiguities": []},
        "missing_evidence": ["Synthetic missing observation"], "clarification": "", "stop_reason": ""}


def done():
    return {"action": "answer", "query": None, "missing_evidence": [],
            "clarification": "", "stop_reason": "evidence_sufficient"}


class CanonicalActionTests(unittest.TestCase):
    def test_preserves_anchored_entity_relation_negation_direction_time_scope(self):
        original = "Selene does not follow Orion before 2030 in lab A."
        def anchor(surface):
            start = original.index(surface)
            return {"surface": surface, "span": [start, start + len(surface)]}
        data = action()
        data["query"].update(entities=[dict(anchor("Selene"), id="e1", canonical="selene_sensor"),
                                      dict(anchor("Orion"), id="e2", canonical="orion_valve")],
            relations=[dict(anchor("does not follow"), subject_id="e1", object_id="e2",
                            predicate="follow", negated=True, direction="subject_to_object")],
            negations=[anchor("not")], time=anchor("before 2030"), scope=anchor("lab A"))
        parsed = parse_action(json.dumps(data), original)
        self.assertEqual(parsed, data)
        self.assertTrue(parsed["query"]["relations"][0]["negated"])

    def test_invalid_span_duplicate_keys_and_thought_trace_are_rejected(self):
        data = action()
        data["query"]["entities"][0]["span"] = [1, 7]
        invalid = [json.dumps(data), '{"action":"answer","action":"stop"}',
                   json.dumps(dict(done(), private_thoughts="not permitted")), "[1]", "not-json"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(GovernedError, "invalid_query_action"):
                parse_action(value, "mystery")

    def test_ambiguity_requires_clarification_instead_of_query(self):
        data = action()
        data["query"]["ambiguities"] = ["Synthetic ambiguous referent"]
        with self.assertRaises(GovernedError):
            parse_action(json.dumps(data), "mystery")
        clarify = dict(done(), action="clarify", clarification="Which synthetic sensor?", stop_reason="ambiguity")
        self.assertEqual(parse_action(json.dumps(clarify), "mystery"), clarify)

    def test_case_order_unicode_repetition_identity(self):
        first = action()["query"]
        second = deepcopy(first)
        second["canonical_terms"] = ["ＡＭＢＥＲ", " amber "]
        self.assertEqual(query_identity(first), query_identity(second))

    def test_flags_default_off_and_limits_reject_nonfinite_or_coerced_values(self):
        self.assertFalse(RuntimeConfig().active_requery_enabled)
        for changes in ({"active_requery_enabled": 1}, {"active_requery_max_rounds": True},
                        {"active_requery_seconds": float("nan")}, {"active_requery_cost_usd": -1},
                        {"active_requery_output_usd_per_million": float("inf")},
                        {"active_requery_seconds": 10 ** 400}, {"active_requery_cost_usd": 10 ** 400}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RuntimeConfig(**changes)

    def test_legacy_partial_stage_config_remains_constructible(self):
        config = AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {"label": Budget(100, 20, 1)})
        self.assertEqual(config.budgets["query"], Budget(100, 20, 1))


class ActiveConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.store = Store(self.root / "state")
        self.scratch = ScratchLog(self.root / "scratch")
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            discord=DiscordConfig("10"), runtime=RuntimeConfig(active_requery_enabled=True,
                retrieval_policy="legacy_v1", turn_seconds=30, summary_max_bytes=256),
            adapter=AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {
                stage: Budget(16384, 2048, 15, "medium") for stage in ("label", "reply", "summary")}))
        self.requests, self.outputs, self.deliveries = [], [action(), action("cobalt"), done()], []
        self.query_index = 0
        self.query_error = None
        self.adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=self.transport)
        self.adapter.offline_mock = True
        self.runtime = create_runtime(self.config, self.store, self.adapter, self.scratch)
        for mark in ("amber", "cobalt"):
            self.runtime.graph.add("10:knowledge", "synthetic:" + mark, "synthetic-source-" + mark, [
                {"text": f"Synthetic {mark} observation is a mock fixture.",
                 "quote": f"Synthetic {mark} observation is a mock fixture.", "marks": [mark]}], 1.0)
        self.message = IncomingMessage("synthetic-message", "20", "10", "30", "Synthetic operator",
                                       "mystery", "2026-10-08T00:00:00Z")
        self.guards = [patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")),
                       patch("socket.socket.connect_ex", side_effect=AssertionError("external network forbidden")),
                       patch("aiohttp.ClientSession", side_effect=AssertionError("external HTTP forbidden"))]
        for guard in self.guards:
            guard.start()

    async def asyncTearDown(self):
        await self.adapter.close()
        self.scratch.close()
        for guard in reversed(self.guards):
            guard.stop()
        self.store.close()
        self.directory.cleanup()

    async def transport(self, path, payload):
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        self.requests.append((path, stage, deepcopy(payload)))
        if path == "/responses/input_tokens":
            return {"input_tokens": 64}
        self.assertEqual(path, "/responses")
        if stage == "query":
            if self.query_error:
                raise self.query_error
            data = self.outputs[min(self.query_index, len(self.outputs) - 1)]
            self.query_index += 1
            text = data if type(data) is str else json.dumps(data)
        elif stage == "selection":
            data = json.loads(payload["input"][0]["content"])
            text = json.dumps({"selected_record_ids": [c["record_id"] for c in data["candidates"][:data["required_selection_count"]]]})
        elif stage == "summary":
            text = json.dumps({"summary": "Synthetic bounded continuity summary."})
        else:
            text = "Synthetic final observation [M1] and [M2]."
            if payload.get("text", {}).get("format", {}).get("name") == "reply":
                text = json.dumps({"action": "reply", "text": text})
        return {"id": f"synthetic-{stage}-{len(self.requests)}", "status": "completed",
            "usage": {"input_tokens": 64, "output_tokens": 32},
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}

    async def deliver(self, value):
        self.deliveries.append(value)
        return DeliveryReceipt(("synthetic-reply",), value.text if isinstance(value, FailureNotice) else value)

    def entries(self):
        path = next((self.root / "scratch").glob("*.jsonl"))
        return path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def event(self, name):
        return [e["fields"] for e in self.entries()[1] if e["event"] == name]

    async def run_turn(self):
        await self.runtime.process(self.message, self.deliver)
        self.assertIsNone(self.adapter._session)

    async def test_actual_console_two_query_rounds_return_new_evidence_to_final_model(self):
        weights = list(self.store.db.execute("SELECT * FROM memory_static"))
        await self.run_turn()
        rounds = self.event("requery_retrieval")
        self.assertEqual([r["round_index"] for r in rounds], [0, 1, 2])
        self.assertEqual([r["request_id"] for r in rounds],
                         ["synthetic-message:initial", "synthetic-message:query:1", "synthetic-message:query:2"])
        bundle = self.event("requery_evidence")[0]["bundle"]
        validate_bundle(bundle)
        self.assertEqual([m["source_id"] for m in bundle_model_materials(bundle)], ["synthetic:amber", "synthetic:cobalt"])
        reply = next(payload for path, stage, payload in self.requests if path == "/responses" and stage == "reply")
        historical = json.loads(reply["input"][0]["content"].split("\n", 1)[1])
        self.assertEqual(historical["memory_citations"], bundle_model_materials(bundle))
        self.assertEqual([s for p, s, _ in self.requests if p == "/responses"], ["query", "query", "query", "reply"])
        self.assertEqual(list(self.store.db.execute("SELECT * FROM memory_static")), weights)
        self.assertEqual(self.event("turn_end")[0]["status"], "delivered")
        report = verify_runs([self.entries()[0]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["complete"], 1)

    async def test_initial_selection_and_summary_share_cumulative_budget(self):
        self.message = replace(self.message, text="amber")
        self.outputs = [done()]
        with patch.object(self.runtime, "_summary", wraps=self.runtime._summary) as summary:
            await self.run_turn()
            self.assertIsNotNone(summary.call_args.kwargs["adapter_override"])
        ledger = self.event("requery_budget_event")[-1]["ledger"]
        self.assertEqual([c["stage"] for c in ledger["calls"]], ["selection", "query", "reply"])
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (192, 96))
        self.assertEqual(ledger["mode"], "mock")
        self.assertEqual(ledger["estimated_cost_usd"], "0")

    async def test_actual_summary_generation_is_accounted_before_query_and_reply(self):
        self.message = replace(self.message, text="amber")
        self.outputs = [done()]
        # Exercise real summary execution independently of the initial selector,
        # whose unchanged 4096-byte admission can reject a long raw history.
        self.config.runtime = replace(self.config.runtime, model_selection_enabled=False)
        self.runtime = create_runtime(self.config, self.store, self.adapter, self.scratch)
        for index in range(3):
            previous = replace(self.message, message_id=f"synthetic-history-{index}", text="Synthetic history " + "x" * 5000)
            self.store.begin(previous)
            self.store.generated(previous.message_id, "Synthetic previous observation.")
            self.store.finish(previous, DeliveryReceipt((f"synthetic-old-reply-{index}",), "Synthetic previous observation."))
        await self.run_turn()
        ledger = self.event("requery_budget_event")[-1]["ledger"]
        self.assertEqual([c["stage"] for c in ledger["calls"]], ["summary", "query", "reply"])
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (192, 96))
        self.assertEqual(len(self.event("checkpoint_saved")), 1)

    async def test_time_budget_is_persisted_and_blocks_transport(self):
        adapter = TurnBudgetAdapter(self.adapter, self.config, self.scratch, "synthetic-budget-trace", "other-message", "10:20")
        adapter.deadline = 0
        with self.assertRaisesRegex(GovernedError, "requery_time_budget"):
            await adapter.call("query", "synthetic", [], "synthetic-budget-trace", ACTION_SCHEMA)
        self.assertEqual(self.requests, [])
        self.assertEqual(json.loads(adapter.path.read_text())["halted"], "requery_time_budget")

    async def test_expiry_before_generation_preserves_time_stop_across_audit_annotation(self):
        adapter = TurnBudgetAdapter(self.adapter, self.config, self.scratch, "synthetic-expiry-trace", "expiry-message", "10:20")
        native = self.transport
        async def expires_after_count(path, payload):
            result = await native(path, payload)
            if path == "/responses/input_tokens":
                adapter.deadline = 0
            return result
        self.adapter._request_override = expires_after_count
        with self.assertRaisesRegex(GovernedError, "requery_time_budget"):
            await adapter.call("query", "synthetic", [], "synthetic-expiry-trace", ACTION_SCHEMA)
        adapter.finish()
        self.assertEqual(adapter.halted, "requery_time_budget")
        self.assertEqual(json.loads(adapter.path.read_text())["phase"], "stopped")
        self.assertEqual([p for p, _, _ in self.requests], ["/responses/input_tokens"])

    async def test_external_async_audit_is_rejected_before_counting(self):
        adapter = TurnBudgetAdapter(self.adapter, self.config, self.scratch, "synthetic-audit-trace", "audit-message", "10:20")
        async def unsafe_audit(event, **fields):
            raise AssertionError("must never await external audit")
        with self.assertRaisesRegex(GovernedError, "async_call_audit_not_supported"):
            await adapter.call("query", "synthetic", [], "synthetic-audit-trace", ACTION_SCHEMA, audit=unsafe_audit)
        self.assertEqual(self.requests, [])
        self.assertEqual(adapter.halted, "requery_ledger_audit_failed")

    def saved_ledger(self):
        return json.loads(next((self.root / "state/active_requery_ledger").glob("*.json")).read_text())

    async def test_initial_selection_admission_failure_is_persisted_as_stopped(self):
        self.message = replace(self.message, text="amber")
        previous = replace(self.message, message_id="synthetic-long-history", text="Synthetic " + "x" * 6000)
        self.store.begin(previous)
        await self.run_turn()
        ledger = self.saved_ledger()
        self.assertEqual(ledger["phase"], "stopped")
        self.assertEqual(ledger["halted"], "selection_input_budget")
        self.assertEqual(self.requests, [])

    async def test_final_reply_input_gate_failure_is_persisted_without_generation_retry(self):
        self.outputs = [done()]
        native = self.transport
        async def limited_reply(path, payload):
            if path == "/responses/input_tokens" and "format" not in payload.get("text", {}):
                self.requests.append((path, "reply", deepcopy(payload)))
                return {"input_tokens": 1000000}
            return await native(path, payload)
        self.adapter._request_override = limited_reply
        await self.run_turn()
        ledger = self.saved_ledger()
        self.assertEqual(ledger["phase"], "stopped")
        self.assertEqual(ledger["halted"], "input_token_limit")
        self.assertEqual([s for p, s, _ in self.requests if p == "/responses"], ["query"])

    async def test_delivery_failure_is_persisted_without_second_delivery(self):
        self.outputs = [done()]
        async def failed_delivery(value):
            self.deliveries.append(value)
            raise RuntimeError("synthetic-delivery-error")
        await self.runtime.process(self.message, failed_delivery)
        ledger = self.saved_ledger()
        self.assertEqual(ledger["phase"], "stopped")
        self.assertEqual(ledger["halted"], "delivery_unknown:RuntimeError")
        self.assertEqual(len(self.deliveries), 1)

    async def test_empty_result_stops_without_final_generation(self):
        self.outputs = [action("not-present")]
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "empty_results")
        self.assertEqual(self.query_index, 1)
        self.assertEqual(self.event("answer_generated")[0]["final_origin"], "bounded_stop")
        self.assertEqual(verify_runs([self.entries()[0]])["issues"], [])

    async def test_duplicate_query_stops_before_second_graph_execution(self):
        self.outputs = [action(), action()]
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "duplicate_query")
        self.assertEqual(len(self.event("requery_retrieval")), 2)
        self.assertEqual(self.query_index, 2)
        self.assertFalse(any(s == "reply" for p, s, _ in self.requests if p == "/responses"))

    async def test_distinct_query_with_no_new_evidence_stops_monotonically(self):
        second = action("AMBER")
        second["query"]["entities"][0]["canonical"] = "alternate-alias"
        self.outputs = [action(), second]
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "no_new_evidence")
        rounds = self.event("requery_retrieval")
        self.assertEqual(len(rounds), 3)
        bundle = self.event("requery_evidence")[0]["bundle"]
        self.assertEqual(len(bundle_model_materials(bundle)), 1)

    async def test_call_budget_stops_before_transport_and_fixed_notice_is_delivered(self):
        self.config.runtime = replace(self.config.runtime, active_requery_max_calls=1)
        await self.run_turn()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.event("requery_stop")[0]["reason"], "requery_call_budget")
        self.assertEqual(len(self.deliveries), 1)

    async def test_token_budget_stops_before_count_request(self):
        self.config.runtime = replace(self.config.runtime, active_requery_input_tokens=128)
        await self.run_turn()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.event("requery_stop")[0]["reason"], "requery_token_budget")

    async def test_round_budget_stops_without_extra_retrieval(self):
        self.config.runtime = replace(self.config.runtime, active_requery_max_rounds=1)
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "query_round_budget")
        self.assertEqual(len(self.event("requery_retrieval")), 2)

    async def test_unknown_generation_usage_is_sticky_and_not_retried(self):
        self.query_error = GovernedError("synthetic_network_loss")
        await self.run_turn()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.event("requery_stop")[0]["ledger"]["halted"], "requery_usage_unknown")
        ledger = json.loads(next((self.root / "state/active_requery_ledger").glob("*.json")).read_text())
        self.assertEqual(ledger["phase"], "halted")
        self.assertIsNone(ledger["calls"][0]["usage"])
        self.assertEqual(ledger["calls"][0]["reserved_output_tokens"], 512)
        with self.assertRaisesRegex(GovernedError, "already_recorded"):
            TurnBudgetAdapter(self.adapter, self.config, self.scratch, "new-trace", self.message.message_id, self.message.scope)
        self.assertEqual(len(self.requests), 2)

    async def test_invalid_action_and_local_graph_exception_do_not_retry_or_leak(self):
        self.outputs = ["invalid-json"]
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "invalid_query_action")
        self.assertEqual(len(self.requests), 2)

    async def test_graph_exception_keeps_initial_evidence_and_clears_handler(self):
        with patch("indeces.active_requery.freeze_retrieval", side_effect=RuntimeError("synthetic_private_error_text")):
            await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "requery_local_error")
        self.assertNotIn("synthetic_private_error_text", str(self.deliveries))
        self.assertEqual(self.store.db.execute("SELECT 1").fetchone()[0], 1)

    async def test_real_mode_disabled_before_any_transport(self):
        self.adapter.offline_mock = False
        await self.run_turn()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.event("turn_end")[0]["code"], "active_requery_real_calls_not_authorized")

    async def test_cost_budget_before_any_transport_with_explicit_synthetic_tariff(self):
        self.adapter.offline_mock = False
        self.config.runtime = replace(self.config.runtime, active_requery_allow_real_calls=True,
            active_requery_cost_usd=0.000001, active_requery_input_usd_per_million=1,
            active_requery_output_usd_per_million=2)
        await self.run_turn()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.event("requery_stop")[0]["reason"], "requery_cost_budget")

    async def test_ledger_write_failure_blocks_model_and_preserves_failed_turn(self):
        with patch("indeces.active_requery._atomic_json", side_effect=GovernedError("requery_ledger_write_failed")):
            await self.run_turn()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.event("turn_end")[0]["code"], "requery_ledger_write_failed")

    async def test_bot_bounded_stop_uses_failure_notice_without_continuation(self):
        self.message = replace(self.message, author_is_bot=True)
        self.outputs = [dict(done(), action="clarify", clarification="Which synthetic sensor?", stop_reason="ambiguity")]
        await self.run_turn()
        self.assertIsInstance(self.deliveries[0], FailureNotice)
        self.assertEqual(self.event("bot_reply_decision"), [])

    async def test_default_off_retains_one_legacy_record_and_no_ledger(self):
        self.config.runtime = replace(self.config.runtime, active_requery_enabled=False)
        self.message = replace(self.message, text="amber")
        await self.run_turn()
        self.assertEqual(self.event("turn_start")[0]["run_record_version"], 1)
        self.assertEqual(len(self.event("retrieval_record")), 1)
        self.assertEqual(self.event("requery_action"), [])
        self.assertFalse((self.root / "state/active_requery_ledger").exists())
        self.assertEqual(verify_runs([self.entries()[0]])["issues"], [])
