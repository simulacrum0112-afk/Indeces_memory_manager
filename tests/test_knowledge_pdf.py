from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, PdfConfig
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces import pdf_import
from indeces.store import Store
from tests.test_knowledge import LabelAdapter, Scratch
from tests.test_knowledge import labels
from tests.pdf_fixtures import text_pdf


RAW_A = b"%PDF-1.4\nsynthetic immutable paper A"
RAW_B = b"%PDF-1.4\nsynthetic immutable paper B"
RAW_C = b"%PDF-1.4\nsynthetic immutable paper C"


def conversion(raw, *texts):
    """Synthetic converter output; these fixtures do not pretend to be PDFs."""
    markdown, pages = "", []
    for number, text in enumerate(texts or ("alpha topic extracted paper",), 1):
        page = f"## PDF page {number}\n\n{text}\n\n"
        start = len(markdown)
        markdown += page
        pages.append({"page_number": number, "start_character": start,
                      "end_character": len(markdown), "text_sha256": hashlib.sha256(page.encode()).hexdigest()})
    metadata = {"schema_version": 1, "original_pdf_sha256": hashlib.sha256(raw).hexdigest(),
                "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                "extractor": {"name": "pypdf", "version": pdf_import.EXTRACTOR_VERSION,
                              "policy_version": pdf_import.POLICY_VERSION, "mode": "plain"},
                "page_count": len(pages), "pages": pages,
                "warnings": sorted(["pdf_text_reading_order_unverified", "pdf_math_tables_figures_not_reconstructed"])}
    pdf_import.validate_conversion(markdown, metadata)
    return {"markdown": markdown, "metadata": metadata}


class PdfKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            knowledge_dir=self.root / "knowledge", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(poll_seconds=0.01, max_files=8),
            pdf=PdfConfig(max_file_bytes=16384, max_pages=3, max_markdown_bytes=32768, seconds=1),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(4096, 512, 15)}))
        self.store = Store(self.config.state_dir)
        self.graph = MemoryGraph(self.store.db)
        self.scratch, self.adapter = Scratch(), LabelAdapter()
        self.quiet = patch("builtins.print")
        self.printed = self.quiet.start()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)

    async def asyncTearDown(self):
        await self.service.close()
        self.quiet.stop()
        self.store.close()
        self.directory.cleanup()

    def file(self, raw=RAW_A, name="paper.pdf"):
        path = self.config.knowledge_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def head(self, name="paper.pdf"):
        row = self.store.db.execute("SELECT v.* FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.path=?", (self.service.scope, name)).fetchone()
        return dict(row) if row else None

    def published(self, name="paper.pdf"):
        row = self.store.db.execute("SELECT v.* FROM knowledge_published p JOIN knowledge_versions v ON v.source_id=p.source_id WHERE p.scope=? AND p.path=?", (self.service.scope, name)).fetchone()
        return dict(row) if row else None

    def version(self, source_id):
        return dict(self.store.db.execute("SELECT * FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone())

    def pdf_version(self, source_id):
        return dict(self.store.db.execute("SELECT * FROM knowledge_pdf_versions WHERE source_id=?", (source_id,)).fetchone())

    def chunks(self, source_id):
        return [dict(row) for row in self.store.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,))]

    def receipts(self, source_id=None, kind=None):
        return [fields for event, fields in self.scratch.events if event == "knowledge_pdf_receipt"
                and (source_id is None or fields["source_id"] == source_id)
                and (kind is None or fields["receipt"] == kind)]

    async def ready(self, raw=RAW_A, text="alpha topic published old paper", name="paper.pdf"):
        self.file(raw, name)
        self.service.scan_once()
        source_id = self.head(name)["source_id"]
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(raw, text))):
            self.assertTrue(await self.service.convert_next())
        self.assertTrue(await self.service.label_next())
        self.assertEqual(self.published(name)["source_id"], source_id)
        return source_id

    async def test_pdf_keeps_original_digest_and_frozen_bytes_until_converted_and_labelled(self):
        self.file()
        self.service.scan_once()
        version = self.head()
        source_id = version["source_id"]
        self.assertEqual((version["path"], version["raw_text"], version["status"]), ("paper.pdf", "", "converting"))
        self.assertEqual(version["digest"], hashlib.sha256(RAW_A).hexdigest())
        self.assertEqual(self.pdf_version(source_id)["pdf_bytes"], RAW_A)
        self.assertIsNone(self.pdf_version(source_id)["metadata_json"])
        self.assertEqual(self.chunks(source_id), [])
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])
        result = conversion(RAW_A, "alpha first physical page", "alpha second physical page")
        converter = AsyncMock(return_value=result)
        with patch.object(pdf_import, "convert_pdf", new=converter):
            await self.service.convert_next()
        converter.assert_awaited_once_with(RAW_A, self.config.pdf)
        current = self.head()
        self.assertEqual(current["source_id"], source_id)
        self.assertEqual((current["status"], current["raw_text"]), ("pending", result["markdown"]))
        self.assertEqual(current["digest"], version["digest"])
        self.assertEqual((current["input_tokens"], current["output_tokens"], current["elapsed_seconds"]), (0, 0, 0))
        frozen = self.pdf_version(source_id)
        self.assertEqual(json.loads(frozen["metadata_json"]), result["metadata"])
        exported = Path(frozen["markdown_path"])
        self.assertEqual(exported.parent, self.config.state_dir / "pdf_markdown")
        self.assertEqual(exported.read_text(encoding="utf-8"), result["markdown"])
        self.assertFalse(exported.is_relative_to(self.config.knowledge_dir))
        self.assertIsNone(self.published())
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], source_id)
        self.assertEqual(self.head()["status"], "ready")
        self.assertEqual([r["receipt"] for r in self.receipts(source_id)], ["queued", "started", "converted"])
        started = self.receipts(source_id, "started")[0]
        self.assertEqual(started["budget"], {"max_file_bytes": 16384, "max_pages": 3,
                                            "max_markdown_bytes": 32768, "seconds": 1})
        self.assertEqual((started["model_requests"], started["input_tokens"], started["output_tokens"]), (0, 0, 0))
        self.service.scan_once()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)
        self.assertFalse(await self.service.convert_next())
        self.assertFalse(any(isinstance(value, bytes) for _, fields in self.scratch.events for value in fields.values()))

    async def test_real_pdf_conversion_and_publication_are_console_silent_with_complete_audit(self):
        raw = text_pdf(["alpha first page material " * 25, "beta second page material " * 25])
        self.file(raw)
        self.service.pdf_limits = replace(self.config.pdf, seconds=10)
        output = StringIO()
        self.quiet.stop()
        try:
            with redirect_stdout(output):
                self.service.scan_once()
                source_id = self.head()["source_id"]
                self.assertTrue(await self.service.convert_next())
                self.assertTrue(await self.service.label_next())
        finally:
            self.printed = self.quiet.start()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.published()["source_id"], source_id)
        self.assertEqual(self.version(source_id)["status"], "ready")
        pdf_receipts = self.receipts(source_id)
        self.assertEqual([receipt["receipt"] for receipt in pdf_receipts],
                         ["queued", "started", "converted"])
        self.assertEqual(pdf_receipts[-1]["pages"], 2)
        self.assertTrue(all(receipt.get("model_requests", 0) == 0 for receipt in pdf_receipts))
        chunks = self.chunks(source_id)
        self.assertGreater(len(chunks), 1)
        label_receipts = [fields for event, fields in self.scratch.events
                          if event == "knowledge_receipt" and fields["source_id"] == source_id]
        self.assertEqual([receipt["receipt"] for receipt in label_receipts],
                         ["started"] + ["progress"] * len(chunks) + ["completed"])
        completed = label_receipts[-1]
        self.assertEqual((completed["reply_source_id"], completed["labelled_chunks"], completed["total_chunks"]),
                         (source_id, len(chunks), len(chunks)))
        self.assertEqual((completed["input_tokens"], completed["output_tokens"]),
                         (25 * len(chunks), 8 * len(chunks)))
        labelled = [fields for event, fields in self.scratch.events
                    if event == "knowledge_chunk_labelled" and fields["source_id"] == source_id]
        self.assertEqual([record["quote"] for record in labelled], [chunk["text"] for chunk in chunks])
        self.assertEqual([call["stage"] for call in self.adapter.calls], ["label"] * len(chunks))

    async def test_real_pdf_conversion_failure_is_visible_and_preserves_audit_and_old_version(self):
        old = await self.ready()
        self.file(text_pdf(["alpha inaccessible encrypted paper"], encrypted=True))
        self.service.pdf_limits = replace(self.config.pdf, seconds=10)
        calls_before = len(self.adapter.calls)
        output = StringIO()
        self.quiet.stop()
        try:
            with redirect_stdout(output):
                self.service.scan_once()
                new = self.head()["source_id"]
                self.assertTrue(await self.service.convert_next())
                self.assertFalse(await self.service.label_next())
        finally:
            self.printed = self.quiet.start()
        self.assertIn("摄入未完成", output.getvalue())
        self.assertIn("stage=conversion", output.getvalue())
        self.assertIn("code=pdf_encrypted", output.getvalue())
        self.assertEqual((self.version(new)["status"], self.version(new)["error"]), ("failed", "pdf_encrypted"))
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.adapter.calls), calls_before)
        receipts = self.receipts(new)
        self.assertEqual([receipt["receipt"] for receipt in receipts], ["queued", "started", "failed"])
        failed = receipts[-1]
        self.assertEqual((failed["reply_source_id"], failed["code"], failed["model_requests"]),
                         (old, "pdf_encrypted", 0))
        self.assertEqual((failed["input_tokens"], failed["output_tokens"]), (0, 0))
        self.assertEqual(self.chunks(new), [])

    async def test_pdf_and_derived_markdown_do_not_use_legacy_eight_kib_text_bound(self):
        raw = RAW_A + b" " * 9000
        self.file(raw)
        self.service.scan_once()
        source_id = self.head()["source_id"]
        result = conversion(raw, "alpha topic " * 1000)
        self.assertGreater(len(raw), self.config.knowledge.max_file_bytes)
        self.assertGreater(len(result["markdown"].encode()), self.config.knowledge.max_file_bytes)
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=result)):
            await self.service.convert_next()
        self.assertTrue(self.service._is_current(source_id))
        chunks = self.chunks(source_id)
        self.assertTrue(all(len(c["text"]) <= self.config.knowledge.chunk_characters for c in chunks))
        self.assertEqual("".join(c["text"] for c in chunks), result["markdown"])
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], source_id)

    async def test_old_publication_and_marks_serve_while_conversion_is_waiting(self):
        old = await self.ready()
        self.file(RAW_B)
        self.service.scan_once()
        new = self.head()["source_id"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(raw, limits):
            entered.set()
            await release.wait()
            return conversion(raw, "beta topic pending new paper")

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            task = asyncio.create_task(self.service.convert_next())
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                self.assertEqual(self.head()["status"], "converting")
                self.assertEqual(self.published()["source_id"], old)
                found = self.graph.retrieve(self.service.scope, [], "alpha", 2, event_id="during-conversion")
                self.assertEqual([r["source_id"] for r in found], [old])
                self.assertIn("published old paper", found[0]["quote"])
                self.assertEqual(self.chunks(new), [])
                self.assertEqual(len(self.adapter.calls), 1)
            finally:
                release.set()
                await task
        self.assertEqual(self.head()["status"], "pending")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.receipts(new, "converted")[0]["reply_source_id"], old)

    async def test_real_multipage_pdf_publishes_only_after_every_chunk_is_labelled(self):
        old = await self.ready()
        raw = text_pdf(["alpha first page paragraph " * 30, "beta second page paragraph " * 30])
        self.file(raw)
        self.service.scan_once()
        new = self.head()["source_id"]
        self.service.pdf_limits = replace(self.config.pdf, seconds=10)
        await self.service.convert_next()
        self.assertEqual(self.version(new)["status"], "pending")
        metadata = json.loads(self.pdf_version(new)["metadata_json"])
        self.assertEqual(metadata["page_count"], 2)
        self.assertIn("alpha first page paragraph", self.version(new)["raw_text"])
        self.assertIn("beta second page paragraph", self.version(new)["raw_text"])
        entered, release = asyncio.Event(), asyncio.Event()
        completed = 0

        async def partial_labels(*args, **kwargs):
            nonlocal completed
            completed += 1
            if completed == 2:
                entered.set()
                await release.wait()
            return labels()

        with patch.object(self.adapter, "call", new=AsyncMock(side_effect=partial_labels)):
            task = asyncio.create_task(self.service.label_next())
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                chunks = self.chunks(new)
                self.assertIsNotNone(chunks[0]["marks_json"])
                self.assertTrue(any(c["marks_json"] is None for c in chunks[1:]))
                self.assertEqual(self.published()["source_id"], old)
                self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE source_id=?", (new,)).fetchone()[0], 0)
            finally:
                release.set()
                await task
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual(self.version(old)["status"], "superseded")
        self.assertTrue(all(c["marks_json"] is not None for c in self.chunks(new)))
        self.assertEqual(self.pdf_version(old)["pdf_bytes"], RAW_A)

    async def test_real_blank_and_encrypted_pdf_keep_published_snapshot_and_need_no_model(self):
        old = await self.ready()
        self.service.pdf_limits = replace(self.config.pdf, seconds=5)
        calls_before = len(self.adapter.calls)
        for raw, code in ((text_pdf([None]), "pdf_needs_ocr_or_review"),
                          (text_pdf(["alpha confidential text"], encrypted=True), "pdf_encrypted")):
            with self.subTest(code=code):
                self.file(raw)
                self.service.scan_once()
                new = self.head()["source_id"]
                await self.service.convert_next()
                self.assertEqual(self.version(new)["error"], code)
                self.assertEqual(self.published()["source_id"], old)
                self.service.scan_once()
                self.assertFalse(await self.service.convert_next())
                self.assertEqual(len(self.adapter.calls), calls_before)

    async def test_ocr_encryption_timeout_and_invalid_input_fail_once_without_replacing_old(self):
        for code in ("pdf_needs_ocr_or_review", "pdf_encrypted", "pdf_time_limit", "pdf_invalid"):
            with self.subTest(code=code):
                old = await self.ready(RAW_A + code.encode(), "alpha stable " + code, name=code + ".pdf")
                self.file(RAW_B + code.encode(), code + ".pdf")
                self.service.scan_once()
                new = self.head(code + ".pdf")["source_id"]
                parser = AsyncMock(side_effect=GovernedError(code))
                with patch.object(pdf_import, "convert_pdf", new=parser):
                    await self.service.convert_next()
                    self.service.scan_once()
                    self.assertFalse(await self.service.convert_next())
                self.assertEqual(parser.await_count, 1)
                self.assertEqual((self.version(new)["status"], self.version(new)["error"]), ("failed", code))
                self.assertEqual(self.published(code + ".pdf")["source_id"], old)
                self.assertEqual(self.version(old)["status"], "ready")
                self.assertEqual(self.chunks(new), [])
                self.assertFalse(await self.service.label_next())

    async def test_outer_conversion_timeout_cancels_converter_with_no_model_usage(self):
        self.service.pdf_limits = replace(self.config.pdf, seconds=0.01)
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        cancelled = asyncio.Event()

        async def blocked(raw, limits):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            await self.service.convert_next()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.version(source_id)["error"], "pdf_time_limit")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.receipts(source_id, "failed")[0]["model_requests"], 0)

    async def test_new_pdf_supersedes_inflight_conversion_without_publishing_stale_markdown(self):
        old = await self.ready()
        self.file(RAW_B)
        self.service.scan_once()
        second = self.head()["source_id"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(raw, limits):
            entered.set()
            await release.wait()
            return conversion(raw, "beta obsolete paper")

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            task = asyncio.create_task(self.service.convert_next())
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                self.file(RAW_C)
                self.service.scan_once()
                third = self.head()["source_id"]
                self.assertEqual(self.version(second)["status"], "superseded")
            finally:
                release.set()
                await task
        self.assertEqual(self.chunks(second), [])
        self.assertIsNone(self.pdf_version(second)["metadata_json"])
        self.assertEqual(self.published()["source_id"], old)
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_C))):
            await self.service.convert_next()
        self.assertEqual(self.head()["source_id"], third)
        self.assertEqual(self.head()["status"], "pending")

    async def test_unscanned_physical_change_cannot_publish_obsolete_conversion(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]

        async def mutate(raw, limits):
            self.file(RAW_B)
            return conversion(raw)

        with patch.object(pdf_import, "convert_pdf", side_effect=mutate):
            await self.service.convert_next()
        self.assertEqual(self.version(source_id)["status"], "superseded")
        self.assertEqual(self.chunks(source_id), [])
        self.assertFalse((self.config.state_dir / "pdf_markdown").exists())
        self.service.scan_once()
        self.assertNotEqual(self.head()["source_id"], source_id)

    async def test_removing_pdf_during_conversion_retires_old_snapshot_and_ignores_late_result(self):
        old = await self.ready()
        path = self.file(RAW_B)
        self.service.scan_once()
        new = self.head()["source_id"]

        async def remove(raw, limits):
            path.unlink()
            self.service.scan_once()
            return conversion(raw)

        with patch.object(pdf_import, "convert_pdf", side_effect=remove):
            await self.service.convert_next()
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.assertEqual(self.version(old)["status"], "superseded")
        self.assertEqual(self.version(new)["status"], "superseded")
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2, event_id="after-delete"), [])
        self.assertEqual(self.pdf_version(old)["pdf_bytes"], RAW_A)

    async def test_restart_recovers_current_conversion_from_frozen_blob(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        await self.service.close()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        parser = AsyncMock(return_value=conversion(RAW_A))
        with patch.object(pdf_import, "convert_pdf", new=parser):
            await self.service.convert_next()
        parser.assert_awaited_once_with(RAW_A, self.config.pdf)
        self.assertEqual(self.head()["source_id"], source_id)
        self.assertEqual(self.head()["status"], "pending")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)

    async def test_restart_recovers_after_matching_markdown_export_preceded_database_commit(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        result = conversion(RAW_A)
        exported = pdf_import.publish_markdown(self.config.state_dir, source_id, result["markdown"])
        self.assertIsNone(self.pdf_version(source_id)["markdown_path"])
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=result)):
            await self.service.convert_next()
        self.assertEqual(self.version(source_id)["status"], "pending")
        self.assertEqual(Path(self.pdf_version(source_id)["markdown_path"]), exported)
        self.assertEqual(exported.read_text(encoding="utf-8"), result["markdown"])

    async def test_cancelled_local_conversion_can_restart_without_replaying_a_model(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        entered = asyncio.Event()

        async def blocked(raw, limits):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            task = asyncio.create_task(self.service.convert_next())
            async with asyncio.timeout(1):
                await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.version(source_id)["status"], "converting")
        self.assertEqual(self.version(source_id)["error"], "pdf_conversion_interrupted")
        self.assertEqual(self.adapter.calls, [])
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_A))):
            await self.service.convert_next()
        self.assertEqual(self.version(source_id)["status"], "pending")
        self.assertIsNone(self.version(source_id)["error"])

    async def test_stale_restart_does_not_run_parser_or_accept_changed_pdf(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        self.file(RAW_B)
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        parser = AsyncMock(return_value=conversion(RAW_A))
        with patch.object(pdf_import, "convert_pdf", new=parser):
            await self.service.convert_next()
        parser.assert_not_awaited()
        self.assertEqual(self.version(source_id)["status"], "superseded")

    async def test_pdf_input_limit_retires_existing_source_instead_of_converting_prefix(self):
        old = await self.ready()
        self.file(RAW_B + b" " * self.config.pdf.max_file_bytes)
        self.service.scan_once()
        self.assertIsNone(self.published())
        self.assertIsNone(self.head())
        self.assertEqual(self.version(old)["error"], "knowledge_file_size_limit")
        self.assertFalse(await self.service.convert_next())
        self.assertEqual(self.pdf_version(old)["pdf_bytes"], RAW_A)

    async def test_zero_byte_pdf_clears_published_source_without_parser_or_model_call(self):
        old = await self.ready()
        calls_before = len(self.adapter.calls)
        versions_before = self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0]
        self.file(b"")
        parser = AsyncMock()
        with patch.object(pdf_import, "convert_pdf", new=parser):
            self.service.scan_once()
            self.assertFalse(await self.service.convert_next())
            self.assertFalse(await self.service.label_next())
        parser.assert_not_awaited()
        self.assertEqual(len(self.adapter.calls), calls_before)
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.assertEqual((self.version(old)["status"], self.version(old)["error"]),
                         ("superseded", "knowledge_pdf_empty"))
        self.assertEqual(self.pdf_version(old)["pdf_bytes"], RAW_A)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], versions_before)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2, event_id="after-pdf-clear"), [])

    async def test_invalid_conversion_metadata_and_output_bounds_keep_old_publication(self):
        old = await self.ready()
        for mode in ("hash", "pages", "output"):
            with self.subTest(mode=mode):
                raw = RAW_B + mode.encode()
                self.file(raw)
                self.service.scan_once()
                new = self.head()["source_id"]
                result = conversion(raw)
                if mode == "hash":
                    result["metadata"]["original_pdf_sha256"] = "0" * 64
                elif mode == "pages":
                    result = conversion(raw, "one", "two", "three", "four")
                else:
                    result = conversion(raw, "alpha " * 6000)
                with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=result)):
                    await self.service.convert_next()
                self.assertEqual(self.version(new)["status"], "failed")
                self.assertEqual(self.published()["source_id"], old)
                self.assertEqual(self.chunks(new), [])
                self.assertIsNone(self.pdf_version(new)["metadata_json"])

    async def test_frozen_pdf_blob_corruption_fails_before_parser(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_pdf_versions SET pdf_bytes=? WHERE source_id=?", (RAW_B, source_id))
        parser = AsyncMock()
        with patch.object(pdf_import, "convert_pdf", new=parser):
            await self.service.convert_next()
        parser.assert_not_awaited()
        self.assertEqual(self.version(source_id)["error"], "pdf_frozen_snapshot_invalid")

    async def test_remaining_version_input_admits_paper_below_stage_cap_without_borrowing(self):
        old = await self.ready()
        self.file(RAW_B)
        self.service.scan_once()
        new = self.head()["source_id"]
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_B))):
            await self.service.convert_next()
        self.config.knowledge = replace(self.config.knowledge, version_input_tokens=4095)
        calls_before = len(self.adapter.calls)
        await self.service.label_next()
        self.assertEqual(self.version(new)["status"], "ready")
        self.assertEqual(len(self.adapter.calls), calls_before + 1)
        self.assertEqual(self.adapter.calls[-1]["input_limit"], 4095)
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual(self.version(new)["input_tokens"], 25)
        self.assertEqual(self.version(old)["status"], "superseded")

    async def test_conversion_database_failure_rolls_back_text_chunks_and_metadata(self):
        old = await self.ready()
        self.file(RAW_B)
        self.service.scan_once()
        new = self.head()["source_id"]
        self.store.db.executescript("""CREATE TRIGGER reject_converted BEFORE UPDATE ON knowledge_versions
            WHEN NEW.status='pending' AND OLD.status='converting'
            BEGIN SELECT RAISE(ABORT, 'synthetic conversion rejection'); END;""")
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_B))):
            await self.service.convert_next()
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.version(new)["raw_text"], "")
        self.assertIsNone(self.pdf_version(new)["metadata_json"])
        self.assertEqual(self.chunks(new), [])
        self.assertEqual(self.published()["source_id"], old)

    async def test_legacy_fixtures_without_pdf_or_state_attributes_use_safe_defaults(self):
        del self.config.pdf
        del self.config.state_dir
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertEqual(self.service.pdf_limits, PdfConfig())
        self.assertFalse((self.root / "state" / "pdf_markdown").exists())
        source_id = await self.ready()
        self.assertEqual(Path(self.pdf_version(source_id)["markdown_path"]).parent, self.root / "state" / "pdf_markdown")

    async def test_export_directory_inside_knowledge_is_rejected_before_writing(self):
        self.config.state_dir = self.config.knowledge_dir / "nested_state"
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_A))):
            await self.service.convert_next()
        self.assertEqual(self.version(source_id)["error"], "pdf_export_inside_knowledge")
        self.assertFalse((self.config.state_dir / "pdf_markdown").exists())

    async def test_conversion_requests_are_serialized_without_using_the_adapter(self):
        self.file(RAW_A, "first.pdf")
        self.file(RAW_B, "second.pdf")
        self.service.scan_once()
        entered, release = asyncio.Event(), asyncio.Event()
        active, peak, received = 0, 0, []

        async def blocked(raw, limits):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            received.append(raw)
            entered.set()
            try:
                await release.wait()
                return conversion(raw)
            finally:
                active -= 1

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            first = asyncio.create_task(self.service.convert_next())
            second = asyncio.create_task(self.service.convert_next())
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                self.assertEqual(received, [RAW_A])
                self.assertEqual(self.adapter.calls, [])
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertEqual(received, [RAW_A, RAW_B])
        self.assertEqual(peak, 1)

    async def test_watcher_file_reads_run_off_loop_with_one_file_buffer(self):
        self.file()
        entered, release = threading.Event(), threading.Event()
        original = self.service._snapshot_file
        owner_thread = threading.get_ident()
        reader_threads = []

        def blocked_read(path):
            reader_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(1):
                raise AssertionError("reader was not released")
            return original(path)

        with patch.object(self.service, "_snapshot_file", side_effect=blocked_read):
            self.service.start()
            try:
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        await asyncio.sleep(0)
                # This coroutine still runs while the filesystem read is held.
                self.assertEqual(self.adapter.calls, [])
                self.assertIsNone(self.head())
                self.assertTrue(all(identifier != owner_thread for identifier in reader_threads))
            finally:
                release.set()
                await self.service.close()
        self.assertIsNone(self.service.background_error)

    async def test_close_drains_cancelled_watcher_reads_before_releasing_pdf_handles(self):
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                path = self.file(name="held-reader.pdf")
                entered, finished = asyncio.Event(), asyncio.Event()
                release = threading.Event()
                self.addCleanup(release.set)
                loop = asyncio.get_running_loop()
                original = self.service._snapshot_file
                handles = []
                closing = None

                def held_read(file_path):
                    try:
                        with file_path.open("rb") as handle:
                            handles.append(handle)
                            loop.call_soon_threadsafe(entered.set)
                            release.wait()
                            if outcome == "failed":
                                raise OSError("synthetic reader failure after cancellation")
                            return original(file_path)
                    finally:
                        loop.call_soon_threadsafe(finished.set)

                with patch.object(self.service, "_snapshot_file", side_effect=held_read):
                    self.service.start()
                    try:
                        async with asyncio.timeout(1):
                            await entered.wait()
                        tasks = list(self.service._tasks)
                        watcher = next(task for task in tasks if task.get_name() == "indeces-knowledge-watch")
                        watcher.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        closing = asyncio.create_task(self.service.close())
                        # Already-finished background tasks cannot account for
                        # a pending close; the reader still owns this real handle.
                        for _ in range(4):
                            await asyncio.sleep(0)
                        self.assertFalse(closing.done())
                        self.assertFalse(finished.is_set())
                        self.assertEqual(len(handles), 1)
                        self.assertFalse(handles[0].closed)
                        closing.cancel()
                        for _ in range(4):
                            await asyncio.sleep(0)
                        self.assertFalse(closing.done())
                    finally:
                        release.set()
                        if closing is not None:
                            await asyncio.gather(closing, return_exceptions=True)
                        async with asyncio.timeout(1):
                            await finished.wait()
                self.assertTrue(closing.cancelled())
                self.assertTrue(handles[0].closed)
                self.assertEqual(self.service._tasks, [])
                self.assertEqual(self.adapter.calls, [])
                self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 0)
                self.assertIsNone(self.published("held-reader.pdf"))
                path.unlink()

    async def test_background_converter_does_not_block_text_updates_and_shutdown_cancels_it(self):
        self.file()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(raw, limits):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(pdf_import, "convert_pdf", side_effect=blocked):
            self.service.start()
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                self.file(b"alpha text source independently labelled", "other.txt")
                async with asyncio.timeout(1):
                    while self.published("other.txt") is None:
                        await asyncio.sleep(0.005)
                self.assertIsNone(self.published())
                self.assertEqual(self.head()["status"], "converting")
            finally:
                await self.service.close()
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(self.service.background_error)
        self.assertTrue(all(task.done() for task in self.service._tasks))

    async def test_converted_audit_failure_pauses_background_without_masking_durable_conversion(self):
        old = await self.ready()
        self.file(RAW_B)
        self.service.scan_once()
        new = self.head()["source_id"]
        original = self.scratch.write

        def fail_converted(event, **fields):
            if event == "knowledge_pdf_converted":
                raise OSError("synthetic private PDF diagnostic")
            return original(event, **fields)

        with patch.object(pdf_import, "convert_pdf", new=AsyncMock(return_value=conversion(RAW_B))), \
                patch.object(self.scratch, "write", side_effect=fail_converted):
            self.service.start()
            async with asyncio.timeout(1):
                while self.service.background_error is None or not all(t.done() for t in self.service._tasks):
                    await asyncio.sleep(0.005)
        self.assertEqual(self.service.background_error, "OSError")
        self.assertEqual(self.version(new)["status"], "pending")
        self.assertIsNotNone(self.pdf_version(new)["metadata_json"])
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.receipts(new, "failed"), [])
        printed = "\n".join(str(call.args[0]) for call in self.printed.call_args_list)
        self.assertIn("后台已暂停", printed)
        self.assertNotIn("synthetic private PDF diagnostic", printed)


if __name__ == "__main__":
    unittest.main()
