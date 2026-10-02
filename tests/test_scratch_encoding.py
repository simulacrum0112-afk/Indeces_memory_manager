"""Byte compatibility with the 0.12.0 scratch writer; synthetic fixtures only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.scratch import ScratchLog, _decode, _validate, canonical, read_records, verify


NOW = datetime(2026, 10, 2, 12, 0, 0, 123456, tzinfo=timezone.utc)
ZERO_HASH = "0" * 64


def legacy_record(event, fields, *, sequence=1, previous=ZERO_HASH):
    """The original full-record encode/decode/encode/hash/encode algorithm."""
    item = {"version": 1, "sequence": sequence, "timestamp": NOW.isoformat(),
            "event": event, "fields": fields, "previous_hash": previous}
    content = canonical(item)
    item = _decode(content.decode("utf-8"))
    _validate(item, sequence, includes_hash=False)
    content = canonical(item)
    digest = hashlib.sha256(content).hexdigest()
    return canonical({**item, "hash": digest}) + b"\n", digest


def large_fields():
    return {"mode": "static", "nodes": [
        {"identity": f"synthetic-{index}", "marks": ("中文主题", f"topic-{index % 17}"),
         "source": {"version": index % 4, "locations": {10: "ten", 2: "two"}},
         "quote": 'Synthetic evidence \\"quoted\\"\n' * 8, "score": index / 7}
        for index in range(2000)
    ]}


class ScratchEncodingTests(unittest.TestCase):
    def assert_legacy_bytes(self, records):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            expected = b""
            previous = ZERO_HASH
            try:
                for sequence, (event, fields) in enumerate(records, 1):
                    record, previous = legacy_record(event, fields, sequence=sequence, previous=previous)
                    expected += record
                    log.write(event, **fields)
                    self.assertEqual(log.previous, previous)
                    self.assertEqual(log.sequence, sequence)
                self.assertEqual(log.path.read_bytes(), expected)
                self.assertEqual(verify(log.path), (len(records), previous))
            finally:
                log.close()

    def test_unicode_escapes_numeric_keys_tuples_floats_and_nested_values(self):
        self.assert_legacy_bytes([
            ("empty", {}),
            ("中文😀", {"unicode": "中文😀é\u2028", "escaped": '\\"\n\r\t\b\f\x00',
                       "hash": 'not a record hash, \\"hash\\": inside payload', "reserved": {"event": "nested"}}),
            ("numbers", {"numeric_keys": {10: "ten", 2: "two", -3: "negative", 0.25: "fraction"},
                         "floats": (0.0, -0.0, 1e-300, 1e300, 2.5), "integer": 10 ** 60,
                         "booleans": (True, False, None)}),
            ("nested", {"values": ({"z": ({2: ("two",)},), "a": []}, [[], {}, ()]),
                        "metadata": {False: "false", True: "true"}}),
        ])

    def test_large_graph_fixture_has_legacy_bytes_and_stable_golden_hash(self):
        fields = large_fields()
        expected, digest = legacy_record("memory_observation", fields)
        self.assertGreater(len(expected), 750000)
        # Freeze a deterministic synthetic fixture independently of the writer.
        self.assertEqual(digest, "f995e4f15587a1c25f6241b824d96b7d1c10997e23231f2ec69605942cbf2ba3")
        self.assert_legacy_bytes([("memory_observation", fields)])

    def test_decoding_freezes_caller_values_before_reusing_encoded_payload(self):
        fields = {"payload": {"rows": ["original", {10: "ten", 2: "two"}]}}
        expected, digest = legacy_record("snapshot", fields)
        original_canonical = canonical
        calls = 0

        def mutate_after_snapshot(value):
            nonlocal calls
            encoded = original_canonical(value)
            calls += 1
            if calls == 1:
                fields["payload"]["rows"][0] = "mutated by caller after encoding"
                fields["payload"]["rows"][1][2] = "changed"
            return encoded

        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            try:
                with patch("indeces.scratch.canonical", side_effect=mutate_after_snapshot):
                    log.write("snapshot", **fields)
                self.assertEqual(log.path.read_bytes(), expected)
                self.assertEqual(log.previous, digest)
                self.assertEqual(read_records(log.path)[0]["fields"]["payload"]["rows"][0], "original")
            finally:
                log.close()

    def test_rejected_inputs_match_legacy_errors_and_leave_writer_usable(self):
        cases = [
            (None, {}), (1, {}), ("", {}), (" spaced ", {}), ("control\n", {}),
            ("invalid", {"value": float("nan")}), ("invalid", {"value": float("inf")}),
            ("invalid", {"value": float("-inf")}), ("invalid", {"value": object()}),
            ("invalid", {"value": "\ud800"}),
            ("duplicate", {"value": {1: "number", "1": "string"}}),
            ("mixed_keys", {"value": {None: "none", 1: "number"}}),
        ]
        recursive = []
        recursive.append(recursive)
        cases.append(("recursive", {"value": recursive}))
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            try:
                for event, fields in cases:
                    with self.subTest(event=event, fields=fields):
                        try:
                            legacy_record(event, fields)
                        except (ValueError, TypeError, UnicodeError) as expected:
                            with self.assertRaises(type(expected)) as actual:
                                log.write(event, **fields)
                            self.assertEqual(str(actual.exception), str(expected))
                        else:
                            self.fail("invalid legacy fixture unexpectedly accepted")
                        self.assertEqual((log.sequence, log.previous), (0, ZERO_HASH))
                        self.assertFalse(log._poisoned)
                        self.assertEqual(log.path.read_bytes(), b"")
                log.write("valid_after_rejection", text="still usable")
                self.assertEqual(verify(log.path)[0], 1)
            finally:
                log.close()


if __name__ == "__main__":
    unittest.main()
