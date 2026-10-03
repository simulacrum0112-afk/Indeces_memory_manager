"""Per-query context matching preserves literal Unicode and graph receipts."""
from collections import Counter
from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch

from indeces.memory import MemoryGraph, _hit
from tests import test_memory_selection_performance as oracle


class MemoryQueryMatchTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.graph = MemoryGraph(self.db)
        self.addCleanup(self.db.close)

    @staticmethod
    def records(count=30):
        return [{"id": index + 1, "scope": "synthetic", "source_id": f"source-{index}",
                 "author_id": "author", "text": "synthetic source " * 12,
                 "quote": "synthetic quotation", "marks": ["alpha", "beta", "extra"],
                 "created_at": 1.0, "active": True, "archived_at": None}
                for index in range(count)]

    @staticmethod
    def edges(context):
        def edge(score):
            return {"static_score": score, "dynamic_score": score * .99,
                    "effective_score": score, "context": list(context),
                    "source_record_ids": [1], "co_count": 1,
                    "dynamic_last_event_id": "synthetic-event"}
        return {("alpha", "beta"): edge(.4),
                ("alpha", "extra"): edge(.8),
                ("beta", "extra"): edge(.7)}

    def compare_selection(self, records, hits, edges, query):
        original = oracle._legacy_selection(self.graph, deepcopy(records), hits, edges, query, "event")
        current = self.graph._selection(deepcopy(records), hits, edges, query, "event")
        self.assertEqual(current, original)
        return current

    def test_each_context_label_matched_once_across_expansion_and_direct_evidence(self):
        records = self.records()
        edges = self.edges(["absent", "absent", "gate"])
        calls = Counter()

        def counted(mark, query):
            calls[mark] += 1
            return _hit(mark, query)

        original = oracle._legacy_selection(self.graph, deepcopy(records), ["alpha", "beta"],
                                             edges, "alpha beta gate", "event")
        with patch("indeces.memory._hit", side_effect=counted):
            current = self.graph._selection(deepcopy(records), ["alpha", "beta"],
                                             edges, "alpha beta gate", "event")
        self.assertEqual(current, original)
        self.assertEqual(calls, {"absent": 1, "gate": 1})
        self.assertEqual(len(current[1]["edge_statistics"]), len(edges))
        self.assertEqual(len(current[1]["ranked_candidates"]), len(records))

    def test_cache_is_discarded_for_each_query_and_scope(self):
        edges = self.edges(["gate"])
        records = self.records(2)
        for query, scope, expected in (("alpha beta gate", "scope-a", True),
                                       ("alpha beta", "scope-a", False),
                                       ("alpha beta", "scope-b", False),
                                       ("alpha beta gate", "scope-b", True)):
            with self.subTest(query=query, scope=scope):
                scoped = [{**record, "scope": scope} for record in records]
                with patch("indeces.memory._hit", wraps=_hit) as match:
                    result, selection = self.graph._selection(scoped, ["alpha", "beta"], edges, query, "event")
                self.assertEqual(match.call_count, 1)
                self.assertTrue(selection["expansion_candidates"])
                self.assertTrue(all(item["context_passed"] == expected
                                    for item in selection["expansion_candidates"]))
                self.assertEqual(bool(result[0]["evidence"]), expected)

    def test_context_gate_does_not_assume_requested_hits_match_query(self):
        result, selection = self.compare_selection(self.records(2), ["alpha", "beta"],
                                                   self.edges(["alpha", "beta"]), "unrelated words")
        self.assertTrue(result)
        self.assertTrue(all(not item["context_passed"] for item in selection["expansion_candidates"]))
        self.assertTrue(all(not record["evidence"] for record in result))

    def test_unicode_ignorecase_and_ascii_token_boundaries_stay_exact(self):
        cases = [("i", "İ", True), ("i", "ı", True), ("s", "ſ", True),
                 ("k", "K", True), ("k", "_K", False), ("i", "İ_", False),
                 ("RuO2", "RuO2!", True), ("RuO2", "RuO20", False),
                 ("H_2", "H_2", True), ("H_2", "xH_2", False),
                 ("中文", "含中文材料", True), ("中文", "other words", False)]
        for mark, query, expected in cases:
            with self.subTest(mark=mark, query=query):
                self.assertEqual(_hit(mark, query), expected)
                result, selection = self.compare_selection(self.records(2), ["alpha", "beta"],
                                                           self.edges([mark]), query)
                self.assertTrue(all(candidate["context_passed"] == expected
                                    for candidate in selection["expansion_candidates"]))
                self.assertEqual(bool(result[0]["evidence"]), expected)

    def test_full_retrieval_audit_and_database_match_legacy_through_source_changes(self):
        for mode in ("static", "dynamic"):
            with self.subTest(mode=mode):
                original_db, current_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
                self.addCleanup(original_db.close)
                self.addCleanup(current_db.close)
                original, current = oracle._LegacyMemoryGraph(original_db), MemoryGraph(current_db)
                for graph in (original, current):
                    for source, marks in (("retired", ["alpha", "beta", "gate"]),
                                          ("retained", ["alpha", "beta", "gate"]),
                                          ("extra", ["alpha", "extra", "gate"]),
                                          ("unrelated", ["unrelated"])):
                        graph.add("synthetic", source, "author", [
                            {"text": source, "quote": source, "marks": marks}], 1.0)

                def compare(event, query, now):
                    original_audit, current_audit = {}, {}
                    original_records = original.retrieve("synthetic", [], query, now, event_id=event,
                                                         audit=original_audit, ranking_mode=mode)
                    current_records = current.retrieve("synthetic", [], query, now, event_id=event,
                                                       audit=current_audit, ranking_mode=mode)
                    self.assertEqual(current_records, original_records)
                    oracle.assert_retrieval_audits_compatible(self, original_audit, current_audit,
                                                            original_db, current_db)
                    oracle.assert_database_compatible(self, original_db, current_db)
                    return current_audit

                compare("first", "alpha beta gate", 2.0)
                compare("context-missing", "alpha beta", 3.0)
                replay = compare("first", "alpha beta gate", 4.0)
                self.assertFalse(replay["observation"]["applied"])
                with patch("indeces.memory.time.time", return_value=5.0):
                    for graph in (original, current):
                        graph.deactivate_source("synthetic", "retired")
                retired = compare("retired", "alpha beta gate", 6.0)
                self.assertTrue(all(candidate["source_id"] != "retired"
                                    for candidate in retired["selection"]["ranked_candidates"]))


if __name__ == "__main__":
    unittest.main()
