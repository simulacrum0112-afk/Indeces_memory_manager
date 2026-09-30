from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from indeces import scratch


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def child_reader(path, ready, release, abandon=False):
    with scratch._open_reader(Path(path)):
        ready.set()
        if not release.wait(timeout=10):
            raise RuntimeError("synthetic child reader release timeout")
        if abandon:
            os._exit(0)


class ScratchObserverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def fixture(self, name="source"):
        clock = Clock()
        log = scratch.ScratchLog(self.root / name, clock=clock)
        self.addCleanup(log.close)
        log.write("observation", trace_id="synthetic-trace", value="synthetic-private-body")
        return log, clock

    def test_snapshot_rows_metadata_and_head_share_one_validated_read(self):
        log, clock = self.fixture()
        clock.now += timedelta(hours=1)
        log.write("observation", trace_id="synthetic-trace", value="kept-version")
        clock.now += timedelta(hours=23)
        log.prune()
        log.close()
        contents = log.path.read_bytes()
        with patch.object(scratch, "_open_reader", wraps=scratch._open_reader) as opened:
            snapshot = scratch.read_snapshot(log.path, max_bytes=len(contents))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(snapshot["records"], scratch.read_records(log.path))
        self.assertEqual(snapshot["checkpoint"], scratch.retention_checkpoint(log.path))
        self.assertEqual(snapshot["head"], scratch.verify(log.path)[1])
        self.assertEqual(snapshot["bytes_read"], len(contents))
        self.assertEqual(snapshot["records"][0]["sequence"], 2)
        self.assertEqual(snapshot["checkpoint"]["removed_through_sequence"], 1)
        self.assertEqual(log.path.read_bytes(), contents)

    def test_frozen_read_closes_handle_before_atomic_replace_and_preserves_read_bytes(self):
        log, _ = self.fixture()
        log.close()
        original = log.path.read_bytes()
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(b"replacement-body")
        with scratch._open_reader(log.path) as reader:
            frozen = reader.read()
            self.assertFalse(reader.closed)
        self.assertTrue(reader.closed)
        os.replace(replacement, log.path)
        self.assertEqual(frozen, original)
        self.assertEqual(log.path.read_bytes(), b"replacement-body")

    def test_retention_succeeds_while_inspector_holds_original_reader_open(self):
        log, clock = self.fixture()
        clock.now += timedelta(hours=1)
        log.write("observation", trace_id="synthetic-trace", value="kept-version")
        original = log.path.read_bytes()
        opened, release, writer_entered, writer_done = (threading.Event() for _ in range(4))
        result, errors, observed = [], [], []
        def inspect():
            try:
                with scratch._open_reader(log.path) as inspector:
                    observed.append(inspector.read())
                    opened.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("synthetic inspector timeout")
            except BaseException as error:
                errors.append(error)
        def prune():
            try:
                result.append(log.prune())
            except BaseException as error:
                errors.append(error)
            finally:
                writer_done.set()
        original_guard = scratch._access_guard
        @contextmanager
        def observed_guard(directory, *, write=False):
            if threading.current_thread().name == "synthetic-writer":
                writer_entered.set()
            with original_guard(directory, write=write):
                yield
        clock.now += timedelta(hours=23)
        inspector = threading.Thread(target=inspect, name="synthetic-inspector")
        writer = threading.Thread(target=prune, name="synthetic-writer")
        with patch.object(scratch, "_access_guard", side_effect=observed_guard):
            inspector.start()
            self.assertTrue(opened.wait(timeout=3))
            writer.start()
            try:
                self.assertTrue(writer_entered.wait(timeout=3))
                if os.name == "nt":
                    self.assertFalse(writer_done.is_set(), "Windows writer must wait for the reader")
            finally:
                release.set()
                inspector.join(timeout=3)
                writer.join(timeout=3)
        self.assertFalse(inspector.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [original])
        self.assertEqual(result[0]["records_removed"], 1)
        self.assertFalse(log._poisoned)
        log.write("after_retention", value="new-record")
        snapshot = scratch.read_snapshot(log.path, max_bytes=1024 * 1024)
        self.assertEqual([row["sequence"] for row in snapshot["records"]], [2, 3])
        self.assertEqual(snapshot["checkpoint"]["removed_through_sequence"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows CRT delete-sharing regression proof")
    def test_windows_delete_sharing_alone_is_insufficient_for_movefileex_replacement(self):
        path, replacement = self.root / "original.txt", self.root / "replacement.txt"
        path.write_bytes(b"old-version")
        replacement.write_bytes(b"new-version")
        with path.open("rb"):
            with self.assertRaises(PermissionError):
                os.replace(replacement, path)
        self.assertEqual(path.read_bytes(), b"old-version")
        self.assertEqual(replacement.read_bytes(), b"new-version")
        with scratch._shared_reader(path) as shared:
            with self.assertRaises(PermissionError):
                os.replace(replacement, path)
            self.assertEqual(shared.read(), b"old-version")
        os.replace(replacement, path)
        self.assertEqual(path.read_bytes(), b"new-version")

    def test_snapshot_cannot_mix_pre_replace_rows_with_post_replace_checkpoint(self):
        original, _ = self.fixture("original")
        original.close()
        updated, clock = self.fixture("updated")
        clock.now += timedelta(hours=1)
        updated.write("observation", trace_id="synthetic-trace", value="updated-version")
        clock.now += timedelta(hours=23)
        updated.prune()
        updated.close()
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(updated.path.read_bytes())
        original_reader = scratch._open_reader
        @contextmanager
        def replace_after_open(path, *, write=False):
            with original_reader(path, write=write) as reader:
                yield reader
            # Replacement can happen immediately after the handle/guard closes,
            # before strict validation of the already frozen bytes starts.
            os.replace(replacement, path)
        with patch.object(scratch, "_open_reader", side_effect=replace_after_open):
            before = scratch.read_snapshot(original.path, max_bytes=1024 * 1024)
        after = scratch.read_snapshot(original.path, max_bytes=1024 * 1024)
        self.assertIsNone(before["checkpoint"])
        self.assertEqual(before["records"][0]["fields"]["value"], "synthetic-private-body")
        self.assertEqual(after["checkpoint"]["removed_through_sequence"], 1)
        self.assertEqual(after["records"][0]["fields"]["value"], "updated-version")
        self.assertNotEqual(before["head"], after["head"])

    @unittest.skipUnless(os.name == "nt", "Windows named-mutex cross-process proof")
    def test_cross_process_reader_serializes_writer_with_canonical_directory_alias(self):
        log, clock = self.fixture()
        clock.now += timedelta(hours=1)
        log.write("observation", value="kept")
        clock.now += timedelta(hours=23)
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        alias = str(log.path).upper()
        reader = context.Process(target=child_reader, args=(alias, ready, release))
        writer_entered, writer_done = threading.Event(), threading.Event()
        errors, results = [], []
        original_guard = scratch._access_guard
        @contextmanager
        def observed_guard(directory, *, write=False):
            if threading.current_thread().name == "synthetic-writer":
                writer_entered.set()
            with original_guard(directory, write=write):
                yield
        def prune():
            try:
                results.append(log.prune())
            except BaseException as error:
                errors.append(error)
            finally:
                writer_done.set()
        reader.start()
        writer = threading.Thread(target=prune, name="synthetic-writer")
        try:
            self.assertTrue(ready.wait(timeout=5))
            with patch.object(scratch, "_access_guard", side_effect=observed_guard):
                writer.start()
                self.assertTrue(writer_entered.wait(timeout=3))
                self.assertFalse(writer_done.is_set())
                release.set()
                writer.join(timeout=5)
            reader.join(timeout=5)
        finally:
            release.set()
            if writer.ident is not None:
                writer.join(timeout=5)
            if reader.is_alive():
                reader.terminate()
                reader.join(timeout=5)
        self.assertEqual(reader.exitcode, 0)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["records_removed"], 1)
        self.assertFalse(log._poisoned)
        self.assertEqual(scratch.verify(log.path)[0], 1)

    @unittest.skipUnless(os.name == "nt", "Windows abandoned-mutex recovery proof")
    def test_aborted_observer_process_does_not_leave_writer_mutex_locked(self):
        log, clock = self.fixture()
        clock.now += timedelta(hours=1)
        log.write("observation", value="kept")
        clock.now += timedelta(hours=23)
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        reader = context.Process(target=child_reader, args=(str(log.path), ready, release, True))
        reader.start()
        try:
            self.assertTrue(ready.wait(timeout=5))
            release.set()
            reader.join(timeout=5)
        finally:
            if reader.is_alive():
                reader.terminate()
                reader.join(timeout=5)
        self.assertEqual(reader.exitcode, 0)
        result = log.prune()
        self.assertEqual(result["records_removed"], 1)
        self.assertFalse(log._poisoned)
        self.assertEqual(scratch.verify(log.path)[0], 1)

    def test_byte_limit_is_enforced_for_a_single_large_record_before_validation(self):
        log, _ = self.fixture()
        log.write("large_record", value="x" * 4096)
        log.close()
        contents = log.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "snapshot exceeds byte limit"):
            scratch.read_snapshot(log.path, max_bytes=1024)
        self.assertEqual(log.path.read_bytes(), contents)
        self.assertEqual(len(scratch.read_snapshot(log.path, max_bytes=len(contents))["records"]), 2)

    def test_actual_byte_count_includes_valid_json_whitespace_and_unicode_escapes(self):
        log, _ = self.fixture()
        log.write("observation", value="中文材料")
        log.close()
        rows = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        raw = ("\n".join(json.dumps(row, ensure_ascii=True) for row in rows) + "\n").encode("utf-8")
        log.path.write_bytes(raw)
        snapshot = scratch.read_snapshot(log.path, max_bytes=len(raw))
        canonical_bytes = sum(len(scratch.canonical(row)) + 1 for row in snapshot["records"])
        self.assertGreater(len(raw), canonical_bytes)
        self.assertEqual(snapshot["bytes_read"], len(raw))
        self.assertEqual(snapshot["records"], rows)

    @unittest.skipUnless(os.name == "nt", "Windows reader wait-limit proof")
    def test_busy_observer_read_is_bounded_and_does_not_poison_writer(self):
        log, clock = self.fixture()
        clock.now += timedelta(hours=1)
        log.write("observation", value="kept")
        clock.now += timedelta(hours=23)
        errors, results = [], []
        def inspect():
            try:
                results.append(scratch.read_snapshot(log.path, max_bytes=1024 * 1024))
            except BaseException as error:
                errors.append(error)
        reader = threading.Thread(target=inspect)
        with patch.object(scratch, "_READER_WAIT_MILLISECONDS", 10):
            with scratch._access_guard(log.path.parent, write=True):
                reader.start()
                reader.join(timeout=2)
                completed_while_busy = not reader.is_alive()
            reader.join(timeout=3)
        self.assertTrue(completed_while_busy)
        self.assertFalse(reader.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OSError)
        self.assertEqual(str(errors[0]), "scratch access is temporarily busy")
        self.assertFalse(log._poisoned)
        self.assertEqual(log.prune()["records_removed"], 1)

    def test_byte_limit_reads_at_most_limit_plus_one_even_for_malformed_file(self):
        path = self.root / "oversized.jsonl"
        path.write_bytes(b"x" * 100_000)
        original_reader = scratch._open_reader
        reads = []
        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def read(self, size):
                reads.append(size)
                return self.stream.read(size)
        @contextmanager
        def counted(path, *, write=False):
            with original_reader(path, write=write) as stream:
                yield Reader(stream)
        with patch.object(scratch, "_open_reader", side_effect=counted), \
                self.assertRaisesRegex(ValueError, "snapshot exceeds byte limit"):
            scratch.read_snapshot(path, max_bytes=1000)
        self.assertEqual(reads, [1001])

    def test_invalid_byte_limits_reject_before_opening_any_file(self):
        path = self.root / "missing.jsonl"
        for invalid in (True, False, 0, -1, 1.0, "1024"):
            with self.subTest(limit=invalid), patch.object(scratch, "_open_reader") as reader, \
                    self.assertRaisesRegex(ValueError, "invalid scratch snapshot byte limit"):
                scratch.read_snapshot(path, max_bytes=invalid)
            reader.assert_not_called()

    def test_trailing_uncommitted_append_is_rejected_without_returning_a_prefix(self):
        log, _ = self.fixture()
        log.close()
        with log.path.open("ab") as stream:
            stream.write(b'{"synthetic_incomplete_private_tail":')
        contents = log.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "scratch structure failed at record 2"):
            scratch.read_snapshot(log.path, max_bytes=len(contents))
        self.assertEqual(log.path.read_bytes(), contents)
        with self.assertRaises(ValueError):
            scratch.read_records(log.path)
        with self.assertRaises(ValueError):
            scratch.verify(log.path)

    def test_completed_tampered_record_still_fails_strict_verification(self):
        log, _ = self.fixture()
        log.close()
        row = json.loads(log.path.read_text(encoding="utf-8"))
        row["fields"]["value"] = "tampered"
        log.path.write_bytes(scratch.canonical(row) + b"\n")
        with self.assertRaisesRegex(ValueError, "scratch chain failed"):
            scratch.read_snapshot(log.path, max_bytes=1024 * 1024)

    def test_reader_always_closes_handle_when_consumer_raises(self):
        path = self.root / "fixture.txt"
        path.write_bytes(b"synthetic")
        with self.assertRaisesRegex(RuntimeError, "synthetic-consumer-failure"):
            with scratch._open_reader(path) as reader:
                raise RuntimeError("synthetic-consumer-failure")
        self.assertTrue(reader.closed)
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, path)
        self.assertEqual(path.read_bytes(), b"replacement")

    def test_readonly_snapshot_never_creates_a_missing_file(self):
        path = self.root / "missing.jsonl"
        snapshot = scratch.read_snapshot(path, max_bytes=1024)
        self.assertEqual(snapshot, {"records": [], "checkpoint": None, "head": "0" * 64, "bytes_read": 0})
        self.assertFalse(path.exists())

    def test_symlink_is_rejected_by_snapshot_and_opened_handle(self):
        target = self.root / "target.txt"
        target.write_bytes(b"unrelated-private-content")
        link = self.root / "linked.jsonl"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this host")
        with self.assertRaises(ValueError):
            scratch.read_snapshot(link, max_bytes=1024)
        with self.assertRaises((ValueError, OSError)):
            with scratch._open_reader(link):
                self.fail("reader followed a link")
        self.assertEqual(target.read_bytes(), b"unrelated-private-content")

    def test_directory_is_rejected_before_reading(self):
        directory = self.root / "directory.jsonl"
        directory.mkdir()
        with self.assertRaises(ValueError):
            scratch.read_snapshot(directory, max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
