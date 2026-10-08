"""Console integration on fresh synthetic data and explicit mock transport only."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
import time
import unittest
from unittest.mock import patch

from indeces import communicability
from indeces.config import RuntimeConfig
from indeces.console import create_runtime
from indeces.requery_records import bundle_model_materials, validate_bundle
from indeces.run_records import digest, verify_runs
from tests import test_active_requery as active_fixture


class CommunicabilityRuntimeConfigTests(unittest.TestCase):
    def test_flag_defaults_off_and_rejects_non_boolean_values(self):
        self.assertFalse(RuntimeConfig().communicability_enabled)
        self.assertTrue(RuntimeConfig(communicability_enabled=True).communicability_enabled)
        for value in (0, 1, None, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeConfig(communicability_enabled=value)


class ActiveCommunicabilityConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition keeps the other module's TestCase out of this module's
        # discovery and reuses its explicit network-denying mock transport.
        self.fixture = active_fixture.ActiveConsoleTests("runTest")
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def configure(self, *, active, diagnostic, text=None, add_edge=False):
        f = self.fixture
        f.config.runtime = replace(f.config.runtime, active_requery_enabled=active,
                                   communicability_enabled=diagnostic)
        if text is not None:
            f.message = replace(f.message, text=text)
        f.runtime = create_runtime(f.config, f.store, f.adapter, f.scratch)
        if add_edge:
            text = "Synthetic amber prism paired observation is a mock fixture."
            f.runtime.graph.add("10:knowledge", "synthetic:pair", "synthetic-source-pair", [
                {"text": text, "quote": text, "marks": ["amber", "prism"]}], 1.0)

    def weights(self):
        return {table: [tuple(row) for row in self.fixture.store.db.execute(
            f"SELECT * FROM {table} ORDER BY scope,a,b")]
            for table in ("memory_static", "memory_dynamic")}

    def generation_stages(self):
        return [stage for path, stage, _ in self.fixture.requests if path == "/responses"]

    def reply_materials(self):
        payload = next(payload for path, stage, payload in self.fixture.requests
                       if path == "/responses" and stage == "reply")
        context = json.loads(payload["input"][0]["content"].split("\n", 1)[1])
        return context["memory_citations"]

    def assert_complete_record(self, version):
        f = self.fixture
        self.assertEqual(f.event("turn_start")[0]["run_record_version"], version)
        self.assertEqual(f.event("turn_end")[0]["status"], "delivered")
        report = verify_runs([f.entries()[0]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["complete"], 1)
        self.assertEqual(len(f.deliveries), 1)

    def assert_diagnostic(self, evidence, kind):
        f = self.fixture
        diagnostics = f.event("communicability_diagnostic")
        self.assertEqual(len(diagnostics), 1)
        diagnostic = diagnostics[0]
        self.assertEqual(diagnostic["evidence_kind"], kind)
        self.assertEqual(diagnostic["evidence_sha256"], digest(evidence))
        self.assertIs(diagnostic["independent_diagnostic"], True)
        receipt = diagnostic["receipt"]
        self.assertEqual(receipt["schema"], "communicability_offline_v1")
        self.assertIs(receipt["proof"], False)
        self.assertIs(receipt["changes_selection"], False)
        self.assertIs(receipt["changes_base_weights"], False)
        events = [entry["event"] for entry in f.entries()[1]]
        self.assertLess(events.index("retrieval_record"), events.index("communicability_diagnostic"))
        self.assertLess(events.index("communicability_diagnostic"), events.index("reply_context"))
        self.assertLess(events.index("reply_context"), events.index("answer_generated"))
        return receipt

    async def test_default_off_ordinary_turn_has_no_diagnostic_work_or_event(self):
        self.configure(active=False, diagnostic=False, text="amber", add_edge=True)
        before = self.weights()
        with patch("indeces.communicability.analyze_frozen_graph",
                   side_effect=AssertionError("disabled diagnostic must not execute")) as analyze:
            await self.fixture.run_turn()
        analyze.assert_not_called()
        self.assertEqual(self.fixture.event("communicability_diagnostic"), [])
        self.assertEqual(self.weights(), before)
        self.assertEqual(self.generation_stages(), ["selection", "reply"])
        retrieval = self.fixture.event("retrieval_record")[0]["record"]
        self.assertEqual(self.reply_materials(), retrieval["model_materials"])
        self.assert_complete_record(1)

    async def test_default_off_active_turn_preserves_two_rounds_and_version_three(self):
        self.configure(active=True, diagnostic=False)
        before = self.weights()
        with patch("indeces.communicability.analyze_frozen_graph",
                   side_effect=AssertionError("disabled diagnostic must not execute")) as analyze:
            await self.fixture.run_turn()
        analyze.assert_not_called()
        self.assertEqual(self.fixture.event("communicability_diagnostic"), [])
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        self.assertEqual([round_["round_index"] for round_ in bundle["rounds"]], [0, 1, 2])
        self.assertEqual(self.reply_materials(), bundle_model_materials(bundle))
        self.assertEqual(self.generation_stages(), ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(3)

    async def test_enabled_ordinary_turn_diagnoses_frozen_retrieval_independently(self):
        self.configure(active=False, diagnostic=True, text="amber", add_edge=True)
        before = self.weights()
        captured = []
        native = communicability.analyze_frozen_graph
        def inspect(evidence, **kwargs):
            captured.append((deepcopy(evidence), kwargs["config"], kwargs["deadline"], time.monotonic()))
            result = native(evidence, **kwargs)
            self.assertEqual(evidence, captured[-1][0])
            return result
        with patch("indeces.communicability.analyze_frozen_graph", side_effect=inspect) as analyze:
            await self.fixture.run_turn()
        analyze.assert_called_once()
        retrieval = self.fixture.event("retrieval_record")[0]["record"]
        self.assertEqual(captured[0][0], retrieval)
        self.assertTrue(captured[0][1].enabled)
        self.assertTrue(math.isfinite(captured[0][2]))
        self.assertLessEqual(captured[0][2], captured[0][3] + 0.5)
        receipt = self.assert_diagnostic(retrieval, "retrieval")
        self.assertTrue(receipt["edges"])
        self.assertEqual(receipt["semantic_status"], "insufficient_relation_semantics")
        self.assertIs(receipt["index_directions_are_semantic_relations"], False)
        self.assertEqual(self.reply_materials(), retrieval["model_materials"])
        self.assertEqual(self.generation_stages(), ["selection", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(1)

    async def test_enabled_two_rounds_diagnose_final_bundle_once_before_reply(self):
        self.configure(active=True, diagnostic=True, add_edge=True)
        before = self.weights()
        native = communicability.analyze_frozen_graph
        with patch("indeces.communicability.analyze_frozen_graph", wraps=native) as analyze:
            await self.fixture.run_turn()
        analyze.assert_called_once()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        validate_bundle(bundle)
        self.assertEqual([round_["round_index"] for round_ in bundle["rounds"]], [0, 1, 2])
        self.assertEqual(analyze.call_args.args[0], bundle)
        receipt = self.assert_diagnostic(bundle, "bundle")
        self.assertNotEqual(receipt["status"], "unknown")
        self.assertTrue(receipt["edges"])
        self.assertEqual(receipt["semantic_status"], "insufficient_relation_semantics")
        events = [entry["event"] for entry in self.fixture.entries()[1]]
        self.assertLess(events.index("requery_evidence"), events.index("communicability_diagnostic"))
        self.assertEqual(self.reply_materials(), bundle_model_materials(bundle))
        self.assertEqual(self.generation_stages(), ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(3)

    async def test_diagnostic_exception_does_not_leak_or_block_ordinary_reply(self):
        self.configure(active=False, diagnostic=True, text="amber", add_edge=True)
        private_fragment = "Synthetic private diagnostic fragment must stay absent."
        before = self.weights()
        with patch("indeces.communicability.analyze_frozen_graph", side_effect=RuntimeError(private_fragment)):
            await self.fixture.run_turn()
        retrieval = self.fixture.event("retrieval_record")[0]["record"]
        receipt = self.assert_diagnostic(retrieval, "retrieval")
        self.assertEqual((receipt["status"], receipt["reason"], receipt["error_type"]),
                         ("unknown", "diagnostic_error", "RuntimeError"))
        self.assertNotIn(private_fragment, json.dumps(self.fixture.entries()[1]))
        self.assertNotIn(private_fragment, str(self.fixture.deliveries))
        self.assertEqual(self.reply_materials(), retrieval["model_materials"])
        self.assertEqual(self.generation_stages(), ["selection", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(1)

    async def test_diagnostic_exception_preserves_active_evidence_and_budget_ledger(self):
        self.configure(active=True, diagnostic=True)
        before = self.weights()
        with patch("indeces.communicability.analyze_frozen_graph", side_effect=ValueError("Synthetic hidden detail")):
            await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        receipt = self.assert_diagnostic(bundle, "bundle")
        self.assertEqual(receipt["error_type"], "ValueError")
        self.assertNotIn("Synthetic hidden detail", json.dumps(self.fixture.entries()[1]))
        self.assertEqual(self.reply_materials(), bundle_model_materials(bundle))
        ledger = self.fixture.saved_ledger()
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual([call["stage"] for call in ledger["calls"]], ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(3)

    async def test_numerical_budget_stop_keeps_original_selection_and_reply(self):
        self.configure(active=False, diagnostic=True, text="amber", add_edge=True)
        before = self.weights()
        native = communicability.analyze_frozen_graph
        def limited_projection(evidence, **kwargs):
            kwargs["config"] = replace(kwargs["config"], max_projection_terms=1)
            return native(evidence, **kwargs)
        with patch("indeces.communicability.analyze_frozen_graph", side_effect=limited_projection):
            await self.fixture.run_turn()
        retrieval = self.fixture.event("retrieval_record")[0]["record"]
        receipt = self.assert_diagnostic(retrieval, "retrieval")
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertIs(receipt["numerical"]["accepted"], False)
        self.assertEqual(receipt["numerical"]["reason"], "projection_terms")
        self.assertEqual(receipt["numerical"]["matvecs"], 2)
        self.assertEqual(self.reply_materials(), retrieval["model_materials"])
        self.assertEqual(self.generation_stages(), ["selection", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(1)

    async def test_diagnostic_scores_stay_out_of_model_context_and_selection(self):
        self.configure(active=True, diagnostic=True)
        before = self.weights()
        synthetic_receipt = {"schema": "communicability_offline_v1", "status": "converged",
            "proof": False, "changes_selection": False, "changes_base_weights": False,
            "scores": [{"node": "Synthetic diagnostic-only sentinel", "value": 9999.0}]}
        with patch("indeces.communicability.analyze_frozen_graph", return_value=synthetic_receipt):
            await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        self.assert_diagnostic(bundle, "bundle")
        self.assertEqual(self.reply_materials(), bundle_model_materials(bundle))
        model_payloads = json.dumps([payload for path, _, payload in self.fixture.requests if path == "/responses"])
        self.assertNotIn("Synthetic diagnostic-only sentinel", model_payloads)
        self.assertEqual(self.generation_stages(), ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete_record(3)

    async def test_diagnostic_audit_write_failure_retains_failure_contract(self):
        self.configure(active=False, diagnostic=True, text="amber", add_edge=True)
        native_write = self.fixture.scratch.write
        def broken_audit(event, **fields):
            if event == "communicability_diagnostic":
                raise RuntimeError("Synthetic private audit failure detail")
            return native_write(event, **fields)
        with patch.object(self.fixture.scratch, "write", side_effect=broken_audit):
            await self.fixture.run_turn()
        self.assertEqual(self.fixture.event("turn_end")[0]["status"], "failed")
        self.assertEqual(self.fixture.event("turn_end")[0]["code"], "RuntimeError")
        self.assertEqual(self.fixture.event("answer_generated"), [])
        self.assertEqual(self.fixture.event("answer_delivered"), [])
        self.assertEqual(self.generation_stages(), ["selection"])
        self.assertEqual(len(self.fixture.deliveries), 1)
        self.assertNotIn("Synthetic private audit failure detail", json.dumps(self.fixture.entries()[1]))
        self.assertNotIn("Synthetic private audit failure detail", str(self.fixture.deliveries))


if __name__ == "__main__":
    unittest.main()
