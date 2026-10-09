"""Execution telemetry uses synthetic stores; no production logs or services."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import sqlite3
import unittest
from unittest.mock import patch

from indeces.contracts import IncomingMessage
from indeces.memory import MemoryGraph
from indeces.npmi_telemetry import NPMIRetrievalTelemetry, validate_receipt
from indeces.runtime import Runtime
from indeces.scratch import canonical
from tests.test_context import SummaryAdapter
from tests.test_run_records import RecordFixture


class NPMIExecutionTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.seed()

    def tearDown(self):
        self.teardown_fixture()

    def retrieve(self, graph=None, query="alpha topic", event_id="telemetry-event"):
        graph = graph or self.graph
        audit = {}
        trace = NPMIRetrievalTelemetry()
        with trace:
            result = graph.retrieve(self.scope, [], query, 2.0, event_id=event_id, audit=audit)
        receipt = trace.receipt()
        validate_receipt(receipt, graph=audit)
        return result, audit, receipt

    def test_actual_static_weight_use_without_formula_recomputation_in_both_policies(self):
        for policy in ("legacy_v1", "concept_v1"):
            with self.subTest(policy=policy):
                graph = MemoryGraph(self.store.db, retrieval_policy=policy)
                result, audit, receipt = self.retrieve(graph, event_id=policy)
                self.assertEqual(len(result), 1)
                self.assertEqual(receipt["path"], "keyed_static_neighborhood_v1")
                self.assertTrue(receipt["precomputed_npmi_used"])
                self.assertFalse(receipt["formula_recomputed"])
                self.assertEqual(receipt["counters"]["precomputed_weight_reads"], 1)
                self.assertEqual(receipt["counters"]["neighbor_ordering_weight_uses"], 2)
                self.assertEqual(receipt["counters"]["candidate_edge_weight_uses"], 1)
                self.assertEqual(receipt["counters"]["formula_evaluation_attempts"], 0)
                self.assertEqual(receipt["stages"]["static_rebuild"]["calls"], 0)

    def test_empty_neighborhood_records_executed_lookup_with_zero_usage(self):
        result, audit, receipt = self.retrieve(query="unmatched synthetic query")
        self.assertEqual(result, [])
        self.assertFalse(receipt["precomputed_npmi_used"])
        self.assertFalse(receipt["formula_recomputed"])
        self.assertEqual(receipt["counters"]["precomputed_edges_loaded"], 0)
        self.assertEqual(receipt["stages"]["precomputed_lookup"]["completed_calls"], 1)

    def test_receipts_are_fresh_and_do_not_change_immutable_replay_audit(self):
        self.retrieve(event_id="same")
        baseline = {}
        expected = self.graph.retrieve(self.scope, [], "alpha topic", 2.0, event_id="same", audit=baseline)
        actual, audit, receipt = self.retrieve(event_id="same")
        self.assertEqual(actual, expected)
        self.assertEqual(canonical(audit), canonical(baseline))
        self.assertEqual(receipt["retrieval_calls"], 1)
        _, _, empty = self.retrieve(query="unmatched synthetic query", event_id="empty")
        self.assertEqual(empty["counters"]["precomputed_weight_reads"], 0)
        self.assertEqual(receipt["counters"]["precomputed_weight_reads"], 1)

    def test_formula_counter_runs_at_real_rebuild_and_does_not_leak_to_next_query(self):
        trace = NPMIRetrievalTelemetry()
        with self.store.db, trace:
            self.graph._rebuild_static(self.scope)
        receipt = trace.receipt()
        self.assertTrue(receipt["formula_recomputed"])
        self.assertEqual(receipt["counters"]["formula_evaluation_attempts"], 1)
        self.assertEqual(receipt["counters"]["formula_evaluations"], 1)
        self.assertEqual(receipt["stages"]["static_rebuild"]["completed_calls"], 1)
        _, _, next_receipt = self.retrieve()
        self.assertFalse(next_receipt["formula_recomputed"])
        self.assertEqual(next_receipt["counters"]["formula_evaluations"], 0)

    def test_failed_formula_attempt_does_not_claim_successful_recomputation(self):
        self.graph.add(self.scope, "synthetic-other-source", "synthetic", [
            {"text": "alpha gamma", "quote": "alpha gamma", "marks": ["alpha", "gamma"]}], 1.5)
        trace = NPMIRetrievalTelemetry()
        with self.assertRaises(ValueError):
            with self.store.db, trace, patch("indeces.memory.math.log", side_effect=ValueError("private error detail")):
                self.graph._rebuild_static(self.scope)
        receipt = trace.receipt()
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_type"], "ValueError")
        self.assertEqual(receipt["counters"]["formula_evaluation_attempts"], 1)
        self.assertEqual(receipt["counters"]["formula_evaluations"], 0)
        self.assertFalse(receipt["formula_recomputed"])
        self.assertNotIn("private error detail", repr(receipt))

    def test_usage_and_formula_flags_and_counters_cannot_contradict_graph(self):
        _, audit, receipt = self.retrieve()
        changes = [lambda r: r.update(precomputed_npmi_used=False),
                   lambda r: r.update(formula_recomputed=True),
                   lambda r: r["counters"].update(precomputed_edges_loaded=2),
                   lambda r: r["counters"].update(candidate_edge_weight_uses=2),
                   lambda r: r["counters"].update(neighbor_ordering_weight_uses=3),
                   lambda r: r["stages"]["precomputed_lookup"].update(completed_calls=0),
                   lambda r: r.update(version=True)]
        for change in changes:
            changed = deepcopy(receipt)
            change(changed)
            with self.assertRaises(ValueError):
                validate_receipt(changed, graph=audit)


class NPMIFailedRuntimeTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.seed()

    def tearDown(self):
        self.teardown_fixture()

    async def test_failed_lookup_logs_real_started_stage_without_private_exception_text(self):
        adapter = SummaryAdapter([])
        runtime = Runtime(self.config, self.store, adapter, self.scratch)
        incoming = IncomingMessage("failed-npmi", "20", "10", "30", "Synthetic", "alpha topic",
                                   "2026-10-06T12:00:00+00:00")

        async def deliver(text):
            self.fail("failed retrieval must not generate or deliver")

        with redirect_stdout(io.StringIO()), patch.object(
                runtime.graph.query_index, "incident_edges", side_effect=sqlite3.OperationalError("private table detail")):
            await runtime.process(incoming, deliver)
        events = [fields for event, fields in self.scratch.events if event == "npmi_retrieval"]
        self.assertEqual(len(events), 1)
        fields = events[0]
        self.assertEqual(fields["event_id"], incoming.message_id)
        receipt = fields["telemetry"]
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["path"], "keyed_static_neighborhood_v1")
        self.assertEqual(receipt["stages"]["precomputed_lookup"]["calls"], 1)
        self.assertEqual(receipt["stages"]["precomputed_lookup"]["completed_calls"], 0)
        self.assertEqual(receipt["stages"]["candidate_scoring"]["calls"], 0)
        self.assertEqual(adapter.calls, [])
        self.assertNotIn("private table detail", repr(self.scratch.events))
