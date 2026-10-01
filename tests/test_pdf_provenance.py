"""Synthetic provenance contracts, without parsing local papers or model calls."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import observer_data, run_records
from indeces.pdf_import import EXTRACTOR_VERSION, POLICY_VERSION, validate_conversion
from indeces.run_records import answer_record, validate_answer, validate_retrieval
from tests.test_run_records import RecordFixture, SOURCE_A


PDF_BYTES = b"%PDF-synthetic-provenance-archive-not-a-parser-fixture"


def conversion(texts):
    parts = [f"## PDF page {index}\n\n{value}\n\n" for index, value in enumerate(texts, 1)]
    markdown, pages, start = "".join(parts), [], 0
    for index, part in enumerate(parts, 1):
        pages.append({"page_number": index, "start_character": start, "end_character": start + len(part),
                      "text_sha256": hashlib.sha256(part.encode()).hexdigest()})
        start += len(part)
    metadata = {"schema_version": 1, "original_pdf_sha256": hashlib.sha256(PDF_BYTES).hexdigest(),
                "markdown_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
                "extractor": {"name": "pypdf", "version": EXTRACTOR_VERSION,
                              "policy_version": POLICY_VERSION, "mode": "plain"},
                "page_count": len(parts), "pages": pages,
                "warnings": ["pdf_math_tables_figures_not_reconstructed", "pdf_text_reading_order_unverified"]}
    validate_conversion(markdown, metadata, hashlib.sha256(PDF_BYTES).hexdigest())
    return markdown, metadata


class PdfProvenanceTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.observer_config = SimpleNamespace(state_dir=self.root / "state", scratch_dir=self.root / "scratch",
                                               knowledge_dir=self.root / "knowledge", discord=self.config.discord)
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_pdf_versions (
            source_id TEXT PRIMARY KEY,pdf_bytes BLOB NOT NULL,metadata_json TEXT,markdown_path TEXT)""")

    def tearDown(self):
        self.teardown_fixture()

    def seed_pdf(self, texts=None):
        markdown, metadata = conversion(texts or ["alpha topic 中文源文字，带 Unicode 页范围用于追溯。"])
        fact = self.seed(markdown.encode(), name="paper.pdf")
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id=?",
                                  (metadata["original_pdf_sha256"], SOURCE_A))
            self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                                  (SOURCE_A, PDF_BYTES, json.dumps(metadata, ensure_ascii=False), "state/pdf-markdown/synthetic.md"))
        return markdown, metadata, fact

    def test_freeze_binds_markdown_and_pdf_hash_without_reading_or_logging_blob(self):
        markdown, metadata, _ = self.seed_pdf()
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            retrieval = self.retrieval()
        finally:
            self.store.db.set_trace_callback(None)
        source = retrieval["sources"][SOURCE_A]
        self.assertEqual(source["pdf_conversion"], metadata)
        self.assertEqual(source["raw_text"], markdown)
        self.assertEqual(source["normalized_text_sha256"], metadata["markdown_sha256"])
        self.assertEqual(source["original_file_bytes_sha256"], metadata["original_pdf_sha256"])
        self.assertFalse(source["original_file_bytes_verifiable"])
        self.assertFalse(source["original_pdf_bytes_verifiable"])
        self.assertFalse(any("pdf_bytes" in query.casefold() for query in queries))
        self.assertNotIn(PDF_BYTES.decode(), json.dumps(retrieval))
        material = retrieval["materials"][0]
        self.assertEqual(material["pdf_page_numbers"], [1])
        self.assertEqual(material["pdf_page_occurrences"][0]["end_character"], len(markdown))
        self.assertEqual(material["model_payload"]["pdf_page_numbers"], [1])
        validate_retrieval(retrieval)

    def test_chunk_crossing_page_boundary_preserves_both_physical_pages(self):
        self.seed_pdf(["alpha topic first physical page content", "alpha topic second physical page content"])
        material = self.retrieval()["materials"][0]
        self.assertEqual(material["pdf_page_numbers"], [1, 2])
        self.assertEqual(material["pdf_page_occurrences"][0]["page_numbers"], [1, 2])

    def test_repeated_identical_chunks_keep_every_occurrence_not_a_unique_page_claim(self):
        repeated = "alpha topic 共享语句 repeated on two physical pages\n\n"
        markdown, metadata, _ = self.seed_pdf([repeated.strip(), repeated.strip()])
        chunks = []
        for page in metadata["pages"]:
            header = f"## PDF page {page['page_number']}\n\n"
            chunks.extend([(len(chunks), header, page["start_character"], ["heading"]),
                           (len(chunks) + 1, repeated, page["start_character"] + len(header), ["alpha", "topic"])])
        with self.store.db:
            self.store.db.execute("DELETE FROM knowledge_chunks WHERE source_id=?", (SOURCE_A,))
            self.store.db.executemany("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)",
                                     [(SOURCE_A, index, value, start, json.dumps(marks)) for index, value, start, marks in chunks])
        self.graph.deactivate_source(self.scope, SOURCE_A)
        self.graph.add(self.scope, SOURCE_A, "fixture", [{"text": value, "quote": value, "marks": marks}
                                                       for _, value, _, marks in chunks], 1.0)
        retrieval = self.retrieval()
        self.assertEqual(len(retrieval["materials"]), 1)
        material = retrieval["materials"][0]
        self.assertEqual(material["pdf_page_numbers"], [1, 2])
        self.assertEqual([item["chunk_index"] for item in material["pdf_page_occurrences"]], [1, 3])
        self.assertEqual(markdown[material["pdf_page_occurrences"][1]["start_character"]:material["pdf_page_occurrences"][1]["end_character"]], repeated)
        changed = deepcopy(retrieval)
        changed["materials"][0]["pdf_page_occurrences"].pop()
        with self.assertRaisesRegex(ValueError, "occurrences"):
            validate_retrieval(changed)

    def test_page_mapping_pdf_hash_and_model_page_tampering_rejected(self):
        self.seed_pdf()
        retrieval = self.retrieval()
        mutations = [lambda value: value["sources"][SOURCE_A]["pdf_conversion"]["pages"][0].update(end_character=1),
                     lambda value: value["sources"][SOURCE_A]["pdf_conversion"].update(original_pdf_sha256="f" * 64),
                     lambda value: value["materials"][0].update(pdf_page_numbers=[99]),
                     lambda value: value["materials"][0]["model_payload"].update(pdf_page_numbers=[99]),
                     lambda value: value["materials"][0].update(pdf_page_numbers=[True]),
                     lambda value: value["materials"][0]["model_payload"].update(pdf_page_numbers=[1.0]),
                     lambda value: value["materials"][0]["pdf_page_occurrences"][0].update(chunk_index=False),
                     lambda value: value["sources"][SOURCE_A].update(original_pdf_bytes_verifiable=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = deepcopy(retrieval)
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_retrieval(changed)

    def test_model_page_metadata_without_frozen_conversion_is_rejected(self):
        self.seed()
        retrieval = self.retrieval()
        retrieval["materials"][0]["model_payload"]["pdf_page_numbers"] = [4]
        with self.assertRaisesRegex(ValueError, "no frozen conversion"):
            validate_retrieval(retrieval)

    def test_pdf_cannot_be_downgraded_to_plain_text_by_removing_provenance(self):
        self.seed_pdf()
        retrieval = self.retrieval()
        source = retrieval["sources"][SOURCE_A]
        source.pop("pdf_conversion")
        source.pop("original_pdf_bytes_verifiable")
        material = retrieval["materials"][0]
        material.pop("pdf_page_numbers")
        material.pop("pdf_page_occurrences")
        material["model_payload"].pop("pdf_page_numbers")
        with self.assertRaisesRegex(ValueError, "lacks frozen conversion"):
            validate_retrieval(retrieval)

    def test_pdf_literal_citation_receipt_inherits_candidate_page_numbers(self):
        self.seed_pdf()
        retrieval = self.retrieval()
        answer = answer_record("The material says this. [M1]", retrieval)
        self.assertEqual(answer["citations"][0]["pdf_page_numbers"], [1])
        self.assertEqual(answer["semantic_support"], "not_evaluated")
        validate_answer(answer, retrieval)
        answer["citations"][0]["pdf_page_numbers"] = [42]
        with self.assertRaises(ValueError):
            validate_answer(answer, retrieval)

    def test_non_pdf_old_snapshot_has_no_new_provenance_fields(self):
        self.seed()
        retrieval = self.retrieval()
        self.assertNotIn("pdf_conversion", retrieval["sources"][SOURCE_A])
        self.assertNotIn("original_pdf_bytes_verifiable", retrieval["sources"][SOURCE_A])
        self.assertNotIn("pdf_page_numbers", retrieval["materials"][0])
        self.assertNotIn("pdf_page_numbers", answer_record("Text. [M1]", retrieval)["citations"][0])
        validate_retrieval(retrieval)

    def test_missing_duplicate_or_oversized_conversion_metadata_fails_freeze(self):
        _, metadata, _ = self.seed_pdf()
        duplicate = json.dumps(metadata).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 2')
        for encoded in (None, duplicate):
            with self.subTest(encoded=encoded):
                self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=? WHERE source_id=?", (encoded, SOURCE_A))
                self.store.db.commit()
                with self.assertRaises(ValueError):
                    self.retrieval(event_id="malformed-" + str(encoded is None))
        self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=? WHERE source_id=?", (json.dumps(metadata), SOURCE_A))
        self.store.db.commit()
        with patch.object(run_records, "MAX_PDF_METADATA_BYTES", 8), self.assertRaises(ValueError):
            self.retrieval(event_id="oversize")

    def test_pathological_metadata_encoding_and_nesting_are_rejected_safely(self):
        self.seed_pdf()
        for encoded in ('{"bad":"\\ud800"}', '{"bad":' + '[' * 1500 + '0' + ']' * 1500 + '}'):
            with self.subTest(encoded=encoded[:30]):
                self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=? WHERE source_id=?", (encoded, SOURCE_A))
                self.store.db.commit()
                with self.assertRaises(ValueError):
                    self.retrieval(event_id="malformed-" + str(len(encoded)))
                result = observer_data.source_details(self.observer_config, SOURCE_A)
                self.assertEqual(result["pdf_conversion_validation"], "invalid_or_unavailable")
                self.assertIsNone(result["pdf_conversion"])
                json.dumps(result, ensure_ascii=False).encode("utf-8")

    def test_frozen_snapshot_survives_later_archive_change_but_observer_reports_mismatch(self):
        self.seed_pdf()
        frozen = self.retrieval()
        self.store.db.execute("UPDATE knowledge_pdf_versions SET pdf_bytes=? WHERE source_id=?", (b"synthetic-modified-archive", SOURCE_A))
        self.store.db.commit()
        validate_retrieval(frozen)
        result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertEqual(result["pdf_conversion_validation"], "validated")
        self.assertFalse(result["pdf_archive"]["digest_matches_metadata"])
        self.assertEqual(result["pdf_archive"]["verification_status"], "digest_mismatch")
        self.assertIn("pdf_archive_digest_mismatch", result["warnings"])

    def test_observer_checks_archive_and_metadata_without_exposing_binary_or_mutating_database(self):
        _, metadata, _ = self.seed_pdf()
        self.store.db.commit()
        database = self.root / "state" / "memory.sqlite3"
        before = database.read_bytes()
        result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertEqual(result["pdf_conversion"], metadata)
        self.assertEqual(result["pdf_archive"]["sha256"], metadata["original_pdf_sha256"])
        self.assertTrue(result["pdf_archive"]["digest_matches_metadata"])
        self.assertTrue(result["pdf_archive"]["digest_matches_source_version"])
        self.assertEqual(result["pdf_conversion_validation"], "validated")
        self.assertNotIn("pdf_bytes", json.dumps(result))
        self.assertNotIn(PDF_BYTES.decode(), json.dumps(result))
        self.assertEqual(database.read_bytes(), before)

    def test_archive_size_cap_skips_blob_fetch_and_hash(self):
        self.seed_pdf()
        self.store.db.commit()
        with patch.object(observer_data, "MAX_PDF_ARCHIVE_BYTES", len(PDF_BYTES) - 1), \
                patch.object(observer_data.hashlib, "sha256", wraps=hashlib.sha256) as hash_call:
            result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertIsNone(result["pdf_archive"]["sha256"])
        self.assertIsNone(result["pdf_archive"]["digest_matches_metadata"])
        self.assertEqual(result["pdf_archive"]["verification_status"], "not_verified_size_limit")
        self.assertTrue(all(call.args[0] != PDF_BYTES for call in hash_call.call_args_list))

    def test_truncated_markdown_and_metadata_limits_are_explicit(self):
        self.seed_pdf()
        self.store.db.commit()
        with patch.object(observer_data, "MAX_SOURCE_CHARACTERS", 8):
            result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["pdf_conversion_validation"], "not_verified_markdown_truncated")
        with patch.object(observer_data, "MAX_PDF_METADATA_BYTES", 8):
            result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertIsNone(result["pdf_conversion"])
        self.assertEqual(result["pdf_conversion_validation"], "not_verified_metadata_limit")
        self.assertIn("pdf_conversion_metadata_size_limit", result["warnings"])

    def test_pending_pdf_without_conversion_metadata_is_not_fabricated(self):
        self.seed_pdf()
        self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=NULL WHERE source_id=?", (SOURCE_A,))
        self.store.db.execute("UPDATE knowledge_versions SET status='converting' WHERE source_id=?", (SOURCE_A,))
        self.store.db.commit()
        result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertIsNone(result["pdf_conversion"])
        self.assertEqual(result["pdf_conversion_validation"], "not_available_yet")
        self.assertEqual(result["pdf_archive"]["verification_status"], "metadata_unavailable")

    def test_published_pdf_missing_metadata_is_invalid_rather_than_pending(self):
        self.seed_pdf()
        self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=NULL WHERE source_id=?", (SOURCE_A,))
        self.store.db.commit()
        result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertEqual(result["pdf_conversion_validation"], "invalid_or_unavailable")
        self.assertIn("published_pdf_conversion_metadata_missing", result["warnings"])

    def test_missing_archive_row_or_table_is_explicit_and_freeze_fails(self):
        self.seed_pdf()
        for action in ("DELETE FROM knowledge_pdf_versions", "DROP TABLE knowledge_pdf_versions"):
            with self.subTest(action=action):
                self.store.db.execute(action)
                self.store.db.commit()
                result = observer_data.source_details(self.observer_config, SOURCE_A)
                self.assertFalse(result["pdf_archive"]["available"])
                self.assertEqual(result["pdf_archive"]["verification_status"], "missing_archive")
                self.assertEqual(result["pdf_conversion_validation"], "invalid_or_unavailable")
                self.assertIn("pdf_archive_or_conversion_provenance_missing", result["warnings"])
                with self.assertRaisesRegex(ValueError, "lacks frozen conversion"):
                    self.retrieval(event_id="missing-" + action.split()[0])

    def test_observer_corrupt_conversion_declaration_is_explicitly_invalid(self):
        _, metadata, _ = self.seed_pdf()
        metadata["pages"][0]["text_sha256"] = "f" * 64
        self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=? WHERE source_id=?",
                              (json.dumps(metadata), SOURCE_A))
        self.store.db.commit()
        result = observer_data.source_details(self.observer_config, SOURCE_A)
        self.assertEqual(result["pdf_conversion_validation"], "invalid_or_unavailable")
        self.assertIn("pdf_conversion_metadata_invalid_or_unavailable", result["warnings"])
        # The distinct archive hash comparison is still true; it does not
        # certify the corrupt Markdown/page declaration or paper semantics.
        self.assertTrue(result["pdf_archive"]["digest_matches_metadata"])


if __name__ == "__main__":
    unittest.main()
