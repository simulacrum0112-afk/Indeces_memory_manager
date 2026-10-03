"""Controlled ingestion recovery fixtures; no real service or model calls."""
from __future__ import annotations

import asyncio
from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, PdfConfig, load_config
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.knowledge_progress import progress_snapshot
from indeces.memory import MemoryGraph
from indeces.store import Store
from indeces import pdf_import
from tests.test_knowledge import LabelAdapter, Scratch, labels
from tests.test_knowledge_pdf import conversion, RAW_A


class IngestionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            knowledge_dir=self.root / "knowledge", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(max_file_bytes=4096, max_files=16, chunk_characters=100,
                version_seconds=30, version_input_tokens=16384, version_output_tokens=8192),
            pdf=PdfConfig(), adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1",
                {"label": Budget(2048, 32, 1)}))
        self.store = Store(self.config.state_dir)
        self.graph = MemoryGraph(self.store.db)
        self.scratch, self.adapter = Scratch(), LabelAdapter()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)

    async def asyncTearDown(self):
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    def file(self, text, name="paper.md"):
        path = self.config.knowledge_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def current(self, name="paper.md"):
        row = self.store.db.execute("""SELECT v.* FROM knowledge_versions v JOIN knowledge_desired d
            ON d.source_id=v.source_id WHERE d.scope=? AND d.path=?""", (self.service.scope, name)).fetchone()
        return dict(row) if row else None

    def audit(self, name="paper.md"):
        return dict(self.store.db.execute("SELECT * FROM knowledge_file_audit WHERE scope=? AND path=?",
                                        (self.service.scope, name)).fetchone())

    async def test_all_text_formats_exact_byte_bound_rejection_and_recovery(self):
        self.config.knowledge = replace(self.config.knowledge, max_file_bytes=12)
        for suffix in (".md", ".markdown", ".txt"):
            name = "paper" + suffix
            self.file("alpha topic!", name)  # Exactly 12 UTF-8 bytes.
        self.service.scan_once()
        for suffix in (".md", ".markdown", ".txt"):
            self.assertEqual(self.current("paper" + suffix)["status"], "pending")
        path = self.file("alpha topic!!")
        with redirect_stdout(io.StringIO()) as output:
            self.service.scan_once()
        item = self.audit()
        self.assertEqual((item["state"], item["stage"], item["byte_count"]), ("rejected", "validation", 13))
        self.assertEqual(json.loads(item["details_json"]),
                         {"actual_bytes": 13, "limit_bytes": 12, "config_key": "knowledge.max_file_bytes"})
        self.assertIn("limit_bytes=12", output.getvalue())
        snapshot = progress_snapshot(self.config)
        rejected = next(item for item in snapshot["files"] if item["path"] == "paper.md")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["audit"]["byte_count"], 13)
        self.assertIsNone(rejected["desired"])
        path.write_bytes(b"alpha topic!")
        self.service.scan_once()
        self.assertEqual(self.current()["status"], "pending")
        await self.service.label_next(source_ids={self.current()["source_id"]})
        self.assertEqual(self.current()["status"], "ready")

    async def test_decoding_failure_persists_while_other_files_complete(self):
        self.file("alpha topic evidence", "good.txt")
        invalid = self.config.knowledge_dir / "broken.md"
        invalid.write_bytes(b"\xff\xfe")
        with redirect_stdout(io.StringIO()):
            self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(self.current("good.txt")["status"], "ready")
        item = self.audit("broken.md")
        self.assertEqual((item["state"], item["stage"], item["error"]),
                         ("rejected", "decode", "UnicodeDecodeError"))
        invalid.unlink()
        self.service.scan_once()
        self.assertEqual(self.audit("broken.md")["state"], "archived")

    async def test_explicit_retry_resumes_only_missing_chunks_and_keeps_known_usage(self):
        self.file("alpha topic unique first block ".ljust(100, ".") + "alpha topic unique second block")
        self.service.scan_once()
        self.adapter.outcomes.extend((labels(), labels(text='{"marks":[]}')))
        await self.service.label_next()
        failed = self.current()
        source_id = failed["source_id"]
        self.assertEqual(failed["error"], "invalid_labels")
        self.assertEqual(self.audit()["stage"], "label_validation")
        self.assertEqual(self.audit()["remote_usage_unknown"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE marks_json IS NOT NULL").fetchone()[0], 1)
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())  # Never silently retry.
        queued = self.service.retry_failed()
        self.assertEqual([item["source_id"] for item in queued["queued"]], [source_id])
        self.assertEqual(self.service.retry_failed()["queued"], [])  # Repeated command is idempotent.
        self.assertEqual(self.current()["input_tokens"], 50)
        await self.service.label_next()
        self.assertEqual(len(self.adapter.calls), 3)
        self.assertEqual(self.current()["source_id"], source_id)
        self.assertEqual((self.current()["input_tokens"], self.current()["output_tokens"]), (75, 24))
        self.assertEqual(self.current()["status"], "ready")
        records = self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE active=1").fetchone()[0]
        self.assertEqual(records, 2)
        self.assertEqual(self.service.retry_failed()["queued"], [])
        self.service.scan_once()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE active=1").fetchone()[0], records)

    async def test_generation_timeout_reports_file_and_persists_failed_chunk_without_replay(self):
        self.file("alpha first block".ljust(100, ".") + "alpha second block")
        self.service.scan_once()
        self.adapter.outcomes.extend((labels(), GovernedError("stage_timeout", remote_usage_unknown=True)))
        with redirect_stdout(io.StringIO()) as output:
            await self.service.label_next()
        message = output.getvalue()
        for value in ("摄入未完成", "paper.md", "stage=label", "code=stage_timeout", "标词块=1/2", "禁止直接 retry"):
            self.assertIn(value, message)
        audit = self.audit()
        self.assertEqual((audit["state"], audit["error"], audit["remote_usage_unknown"]), ("failed", "stage_timeout", 1))
        self.assertEqual(json.loads(audit["details_json"]),
                         {"failed_chunk_index": 1, "labelled_chunks": 1, "total_chunks": 2})
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.service.retry_failed()["blocked"][0]["code"], "knowledge_retry_unknown_usage")
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE active=1").fetchone()[0], 0)

    async def test_retry_after_restart_retains_stage_and_confirmed_partial_labels(self):
        self.file("alpha first".ljust(100, ".") + "alpha second")
        self.service.scan_once()
        self.adapter.outcomes.extend((labels(), labels(text='{"marks":[]}')))
        await self.service.label_next()
        restarted = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        try:
            restarted.scan_once()
            self.assertEqual(self.audit()["stage"], "label_validation")
            self.assertEqual(len(restarted.retry_failed()["queued"]), 1)
            await restarted.label_next()
            self.assertEqual(self.current()["status"], "ready")
            self.assertEqual(len(self.adapter.calls), 3)
        finally:
            await restarted.close()

    async def test_interrupted_restart_creates_missing_file_audit_and_visible_failure(self):
        self.file("alpha interrupted paper")
        self.service.scan_once()
        source_id = self.current()["source_id"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling' WHERE source_id=?", (source_id,))
            self.store.db.execute("DELETE FROM knowledge_file_audit WHERE source_id=?", (source_id,))
        with redirect_stdout(io.StringIO()) as output:
            restarted = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        try:
            self.assertIn("code=interrupted_unknown_usage", output.getvalue())
            self.assertEqual((self.audit()["state"], self.audit()["remote_usage_unknown"]), ("failed", 1))
            self.assertEqual(restarted.retry_failed()["blocked"][0]["code"], "knowledge_retry_unknown_usage")
            self.assertEqual(self.adapter.calls, [])
        finally:
            await restarted.close()

    async def test_unknown_usage_changed_snapshot_and_exhausted_budget_stay_blocked(self):
        path = self.file("alpha topic evidence")
        self.service.scan_once()
        self.adapter.outcomes.append(GovernedError("provider_network_error", remote_usage_unknown=True))
        await self.service.label_next()
        self.assertEqual(self.service.retry_failed()["blocked"][0]["code"], "knowledge_retry_unknown_usage")
        path.write_text("different content", encoding="utf-8")
        self.assertEqual(self.service.retry_failed()["blocked"][0]["code"], "knowledge_retry_snapshot_changed")
        path.write_text("alpha topic evidence", encoding="utf-8")
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_file_audit SET remote_usage_unknown=0")
            self.store.db.execute("UPDATE knowledge_versions SET elapsed_seconds=?", (self.config.knowledge.version_seconds,))
        self.assertEqual(self.service.retry_failed()["blocked"][0]["code"], "knowledge_version_time_limit")
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_targeted_pending_resume_never_processes_unrelated_sources(self):
        self.file("alpha oldest uncompleted document", "older.md")
        self.service.scan_once()
        self.file("beta requested uncompleted document", "requested.md")
        self.service.scan_once()
        selected = self.current("requested.md")["source_id"]
        queued = self.service.retry_failed("requested.md", include_incomplete=True)
        self.assertEqual([item["source_id"] for item in queued["queued"]], [selected])
        self.assertFalse(await self.service.label_next(source_ids=set()))
        await self.service.label_next(source_ids={selected})
        self.assertEqual(self.current("requested.md")["status"], "ready")
        self.assertEqual(self.current("older.md")["status"], "pending")
        self.assertFalse(await self.service.label_next(source_ids={selected}))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.service.retry_failed("missing.md")["blocked"][0]["code"], "knowledge_retry_not_current")

    async def test_pdf_conversion_failure_can_retry_frozen_bytes_without_duplicate_version(self):
        path = self.config.knowledge_dir / "paper.pdf"
        path.write_bytes(RAW_A)
        self.service.scan_once()
        selected = self.current("paper.pdf")["source_id"]
        self.assertFalse(await self.service.convert_next(source_ids=set()))
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(side_effect=GovernedError("pdf_time_limit"))):
            await self.service.convert_next(source_ids={selected})
        self.assertEqual(self.current("paper.pdf")["status"], "failed")
        self.assertEqual(len(self.service.retry_failed()["queued"]), 1)
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_A))):
            await self.service.convert_next(source_ids={selected})
        await self.service.label_next(source_ids={selected})
        self.assertEqual(self.current("paper.pdf")["status"], "ready")
        self.assertEqual(self.current("paper.pdf")["source_id"], selected)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT pdf_bytes FROM knowledge_pdf_versions").fetchone()[0], RAW_A)

    async def test_recovery_from_unused_input_reservation_preserves_retrieval_baseline(self):
        self.file("alpha topic original evidence", "baseline.md")
        self.service.scan_once()
        await self.service.label_next()
        baseline = self.graph.retrieve(self.service.scope, [], "alpha", 1.0, event_id="before")
        expected = [(item["source_id"], item["quote"]) for item in baseline]
        self.assertTrue(expected)
        self.file("beta independent evidence")
        self.service.scan_once()
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='failed',error='knowledge_version_token_limit',input_tokens=14361 WHERE path='paper.md'")
        queued = self.service.retry_failed()
        self.assertEqual(len(queued["queued"]), 1)  # 2023 remains: below stage 2048, above actual25.
        self.adapter.outcomes.append(labels(text='{"marks":["beta"]}'))
        await self.service.label_next()
        self.assertEqual(self.current()["status"], "ready")
        self.assertEqual(self.adapter.calls[-1]["input_limit"], 2023)
        after = self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="after")
        self.assertEqual([(item["source_id"], item["quote"]) for item in after], expected)

    async def test_more_than_3000_mark_appearances_and_nodes_publish_without_truncation(self):
        self.config.knowledge = replace(self.config.knowledge, max_file_bytes=65536, chunk_characters=40,
                                        version_input_tokens=65536, version_seconds=60)
        content = "".join(f"topic-{i} controlled evidence".ljust(40, ".") for i in range(501))
        self.file(content)
        for i in range(501):
            marks = [f"topic-{i}", *[f"marker_{i}_{j}" for j in range(5)]]
            self.adapter.outcomes.append(labels(text=json.dumps({"marks": marks})))
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(self.current()["status"], "ready")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE marks_json IS NOT NULL").fetchone()[0], 501)
        counts = self.store.db.execute("""SELECT COUNT(*),COUNT(DISTINCT j.value) FROM memory_records r,
            json_each(r.marks_json) j WHERE r.scope=? AND r.active=1""", (self.service.scope,)).fetchone()
        self.assertEqual(tuple(counts), (3006, 3006))
        result = self.graph.retrieve(self.service.scope, [], "topic-500", 1.0, event_id="large-last")
        self.assertTrue(any("topic-500" in item["quote"] for item in result))


class KnowledgeBoundValidationTests(unittest.TestCase):
    def test_direct_and_toml_construction_use_same_validation(self):
        template = Path("config.example.toml").read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in (0, True, 1048577):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    KnowledgeConfig(max_file_bytes=value)
                encoded = str(value).lower() if type(value) is bool else str(value)
                path.write_text(template.replace("max_file_bytes = 8192", "max_file_bytes = " + encoded), encoding="utf-8")
                with self.subTest(toml=value), self.assertRaises(ValueError):
                    load_config(path)
            self.assertEqual(KnowledgeConfig(max_file_bytes=1048576).max_file_bytes, 1048576)
        config = KnowledgeConfig()
        self.assertEqual(config.file_limit(".md", PdfConfig()), (8192, "knowledge.max_file_bytes"))
        self.assertEqual(config.file_limit(".PDF", PdfConfig()), (16777216, "pdf.max_file_bytes"))

    def test_existing_database_is_backed_up_before_additive_audit_migration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            graph = MemoryGraph(store.db)
            config = SimpleNamespace(name="Indeces", state_dir=root / "state", knowledge_dir=root / "knowledge",
                discord=DiscordConfig("10"), knowledge=KnowledgeConfig(), adapter=SimpleNamespace())
            service = KnowledgeService(config, store, graph, None, Scratch())
            store.db.execute("DROP TABLE knowledge_file_audit")
            store.db.commit()
            before = "\n".join(store.db.iterdump())
            KnowledgeService(config, store, graph, None, Scratch())
            backups = list((root / "state" / "migration_backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as copy:
                self.assertEqual("\n".join(copy.iterdump()), before)
            KnowledgeService(config, store, graph, None, Scratch())
            self.assertEqual(len(list((root / "state" / "migration_backups").glob("*.sqlite3"))), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
