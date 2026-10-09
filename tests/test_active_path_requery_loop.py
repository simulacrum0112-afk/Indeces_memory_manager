"""Native temporary graph and HTTP-shaped mocks for path feedback wiring.

This module composes the existing offline fixture without inheriting its tests.
Every source, prompt, response and credential below is synthetic.
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


def goal_action():
    return {
        "action": "query",
        "query": {
            "entities": [
                {"id": "e1", "surface": "alpha", "canonical": "alpha", "span": [0, 5]},
                {"id": "e2", "surface": "gamma", "canonical": "gamma", "span": [12, 17]},
            ],
            "relations": [{"subject_id": "e1", "object_id": "e2", "predicate": "links",
                           "surface": "links", "span": [6, 11], "negated": False,
                           "direction": "subject_to_object"}],
            "negations": [], "time": None, "scope": None,
            "canonical_terms": ["alpha", "gamma"], "ambiguities": [],
        },
        "missing_evidence": ["Synthetic relation gap"],
        "clarification": "", "stop_reason": "",
    }


def typed_quote(subject, object_):
    return "typed_facts_v1: " + json.dumps({"facts": [{
        "subject": subject, "object": object_, "relation": "links", "polarity": "positive",
    }]}, separators=(",", ":"))


def query_plan(payload):
    return json.loads(payload["input"][-1]["content"])


class ActivePathRequeryLoopTests(unittest.IsolatedAsyncioTestCase):
    def make_fixture(self, *, enabled=True, path_config=None, consume_feedback=False):
        fixture = legacy.ActiveConsoleTests("runTest")
        fixture.setUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.config.runtime = replace(fixture.config.runtime,
                                         path_requery_loop_enabled=enabled)
        fixture.runtime = create_runtime(fixture.config, fixture.store,
                                         fixture.adapter, fixture.scratch)
        fixture.runtime.path_requery_loop_config = path_config or PathRequeryLoopConfig(
            enabled=True, max_seconds=3)
        fixture.message = replace(fixture.message, text="alpha links gamma")
        fixture.outputs = [goal_action(), legacy.done()]

        # The fixture's existing amber/cobalt records are unrelated background.
        # Both chain records have native IDs and the same synthetic source.
        chunks = []
        for subject, object_ in (("alpha", "beta"), ("beta", "gamma")):
            text = typed_quote(subject, object_)
            chunks.append({"text": text, "quote": text, "marks": [subject, object_]})
        chain = fixture.runtime.graph.add("10:knowledge", "synthetic:loop-chain",
                                           "synthetic-author", chunks, 1.0)
        decoy_text = "Synthetic alpha endpoint observation without a typed relation."
        decoy = fixture.runtime.graph.add("10:knowledge", "synthetic:loop-decoy",
            "synthetic-author", [{"text": decoy_text, "quote": decoy_text,
                                    "marks": ["alpha"]}], 1.0)[0]
        beta_text = "Synthetic beta-only observation located by a direct beta query."
        beta = fixture.runtime.graph.add("10:knowledge", "synthetic:loop-beta-only",
            "synthetic-author", [{"text": beta_text, "quote": beta_text,
                                   "marks": ["beta"]}], 1.0)[0]
        # With six records alpha df=2, beta df=3 and co=1 make alpha-beta
        # NPMI exactly zero. This unrelated record keeps both native chain
        # edges positive without injecting or changing any graph weights.
        background = "Synthetic unrelated observation for the native graph fixture."
        fixture.runtime.graph.add("10:knowledge", "synthetic:loop-background",
            "synthetic-author", [{"text": background, "quote": background,
                                   "marks": ["isolated"]}], 1.0)
        fixture.chain_ids = {row["id"] for row in chain}
        fixture.decoy_id = decoy["id"]
        fixture.beta_id = beta["id"]
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
                        "beta" in feedback["result"]["effective_terms"] and
                        plan.get("path_feedback_sha256") == digest(feedback))
                    fixture.outputs[1] = (legacy.done() if fixture.feedback_was_consumed else
                        dict(legacy.done(), action="stop", stop_reason="synthetic_missing_feedback"))
            return await original_transport(path, payload)

        fixture.adapter._request_override = transport
        return fixture

    async def run_captured(self, fixture):
        calls = []
        original = MemoryGraph.retrieve

        def retrieve(graph, scope, marks, query, now, **kwargs):
            records = original(graph, scope, marks, query, now, **kwargs)
            audit = kwargs.get("audit") or {}
            selection = audit.get("selection", {})
            calls.append({"event_id": kwargs.get("event_id"), "scope": scope,
                          "marks": deepcopy(marks), "query": query,
                          "selected_ids": [row["id"] for row in records],
                          "direct_hits": deepcopy(audit.get("match", {}).get("direct_hits", [])),
                          "ranked_ids": [row["record_id"] for row in
                                         selection.get("ranked_candidates", [])]})
            return records

        before = sorted(tuple(row) for row in fixture.store.db.execute("SELECT * FROM memory_static"))
        with patch.object(MemoryGraph, "retrieve", autospec=True, side_effect=retrieve):
            await fixture.run_turn()
        after = sorted(tuple(row) for row in fixture.store.db.execute("SELECT * FROM memory_static"))
        self.assertEqual(after, before)
        report = verify_runs([fixture.entries()[0]])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["complete"], 1)
        return calls

    def assert_initial_source_preconditions(self, fixture):
        initial = fixture.event("requery_retrieval")[0]["record"]
        selected = {item["record_id"] for item in initial["materials"]}
        self.assertTrue(fixture.chain_ids <= selected, "Both native chain fragments must be frozen initially")
        self.assertIn(fixture.decoy_id, selected, "The endpoint decoy must occupy the third initial slot")
        self.assertNotIn(fixture.beta_id, selected, "The beta-only source must be genuinely new")
        return initial

    @staticmethod
    def baseline_query(fixture):
        query = legacy.parse_action(json.dumps(goal_action()), fixture.message.text)["query"]
        return fixture.message.text + "\nCanonical query data:\n" + json.dumps(
            query, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def generation_stages(fixture):
        return [stage for path, stage, _ in fixture.requests if path == "/responses"]

    async def test_sourced_interior_changes_native_query_and_next_planner_receives_bound_feedback(self):
        fixture = self.make_fixture(consume_feedback=True, path_config=PathRequeryLoopConfig(
            enabled=True, max_seconds=3, composition_rules=({
                "rule_id": "synthetic-links-composition", "relation1": "links",
                "relation2": "links", "result": "links",
            },)))
        calls = await self.run_captured(fixture)
        initial = self.assert_initial_source_preconditions(fixture)
        query = next(row for row in calls if row["event_id"].endswith(":query:1"))
        pre = fixture.event("path_requery_feedback")[0]
        post = fixture.event("path_requery_feedback_result")[0]
        receipt = pre["receipt"]
        formal = next(candidate["formal_derivation"] for candidate in receipt["candidates"]
                      if candidate["formal_derivation"] is not None)
        self.assertEqual(formal["rule_ids"], ["synthetic-links-composition"])
        self.assertEqual(formal["status"], "formal_under_declared_rule")
        self.assertFalse(formal["proof"])
        self.assertFalse(formal["truth_verified"])
        self.assertFalse(formal["semantic_support_verified"])
        self.assertIn("beta", receipt["added_terms"])
        self.assertEqual(query["marks"], receipt["effective_terms"])
        self.assertEqual(query["query"], self.baseline_query(fixture) +
            "\nSourced graph terms:\n" + json.dumps(receipt["effective_terms"],
                                                    ensure_ascii=False, sort_keys=True))
        self.assertIn("beta", query["direct_hits"])
        self.assertIn(fixture.beta_id, query["selected_ids"])
        self.assertNotEqual(query["selected_ids"], [item["record_id"] for item in initial["materials"]])
        self.assertIn(fixture.beta_id, query["ranked_ids"])
        self.assertEqual(pre["receipt_sha256"], digest(receipt))
        self.assertEqual(post["receipt_sha256"], pre["receipt_sha256"])
        self.assertEqual(post["actual_query_sha256"], digest(query["query"]))
        self.assertEqual(post["result_receipt_sha256"], digest(post["result_receipt"]))
        self.assertTrue(post["added_evidence_uids"])
        self.assertIsNone(fixture.planning_inputs[0]["path_feedback"])
        self.assertEqual(fixture.planning_inputs[0]["path_feedback_sha256"], digest(None))
        next_feedback = fixture.planning_inputs[1]["path_feedback"]
        self.assertEqual(next_feedback, {"input": receipt, "result": post["result_receipt"]})
        self.assertEqual(fixture.planning_inputs[1]["path_feedback_sha256"], digest(next_feedback))
        self.assertTrue(fixture.feedback_was_consumed)
        self.assertEqual(self.generation_stages(fixture), ["selection", "query", "query", "reply"])
        self.assertEqual(len(fixture.requests), 8)

        # Both source-backed fragments survive into the delivered frozen bundle;
        # the UID assertion concerns native records, not physical PDF chunks.
        bundle = fixture.event("requery_evidence")[0]["bundle"]
        uid_by_id = {item["material"]["record_id"]: item["evidence_uid"] for item in bundle["materials"]}
        beta_clue = next(row for row in receipt["clues"] if row["term"] == "beta")
        provenance = beta_clue["provenance"]
        self.assertEqual({row["evidence_uid"] for row in provenance},
                         {uid_by_id[record_id] for record_id in fixture.chain_ids})
        self.assertEqual({row.get("source_record_id", row.get("record_id")) for row in provenance},
                         fixture.chain_ids)
        self.assertTrue(all(candidate["round_index"] == 0 for candidate in receipt["candidates"]))
        self.assertEqual(bundle["rounds"][0]["record_sha256"], digest(initial))
        self.assertTrue(all(row["record_sha256"] == digest(initial) for row in provenance))
        reply = next(payload for path, stage, payload in fixture.requests
                     if path == "/responses" and stage == "reply")
        context = json.loads(reply["input"][0]["content"].split("\n", 1)[1])
        self.assertEqual(context["memory_citations"], bundle_model_materials(bundle))
        chain_uids = {uid_by_id[record_id] for record_id in fixture.chain_ids}
        chain_materials = [item for item in context["memory_citations"]
                           if item["evidence_uid"] in chain_uids]
        self.assertEqual(len(chain_materials), 2)
        self.assertEqual({item["quote"] for item in chain_materials},
                         {typed_quote("alpha", "beta"), typed_quote("beta", "gamma")})
        self.assertEqual({item["citation_id"] for item in chain_materials},
                         {item["citation_id"] for item in bundle["materials"]
                          if item["evidence_uid"] in chain_uids})
        self.assertTrue(all(item["citation_marker"] == f'[{item["citation_id"]}]'
                            for item in chain_materials))
        self.assertEqual(len(fixture.event("answer_delivered")), 1)
        self.assertEqual(fixture.event("answer_delivered")[0]["bundle_sha256"], digest(bundle))
        self.assertTrue(fixture.saved_ledger()["usage_complete"])
        self.assertEqual(fixture.saved_ledger()["automatic_retries"], 0)

    async def test_disabled_loop_preserves_exact_baseline_query_and_planner_payload(self):
        fixture = self.make_fixture(enabled=False)
        calls = await self.run_captured(fixture)
        self.assert_initial_source_preconditions(fixture)
        query = next(row for row in calls if row["event_id"].endswith(":query:1"))
        self.assertEqual(query["marks"], ["alpha", "gamma"])
        self.assertEqual(query["query"], self.baseline_query(fixture))
        self.assertNotIn("Sourced graph terms", query["query"])
        self.assertNotIn(fixture.beta_id, query["selected_ids"])
        self.assertEqual(fixture.event("path_requery_feedback"), [])
        self.assertEqual(fixture.event("path_requery_feedback_result"), [])
        self.assertEqual(set(fixture.planning_inputs[0]), {
            "original_text", "round_index", "evidence", "remaining_query_rounds", "history_context"})
        self.assertTrue(all("path_feedback" not in payload and "path_feedback_sha256" not in payload
                            for payload in fixture.planning_inputs))
        self.assertTrue(all("effective_terms" not in row and "actual_query_sha256" not in row
                            for row in fixture.event("requery_retrieval")))
        # The unchanged query adds no evidence, so dev10 consolidation sends
        # its final reply without another planning round.
        self.assertEqual(self.generation_stages(fixture), ["selection", "query", "reply"])
        self.assertEqual(len(fixture.requests), 6)

    async def test_enabled_loop_requires_actual_offline_mock_transport_before_requests(self):
        for missing in ("mock_marker", "request_override"):
            with self.subTest(missing=missing):
                fixture = self.make_fixture(enabled=False)
                fixture.config.runtime = replace(fixture.config.runtime, path_requery_loop_enabled=True)
                if missing == "mock_marker":
                    fixture.adapter.offline_mock = False
                else:
                    fixture.adapter._request_override = None
                with self.assertRaisesRegex(GovernedError, "active_requery_real_calls_not_authorized"):
                    create_runtime(fixture.config, fixture.store, fixture.adapter, fixture.scratch)
                self.assertEqual(fixture.requests, [])

    async def test_path_budget_stops_before_query_retrieval_or_final_generation(self):
        fixture = self.make_fixture(path_config=PathRequeryLoopConfig(
            enabled=True, max_seconds=3, max_nodes=1))
        calls = await self.run_captured(fixture)
        self.assert_initial_source_preconditions(fixture)
        self.assertEqual(len(calls), 0)
        self.assertEqual(self.generation_stages(fixture), ["selection", "query"])
        self.assertEqual(len(fixture.requests), 4)
        self.assertEqual(len(fixture.event("requery_retrieval")), 1)
        self.assertEqual(fixture.event("path_requery_feedback")[0]["receipt"]["status"], "budget_stop")
        self.assertEqual(fixture.event("path_requery_feedback_result"), [])
        self.assertEqual(fixture.event("requery_stop")[0]["reason"], "path_requery_budget")
        self.assertTrue(fixture.saved_ledger()["usage_complete"])
        self.assertEqual(fixture.saved_ledger()["unknown_generation_count"], 0)
        self.assertEqual(fixture.saved_ledger()["automatic_retries"], 0)

    async def test_shared_call_and_token_limits_block_transport_without_path_budget_bypass(self):
        for limits, reason in (({"active_requery_max_calls": 1}, "requery_call_budget"),
                               ({"active_requery_input_tokens": 128}, "requery_token_budget")):
            with self.subTest(reason=reason):
                fixture = self.make_fixture()
                fixture.config.runtime = replace(fixture.config.runtime, **limits)
                calls = await self.run_captured(fixture)
                self.assertEqual(len(calls), 0)
                self.assertEqual(self.generation_stages(fixture), ["selection"])
                self.assertEqual(len(fixture.requests), 2)
                self.assertEqual(fixture.event("path_requery_feedback"), [])
                self.assertEqual(fixture.event("requery_stop")[0]["reason"], reason)
                self.assertEqual(fixture.saved_ledger()["unknown_generation_count"], 0)

    async def test_unknown_generation_usage_cannot_use_path_feedback_to_retry_or_finalize(self):
        fixture = self.make_fixture()
        fixture.query_error = GovernedError("synthetic_network_loss")
        calls = await self.run_captured(fixture)
        self.assertEqual(len(calls), 0)
        self.assertEqual(len(fixture.requests), 4)
        self.assertEqual(self.generation_stages(fixture), ["selection", "query"])
        self.assertEqual(fixture.event("path_requery_feedback_result"), [])
        ledger = fixture.saved_ledger()
        self.assertEqual(ledger["phase"], "halted")
        self.assertEqual(ledger["halted"], "requery_usage_unknown")
        self.assertFalse(ledger["usage_complete"])
        self.assertEqual(ledger["unknown_generation_count"], 1)
        self.assertEqual([call["stage"] for call in ledger["calls"]], ["selection", "query"])
        self.assertIsNone(ledger["calls"][1]["usage"])
        self.assertEqual(ledger["calls"][1]["reserved_output_tokens"], 512)
        self.assertEqual(ledger["automatic_retries"], 0)
        before = len(fixture.requests)
        with self.assertRaisesRegex(GovernedError, "already_recorded"):
            legacy.TurnBudgetAdapter(fixture.adapter, fixture.config, fixture.scratch,
                                     "synthetic-new-trace", fixture.message.message_id, fixture.message.scope)
        self.assertEqual(len(fixture.requests), before)
