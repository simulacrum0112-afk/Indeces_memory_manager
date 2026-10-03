"""Versioned neighborhood receipts preserve local evidence and legacy records."""
from __future__ import annotations

from copy import deepcopy
import sqlite3
import unittest

from indeces.run_records import (digest, freeze_retrieval, validate_graph_audit,
                                validate_retrieval)
from indeces.memory import MemoryGraph
from indeces.observer_data import _verify_retained
from indeces.scratch import CanonicalSnapshot, canonical
from tests.test_memory_selection_performance import _LegacyMemoryGraph


class NeighborhoodAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.graph = _LegacyMemoryGraph(self.db)
        self.graph.add("scope", "synthetic-source", "synthetic-author", [
            {"text": "alpha beta local", "quote": "alpha beta local", "marks": ["alpha", "beta"]},
            {"text": "alpha near local", "quote": "alpha near local", "marks": ["alpha", "near"]},
            {"text": "remote unrelated", "quote": "remote unrelated", "marks": ["remote", "unrelated"]},
            {"text": "isolated local", "quote": "isolated local", "marks": ["isolated"]},
        ], 1.0)

    def full(self, *, query="alpha beta", event="synthetic-event", now=2.0):
        audit = {}
        records = self.graph.retrieve("scope", [], query, now, event_id=event,
                                      audit=audit, ranking_mode="static")
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

    def neighborhood(self, original):
        audit = deepcopy(original)
        audit.update(schema_version=2, audit_scope="direct_hit_neighborhood_v1")
        hits = set(audit["match"]["direct_hits"])
        selection = audit["selection"]
        selection["edge_statistics_scope"] = "direct_hit_incident_v1"
        selection["edge_statistics"] = [edge for edge in selection["edge_statistics"]
                                         if edge["a"] in hits or edge["b"] in hits]
        endpoints = {edge[key] for edge in selection["edge_statistics"] for key in ("a", "b")}
        selection["mark_frequencies_scope"] = "edge_endpoints_v1"
        selection["mark_frequencies"] = {mark: count for mark, count in selection["mark_frequencies"].items()
                                          if mark in endpoints}
        observation = audit["observation"]
        observation.update(status="replay" if audit["replay"] else "disabled", applied=False,
                           dynamic_shadow_enabled=False, changed_edges=[], seeded_edges=0,
                           seeded_before=observation["seeded_after"])
        return self.rebind(audit)

    def test_local_evidence_and_frozen_selected_materials_match_full_oracle_bytes(self):
        records, full = self.full()
        local = self.neighborhood(full)
        validate_graph_audit(local)
        hits = set(full["match"]["direct_hits"])
        expected = [edge for edge in full["selection"]["edge_statistics"]
                    if edge["a"] in hits or edge["b"] in hits]
        self.assertEqual(canonical(local["selection"]["edge_statistics"]), canonical(expected))
        self.assertNotIn("remote", local["selection"]["mark_frequencies"])
        for key in ("expansion_candidates", "ranked_candidates", "selected", "selected_record_ids"):
            self.assertEqual(canonical(local["selection"][key]), canonical(full["selection"][key]), key)
        old = freeze_retrieval(self.db, "scope", full["event_id"], "alpha beta", records, full)
        receipt = freeze_retrieval(self.db, "scope", local["event_id"], "alpha beta", records,
                                   CanonicalSnapshot(local))
        validate_retrieval(receipt)
        validate_graph_audit(receipt["graph_audit"], receipt)
        for key in ("model_materials", "materials", "sources"):
            self.assertEqual(canonical(receipt[key]), canonical(old[key]), key)
        self.assertNotEqual(receipt["graph_audit_sha256"], old["graph_audit_sha256"])

    def test_unknown_or_missing_scope_fields_and_downgrade_are_rejected(self):
        _, full = self.full()
        for mutate in (
            lambda audit: audit.pop("audit_scope"),
            lambda audit: audit.update(audit_scope="full"),
            lambda audit: audit.update(schema_version=1),
            lambda audit: audit.update(schema_version=True),
            lambda audit: audit["selection"].pop("mark_frequencies_scope"),
            lambda audit: audit["selection"].update(mark_frequencies_scope="all"),
            lambda audit: audit["selection"].pop("edge_statistics_scope"),
            lambda audit: audit["observation"].pop("dynamic_shadow_enabled"),
        ):
            with self.subTest(mutate=mutate):
                local = self.neighborhood(full)
                mutate(local)
                with self.assertRaises(ValueError):
                    validate_graph_audit(self.rebind(local))

    def test_frequency_scope_rejects_missing_and_unrelated_endpoints(self):
        _, full = self.full()
        for mutate in (
            lambda selection: selection["mark_frequencies"].pop("alpha"),
            lambda selection: selection["mark_frequencies"].update(remote=1),
            lambda selection: selection["mark_frequencies"].update(alpha=True),
        ):
            local = self.neighborhood(full)
            mutate(local["selection"])
            with self.assertRaises(ValueError):
                validate_graph_audit(self.rebind(local))

    def test_v2_rejects_any_dynamic_write_or_dynamic_ranking(self):
        _, full = self.full()
        for mutate in (
            lambda audit: audit["observation"].update(applied=True),
            lambda audit: audit["observation"].update(dynamic_shadow_enabled=True),
            lambda audit: audit["observation"].update(changed_edges=[{}]),
            lambda audit: audit["observation"].update(seeded_edges=1),
            lambda audit: audit["observation"].update(seeded_before=False, seeded_after=True),
            lambda audit: audit["observation"].update(status="applied"),
            lambda audit: audit["selection"].update(ranking_mode="dynamic", weight_basis="dynamic_or_static"),
        ):
            local = self.neighborhood(full)
            mutate(local)
            with self.assertRaises(ValueError):
                validate_graph_audit(self.rebind(local))

    def test_replay_links_legacy_original_without_reinterpreting_its_transitions(self):
        _, full = self.full()
        stored = self.db.execute("SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                                 ("scope", full["event_id"])).fetchone()[0]
        _, replay = self.full(now=3.0)
        local = self.neighborhood(replay)
        self.assertTrue(local["replay"])
        self.assertEqual(local["original_event"], replay["original_event"])
        self.assertEqual(local["durable_payload_sha256"], full["durable_payload_sha256"])
        validate_graph_audit(local)
        self.assertEqual(self.db.execute("SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                                         ("scope", full["event_id"])).fetchone()[0], stored)
        # Old full graph records keep their original version and validation.
        self.assertEqual(full["schema_version"], 1)
        validate_graph_audit(full)

    def test_no_hit_and_isolated_hits_need_no_edge_frequency_rows(self):
        for query in ("unknown synthetic words", "isolated"):
            with self.subTest(query=query):
                records, full = self.full(query=query, event=query)
                local = self.neighborhood(full)
                self.assertEqual(local["selection"]["edge_statistics"], [])
                self.assertEqual(local["selection"]["mark_frequencies"], {})
                validate_graph_audit(local)
                receipt = freeze_retrieval(self.db, "scope", query, query, records, local)
                validate_graph_audit(local, receipt)

    def test_production_static_path_matches_frozen_legacy_selection_without_shadow_writes(self):
        # Establish a real legacy shadow state, then freeze it. The reference
        # performs the old complete selection without another observation.
        self.full(event="legacy-shadow-state")
        before = list(self.db.execute("SELECT * FROM memory_dynamic ORDER BY scope,a,b"))
        old_records = self.graph._records("scope")
        old_edges = self.graph._edges("scope", "static")
        expected_records, expected = self.graph._selection(
            old_records, ["alpha", "beta"], old_edges, "alpha beta", "new-local-event")
        for record in expected_records:
            record.update(ranking_mode="static", weight_basis="static_npmi")
        current = MemoryGraph(self.db)
        actual = {}
        records = current.retrieve("scope", [], "alpha beta", 3.0,
                                   event_id="new-local-event", audit=actual)
        self.assertEqual(actual["schema_version"], 2)
        self.assertEqual(actual["audit_scope"], "direct_hit_neighborhood_v1")
        self.assertEqual(actual["observation"]["status"], "disabled")
        self.assertEqual(canonical(records), canonical(expected_records))
        hits = set(actual["match"]["direct_hits"])
        expected_statistics = [edge for edge in expected["edge_statistics"]
                               if edge["a"] in hits or edge["b"] in hits]
        self.assertEqual(canonical(actual["selection"]["edge_statistics"]), canonical(expected_statistics))
        for key in ("expansion_candidates", "ranked_candidates", "selected", "selected_record_ids"):
            self.assertEqual(canonical(actual["selection"][key]), canonical(expected[key]), key)
        self.assertEqual(list(self.db.execute("SELECT * FROM memory_dynamic ORDER BY scope,a,b")), before)
        self.assertIsNone(self.db.execute("SELECT 1 FROM memory_events WHERE scope=? AND event_id=?",
                                          ("scope", "new-local-event")).fetchone())
        validate_graph_audit(actual)
        receipt = freeze_retrieval(self.db, "scope", "new-local-event", "alpha beta", records,
                                   CanonicalSnapshot(actual))
        validate_graph_audit(actual, receipt)

    def test_production_v2_replay_keeps_durable_original_and_current_local_source_gate(self):
        current = MemoryGraph(self.db)
        original = {}
        current.retrieve("scope", [], "alpha beta", 2.0,
                         event_id="new-v2-replay", audit=original)
        original_json = self.db.execute(
            "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
            ("scope", "new-v2-replay")).fetchone()[0]
        current.deactivate_source("scope", "synthetic-source")
        replay = {}
        records = current.retrieve("scope", [], "alpha beta", 3.0,
                                   event_id="new-v2-replay", audit=replay)
        self.assertEqual(records, [])
        self.assertEqual(replay["observation"]["status"], "replay")
        self.assertEqual(replay["selection"]["edge_statistics"], [])
        self.assertEqual(replay["selection"]["mark_frequencies"], {})
        self.assertEqual(replay["original_event"]["payload_sha256"], original["durable_payload_sha256"])
        self.assertEqual(self.db.execute(
            "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
            ("scope", "new-v2-replay")).fetchone()[0], original_json)
        validate_graph_audit(replay)

    def test_observer_local_validation_explicitly_reports_neighborhood_scope(self):
        current = MemoryGraph(self.db)
        audit = {}
        current.retrieve("scope", [], "alpha beta", 2.0,
                         event_id="synthetic-viewer", audit=audit)
        report = _verify_retained([{
            "sequence": 1, "event": "memory_observation",
            "fields": {"audit": audit, "audit_sha256": digest(audit)}
        }], False, [], "synthetic-viewer", False)
        self.assertEqual(report["status"], "retained_checks_passed")
        self.assertIn("graph_audit_hit_neighborhood_only", report["warnings"])


if __name__ == "__main__":
    unittest.main()
