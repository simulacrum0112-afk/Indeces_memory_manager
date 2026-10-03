"""Production static queries never repair/load global state inside the query."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from indeces.memory import MemoryGraph
from indeces.run_records import validate_graph_audit
from indeces.scratch import canonical


class NeighborhoodStoragePathTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.graph = MemoryGraph(self.db)
        self.graph.add("scope", "target", "author", [
            {"text": "target", "quote": "target", "marks": ["alpha", "beta"]}], 1)

    def retrieve(self, event="query"):
        audit = {}
        records = self.graph.retrieve("scope", [], "alpha beta", 2, event_id=event, audit=audit)
        validate_graph_audit(audit)
        return records, audit

    def test_cold_new_and_replay_queries_do_not_enter_any_full_graph_path(self):
        self.graph = MemoryGraph(self.db)
        statements = []
        self.db.set_trace_callback(statements.append)
        with patch.object(self.graph, "_records", side_effect=AssertionError("whole records")), \
             patch.object(self.graph, "_source_edges", side_effect=AssertionError("whole edges")), \
             patch.object(self.graph, "_edges", side_effect=AssertionError("whole edges")), \
             patch.object(self.graph, "_observe", side_effect=AssertionError("shadow")), \
             patch.object(self.graph, "refresh_query_index", side_effect=AssertionError("query repair")):
            first, audit = self.retrieve()
            again, replay = self.retrieve()
        self.assertEqual(canonical(first), canonical(again))
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["durable_payload_sha256"], audit["durable_payload_sha256"])
        self.assertFalse(any("SELECT payload_json" in statement for statement in statements))
        source_reads = [sql for sql in statements if "FROM memory_records" in sql]
        self.assertTrue(source_reads)
        self.assertTrue(all("WHERE id=" in sql for sql in source_reads))
        self.assertFalse(any("FROM memory_static" in sql or "FROM memory_support" in sql for sql in statements))

    def test_publication_rolls_back_records_graph_indexes_and_revisions_together(self):
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, "synthetic rollback"):
            with self.db:
                self.db.execute("BEGIN")
                self.graph.deactivate_source("scope", "target", commit=False)
                self.graph.add("scope", "replacement", "author", [
                    {"text": "replacement", "quote": "replacement", "marks": ["alpha", "new"]}],
                    3, commit=False)
                raise RuntimeError("synthetic rollback")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.retrieve()[0][0]["source_id"], "target")

    def state(self):
        tables = [row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'memory_*' ORDER BY name")]
        return {table: list(self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}

    def test_dirty_external_writes_fail_closed_then_explicit_publication_repairs(self):
        self.db.execute("UPDATE memory_records SET marks_json='[\"gamma\"]' WHERE scope='scope'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "index is stale"):
            self.retrieve()
        with self.db:
            self.graph._rebuild_static("scope")
        records, _ = self.retrieve()
        self.assertEqual(records, [])
        self.assertEqual(len(self.graph.retrieve("scope", [], "gamma", 3)), 1)

    def test_other_connection_publication_is_seen_without_global_cache_rebuild(self):
        # Shared URI gives two handles without any user filesystem access.
        db1 = sqlite3.connect("file:subgraph-publication?mode=memory&cache=shared", uri=True)
        db2 = sqlite3.connect("file:subgraph-publication?mode=memory&cache=shared", uri=True)
        self.addCleanup(db1.close)
        self.addCleanup(db2.close)
        first, second = MemoryGraph(db1), MemoryGraph(db2)
        second.add("scope", "new", "author", [{"text": "new", "quote": "new", "marks": ["alpha"]}], 3)
        with patch.object(first, "_records", side_effect=AssertionError("global cache rebuild")):
            self.assertEqual(first.retrieve("scope", [], "alpha", 4)[0]["source_id"], "new")

    def test_audit_trigger_source_mutation_rolls_back_before_return(self):
        self.db.executescript("""
            CREATE TRIGGER synthetic_audit_source_mutation AFTER INSERT ON memory_event_audits
            BEGIN UPDATE memory_records SET marks_json='["gamma"]' WHERE scope='scope'; END;
        """)
        before = self.state()
        audit = {"unchanged": True}
        with self.assertRaisesRegex(ValueError, "sources changed"):
            self.graph.retrieve("scope", [], "alpha beta", 2, audit=audit)
        self.assertEqual(audit, {"unchanged": True})
        self.assertEqual(self.state(), before)

    def test_source_ddl_and_temp_shadow_are_rejected_without_fallback(self):
        self.db.execute("DROP TABLE memory_query_postings")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "schema changed"):
            self.retrieve()

    def test_unrelated_ddl_is_compatible_and_does_not_rebuild(self):
        self.db.execute("CREATE TABLE unrelated_message_state(id INTEGER PRIMARY KEY)")
        self.db.commit()
        with patch.object(self.graph, "refresh_query_index", side_effect=AssertionError("rebuild")):
            self.assertEqual(len(self.retrieve()[0]), 1)

    def test_temporary_index_and_provenance_shadows_never_override_main(self):
        for table in ("memory_query_postings", "memory_query_scopes", "memory_query_event_headers",
                      "memory_records", "memory_dynamic", "memory_event_audits"):
            with self.subTest(table=table):
                self.db.execute(f"CREATE TEMP TABLE {table} AS SELECT * FROM main.{table} WHERE 0")
                with self.assertRaisesRegex(ValueError, "temporary shadow"):
                    self.retrieve()
                self.db.execute(f"DROP TABLE temp.{table}")
                self.db.commit()
        self.assertEqual(len(self.retrieve()[0]), 1)

    def test_audit_provenance_trigger_removal_is_rejected(self):
        self.db.execute("DROP TRIGGER memory_event_audits_no_update")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "schema changed"):
            self.retrieve()

    def test_audit_insert_failure_rolls_back_header_and_preserves_frozen_values(self):
        self.db.executescript("""
            CREATE TRIGGER synthetic_audit_fail BEFORE INSERT ON memory_event_audits
            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END;
        """)
        before = self.state()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic audit failure"):
            self.retrieve()
        self.assertEqual(self.state(), before)

    def test_legacy_payload_header_migration_preserves_bytes_and_replay_uses_header(self):
        from tests.test_memory_selection_performance import _LegacyMemoryGraph
        legacy = _LegacyMemoryGraph(self.db)
        original = {}
        legacy.retrieve("scope", [], "alpha beta", 2, event_id="legacy", audit=original)
        serialized = self.db.execute("SELECT payload_json FROM memory_event_audits WHERE event_id='legacy'").fetchone()[0]
        self.graph = MemoryGraph(self.db)
        reads = []
        self.db.set_trace_callback(reads.append)
        records, replay = self.retrieve("legacy")
        self.assertTrue(records)
        self.assertFalse(any("payload_json" in sql for sql in reads))
        self.assertEqual(replay["original_event"]["payload_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(self.db.execute("SELECT payload_json FROM memory_event_audits WHERE event_id='legacy'").fetchone()[0], serialized)


if __name__ == "__main__":
    unittest.main()
