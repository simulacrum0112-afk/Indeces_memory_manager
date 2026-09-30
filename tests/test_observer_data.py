"""Only synthetic local fixtures; inspection cannot mutate the agent state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import DiscordConfig, KnowledgeConfig
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces import observer_data
from indeces.run_records import answer_record, digest, freeze_retrieval
from indeces.scratch import ScratchLog, canonical, read_snapshot
from indeces.store import Store


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class ObserverDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(name="Indices", state_dir=self.root / "state", scratch_dir=self.root / "scratch",
            knowledge_dir=self.root / "knowledge", discord=DiscordConfig("10"), knowledge=KnowledgeConfig())
        self.clock_patch = patch("indeces.observer_data._now", return_value=NOW)
        self.clock_patch.start()
        self.store = None

    def tearDown(self):
        self.clock_patch.stop()
        if self.store:
            self.store.close()
        self.temp.cleanup()

    def database(self):
        self.store = Store(self.config.state_dir)
        self.graph = MemoryGraph(self.store.db)
        self.knowledge = KnowledgeService(self.config, self.store, self.graph, None, None)
        return self.store.db

    def source(self, source_id, text, marks, *, status="ready", scope="10:knowledge", path="sample.md", publish=True):
        db = self.store.db
        with db:
            db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                (source_id, path, hashlib.sha256(text.encode()).hexdigest(), text, status, 1, scope))
            db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)", (source_id, 0, text, 0, json.dumps(marks)))
            db.execute("INSERT OR REPLACE INTO knowledge_desired VALUES(?,?,?)", (scope, path, source_id))
            if publish:
                db.execute("INSERT OR REPLACE INTO knowledge_published VALUES(?,?,?)", (scope, path, source_id))
        if publish:
            self.graph.add(scope, source_id, "fixture", [{"text": text, "quote": text, "marks": marks}], 1)

    def log(self, events):
        clock = Clock(NOW - timedelta(hours=25))
        log = ScratchLog(self.config.scratch_dir, clock=clock)
        for when, name, fields in events:
            clock.value = when
            log.write(name, **fields)
        path = log.path
        log.close()
        return path

    def test_empty_uninitialized_views_create_nothing(self):
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["graph"]["nodes"], [])
        self.assertEqual(result["scratch"]["files"], [])
        self.assertFalse(self.config.state_dir.exists())
        self.assertFalse(self.config.scratch_dir.exists())
        self.assertFalse(observer_data.source_details(self.config, "none")["found"])
        self.assertIsNone(observer_data.edge_details(self.config, "a", "b")["edge"])

    def test_snapshot_is_readonly_and_scope_isolated(self):
        db = self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        self.source("other", "secret foreign scope", ["foreign"], scope="20:knowledge", path="other.md")
        self.graph.retrieve("10:knowledge", [], "alpha beta", NOW.timestamp(), event_id="event-1")
        before = "\n".join(db.iterdump())
        files_before = {path.name: path.read_bytes() for path in self.config.state_dir.iterdir()}
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["mode"], "literal_hits")
        self.assertEqual(result["graph"]["active_record_count"], 1)
        self.assertEqual({node["id"] for node in result["graph"]["nodes"]}, {"alpha", "beta"})
        self.assertEqual(result["graph"]["edges"][0]["source_ids"], ["one"])
        self.assertEqual(result["graph"]["edges"][0]["dynamic_weight"], 1.99)
        self.assertEqual(result["graph"]["edges"][0]["effective_weight"], 1.99)
        self.assertEqual(result["warnings"], [])
        self.assertEqual("\n".join(db.iterdump()), before)
        self.assertEqual({path.name: path.read_bytes() for path in self.config.state_dir.iterdir()}, files_before)
        with observer_data._database(self.config) as readonly:
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("DELETE FROM memory_records")

    def test_previous_published_version_is_separate_from_pending(self):
        self.database()
        self.source("old", "old source alpha beta", ["alpha", "beta"])
        self.source("new", "new unlabelled source", [], status="labelling", publish=False)
        result = observer_data.snapshot(self.config)
        self.assertEqual([row["source_id"] for row in result["knowledge"]["published"]], ["old"])
        self.assertEqual([row["source_id"] for row in result["knowledge"]["pending"]], ["new"])
        self.assertEqual(result["knowledge"]["counts"], {"published": 1, "pending": 1, "versions": 2})
        old = observer_data.source_details(self.config, "old")
        self.assertTrue(old["published"])
        self.assertTrue(old["active"])
        self.assertFalse(old["desired"])
        self.assertEqual(old["version"]["raw_text"], "old source alpha beta")
        self.assertEqual(old["chunks"][0]["marks"], ["alpha", "beta"])
        new = observer_data.source_details(self.config, "new")
        self.assertTrue(new["desired"])
        self.assertFalse(new["published"])
        self.assertFalse(new["active"])

    def test_foreign_source_cannot_be_retrieved_by_id(self):
        self.database()
        self.source("foreign-id", "do not disclose", ["hidden"], scope="99:knowledge")
        result = observer_data.source_details(self.config, "foreign-id")
        self.assertFalse(result["found"])
        self.assertNotIn("do not disclose", json.dumps(result))

    def test_unsupported_dynamic_edge_is_historical_only(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        self.graph.retrieve("10:knowledge", [], "alpha beta", NOW.timestamp(), event_id="event-1")
        self.graph.deactivate_source("10:knowledge", "one")
        edge = observer_data.edge_details(self.config, "beta", "alpha")
        self.assertFalse(edge["edge"]["active_source_support"])
        self.assertIsNone(edge["edge"]["effective_weight"])
        self.assertEqual(edge["edge"]["source_ids"], [])
        self.assertEqual(edge["edge"]["dynamic_weight"], 1.99)
        self.assertEqual(edge["history"][0]["event_id"], "event-1")
        self.assertEqual(edge["history"][0]["change"]["reinforcement_added"], 1.0)
        self.assertEqual(edge["history"][0]["parameters"]["decay"], 0.99)
        self.assertEqual(observer_data.snapshot(self.config)["graph"]["nodes"][0]["frequency"], 0)

    def test_weight_history_is_newest_first_and_durable(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        self.graph.retrieve("10:knowledge", [], "alpha beta", 1, event_id="old-event")
        self.graph.retrieve("10:knowledge", [], "alpha", NOW.timestamp(), event_id="new-event")
        history = observer_data.edge_details(self.config, "alpha", "beta")["history"]
        self.assertEqual([item["event_id"] for item in history], ["new-event", "old-event"])
        self.assertEqual(history[0]["change"]["reinforcement_added"], 0)
        self.assertEqual(history[0]["change"]["after_weight"], 1.9701)
        self.assertEqual(history[1]["query"], "alpha beta")

    def test_scratch_ignores_expired_future_and_unmanaged_content(self):
        path = self.log([
            (NOW - timedelta(hours=25), "turn_start", {"trace_id": "old", "text": "expired-private"}),
            (NOW - timedelta(hours=24), "turn_start", {"trace_id": "boundary", "text": "expired-boundary"}),
            (NOW - timedelta(minutes=1), "turn_start", {"trace_id": "live", "input": {"text": "retained"}}),
            (NOW + timedelta(minutes=1), "turn_end", {"trace_id": "future", "status": "delivered"}),
        ])
        (path.parent / "unmanaged.jsonl").write_text("private unmanaged", encoding="utf-8")
        result = observer_data.snapshot(self.config)
        self.assertEqual([row["trace_id"] for row in result["scratch"]["traces"]], ["live"])
        detail = observer_data.trace_details(self.config, "old")
        self.assertEqual(detail["records"], [])
        self.assertNotIn("expired-private", json.dumps(result) + json.dumps(detail))
        live = observer_data.trace_details(self.config, "live")
        self.assertEqual(live["records"][0]["fields"]["input"]["text"], "retained")
        self.assertEqual(live["verification"]["lifecycle"], "not_evaluated")

    def test_incomplete_append_returns_no_unverified_prefix(self):
        path = self.log([(NOW - timedelta(minutes=1), "turn_start", {"trace_id": "one"})])
        with path.open("ab") as stream:
            stream.write(b'{"private_partial":"do not disclose"')
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["scratch"]["traces"], [])
        self.assertIsNotNone(result["scratch"]["files"][0]["error"])
        detail = observer_data.trace_details(self.config, "one")
        self.assertEqual(detail["records"], [])
        self.assertNotIn("do not disclose", json.dumps(result) + json.dumps(detail))

    def test_offline_expiry_is_partial_without_inventing_checkpoint(self):
        self.log([
            (NOW - timedelta(hours=25), "turn_start", {"trace_id": "cross-window", "text": "expired synthetic text"}),
            (NOW, "turn_end", {"trace_id": "cross-window", "status": "delivered"}),
        ])
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["scratch"]["traces"][0]["status"], "retention_partial")
        detail = observer_data.trace_details(self.config, "cross-window")
        self.assertEqual(detail["checkpoints"], [])
        self.assertEqual(len(detail["records"]), 1)
        self.assertIn("retention_partial", detail["verification"]["warnings"])
        self.assertIn("viewer_cutoff_removed_trace_records", detail["verification"]["warnings"])
        self.assertNotIn("expired synthetic text", json.dumps(result) + json.dumps(detail))

    def test_retention_partial_checkpoint_is_disclosed(self):
        clock = Clock(NOW - timedelta(hours=25))
        log = ScratchLog(self.config.scratch_dir, clock=clock)
        log.write("turn_start", trace_id="cross-window")
        clock.value = NOW - timedelta(minutes=1)
        log.write("turn_end", trace_id="cross-window", status="delivered")
        clock.value = NOW
        log.prune()
        log.close()
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["scratch"]["traces"][0]["status"], "retention_partial")
        detail = observer_data.trace_details(self.config, "cross-window")
        self.assertEqual(len(detail["records"]), 1)
        self.assertEqual(len(detail["checkpoints"]), 1)
        self.assertIn("retention_partial", detail["verification"]["warnings"])

    def test_truncation_bounds_nodes_edges_history_source_and_trace(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        self.source("two", "gamma delta", ["gamma", "delta"], path="second.md")
        self.graph.retrieve("10:knowledge", [], "alpha beta", 1, event_id="a")
        self.graph.retrieve("10:knowledge", [], "alpha beta", 2, event_id="b")
        with patch.object(observer_data, "MAX_NODES", 2), patch.object(observer_data, "MAX_EDGES", 1):
            graph = observer_data.snapshot(self.config)["graph"]
        self.assertEqual(graph["total_nodes"], 4)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertTrue(graph["truncated"])
        with patch.object(observer_data, "MAX_HISTORY_CHANGES", 1):
            detail = observer_data.edge_details(self.config, "alpha", "beta")
        self.assertEqual(len(detail["history"]), 1)
        self.assertTrue(detail["truncated"])
        with patch.object(observer_data, "MAX_SOURCE_CHARACTERS", 4):
            source = observer_data.source_details(self.config, "one")
        self.assertEqual(source["version"]["raw_text"], "alph")
        self.assertTrue(source["truncated"])
        self.log([(NOW - timedelta(minutes=1), "first", {"trace_id": "one"}), (NOW, "last", {"trace_id": "one"})])
        with patch.object(observer_data, "MAX_TRACE_RECORDS", 1):
            trace = observer_data.trace_details(self.config, "one")
        self.assertEqual([row["event"] for row in trace["records"]], ["last"])
        self.assertTrue(trace["truncated"])

    def test_per_record_retrieval_graph_and_citation_checks(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        audit = {}
        records = self.graph.retrieve("10:knowledge", [], "alpha beta", 1, event_id="event", audit=audit)
        frozen = freeze_retrieval(self.store.db, "10:knowledge", "event", "alpha beta", records, audit)
        answer = answer_record("Synthetic answer [M1].", frozen)
        self.log([
            (NOW - timedelta(minutes=1), "memory_observation", {"trace_id": "one", "audit": audit, "audit_sha256": digest(audit)}),
            (NOW - timedelta(seconds=30), "retrieval_record", {"trace_id": "one", "record": frozen, "record_sha256": digest(frozen)}),
            (NOW, "answer_generated", {"trace_id": "one", "record": answer, "retrieval_sha256": digest(frozen)}),
        ])
        trace = observer_data.trace_details(self.config, "one")
        self.assertEqual(trace["verification"]["status"], "retained_checks_passed")
        self.assertEqual(trace["verification"]["checked_records"], 3)
        self.assertEqual(trace["verification"]["issues"], [])
        self.assertEqual(trace["verification"]["lifecycle"], "not_evaluated")

    def test_corrupt_frozen_record_reports_invalid_even_with_valid_envelope(self):
        self.log([(NOW, "retrieval_record", {"trace_id": "one", "record": {}, "record_sha256": "0" * 64})])
        detail = observer_data.trace_details(self.config, "one")
        self.assertEqual(detail["verification"]["status"], "invalid")
        self.assertEqual(detail["verification"]["issues"][0]["reason"], "retained_record_check_failed")

    def test_edge_lists_and_graph_payload_have_explicit_bounds(self):
        self.database()
        self.source("one", "alpha beta gamma", ["alpha", "beta", "gamma"])
        self.source("two", "alpha beta gamma two", ["alpha", "beta", "gamma"], path="two.md")
        with patch.object(observer_data, "MAX_EDGE_SOURCES", 1), patch.object(observer_data, "MAX_EDGE_CONTEXT", 0):
            edge = observer_data.edge_details(self.config, "alpha", "beta")["edge"]
        self.assertEqual(edge["source_count"], 2)
        self.assertEqual(edge["source_ids"], ["one"])
        self.assertEqual(edge["context_count"], 1)
        self.assertEqual(edge["context"], [])
        self.assertTrue(edge["truncated"])
        with patch.object(observer_data, "MAX_GRAPH_BYTES", 1):
            graph = observer_data.snapshot(self.config)["graph"]
        self.assertEqual(graph["edges"], [])
        self.assertTrue(graph["truncated"])

    def test_history_scan_and_payload_caps_disclose_omission(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        for index in range(3):
            self.graph.retrieve("10:knowledge", [], "alpha beta", index + 1, event_id=f"event-{index}")
        with patch.object(observer_data, "MAX_HISTORY_EVENTS", 2):
            result = observer_data.edge_details(self.config, "alpha", "beta")
        self.assertEqual([row["event_id"] for row in result["history"]], ["event-2", "event-1"])
        self.assertTrue(result["truncated"])
        with patch.object(observer_data, "MAX_HISTORY_BYTES", 1):
            result = observer_data.edge_details(self.config, "alpha", "beta")
        self.assertEqual(result["history"], [])
        self.assertTrue(result["truncated"])
        with patch.object(observer_data, "MAX_AUDIT_BYTES", 1):
            result = observer_data.edge_details(self.config, "alpha", "beta")
        self.assertEqual(result["history"], [])
        self.assertTrue(result["truncated"])

    def test_trace_and_source_aggregate_byte_caps(self):
        self.database()
        self.source("one", "alpha beta", ["alpha", "beta"])
        with patch.object(observer_data, "MAX_SOURCE_BYTES", 1):
            source = observer_data.source_details(self.config, "one")
        self.assertEqual(source["chunks"], [])
        self.assertTrue(source["truncated"])
        self.log([(NOW, "example", {"trace_id": "one", "text": "synthetic payload"})])
        with patch.object(observer_data, "MAX_TRACE_BYTES", 1):
            trace = observer_data.trace_details(self.config, "one")
        self.assertEqual(trace["records"], [])
        self.assertTrue(trace["truncated"])
        self.assertIn("view_truncated", trace["verification"]["warnings"])

    def test_scratch_size_limit_withholds_entire_file(self):
        self.log([(NOW, "example", {"trace_id": "one", "text": "synthetic payload"})])
        with patch.object(observer_data, "MAX_SCRATCH_BYTES", 1):
            result = observer_data.snapshot(self.config)
        self.assertEqual(result["scratch"]["traces"], [])
        self.assertEqual(result["scratch"]["files"][0]["error"], "temporarily_unverifiable_or_exceeds_limit")
        self.assertNotIn("synthetic payload", json.dumps(result))

    def test_invalid_sqlite_is_a_safe_empty_state(self):
        self.config.state_dir.mkdir()
        path = self.config.state_dir / "memory.sqlite3"
        path.write_bytes(b"synthetic corrupt database")
        before = path.read_bytes()
        result = observer_data.snapshot(self.config)
        self.assertEqual(result["graph"]["nodes"], [])
        self.assertEqual(result["warnings"], ["database_snapshot_unavailable"])
        self.assertEqual(path.read_bytes(), before)

    def test_absent_positive_npmi_is_null_not_a_zero_measurement(self):
        self.database()
        self.source("ab", "alpha beta", ["alpha", "beta"], path="ab.md")
        self.source("ac", "alpha gamma", ["alpha", "gamma"], path="ac.md")
        self.source("bc", "beta gamma", ["beta", "gamma"], path="bc.md")
        edge = observer_data.edge_details(self.config, "alpha", "beta")["edge"]
        self.assertIsNone(edge["static_weight"])
        self.assertIsNone(edge["dynamic_weight"])
        self.assertEqual(edge["effective_weight"], 0.0)
        self.assertTrue(edge["active_source_support"])
        self.graph.retrieve("10:knowledge", [], "alpha beta", 1, event_id="event")
        edge = observer_data.edge_details(self.config, "alpha", "beta")["edge"]
        self.assertIsNone(edge["static_weight"])
        self.assertEqual(edge["dynamic_weight"], 1.0)
        self.assertEqual(edge["effective_weight"], 1.0)

    def test_invalid_calendar_log_names_are_not_read(self):
        self.config.scratch_dir.mkdir()
        for name in ("2026-99-99.jsonl", "2026-02-30.jsonl", "0000-01-01.jsonl"):
            (self.config.scratch_dir / name).write_text("synthetic unmanaged private", encoding="utf-8")
        with patch.object(observer_data.scratch, "read_snapshot") as reader:
            result = observer_data.snapshot(self.config)
        reader.assert_not_called()
        self.assertEqual(result["scratch"]["files"], [])
        self.assertNotIn("synthetic unmanaged private", json.dumps(result))

    def test_actual_raw_bytes_enforce_aggregate_budget(self):
        path = self.log([(NOW, "example", {"trace_id": "one"})])
        line = path.read_text(encoding="utf-8").rstrip("\n")
        # Whitespace is valid JSON and leaves the canonical hash unchanged.
        padded = (line + " " * 1024 + "\n").encode("utf-8")
        first = path.parent / "2026-10-02.jsonl"
        second = path.parent / "2026-10-01.jsonl"
        path.unlink()
        first.write_bytes(padded)
        second.write_bytes(padded)
        self.assertEqual(read_snapshot(first)["bytes_read"], len(padded))
        with patch.object(observer_data, "MAX_TOTAL_SCRATCH_BYTES", len(padded) + 1):
            result = observer_data.snapshot(self.config)
        files = result["scratch"]["files"]
        self.assertEqual(files[0]["retained_records"], 1)
        self.assertEqual(len(files), 1)
        self.assertTrue(result["scratch"]["truncated"])
        self.assertIn("scratch_aggregate_read_limit", result["warnings"])

    def test_pending_cleanup_checkpoint_has_explicit_warning(self):
        clock = Clock(NOW - timedelta(hours=25))
        log = ScratchLog(self.config.scratch_dir, clock=clock)
        log.write("turn_start", trace_id="one")
        clock.value = NOW
        log.write("turn_end", trace_id="one", status="delivered")
        log.prune()
        path = log.path
        log.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        header["cleanup_pending"] = True
        header["hash"] = hashlib.sha256(canonical({key: value for key, value in header.items() if key != "hash"})).hexdigest()
        path.write_bytes(canonical(header) + b"\n" + "\n".join(lines[1:]).encode("utf-8") + b"\n")
        result = observer_data.snapshot(self.config)
        self.assertIn(f"scratch_cleanup_pending:{path.name}", result["warnings"])
        detail = observer_data.trace_details(self.config, "one")
        self.assertIn(f"scratch_cleanup_pending:{path.name}", detail["verification"]["warnings"])

    def test_failed_file_attempts_cannot_exceed_aggregate_read_budget(self):
        self.config.scratch_dir.mkdir()
        for name in ("2026-10-02.jsonl", "2026-10-01.jsonl", "2026-09-30.jsonl"):
            (self.config.scratch_dir / name).write_bytes(b"synthetic corrupt content")
        allocations = []
        def failed_read(path, *, max_bytes):
            allocations.append(max_bytes + 1)
            raise ValueError("synthetic invalid frozen data")
        with patch.object(observer_data, "MAX_TOTAL_SCRATCH_BYTES", 20), \
                patch.object(observer_data, "MAX_SCRATCH_BYTES", 8), \
                patch.object(observer_data.scratch, "read_snapshot", side_effect=failed_read):
            result = observer_data.snapshot(self.config)
        self.assertLessEqual(sum(allocations), 20)
        self.assertEqual(allocations, [9, 9, 2])
        self.assertTrue(result["scratch"]["truncated"])
        self.assertIn("scratch_aggregate_read_limit", result["warnings"])

    def test_junction_checks_refuse_state_and_database_before_sqlite_connect(self):
        original = Path.is_junction
        for label in ("state", "database"):
            def is_junction(candidate, kind=label):
                expected = self.config.state_dir if kind == "state" else self.config.state_dir / "memory.sqlite3"
                return candidate == expected or original(candidate)
            with self.subTest(kind=label), patch.object(Path, "is_junction", autospec=True, side_effect=is_junction), \
                    patch.object(observer_data.sqlite3, "connect") as connect:
                result = observer_data.snapshot(self.config)
            connect.assert_not_called()
            self.assertIn("database_snapshot_unavailable", result["warnings"])

    def test_database_resolved_parent_escape_is_refused(self):
        original = Path.resolve
        def resolve(candidate, *args, **kwargs):
            if candidate == self.config.state_dir / "memory.sqlite3":
                return self.root / "escape" / "memory.sqlite3"
            return original(candidate, *args, **kwargs)
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve), \
                patch.object(observer_data.sqlite3, "connect") as connect:
            result = observer_data.snapshot(self.config)
        connect.assert_not_called()
        self.assertIn("database_snapshot_unavailable", result["warnings"])

    def test_real_state_directory_symlink_is_refused_without_following(self):
        target = self.root / "synthetic-outside-state"
        target.mkdir()
        try:
            self.config.state_dir.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable on this host")
        with patch.object(observer_data.sqlite3, "connect") as connect:
            result = observer_data.snapshot(self.config)
        connect.assert_not_called()
        self.assertIn("database_snapshot_unavailable", result["warnings"])

    def test_real_database_symlink_is_refused_without_following(self):
        self.config.state_dir.mkdir()
        target = self.root / "synthetic-outside-database"
        target.write_bytes(b"synthetic non-database")
        try:
            (self.config.state_dir / "memory.sqlite3").symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable on this host")
        with patch.object(observer_data.sqlite3, "connect") as connect:
            result = observer_data.snapshot(self.config)
        connect.assert_not_called()
        self.assertIn("database_snapshot_unavailable", result["warnings"])
        self.assertEqual(target.read_bytes(), b"synthetic non-database")

    def test_uninitialized_database_does_not_migrate(self):
        self.config.state_dir.mkdir()
        path = self.config.state_dir / "memory.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE synthetic(value TEXT)")
        db.commit()
        db.close()
        before = path.read_bytes()
        self.assertEqual(observer_data.snapshot(self.config)["graph"]["nodes"], [])
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
