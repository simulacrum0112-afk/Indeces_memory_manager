"""Canonical audit snapshots preserve recall values and independent checks."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import math
import unittest
from unittest.mock import patch

from indeces import run_records
from indeces.run_records import answer_record, digest, freeze_retrieval, validate_graph_audit, validate_retrieval
from indeces.scratch import canonical
from tests.test_run_records import RecordFixture, SOURCE_B


class ListSubclass(list):
    pass


class RetrievalFreezingTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.seed()
        self.seed(b"alpha topic second documented source", source_id=SOURCE_B, name="second.md")
        self.audit = {}
        self.records = self.graph.retrieve(self.scope, [], "alpha topic", 2.0,
                                          event_id="synthetic-freeze", audit=self.audit, ranking_mode="static")

    def tearDown(self):
        self.teardown_fixture()

    def freeze(self, audit=None):
        return freeze_retrieval(self.store.db, self.scope, "synthetic-freeze", "alpha topic",
                                self.records, self.audit if audit is None else audit)

    def baseline(self, audit=None):
        # Force the compatibility branch, which retains the original deepcopy
        # snapshot and the same canonical digest and independent validation.
        with patch.object(run_records, "_native_json_tree", return_value=False):
            return self.freeze(audit)

    def test_complete_output_and_evidence_match_original_deepcopy_snapshot(self):
        baseline = self.baseline()
        frozen = self.freeze()
        self.assertEqual(frozen, baseline)
        self.assertEqual(canonical(frozen), canonical(baseline))
        self.assertEqual(frozen["graph_audit_sha256"], digest(self.audit))
        validate_retrieval(frozen)
        validate_graph_audit(frozen["graph_audit"], frozen)
        self.assertEqual(answer_record("Documented answer [M1] and [M2].", frozen),
                         answer_record("Documented answer [M1] and [M2].", baseline))
        self.assertEqual(frozen["graph_audit"]["selection"]["ranked_candidates"],
                         baseline["graph_audit"]["selection"]["ranked_candidates"])
        self.assertEqual(frozen["graph_audit"]["observation"]["changed_edges"],
                         baseline["graph_audit"]["observation"]["changed_edges"])

    def test_native_audit_uses_json_snapshot_without_audit_deepcopy(self):
        with patch.object(run_records, "deepcopy", wraps=deepcopy) as copy:
            frozen = self.freeze()
        self.assertFalse(any(call.args[0] is self.audit for call in copy.call_args_list))
        self.assertIsNot(frozen["graph_audit"], self.audit)
        self.assertEqual(frozen["graph_audit"], deepcopy(self.audit))

    def test_original_mutation_cannot_change_frozen_graph_evidence_or_hash(self):
        frozen = self.freeze()
        expected = deepcopy(frozen)
        self.audit["request"]["query"] = "different synthetic query"
        self.audit["selection"]["ranked_candidates"][0]["evidence"][0]["effective_score"] += 10
        self.audit["observation"]["changed_edges"].clear()
        self.audit["selection"]["selected_record_ids"].reverse()
        self.assertEqual(frozen, expected)
        validate_retrieval(frozen)
        validate_graph_audit(frozen["graph_audit"], frozen)

    def test_unicode_float_and_sequence_values_and_canonical_hash_are_exact(self):
        floats = [-0.0, 0.0, 1.0, -1.25, 1e-200, 1e200, 1.2345678901234567]
        self.audit["synthetic_values"] = {
            "unicode": ["中文 α 😀", "e\u0301", "é", "日本語"],
            "floats": floats,
            "sequence": [None, False, True, 0, -1, 2 ** 100, {"z": [3, 1, 2], "a": "顺序"}],
        }
        baseline = self.baseline()
        frozen = self.freeze()
        self.assertEqual(frozen, baseline)
        self.assertEqual(canonical(frozen["graph_audit"]), canonical(self.audit))
        self.assertEqual(frozen["graph_audit_sha256"], hashlib.sha256(canonical(self.audit)).hexdigest())
        copied_values = frozen["graph_audit"]["synthetic_values"]
        self.assertEqual(copied_values["unicode"], self.audit["synthetic_values"]["unicode"])
        self.assertEqual([value.hex() for value in copied_values["floats"]], [value.hex() for value in floats])
        self.assertEqual(math.copysign(1, copied_values["floats"][0]), -1)
        self.assertEqual(copied_values["sequence"][-1]["z"], [3, 1, 2])

    def test_validator_independently_rehashes_frozen_audit_and_detects_tampering(self):
        with patch.object(run_records, "canonical", wraps=canonical) as encode:
            frozen = self.freeze()
        encoded_values = [call.args[0] for call in encode.call_args_list]
        self.assertTrue(any(value is self.audit for value in encoded_values))
        self.assertTrue(any(value is frozen["graph_audit"] for value in encoded_values))
        frozen["graph_audit"]["request"]["query"] = "tampered synthetic query"
        with self.assertRaisesRegex(ValueError, "graph audit digest mismatch"):
            validate_retrieval(frozen)

    def test_nonnative_json_compatible_values_keep_original_deepcopy_contract(self):
        values = [
            ("tuple", ("中文", [1, {"x": 2}]), tuple),
            ("integer_keys", {2: ["two"], 1: "one"}, dict),
            ("ordered_mapping", OrderedDict((("z", [2]), ("a", [1]))), OrderedDict),
            ("list_subclass", ListSubclass([3, {"x": [1, 2]}]), ListSubclass),
        ]
        for name, value, expected_type in values:
            with self.subTest(name=name):
                audit = deepcopy(self.audit)
                audit["synthetic_nonnative"] = value
                expected = self.baseline(audit)
                with patch.object(run_records, "deepcopy", wraps=deepcopy) as copy:
                    frozen = self.freeze(audit)
                self.assertTrue(any(call.args[0] is audit for call in copy.call_args_list))
                self.assertEqual(frozen, expected)
                self.assertIs(type(frozen["graph_audit"]["synthetic_nonnative"]), expected_type)
                self.assertEqual(frozen["graph_audit_sha256"], digest(audit))
                if name == "integer_keys":
                    self.assertEqual(set(frozen["graph_audit"]["synthetic_nonnative"]), {1, 2})
                if name == "ordered_mapping":
                    self.assertEqual(list(frozen["graph_audit"]["synthetic_nonnative"]), ["z", "a"])
                validate_retrieval(frozen)

    def test_nonnative_nested_mutation_does_not_change_compatibility_snapshot(self):
        nested = {"synthetic_nonnative": ("tuple", [1, {"x": [2, 3]}])}
        frozen = self.freeze(nested)
        expected = deepcopy(frozen)
        nested["synthetic_nonnative"][1][1]["x"].append(4)
        self.assertEqual(frozen, expected)
        validate_retrieval(frozen)

    def test_invalid_json_values_remain_rejected_without_weakening_canonical_rules(self):
        values = [(float("nan"), ValueError), (float("inf"), ValueError),
                  (float("-inf"), ValueError), ({1, 2}, TypeError), ("\ud800", UnicodeEncodeError)]
        for value, error_type in values:
            with self.subTest(error_type=error_type.__name__, value_type=type(value).__name__):
                audit = {"invalid": value}
                with self.assertRaises(error_type):
                    digest(audit)
                with self.assertRaises(error_type):
                    self.freeze(audit)

    def test_large_synthetic_nested_audit_preserves_every_entry_and_order(self):
        self.audit["synthetic_large"] = [
            {"index": index, "weight": index / 7.0, "context": ["中文", str(index)],
             "evidence": [index, index + 1], "enabled": bool(index % 2)}
            for index in range(2000)
        ]
        expected = self.baseline()
        frozen = self.freeze()
        self.assertEqual(frozen, expected)
        self.assertEqual([row["index"] for row in frozen["graph_audit"]["synthetic_large"]], list(range(2000)))
        validate_retrieval(frozen)


if __name__ == "__main__":
    unittest.main()
