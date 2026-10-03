"""Synthetic maintenance metadata; never uses real documents or model calls."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import DiscordConfig, KnowledgeConfig, PdfConfig
from indeces.knowledge import KnowledgeService
from indeces.knowledge_audit import audit_snapshot, render_audit
from indeces import knowledge_progress as progress


SCHEMA = """
CREATE TABLE knowledge_versions(source_id TEXT PRIMARY KEY,path,digest,raw_text,status,
 created_at,input_tokens,output_tokens,elapsed_seconds,error,scope);
CREATE TABLE knowledge_chunks(source_id,chunk_index,text,start_character,marks_json);
CREATE TABLE knowledge_desired(scope,path,source_id);
CREATE TABLE knowledge_published(scope,path,source_id);
CREATE TABLE knowledge_file_audit(scope,path,source_id,suffix,byte_count,state,stage,
 error,details_json,observed_at,remote_usage_unknown,PRIMARY KEY(scope,path));
CREATE TABLE knowledge_root_quarantine(scope,path,digest,source_id,reason,
 PRIMARY KEY(scope,path,digest));
CREATE TABLE reingest_batches(batch_id TEXT PRIMARY KEY,scope,status,pilot_passed);
CREATE TABLE reingest_documents(attempt_id TEXT PRIMARY KEY,batch_id,scope,path,digest,
 old_source_id,new_source_id,total_chunks,published);
CREATE TABLE reingest_labels(attempt_id,chunk_index,marks_json,call_key);
CREATE TABLE reingest_calls(call_key,attempt_id,chunk_index,status,phase,elapsed_seconds,
 actual_input_tokens,actual_output_tokens,reserved_input_tokens,reserved_output_tokens);
CREATE TABLE reingest_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,batch_id,attempt_id,
 call_key,event,recorded_at,details_json);
"""


class ReingestViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(state_dir=self.root / "state", knowledge_dir=self.root / "knowledge",
            discord=DiscordConfig("10"), knowledge=KnowledgeConfig(), pdf=PdfConfig())
        self.config.state_dir.mkdir()
        self.config.knowledge_dir.mkdir()
        self.path = self.config.knowledge_dir / "sample.pdf"
        self.path.write_bytes(b"synthetic-pdf-digest-fixture")
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.db = sqlite3.connect(self.config.state_dir / "memory.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.service = KnowledgeService.__new__(KnowledgeService)
        self.service.scope = "10:knowledge"
        self.service.root = self.config.knowledge_dir
        self.service.store = SimpleNamespace(db=self.db)
        self.service._errors = {}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fixture(self, *, published=True, scope="10:knowledge", pilot=True):
        for source, status, error, count in (("old", "failed", "stage_timeout", 7),
                                             ("new", "ready", None, 10)):
            if source == "new" and not published:
                continue
            self.db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (source, "sample.pdf", self.digest, "RAW_SENTINEL", status, 1, 50, 10, 2, error, scope))
            self.db.executemany("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)",
                [(source, index, "CHUNK_SENTINEL", index * 400, '["MARK_SENTINEL"]' if index < count else None)
                 for index in range(10)])
        current = "new" if published else "old"
        self.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?)", (scope, "sample.pdf", current))
        if published:
            self.db.execute("INSERT INTO knowledge_published VALUES(?,?,?)", (scope, "sample.pdf", "new"))
        self.db.execute("INSERT INTO knowledge_file_audit VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (scope, "sample.pdf", "old", ".pdf", 28, "failed", "label", "stage_timeout", '{"checkpoint":7}', 2, 1))
        self.db.execute("INSERT INTO knowledge_root_quarantine VALUES(?,?,?,?,?)",
            (scope, "sample.pdf", self.digest, "old", "stage_timeout"))
        self.db.execute("INSERT INTO reingest_batches VALUES(?,?,?,?)",
            ("batch", scope, "complete" if published else "pilot", int(pilot)))
        self.db.execute("INSERT INTO reingest_documents VALUES(?,?,?,?,?,?,?,?,?)",
            ("attempt", "batch", scope, "sample.pdf", self.digest, "old", "new", 10, int(published)))
        self.db.executemany("INSERT INTO reingest_labels VALUES(?,?,?,?)",
            [("attempt", index, '["NEW_MARK_SENTINEL"]', "call") for index in range(10 if published else 3)])
        self.db.executemany("INSERT INTO reingest_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
            [("call1", "attempt", 0, "success", "generation", 1.25, 30, 8, 4096, 512),
             ("call2", "attempt", 1, "success", "generation", 1.5, 40, 9, 4096, 512)])
        self.db.commit()

    def old_rows(self):
        return tuple(tuple(row) for row in self.db.execute("SELECT * FROM knowledge_file_audit")), \
            tuple(self.db.execute("SELECT * FROM knowledge_versions WHERE source_id='old'").fetchone()), \
            tuple(tuple(row) for row in self.db.execute("SELECT * FROM knowledge_chunks WHERE source_id='old'")), \
            tuple(tuple(row) for row in self.db.execute("SELECT * FROM knowledge_root_quarantine"))

    def test_unchanged_watcher_preserves_old_failure_and_unknown_quarantine(self):
        self.fixture()
        old = self.old_rows()
        self.service._apply_snapshot(self.path, (self.path.read_bytes(), self.digest))
        self.service._apply_snapshot(self.path, (self.path.read_bytes(), self.digest))
        self.assertEqual(old, self.old_rows())
        self.assertTrue(self.service._certified_reingest_publication("sample.pdf", self.digest, "new"))
        self.assertFalse(self.service._certified_reingest_publication("sample.pdf", self.digest, "old"))
        self.assertFalse(self.service._certified_reingest_publication("different.pdf", self.digest, "new"))

    def test_publication_certificate_requires_pilot_and_both_exact_pointers(self):
        self.fixture(pilot=False)
        self.assertFalse(self.service._certified_reingest_publication("sample.pdf", self.digest, "new"))
        self.db.execute("UPDATE reingest_batches SET pilot_passed=1")
        self.db.execute("UPDATE knowledge_published SET source_id='old'")
        self.assertFalse(self.service._certified_reingest_publication("sample.pdf", self.digest, "new"))
        self.db.commit()
        maintenance = progress.progress_snapshot(self.config)["files"][0]["reingest"]
        self.assertFalse(maintenance["current_publication"])

    def test_ready_views_separate_current_publication_and_preserved_old_failure(self):
        self.fixture()
        before = "\n".join(self.db.iterdump())
        snapshot = progress.progress_snapshot(self.config)
        row = snapshot["files"][0]
        self.assertTrue(row["complete"])
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["audit"]["error"], "stage_timeout")
        self.assertTrue(row["reingest"]["current_publication"])
        self.assertEqual(row["reingest"]["actual_input_tokens"], 70)
        self.assertEqual(row["reingest"]["actual_output_tokens"], 17)
        self.assertEqual(row["reingest"]["preserved_failure"]["labelled_chunks"], 7)
        self.assertEqual(row["reingest"]["preserved_failure"]["remote_usage_unknown"], 1)
        report = audit_snapshot(self.config)
        audited = report["files"][0]
        self.assertEqual((audited["state"], audited["stage"]), ("ready", "published"))
        self.assertIsNone(audited["stored_error"])
        self.assertEqual(audited["last_observation"]["source_id"], "old")
        self.assertEqual(audited["last_observation"]["error"], "stage_timeout")
        self.assertIn("preserved_old_failure=failed/stage_timeout", render_audit(report))
        self.assertIn("保留旧失败", progress._render(snapshot))
        self.assertEqual(before, "\n".join(self.db.iterdump()))

    def test_pending_progress_uses_independent_counts_without_clearing_old_failure(self):
        self.fixture(published=False)
        self.db.execute("INSERT INTO reingest_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("unknown", "attempt", 3, "usage_unknown", "generation", 45, None, None, 4096, 512))
        self.db.commit()
        row = progress.progress_snapshot(self.config)["files"][0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["desired"]["labelled_chunks"], 7)
        self.assertEqual(row["reingest"]["labelled_chunks"], 3)
        self.assertEqual(row["reingest"]["actual_input_tokens"], 70)
        self.assertEqual(row["reingest"]["usage_unknown_calls"], 1)
        self.assertEqual(row["reingest"]["elapsed_seconds"], 47.75)
        self.assertFalse(row["reingest"]["current_publication"])

    def test_other_scope_maintenance_never_appears(self):
        self.fixture(scope="20:knowledge")
        snapshot = progress.progress_snapshot(self.config)
        self.assertEqual(snapshot["files"], [])
        report = audit_snapshot(self.config)
        self.assertNotIn("reingest", report["files"][0])

    def test_later_failure_audit_is_append_only_visible_and_does_not_inherit_old_unknown(self):
        self.fixture()
        before = self.old_rows()
        self.service._audit_file(self.path, source_id="new", state="failed", stage="publication",
            error="knowledge_publication_error", details={"failed_chunk_index": 2}, remote_usage_unknown=False)
        self.service._audit_file(self.path, source_id="new", state="failed", stage="publication",
            error="knowledge_publication_error", details={"failed_chunk_index": 2})
        self.assertEqual(before, self.old_rows())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reingest_events").fetchone()[0], 1)
        current = progress.progress_snapshot(self.config)["files"][0]
        self.assertEqual(current["audit"]["error"], "knowledge_publication_error")
        self.assertEqual(current["audit"]["remote_usage_unknown"], 0)
        self.assertEqual(current["preserved_audit"]["error"], "stage_timeout")
        self.assertEqual(current["reingest"]["preserved_failure"]["remote_usage_unknown"], 1)
        report = audit_snapshot(self.config)
        self.assertEqual(report["files"][0]["current_observation"]["error"], "knowledge_publication_error")
        self.assertEqual(report["files"][0]["last_observation"]["error"], "stage_timeout")
        self.assertIn("current_observation=failed/publication/knowledge_publication_error", render_audit(report))

    def test_new_source_audit_cannot_inherit_previous_source_unknown_flag(self):
        self.fixture()
        self.service._audit_file(self.path, source_id="new", state="failed", stage="label",
            error="stage_timeout", remote_usage_unknown=True)
        self.service._audit_file(self.path, source_id="replacement", state="pending", stage="label")
        audit = progress.progress_snapshot(self.config)["files"][0]["audit"]
        self.assertEqual(audit["source_id"], "replacement")
        self.assertIsNone(audit["remote_usage_unknown"])

    def test_successor_unknown_failure_retains_exact_digest_retry_prohibition(self):
        self.fixture()
        self.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id='new'", ("b" * 64,))
        self.db.commit()
        self.service._audit_file(self.path, source_id="new", state="failed", stage="label",
            error="stage_timeout", remote_usage_unknown=True)
        quarantine = self.db.execute("SELECT source_id,reason FROM knowledge_root_quarantine WHERE digest=?", ("b" * 64,)).fetchone()
        self.assertEqual(tuple(quarantine), ("new", "stage_timeout"))
        original = self.db.execute("SELECT source_id,remote_usage_unknown FROM knowledge_file_audit").fetchone()
        self.assertEqual(tuple(original), ("old", 1))

    def test_progress_selects_metadata_without_reading_source_or_label_contents(self):
        self.fixture()
        real = progress._database
        @contextmanager
        def guarded(config):
            with real(config) as db:
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ and (table, column) in {
                        ("knowledge_versions", "raw_text"), ("knowledge_chunks", "text"),
                        ("reingest_labels", "marks_json")}:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield db
        with patch.object(progress, "_database", guarded):
            snapshot = progress.progress_snapshot(self.config)
        self.assertEqual(snapshot["status"], "ok")
        rendered = json.dumps(snapshot)
        for sentinel in ("RAW_SENTINEL", "CHUNK_SENTINEL", "MARK_SENTINEL", "NEW_MARK_SENTINEL"):
            self.assertNotIn(sentinel, rendered)


if __name__ == "__main__":
    unittest.main()
