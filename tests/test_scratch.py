import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.scratch import ScratchLog, canonical, verify


class FaultStream:
    """Proxy a temporary test stream with failures at explicit I/O boundaries."""

    def __init__(self, stream, fault):
        self.stream = stream
        self.fault = fault
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text):
        self.writes += 1
        if self.fault == "write_before":
            raise OSError("synthetic write failure")
        if self.fault in {"short_write", "write_after"}:
            count = len(text) // 2
            self.stream.write(text[:count])
            if self.fault == "write_after":
                raise OSError("synthetic partial write failure")
            return count
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()
        if self.fault == "flush":
            raise OSError("synthetic flush failure")


class ScratchTests(unittest.TestCase):
    @staticmethod
    def record(**changes):
        item = {"version": 1, "sequence": 1, "timestamp": "2026-09-30T00:00:00+00:00",
                "event": "observation", "fields": {"value": 1}, "previous_hash": "0" * 64}
        item.update(changes)
        item["hash"] = hashlib.sha256(canonical(item)).hexdigest()
        return item

    def test_records_survive_reopen_and_extend_same_hash_chain(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("http_request", trace_id="trace", call_id="call", payload={"input": "中文 source"})
            log.write("http_response", trace_id="trace", call_id="call", payload={"text": "visible reply"})
            second_hash = log.previous
            log.close()
            self.assertEqual(verify(path), (2, second_hash))
            log = ScratchLog(Path(directory))
            self.assertEqual(log.sequence, 2)
            log.write("call_end", status="completed")
            third_hash = log.previous
            log.close()
            self.assertEqual(verify(path), (3, third_hash))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[2]["previous_hash"], second_hash)
            self.assertEqual([row["sequence"] for row in rows], [1, 2, 3])
            self.assertEqual(rows[0]["fields"]["payload"]["input"], "中文 source")

    def test_payload_tampering_is_detected_and_prevents_append(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("http_request", payload={"text": "original"})
            log.close()
            item = json.loads(path.read_text(encoding="utf-8"))
            item["fields"]["payload"]["text"] = "edited"
            path.write_text(json.dumps(item) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(path)
            with self.assertRaises(ValueError):
                ScratchLog(Path(directory))

    def test_removed_middle_record_breaks_chain(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            for index in range(3):
                log.write("observation", index=index)
            log.close()
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                verify(path)

    def test_incomplete_write_is_detected_on_restart(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            path = log.path
            log.write("start", value=1)
            log.close()
            with path.open("a", encoding="utf-8") as stream:
                stream.write('{"event":')
            with self.assertRaises(ValueError):
                verify(path)

    def test_canonical_json_is_stable_and_rejects_nonfinite_values(self):
        self.assertEqual(canonical({"b": 2, "a": "中文"}), canonical({"a": "中文", "b": 2}))
        with self.assertRaises(ValueError):
            canonical({"number": float("nan")})

    def test_valid_utc_z_and_fractional_timestamps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            for timestamp in ["2026-09-30T00:00:00Z", "2026-09-30T00:00:00.123456+00:00"]:
                row = self.record(timestamp=timestamp)
                path.write_bytes(canonical(row) + b"\n")
                self.assertEqual(verify(path), (1, row["hash"]))

    def test_hash_valid_structural_errors_are_rejected(self):
        changes = [
            {"version": True}, {"version": 1.0}, {"version": 2},
            {"sequence": True}, {"sequence": 0}, {"sequence": 7}, {"sequence": "1"},
            {"event": None}, {"event": ""}, {"event": " whitespace "}, {"event": "control\n"},
            {"fields": []}, {"fields": None}, {"previous_hash": "invalid"},
            {"timestamp": None}, {"timestamp": "2026-09-30"},
            {"timestamp": "2026-09-30T00:00:00"}, {"timestamp": "2026-09-30 00:00:00+00:00"},
            {"timestamp": "2026-09-30T00:00:00+01:00"}, {"timestamp": "2026-02-30T00:00:00Z"},
            {"extra": "unversioned extension"},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            for changed in changes:
                with self.subTest(field=next(iter(changed))):
                    path.write_bytes(canonical(self.record(**changed)) + b"\n")
                    with self.assertRaisesRegex(ValueError, "scratch structure failed at record 1"):
                        verify(path)

    def test_missing_required_field_and_nonobject_row_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            row = self.record()
            for key in row:
                changed = dict(row)
                del changed[key]
                path.write_bytes(canonical(changed) + b"\n")
                with self.assertRaises(ValueError):
                    verify(path)
            path.write_bytes(b"[]\n")
            with self.assertRaises(ValueError):
                verify(path)

    def test_duplicate_keys_at_any_depth_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            original = canonical(self.record()).decode()
            for changed in [original.replace('"event":"observation"', '"event":"observation","event":"observation"'),
                            original.replace('"value":1', '"value":1,"value":1')]:
                path.write_text(changed + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "scratch structure failed at record 1"):
                    verify(path)

    def test_nonfinite_constants_and_numeric_overflow_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            original = canonical(self.record()).decode()
            for value in ["NaN", "Infinity", "-Infinity", "1e999"]:
                path.write_text(original.replace('"value":1', '"value":' + value) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "scratch structure failed at record 1"):
                    verify(path)

    def test_sequence_must_match_position_even_when_hashes_are_recomputed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            first = self.record()
            for sequence in [1, 3]:
                second = self.record(sequence=sequence, previous_hash=first["hash"])
                path.write_bytes(canonical(first) + b"\n" + canonical(second) + b"\n")
                with self.assertRaisesRegex(ValueError, "scratch structure failed at record 2"):
                    verify(path)

    def test_missing_newline_boundary_is_rejected_without_repair(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.jsonl"
            contents = canonical(self.record())
            path.write_bytes(contents)
            with self.assertRaises(ValueError):
                verify(path)
            self.assertEqual(path.read_bytes(), contents)

    def test_serialization_errors_do_not_advance_state_or_poison_writer(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            for value in [float("nan"), float("inf"), object()]:
                with self.assertRaises((ValueError, TypeError)):
                    log.write("synthetic_invalid", value=value)
                self.assertEqual(log.sequence, 0)
                self.assertEqual(log.previous, "0" * 64)
                self.assertFalse(log._poisoned)
                self.assertEqual(log.path.read_bytes(), b"")
            log.write("observation", value="valid after rejection")
            path, digest = log.path, log.previous
            log.close()
            self.assertEqual(verify(path), (1, digest))

    def test_event_errors_do_not_write_or_advance_state(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            for event in [None, 1, "", " spaced ", "control\n"]:
                with self.assertRaises(ValueError):
                    log.write(event)
                self.assertEqual(log.sequence, 0)
                self.assertEqual(log.path.read_bytes(), b"")
            log.write("valid")
            log.close()

    def test_serialized_snapshot_hash_matches_normalized_json_values(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            # JSON serializes tuple values as arrays and integral object keys as
            # strings; hash the same normalized representation that is appended.
            log.write("snapshot", values=(1, 2), metadata={2: "two", 10: "ten"})
            path, digest = log.path, log.previous
            log.close()
            self.assertEqual(verify(path), (1, digest))

    def test_write_and_flush_failures_latch_poison_and_preserve_bytes(self):
        for fault in ["write_before", "short_write", "write_after", "flush"]:
            with self.subTest(fault=fault), TemporaryDirectory() as directory:
                log = ScratchLog(Path(directory))
                log.write("original")
                sequence, previous = log.sequence, log.previous
                stream = FaultStream(log.stream, fault)
                log.stream = stream
                with self.assertRaises(OSError):
                    log.write("attempt")
                self.assertTrue(log._poisoned)
                self.assertEqual((log.sequence, log.previous), (sequence, previous))
                stream.stream.flush()
                contents = log.path.read_bytes()
                with self.assertRaisesRegex(OSError, "scratch writer unavailable"):
                    log.write("must_not_append")
                self.assertEqual(stream.writes, 1)
                self.assertEqual(log.path.read_bytes(), contents)
                log.close()
                if fault in {"short_write", "write_after"}:
                    with self.assertRaises(ValueError):
                        ScratchLog(Path(directory))
                    self.assertEqual(log.path.read_bytes(), contents)

    def test_fsync_failure_latches_even_after_complete_append(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            log.write("original")
            previous = log.previous
            with patch("indeces.scratch.os.fsync", side_effect=OSError("synthetic fsync failure")):
                with self.assertRaises(OSError):
                    log.write("complete_but_not_confirmed_durable")
            contents = log.path.read_bytes()
            self.assertTrue(log._poisoned)
            self.assertEqual((log.sequence, log.previous), (1, previous))
            with self.assertRaises(OSError):
                log.write("must_not_append")
            self.assertEqual(log.path.read_bytes(), contents)
            # A locally readable complete row does not prove its fsync succeeded.
            self.assertEqual(verify(log.path)[0], 2)
            log.close()

    def test_interrupt_during_io_poison_latches(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            with patch("indeces.scratch.os.fsync", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    log.write("interrupted")
            self.assertTrue(log._poisoned)
            with self.assertRaises(OSError):
                log.write("must_not_append")
            log.close()

    def test_closed_writer_does_not_advance_state(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory))
            log.close()
            with self.assertRaisesRegex(ValueError, "scratch log is closed"):
                log.write("must_not_append")
            self.assertEqual(log.sequence, 0)


if __name__ == "__main__":
    unittest.main()
