"""Offline synthetic trace regressions for the opt-in path/requery loop.

The native adapter uses an HTTP-shaped mock, a temporary Store and network
guards. No stored questions, private materials or provider are consulted.
"""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from indeces.console import create_runtime
from indeces.contracts import GovernedError
from indeces.requery_records import PATH_REQUERY_LOOP_POLICY, verify_active_turn
from indeces.run_records import digest, verify_runs
from tests import test_active_requery as fixtures


class PathLoopRecordTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ActiveConsoleTests.setUp
    asyncTearDown = fixtures.ActiveConsoleTests.asyncTearDown
    transport = fixtures.ActiveConsoleTests.transport
    deliver = fixtures.ActiveConsoleTests.deliver
    entries = fixtures.ActiveConsoleTests.entries
    event = fixtures.ActiveConsoleTests.event
    run_turn = fixtures.ActiveConsoleTests.run_turn
    saved_ledger = fixtures.ActiveConsoleTests.saved_ledger

    def enable(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig
        self.config.runtime = replace(self.config.runtime, path_requery_loop_enabled=True)
        self.runtime = create_runtime(self.config, self.store, self.adapter, self.scratch)
        self.runtime.path_requery_loop_config = PathRequeryLoopConfig(enabled=True, max_seconds=3)

    def trace(self):
        trace_id = self.event("turn_start")[0]["trace_id"]
        return [(row["event"], deepcopy(row["fields"])) for row in self.entries()[1]
                if row["fields"].get("trace_id") == trace_id]

    def verify(self, events):
        start = next((fields for event, fields in events if event == "turn_start"), None)
        return verify_active_turn(events, start)

    @staticmethod
    def resign(fields, key="receipt"):
        receipt = fields[key]
        receipt["feedback_sha256"] = digest({k: v for k, v in receipt.items()
                                             if k != "feedback_sha256"})
        fields[key + "_sha256"] = digest(receipt)

    async def test_declared_loop_binds_parent_queries_results_and_next_planner(self):
        self.enable()
        await self.run_turn()
        events = self.trace()
        self.assertEqual(self.verify(events)["status"], "complete")
        self.assertEqual(verify_runs([self.entries()[0]])["issues"], [])
        start = self.event("turn_start")[0]
        self.assertEqual(start["path_requery_loop_policy"], PATH_REQUERY_LOOP_POLICY)
        self.assertTrue(start["path_requery_loop_offline_mock"])
        before = self.event("path_requery_feedback")
        after = self.event("path_requery_feedback_result")
        self.assertEqual(len(before), 2)
        self.assertEqual(len(after), 2)
        planning = [json.loads(payload["input"][0]["content"])
                    for path, stage, payload in self.requests
                    if path == "/responses" and stage == "query"]
        self.assertIsNone(planning[0]["path_feedback"])
        self.assertEqual(planning[0]["path_feedback_sha256"], digest(None))
        for index in range(1, len(planning)):
            expected = {"input": before[index - 1]["receipt"],
                        "result": after[index - 1]["result_receipt"]}
            self.assertEqual(planning[index]["path_feedback"], expected)
            self.assertEqual(planning[index]["path_feedback_sha256"], digest(expected))
        self.assertEqual(self.saved_ledger()["phase"], "completed")

    async def test_rehashed_forged_parent_mapping_and_effective_terms_are_rejected(self):
        self.enable()
        await self.run_turn()
        for change in ("parent", "action", "baseline", "terms", "policy"):
            damaged = self.trace()
            fields = next(f for e, f in damaged if e == "path_requery_feedback")
            receipt = fields["receipt"]
            if change == "parent":
                receipt["demand_binding"]["parent_evidence_sha256"] = "0" * 64
            elif change == "action":
                receipt["demand_binding"]["action_sha256"] = "0" * 64
            elif change == "baseline":
                receipt["baseline_terms"] = ["synthetic-forged-baseline"]
            elif change == "terms":
                receipt["effective_terms"].append("synthetic-unsourced-node")
                receipt["added_terms"].append("synthetic-unsourced-node")
            else:
                receipt["policy"] = "synthetic-unknown-policy"
            self.resign(fields)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(damaged)

    async def test_native_query_and_result_binding_tampering_is_rejected(self):
        self.enable()
        await self.run_turn()
        for change in ("query-terms", "query-hash", "added", "before-hash", "result-parent"):
            damaged = self.trace()
            native = next(f for e, f in damaged if e == "requery_retrieval" and f["round_index"] == 1)
            result = next(f for e, f in damaged if e == "path_requery_feedback_result")
            if change == "query-terms":
                native["effective_terms"] = ["synthetic-forged-argument"]
            elif change == "query-hash":
                native["actual_query_sha256"] = "0" * 64
            elif change == "added":
                result["added_evidence_uids"] = ["0" * 64]
            elif change == "before-hash":
                result["receipt_sha256"] = "0" * 64
            else:
                result["result_receipt"]["demand_binding"]["parent_evidence_sha256"] = "0" * 64
                self.resign(result, "result_receipt")
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(damaged)

    async def test_policy_removal_and_real_mode_are_rejected(self):
        self.enable()
        await self.run_turn()
        for change in ("missing", "unknown", "mock", "ledger-mode"):
            damaged = self.trace()
            start = damaged[0][1]
            if change == "missing":
                start.pop("path_requery_loop_policy")
            elif change == "unknown":
                start["path_requery_loop_policy"] = "synthetic-unknown-policy"
            elif change == "mock":
                start["path_requery_loop_offline_mock"] = False
            else:
                for event, fields in damaged:
                    if event in {"requery_budget_event", "requery_stop"}:
                        fields["ledger"]["mode"] = "configured_real"
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(damaged)

    async def test_missing_reordered_or_post_terminal_feedback_is_rejected(self):
        self.enable()
        await self.run_turn()
        for change in ("missing", "reordered", "terminal", "stop"):
            damaged = self.trace()
            index = next(i for i, (e, _) in enumerate(damaged) if e == "path_requery_feedback_result")
            if change == "missing":
                damaged.pop(index)
            elif change == "reordered":
                result = damaged.pop(index)
                pre = next(i for i, (e, _) in enumerate(damaged) if e == "path_requery_feedback")
                damaged.insert(pre, result)
            elif change == "terminal":
                damaged.append(deepcopy(next(item for item in damaged if item[0] == "path_requery_feedback")))
            else:
                extra = deepcopy(next(item for item in damaged if item[0] == "path_requery_feedback"))
                stop = next(i for i, (e, _) in enumerate(damaged) if e == "requery_stop")
                damaged.insert(stop + 1, extra)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(damaged)

    async def test_next_planner_feedback_downgrade_is_rejected(self):
        self.enable()
        await self.run_turn()
        damaged = self.trace()
        query_calls = [f["call_id"] for e, f in damaged if e == "call_start" and f["stage"] == "query"]
        for event, fields in damaged:
            if event == "http_request" and fields.get("call_id") == query_calls[1]:
                payload = json.loads(fields["payload"]["input"][0]["content"])
                payload["path_feedback"] = None
                payload["path_feedback_sha256"] = digest(None)
                fields["payload"]["input"][0]["content"] = json.dumps(payload, ensure_ascii=False)
        with self.assertRaises(ValueError):
            self.verify(damaged)

    async def test_retained_receipt_verifies_hash_only_and_declares_partial_binding(self):
        self.enable()
        await self.run_turn()
        pre = next(item for item in self.trace() if item[0] == "path_requery_feedback")
        report = verify_active_turn([pre], partial=True)
        self.assertEqual(report["status"], "retention_partial")
        self.assertIn("retained_path_feedback_binding_partial", report["warnings"])
        damaged = deepcopy(pre)
        damaged[1]["receipt"]["effective_terms"].append("synthetic-tamper")
        damaged[1]["receipt_sha256"] = digest(damaged[1]["receipt"])
        with self.assertRaises(ValueError):
            verify_active_turn([damaged], partial=True)

    async def test_unknown_usage_closes_without_graph_feedback_or_reopened_calls(self):
        self.enable()
        self.query_error = GovernedError("synthetic_network_loss")
        await self.run_turn()
        self.assertEqual(self.event("path_requery_feedback"), [])
        self.assertEqual(self.event("path_requery_feedback_result"), [])
        ledger = self.saved_ledger()
        self.assertEqual((ledger["phase"], ledger["halted"], ledger["automatic_retries"]),
                         ("halted", "requery_usage_unknown", 0))
        self.assertEqual(ledger["unknown_generation_count"], 1)
        self.assertEqual([stage for path, stage, _ in self.requests if path == "/responses"], ["query"])
        report = self.verify(self.trace())
        self.assertEqual(report["status"], "complete")  # The bounded notice was delivered.
        self.assertIn("remote_generation_usage_unknown", report["warnings"])

    async def test_toggle_off_keeps_dev10_planner_schema_and_receipts(self):
        await self.run_turn()
        self.assertEqual(self.event("path_requery_feedback"), [])
        self.assertEqual(self.event("path_requery_feedback_result"), [])
        self.assertNotIn("path_requery_loop_policy", self.event("turn_start")[0])
        for path, stage, payload in self.requests:
            if path == "/responses" and stage == "query":
                planning = json.loads(payload["input"][0]["content"])
                self.assertEqual(set(planning), {"original_text", "round_index", "evidence",
                                                "remaining_query_rounds", "history_context"})
        self.assertEqual(self.verify(self.trace())["status"], "complete")

    async def test_clock_cutoff_with_unhashed_demand_keeps_baseline_and_closes(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig
        self.enable()
        self.runtime.path_requery_loop_config = PathRequeryLoopConfig(enabled=True, max_seconds=1e-12)
        await self.run_turn()
        pre = self.event("path_requery_feedback")[0]["receipt"]
        self.assertEqual((pre["status"], pre["reason"]), ("budget_stop", "seconds"))
        self.assertIsNone(pre["demand_binding"])
        self.assertEqual(pre["effective_terms"], pre["baseline_terms"])
        self.assertEqual(pre["added_terms"], [])
        self.assertEqual(self.event("path_requery_feedback_result"), [])
        self.assertEqual(len(self.event("requery_retrieval")), 1)
        self.assertEqual([stage for path, stage, _ in self.requests if path == "/responses"], ["query"])
        self.assertEqual(self.event("requery_stop")[0]["reason"], "path_requery_budget")
        self.assertEqual((self.saved_ledger()["phase"], self.saved_ledger()["automatic_retries"]),
                         ("stopped", 0))
        self.assertEqual(self.saved_ledger()["halted"], "path_requery_budget")
        report = self.verify(self.trace())
        self.assertEqual(report["status"], "complete")
        self.assertIn("path_feedback_clock_cutoff_not_replayed", report["warnings"])
        self.assertIn("path_feedback_cutoff_demand_hash_unavailable", report["warnings"])

    async def test_term_cutoff_replays_without_query_or_model_reply(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig
        self.enable()
        self.runtime.path_requery_loop_config = PathRequeryLoopConfig(
            enabled=True, max_terms=1, max_added_terms=1, max_seconds=3)
        planned = fixtures.action()
        planned["query"]["canonical_terms"] = ["amber", "cobalt"]
        self.outputs = [planned]
        await self.run_turn()
        self.assertEqual(self.event("path_requery_feedback")[0]["receipt"]["reason"], "terms")
        self.assertEqual(len(self.event("requery_retrieval")), 1)
        self.assertEqual([stage for path, stage, _ in self.requests if path == "/responses"], ["query"])
        self.assertEqual(self.verify(self.trace())["status"], "complete")

    async def test_result_node_cutoff_retains_native_round_and_closes_without_reply(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig
        self.enable()
        self.runtime.path_requery_loop_config = PathRequeryLoopConfig(enabled=True, max_nodes=1, max_seconds=3)
        self.runtime.graph.add("10:knowledge", "synthetic:pair", "synthetic-pair", [{
            "text": "Synthetic amber sensor pair observation.",
            "quote": "Synthetic amber sensor pair observation.", "marks": ["amber", "sensor"]}], 1.0)
        await self.run_turn()
        self.assertEqual(len(self.event("requery_retrieval")), 2)
        result = self.event("path_requery_feedback_result")[0]["result_receipt"]
        self.assertEqual((result["status"], result["reason"]), ("budget_stop", "nodes"))
        self.assertEqual(self.event("requery_stop")[0]["reason"], "path_requery_budget")
        self.assertEqual([stage for path, stage, _ in self.requests if path == "/responses"], ["query"])
        self.assertEqual(self.saved_ledger()["phase"], "stopped")
        self.assertEqual(self.saved_ledger()["halted"], "path_requery_budget")
        self.assertEqual(self.verify(self.trace())["status"], "complete")

    async def test_duplicate_effective_query_stop_keeps_unexecuted_pre_receipt(self):
        self.enable()
        self.outputs = [fixtures.action(), fixtures.action()]
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "duplicate_query")
        self.assertEqual(len(self.event("path_requery_feedback")), 2)
        self.assertEqual(len(self.event("path_requery_feedback_result")), 1)
        self.assertEqual(self.verify(self.trace())["status"], "complete")

    async def test_exhausted_round_stop_keeps_unexecuted_pre_receipt(self):
        self.enable()
        self.config.runtime = replace(self.config.runtime, active_requery_max_rounds=1)
        await self.run_turn()
        self.assertEqual(self.event("requery_stop")[0]["reason"], "query_round_budget")
        self.assertEqual(len(self.event("path_requery_feedback")), 2)
        self.assertEqual(len(self.event("path_requery_feedback_result")), 1)
        self.assertEqual(self.verify(self.trace())["status"], "complete")


if __name__ == "__main__":
    unittest.main()
