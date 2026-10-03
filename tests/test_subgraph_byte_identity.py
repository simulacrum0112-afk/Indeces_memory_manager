"""Static neighborhood bytes equal the frozen full-selection oracle.

The new static path intentionally leaves shadow state unchanged.  These
comparisons use identical frozen source/dynamic state and never apply old
global shadow observation; they do not claim complete audit-envelope identity.
"""
from __future__ import annotations

from copy import deepcopy
import sqlite3
import unittest

from indeces.memory import MAX_DIRECT, MemoryGraph, _hit
from indeces.run_records import freeze_retrieval, validate_graph_audit
from indeces.scratch import canonical
from tests.test_memory_selection_performance import _legacy_edges, _legacy_selection
from verification.benchmark_subgraph_retrieval import strict_projection


class SubgraphByteIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.graph = MemoryGraph(self.db)
        facts = [{"text": "direct pair", "quote": "direct pair", "marks": ["alpha", "beta"]}]
        facts += [{"text": f"neighbor {index}", "quote": f"neighbor {index}",
                   "marks": ["alpha", f"near-{index}"]} for index in range(12)]
        facts += [{"text": "context gate", "quote": "context gate", "marks": ["beta", "gated", "context"]},
                  {"text": "unicode", "quote": "unicode", "marks": ["alpha", "中文", "RuO2"]},
                  {"text": "unrelated", "quote": "unrelated", "marks": ["remote", "unused"]},
                  {"text": "orphan one", "quote": "orphan one", "marks": ["orphan-one"]},
                  {"text": "orphan two", "quote": "orphan two", "marks": ["orphan-two"]}]
        self.graph.add("scope", "synthetic-source", "synthetic-author", facts, 1.0)
        self.counter = 0

    def compare(self, query):
        self.counter += 1
        event = f"synthetic-frozen-{self.counter}"
        rows = self.db.execute(
            "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at "
            "FROM memory_records WHERE scope=? AND active=1 ORDER BY id", ("scope",))
        source_records = [self.graph._record(row) for row in rows]
        known = {mark for record in source_records for mark in record["marks"]} - self.graph.self_marks
        hits = sorted((mark for mark in known if _hit(mark, query)), key=lambda mark: (-len(mark), mark))[:MAX_DIRECT]
        old_records, old_selection = _legacy_selection(self.graph, deepcopy(source_records), hits,
                                                       _legacy_edges(self.graph, "scope", "static"), query, event)
        old_selection.update(ranking_mode="static", weight_basis="static_npmi")
        for record in old_records:
            record.update(ranking_mode="static", weight_basis="static_npmi")
        before = list(self.db.execute("SELECT * FROM memory_dynamic ORDER BY scope,a,b"))
        audit = {}
        records = self.graph.retrieve("scope", [], query, 3.0, event_id=event, audit=audit, ranking_mode="static")
        self.assertEqual(canonical(records), canonical(old_records))
        self.assertEqual(canonical(strict_projection(audit["selection"], hits)),
                         canonical(strict_projection(old_selection, hits)))
        self.assertEqual(list(self.db.execute("SELECT * FROM memory_dynamic ORDER BY scope,a,b")), before)
        self.assertEqual(audit["observation"]["changed_edges"], [])
        self.assertEqual(audit["audit_scope"], "direct_hit_neighborhood_v1")
        frozen = freeze_retrieval(self.db, "scope", event, query, records, audit)
        validate_graph_audit(audit, frozen)
        return records, audit

    def test_direct_dense_context_no_hit_and_unicode_exact_bytes(self):
        for query in ("alpha beta", "alpha", "alpha beta gated context", "alpha 中文 RuO2", "orphan-one orphan-two", "unknown"):
            with self.subTest(query=query):
                self.compare(query)

    def test_retained_nonzero_dynamic_history_is_exact_but_never_written(self):
        # Distinct shadow history verifies the new path reads stored values,
        # rather than replacing evidence fields with zero or activating decay.
        with self.db:
            for index, (a, b, weight, context) in enumerate(self.db.execute(
                    "SELECT a,b,weight,context_json FROM memory_static WHERE scope=? ORDER BY a,b", ("scope",))):
                self.db.execute("INSERT OR REPLACE INTO memory_dynamic VALUES(?,?,?,?,?,?,?)",
                                ("scope", a, b, weight + index / 10, context, weight, "synthetic-old-event"))
        records, audit = self.compare("alpha beta context")
        self.assertTrue(any(edge["dynamic_score"] > 0 for edge in audit["selection"]["edge_statistics"]))
        self.assertTrue(records)

    def test_source_publication_and_retirement_refresh_the_same_oracle(self):
        self.compare("alpha beta")
        self.graph.add("scope", "synthetic-new-source", "synthetic-author", [
            {"text": "new pair", "quote": "new pair", "marks": ["alpha", "new-neighbor"]}], 4.0)
        self.compare("alpha new-neighbor")
        self.graph.deactivate_source("scope", "synthetic-new-source")
        _, audit = self.compare("alpha new-neighbor")
        self.assertNotIn("new-neighbor", audit["match"]["direct_hits"])
        self.assertFalse(any("new-neighbor" in (edge["a"], edge["b"])
                             for edge in audit["selection"]["edge_statistics"]))


if __name__ == "__main__":
    unittest.main()
