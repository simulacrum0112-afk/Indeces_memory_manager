"""Automatic path diagnostics from native synthetic frozen evidence, never hand paths."""
from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from indeces import path_hypotheses
from indeces.config import RuntimeConfig
from indeces.console import create_runtime
from indeces.requery_records import bundle_model_materials, validate_bundle
from indeces.run_records import digest, verify_runs
from tests import test_active_requery as active_fixture


QUESTION = "Aster links Brine during phase_one in bench."


def demand_action(*, target="beta", terms=("alpha",)):
    """A new mock provider action whose every surface is in QUESTION."""
    def anchor(surface):
        start = QUESTION.index(surface)
        return {"surface": surface, "span": [start, start + len(surface)]}
    return {"action": "query", "query": {
        "entities": [dict(anchor("Aster"), id="e1", canonical="alpha"),
                     dict(anchor("Brine"), id="e2", canonical=target)],
        "relations": [dict(anchor("links"), subject_id="e1", object_id="e2", predicate="links",
                           negated=False, direction="subject_to_object")],
        "negations": [], "time": anchor("phase_one"), "scope": anchor("bench"),
        "canonical_terms": list(terms), "ambiguities": []},
        "missing_evidence": ["Synthetic directed relation fixture is missing."],
        "clarification": "", "stop_reason": ""}


def fact_quote(subject="alpha", target="beta", relation="links"):
    return "typed_facts_v1: " + json.dumps({"facts": [{
        "subject": subject, "object": target, "relation": relation,
        "polarity": "positive", "time": "phase_one", "scope": "bench"}]}, separators=(",", ":"))


class PathHypothesesRuntimeConfigTests(unittest.TestCase):
    def test_default_off_flag_requires_an_actual_boolean(self):
        self.assertFalse(RuntimeConfig().path_hypotheses_enabled)
        self.assertTrue(RuntimeConfig(path_hypotheses_enabled=True).path_hypotheses_enabled)
        for value in (None, 0, 1, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RuntimeConfig(path_hypotheses_enabled=value)


class ActivePathHypothesesConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep imported TestCases inside a module rather than global aliases,
        # which avoids accidental repeated unittest discovery.
        self.fixture = active_fixture.ActiveConsoleTests("runTest")
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def configure(self, *, active=True, enabled=True, text=QUESTION, policy="legacy_v1"):
        f = self.fixture
        f.config.runtime = replace(f.config.runtime, active_requery_enabled=active,
            path_hypotheses_enabled=enabled, communicability_enabled=False, retrieval_policy=policy)
        f.message = replace(f.message, text=text)
        f.runtime = create_runtime(f.config, f.store, f.adapter, f.scratch)

    def add_source(self, quote, marks=("alpha", "beta"), source="synthetic:typed-path"):
        self.fixture.runtime.graph.add("10:knowledge", source, source + ":version", [
            {"text": quote, "quote": quote, "marks": list(marks)}], 1.0)

    def weights(self):
        return {table: [tuple(row) for row in self.fixture.store.db.execute(
            f"SELECT * FROM {table} ORDER BY scope,a,b")]
            for table in ("memory_static", "memory_dynamic")}

    def stages(self):
        return [stage for path, stage, _ in self.fixture.requests if path == "/responses"]

    def final_materials(self):
        context = self.fixture.event("reply_context")[0]
        data = json.loads(context["messages"][0]["content"].split("\n", 1)[1])
        return data["memory_citations"]

    def assert_complete(self, version=3):
        self.assertEqual(self.fixture.event("turn_start")[0]["run_record_version"], version)
        self.assertEqual(self.fixture.event("turn_end")[0]["status"], "delivered")
        report = verify_runs([self.fixture.entries()[0]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["complete"], 1)
        self.assertEqual(len(self.fixture.deliveries), 1)

    def diagnostic(self, evidence, kind="bundle"):
        events = self.fixture.event("path_hypotheses_diagnostic")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["evidence_kind"], kind)
        self.assertEqual(event["evidence_sha256"], digest(evidence))
        self.assertEqual(event["query_actions_sha256"], digest(event["query_actions"]))
        self.assertIs(event["independent_diagnostic"], True)
        self.assertEqual(event["receipt"]["schema"], "automatic_path_hypotheses_v1")
        self.assertIs(event["receipt"]["proof"], False)
        self.assertIs(event["receipt"]["changes_selection"], False)
        self.assertIs(event["receipt"]["changes_base_weights"], False)
        names = [entry["event"] for entry in self.fixture.entries()[1]]
        self.assertLess(names.index("path_hypotheses_diagnostic"), names.index("reply_context"))
        self.assertEqual(self.fixture.event("communicability_diagnostic"), [])
        return event

    def assert_action_bindings(self, event, bundle):
        actions = event["query_actions"]
        emitted = [entry for entry in self.fixture.event("requery_action") if entry["action"]["action"] == "query"]
        for wrapper in actions:
            round_ = bundle["rounds"][wrapper["evidence_round_index"]]
            self.assertEqual(wrapper["planning_call_id"], round_["planning_call_id"])
            self.assertEqual(wrapper["action_sha256"], digest(wrapper["action"]))
            self.assertIs(wrapper["anchors_validated"], True)
            matching = [entry for entry in emitted if entry["planning_call_id"] == wrapper["planning_call_id"]]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["action"], wrapper["action"])
            call = [entry for entry in self.fixture.event("call_end")
                    if entry["call_id"] == wrapper["planning_call_id"]]
            self.assertEqual(len(call), 1)
            self.assertEqual(json.loads(call[0]["result"]["text"]), wrapper["action"])
        self.assertEqual(self.final_materials(), bundle_model_materials(bundle))

    async def test_default_off_ordinary_turn_does_not_call_path_diagnostic(self):
        self.configure(active=False, enabled=False, text="amber")
        before = self.weights()
        with patch("indeces.path_hypotheses.analyze_path_hypotheses",
                   side_effect=AssertionError("disabled path diagnostics must not execute")) as analyze:
            await self.fixture.run_turn()
        analyze.assert_not_called()
        self.assertEqual(self.fixture.event("path_hypotheses_diagnostic"), [])
        self.assertEqual(self.stages(), ["selection", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete(1)

    async def test_default_off_active_turn_preserves_native_two_rounds(self):
        self.configure(enabled=False, text="mystery")
        before = self.weights()
        with patch("indeces.path_hypotheses.analyze_path_hypotheses",
                   side_effect=AssertionError("disabled path diagnostics must not execute")) as analyze:
            await self.fixture.run_turn()
        analyze.assert_not_called()
        self.assertEqual(self.fixture.event("path_hypotheses_diagnostic"), [])
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        self.assertEqual([round_["round_index"] for round_ in bundle["rounds"]], [0, 1, 2])
        self.assertEqual(self.final_materials(), bundle_model_materials(bundle))
        self.assertEqual(self.stages(), ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_native_quote_and_provider_action_automatically_propose_pending_path(self):
        self.configure()
        self.add_source(fact_quote())
        self.fixture.outputs = [demand_action(), active_fixture.done()]
        before = self.weights()
        native = path_hypotheses.analyze_path_hypotheses
        with patch("indeces.path_hypotheses.analyze_path_hypotheses", wraps=native) as analyze:
            await self.fixture.run_turn()
        analyze.assert_called_once()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        validate_bundle(bundle)
        self.assertEqual(analyze.call_args.args[0], bundle)
        event = self.diagnostic(bundle)
        self.assertEqual(analyze.call_args.args[1], event["query_actions"])
        self.assertEqual(len(event["query_actions"]), 1)
        self.assert_action_bindings(event, bundle)
        receipt = event["receipt"]
        pending = [candidate for candidate in receipt["candidates"] if candidate["status"] == "pending_hypothesis"]
        self.assertTrue(pending, receipt)
        self.assertIs(receipt["proposals_verified"], False)
        self.assertIs(receipt["truth_verified"], False)
        candidate = pending[0]
        self.assertEqual(candidate["path_nodes"], ["alpha", "beta"])
        self.assertEqual(candidate["round_index"], 1)
        self.assertEqual(candidate["planning_call_id"], bundle["rounds"][1]["planning_call_id"])
        self.assertTrue(candidate["edge_bindings"])
        binding_json = json.dumps(candidate["edge_bindings"])
        material = next(item for item in bundle["materials"] if item["material"]["source_id"] == "synthetic:typed-path")
        self.assertIn(material["evidence_uid"], binding_json)
        self.assertIn("synthetic:typed-path", binding_json)
        self.assertEqual(self.stages(), ["query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        ledger = self.fixture.saved_ledger()
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (192, 96))
        self.assert_complete()

    async def test_default_concept_policy_actual_rounds_plain_bodies_stay_unknown(self):
        self.configure(text="mystery", policy=RuntimeConfig().retrieval_policy)
        self.assertEqual(self.fixture.runtime.graph.retrieval_policy, "concept_v1")
        before = self.weights()
        await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        self.assertEqual([round_["round_index"] for round_ in bundle["rounds"]], [0, 1, 2])
        event = self.diagnostic(bundle)
        self.assertEqual(len(event["query_actions"]), 2)
        self.assert_action_bindings(event, bundle)
        self.assertEqual(event["receipt"]["status"], "unknown")
        self.assertFalse(any(candidate["status"] == "pending_hypothesis"
                             for candidate in event["receipt"]["candidates"]))
        self.assertEqual(self.stages(), ["query", "query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_ordinary_turn_has_no_logical_demand_or_new_planner_call(self):
        self.configure(active=False, text="alpha beta")
        self.add_source(fact_quote())
        before = self.weights()
        await self.fixture.run_turn()
        retrieval = self.fixture.event("retrieval_record")[0]["record"]
        event = self.diagnostic(retrieval, "retrieval")
        self.assertEqual(event["query_actions"], [])
        self.assertEqual(event["receipt"]["status"], "unknown")
        self.assertIn("no_logical_demand", json.dumps(event["receipt"]))
        self.assertEqual(self.final_materials(), retrieval["model_materials"])
        self.assertEqual(self.stages(), ["selection", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete(1)

    async def test_source_parser_exception_falls_back_without_calls_or_private_detail(self):
        self.configure()
        self.add_source(fact_quote())
        self.fixture.outputs = [demand_action(), active_fixture.done()]
        before = self.weights()
        private_fragment = "Synthetic private parsing fragment must remain absent."
        with patch("indeces.path_hypotheses._parse_typed_facts", side_effect=RuntimeError(private_fragment)) as parse:
            await self.fixture.run_turn()
        self.assertTrue(parse.called)
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        event = self.diagnostic(bundle)
        self.assert_action_bindings(event, bundle)
        self.assertEqual((event["receipt"]["status"], event["receipt"]["reason"], event["receipt"]["error_type"]),
                         ("unknown", "diagnostic_error", "RuntimeError"))
        self.assertNotIn(private_fragment, json.dumps(self.fixture.entries()[1]))
        self.assertNotIn(private_fragment, str(self.fixture.deliveries))
        self.assertEqual(self.stages(), ["query", "query", "reply"])
        ledger = self.fixture.saved_ledger()
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (192, 96))
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_invalid_json_source_cannot_become_a_pending_relation(self):
        self.configure()
        self.add_source('typed_facts_v1: {"facts":[{"subject":"alpha","object":"beta",}')
        self.fixture.outputs = [demand_action(), active_fixture.done()]
        before = self.weights()
        await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        event = self.diagnostic(bundle)
        self.assert_action_bindings(event, bundle)
        self.assertEqual(event["receipt"]["status"], "unknown")
        self.assertFalse(any(candidate["status"] == "pending_hypothesis"
                             for candidate in event["receipt"]["candidates"]))
        self.assertEqual(self.stages(), ["query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_reverse_source_direction_never_satisfies_forward_demand(self):
        self.configure()
        self.add_source(fact_quote("beta", "alpha"))
        self.fixture.outputs = [demand_action(), active_fixture.done()]
        before = self.weights()
        await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        event = self.diagnostic(bundle)
        self.assert_action_bindings(event, bundle)
        self.assertFalse(any(candidate["status"] == "pending_hypothesis"
                             for candidate in event["receipt"]["candidates"]))
        self.assertIn("direction", json.dumps(event["receipt"]))
        self.assertEqual(self.stages(), ["query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_native_multihop_fact_chain_has_no_authorized_relation_composition(self):
        self.configure()
        self.add_source(fact_quote(), source="synthetic:first-edge")
        self.add_source(fact_quote("beta", "gamma"), ("beta", "gamma"), "synthetic:second-edge")
        self.fixture.outputs = [demand_action(target="gamma"), active_fixture.done()]
        before = self.weights()
        await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        event = self.diagnostic(bundle)
        self.assert_action_bindings(event, bundle)
        chain = [candidate for candidate in event["receipt"]["candidates"]
                 if candidate["path_nodes"] == ["alpha", "beta", "gamma"]]
        self.assertTrue(chain, event["receipt"])
        self.assertFalse(any(candidate["status"] == "pending_hypothesis" for candidate in chain))
        self.assertIn("relation_composition_not_authorized", json.dumps(chain))
        self.assertEqual(self.stages(), ["query", "query", "reply"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()

    async def test_duplicate_query_preserves_only_successfully_appended_action_binding(self):
        self.configure()
        self.add_source(fact_quote())
        self.fixture.outputs = [demand_action(), demand_action()]
        before = self.weights()
        await self.fixture.run_turn()
        bundle = self.fixture.event("requery_evidence")[0]["bundle"]
        event = self.diagnostic(bundle)
        self.assertEqual(len(bundle["rounds"]), 2)
        self.assertEqual(len(event["query_actions"]), 1)
        self.assertEqual(len(self.fixture.event("requery_action")), 2)
        self.assert_action_bindings(event, bundle)
        self.assertEqual(self.fixture.event("requery_stop")[0]["reason"], "duplicate_query")
        self.assertEqual(self.stages(), ["query", "query"])
        self.assertEqual(self.weights(), before)
        self.assert_complete()


if __name__ == "__main__":
    unittest.main()
