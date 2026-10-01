import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from indeces.config import PdfConfig, load_config
from indeces.contracts import GovernedError
from indeces import pdf_import
from tests.pdf_fixtures import text_pdf


TEXT = "The polymer study measures temperature and pressure with original reference material."


class PdfImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_preserves_pages_and_version_hashes(self):
        raw = text_pdf([TEXT, "Second page: " + TEXT])
        result = await pdf_import.convert_pdf(raw, PdfConfig())
        self.assertIn("## PDF page 1", result["markdown"])
        self.assertIn("## PDF page 2", result["markdown"])
        self.assertIn(TEXT, result["markdown"])
        self.assertEqual(result["metadata"]["original_pdf_sha256"], hashlib.sha256(raw).hexdigest())
        pdf_import.validate_conversion(result["markdown"], result["metadata"], hashlib.sha256(raw).hexdigest())
        broken = {**result["metadata"], "original_pdf_sha256": "0" * 64}
        with self.assertRaises(ValueError):
            pdf_import.validate_conversion(result["markdown"], broken, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["metadata"]["extractor"]["version"], "6.19.0")

    async def test_blank_page_rejects_whole_document_instead_of_silent_partial_text(self):
        with self.assertRaises(GovernedError) as error:
            await pdf_import.convert_pdf(text_pdf([TEXT, None]), PdfConfig())
        self.assertEqual(error.exception.code, "pdf_needs_ocr_or_review")

    async def test_encrypted_and_invalid_inputs_have_fixed_error_codes(self):
        for raw, code in ((text_pdf([TEXT], encrypted=True), "pdf_encrypted"),
                          (b"%PDF-1.7\nnot a PDF", "pdf_invalid")):
            with self.subTest(code=code), self.assertRaises(GovernedError) as error:
                await pdf_import.convert_pdf(raw, PdfConfig())
            self.assertEqual(error.exception.code, code)

    async def test_independent_input_page_and_output_caps(self):
        cases = [(text_pdf([TEXT]) + b" " * 4096, PdfConfig(max_file_bytes=1024), "pdf_file_size_limit"),
                 (text_pdf([TEXT, TEXT]), PdfConfig(max_pages=1), "pdf_page_limit"),
                 (text_pdf([TEXT * 30]), PdfConfig(max_markdown_bytes=1024), "pdf_output_limit")]
        for raw, limits, code in cases:
            with self.subTest(code=code), self.assertRaises(GovernedError) as error:
                await pdf_import.convert_pdf(raw, limits)
            self.assertEqual(error.exception.code, code)

    async def test_malformed_worker_protocol_has_a_fixed_failure_code(self):
        class Finished:
            returncode = 0
            async def wait(self):
                return 0
        for response in (json.dumps({"ok": True, "markdown": 42, "metadata": {"page_count": 1}}),
                         json.dumps({"ok": True, "markdown": "draft", "metadata": "invalid"}),
                         '[' * 1100 + '0' + ']' * 1100):
            async def fake(*args, **kwargs):
                Path(args[5]).write_text(response, encoding="utf-8")
                return Finished()
            with self.subTest(response_prefix=response[:30]), patch.object(
                    pdf_import.asyncio, "create_subprocess_exec", side_effect=fake):
                with self.assertRaises(GovernedError) as error:
                    await pdf_import.convert_pdf(text_pdf([TEXT]), PdfConfig())
            self.assertEqual(error.exception.code, "pdf_worker_failure")

    async def test_deadline_kills_and_reaps_a_real_worker(self):
        created = []
        real_create = asyncio.create_subprocess_exec
        async def tracked(*args, **kwargs):
            process = await real_create(*args, **kwargs)
            created.append(process)
            return process
        with patch.object(pdf_import.asyncio, "create_subprocess_exec", side_effect=tracked):
            with self.assertRaises(GovernedError) as error:
                await pdf_import.convert_pdf(text_pdf([TEXT]), PdfConfig(seconds=0.00001))
        self.assertEqual(error.exception.code, "pdf_time_limit")
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)

    async def test_cancellation_during_spawn_still_reaps_handle(self):
        spawned, release = asyncio.Event(), asyncio.Event()
        created = []
        real_create = asyncio.create_subprocess_exec
        async def held(*args, **kwargs):
            # A harmless synthetic sleeper verifies cleanup during spawn.
            process = await real_create(args[0], "-c", "import time; time.sleep(60)", **kwargs)
            created.append(process)
            spawned.set()
            await release.wait()
            return process
        with patch.object(pdf_import.asyncio, "create_subprocess_exec", side_effect=held):
            task = asyncio.create_task(pdf_import.convert_pdf(text_pdf([TEXT]), PdfConfig()))
            await asyncio.wait_for(spawned.wait(), 10)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(created[0].returncode)

    async def test_repeated_cancellation_cannot_abandon_spawned_process(self):
        spawned, release = asyncio.Event(), asyncio.Event()
        created = []
        real_create = asyncio.create_subprocess_exec
        async def held(*args, **kwargs):
            process = await real_create(args[0], "-c", "import time; time.sleep(60)", **kwargs)
            created.append(process)
            spawned.set()
            await release.wait()
            return process
        with patch.object(pdf_import.asyncio, "create_subprocess_exec", side_effect=held):
            task = asyncio.create_task(pdf_import.convert_pdf(text_pdf([TEXT]), PdfConfig()))
            await asyncio.wait_for(spawned.wait(), 10)
            task.cancel()
            # Enter finally while the process creation wrapper still owns its handle.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        self.assertIsNotNone(created[0].returncode)

    async def test_occurrences_include_duplicate_chunks_and_cross_page_ranges(self):
        result = await pdf_import.convert_pdf(text_pdf([TEXT, TEXT]), PdfConfig())
        metadata = result["metadata"]
        chunks = [{"chunk_index": n, "start_character": page["start_character"] + len(f"## PDF page {n + 1}\n\n"),
                   "text": TEXT} for n, page in enumerate(metadata["pages"])]
        self.assertEqual([item["page_numbers"] for item in pdf_import.page_occurrences(chunks, TEXT, metadata)], [[1], [2]])
        boundary = metadata["pages"][0]["end_character"]
        both = {"chunk_index": 2, "start_character": boundary - 2, "text": result["markdown"][boundary-2:boundary+20]}
        self.assertEqual(pdf_import.page_occurrences([both], both["text"], metadata)[0]["page_numbers"], [1, 2])

    async def test_unicode_page_offsets_are_characters_and_tampering_is_rejected(self):
        result = await pdf_import.convert_pdf(text_pdf([TEXT]), PdfConfig())
        value = "## PDF page 1\n\n中文论文温度与压力 αβ。\n\n"
        metadata = {**result["metadata"], "markdown_sha256": hashlib.sha256(value.encode()).hexdigest(),
                    "pages": [{"page_number": 1, "start_character": 0, "end_character": len(value),
                               "text_sha256": hashlib.sha256(value.encode()).hexdigest()}]}
        pdf_import.validate_conversion(value, metadata)
        malformed = {**metadata, "pages": [{**metadata["pages"][0], "end_character": len(value.encode())}]}
        with self.assertRaises(ValueError):
            pdf_import.validate_conversion(value, malformed)


class PdfConfigAndExportTests(unittest.TestCase):
    def test_existing_configuration_loads_without_pdf_section(self):
        example = Path(__file__).resolve().parents[1] / "config.example.toml"
        text = example.read_text(encoding="utf-8")
        start, end = text.index("[pdf]"), text.index("[adapter]")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text[:start] + text[end:], encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.pdf, PdfConfig())
            self.assertEqual(config.knowledge.max_file_bytes, 8192)
            self.assertEqual(config.adapter.budgets["label"].input_tokens, 4096)

    def test_invalid_local_bounds_are_rejected(self):
        for fields in ({"seconds": float("nan")}, {"max_pages": True}, {"max_pages": 0},
                       {"max_file_bytes": 1}, {"max_markdown_bytes": 8 * 1024 * 1024}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                PdfConfig(**fields)

    def test_export_is_idempotent_and_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            source_id = "kb:" + "a" * 32
            target = pdf_import.publish_markdown(directory, source_id, "original draft")
            self.assertEqual(pdf_import.publish_markdown(directory, source_id, "original draft"), target)
            target.write_text("user review notes", encoding="utf-8")
            with self.assertRaises(GovernedError) as error:
                pdf_import.publish_markdown(directory, source_id, "original draft")
            self.assertEqual(error.exception.code, "pdf_export_conflict")
            self.assertEqual(target.read_text(), "user review notes")

    def test_export_does_not_overwrite_an_atomic_publication_race(self):
        with tempfile.TemporaryDirectory() as directory:
            source_id = "kb:" + "b" * 32
            def race(source, target):
                Path(target).write_bytes(b"concurrent user edit")
                raise FileExistsError()
            with patch.object(pdf_import.os, "link", side_effect=race), self.assertRaises(GovernedError):
                pdf_import.publish_markdown(directory, source_id, "draft")
            self.assertEqual((Path(directory) / "pdf_markdown" / ("b" * 32 + ".md")).read_bytes(), b"concurrent user edit")

    def test_fdopen_failure_closes_descriptor_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pdf_import.os, "fdopen", side_effect=OSError("synthetic failure")) as mocked:
                with self.assertRaises(OSError):
                    pdf_import.publish_markdown(directory, "kb:" + "c" * 32, "draft")
            descriptor = mocked.call_args.args[0]
            with self.assertRaises(OSError):
                pdf_import.os.fstat(descriptor)
            self.assertFalse(list((Path(directory) / "pdf_markdown").glob(".pdf-md-*.tmp")))
