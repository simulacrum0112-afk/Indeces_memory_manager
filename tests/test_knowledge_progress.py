"""Synthetic metadata fixtures only: no model, raw source or live runtime state."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import knowledge_progress as progress


SCHEMA = """
CREATE TABLE knowledge_versions (
 source_id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT NOT NULL,
 raw_text TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
 error TEXT, scope TEXT NOT NULL);
CREATE TABLE knowledge_chunks (
 source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, text TEXT NOT NULL,
 start_character INTEGER NOT NULL, marks_json TEXT,
 PRIMARY KEY(source_id,chunk_index));
CREATE TABLE knowledge_desired (
 scope TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT NOT NULL,
 PRIMARY KEY(scope,path));
CREATE TABLE knowledge_published (
 scope TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT NOT NULL,
 PRIMARY KEY(scope,path));
CREATE TABLE knowledge_pdf_versions (source_id TEXT PRIMARY KEY, pdf_bytes BLOB);
CREATE TABLE memory_records (scope TEXT, text TEXT);
"""


class KnowledgeProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(
            state_dir=self.root / "state", knowledge_dir=self.root / "knowledge",
            scratch_dir=self.root / "scratch", discord=SimpleNamespace(guild_id="10"))
        self.db = None

    def tearDown(self):
        if self.db is not None:
            self.db.close()
        self.temp.cleanup()

    def database(self):
        self.config.state_dir.mkdir()
        self.db = sqlite3.connect(self.config.state_dir / "memory.sqlite3")
        self.db.executescript(SCHEMA)
        return self.db

    def source(self, source_id, *, path="sample.pdf", scope="10:knowledge",
               status="labelling", labels=(None, None, None), error=None,
               desired=True, published=False):
        self.db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?)",
                        (source_id, path, "a" * 64, "RAW_TEXT_MUST_NOT_BE_READ",
                         status, 1.0, error, scope))
        self.db.executemany("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)",
                            [(source_id, i, "CHUNK_TEXT_MUST_NOT_BE_READ", i * 400, marks)
                             for i, marks in enumerate(labels)])
        if desired:
            self.db.execute("INSERT OR REPLACE INTO knowledge_desired VALUES(?,?,?)",
                            (scope, path, source_id))
        if published:
            self.db.execute("INSERT OR REPLACE INTO knowledge_published VALUES(?,?,?)",
                            (scope, path, source_id))
        self.db.commit()

    def once(self):
        output = io.StringIO()
        with redirect_stdout(output):
            progress.show_knowledge_progress(self.config, watch=False)
        return output.getvalue()

    def test_missing_database_creates_no_directories_or_files(self):
        self.assertEqual(progress.progress_snapshot(self.config)["status"], "database_missing")
        self.assertIn("不创建数据库", self.once())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unconfigured_guild_never_opens_database(self):
        with patch.object(progress, "_database") as read:
            for value in (None, "", "   "):
                with self.subTest(value=value):
                    self.config.discord.guild_id = value
                    result = progress.progress_snapshot(self.config)
                    self.assertEqual(result["status"], "scope_unconfigured")
                    self.assertIsNone(result["scope"])
                    self.assertIn("未打开数据库", self.once())
            read.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_readonly_metadata_does_not_select_private_columns_or_modify_database(self):
        self.database()
        self.source("one", labels=("MARK_CONTENT_MUST_NOT_BE_READ", None))
        self.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?)",
                        ("one", b"PDF_BYTES_MUST_NOT_BE_READ"))
        self.db.execute("INSERT INTO memory_records VALUES(?,?)",
                        ("10:knowledge", "GRAPH_MUST_NOT_BE_READ"))
        self.db.commit()
        before_dump = "\n".join(self.db.iterdump())
        before_files = {item.name: item.read_bytes() for item in self.config.state_dir.iterdir()}
        real_database = progress._database
        reads = []

        @contextmanager
        def guarded(config):
            with real_database(config) as db:
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ:
                        reads.append((table, column))
                        if table in {"memory_records", "knowledge_pdf_versions"}:
                            return sqlite3.SQLITE_DENY
                        if (table, column) in {("knowledge_versions", "raw_text"),
                                               ("knowledge_chunks", "text")}:
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield db

        with patch.object(progress, "_database", guarded):
            result = progress.progress_snapshot(self.config)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"][0]["desired"]["labelled_chunks"], 1)
        self.assertTrue(any(table == "knowledge_chunks" for table, _ in reads))
        for sentinel in ("RAW_TEXT_MUST_NOT_BE_READ", "CHUNK_TEXT_MUST_NOT_BE_READ",
                         "MARK_CONTENT_MUST_NOT_BE_READ", "PDF_BYTES_MUST_NOT_BE_READ",
                         "GRAPH_MUST_NOT_BE_READ"):
            self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual("\n".join(self.db.iterdump()), before_dump)
        self.assertEqual({item.name: item.read_bytes() for item in self.config.state_dir.iterdir()},
                         before_files)
        self.assertFalse(self.config.scratch_dir.exists())
        self.assertFalse(self.config.knowledge_dir.exists())

    def test_next_snapshot_counts_committed_chunk_updates_without_decoding_marks(self):
        self.database()
        self.source("one", labels=(None, "[]", None))
        first = progress.progress_snapshot(self.config)["files"][0]["desired"]
        self.assertEqual((first["labelled_chunks"], first["total_chunks"]), (1, 3))
        self.db.execute("UPDATE knowledge_chunks SET marks_json='not-json' WHERE chunk_index=2")
        self.db.commit()
        second = progress.progress_snapshot(self.config)["files"][0]["desired"]
        self.assertEqual((second["labelled_chunks"], second["total_chunks"]), (2, 3))
        self.assertNotIn("not-json", json.dumps(second))

    def test_failed_replacement_keeps_separate_previous_published_version(self):
        self.database()
        self.source("old", status="ready", labels=("[]", "[]"), published=True)
        self.source("new", status="failed", labels=("[]", None, None),
                    error="knowledge_version_token_limit")
        item = progress.progress_snapshot(self.config)["files"][0]
        self.assertEqual(item["desired"]["source_id"], "new")
        self.assertEqual(item["published"]["source_id"], "old")
        self.assertEqual(item["state"], "failed")
        self.assertFalse(item["complete"])
        self.assertTrue(item["published_retrievable"])
        output = self.once()
        self.assertIn("最新版本：new", output)
        self.assertIn("可检索发布版本：old", output)
        self.assertIn("标词块=1/3", output)
        self.assertIn("knowledge_version_token_limit", output)

    def test_ready_is_complete_only_with_consistent_ready_published_pointer(self):
        self.database()
        self.source("old", status="ready", published=True, labels=("[]",))
        self.source("new", status="ready", labels=("[]",))
        item = progress.progress_snapshot(self.config)["files"][0]
        self.assertFalse(item["complete"])
        self.assertEqual(item["state"], "ready_unpublished")
        self.db.execute("UPDATE knowledge_published SET source_id='new'")
        self.db.commit()
        self.assertTrue(progress.progress_snapshot(self.config)["files"][0]["complete"])
        self.db.execute("UPDATE knowledge_versions SET status='labelling' WHERE source_id='new'")
        self.db.commit()
        item = progress.progress_snapshot(self.config)["files"][0]
        self.assertFalse(item["complete"])
        self.assertFalse(item["published_retrievable"])
        self.assertIn("published_not_ready", item["warnings"])

    def test_scope_and_path_validation_hide_foreign_or_misdirected_versions(self):
        self.database()
        self.source("local", status="pending", path="local.pdf")
        self.source("FOREIGN_PRIVATE_ID", path="PRIVATE_FOREIGN_PATH.pdf",
                    scope="99:knowledge", status="ready", published=True)
        self.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?)",
                        ("10:knowledge", "broken.pdf", "FOREIGN_PRIVATE_ID"))
        self.db.execute("INSERT INTO knowledge_published VALUES(?,?,?)",
                        ("10:knowledge", "broken.pdf", "FOREIGN_PRIVATE_ID"))
        self.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?)",
                        ("10:knowledge", "wrong-path.pdf", "local"))
        self.db.commit()
        result = progress.progress_snapshot(self.config)
        self.assertEqual(result["total_files"], 3)
        self.assertNotIn("FOREIGN_PRIVATE", json.dumps(result))
        items = {item["path"]: item for item in result["files"]}
        self.assertIsNone(items["broken.pdf"]["desired"])
        self.assertIsNone(items["broken.pdf"]["published"])
        self.assertEqual(items["broken.pdf"]["state"], "pointer_invalid")
        self.assertIsNone(items["wrong-path.pdf"]["desired"])
        self.assertFalse(items["broken.pdf"]["published_retrievable"])
        self.assertIn("version_pointer_inconsistency", result["warnings"])

    def test_old_schema_has_fixed_message_and_is_not_migrated(self):
        self.config.state_dir.mkdir()
        self.db = sqlite3.connect(self.config.state_dir / "memory.sqlite3")
        self.db.execute("CREATE TABLE knowledge_versions(source_id TEXT, raw_text TEXT)")
        self.db.commit()
        before = "\n".join(self.db.iterdump())
        result = progress.progress_snapshot(self.config)
        self.assertEqual(result["status"], "schema_unavailable")
        self.assertIn("未执行迁移", self.once())
        self.assertEqual("\n".join(self.db.iterdump()), before)

    def test_old_columns_are_detected_even_when_all_table_names_exist(self):
        self.database()
        self.db.execute("ALTER TABLE knowledge_versions RENAME COLUMN scope TO old_scope")
        self.db.commit()
        self.assertEqual(progress.progress_snapshot(self.config)["status"], "schema_unavailable")

    def test_read_errors_return_fixed_message_without_exception_details(self):
        @contextmanager
        def unreadable(config):
            raise OSError("PRIVATE_PATH_AND_TOKEN_MUST_NOT_LEAK")
            yield None

        with patch.object(progress, "_database", unreadable):
            result = progress.progress_snapshot(self.config)
            output = self.once()
        self.assertEqual(result["status"], "snapshot_unavailable")
        self.assertIn("稍后刷新", output)
        self.assertNotIn("PRIVATE_PATH", json.dumps(result) + output)

    def test_current_paths_only_are_counted_and_file_limit_is_explicit(self):
        self.database()
        for i in range(260):
            self.source(f"id-{i:03}", path=f"file-{i:03}.pdf", status="pending", labels=())
        self.source("historical", path="old-only.pdf", status="superseded", desired=False, labels=())
        result = progress.progress_snapshot(self.config)
        self.assertEqual(result["total_files"], 260)
        self.assertEqual(result["displayed_files"], 256)
        self.assertEqual(result["file_limit"], 256)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["files"][0]["path"], "file-000.pdf")
        self.assertEqual(result["files"][-1]["path"], "file-255.pdf")
        output = self.once()
        self.assertIn("另有 4 个路径未显示", output)
        self.assertIn("显示范围状态：待标词=256", output)
        self.assertNotIn("old-only", output)

    def test_conversion_states_do_not_claim_publication_or_running_service(self):
        self.database()
        for i, state in enumerate(("conversion_pending", "converting", "pending", "labelling", "failed")):
            self.source(str(i), path=f"{i}.pdf", status=state, labels=())
        output = self.once()
        self.assertIn("待转换/转换中", output)
        self.assertIn("停服后仍显示最近保存的状态", output)
        self.assertIn("待标词", output)
        self.assertNotIn("已发布完成", output)
        self.assertNotIn("%", output)
        self.assertNotIn("页进度", output)

    def test_terminal_escapes_path_scope_errors_and_unicode_controls(self):
        self.database()
        unsafe = "bad\x1b[2J\r\n\x9b31m\u202e\u2028\ud800.pdf"
        # SQLite UTF-8 refuses isolated surrogates, so test those separately.
        safe_sql = unsafe.replace("\ud800", "")
        self.source("id\x1b[31m", path=safe_sql, status="failed",
                    error="failure\t\x07\u2066")
        output = self.once()
        for control in ("\x1b", "\r", "\x9b", "\x07", "\u202e", "\u2028", "\u2066"):
            self.assertNotIn(control, output)
        self.assertIn("\\u001b[2J\\r\\n", output)
        self.assertIn("failure\\t\\u0007", output)
        self.assertEqual(progress._terminal_text("\ud800"), "\\ud800")

    def test_oversized_metadata_is_bounded_and_path_truncation_is_stated(self):
        self.database()
        self.source("i" * 300, path="p" * 2200, error="e" * 10000, status="failed")
        item = progress.progress_snapshot(self.config)["files"][0]
        self.assertEqual(len(item["path"]), 2048)
        self.assertTrue(item["path_truncated"])
        self.assertEqual(len(item["desired"]["source_id"]), 256)
        self.assertEqual(len(item["desired"]["error"]), 256)
        self.assertIn("[路径已截断]", self.once())

    def test_one_snapshot_is_consistent_during_concurrent_wal_commit(self):
        self.database()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.source("old", status="ready", labels=("[]",), published=True)
        self.source("new", status="pending", labels=(None,), desired=False)
        real_tables = progress._tables
        committed = False

        def commit_after_first_read(db):
            nonlocal committed
            names = real_tables(db)
            if not committed:
                self.db.execute("UPDATE knowledge_desired SET source_id='new'")
                self.db.execute("UPDATE knowledge_chunks SET marks_json='[]' WHERE source_id='new'")
                self.db.commit()
                committed = True
            return names

        with patch.object(progress, "_tables", commit_after_first_read):
            result = progress.progress_snapshot(self.config)
        item = result["files"][0]
        self.assertEqual(item["desired"]["source_id"], "old")
        self.assertTrue(item["complete"])
        next_item = progress.progress_snapshot(self.config)["files"][0]
        self.assertEqual(next_item["desired"]["source_id"], "new")
        self.assertEqual(next_item["desired"]["labelled_chunks"], 1)
        self.assertFalse(next_item["complete"])

    def test_watch_prints_changes_only_closes_database_before_sleep_and_exits_on_ctrl_c(self):
        self.database()
        self.source("one", labels=(None,))
        real_database = progress._database
        closed = []
        sleeps = 0

        @contextmanager
        def tracked(config):
            with real_database(config) as db:
                yield db
            closed.append(db)

        def sleep(seconds):
            nonlocal sleeps
            self.assertEqual(seconds, 2.0)
            self.assertEqual(len(closed), sleeps + 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                closed[-1].execute("SELECT 1")
            sleeps += 1
            if sleeps == 1:
                self.db.execute("UPDATE knowledge_chunks SET marks_json='[]'")
                self.db.commit()
            if sleeps == 3:
                raise KeyboardInterrupt()

        output = io.StringIO()
        with patch.object(progress, "_database", tracked), patch.object(progress.time, "sleep", sleep):
            with redirect_stdout(output):
                progress.show_knowledge_progress(self.config)
        self.assertEqual(output.getvalue().count("知识库当前版本状态（只读）"), 2)
        self.assertIn("标词块=0/1", output.getvalue())
        self.assertIn("标词块=1/1", output.getvalue())
        self.assertIn("未向服务发送停止请求", output.getvalue())
        self.assertEqual(sleeps, 3)

    def test_invalid_refresh_interval_is_rejected_before_database_read(self):
        with patch.object(progress, "progress_snapshot") as read:
            for value in (0, -1, float("nan"), float("inf"), True, "2"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    progress.show_knowledge_progress(self.config, refresh_seconds=value)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
