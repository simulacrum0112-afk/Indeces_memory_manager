"""Synthetic native two-source, two-round path feedback controls.

Only the HTTP-shaped model transport is mocked. Sources, NPMI/DF, retrieval,
selection, frozen bundles, ledger and final context use the actual offline code.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from indeces.console import create_runtime
from indeces.contracts import GovernedError
from indeces.memory import MemoryGraph
from indeces.path_requery_loop import PathRequeryLoopConfig
from indeces.requery_records import bundle_model_materials
from indeces.run_records import digest, verify_runs
from tests import test_active_requery as legacy
from tests.test_active_path_requery_loop import goal_action, query_plan


def source_quote(subject, object_, *, polarity="positive", conditions=None,
                 extra_facts=()):
    fact = {"subject": subject, "object": object_, "relation": "links",
            "polarity": polarity}
    if conditions is not None:
        fact["conditions"] = conditions
    return "typed_facts_v1: " + json.dumps({"facts": [fact, *extra_facts]},
                                          separators=(",", ":"))


class ActivePathRequeryC9Tests(unittest.IsolatedAsyncioTestCase):
    def make_fixture(self, *, enabled=True, second_kind="positive", path_config=None,
                     consume_feedback=False):
        fixture = legacy.ActiveConsoleTests("runTest")
        fixture.setUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.config.runtime = replace(fixture.config.runtime,
                                         path_requery_loop_enabled=enabled)
        fixture.runtime = create_runtime(fixture.config, fixture.store,
                                         fixture.adapter, fixture.scratch)
        fixture.runtime.path_requery_loop_config = path_config or PathRequeryLoopConfig(
            enabled=True, max_seconds=3, composition_rules=({
                "rule_id": "synthetic-links-composition", "relation1": "links",
                "relation2": "links", "result": "links",
            },))
        fixture.message = replace(fixture.message, text="alpha links gamma")
        fixture.outputs = [goal_action(), legacy.done()]
        quote_a = source_quote("alpha", "beta")
        if second_kind == "reverse":
            quote_b = source_quote("gamma", "beta")
        elif second_kind == "negative":
            quote_b = source_quote("beta", "gamma", polarity="negative")
        elif second_kind == "conditional":
            quote_b = source_quote("beta", "gamma", conditions=["synthetic K"])
        elif second_kind == "conflict":
            quote_b = source_quote("beta", "gamma", extra_facts=({
                "subject": "beta", "object": "gamma", "relation": "links",
                "polarity": "negative"},))
        else:
            quote_b = source_quote("beta", "gamma")
        b = fixture.runtime.graph.add("10:knowledge", "synthetic:c9-source-B",
            "synthetic-author-B", [{"text": quote_b, "quote": quote_b,
                                     "marks": ["beta", "gamma"]}], 1.0)[0]
        a = fixture.runtime.graph.add("10:knowledge", "synthetic:c9-source-A",
            "synthetic-author-A", [{"text": quote_a, "quote": quote_a,
                                     "marks": ["alpha", "beta"]}], 1.0)[0]
        fixture.source_ids = {"A": a["id"], "B": b["id"]}
        fixture.source_quotes = {"A": quote_a, "B": quote_b}
        # The two alpha-gamma bibliography records occupy two initial slots.
        # Native initial beta expansion ties A and B; the newer A wins slot
        # three. After beta becomes direct, the true NPMI order BG > AB puts
        # B in slot three and A below the three-reference limit. No individual
        # round contains both chain bodies. Bibliography cannot supply clues.
        fixture.decoy_ids = set()
        for name in ("entry-one", "entry-two"):
            quote = "Bibliography\nSynthetic alpha gamma publication " + name + "."
            row = fixture.runtime.graph.add("10:knowledge", "synthetic:c9-" + name,
                "synthetic-author", [{"text": quote, "quote": quote,
                                        "marks": ["alpha", "gamma"]}], 1.0)[0]
            fixture.decoy_ids.add(row["id"])
        # N=20, alpha DF=4, beta DF=2, gamma DF=3; native positive weights
        # are AB=.3059, BG=.4019 and AG=.5229. This one alpha-only record
        # and unrelated observations alter only actual synthetic source DF.
        quote = "Synthetic alpha endpoint observation."
        fixture.runtime.graph.add("10:knowledge", "synthetic:c9-alpha-only",
            "synthetic-author", [{"text": quote, "quote": quote,
                                    "marks": ["alpha"]}], 1.0)
        for number in range(13):
            quote = f"Synthetic unrelated background observation {number}."
            fixture.runtime.graph.add("10:knowledge", f"synthetic:c9-background-{number}",
                "synthetic-author", [{"text": quote, "quote": quote,
                                        "marks": [f"unrelated{number}"]}], 1.0)
        fixture.planning_inputs = []
        fixture.feedback_was_consumed = False
        original_transport = fixture.transport

        async def transport(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            if path == "/responses" and stage == "query":
                plan = query_plan(payload)
                fixture.planning_inputs.append(deepcopy(plan))
                if consume_feedback and fixture.query_index > 0:
                    feedback = plan.get("path_feedback")
                    fixture.feedback_was_consumed = bool(feedback and
                        feedback["result"].get("accumulated_candidate_indices") and
                        plan.get("path_feedback_sha256") == digest(feedback))
                    fixture.outputs[1] = (legacy.done() if fixture.feedback_was_consumed else
                        dict(legacy.done(), action="stop", stop_reason="requested_stop"))
            return await original_transport(path, payload)

        fixture.adapter._request_override = transport
        return fixture

    async def run_captured(self, fixture, *, verify=True):
        calls = []
        original = MemoryGraph.retrieve

        def retrieve(graph, scope, marks, query, now, **kwargs):
            records = original(graph, scope, marks, query, now, **kwargs)
            audit = kwargs.get("audit") or {}
            calls.append({"event_id": kwargs.get("event_id"), "scope": scope,
                "marks": deepcopy(marks), "query": query,
                "selected_ids": [row["id"] for row in records],
                "direct_hits": deepcopy(audit.get("match", {}).get("direct_hits", [])),
                "ranked_ids": [row["record_id"] for row in
                    audit.get("selection", {}).get("ranked_candidates", [])]})
            return records

        before = sorted(tuple(row) for row in fixture.store.db.execute("SELECT * FROM memory_static"))
        with patch.object(MemoryGraph, "retrieve", autospec=True, side_effect=retrieve):
            await fixture.run_turn()
        self.assertEqual(sorted(tuple(row) for row in fixture.store.db.execute("SELECT * FROM memory_static")),
                         before)
        if verify:
            report = verify_runs([fixture.entries()[0]])
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["counts"]["complete"], 1)
        fixture.captured_calls = calls
        return calls

    def assert_initial_preconditions(self, fixture):
        initial = fixture.event("requery_retrieval")[0]["record"]
        ids = {item["record_id"] for item in initial["materials"]}
        self.assertIn(fixture.source_ids["A"], ids)
        self.assertNotIn(fixture.source_ids["B"], ids,
                         "The missing source must not already be frozen in round zero")
        self.assertEqual(ids, {fixture.source_ids["A"], *fixture.decoy_ids})
        ranked = {row["record_id"] for row in initial["graph_audit"]["selection"]["ranked_candidates"]}
        self.assertIn(fixture.source_ids["B"], ranked,
                      "The native candidate audit may contain B, but B is not frozen evidence")
        return initial

    @staticmethod
    def generation_stages(fixture):
        return [stage for path, stage, _ in fixture.requests if path == "/responses"]

    async def test_fixture_native_initial_selection_has_only_first_chain_source(self):
        fixture = self.make_fixture(enabled=False)
        await self.run_captured(fixture)
        self.assert_initial_preconditions(fixture)

    def assert_strict_two_round_sources(self, fixture):
        initial = self.assert_initial_preconditions(fixture)
        rounds = fixture.event("requery_retrieval")
        self.assertEqual([round_["round_index"] for round_ in rounds], [0, 1])
        second = rounds[1]["record"]
        selected = {row["record_id"] for row in second["materials"]}
        self.assertIn(fixture.source_ids["B"], selected)
        self.assertNotIn(fixture.source_ids["A"], selected,
                         "A must not reappear: no individual round may contain both chain bodies")
        pre = fixture.event("path_requery_feedback")[0]
        post = fixture.event("path_requery_feedback_result")[0]
        receipt, result = pre["receipt"], post["result_receipt"]
        self.assertEqual(receipt["added_terms"], ["beta"])
        self.assertFalse(any(row["structural_support_qualified"]
                             for row in receipt["candidates"]),
                         "Unselected B must not supply the initial path")
        self.assertFalse(any(row["formal_derivation"] for row in receipt["candidates"]))
        beta = next(row for row in receipt["clues"] if row["term"] == "beta")
        self.assertEqual({row.get("source_record_id", row.get("record_id"))
                          for row in beta["provenance"]}, {fixture.source_ids["A"]})
        self.assertEqual({row["round_index"] for row in beta["provenance"]}, {0})
        self.assertTrue(receipt["frontier_clues"], "A half-edge supplies the missing-edge clue")
        query, = fixture.captured_calls
        self.assertIn("beta", query["direct_hits"])
        self.assertEqual(query["marks"], receipt["effective_terms"])
        self.assertEqual(query["selected_ids"], [row["record_id"] for row in second["materials"]])
        self.assertEqual(post["actual_query_sha256"], digest(query["query"]))
        self.assertEqual(post["receipt_sha256"], digest(receipt))
        self.assertEqual(post["result_receipt_sha256"], digest(result))
        accumulated = [result["candidates"][index]
                       for index in result["accumulated_candidate_indices"]]
        candidate = next(row for row in accumulated if row["path_nodes"] == ["alpha", "beta", "gamma"])
        self.assertEqual(candidate["analysis_scope"], "current_task_frozen_rounds")
        self.assertEqual(candidate["round_indices"], [0, 1])
        self.assertIsNone(candidate["round_index"])
        sources = candidate["edge_source_bindings"]
        by_id = {row.get("source_record_id", row.get("record_id")): row for row in sources}
        self.assertEqual(set(by_id), set(fixture.source_ids.values()))
        self.assertEqual(by_id[fixture.source_ids["A"]]["round_index"], 0)
        self.assertEqual(by_id[fixture.source_ids["B"]]["round_index"], 1)
        self.assertEqual(by_id[fixture.source_ids["A"]]["record_sha256"], digest(initial))
        self.assertEqual(by_id[fixture.source_ids["B"]]["record_sha256"], digest(second))
        self.assertEqual(len({row["source_id"] for row in sources}), 2)
        self.assertEqual(len({row["evidence_uid"] for row in sources}), 2)
        self.assertTrue(all(row["source_version"]["kind"] == "record_content_version"
                            for row in sources))
        self.assertTrue(all(not row["proof"] and not row["truth_verified"]
                            and not row["semantic_support_verified"] for row in accumulated))
        self.assertFalse(any(row["structural_support_qualified"]
                             for row in result["candidates"] if row["round_index"] is not None),
                         "The result must depend on accumulation rather than a complete single round")
        return candidate, result

    def assert_final_sources_and_ledger(self, fixture):
        bundle = fixture.event("requery_evidence")[0]["bundle"]
        reply = next(payload for path, stage, payload in fixture.requests
                     if path == "/responses" and stage == "reply")
        context = json.loads(reply["input"][0]["content"].split("\n", 1)[1])
        materials = bundle_model_materials(bundle)
        self.assertEqual(context["memory_citations"], materials)
        selected = {row["id"]: row for row in materials
                    if row["id"] in fixture.source_ids.values()}
        self.assertEqual(set(selected), set(fixture.source_ids.values()))
        self.assertEqual(selected[fixture.source_ids["A"]]["round_index"], 0)
        self.assertEqual(selected[fixture.source_ids["B"]]["round_index"], 1)
        self.assertEqual({row["quote"] for row in selected.values()}, set(fixture.source_quotes.values()))
        self.assertEqual(len({row["citation_id"] for row in selected.values()}), 2)
        self.assertTrue(all(row["citation_marker"] == f'[{row["citation_id"]}]'
                            for row in selected.values()))
        ledger = fixture.saved_ledger()
        self.assertTrue(ledger["usage_complete"])
        self.assertEqual(ledger["automatic_retries"], 0)
        self.assertEqual(ledger["unknown_generation_count"], 0)
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual(len(fixture.event("answer_delivered")), 1)
        self.assertEqual(fixture.event("answer_delivered")[0]["bundle_sha256"], digest(bundle))
        return bundle, ledger

    async def test_frontier_recovers_missing_second_source_and_accumulates_cross_round_path(self):
        fixture = self.make_fixture(consume_feedback=True)
        await self.run_captured(fixture)
        candidate, result = self.assert_strict_two_round_sources(fixture)
        formal = candidate["formal_derivation"]
        self.assertIsNotNone(formal)
        self.assertEqual(formal["status"], "formal_under_declared_rule")
        self.assertEqual(formal["rule_ids"], ["synthetic-links-composition"])
        self.assertFalse(formal["proof"])
        self.assertFalse(formal["truth_verified"])
        self.assertFalse(formal["semantic_support_verified"])
        self.assertTrue(fixture.feedback_was_consumed)
        self.assertIsNone(fixture.planning_inputs[0]["path_feedback"])
        pre = fixture.event("path_requery_feedback")[0]["receipt"]
        expected = {"input": pre, "result": result}
        self.assertEqual(fixture.planning_inputs[1]["path_feedback"], expected)
        self.assertEqual(fixture.planning_inputs[1]["path_feedback_sha256"], digest(expected))
        self.assertEqual(self.generation_stages(fixture), ["selection", "query", "query", "reply"])
        self.assert_final_sources_and_ledger(fixture)

    async def test_reverse_negative_condition_and_conflict_claims_do_not_authorize_composition(self):
        for kind in ("reverse", "negative", "conditional", "conflict"):
            with self.subTest(kind=kind):
                fixture = self.make_fixture(second_kind=kind)
                await self.run_captured(fixture)
                candidate, _ = self.assert_strict_two_round_sources(fixture)
                self.assertIsNone(candidate["formal_derivation"])
                binding = next(row for row in candidate["edge_bindings"]
                    if row.get("source_record_id", row.get("record_id")) == fixture.source_ids["B"])
                fact = binding["fact"]
                if kind == "reverse":
                    self.assertEqual((fact["subject"], fact["object"]), ("gamma", "beta"))
                elif kind == "negative":
                    self.assertEqual(fact["polarity"], "negative")
                elif kind == "conditional":
                    self.assertEqual(fact["conditions"], ["synthetic K"])
                else:
                    self.assertEqual({row["fact"]["polarity"] for row in candidate["edge_bindings"]
                        if row.get("source_record_id", row.get("record_id")) == fixture.source_ids["B"]},
                        {"positive", "negative"})
                self.assert_final_sources_and_ledger(fixture)

    async def test_off_and_on_share_initial_frozen_input_and_unmodified_cumulative_limits(self):
        initial_hashes, ledgers = [], []
        for enabled in (False, True):
            fixture = self.make_fixture(enabled=enabled)
            with patch("time.time", return_value=1720000000.0):
                await self.run_captured(fixture)
            initial = self.assert_initial_preconditions(fixture)
            initial_hashes.append(digest(initial))
            ledger = fixture.saved_ledger()
            ledgers.append(ledger)
            self.assertEqual(ledger["automatic_retries"], 0)
            self.assertEqual(ledger["unknown_generation_count"], 0)
            self.assertTrue(ledger["usage_complete"])
            if enabled:
                self.assert_strict_two_round_sources(fixture)
                self.assert_final_sources_and_ledger(fixture)
            else:
                self.assertEqual(fixture.event("path_requery_feedback"), [])
                self.assertEqual(fixture.event("path_requery_feedback_result"), [])
                self.assertNotIn(fixture.source_ids["B"], {row["material"]["record_id"]
                    for row in fixture.event("requery_evidence")[0]["bundle"]["materials"]})
                self.assertTrue(all("path_feedback" not in row and "path_feedback_sha256" not in row
                                    for row in fixture.planning_inputs))
                self.assertTrue(all("effective_terms" not in row and "actual_query_sha256" not in row
                                    for row in fixture.event("requery_retrieval")))
                self.assertEqual(self.generation_stages(fixture), ["selection", "query", "reply"])
        self.assertEqual(initial_hashes[0], initial_hashes[1])
        self.assertEqual(ledgers[0]["limits"], ledgers[1]["limits"])

    async def test_path_cutoff_call_token_deadline_and_unknown_usage_stop_without_extra_transport(self):
        for kind in ("path", "call", "token", "deadline", "unknown"):
            with self.subTest(kind=kind):
                fixture = self.make_fixture(path_config=PathRequeryLoopConfig(
                    enabled=True, max_seconds=3, max_nodes=1) if kind == "path" else None)
                if kind == "call":
                    fixture.config.runtime = replace(fixture.config.runtime, active_requery_max_calls=1)
                elif kind == "token":
                    fixture.config.runtime = replace(fixture.config.runtime, active_requery_input_tokens=128)
                elif kind == "deadline":
                    fixture.config.runtime = replace(fixture.config.runtime, active_requery_seconds=0.000001)
                elif kind == "unknown":
                    fixture.query_error = GovernedError("synthetic_network_loss")
                await self.run_captured(fixture, verify=kind != "deadline")
                if kind == "deadline":
                    report = verify_runs([fixture.entries()[0]])
                    self.assertEqual(report["issues"], [])
                    self.assertEqual(fixture.event("turn_end")[0]["code"], "requery_time_budget")
                self.assertEqual(fixture.captured_calls, [])
                self.assertEqual(fixture.event("path_requery_feedback_result"), [])
                stages = self.generation_stages(fixture)
                self.assertNotIn("reply", stages)
                ledger = fixture.saved_ledger()
                self.assertEqual(ledger["automatic_retries"], 0)
                if kind == "unknown":
                    self.assertEqual(stages, ["selection", "query"])
                    self.assertEqual(ledger["halted"], "requery_usage_unknown")
                    self.assertFalse(ledger["usage_complete"])
                    self.assertEqual(ledger["unknown_generation_count"], 1)
                else:
                    self.assertTrue(ledger["usage_complete"])
                    self.assertEqual(ledger["unknown_generation_count"], 0)
                    self.assertLessEqual(len(stages), 2)
                before = len(fixture.requests)
                with self.assertRaisesRegex(GovernedError, "already_recorded"):
                    legacy.TurnBudgetAdapter(fixture.adapter, fixture.config, fixture.scratch,
                        "synthetic-new-trace", fixture.message.message_id, fixture.message.scope)
                self.assertEqual(len(fixture.requests), before)

    async def test_accumulated_feedback_remains_subject_to_next_planner_call_and_unknown_gates(self):
        for kind in ("call", "unknown"):
            with self.subTest(kind=kind):
                fixture = self.make_fixture()
                if kind == "call":
                    fixture.config.runtime = replace(fixture.config.runtime, active_requery_max_calls=3)
                else:
                    previous = fixture.adapter._request_override

                    async def fail_second_query(path, payload):
                        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
                        if path == "/responses" and stage == "query" and fixture.query_index >= 1:
                            fixture.query_error = GovernedError("synthetic_network_loss_after_accumulation")
                        return await previous(path, payload)

                    fixture.adapter._request_override = fail_second_query
                await self.run_captured(fixture)
                self.assert_strict_two_round_sources(fixture)
                self.assertEqual(len(fixture.event("path_requery_feedback_result")), 1)
                self.assertEqual(len(fixture.captured_calls), 1)
                self.assertNotIn("reply", self.generation_stages(fixture))
                ledger = fixture.saved_ledger()
                self.assertEqual(ledger["automatic_retries"], 0)
                if kind == "call":
                    # One reply call stays reserved. The unmodified admission
                    # rule rejects the next planner at 2 completed calls / cap3.
                    self.assertEqual(self.generation_stages(fixture), ["selection", "query"])
                    self.assertIn("requery_call_budget", [row["reason"] for row in fixture.event("requery_stop")])
                    self.assertTrue(ledger["usage_complete"])
                    self.assertEqual(ledger["unknown_generation_count"], 0)
                else:
                    self.assertEqual(self.generation_stages(fixture), ["selection", "query", "query"])
                    self.assertEqual(ledger["halted"], "requery_usage_unknown")
                    self.assertFalse(ledger["usage_complete"])
                    self.assertEqual(ledger["unknown_generation_count"], 1)
                    self.assertIsNone(ledger["calls"][-1]["usage"])
                    # The request really consumed the accumulated payload
                    # before the unknown response closed further admission.
                    feedback = fixture.planning_inputs[1]["path_feedback"]
                    self.assertTrue(feedback["result"]["accumulated_candidate_indices"])
                    self.assertEqual(fixture.planning_inputs[1]["path_feedback_sha256"], digest(feedback))
                before = len(fixture.requests)
                with self.assertRaisesRegex(GovernedError, "already_recorded"):
                    legacy.TurnBudgetAdapter(fixture.adapter, fixture.config, fixture.scratch,
                        "synthetic-reopen-trace", fixture.message.message_id, fixture.message.scope)
                self.assertEqual(len(fixture.requests), before)


if __name__ == "__main__":
    unittest.main()
