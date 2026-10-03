import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.memory import MemoryGraph, _IndexedRecords, _hit
from indeces.memory_index import KeyedMemoryIndex, REQUIRED_QUERY_TABLES, SCHEMA_VERSION, lazy_decay


class KeyedMemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.graph = MemoryGraph(self.db)
        self.index = KeyedMemoryIndex(self.db, self.graph.self_marks)
        self.index.ensure_schema()

    def tearDown(self):
        self.db.close()

    def fact(self, source, marks, scope="scope", now=1):
        return self.graph.add(scope, source, "author", [
            {"text": source, "quote": source, "marks": marks}], now)[0]

    def publish(self, scope="scope"):
        with self.db:
            self.db.execute("BEGIN")
            self.index.rebuild_scope(scope, self.graph._records(scope))

    def test_directed_coordinates_share_symmetric_static_evidence(self):
        self.fact("pair", ["alpha", "beta"])
        self.publish()
        ids = self.index.mark_ids("scope", ["alpha", "beta"])
        rows = [self.db.execute(
            "SELECT static_score,co_count,context_json,evidence_json,last_updated,primary_edge "
            "FROM memory_query_edges WHERE scope=? AND i=? AND j=?",
            ("scope", ids[a], ids[b])).fetchone()
            for a, b in (("alpha", "beta"), ("beta", "alpha"))]
        self.assertEqual(rows[0][:-1], rows[1][:-1])
        self.assertEqual([row[-1] for row in rows], [1, 0])
        edges, ordinals = self.index.incident_edges("scope", ["beta"])
        self.assertEqual(list(edges), [("alpha", "beta")])
        self.assertEqual(ordinals, {("alpha", "beta"): 0})

    def test_stable_ids_retired_rows_and_unmodified_edge_timestamp(self):
        with patch("indeces.memory_index.time.time", return_value=1):
            self.fact("pair", ["alpha", "beta"], now=1)
        self.publish()
        original = self.index.mark_ids("scope", ["alpha", "beta"])
        self.fact("other", ["gamma"], now=2)
        self.publish()
        self.assertEqual(original, self.index.mark_ids("scope", original))
        # NPMI and evidence remain identical; adding an unrelated source must
        # not pretend that this existing edge was dynamically accessed.
        timestamp = self.db.execute(
            "SELECT last_updated FROM memory_query_edges WHERE scope='scope' AND primary_edge=1"
        ).fetchone()[0]
        self.assertEqual(timestamp, 1)
        self.graph.deactivate_source("scope", "pair")
        self.publish()
        self.assertEqual(self.index.mark_ids("scope", original), {})
        saved = dict(self.db.execute("SELECT label,id FROM memory_query_marks"))
        self.assertEqual({label: saved[label] for label in original}, original)
        self.assertEqual(self.db.execute(
            "SELECT active FROM memory_query_edges WHERE scope='scope' AND primary_edge=1"
        ).fetchone()[0], 0)
        self.fact("new-pair", ["alpha", "beta"], now=3)
        self.publish()
        self.assertEqual(original, self.index.mark_ids("scope", original))
        self.assertEqual(self.index.meta("scope")["schema_version"], SCHEMA_VERSION)

    def test_retiring_newest_source_cannot_move_publication_timestamp_backwards(self):
        self.fact("older", ["alpha", "beta"], now=5)
        self.fact("newest", ["alpha", "beta"], now=20)
        records = self.graph._records("scope")
        edge = {("alpha", "beta"): {"static_score": 1.0, "co_count": 2,
                 "context": "[]", "source_record_ids": "[1,2]"}}
        self.db.execute("DELETE FROM memory_query_edges")
        with self.db:
            self.index.rebuild_scope("scope", records, edge, updated_at=100)
        # The changed edge now refers solely to an older source, while its
        # publication occurs later than the original publication.
        edge[("alpha", "beta")].update(co_count=1, source_record_ids="[1]")
        with self.db:
            self.db.execute("BEGIN")
            self.index.rebuild_scope("scope", records[:1], edge, updated_at=120)
        self.assertEqual(self.db.execute(
            "SELECT last_updated FROM memory_query_edges WHERE primary_edge=1").fetchone()[0], 120)
        # Unchanged evidence keeps its timestamp even when a maintenance
        # publication occurs later. Clock reversal also cannot lower it.
        with self.db:
            self.db.execute("BEGIN")
            self.index.rebuild_scope("scope", records[:1], edge, updated_at=140)
        self.assertEqual(self.db.execute(
            "SELECT last_updated FROM memory_query_edges WHERE primary_edge=1").fetchone()[0], 120)
        edge[("alpha", "beta")].update(co_count=3)
        with self.db:
            self.db.execute("BEGIN")
            self.index.rebuild_scope("scope", records[:1], edge, updated_at=90)
        self.assertEqual(self.db.execute(
            "SELECT last_updated FROM memory_query_edges WHERE primary_edge=1").fetchone()[0], 120)

    def test_literal_matcher_has_exact_unicode_and_token_semantics(self):
        marks = ["i", "s", "k", "a-b", "a.b", "alpha", "中文", "straße", "ss",
                 "mañana", "a_b", "１２", "İnput", "Σ"]
        for ordinal, mark in enumerate(marks):
            self.fact(str(ordinal), [mark])
        self.publish()
        records = _IndexedRecords(self.graph._records("scope"), self.graph.self_marks)
        for query in ["İ ı ſ K", "robot alpha2 _alpha alpha!", "x中文x", "Straße STRASSE",
                      "ß ss", "mañana MAÑANA", "a-b a.b a_b", "Input İNPUT Σ ς σ", "x１２x", ""]:
            with self.subTest(query=query):
                expected = {mark for mark in records.known if _hit(mark, query)}
                self.assertEqual(self.index.literal_matches("scope", query), expected)
                self.assertEqual(self.index.literal_matches("scope", query), records.literal_matches(query))

    def test_queries_use_only_point_prefix_posting_and_adjacency_reads(self):
        target = self.fact("target", ["alpha", "beta"])
        self.fact("unrelated", ["gamma", "delta"], scope="other")
        for ordinal in range(40):
            self.fact("noise-" + str(ordinal), ["noise" + str(ordinal)])
        self.publish()
        self.publish("other")
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(self.index.literal_matches("scope", "alpha!"), {"alpha"})
            self.assertEqual(self.index.frequencies("scope", ["alpha", "beta"]), {"alpha": 1, "beta": 1})
            edges, _ = self.index.incident_edges("scope", ["alpha"])
            self.assertEqual(list(edges), [("alpha", "beta")])
            self.assertEqual(self.index.records_for_marks("scope", ["beta"]), [target])
            self.assertEqual(self.index.records_for_marks("other", ["beta"]), [])
        finally:
            self.db.set_trace_callback(None)
        for sql in statements:
            with self.subTest(sql=sql):
                self.assertTrue(sql.startswith("SELECT"))
                self.assertNotIn("FROM memory_static", sql)
                self.assertNotIn("FROM memory_support", sql)
                self.assertNotIn("COUNT(", sql)
                if "FROM memory_records" in sql:
                    self.assertIn("WHERE id=", sql)
                if "FROM memory_query_trie" in sql:
                    self.assertIn("AND prefix=", sql)
                if "FROM memory_query_postings" in sql:
                    self.assertIn("AND mark_id=", sql)
                if "FROM memory_query_adjacency" in sql:
                    self.assertIn("AND x.mark_id=", sql)

    def test_primary_reverse_rows_and_source_order_are_preserved(self):
        self.fact("pair", ["alpha", "beta"])
        records = self.graph._records("scope")
        edges = {
            ("beta", "alpha"): {"static_score": 0.4, "co_count": 7,
                "context": '["reverse"]', "source_record_ids": "[1]"},
            ("alpha", "beta"): {"static_score": 0.5, "co_count": 8,
                "context": '["forward"]', "source_record_ids": "[2]"},
            ("alpha", "alpha"): {"static_score": 0.6, "co_count": 9,
                "context": "[]", "source_record_ids": "[3]"}}
        with self.db:
            self.db.execute("BEGIN")
            self.index.rebuild_scope("scope", records, edges)
        retrieved, ordinals = self.index.incident_edges("scope", ["alpha"])
        self.assertEqual(list(retrieved), list(edges))
        self.assertEqual(ordinals, {pair: ordinal for ordinal, pair in enumerate(edges)})
        for pair, edge in edges.items():
            expected = dict(edge, context=json.loads(edge["context"]),
                            source_record_ids=json.loads(edge["source_record_ids"]))
            self.assertEqual(retrieved[pair], expected)

    def test_sqlite_query_plans_use_complete_point_or_adjacency_keys(self):
        queries = [
            ("SELECT m.id FROM memory_query_marks m JOIN memory_query_frequencies f "
             "ON f.mark_id=m.id AND f.scope=? WHERE m.label=?", ("scope", "alpha"),
             ("label=?", "scope=? AND mark_id=?")),
            ("SELECT e.a,e.b,e.static_score FROM memory_query_adjacency x JOIN memory_query_edges e "
             "ON e.scope=x.scope AND e.i=x.i AND e.j=x.j WHERE x.scope=? AND x.mark_id=? "
             "AND x.static_score>0 AND e.active=1 AND e.primary_edge=1", ("scope", 1),
             ("scope=? AND mark_id=?", "scope=? AND i=? AND j=?")),
            ("SELECT terminals_json FROM memory_query_trie WHERE scope=? AND kind=? AND prefix=?",
             ("scope", "latin", "alpha"), ("scope=? AND kind=? AND prefix=?",)),
            ("SELECT record_id FROM memory_query_postings WHERE scope=? AND mark_id=?",
             ("scope", 1), ("scope=? AND mark_id=?",))]
        for sql, arguments, keys in queries:
            plan = " ".join(row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + sql, arguments))
            with self.subTest(sql=sql, plan=plan):
                self.assertNotIn("SCAN", plan)
                for key in keys:
                    self.assertIn(key, plan)

    def test_publication_requires_owned_transaction_and_query_has_no_side_effects(self):
        with self.assertRaisesRegex(ValueError, "caller-owned transaction"):
            self.index.rebuild_scope("empty", [])
        before = self.db.total_changes
        self.assertIsNone(self.index.meta("missing"))
        self.assertEqual(self.index.literal_matches("missing", "alpha"), set())
        self.assertEqual(self.index.incident_edges("missing", ["alpha"]), ({}, {}))
        self.assertEqual(self.index.records_for_marks("missing", ["alpha"]), [])
        self.assertEqual(self.db.total_changes, before)

    def test_immutable_event_header_preserves_original_audit_bytes(self):
        self.fact("pair", ["alpha", "beta"])
        audit = {}
        self.graph.retrieve("scope", [], "alpha beta", 10, event_id="synthetic-event", audit=audit)
        serialized = self.db.execute(
            "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
            ("scope", "synthetic-event")).fetchone()[0]
        with self.db:
            header = self.index.publish_event_header("scope", "synthetic-event", serialized)
        self.assertEqual(header["payload_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(header["query"], "alpha beta")
        self.assertEqual(header["direct_hit_count"], 2)
        self.assertEqual(header["ranking_mode"], "static")
        self.assertEqual(self.db.execute(
            "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
            ("scope", "synthetic-event")).fetchone()[0], serialized)
        for sql in ("UPDATE memory_query_event_headers SET query='tampered'",
                    "DELETE FROM memory_query_event_headers"):
            with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                self.db.execute(sql)
        altered = json.loads(serialized)
        altered["request"]["query"] = "mismatch"
        with self.assertRaisesRegex(ValueError, "header mismatch"):
            self.index.publish_event_header("scope", "synthetic-event", json.dumps(altered))

    def test_missing_event_header_migrates_only_at_explicit_startup(self):
        payload = {"scope": "scope", "event_id": "legacy", "request": {"query": "alpha", "observed_at": 1},
                   "match": {"direct_hits": ["alpha"]}, "selection": {},
                   "observation": {"original_changes_known": False, "status": "legacy_replay", "changed_edges": []}}
        serialized = json.dumps(payload, indent=2)
        self.db.execute("INSERT INTO memory_event_audits VALUES(?,?,?)", ("scope", "legacy", serialized))
        self.assertIsNone(self.index.event_header("scope", "legacy"))
        self.assertEqual(self.index.migrate_missing_event_headers(), 1)
        self.assertEqual(self.index.migrate_missing_event_headers(), 0)
        header = self.index.event_header("scope", "legacy")
        self.assertEqual(header["ranking_mode"], "dynamic")
        self.assertFalse(header["original_changes_known"])
        self.assertEqual(self.db.execute(
            "SELECT payload_json FROM memory_event_audits WHERE event_id='legacy'").fetchone()[0], serialized)


class KeyedMigrationTests(unittest.TestCase):
    def test_missing_authoritative_ids_fail_closed_without_recreating_tables(self):
        with TemporaryDirectory() as directory:
            for filename in (":memory:", str(Path(directory) / "test-memory.sqlite3")):
                with self.subTest(filename=filename):
                    db = sqlite3.connect(filename)
                    try:
                        graph = MemoryGraph(db)
                        graph.add("scope", "synthetic", "author", [
                            {"text": "synthetic", "quote": "synthetic", "marks": ["alpha", "beta"]}], 1)
                        db.execute("DROP TABLE memory_query_marks")
                        db.commit()
                        before = list(db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
                        sources = list(db.execute("SELECT * FROM memory_records"))
                        edges = list(db.execute("SELECT * FROM memory_query_edges"))
                        with self.assertRaisesRegex(ValueError, "memory_query_mark_ids_missing"):
                            MemoryGraph(db)
                        self.assertEqual(list(db.execute(
                            "SELECT name,sql FROM sqlite_master ORDER BY name")), before)
                        self.assertEqual(list(db.execute("SELECT * FROM memory_records")), sources)
                        self.assertEqual(list(db.execute("SELECT * FROM memory_query_edges")), edges)
                        if filename != ":memory:":
                            backups = list((Path(directory) / "migration_backups").glob("*.sqlite3"))
                            self.assertEqual(len(backups), 1)
                            saved = sqlite3.connect(backups[0])
                            try:
                                self.assertEqual(list(saved.execute("SELECT * FROM memory_records")), sources)
                                self.assertEqual(list(saved.execute("SELECT * FROM memory_query_edges")), edges)
                            finally:
                                saved.close()
                    finally:
                        db.close()

    def test_incomplete_schema_is_reported_and_backed_up_even_with_scope_metadata(self):
        with TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "test-memory.sqlite3")
            try:
                graph = MemoryGraph(db)
                graph.add("scope", "synthetic", "author", [
                    {"text": "synthetic", "quote": "synthetic", "marks": ["alpha", "beta"]}], 1)
                db.execute("DROP TABLE memory_query_postings")
                db.commit()
                index = KeyedMemoryIndex(db)
                backup = index.ensure_schema()
                self.assertTrue(index.schema_recreated)
                self.assertEqual(index.missing_tables, {"memory_query_postings"})
                self.assertTrue(backup.is_file())
                self.assertIsNotNone(index.meta("scope"))
                # Creating an empty table alone deliberately does not certify
                # its content. Startup must republish the source scopes.
                self.assertEqual(db.execute("SELECT count(*) FROM memory_query_postings").fetchone()[0], 0)
                self.assertIsNone(index.ensure_schema())
                self.assertFalse(index.schema_recreated)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue(REQUIRED_QUERY_TABLES <= tables)
            finally:
                db.close()

    def test_dropped_postings_reopening_memory_graph_republishes_before_queries(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test-memory.sqlite3"
            db = sqlite3.connect(path)
            graph = MemoryGraph(db)
            graph.add("scope", "synthetic", "author", [
                {"text": "synthetic", "quote": "synthetic", "marks": ["alpha", "beta"]}], 1)
            ids = graph.query_index.mark_ids("scope", ["alpha", "beta"])
            db.execute("DROP TABLE memory_query_postings")
            db.commit()
            db.close()
            db = sqlite3.connect(path)
            try:
                reopened = MemoryGraph(db)
                self.assertTrue(reopened.query_index.schema_recreated)
                self.assertEqual(reopened.query_index.mark_ids("scope", ["alpha", "beta"]), ids)
                result = reopened.retrieve("scope", [], "alpha beta", 2, event_id="after-reopen")
                self.assertEqual([row["source_id"] for row in result], ["synthetic"])
                self.assertTrue(list((Path(directory) / "migration_backups").glob("*.sqlite3")))
            finally:
                db.close()

    def test_existing_disk_store_is_backed_up_before_schema_migration_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test-memory.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE memory_records(id INTEGER PRIMARY KEY,text TEXT)")
                db.execute("INSERT INTO memory_records VALUES(1,'synthetic original')")
                db.commit()
                index = KeyedMemoryIndex(db)
                backup = index.ensure_schema()
                self.assertTrue(backup.is_file())
                copied = sqlite3.connect(backup)
                try:
                    self.assertEqual(copied.execute("SELECT text FROM memory_records").fetchone()[0],
                                     "synthetic original")
                    self.assertIsNone(copied.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='memory_query_edges'").fetchone())
                finally:
                    copied.close()
                self.assertIsNone(index.ensure_schema())
                self.assertEqual(len(list(backup.parent.glob("*.sqlite3"))), 1)
            finally:
                db.close()

    def test_existing_disk_migration_refuses_uncommitted_store(self):
        with TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "test-memory.sqlite3")
            try:
                db.execute("CREATE TABLE memory_records(id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO memory_records VALUES(1)")
                with self.assertRaisesRegex(ValueError, "requires_committed_store"):
                    KeyedMemoryIndex(db).ensure_schema()
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='memory_query_edges'").fetchone())
            finally:
                db.close()


class LazyDecayStructureTests(unittest.TestCase):
    def test_lazy_settlement_equals_rounded_explicit_cycles_and_keeps_remainder(self):
        for weight in (0.123456, 1.5, 100.000001, 0.000001):
            for count in (0, 1, 73, 2000):
                expected = weight
                for _ in range(count):
                    expected = round(expected * 0.99, 6)
                settled = lazy_decay(weight, 10, 10 + count * 60 + 17, period_seconds=60)
                self.assertEqual(settled, {"weight": expected, "last_updated": 10 + count * 60,
                                           "cycles": count})

    def test_lazy_helper_requires_explicit_valid_time_policy(self):
        with self.assertRaises(TypeError):
            lazy_decay(1, 0, 10)
        for arguments in ((1, 5, 4, 60), (1, 0, 10, 0), (float("nan"), 0, 10, 1)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                lazy_decay(*arguments[:3], period_seconds=arguments[3])


if __name__ == "__main__":
    unittest.main()
