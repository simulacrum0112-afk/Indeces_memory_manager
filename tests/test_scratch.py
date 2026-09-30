import hashlib
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.scratch import ScratchLog, canonical, read_records, retention_checkpoint, verify


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


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


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


class RetentionTests(unittest.TestCase):
    NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

    @staticmethod
    def file(path, events, *, newline=b"\n"):
        previous = "0" * 64
        lines = []
        for sequence, (timestamp, fields) in enumerate(events, 1):
            row = ScratchTests.record(sequence=sequence, timestamp=timestamp.isoformat(), fields=fields,
                                      previous_hash=previous)
            previous = row["hash"]
            lines.append(canonical(row) + newline)
        contents = b"".join(lines)
        path.write_bytes(contents)
        return lines

    def test_exact_24_hour_boundary_and_original_suffix_preserved(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("observation", private_body="expires_at_exact_boundary", trace_id="partial")
            clock.now += timedelta(microseconds=1)
            log.write("observation", private_body="still_fresh", trace_id="partial")
            original = log.path.read_bytes().splitlines(keepends=True)
            original_head = log.previous
            clock.now = self.NOW + timedelta(hours=24)
            result = log.prune()
            self.assertEqual(result["records_removed"], 1)
            contents = log.path.read_bytes().splitlines(keepends=True)
            self.assertEqual(contents[1:], original[1:])
            self.assertNotIn(b"expires_at_exact_boundary", b"".join(contents))
            checkpoint = retention_checkpoint(log.path)
            self.assertEqual(checkpoint["removed_through_sequence"], 1)
            self.assertEqual(checkpoint["removed_head_hash"], json.loads(original[0])["hash"])
            self.assertEqual(checkpoint["partial_trace_ids"], ["partial"])
            self.assertEqual(verify(log.path), (1, original_head))
            log.write("after_prune")
            self.assertEqual([row["sequence"] for row in read_records(log.path)], [2, 3])
            self.assertEqual(read_records(log.path)[1]["previous_hash"], original_head)
            log.close()

    def test_startup_prunes_all_managed_files_and_uses_sequence_not_retained_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old = root / "2026-09-29.jsonl"
            current = root / "2026-09-30.jsonl"
            self.file(old, [(self.NOW - timedelta(hours=25), {"private_body": "fully_expired"})])
            original = self.file(current, [(self.NOW - timedelta(hours=24), {"private_body": "old"}),
                                           (self.NOW - timedelta(hours=1), {"private_body": "fresh"})])
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertFalse(old.exists())
            self.assertEqual(log.startup_retention["records_removed"], 2)
            self.assertEqual(log.startup_retention["files_removed"], 1)
            self.assertEqual(verify(current)[0], 1)
            self.assertEqual(log.sequence, 2)
            self.assertEqual(current.read_bytes().splitlines(keepends=True)[1:], original[1:])
            log.write("continued")
            self.assertEqual([row["sequence"] for row in read_records(current)], [2, 3])
            log.close()
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(log.sequence, 3)
            self.assertEqual(verify(current)[0], 2)
            log.close()

    def test_current_file_can_become_only_a_checkpoint_and_extend(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("private_expired", private_body="gone", trace_id="no_retained_events")
            old_head = log.previous
            clock.now += timedelta(hours=24)
            log.prune()
            self.assertEqual(verify(log.path), (0, old_head))
            self.assertEqual(read_records(log.path), [])
            self.assertEqual(retention_checkpoint(log.path)["partial_trace_ids"], [])
            self.assertNotIn(b"private_expired", log.path.read_bytes())
            log.write("new_after_empty")
            records = read_records(log.path)
            self.assertEqual(records[0]["sequence"], 2)
            self.assertEqual(records[0]["previous_hash"], old_head)
            log.close()

    def test_no_expired_events_leave_bytes_and_writer_unchanged(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=Clock(self.NOW))
            log.write("recent")
            contents, stream = log.path.read_bytes(), log.stream
            result = log.prune()
            self.assertEqual(result["files_changed"], 0)
            self.assertEqual(log.path.read_bytes(), contents)
            self.assertIs(log.stream, stream)
            self.assertIsNone(retention_checkpoint(log.path))
            log.close()

    def test_crlf_suffix_bytes_are_not_normalized_when_pruned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "2026-09-30.jsonl"
            lines = self.file(path, [(self.NOW - timedelta(hours=25), {"value": "old"}),
                                    (self.NOW - timedelta(hours=1), {"value": "fresh"})], newline=b"\r\n")
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertTrue(path.read_bytes().endswith(lines[1]))
            self.assertEqual(read_records(path)[0]["sequence"], 2)
            log.close()

    def test_cross_file_partial_ids_survive_removing_whole_expired_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old = root / "2026-09-29.jsonl"
            fresh = root / "2026-09-30.jsonl"
            self.file(old, [(self.NOW - timedelta(hours=25), {"trace_id": "split", "call_id": "call-split"}),
                            (self.NOW - timedelta(hours=25), {"trace_id": "expired-only", "call_id": "call-expired"})])
            original = self.file(fresh, [(self.NOW - timedelta(hours=1), {"trace_id": "split", "call_id": "call-split"}),
                                        (self.NOW - timedelta(hours=1), {"trace_id": "complete-fresh", "call_id": "call-fresh"})])
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertFalse(old.exists())
            checkpoint = retention_checkpoint(fresh)
            self.assertEqual(checkpoint["removed_through_sequence"], 0)
            self.assertEqual(checkpoint["removed_head_hash"], "0" * 64)
            self.assertEqual(checkpoint["partial_trace_ids"], ["split"])
            self.assertEqual(checkpoint["partial_call_ids"], ["call-split"])
            self.assertEqual(fresh.read_bytes().splitlines(keepends=True)[1:], original)
            self.assertEqual(verify(fresh)[0], 2)
            log.close()
            # A repeat startup has no expired prefix left but must retain the
            # existing local declaration for still-present split events.
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(retention_checkpoint(fresh)["partial_trace_ids"], ["split"])
            log.close()

    def test_partial_ids_are_removed_after_their_last_retained_event_expires(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            current = root / "2026-09-30.jsonl"
            self.file(current, [(self.NOW - timedelta(hours=25), {"trace_id": "split", "call_id": "split-call"}),
                                (self.NOW - timedelta(hours=23), {"trace_id": "split", "call_id": "split-call"}),
                                (self.NOW, {"trace_id": "fresh"})])
            clock = Clock(self.NOW)
            log = ScratchLog(root, clock=clock)
            self.assertEqual(retention_checkpoint(current)["partial_trace_ids"], ["split"])
            clock.now += timedelta(hours=1)
            log.prune()
            checkpoint = retention_checkpoint(current)
            self.assertEqual(checkpoint["partial_trace_ids"], [])
            self.assertEqual(checkpoint["partial_call_ids"], [])
            self.assertEqual(checkpoint["removed_through_sequence"], 2)
            self.assertEqual([row["fields"]["trace_id"] for row in read_records(current)], ["fresh"])
            log.close()

    def test_unmanaged_names_and_invalid_date_names_are_not_touched(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("notes.jsonl", "2026-02-30.jsonl", "2026-09-30.jsonl.backup", ".scratch-retention-not-owned.tmp"):
                (root / name).write_bytes(b"unknown_private_content")
            log = ScratchLog(root, clock=Clock(self.NOW))
            for name in ("notes.jsonl", "2026-02-30.jsonl", "2026-09-30.jsonl.backup", ".scratch-retention-not-owned.tmp"):
                self.assertEqual((root / name).read_bytes(), b"unknown_private_content")
            log.close()

    def test_reserved_orphan_temporaries_are_removed_without_reading_contents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            orphans = [root / (".scratch-retention-" + char * 32 + ".tmp") for char in ("a", "b")]
            for path in orphans:
                path.write_bytes(b"not JSON; possibly partial retained private text")
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                if path in orphans:
                    raise AssertionError("orphan contents must not be opened")
                return original_open(path, *args, **kwargs)
            with patch.object(Path, "open", guarded_open):
                log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(log.startup_retention["temporary_files_removed"], 2)
            self.assertTrue(all(not path.exists() for path in orphans))
            log.close()

    def test_replace_failure_preserves_original_file_and_poisons_writer(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("expires")
            original = log.path.read_bytes()
            clock.now += timedelta(hours=24)
            with patch("indeces.scratch.os.replace", side_effect=OSError("synthetic replace failure")) as replacement:
                with self.assertRaises(OSError):
                    log.prune()
            self.assertEqual(replacement.call_count, 1)
            self.assertTrue(log._poisoned)
            self.assertEqual(log.path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".scratch-retention-*.tmp")), [])
            with self.assertRaises(OSError):
                log.write("must_not_replay")
            log.close()

    def test_temporary_fsync_failure_does_not_replace_or_close_writer(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("expires")
            original = log.path.read_bytes()
            clock.now += timedelta(hours=24)
            with patch("indeces.scratch.os.fsync", side_effect=OSError("synthetic temp fsync failure")), \
                    patch("indeces.scratch.os.replace") as replacement:
                with self.assertRaises(OSError):
                    log.prune()
            replacement.assert_not_called()
            self.assertFalse(log.stream.closed)
            self.assertEqual(log.path.read_bytes(), original)
            self.assertTrue(log._poisoned)
            self.assertEqual(list(Path(directory).glob(".scratch-retention-*.tmp")), [])
            log.close()

    def test_temporary_short_write_is_rejected_before_replace(self):
        class ShortBinaryStream(FaultStream):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("expires")
            original = log.path.read_bytes()
            clock.now += timedelta(hours=24)
            original_open = Path.open
            faults = []
            def short_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if args and args[0] == "xb":
                    proxy = ShortBinaryStream(stream, "short_write")
                    faults.append(proxy)
                    return proxy
                return stream
            with patch.object(Path, "open", short_open), patch("indeces.scratch.os.replace") as replacement:
                with self.assertRaisesRegex(OSError, "incomplete scratch retention write"):
                    log.prune()
            replacement.assert_not_called()
            self.assertEqual(len(faults), 1)
            self.assertEqual(faults[0].writes, 1)
            self.assertEqual(log.path.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob(".scratch-retention-*.tmp")), [])
            self.assertTrue(log._poisoned)
            log.close()

    def test_cross_file_metadata_commits_before_expired_file_deletion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            clock = Clock(self.NOW)
            log = ScratchLog(root, clock=clock)
            log.write("kept_tail", trace_id="split")
            old = root / "2026-09-29.jsonl"
            self.file(old, [(self.NOW - timedelta(hours=25), {"trace_id": "split"})])
            deleted = []
            original_unlink = Path.unlink
            def guarded_unlink(path, *args, **kwargs):
                if path == old:
                    self.assertEqual(retention_checkpoint(log.path)["partial_trace_ids"], ["split"])
                    deleted.append(path)
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", guarded_unlink):
                log.prune()
            self.assertEqual(deleted, [old])
            log.close()

    def test_cross_file_rewrite_failure_does_not_delete_expired_source_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            log = ScratchLog(root, clock=Clock(self.NOW))
            log.write("kept_tail", trace_id="split")
            original = log.path.read_bytes()
            old = root / "2026-09-29.jsonl"
            self.file(old, [(self.NOW - timedelta(hours=25), {"trace_id": "split"})])
            old_bytes = old.read_bytes()
            with patch("indeces.scratch.os.replace", side_effect=OSError("synthetic metadata failure")):
                with self.assertRaises(OSError):
                    log.prune()
            self.assertEqual(old.read_bytes(), old_bytes)
            self.assertEqual(log.path.read_bytes(), original)
            self.assertTrue(log._poisoned)
            log.close()

    def test_reopen_failure_preserves_committed_retained_file_without_replaying(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("expired_body")
            clock.now += timedelta(hours=1)
            log.write("kept_body")
            kept_line = log.path.read_bytes().splitlines(keepends=True)[1]
            clock.now = self.NOW + timedelta(hours=24)
            original_open = Path.open
            reopened = []
            def guarded_open(path, *args, **kwargs):
                if path == log.path and args and args[0] == "a":
                    reopened.append(path)
                    raise OSError("synthetic reopen failure")
                return original_open(path, *args, **kwargs)
            with patch.object(Path, "open", guarded_open):
                with self.assertRaises(OSError):
                    log.prune()
            self.assertEqual(len(reopened), 1)
            self.assertTrue(log._poisoned)
            self.assertTrue(log.stream.closed)
            self.assertEqual(log.path.read_bytes().splitlines(keepends=True)[1:], [kept_line])
            self.assertEqual(verify(log.path)[0], 1)
            with self.assertRaises(OSError):
                log.write("must_not_replay")
            log.close()

    def test_all_files_are_validated_before_any_expired_file_is_deleted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old, corrupt = root / "2026-09-28.jsonl", root / "2026-09-29.jsonl"
            self.file(old, [(self.NOW - timedelta(hours=48), {"value": "valid expired"})])
            corrupt.write_bytes(b"not a complete record")
            original = old.read_bytes()
            with self.assertRaises(ValueError):
                ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(old.read_bytes(), original)
            self.assertEqual(corrupt.read_bytes(), b"not a complete record")

    def test_backward_record_time_rejected_before_deleting_expired_prefix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "2026-09-30.jsonl"
            self.file(path, [(self.NOW - timedelta(hours=25), {}), (self.NOW - timedelta(hours=26), {})])
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(path.read_bytes(), original)

    def test_clock_rollback_refuses_pruning_and_append_without_fabricating_timestamp(self):
        with TemporaryDirectory() as directory:
            clock = Clock(self.NOW)
            log = ScratchLog(Path(directory), clock=clock)
            log.write("actual_utc")
            original = log.path.read_bytes()
            clock.now -= timedelta(microseconds=1)
            with self.assertRaisesRegex(ValueError, "clock moved backwards"):
                log.write("must_not_clamp")
            self.assertEqual(log.path.read_bytes(), original)
            self.assertTrue(log._poisoned)
            with self.assertRaises(OSError):
                log.prune()
            self.assertTrue(log._poisoned)
            self.assertEqual(log.path.read_bytes(), original)
            log.close()

    def test_cross_file_declaration_failure_keeps_all_original_event_rows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first, second = root / "2026-09-29.jsonl", root / "2026-09-30.jsonl"
            first_lines = self.file(first, [(self.NOW - timedelta(hours=25), {"trace_id": "a"}),
                                            (self.NOW - timedelta(hours=1), {"trace_id": "b"})])
            second_lines = self.file(second, [(self.NOW - timedelta(hours=25), {"trace_id": "b"}),
                                              (self.NOW - timedelta(hours=1), {"trace_id": "a"})])
            original_replace = os.replace
            replacements = []
            def fail_second_replace(source, target):
                replacements.append(target)
                if len(replacements) == 2:
                    raise OSError("synthetic second declaration failure")
                return original_replace(source, target)
            with patch("indeces.scratch.os.replace", fail_second_replace):
                with self.assertRaises(OSError):
                    ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(replacements, [first, second])
            self.assertEqual(first.read_bytes().splitlines(keepends=True)[1:], first_lines)
            self.assertEqual(second.read_bytes().splitlines(keepends=True), second_lines)
            self.assertEqual(retention_checkpoint(first)["partial_trace_ids"], ["b"])
            self.assertTrue(retention_checkpoint(first)["cleanup_pending"])
            self.assertEqual(verify(first)[0], 2)
            # Restart safely completes the interrupted multi-file cleanup.
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual([row["fields"]["trace_id"] for row in read_records(first)], ["b"])
            self.assertEqual([row["fields"]["trace_id"] for row in read_records(second)], ["a"])
            self.assertEqual(retention_checkpoint(first)["partial_trace_ids"], ["b"])
            self.assertEqual(retention_checkpoint(second)["partial_trace_ids"], ["a"])
            self.assertFalse(retention_checkpoint(first)["cleanup_pending"])
            self.assertFalse(retention_checkpoint(second)["cleanup_pending"])
            log.close()

    def test_pending_header_allows_old_rows_but_completed_header_rejects_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "2026-09-30.jsonl"
            lines = self.file(path, [(self.NOW - timedelta(hours=25), {"trace_id": "split"}),
                                    (self.NOW - timedelta(hours=1), {"trace_id": "split"})])
            header = {"kind": "scratch_retention_checkpoint", "version": 1, "removed_through_sequence": 0,
                      "removed_head_hash": "0" * 64, "pruned_at": self.NOW.isoformat(),
                      "cutoff": (self.NOW - timedelta(hours=24)).isoformat(),
                      "partial_trace_ids": ["split"], "partial_call_ids": [], "cleanup_pending": True}
            header["hash"] = hashlib.sha256(canonical(header)).hexdigest()
            path.write_bytes(canonical(header) + b"\n" + b"".join(lines))
            self.assertEqual(verify(path)[0], 2)
            completed = {key: value for key, value in header.items() if key != "hash"}
            completed["cleanup_pending"] = False
            completed["hash"] = hashlib.sha256(canonical(completed)).hexdigest()
            path.write_bytes(canonical(completed) + b"\n" + b"".join(lines))
            with self.assertRaises(ValueError):
                verify(path)
            path.write_bytes(canonical(header) + b"\n" + b"".join(lines))
            log = ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(verify(path)[0], 1)
            self.assertFalse(retention_checkpoint(path)["cleanup_pending"])
            log.close()

    def test_future_event_refuses_startup_pruning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "2026-09-30.jsonl"
            self.file(path, [(self.NOW + timedelta(seconds=1), {})])
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "clock moved backwards"):
                ScratchLog(root, clock=Clock(self.NOW))
            self.assertEqual(path.read_bytes(), original)

    def test_checkpoint_tampering_missing_fields_and_middle_header_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "2026-09-30.jsonl"
            self.file(path, [(self.NOW - timedelta(hours=25), {}), (self.NOW, {})])
            log = ScratchLog(root, clock=Clock(self.NOW))
            log.close()
            original = path.read_bytes().splitlines(keepends=True)
            checkpoint = json.loads(original[0])
            changed = dict(checkpoint)
            changed["removed_through_sequence"] += 1
            path.write_bytes(canonical(changed) + b"\n" + original[1])
            with self.assertRaisesRegex(ValueError, "retention checkpoint"):
                verify(path)
            for key in checkpoint:
                changed = dict(checkpoint)
                del changed[key]
                path.write_bytes(canonical(changed) + b"\n" + original[1])
                with self.assertRaises(ValueError):
                    verify(path)
            path.write_bytes(original[1] + original[0])
            with self.assertRaises(ValueError):
                verify(path)

    def test_closed_writer_refuses_retention(self):
        with TemporaryDirectory() as directory:
            log = ScratchLog(Path(directory), clock=Clock(self.NOW))
            log.close()
            with self.assertRaisesRegex(ValueError, "scratch log is closed"):
                log.prune()

    def test_naive_test_clock_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "aware datetime"):
                ScratchLog(Path(directory), clock=Clock(self.NOW.replace(tzinfo=None)))

    def test_symlink_managed_log_and_reserved_temp_are_rejected_without_following(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root / "outside-private.txt"
            outside.write_bytes(b"must not modify")
            for name in ("2026-09-29.jsonl", ".scratch-retention-" + "a" * 32 + ".tmp"):
                link = root / name
                try:
                    link.symlink_to(outside)
                except (OSError, NotImplementedError):
                    self.skipTest("symlink creation is not permitted on this platform")
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    ScratchLog(root, clock=Clock(self.NOW))
                self.assertTrue(link.is_symlink())
                self.assertEqual(outside.read_bytes(), b"must not modify")
                link.unlink()

    def test_directory_link_is_rejected_without_creating_logs_in_its_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "linked-scratch"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlink creation is not permitted on this platform")
            with self.assertRaisesRegex(ValueError, "must not be a link"):
                ScratchLog(link, clock=Clock(self.NOW))
            self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
