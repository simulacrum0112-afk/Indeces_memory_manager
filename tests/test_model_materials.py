"""Model views stay small while complete synthetic graph evidence is frozen."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import unittest

from indeces.memory import MemoryGraph
from indeces.run_records import (MODEL_PROJECTION, answer_record, digest, freeze_retrieval,
                                validate_graph_audit, validate_retrieval)
from indeces.scratch import canonical
from tests import test_memory_selection_performance as memory_selection_fixture
from tests.test_pdf_provenance import PDF_BYTES, conversion
from tests.test_run_records import RecordFixture, SOURCE_A


PROJECTION_KEYS = {
    "citation_id", "citation_marker", "id", "source_id", "scope", "text", "text_truncated",
    "quote", "marks", "direct_marks", "expanded_marks", "ranking_mode", "weight_basis",
    "static_score", "dynamic_score", "ranking_score",
}


class ModelMaterialTests(unittest.TestCase):
    def receipt(self, count=5, *, mode="static", query="alpha beta gamma theta"):
        with closing(sqlite3.connect(":memory:")) as db:
            graph = MemoryGraph(db)
            facts = [{"text": "alpha beta gamma theta synthetic source " + str(index),
                      "quote": "alpha beta gamma theta synthetic source " + str(index),
                      "marks": ["alpha", "beta", "gamma", "theta"]}
                     for index in range(count)]
            graph.add("synthetic", "synthetic-source", "author", facts, 1.0)
            audit = {}
            selected = graph.retrieve("synthetic", [], query, 2.0, event_id="synthetic-event",
                                      audit=audit, ranking_mode=mode)
            original_records, original_audit = deepcopy(selected), canonical(audit)
            before = memory_selection_fixture.MemorySelectionPerformanceTests.database_state(db)
            receipt = freeze_retrieval(db, "synthetic", "synthetic-event", query, selected, audit)
            self.assertEqual(selected, original_records)
            self.assertEqual(canonical(receipt["graph_audit"]), original_audit)
            self.assertEqual(memory_selection_fixture.MemorySelectionPerformanceTests.database_state(db), before)
        return receipt, original_records

    @staticmethod
    def legacy(receipt, selected):
        legacy = deepcopy(receipt)
        del legacy["model_projection"]
        legacy["model_materials"] = []
        for index, (material, record) in enumerate(zip(legacy["materials"], selected), 1):
            payload = deepcopy(record)
            payload.update(citation_id=f"M{index}", citation_marker=f"[M{index}]")
            if "pdf_page_numbers" in material:
                payload["pdf_page_numbers"] = deepcopy(material["pdf_page_numbers"])
            material["model_payload"] = payload
            legacy["model_materials"].append(payload)
        return legacy

    def assert_valid(self, receipt):
        validate_retrieval(receipt)
        validate_graph_audit(receipt["graph_audit"], receipt)

    def assert_both_reject(self, receipt):
        with self.assertRaises(ValueError):
            validate_retrieval(receipt)
        with self.assertRaises(ValueError):
            validate_graph_audit(receipt["graph_audit"], receipt)

    def test_support_scale_is_absent_from_model_view_and_complete_in_audit(self):
        sizes = []
        for count in (100, 200):
            with self.subTest(count=count):
                receipt, selected = self.receipt(count)
                self.assertEqual(receipt["version"], 1)
                self.assertEqual(receipt["model_projection"], MODEL_PROJECTION)
                self.assertEqual(len(receipt["model_materials"]), 3)
                self.assert_valid(receipt)
                for material, payload, original in zip(receipt["materials"], receipt["model_materials"], selected):
                    self.assertEqual(set(payload), PROJECTION_KEYS)
                    self.assertNotIn("evidence", payload)
                    self.assertEqual(payload["quote"], material["quote"])
                    self.assertEqual(payload["direct_marks"], original["direct_marks"])
                    self.assertEqual(payload["expanded_marks"], original["expanded_marks"])
                    self.assertTrue(all(len(edge["source_record_ids"]) == count for edge in original["evidence"]))
                self.assertTrue(all(len(edge["source_record_ids"]) == count
                                    for edge in receipt["graph_audit"]["selection"]["edge_statistics"]))
                sizes.append(len(canonical(receipt["model_materials"])))
                legacy = self.legacy(receipt, selected)
                self.assertGreater(len(canonical(legacy["model_materials"])), sizes[-1] * 5)
                self.assertEqual(answer_record("Synthetic assertion [M1]", receipt),
                                 answer_record("Synthetic assertion [M1]", legacy))
        self.assertLess(max(sizes), 2000)
        self.assertLess(abs(sizes[1] - sizes[0]), 50)

    def test_projection_identity_text_marks_and_rank_tampering_are_rejected(self):
        original, _ = self.receipt()
        edits = {
            "citation_id": "M99", "citation_marker": "[M99]", "id": -1,
            "source_id": "other-source", "scope": "other-scope", "text": "forged",
            "text_truncated": True, "quote": "forged", "marks": ["forged"],
            "direct_marks": [], "expanded_marks": ["forged"], "ranking_mode": "dynamic",
            "weight_basis": "dynamic_or_static", "static_score": 99.0,
            "dynamic_score": 99.0, "ranking_score": 99.0,
        }
        for key, value in edits.items():
            with self.subTest(field=key):
                changed = deepcopy(original)
                changed["model_materials"][0][key] = value
                self.assert_both_reject(changed)

    def test_missing_extra_and_type_changed_projection_fields_are_rejected(self):
        original, _ = self.receipt()
        for key in sorted(PROJECTION_KEYS):
            with self.subTest(missing=key):
                changed = deepcopy(original)
                del changed["model_materials"][0][key]
                self.assert_both_reject(changed)
        for key in ("evidence", "author_id", "source_record_ids"):
            with self.subTest(extra=key):
                changed = deepcopy(original)
                changed["model_materials"][0][key] = []
                self.assert_both_reject(changed)
        changed = deepcopy(original)
        changed["model_materials"][0]["static_score"] = int(changed["model_materials"][0]["static_score"])
        self.assert_both_reject(changed)

    def test_unknown_or_removed_projection_marker_cannot_downgrade_compact_view(self):
        original, _ = self.receipt()
        for value in (None, "", "citation_material_v99", 1):
            with self.subTest(marker=value):
                changed = deepcopy(original)
                changed["model_projection"] = value
                self.assert_both_reject(changed)
        changed = deepcopy(original)
        del changed["model_projection"]
        self.assert_both_reject(changed)

    def test_legacy_complete_evidence_and_independent_rank_binding_remain_valid(self):
        for mode in ("static", "dynamic"):
            with self.subTest(mode=mode):
                receipt, selected = self.receipt(mode=mode)
                legacy = self.legacy(receipt, selected)
                self.assert_valid(legacy)
                for key, value in (("evidence", []), ("direct_marks", []), ("expanded_marks", ["forged"]),
                                   ("static_score", 999.0), ("dynamic_score", 999.0), ("ranking_score", 999.0)):
                    changed = deepcopy(legacy)
                    changed["model_materials"][0][key] = value
                    self.assert_both_reject(changed)

    def test_legacy_dynamic_receipt_before_explicit_ranking_mode_remains_valid(self):
        receipt, selected = self.receipt(mode="dynamic")
        legacy = self.legacy(receipt, selected)
        audit = legacy["graph_audit"]
        for key in ("ranking_mode", "weight_basis"):
            audit["selection"].pop(key)
            for payload in legacy["model_materials"]:
                payload.pop(key)
        audit["durable_payload_sha256"] = digest({key: value for key, value in audit.items()
                                                   if key != "durable_payload_sha256"})
        legacy["graph_audit_sha256"] = digest(audit)
        self.assert_valid(legacy)

    def test_no_hit_projection_is_empty_and_rejects_unknown_marker(self):
        receipt, _ = self.receipt(query="unseen synthetic terms")
        self.assert_valid(receipt)
        self.assertEqual(receipt["model_materials"], [])
        self.assertEqual(receipt["materials"], [])
        receipt["model_projection"] = "unknown"
        self.assert_both_reject(receipt)


class PDFModelMaterialTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def tearDown(self):
        self.teardown_fixture()

    def test_projection_preserves_physical_pages_and_full_conversion_evidence(self):
        markdown, metadata = conversion(["alpha topic synthetic page one", "alpha topic synthetic page two"])
        self.seed(markdown.encode(), name="synthetic.pdf")
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id=?",
                                  (metadata["original_pdf_sha256"], SOURCE_A))
            self.store.db.execute("CREATE TABLE IF NOT EXISTS knowledge_pdf_versions "
                                  "(source_id TEXT PRIMARY KEY,pdf_bytes BLOB NOT NULL,metadata_json TEXT,markdown_path TEXT)")
            self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                                  (SOURCE_A, PDF_BYTES, json.dumps(metadata), "synthetic.md"))
        receipt = self.retrieval()
        validate_retrieval(receipt)
        validate_graph_audit(receipt["graph_audit"], receipt)
        payload = receipt["model_materials"][0]
        self.assertEqual(set(payload), PROJECTION_KEYS | {"pdf_page_numbers"})
        self.assertEqual(payload["pdf_page_numbers"], [1, 2])
        self.assertEqual(receipt["sources"][SOURCE_A]["pdf_conversion"], metadata)
        self.assertTrue(receipt["materials"][0]["pdf_page_occurrences"])
        changed = deepcopy(receipt)
        changed["model_materials"][0]["pdf_page_numbers"] = [99]
        with self.assertRaises(ValueError):
            validate_retrieval(changed)
        with self.assertRaises(ValueError):
            validate_graph_audit(changed["graph_audit"], changed)


if __name__ == "__main__":
    unittest.main()
