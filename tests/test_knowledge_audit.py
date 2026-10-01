from dataclasses import replace
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.config import DiscordConfig, KnowledgeConfig, PdfConfig
from indeces.knowledge_audit import audit_snapshot, render_audit


class KnowledgeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(knowledge_dir=self.root / "knowledge", state_dir=self.root / "state",
            discord=DiscordConfig("10"), knowledge=KnowledgeConfig(max_file_bytes=32), pdf=PdfConfig())
        self.config.knowledge_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, data):
        path = self.config.knowledge_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def db(self):
        self.config.state_dir.mkdir()
        db = sqlite3.connect(self.config.state_dir / "memory.sqlite3")
        db.executescript("""
            CREATE TABLE knowledge_versions(source_id,path,digest,status,error,scope,input_tokens,output_tokens,elapsed_seconds);
            CREATE TABLE knowledge_chunks(source_id,marks_json);
            CREATE TABLE knowledge_desired(scope,path,source_id);
            CREATE TABLE knowledge_published(scope,path,source_id);
        """)
        return db

    def test_complete_format_inventory_boundary_and_exclusions(self):
        self.file("edge.md", b"a" * 32)
        self.file("large.txt", b"a" * 33)
        self.file("utf8.markdown", b"\xff")
        self.file("empty.pdf", b"")
        self.file("input.docx", b"ignore")
        self.file("_staging/paper.pdf", b"draft")
        self.file(".indeces/README.md", b"managed")
        result = audit_snapshot(self.config)
        rows = {r["path"]: r for r in result["files"]}
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows["edge.md"]["state"], "unseen")
        self.assertEqual(rows["large.txt"]["validation_error"], "knowledge_file_size_limit")
        self.assertEqual(rows["large.txt"]["limit_bytes"], 32)
        self.assertEqual(rows["utf8.markdown"]["validation_error"], "knowledge_invalid_utf8")
        self.assertEqual(rows["empty.pdf"]["validation_error"], "knowledge_pdf_empty")
        for name in ("input.docx", "_staging/paper.pdf", ".indeces/README.md"):
            self.assertEqual(rows[name]["state"], "skipped")
        self.assertFalse(self.config.state_dir.exists())

    def test_saved_failure_retains_stage_and_partial_progress_without_writes(self):
        self.file("paper.md", b"old content")
        with closing(self.db()) as db, db:
            db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?)",
                ("v", "paper.md", hashlib.sha256(b"old content").hexdigest(), "failed", "invalid_labels", "10:knowledge", 8, 4, 2.0))
            db.execute("INSERT INTO knowledge_desired VALUES('10:knowledge','paper.md','v')")
            db.executemany("INSERT INTO knowledge_chunks VALUES('v',?)", [('[]',), (None,)])
        path = self.config.state_dir / "memory.sqlite3"
        before = path.read_bytes()
        row = audit_snapshot(self.config)["files"][0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["stage"], "labelling")
        self.assertEqual(row["desired"]["labelled_chunks"], 1)
        self.assertEqual(row["desired"]["total_chunks"], 2)
        self.assertEqual(before, path.read_bytes())
        self.file("paper.md", b"changed")
        row = audit_snapshot(self.config)["files"][0]
        self.assertEqual(row["state"], "changed")
        self.assertEqual(row["stored_error"], "invalid_labels")

    def test_no_progress_display_cap_and_global_resource_bound_explicit(self):
        self.config.knowledge = replace(self.config.knowledge, max_files=300)
        for index in range(270):
            self.file(f"{index:03}.md", b"x")
        report = audit_snapshot(self.config)
        self.assertEqual(report["total_paths"], 270)
        self.assertFalse(report["truncated"])
        self.assertEqual(report["states"], {"unseen": 270})
        self.config.knowledge = replace(self.config.knowledge, max_files=256)
        report = audit_snapshot(self.config)
        self.assertEqual(report["states"], {"rejected": 270})
        self.assertIn("knowledge_file_count_limit", report["warnings"])

    def test_render_escapes_terminal_controls(self):
        self.file("a.md", b"a")
        report = audit_snapshot(self.config)
        report["files"][0]["path"] = "\x1b[31mx\n"
        report["files"][0]["suffix"] = ".pdf\x1b[2J"
        report["files"][0]["stage"] = "\x1b[3J"
        report["files"][0]["state"] = "\x1b[4J"
        text = render_audit(report)
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u001b[31mx\\n", text)

    def test_pointer_path_mismatch_never_declares_current_or_retrievable(self):
        self.file("paper.md", b"content")
        with closing(self.db()) as db, db:
            db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?)",
                ("v", "other.md", hashlib.sha256(b"content").hexdigest(), "ready", None, "10:knowledge", 0, 0, 0))
            db.execute("INSERT INTO knowledge_desired VALUES('10:knowledge','paper.md','v')")
            db.execute("INSERT INTO knowledge_published VALUES('10:knowledge','paper.md','v')")
        row = audit_snapshot(self.config)["files"][0]
        self.assertFalse(row["published_retrievable"])
        self.assertFalse(row["current_file_matches_desired"])
        self.assertEqual(set(row["pointer_warnings"]), {"desired_pointer_invalid", "published_pointer_invalid"})

    def test_last_saved_rejection_is_explicit_and_separate_from_current_read(self):
        self.file("paper.md", b"valid")
        with closing(self.db()) as db, db:
            db.execute("CREATE TABLE knowledge_file_audit(scope,path,source_id,suffix,byte_count,state,stage,error,details_json,observed_at,remote_usage_unknown)")
            db.execute("INSERT INTO knowledge_file_audit VALUES('10:knowledge','paper.md',NULL,'.md',40,'rejected','validation','knowledge_file_size_limit','{}',0,NULL)")
        report = audit_snapshot(self.config)
        self.assertEqual(report["files"][0]["state"], "unseen")
        self.assertIn("last_observation=rejected/validation/knowledge_file_size_limit", render_audit(report))


if __name__ == "__main__":
    unittest.main()
