"""Bounded optional-selector regression with synthetic state and transport."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import console
from indeces.config import RuntimeConfig, load_config
from indeces.model_selection import ModelNPMILabelSelector
from indeces.run_records import validate_graph_audit, validate_retrieval, verify_runs
from tests.test_model_selection import run_synthetic_console_trial
from tests import test_observer_service as observer_tests
from tests.test_selection_policy_integration import SelectorFixture


class OptionalModelSelectionConfigTests(unittest.TestCase):
    def load_toggle(self, literal=None):
        template = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
        lines = [line for line in template.splitlines()
                 if not line.startswith("model_selection_enabled =")]
        if literal is not None:
            lines.insert(lines.index("[runtime]") + 1, "model_selection_enabled = " + literal)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.toml"
            path.write_text("\n".join(lines), encoding="utf-8")
            return load_config(path)

    def test_default_true_and_explicit_values_preserve_other_settings(self):
        baseline = self.load_toggle()
        self.assertIs(baseline.runtime.model_selection_enabled, True)
        self.assertIs(RuntimeConfig().model_selection_enabled, True)
        for literal, enabled in (("true", True), ("false", False)):
            with self.subTest(literal=literal):
                config = self.load_toggle(literal)
                self.assertIs(config.runtime.model_selection_enabled, enabled)
                self.assertEqual(replace(config.runtime, model_selection_enabled=True), baseline.runtime)
                self.assertEqual(config.adapter, baseline.adapter)
                self.assertEqual(config.knowledge, baseline.knowledge)
                self.assertEqual(config.pdf, baseline.pdf)

    def test_direct_construction_rejects_non_boolean_values(self):
        for value in (0, 1, 0.0, 1.0, "true", "false", None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a boolean"):
                RuntimeConfig(model_selection_enabled=value)

    def test_toml_loading_rejects_non_boolean_values(self):
        for literal in ('"true"', '"false"', "0", "1", "0.0", "[]", "{}"):
            with self.subTest(literal=literal), self.assertRaisesRegex(ValueError, "must be a boolean"):
                self.load_toggle(literal)


class OptionalModelSelectionRuntimeTests(SelectorFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_selector_fixture()
        self.seed_candidates()

    def tearDown(self):
        self.teardown_selector_fixture()

    @staticmethod
    def events(result, name):
        return [entry["fields"] for entry in result["entries"] if entry["event"] == name]

    def assert_complete(self, result):
        retrieval = self.events(result, "retrieval_record")[0]["record"]
        validate_retrieval(retrieval)
        validate_graph_audit(retrieval["graph_audit"], retrieval)
        self.assertEqual([m["quote"] for m in retrieval["materials"]],
                         [m["quote"] for m in retrieval["model_materials"]])
        self.assertTrue(all(len(m["quote"]) == 400 for m in retrieval["model_materials"]))
        self.assertEqual(self.events(result, "turn_end")[0]["status"], "delivered")
        self.assertEqual(len(result["deliveries"]), 1)
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["complete"], 1)
        return retrieval

    async def test_explicit_true_retains_model_selection_record_contract(self):
        self.config.runtime = replace(self.config.runtime, model_selection_enabled=True)
        result = await run_synthetic_console_trial(self)
        self.assertIsInstance(result["runtime"].model_selector, ModelNPMILabelSelector)
        retrieval = self.assert_complete(result)
        self.assertEqual(retrieval["version"], 2)
        self.assertEqual(retrieval["model_projection"], "citation_material_selector_v1")
        self.assertEqual([m["id"] for m in retrieval["model_materials"]], result["choices"])
        self.assertEqual([stage for _, _, stage in result["requests"]],
                         ["selection", "selection", "reply", "reply"])
        self.assertEqual(result["call_counts"], {"prepare": 1, "finish": 1, "retrieve": 0})
        self.assertIn("model_selection_policy", self.events(result, "turn_start")[0])

    async def assert_disabled_contract(self, policy):
        self.config.runtime = replace(self.config.runtime, model_selection_enabled=False,
                                      retrieval_policy=policy)
        with patch("indeces.model_selection.ModelNPMILabelSelector",
                   side_effect=AssertionError("Disabled factory must not construct a selector")):
            result = await run_synthetic_console_trial(self)
        self.assertIsNone(result["runtime"].model_selector)
        self.assertIsNone(result["runtime"].graph.selector)
        self.assertEqual(result["runtime"].graph.retrieval_policy, policy)
        self.assertEqual(result["call_counts"], {"prepare": 0, "finish": 0, "retrieve": 1})
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "reply"), ("/responses", "reply")])
        self.assertEqual([fields["stage"] for fields in self.events(result, "call_start")], ["reply"])
        self.assertEqual([fields["stage"] for fields in self.events(result, "call_end")], ["reply"])
        self.assertNotIn("model_selection_policy", self.events(result, "turn_start")[0])
        for event in ("model_selection_input", "model_selection_decision"):
            self.assertEqual(self.events(result, event), [])
        retrieval = self.assert_complete(result)
        self.assertEqual(retrieval["version"], 1)
        self.assertEqual(len(retrieval["model_materials"]), 3)
        self.assertEqual(retrieval["model_projection"], "citation_material_v1")
        self.assertNotIn("selector_receipt_sha256", retrieval)
        selection = retrieval["graph_audit"]["selection"]
        self.assertNotIn("selector_receipt", selection)
        self.assertNotIn("selector_contract", selection)
        expected = [candidate["record_id"] for candidate in selection["ranked_candidates"][:3]]
        self.assertEqual([material["id"] for material in retrieval["model_materials"]], expected)

    async def test_false_uses_existing_concept_path_without_selection_calls(self):
        await self.assert_disabled_contract("concept_v1")

    async def test_false_preserves_explicit_legacy_path_without_selection_calls(self):
        await self.assert_disabled_contract("legacy_v1")


class OptionalModelSelectionStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        observer_tests.ObserverServiceTests.setUp(self)

    async def assert_startup_receipt(self, enabled):
        self.config = replace(self.config,
                              runtime=replace(self.config.runtime, model_selection_enabled=enabled))

        def fake_runtime(*args, selector=None):
            return SimpleNamespace(graph=object(), model_selector=selector)

        with observer_tests.ObserverServiceTests.resources(self), patch.object(console, "Runtime", side_effect=fake_runtime), \
                patch.object(console, "prepare_knowledge_directory"):
            await console.serve(self.config, "synthetic-key", "synthetic-token")
        starts = [fields for event, fields in self.scratch.records if event == "service_start"]
        self.assertEqual(len(starts), 1)
        self.assertIs(starts[0]["model_selection_enabled"], enabled)
        self.assertEqual(starts[0]["evidence_selection"],
                         "model_npmi_labels_v1" if enabled else self.config.runtime.retrieval_policy)
        self.adapter.call.assert_not_called()
        observer_tests.ObserverServiceTests.cleanup_assertions(self)

    async def test_enabled_startup_receipt_reports_model_mode(self):
        await self.assert_startup_receipt(True)

    async def test_disabled_startup_receipt_reports_deterministic_mode(self):
        await self.assert_startup_receipt(False)
