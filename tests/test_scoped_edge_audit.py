"""Scoped statistics retain complete one-hop evidence and legacy validation."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import sqlite3
import unittest

from indeces.memory import MemoryGraph
from indeces.run_records import digest, freeze_retrieval, validate_graph_audit, validate_retrieval
from indeces.scratch import canonical
from tests.test_memory_selection_performance import _LegacyMemoryGraph


class ScopedEdgeAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        # This frozen full-graph oracle is independent of the new scoped
        # edge materialization. It provides the original evidence bytes.
        self.graph = _LegacyMemoryGraph(self.db)
        facts = [{"text": "direct pair", "quote": "direct pair", "marks": ["alpha", "beta"]}]
        facts.extend({"text": f"neighbor {index}", "quote": f"neighbor {index}",
                      "marks": ["alpha", f"near-{index}"]} for index in range(8))
        facts.extend([
            {"text": "gated neighbor", "quote": "gated neighbor",
             "marks": ["beta", "gated", "missing-context"]},
            {"text": "unrelated pair", "quote": "unrelated pair", "marks": ["remote", "unused"]},
            {"text": "orphan one", "quote": "orphan one", "marks": ["orphan-one"]},
            {"text": "orphan two", "quote": "orphan two", "marks": ["orphan-two"]},
        ])
        self.graph.add("scope", "synthetic-source", "synthetic-author", facts, 1.0)
        self.counter = 0

    def full_audit(self, *, query="alpha beta", mode="static", event=None):
        self.counter += 1
        event = event or f"synthetic-event-{self.counter}"
        audit = {}
        records = self.graph.retrieve("scope", [], query, float(self.counter + 1),
                                      event_id=event, audit=audit, ranking_mode=mode)
        validate_graph_audit(audit)
        return records, audit

    @staticmethod
    def rebind(audit):
        if audit["replay"] and "original_event" in audit:
            audit["durable_payload_sha256"] = audit["original_event"]["payload_sha256"]
        else:
            audit["durable_payload_sha256"] = digest({
                key: value for key, value in audit.items() if key != "durable_payload_sha256"})
        return audit

    def scoped_audit(self, original):
        audit = deepcopy(original)
        hits = set(audit["match"]["direct_hits"])
        selection = audit["selection"]
        selection["edge_statistics_scope"] = "direct_hit_incident_v1"
        selection["edge_statistics"] = [edge for edge in selection["edge_statistics"]
                                        if edge["a"] in hits or edge["b"] in hits]
        return self.rebind(audit)

    def test_scope_preserves_relevant_edge_and_all_other_evidence_bytes(self):
        records, full = self.full_audit()
        scoped = self.scoped_audit(full)
        validate_graph_audit(scoped)
        self.assertLess(len(scoped["selection"]["edge_statistics"]), full["selection"]["live_edge_count"])
        hits = set(full["match"]["direct_hits"])
        expected = [edge for edge in full["selection"]["edge_statistics"]
                    if edge["a"] in hits or edge["b"] in hits]
        self.assertEqual(canonical(scoped["selection"]["edge_statistics"]), canonical(expected))
        for key, value in full["selection"].items():
            if key != "edge_statistics":
                self.assertEqual(canonical(scoped["selection"][key]), canonical(value), key)
        self.assertEqual(canonical(scoped["observation"]), canonical(full["observation"]))
        full_receipt = freeze_retrieval(self.db, "scope", full["event_id"], "alpha beta", records, full)
        scoped_receipt = freeze_retrieval(self.db, "scope", full["event_id"], "alpha beta", records, scoped)
        validate_retrieval(scoped_receipt)
        validate_graph_audit(scoped, scoped_receipt)
        for key in ("model_materials", "materials", "sources"):
            self.assertEqual(canonical(scoped_receipt[key]), canonical(full_receipt[key]), key)

    def test_legacy_full_statistics_still_validate_without_scope_marker(self):
        _, full = self.full_audit()
        self.assertNotIn("edge_statistics_scope", full["selection"])
        validate_graph_audit(full)

    def test_unknown_or_deleted_scope_marker_does_not_allow_omissions(self):
        _, full = self.full_audit()
        for marker in (None, "full", "direct_hit_incident_v2", False):
            with self.subTest(marker=marker):
                scoped = self.scoped_audit(full)
                scoped["selection"]["edge_statistics_scope"] = marker
                with self.assertRaisesRegex(ValueError, "unknown edge statistics scope"):
                    validate_graph_audit(self.rebind(scoped))
        scoped = self.scoped_audit(full)
        del scoped["selection"]["edge_statistics_scope"]
        with self.assertRaisesRegex(ValueError, "committed/live weight mismatch"):
            validate_graph_audit(self.rebind(scoped))

    def test_scoped_marker_rejects_dynamic_ranking(self):
        _, full = self.full_audit(mode="dynamic")
        with self.assertRaisesRegex(ValueError, "require static ranking"):
            validate_graph_audit(self.scoped_audit(full))

    def test_global_live_count_cannot_be_smaller_than_scoped_statistics(self):
        _, full = self.full_audit()
        for count in (-1, True, 1.5, len(self.scoped_audit(full)["selection"]["edge_statistics"]) - 1):
            with self.subTest(count=count):
                scoped = self.scoped_audit(full)
                scoped["selection"]["live_edge_count"] = count
                with self.assertRaisesRegex(ValueError, "scoped live edge count mismatch"):
                    validate_graph_audit(self.rebind(scoped))

    def test_unrelated_or_out_of_order_statistics_are_rejected(self):
        _, full = self.full_audit()
        scoped = self.scoped_audit(full)
        scoped["selection"]["edge_statistics"].append(next(
            deepcopy(edge) for edge in full["selection"]["edge_statistics"]
            if (edge["a"], edge["b"]) == ("remote", "unused")))
        scoped["selection"]["edge_statistics"].sort(key=lambda edge: (edge["a"], edge["b"]))
        with self.assertRaisesRegex(ValueError, "outside direct hit scope"):
            validate_graph_audit(self.rebind(scoped))
        scoped = self.scoped_audit(full)
        scoped["selection"]["edge_statistics"].reverse()
        with self.assertRaisesRegex(ValueError, "statistics order mismatch"):
            validate_graph_audit(self.rebind(scoped))

    def test_applied_transition_requires_even_unselected_local_edge(self):
        _, full = self.full_audit()
        scoped = self.scoped_audit(full)
        rejected = next(item for item in scoped["selection"]["expansion_candidates"]
                        if item["neighbor_rank"] > 5)
        pair = tuple(sorted((rejected["from_mark"], rejected["mark"])))
        scoped["selection"]["edge_statistics"] = [edge for edge in scoped["selection"]["edge_statistics"]
                                                   if (edge["a"], edge["b"]) != pair]
        with self.assertRaisesRegex(ValueError, "committed/live weight mismatch"):
            validate_graph_audit(self.rebind(scoped))

    def test_rejected_neighbor_decisions_remain_bound_to_complete_evidence(self):
        _, full = self.full_audit()
        scoped = self.scoped_audit(full)
        decisions = scoped["selection"]["expansion_candidates"]
        self.assertTrue(any(item["neighbor_rank"] > 5 for item in decisions))
        self.assertTrue(any(not item["context_passed"] for item in decisions))
        self.assertEqual(canonical(decisions), canonical(full["selection"]["expansion_candidates"]))
        for rejection in ("neighbor_limit", "context"):
            with self.subTest(rejection=rejection):
                changed = deepcopy(scoped)
                item = next(candidate for candidate in changed["selection"]["expansion_candidates"]
                            if (candidate["neighbor_rank"] > 5 if rejection == "neighbor_limit"
                                else not candidate["context_passed"]))
                item["source_record_ids"] = []
                with self.assertRaisesRegex(ValueError, "expansion evidence disagrees"):
                    validate_graph_audit(self.rebind(changed))

    def test_replay_still_requires_expansion_and_ranking_statistics(self):
        self.full_audit(event="synthetic-replay")
        _, full = self.full_audit(event="synthetic-replay")
        self.assertTrue(full["replay"])
        scoped = self.scoped_audit(full)
        validate_graph_audit(scoped)
        missing_expansion = deepcopy(scoped)
        missing_expansion["selection"]["edge_statistics"] = []
        with self.assertRaisesRegex(ValueError, "evidence has no live edge"):
            validate_graph_audit(self.rebind(missing_expansion))
        query = "beta gated missing-context"
        self.full_audit(query=query, event="synthetic-direct-replay")
        _, direct = self.full_audit(query=query, event="synthetic-direct-replay")
        scoped = self.scoped_audit(direct)
        scoped["selection"]["edge_statistics"] = [edge for edge in scoped["selection"]["edge_statistics"]
                                                   if (edge["a"], edge["b"]) != ("beta", "gated")]
        with self.assertRaisesRegex(ValueError, "ranking evidence has no live edge"):
            validate_graph_audit(self.rebind(scoped))

    def test_unsupported_direct_pair_and_no_hit_do_not_gain_live_edges(self):
        for query in ("orphan-one orphan-two", "unknown synthetic words"):
            with self.subTest(query=query):
                _, full = self.full_audit(query=query)
                scoped = self.scoped_audit(full)
                self.assertEqual(scoped["selection"]["edge_statistics"], [])
                validate_graph_audit(scoped)

    def test_replay_of_persisted_legacy_full_event_keeps_original_bytes_after_retirement(self):
        self.graph.add("scope", "retiring-synthetic-source", "synthetic-author", [
            {"text": "retiring neighbor", "quote": "retiring neighbor", "marks": ["alpha", "retiring-near"]}], 1.0)
        event = "persisted-legacy-full-event"
        _, original = self.full_audit(event=event)
        self.assertNotIn("edge_statistics_scope", original["selection"])
        stored = self.db.execute("SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                                 ("scope", event)).fetchone()[0]
        original_digest = hashlib.sha256(stored.encode()).hexdigest()
        self.assertEqual(original["durable_payload_sha256"], original_digest)
        # Opening an existing legacy database with the new implementation
        # must preserve its immutable full receipt and scope only replay's
        # fresh current-source selection.
        current = MemoryGraph(self.db)
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    self.assertEqual(current.deactivate_source("scope", "retiring-synthetic-source"), 1)
                expected_records, full_replay = self.full_audit(event=event)
                actual = {}
                records = current.retrieve("scope", [], "alpha beta", float(self.counter + 1),
                                           event_id=event, audit=actual, ranking_mode="static")
                self.assertTrue(actual["replay"])
                self.assertEqual(actual["selection"]["edge_statistics_scope"], "direct_hit_incident_v1")
                self.assertEqual(actual["durable_payload_sha256"], original_digest)
                self.assertEqual(actual["original_event"]["payload_sha256"], original_digest)
                self.assertEqual(self.db.execute(
                    "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                    ("scope", event)).fetchone()[0].encode(), stored.encode())
                self.assertEqual(canonical(records), canonical(expected_records))
                # The replay's fresh receipt explicitly adopts neighborhood
                # v2 while retaining the immutable legacy original event.
                # Compare all local evidence with the same frozen dynamic
                # state, and separately declare the new observation contract.
                projected = self.scoped_audit(full_replay)
                projected.update(schema_version=2, audit_scope="direct_hit_neighborhood_v1")
                selection = projected["selection"]
                endpoints = {edge[key] for edge in selection["edge_statistics"] for key in ("a", "b")}
                selection["mark_frequencies"] = {mark: count for mark, count in selection["mark_frequencies"].items()
                                                  if mark in endpoints}
                selection["mark_frequencies_scope"] = "edge_endpoints_v1"
                old_observation = projected["observation"]
                projected["observation"] = {
                    "status": "replay", "applied": False,
                    "original_changes_known": old_observation["original_changes_known"],
                    "seeded_before": old_observation["seeded_before"],
                    "seeded_after": old_observation["seeded_after"], "seeded_edges": 0,
                    "changed_edges": [], "dynamic_shadow_enabled": False,
                    "reason": "static_shadow_disabled"}
                self.assertEqual(canonical(actual), canonical(projected))
                validate_graph_audit(actual)
                if retired:
                    self.assertFalse(any(candidate["source_id"] == "retiring-synthetic-source"
                                         for candidate in actual["selection"]["ranked_candidates"]))
                    self.assertFalse(any(edge["b"] == "retiring-near"
                                         for edge in actual["selection"]["edge_statistics"]))


if __name__ == "__main__":
    unittest.main()
