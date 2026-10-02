"""Immutable audit reuse preserves complete synthetic retrieval receipts."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.run_records import freeze_retrieval, validate_graph_audit, validate_retrieval
from indeces.scratch import CanonicalSnapshot, canonical
from tests.test_pdf_provenance import PDF_BYTES, conversion
from tests.test_run_records import RecordFixture, SOURCE_A


class RetrievalSnapshotTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def tearDown(self):
        self.teardown_fixture()

    def selected(self, query="alpha topic", event_id="synthetic-snapshot"):
        audit = {}
        records = self.graph.retrieve(self.scope, [], query, 2.0, event_id=event_id, audit=audit)
        return records, audit

    def freeze(self, records, audit, *, query="alpha topic", event_id="synthetic-snapshot"):
        return freeze_retrieval(self.store.db, self.scope, event_id, query, records, audit)

    def assert_native_equal(self, records, audit, **kwargs):
        original = self.freeze(records, audit, **kwargs)
        frozen = self.freeze(records, CanonicalSnapshot(audit), **kwargs)
        self.assertEqual(frozen, original)
        self.assertEqual(canonical(frozen), canonical(original))
        validate_retrieval(frozen)
        validate_graph_audit(frozen["graph_audit"], frozen)
        return frozen

    def test_complete_markdown_source_receipt_and_bytes_are_identical(self):
        raw = b"\xef\xbb\xbf" + ("alpha topic 中文资料，含 \\\"quoted\\\" 内容。\n" * 20).encode()
        self.seed(raw)
        records, audit = self.selected()
        # The snapshot must not depend on caller insertion order.
        audit = dict(reversed(list(audit.items())))
        frozen = self.assert_native_equal(records, audit)
        self.assertTrue(frozen["model_materials"][0]["text_truncated"])
        self.assertEqual(frozen["sources"][SOURCE_A]["original_file_bytes_sha256"],
                         hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(frozen["sources"][SOURCE_A]["normalized_text_sha256"],
                            frozen["sources"][SOURCE_A]["original_file_bytes_sha256"])

    def test_no_hit_snapshot_and_legacy_source_receipts_are_identical(self):
        self.graph.add(self.scope, "synthetic-legacy", "legacy-author", [{
            "text": "alpha topic legacy 中文", "quote": "alpha topic legacy 中文",
            "marks": ["alpha", "topic"]}], 1.0)
        records, audit = self.selected()
        frozen = self.assert_native_equal(records, audit)
        self.assertFalse(frozen["sources"]["synthetic-legacy"]["metadata_available"])
        records, audit = self.selected(query="unseen words", event_id="synthetic-no-hit")
        frozen = self.assert_native_equal(records, audit, query="unseen words", event_id="synthetic-no-hit")
        self.assertEqual(frozen["materials"], [])
        self.assertEqual(frozen["sources"], {})

    def test_pdf_source_page_bindings_and_bytes_remain_identical(self):
        markdown, metadata = conversion(["alpha topic synthetic 中文 page one",
                                         "alpha topic synthetic 中文 page two"])
        self.seed(markdown.encode(), name="paper.pdf")
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id=?",
                                  (metadata["original_pdf_sha256"], SOURCE_A))
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS knowledge_pdf_versions (
                source_id TEXT PRIMARY KEY,pdf_bytes BLOB NOT NULL,metadata_json TEXT,markdown_path TEXT)""")
            self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                                  (SOURCE_A, PDF_BYTES, json.dumps(metadata, ensure_ascii=False),
                                   "state/pdf-markdown/synthetic.md"))
        records, audit = self.selected()
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            frozen = self.assert_native_equal(records, audit)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(frozen["sources"][SOURCE_A]["pdf_conversion"], metadata)
        self.assertEqual(frozen["materials"][0]["pdf_page_numbers"], [1, 2])
        self.assertFalse(any("pdf_bytes" in query.casefold() for query in queries))

    def test_native_float_unicode_and_nested_order_bytes_are_identical(self):
        # This isolates serialization compatibility from graph arithmetic;
        # all receipt fields below are synthetic JSON values.
        audit = {"z": [1.0, -0.0, .3333, 1e200, True, None],
                 "中文": {"quoted": "雪 \\\" \\ newline\n", "a": {"nested": [1, 2, 3]}}}
        original = self.freeze([], audit)
        frozen = self.freeze([], CanonicalSnapshot(audit))
        self.assertEqual(frozen, original)
        self.assertEqual(canonical(frozen), canonical(original))
        validate_retrieval(frozen)

    def test_snapshot_isolated_from_input_and_decoded_copy_mutations(self):
        self.seed()
        records, audit = self.selected()
        original = self.freeze(records, audit)
        snapshot = CanonicalSnapshot(audit)
        audit["request"]["query"] = "caller changed"
        audit["observation"]["changed_edges"].clear()
        decoded = snapshot.decode()
        decoded["request"]["query"] = "decoded copy changed"
        frozen = self.freeze(records, snapshot)
        self.assertEqual(frozen, original)
        self.assertEqual(snapshot.decode(), original["graph_audit"])
        validate_retrieval(frozen)

    def test_public_validation_detects_returned_audit_and_digest_changes(self):
        self.seed()
        records, audit = self.selected()
        frozen = self.freeze(records, CanonicalSnapshot(audit))
        for mutate in (lambda value: value["graph_audit"]["request"].update(query="changed"),
                       lambda value: value.update(graph_audit_sha256="0" * 64)):
            changed = deepcopy(frozen)
            mutate(changed)
            with self.assertRaisesRegex(ValueError, "graph audit digest mismatch"):
                validate_retrieval(changed)

    def test_independent_digest_recalculation_detects_corrupt_snapshot_hash(self):
        self.seed()
        records, audit = self.selected()
        snapshot = CanonicalSnapshot(audit)
        # Normal callers cannot assign this frozen property. Simulate a
        # damaged internal object to prove freeze still independently hashes.
        object.__setattr__(snapshot, "sha256", "0" * 64)
        with self.assertRaisesRegex(ValueError, "graph audit digest mismatch"):
            self.freeze(records, snapshot)

    def test_nonobject_and_unrelated_snapshot_objects_are_rejected(self):
        for value in ([], "audit", 3.0, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "snapshot must contain an object"):
                    self.freeze([], CanonicalSnapshot(value))

        class SnapshotSubclass(CanonicalSnapshot):
            pass

        for value in (SimpleNamespace(content=b"{}", sha256="0" * 64, decode=lambda: {}),
                      SnapshotSubclass({})):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(TypeError):
                    self.freeze([], value)

    def test_plain_dict_nonnative_values_keep_original_freeze_semantics(self):
        audit = {"tuple": (1, "中文"), "numeric": {1: "one", 2: "two"}}
        frozen = self.freeze([], audit)
        self.assertEqual(frozen["graph_audit"], audit)
        self.assertIsInstance(frozen["graph_audit"]["tuple"], tuple)
        self.assertEqual(set(frozen["graph_audit"]["numeric"]), {1, 2})
        audit["numeric"][1] = "changed"
        self.assertEqual(frozen["graph_audit"]["numeric"][1], "one")
        validate_retrieval(frozen)

    def test_snapshot_removes_one_encode_but_retains_fresh_hash_validation(self):
        self.seed()
        records, audit = self.selected()
        snapshot = CanonicalSnapshot(audit)
        full_audit_encodes = []

        def counted(value):
            if isinstance(value, dict) and value.get("schema_version") == 1 and "observation" in value:
                full_audit_encodes.append(value)
            return canonical(value)

        with patch("indeces.run_records.canonical", side_effect=counted):
            self.freeze(records, audit)
        self.assertEqual(len(full_audit_encodes), 2)
        full_audit_encodes.clear()
        with patch("indeces.run_records.canonical", side_effect=counted):
            frozen = self.freeze(records, snapshot)
        self.assertEqual(len(full_audit_encodes), 1)
        self.assertIs(full_audit_encodes[0], frozen["graph_audit"])
        self.assertIsNot(frozen["graph_audit"], audit)


if __name__ == "__main__":
    unittest.main()
