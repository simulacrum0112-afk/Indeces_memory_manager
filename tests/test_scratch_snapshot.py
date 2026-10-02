"""Frozen JSON scratch fields preserve the legacy bytes and failure contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timedelta
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.scratch import CanonicalSnapshot, ScratchLog, _decode, canonical, read_records, verify
from tests.test_scratch import FaultStream
from tests.test_scratch_encoding import NOW, ZERO_HASH, large_fields, legacy_record


class DuplicateKeys(dict):
    def items(self):
        return [("same", "first"), ("same", "second")]


class ScratchSnapshotTests(unittest.TestCase):
    def test_snapshot_normalizes_json_values_and_freezes_original_tree(self):
        original = {"tuple": (1, {10: "ten", 2: "two"}), "unicode": "中文😀",
                    "number": -0.0, "nested": [{"value": "original"}]}
        expected = canonical(_decode(canonical(original).decode("utf-8")))
        snapshot = CanonicalSnapshot(original)
        self.assertEqual(snapshot.content, expected)
        self.assertEqual(snapshot.sha256, hashlib.sha256(expected).hexdigest())
        original["nested"][0]["value"] = "caller mutation"
        self.assertEqual(snapshot.content, expected)
        first, second = snapshot.decode(), snapshot.decode()
        self.assertEqual(first["tuple"], [1, {"10": "ten", "2": "two"}])
        self.assertIsNot(first, second)
        self.assertIsNot(first["nested"], second["nested"])
        first["nested"][0]["value"] = "decoded mutation"
        self.assertEqual(second["nested"][0]["value"], "original")
        self.assertEqual(snapshot.decode()["nested"][0]["value"], "original")

    def test_snapshot_is_immutable_and_does_not_accept_raw_bytes_or_digest(self):
        snapshot = CanonicalSnapshot({"valid": True})
        for attribute, value in (("content", b'{"unchecked":NaN}'), ("sha256", ZERO_HASH)):
            with self.subTest(attribute=attribute), self.assertRaises(FrozenInstanceError):
                setattr(snapshot, attribute, value)
        with self.assertRaises(TypeError):
            CanonicalSnapshot(b'{"unchecked":NaN}')
        with self.assertRaises(TypeError):
            CanonicalSnapshot(content=b"{}", sha256=ZERO_HASH)
        # A supplied string remains a quoted JSON string, never parsed as JSON.
        raw_text = '{"duplicate":1,"duplicate":2,"value":NaN}'
        self.assertEqual(CanonicalSnapshot(raw_text).decode(), raw_text)

    def test_snapshot_rejects_duplicates_nonfinite_and_non_json_values(self):
        recursive = []
        recursive.append(recursive)
        for value in (DuplicateKeys(seed=True), {1: "one", "1": "duplicate"}, float("nan"),
                      float("inf"), {"bad": object()}, "\ud800", recursive):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError, UnicodeError)):
                CanonicalSnapshot(value)

    def test_snapshot_supports_all_json_tree_roots(self):
        for value in (None, True, 42, -0.0, "quoted\\\"\n中文😀", (), (1, {2: "two"})):
            with self.subTest(value=value):
                snapshot = CanonicalSnapshot(value)
                self.assertEqual(snapshot.content, canonical(_decode(canonical(value).decode("utf-8"))))

    def test_snapshot_writes_match_legacy_bytes_for_escapes_keys_and_multiple_payloads(self):
        payloads = [
            ("empty", {}, {}),
            ("中文😀", {"audit": {"quoted": '\\"\n\x00', "hash": 'fake ","hash":"payload',
                                    "nested": ({10: "ten", 2: "two"}, [True, None, -0.0])},
                        "scalar": '}{"event":"other","fields":{}'},
             {"trace_id": "synthetic", "metadata": {3: ("three",)}, "audit_sha256": ZERO_HASH}),
            ("snapshot_names", {'"key\n中文': [1e-300, 1e300], "hash": {"event": "inside fields"}},
             {"ordinary": (False, None)}),
        ]
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            expected, previous = b"", ZERO_HASH
            try:
                for sequence, (event, frozen_values, fields) in enumerate(payloads, 1):
                    snapshots = {key: CanonicalSnapshot(value) for key, value in frozen_values.items()}
                    record, previous = legacy_record(event, {**fields, **frozen_values},
                                                     sequence=sequence, previous=previous)
                    expected += record
                    log.write_snapshot(event, snapshots=snapshots, **fields)
                    self.assertEqual((log.sequence, log.previous), (sequence, previous))
                self.assertEqual(log.path.read_bytes(), expected)
                self.assertEqual(verify(log.path), (len(payloads), previous))
            finally:
                log.close()

    def test_large_snapshot_can_be_reused_without_reencoding_or_changing_bytes(self):
        values = large_fields()
        snapshot = CanonicalSnapshot(values)
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            expected, previous = b"", ZERO_HASH
            try:
                for sequence in (1, 2):
                    record, previous = legacy_record("memory_observation", {"audit": values, "trace_id": "synthetic"},
                                                     sequence=sequence, previous=previous)
                    expected += record
                    # The snapshot byte buffer is embedded without another tree
                    # decode; ordinary fields and record metadata remain strict.
                    with patch.object(CanonicalSnapshot, "decode", side_effect=AssertionError("unexpected snapshot decode")):
                        log.write_snapshot("memory_observation", snapshots={"audit": snapshot}, trace_id="synthetic")
                self.assertEqual(log.path.read_bytes(), expected)
                self.assertEqual(verify(log.path), (2, previous))
            finally:
                log.close()

    def test_rejected_snapshot_fields_do_not_write_advance_or_poison(self):
        class SnapshotSubclass(CanonicalSnapshot):
            pass

        snapshot = CanonicalSnapshot({"valid": True})
        cases = [
            (None, {}), ([], {}), ({1: snapshot}, {}), ({"audit": b'{"bad":NaN}'}, {}),
            ({"audit": SimpleNamespace(content=b"{}", sha256=ZERO_HASH)}, {}),
            ({"audit": SnapshotSubclass({"value": 1})}, {}),
            ({"audit": snapshot}, {"audit": "overlap"}),
            ({"audit": snapshot}, {"invalid": float("nan")}),
            ({"audit": snapshot}, {"invalid": DuplicateKeys(seed=True)}),
        ]
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            try:
                for snapshots, fields in cases:
                    with self.subTest(snapshots=snapshots), self.assertRaises((TypeError, ValueError)):
                        log.write_snapshot("snapshot", snapshots=snapshots, **fields)
                    self.assertEqual((log.sequence, log.previous), (0, ZERO_HASH))
                    self.assertEqual(log.path.read_bytes(), b"")
                    self.assertFalse(log._poisoned)
                log.write_snapshot("valid_after_rejection", snapshots={"audit": snapshot})
                self.assertEqual(read_records(log.path)[0]["fields"]["audit"], {"valid": True})
            finally:
                log.close()

    def test_ordinary_fields_freeze_before_append_and_invalid_events_match_legacy(self):
        fields = {"mutable": {"value": "original", "numeric_keys": {10: "ten", 2: "two"}}}
        snapshot = CanonicalSnapshot({"audit": True})
        expected, digest = legacy_record("snapshot", {**fields, "audit": snapshot.decode()})
        original_canonical = canonical
        calls = 0

        def mutate_after_snapshot(value):
            nonlocal calls
            encoded = original_canonical(value)
            calls += 1
            if calls == 1:
                fields["mutable"]["value"] = "caller changed after freeze"
            return encoded

        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            try:
                for event in (None, 1, "", " spaced ", "control\n"):
                    with self.subTest(event=event), self.assertRaises(ValueError):
                        log.write_snapshot(event, snapshots={"audit": snapshot})
                    self.assertFalse(log._poisoned)
                    self.assertEqual(log.path.read_bytes(), b"")
                with patch("indeces.scratch.canonical", side_effect=mutate_after_snapshot):
                    log.write_snapshot("snapshot", snapshots={"audit": snapshot}, **fields)
                self.assertEqual(log.path.read_bytes(), expected)
                self.assertEqual(log.previous, digest)
            finally:
                log.close()

    def test_snapshot_clock_failure_poisons_and_closed_writer_stays_closed(self):
        now = [NOW]
        snapshot = CanonicalSnapshot({"valid": True})
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: now[0])
            try:
                log.write_snapshot("first", snapshots={"audit": snapshot})
                state, contents = (log.sequence, log.previous), log.path.read_bytes()
                now[0] -= timedelta(seconds=1)
                with self.assertRaisesRegex(ValueError, "scratch clock moved backwards"):
                    log.write_snapshot("backwards", snapshots={"audit": snapshot})
                self.assertTrue(log._poisoned)
                self.assertEqual((log.sequence, log.previous), state)
                with self.assertRaisesRegex(OSError, "scratch writer unavailable"):
                    log.write("poison_applies_to_ordinary_write")
                self.assertEqual(log.path.read_bytes(), contents)
            finally:
                log.close()
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=lambda: NOW)
            log.close()
            with self.assertRaisesRegex(ValueError, "scratch log is closed"):
                log.write_snapshot("closed", snapshots={"audit": snapshot})
            self.assertFalse(log._poisoned)
            self.assertEqual(log.sequence, 0)

    def test_snapshot_write_flush_and_fsync_failures_poison_without_advancing(self):
        snapshot = CanonicalSnapshot({"valid": True})
        for fault in ("write_before", "short_write", "write_after", "flush", "fsync"):
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                log = ScratchLog(Path(directory), clock=lambda: NOW)
                try:
                    log.write("original")
                    state = (log.sequence, log.previous)
                    if fault != "fsync":
                        log.stream = FaultStream(log.stream, fault)
                    with patch("indeces.scratch.os.fsync", side_effect=OSError("synthetic fsync failure") if fault == "fsync" else None):
                        with self.assertRaises(OSError):
                            log.write_snapshot("attempt", snapshots={"audit": snapshot})
                    self.assertTrue(log._poisoned)
                    self.assertEqual((log.sequence, log.previous), state)
                    contents = log.path.read_bytes()
                    with self.assertRaisesRegex(OSError, "scratch writer unavailable"):
                        log.write_snapshot("must_not_append", snapshots={"audit": snapshot})
                    self.assertEqual(log.path.read_bytes(), contents)
                    if fault == "fsync":
                        self.assertEqual(verify(log.path)[0], 2)
                finally:
                    log.close()


if __name__ == "__main__":
    unittest.main()
