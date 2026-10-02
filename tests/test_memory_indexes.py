"""Indexed retrieval must retain full legacy bytes, state, and source scope."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import random
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from indeces import memory
from indeces.memory import MemoryGraph
from indeces.run_records import validate_graph_audit
from indeces.scratch import canonical
from tests import test_memory_selection_performance as oracle


_BASELINE_TABLES = oracle.MemorySelectionPerformanceTests.database_state


class FreshLegacyGraph(oracle._LegacyMemoryGraph):
    @contextmanager
    def _retrieval_transaction(self, scope):
        # Bypass every new source cache and hit index in this oracle.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            yield SimpleNamespace(records=FullScanRecords(self._records(scope), self.self_marks))

    def _records(self, scope):
        # The frozen selection/edge oracle must also read records freshly; it
        # must not share the new cache implementation under investigation.
        rows = self.connection.execute(
            "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at "
            "FROM memory_records WHERE scope=? AND active=1 ORDER BY id", (scope,))
        return [self._record(row) for row in rows]


class FullScanRecords(list):
    def __init__(self, records, self_marks):
        super().__init__(records)
        self.known = {mark for record in records for mark in record["marks"]} - self_marks

    def literal_matches(self, query):
        return {mark for mark in self.known if memory._hit(mark, query)}


class CountedLookup(dict):
    def __init__(self, mapping):
        super().__init__(mapping)
        self.keys_read = []

    def get(self, key, default=None):
        self.keys_read.append(key)
        return super().get(key, default)


class MemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.filename = Path(self.directory.name) / "synthetic.sqlite3"
        self.db = sqlite3.connect(self.filename)
        self.original_db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.addCleanup(self.original_db.close)
        self.graph = MemoryGraph(self.db)
        self.original = FreshLegacyGraph(self.original_db)
        self.statements = []
        self.db.set_trace_callback(self.statements.append)
        self.counter = 0
        for source, marks in (
            ("retiring", ["alpha", "beta", "gamma", "theta"]),
            ("retained", ["alpha", "beta", "gamma", "theta"]),
            ("neighbor", ["alpha", "extra", "gate"]),
            ("unicode", ["中文", "RuO2", "alpha"]),
            ("unrelated", ["unrelated", "other"]),
            ("orphan-a", ["orphan-one"]),
            ("orphan-b", ["orphan-two"]),
        ):
            self.add(source, marks)
        for graph in (self.graph, self.original):
            graph.add("other-scope", "separate-source", "author", [
                {"text": "separate source", "quote": "separate source", "marks": ["alpha", "private"]}], 1.0)

    def add(self, source, marks, *, now=1.0):
        fact = {"text": source + " documented synthetic text", "quote": source + " documented synthetic text",
                "marks": marks}
        for graph in (self.original, self.graph):
            graph.add("synthetic", source, "author", [fact], now)

    def compare(self, query="alpha beta gamma theta", *, mode="static", event=None,
                now=None, scope="synthetic", validate=True):
        self.counter += 1
        event = event or "indexed-event-" + str(self.counter)
        now = float(self.counter + 10) if now is None else now
        original_audit, current_audit = {}, {}
        original = self.original.retrieve(scope, [], query, now, event_id=event,
                                          audit=original_audit, ranking_mode=mode)
        current = self.graph.retrieve(scope, [], query, now, event_id=event,
                                     audit=current_audit, ranking_mode=mode)
        self.assertEqual(current, original)
        # Dict equality alone misses -0.0 and float serialization differences.
        self.assertEqual(canonical(current), canonical(original))
        self.assertEqual(canonical(current_audit), canonical(original_audit))
        # These verification reads are not retrieval work and must not enter
        # the cache workload counters attached to the actual connection.
        self.db.set_trace_callback(None)
        try:
            self.assertEqual(_BASELINE_TABLES(self.db), _BASELINE_TABLES(self.original_db))
        finally:
            self.db.set_trace_callback(self.statements.append)
        if validate:
            validate_graph_audit(current_audit)
        return current, current_audit

    def fresh_metadata_reads(self):
        return [statement for statement in self.statements
                if re.search(r"\bSELECT\b", statement, re.IGNORECASE)
                and re.search(r"\bFROM\s+(?:main\.)?(?:memory_records|memory_support|memory_static)\b",
                              statement, re.IGNORECASE)]

    def assert_warm_work(self):
        self.assertEqual(self.fresh_metadata_reads(), [])
        # Full shadow decay and provenance observation remain mandatory.
        self.assertTrue(any(re.search(r"\bFROM\s+memory_dynamic\s+d\b", statement, re.IGNORECASE)
                            and "memory_support" in statement and "memory_static" in statement
                            for statement in self.statements))

    def test_cold_warm_static_dynamic_no_hit_and_replay_keep_complete_bytes(self):
        self.compare(event="static-initial", now=2.0)
        self.statements.clear()
        _, audit = self.compare(query="alpha", event="static-warm", now=3.0)
        self.assert_warm_work()
        self.assertTrue(audit["selection"]["edge_statistics"])
        self.assertTrue(audit["observation"]["changed_edges"])
        self.compare(query="alpha", mode="dynamic", event="dynamic-initial", now=4.0)
        self.statements.clear()
        self.compare(query="alpha", mode="dynamic", event="dynamic-warm", now=5.0)
        self.assert_warm_work()
        self.compare(query="alpha", event="static-warm", now=6.0)
        self.compare(query="unseen words", event="no-hit", now=7.0)
        self.compare(query="alpha", scope="other-scope", event="other-scope", now=8.0)

    def test_unrelated_store_write_and_shadow_events_do_not_invalidate_static_index(self):
        self.compare(query="alpha", event="initial", now=2.0)
        for db in (self.original_db, self.db):
            db.execute("CREATE TABLE synthetic_unrelated_store(value TEXT)")
            db.commit()
        # DDL must first be observed; only the subsequent unrelated DML should
        # leave the validated source/static index warm.
        self.compare(query="alpha", event="after-ddl", now=3.0)
        for db in (self.original_db, self.db):
            db.execute("INSERT INTO synthetic_unrelated_store VALUES('synthetic')")
            db.commit()
        self.statements.clear()
        self.compare(query="alpha", event="after-unrelated-write", now=4.0)
        self.assert_warm_work()
        self.statements.clear()
        self.compare(query="alpha", event="after-shadow-write", now=5.0)
        self.assert_warm_work()

    def test_add_deactivate_and_replacement_invalidate_before_static_rebuild(self):
        self.compare(query="alpha", now=2.0)
        self.add("new-source", ["alpha", "fresh", "gate"], now=3.0)
        _, added = self.compare(query="alpha fresh gate", now=4.0)
        self.assertTrue(any(record["source_id"] == "new-source"
                            for record in added["selection"]["ranked_candidates"]))
        with patch("indeces.memory.time.time", return_value=5.0):
            for graph in (self.original, self.graph):
                graph.deactivate_source("synthetic", "retiring")
        _, retired = self.compare(now=6.0)
        self.assertTrue(all(record["source_id"] != "retiring"
                            for record in retired["selection"]["ranked_candidates"]))
        self.add("replacement", ["alpha", "beta", "fresh"], now=7.0)
        self.compare(query="alpha beta fresh", now=8.0)

    def test_transactional_replacement_commit_false_and_rollback_cannot_publish_stale_cache(self):
        self.compare(query="alpha", now=2.0)
        for graph, db in ((self.original, self.original_db), (self.graph, self.db)):
            db.execute("BEGIN IMMEDIATE")
            with patch("indeces.memory.time.time", return_value=3.0):
                graph.deactivate_source("synthetic", "retiring", commit=False)
            graph.add("synthetic", "uncommitted", "author", [{
                "text": "uncommitted synthetic text", "quote": "uncommitted synthetic text",
                "marks": ["alpha", "temporary"]}], 3.0, commit=False)
            # Force candidate data into any cache while its transaction is
            # still uncommitted, then restore the committed state.
            graph._records("synthetic")
            graph._edges("synthetic", "static")
            db.rollback()
        self.compare(query="alpha temporary", now=4.0)
        for graph, db in ((self.original, self.original_db), (self.graph, self.db)):
            db.execute("BEGIN IMMEDIATE")
            with patch("indeces.memory.time.time", return_value=5.0):
                graph.deactivate_source("synthetic", "retiring", commit=False)
            graph.add("synthetic", "committed-replacement", "author", [{
                "text": "committed replacement synthetic text", "quote": "committed replacement synthetic text",
                "marks": ["alpha", "replacement"]}], 5.0, commit=False)
            db.commit()
        self.compare(query="alpha replacement", now=6.0)

    def test_second_graph_on_same_connection_invalidates_cached_source_metadata(self):
        self.compare(query="alpha", now=2.0)
        other = MemoryGraph(self.db)
        other.add("synthetic", "same-connection", "author", [{
            "text": "same connection synthetic text", "quote": "same connection synthetic text",
            "marks": ["alpha", "new-mark"]}], 3.0)
        self.original.add("synthetic", "same-connection", "author", [{
            "text": "same connection synthetic text", "quote": "same connection synthetic text",
            "marks": ["alpha", "new-mark"]}], 3.0)
        self.compare(query="alpha new-mark", now=4.0)

    def test_alternating_warm_graphs_share_tracking_without_reinstalling_triggers(self):
        first = self.graph
        second = MemoryGraph(self.db)
        try:
            for index, graph in enumerate((first, second)):
                self.graph = graph
                self.compare(query="alpha", event="warm-graph-" + str(index), now=2.0 + index)
            schema_version = self.db.execute("PRAGMA temp.schema_version").fetchone()[0]
            for index, graph in enumerate((first, second, first, second)):
                self.graph = graph
                self.statements.clear()
                self.compare(query="alpha", event="alternating-graph-" + str(index), now=4.0 + index)
                self.assert_warm_work()
                self.assertFalse(any(re.search(r"\b(?:CREATE\s+(?:TEMP\s+)?|DROP\s+)TRIGGER\b",
                                                statement, re.IGNORECASE)
                                     for statement in self.statements))
                self.assertEqual(self.db.execute("PRAGMA temp.schema_version").fetchone()[0], schema_version)
        finally:
            self.graph = first

    def test_cross_scope_insert_or_replace_invalidates_deleted_conflict_scope_without_recursive_triggers(self):
        self.compare(query="alpha", now=2.0)
        self.compare(query="alpha private", scope="other-scope", now=3.0)
        for graph, db in ((self.original, self.original_db), (self.graph, self.db)):
            db.execute("PRAGMA recursive_triggers=OFF")
            old_id = db.execute("SELECT id FROM memory_records WHERE source_id='retiring'").fetchone()[0]
            replacement = list(db.execute(
                "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at,fingerprint "
                "FROM memory_records WHERE scope='other-scope' LIMIT 1").fetchone())
            replacement[0] = old_id
            replacement[2] = "cross-scope-insert-replacement"
            db.execute(
                "INSERT OR REPLACE INTO memory_records "
                "(id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at,fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", replacement)
            graph._rebuild_static("synthetic")
            graph._rebuild_static("other-scope")
            db.commit()
        _, audit = self.compare(query="alpha", now=4.0)
        self.assertFalse(any(candidate["source_id"] == "retiring"
                             for candidate in audit["selection"]["ranked_candidates"]))
        self.compare(query="alpha private", scope="other-scope", now=5.0)

    def test_cross_scope_update_or_replace_invalidates_deleted_conflict_scope_without_recursive_triggers(self):
        self.compare(query="alpha", now=2.0)
        self.compare(query="alpha private", scope="other-scope", now=3.0)
        for graph, db in ((self.original, self.original_db), (self.graph, self.db)):
            db.execute("PRAGMA recursive_triggers=OFF")
            old_id = db.execute("SELECT id FROM memory_records WHERE source_id='retiring'").fetchone()[0]
            replacement_id = db.execute("SELECT id FROM memory_records WHERE scope='other-scope'").fetchone()[0]
            db.execute("UPDATE OR REPLACE memory_records SET id=? WHERE id=?", (old_id, replacement_id))
            graph._rebuild_static("synthetic")
            graph._rebuild_static("other-scope")
            db.commit()
        _, audit = self.compare(query="alpha", now=4.0)
        self.assertFalse(any(candidate["source_id"] == "retiring"
                             for candidate in audit["selection"]["ranked_candidates"]))
        self.compare(query="alpha private", scope="other-scope", now=5.0)

    def test_second_connection_changes_are_visible_at_next_snapshot(self):
        self.compare(query="alpha", now=2.0)
        with closing(sqlite3.connect(self.filename)) as other_db:
            other = MemoryGraph(other_db)
            other.add("synthetic", "other-connection", "author", [{
                "text": "other connection synthetic text", "quote": "other connection synthetic text",
                "marks": ["alpha", "external"]}], 3.0)
        self.original.add("synthetic", "other-connection", "author", [{
            "text": "other connection synthetic text", "quote": "other connection synthetic text",
            "marks": ["alpha", "external"]}], 3.0)
        self.compare(query="alpha external", now=4.0)

    def test_main_and_temp_ddl_are_observed_without_changing_results(self):
        self.compare(query="alpha", now=2.0)
        for statement in ("CREATE TABLE synthetic_ddl(value TEXT)",
                          "CREATE TEMP TABLE synthetic_temp_ddl(value TEXT)",
                          "CREATE INDEX synthetic_index ON memory_records(scope,active,id)"):
            for db in (self.original_db, self.db):
                db.execute(statement)
                db.commit()
            self.statements.clear()
            self.compare(query="alpha")
            self.assertTrue(self.fresh_metadata_reads())

    def test_records_rename_recreate_and_rename_back_rebind_revision_triggers(self):
        self.compare(query="alpha", now=2.0)
        for db in (self.original_db, self.db):
            create_sql = db.execute(
                "SELECT sql FROM main.sqlite_master WHERE type='table' AND name='memory_records'").fetchone()[0]
            db.execute("ALTER TABLE main.memory_records RENAME TO synthetic_previous_records")
            db.execute(create_sql)
            db.execute("INSERT INTO main.memory_records SELECT * FROM main.synthetic_previous_records")
            db.commit()
        self.compare(query="alpha", now=3.0)
        for db in (self.original_db, self.db):
            db.execute("UPDATE main.memory_records SET author_id='recreated-author' WHERE scope='synthetic'")
            db.commit()
        self.statements.clear()
        records, _ = self.compare(query="alpha", now=4.0)
        self.assertTrue(self.fresh_metadata_reads())
        self.assertTrue(records)
        self.assertTrue(all(record["author_id"] == "recreated-author" for record in records))

        for db in (self.original_db, self.db):
            db.execute("DROP TABLE main.memory_records")
            db.execute("ALTER TABLE main.synthetic_previous_records RENAME TO memory_records")
            db.commit()
        self.compare(query="alpha", now=5.0)
        for db in (self.original_db, self.db):
            db.execute("UPDATE main.memory_records SET author_id='restored-author' WHERE scope='synthetic'")
            db.commit()
        self.statements.clear()
        records, _ = self.compare(query="alpha", now=6.0)
        self.assertTrue(self.fresh_metadata_reads())
        self.assertTrue(all(record["author_id"] == "restored-author" for record in records))
        tracked = self.db.execute(
            "SELECT tbl_name FROM temp.sqlite_master WHERE type='trigger' "
            "AND name LIKE 'indeces_retrieval_%records%'").fetchall()
        self.assertEqual(len(tracked), 5)
        self.assertTrue(all(table == "memory_records" for table, in tracked))

    def test_source_change_during_observation_discards_snapshot_after_commit(self):
        for db in (self.original_db, self.db):
            db.execute("CREATE TRIGGER synthetic_observation_source_change "
                       "AFTER INSERT ON memory_events "
                       "WHEN NEW.event_id='source-change-during-observation' "
                       "BEGIN UPDATE memory_records SET author_id='observation-author' "
                       "WHERE scope=NEW.scope; "
                       "UPDATE memory_support SET context_json='[\"observation-context\"]' "
                       "WHERE scope=NEW.scope AND a='alpha' AND b='beta'; "
                       "UPDATE memory_static SET context_json='[\"observation-context\"]' "
                       "WHERE scope=NEW.scope AND a='alpha' AND b='beta'; END")
            db.commit()
        self.compare(query="alpha", event="before-observation-change", now=2.0)
        self.assertIn("synthetic", self.graph._retrieval_cache)
        records, audit = self.compare(query="alpha", event="source-change-during-observation", now=3.0)
        # Selection observes the records loaded at the start of this turn;
        # the next retrieval must load the source change committed by it.
        self.assertTrue(all(record["author_id"] == "author" for record in records))
        changed = next(edge for edge in audit["selection"]["edge_statistics"]
                       if (edge["a"], edge["b"]) == ("alpha", "beta"))
        self.assertEqual(changed["context"], ["observation-context"])
        self.assertNotIn("synthetic", self.graph._retrieval_cache)
        self.statements.clear()
        records, _ = self.compare(query="alpha", event="after-observation-change", now=4.0)
        self.assertTrue(self.fresh_metadata_reads())
        self.assertTrue(all(record["author_id"] == "observation-author" for record in records))

    def test_dynamic_zero_filters_malformed_static_weight_before_unused_neighbor_sort(self):
        self.compare(query="alpha", mode="dynamic", event="normal-dynamic-seed", now=2.0)
        for db in (self.original_db, self.db):
            db.execute("UPDATE memory_static SET weight='malformed' "
                       "WHERE scope='synthetic' AND a='alpha' AND b='beta'")
            db.execute("UPDATE memory_dynamic SET weight=0 "
                       "WHERE scope='synthetic' AND a='alpha' AND b='beta'")
            db.commit()
        # This deliberately corrupted row cannot satisfy the audit validator,
        # but the historical dynamic filter must still handle it identically.
        _, audit = self.compare(query="alpha", mode="dynamic", event="malformed-filtered", now=3.0,
                                validate=False)
        self.assertFalse(any((edge["a"], edge["b"]) == ("alpha", "beta")
                             for edge in audit["selection"]["edge_statistics"]))
        changed = next(edge for edge in audit["observation"]["changed_edges"]
                       if (edge["a"], edge["b"]) == ("alpha", "beta"))
        self.assertEqual(changed["static_score"], "malformed")
        self.assertEqual(changed["after_weight"], 0.0)

    def test_temp_records_shadow_remains_fresh_after_caller_owned_update(self):
        self.compare(query="alpha", now=2.0)
        for db in (self.original_db, self.db):
            db.execute("CREATE TEMP TABLE memory_records AS SELECT * FROM main.memory_records")
            db.commit()
        self.compare(query="alpha", event="temporary-records-warm", now=3.0)
        for db in (self.original_db, self.db):
            db.execute("UPDATE temp.memory_records SET author_id='temporary-author', "
                       "text='temporary synthetic text',quote='temporary synthetic text' "
                       "WHERE scope='synthetic'")
            db.commit()
        self.statements.clear()
        records, _ = self.compare(query="alpha", event="temporary-records-updated", now=4.0)
        self.assertTrue(self.fresh_metadata_reads())
        self.assertTrue(records)
        self.assertTrue(all(record["author_id"] == "temporary-author" for record in records))
        self.assertTrue(all(record["text"] == "temporary synthetic text" for record in records))
        self.assertEqual(self.db.execute(
            "SELECT DISTINCT author_id FROM main.memory_records WHERE scope='synthetic'").fetchall(),
            [("author",)])
        self.assertNotIn("synthetic", self.graph._retrieval_cache)

    def test_inactive_blob_edge_key_is_filtered_without_sorting_mixed_types(self):
        self.compare(query="alpha", mode="dynamic", event="before-inactive-blob", now=2.0)
        for db in (self.original_db, self.db):
            pair = ("synthetic", sqlite3.Binary(b"inactive-source-key"), "omega")
            db.execute("INSERT INTO memory_support VALUES(?,?,?,'[]','[]',0)", pair)
            db.execute("INSERT INTO memory_static VALUES(?,?,?,0,'[]','[]',0)", pair)
            # The scope was already seeded. This new unsupported edge has
            # no dynamic row, so both static and dynamic effective weights
            # are zero and the historical path excludes it before sorting.
            self.assertIsNone(db.execute(
                "SELECT weight FROM memory_dynamic WHERE scope=? AND a=? AND b=?", pair).fetchone())
            db.commit()
        for index, mode in enumerate(("static", "dynamic")):
            _, audit = self.compare(query="alpha", mode=mode, event="inactive-blob-" + mode,
                                    now=3.0 + index)
            self.assertTrue(all(isinstance(edge["a"], str) and isinstance(edge["b"], str)
                                for edge in audit["selection"]["edge_statistics"]))

    def test_mutating_returned_records_and_audit_cannot_poison_cached_values(self):
        records, audit = self.compare(query="alpha beta gamma theta", now=2.0)
        before = deepcopy(audit)
        records[0]["marks"].append("caller-injected-mark")
        records[0]["direct_marks"].clear()
        records[0]["evidence"][0]["context"].append("caller-injected-context")
        records[0]["evidence"][0]["source_record_ids"].append(987654)
        audit["match"]["literal_matches"].append("caller-injected-match")
        audit["selection"]["mark_frequencies"]["caller-injected-frequency"] = 99
        audit["selection"]["edge_statistics"][0]["context"].append("caller-injected-edge-context")
        audit["selection"]["edge_statistics"][0]["source_record_ids"].append(987653)
        audit["selection"]["ranked_candidates"][0]["marks"].append("caller-injected-candidate")
        audit["selection"]["ranked_candidates"][0]["evidence"].clear()
        audit["observation"]["changed_edges"][0]["source_context"].append("caller-injected-observation")
        self.assertNotEqual(audit, before)
        next_records, next_audit = self.compare(query="alpha beta gamma theta", now=3.0)
        self.assertNotIn("caller-injected", repr(next_records))
        self.assertNotIn("caller-injected", repr(next_audit))

    def test_self_mark_configuration_and_connection_identity_do_not_reuse_wrong_index(self):
        self.compare(query="alpha beta", now=2.0)
        for graph in (self.original, self.graph):
            graph.self_marks.add("alpha")
        # Changing this private configuration directly does not rebuild the
        # baseline's static rows; only exact legacy bytes are required here.
        self.compare(query="alpha beta", now=3.0, validate=False)
        other_original_db, other_current_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        self.addCleanup(other_original_db.close)
        self.addCleanup(other_current_db.close)
        other_original, other_current = FreshLegacyGraph(other_original_db), MemoryGraph(other_current_db)
        for graph in (other_original, other_current):
            graph.add("synthetic", "different-database", "author", [{
                "text": "different database synthetic text", "quote": "different database synthetic text",
                "marks": ["beta", "separate"]}], 4.0)
        self.original.connection, self.graph.connection = other_original_db, other_current_db
        self.original_db, self.db = other_original_db, other_current_db
        self.compare(query="beta separate", now=5.0)

    def test_failed_selection_transaction_clears_staged_shadow_state_and_index(self):
        self.compare(query="alpha", now=2.0)
        for graph, db in ((self.original, self.original_db), (self.graph, self.db)):
            before = _BASELINE_TABLES(db)
            with patch.object(graph, "_selection", side_effect=ValueError("synthetic selection failure")):
                with self.assertRaisesRegex(ValueError, "synthetic selection failure"):
                    graph.retrieve("synthetic", [], "alpha beta", 3.0, event_id="failed-index-turn")
            self.assertEqual(_BASELINE_TABLES(db), before)
        self.compare(query="alpha beta", event="failed-index-turn", now=4.0)

    def test_literal_trie_matches_exact_latin_unicode_casefold_and_ascii_boundary_gate(self):
        marks = ["i", "k", "s", "ss", "ffi", "RuO2", "H_2", "a.b", "m-4", "_", ".a", "a-",
                 "Straße", "ß", "ﬃ", "İ", "ı", "ſ", "K", "中文", "日本語", "Σ", "σ", "ς",
                 "e\u0301", "é", "i\u0307", "\u0307", "", "bot"]
        records = [{"marks": marks}]
        indexed = memory._IndexedRecords(records, {"bot"})
        queries = ["", "İ ı ſ K", "İ_ _ı _ſ _K", "İi iİ Kk kK", "ß ss ﬃ ffi", "STRASSE Straße",
                   "RuO2! RuO20 xH_2 H_2", "a.b xa.b a.bx m-4 m-40", "foo.a .a a- abc-", "_ __",
                   "中文材料 日本語", "Σ σ ς", "e\u0301 é", "İ i\u0307", "bot", "\n\t", "😀中文😀"]
        for query in queries:
            with self.subTest(query=query):
                expected = {mark for mark in set(marks) - {"bot"} if memory._hit(mark, query)}
                self.assertEqual(indexed.literal_matches(query), expected)

    def test_literal_trie_seeded_unicode_queries_match_full_baseline_scan(self):
        rng = random.Random(331179)
        alphabet = "aAiIkKsS_09.- İıſKßﬃΣσς中文日本語é\u0301\u0307😀\n\t"
        marks = {"".join(rng.choices(alphabet, k=rng.randrange(1, 8))) for _ in range(500)}
        indexed = memory._IndexedRecords([{"marks": list(marks)}], set())
        queries = ["".join(rng.choices(alphabet, k=rng.randrange(0, 40))) for _ in range(80)]
        queries.extend(rng.sample(sorted(marks), 40))
        for query in queries:
            expected = {mark for mark in marks if memory._hit(mark, query)}
            self.assertEqual(indexed.literal_matches(query), expected)

    def test_literal_trie_confirmation_work_depends_on_query_candidates_not_all_known_marks(self):
        labels = ["disconnected-label-" + str(index) for index in range(5000)]
        labels.extend(["alpha", "gate", "中文"])
        indexed = memory._IndexedRecords([{"marks": labels}], set())
        self.assertEqual(len(indexed.known), 5003)
        for query in ("alpha gate", "中文材料", "no matching labels"):
            expected = {mark for mark in labels if memory._hit(mark, query)}
            with patch("indeces.memory._hit", wraps=memory._hit) as confirm:
                result = indexed.literal_matches(query)
            self.assertEqual(result, expected)
            self.assertEqual(confirm.call_count, len(expected))
            self.assertLessEqual(confirm.call_count, 2)

    def test_indexed_selection_reads_only_allowed_postings_and_direct_hit_neighbor_buckets(self):
        self.compare(query="alpha beta", now=2.0)
        original_selection = self.graph._selection
        measured = {}

        def indexed_selection(records, hits, edges, query, event_id):
            self.assertIsInstance(records, memory._IndexedRecords)
            self.assertIsInstance(edges, memory._IndexedEdges)
            old_postings, old_neighbors, old_incident = records.postings, edges.static_neighbors, edges.incident
            records.postings = CountedLookup(old_postings)
            edges.static_neighbors = CountedLookup(old_neighbors)
            edges.incident = CountedLookup(old_incident)
            try:
                result = original_selection(records, hits, edges, query, event_id)
                measured.update(postings=list(records.postings.keys_read),
                                neighbors=list(edges.static_neighbors.keys_read),
                                incident=list(edges.incident.keys_read), hits=list(hits))
                return result
            finally:
                records.postings, edges.static_neighbors, edges.incident = old_postings, old_neighbors, old_incident

        with patch.object(self.graph, "_selection", side_effect=indexed_selection):
            _, audit = self.compare(query="alpha beta", now=3.0)
        allowed = set(audit["match"]["direct_hits"]) | set(audit["selection"]["expanded_marks"])
        self.assertEqual(set(measured["postings"]), allowed)
        self.assertEqual(len(measured["postings"]), len(allowed))
        self.assertLessEqual(len(measured["postings"]), 6)
        self.assertEqual(measured["neighbors"], measured["hits"])
        self.assertLessEqual(len(measured["neighbors"]), 4)
        self.assertTrue(set(measured["incident"]) <= set(measured["hits"]))
        self.assertLessEqual(len(measured["incident"]), len(measured["hits"]))
        self.assertEqual(len(audit["selection"]["edge_statistics"]), audit["selection"]["live_edge_count"])


if __name__ == "__main__":
    unittest.main()
