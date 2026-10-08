"""Bounded local ledger replacement recovery using synthetic files and providers."""
from __future__ import annotations

from copy import deepcopy
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from indeces.active_requery import _atomic_json
from indeces.contracts import GovernedError
from tests import test_requery_ledger_io as io_fixtures

FaultStream = io_fixtures.FaultStream
os_fault = io_fixtures.os_fault


class AtomicRecoveryTests(unittest.TestCase):
    """A recovery can rename only the caller's unchanged, closed snapshot."""

    def setUp(self):
        # Exercise the Windows policy on both supported CI platforms; the
        # process/kernel lease continues to use the actual host OS.
        platform = patch("indeces.ledger_io._WINDOWS_RECOVERY", True)
        platform.start()
        self.addCleanup(platform.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "synthetic-ledger.json"
        self.target.write_text('{"previous":true}\n', encoding="utf-8")
        self.previous = self.target.read_bytes()
        self.pending = self.target.with_name(self.target.name + ".pending")

    def is_owned_pending(self, path):
        return path.parent == self.target.parent and path.name.endswith(".pending") and path != self.pending

    def test_exact_windows_denial_replaces_same_closed_snapshot_without_rewriting(self):
        original_open, original_replace, original_fsync = Path.open, Path.replace, os.fsync
        opened, replacements, synchronized, snapshots = [], [], [], []

        def track_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if self.is_owned_pending(path) and args and args[0] in {"w", "x"}:
                opened.append(stream)
            return stream

        def track_fsync(fd):
            synchronized.append(fd)
            return original_fsync(fd)

        def transient_denial(path, destination):
            replacements.append(path)
            self.assertEqual(destination, self.target)
            self.assertTrue(opened and all(stream.closed for stream in opened))
            snapshots.append(path.read_bytes())
            if len(replacements) == 1:
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "open", track_open), patch.object(Path, "replace", transient_denial), \
                patch("os.fsync", track_fsync):
            recovered = _atomic_json(self.target, {"known_usage": {"input_tokens": 64, "output_tokens": 32}})
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(synchronized), 1)
        self.assertEqual(len(replacements), 2)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         {"known_usage": {"input_tokens": 64, "output_tokens": 32}})
        self.assertEqual(recovered["first_failure"], {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(recovered["replace_attempts"], 2)
        self.assertGreaterEqual(recovered["intentional_wait_seconds"], 0)
        self.assertLessEqual(recovered["intentional_wait_seconds"], 0.03)
        self.assertFalse(self.pending.exists())

    def test_permanent_exact_denial_has_three_attempt_ceiling_and_first_cause(self):
        observed = []

        def denied(path, destination):
            observed.append(path.read_bytes())
            raise os_fault(13, 5)

        with patch.object(Path, "replace", denied), self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "replacement"})
        self.assertEqual(len(observed), 3)
        self.assertTrue(all(value == observed[0] for value in observed))
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(self.target.read_bytes(), self.previous)

    def test_third_attempt_success_has_one_snapshot_and_bounded_point_zero_three_wait(self):
        original_replace, snapshots = Path.replace, []

        def third_attempt_succeeds(path, destination):
            snapshots.append(path.read_bytes())
            if len(snapshots) < 3:
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "replace", third_attempt_succeeds):
            recovered = _atomic_json(self.target, {"synthetic": "third-attempt"})
        self.assertEqual(len(snapshots), 3)
        self.assertTrue(all(value == snapshots[0] for value in snapshots))
        self.assertEqual(recovered["replace_attempts"], 3)
        self.assertEqual(recovered["intentional_wait_seconds"], 0.03)
        self.assertEqual(recovered["first_failure"], {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"synthetic": "third-attempt"})

    def test_different_second_error_stops_and_preserves_first_denial(self):
        observed = []

        def denied(path, destination):
            observed.append(path.read_bytes())
            raise os_fault(13, 5) if len(observed) == 1 else os_fault(2, 2)

        with patch.object(Path, "replace", denied), self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "replacement"})
        self.assertEqual(len(observed), 2)
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(self.target.read_bytes(), self.previous)

    def test_other_error_codes_have_no_local_retry(self):
        for errno, winerror in ((13, 32), (5, 5), (2, 2), (13, None), (True, 5), (13, True)):
            with self.subTest(errno=errno, winerror=winerror):
                error = os_fault(errno, winerror)
                with patch.object(Path, "replace", side_effect=error) as rename, \
                        self.assertRaises(GovernedError):
                    _atomic_json(self.target, {"synthetic": "replacement"})
                self.assertEqual(rename.call_count, 1)
                self.assertEqual(self.target.read_bytes(), self.previous)
                # Retained owned pending data can never be a reason to accept foreign bytes.
                if self.pending.exists():
                    self.pending.unlink()

    def test_non_windows_policy_does_not_retry_even_exact_windows_shaped_error(self):
        with patch("indeces.ledger_io._WINDOWS_RECOVERY", False), \
                patch.object(Path, "replace", side_effect=os_fault(13, 5)) as rename, \
                self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "replacement"})
        self.assertEqual(rename.call_count, 1)
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(self.target.read_bytes(), self.previous)

    def test_explicitly_disabled_recovery_has_one_attempt(self):
        with patch.object(Path, "replace", side_effect=os_fault(13, 5)) as rename, \
                self.assertRaises(GovernedError):
            _atomic_json(self.target, {"synthetic": "replacement"}, allow_recovery=False)
        self.assertEqual(rename.call_count, 1)
        self.assertEqual(self.target.read_bytes(), self.previous)

    def test_write_flush_fsync_and_close_exact_denials_never_retry_replace(self):
        original_open = Path.open
        for operation in ("write", "flush", "fsync", "close"):
            with self.subTest(operation=operation):
                streams = []

                def broken_open(path, *args, **kwargs):
                    stream = original_open(path, *args, **kwargs)
                    if self.is_owned_pending(path) and args and args[0] in {"w", "x"}:
                        stream = FaultStream(stream, operation, os_fault(13, 5))
                        streams.append(stream)
                    return stream

                with patch.object(Path, "open", broken_open), patch.object(Path, "replace") as rename, \
                        patch("os.fsync", side_effect=os_fault(13, 5) if operation == "fsync" else None), \
                        self.assertRaises(GovernedError) as caught:
                    _atomic_json(self.target, {"synthetic": "replacement"})
                self.assertEqual(caught.exception.ledger_io_failure["operation"], operation)
                self.assertEqual(rename.call_count, 0)
                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].stream.closed)
                self.assertEqual(streams[0].close_count, 1)
                self.assertEqual(self.target.read_bytes(), self.previous)
                if self.pending.exists():
                    self.pending.unlink()

    def test_foreign_fixed_pending_is_never_opened_truncated_or_removed(self):
        foreign = b"synthetic foreign pending must remain\n"
        self.pending.write_bytes(foreign)
        _atomic_json(self.target, {"synthetic": "owner"})
        self.assertEqual(self.pending.read_bytes(), foreign)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"synthetic": "owner"})

    def test_exclusive_random_pending_creation_cannot_truncate_collision(self):
        from types import SimpleNamespace
        identifier = "b" * 32
        foreign = self.target.with_name(self.target.name + "." + identifier + ".pending")
        foreign.write_bytes(b"synthetic collision owned by another writer\n")
        previous = foreign.read_bytes()
        with patch("indeces.ledger_io.uuid.uuid4", return_value=SimpleNamespace(hex=identifier)), \
                patch.object(Path, "replace") as rename, self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "must-not-overwrite-collision"})
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertEqual(caught.exception.ledger_io_failure["operation"], "open")
        self.assertEqual(rename.call_count, 0)
        self.assertEqual(foreign.read_bytes(), previous)
        self.assertEqual(self.target.read_bytes(), self.previous)

    def check_drift(self, subject, change):
        original_replace = Path.replace
        calls, foreign_paths = [], []
        foreign = b'{"synthetic":"external-owner"}\n'

        def replace_with_drift(path, destination):
            calls.append(path)
            changed = self.target if subject == "target" else path
            if len(calls) == 1:
                if change == "overwrite":
                    changed.write_bytes(foreign)
                else:
                    other = changed.with_name(changed.name + ".foreign")
                    other.write_bytes(foreign)
                    original_replace(other, changed)
                foreign_paths.append(changed)
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "replace", replace_with_drift), self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "would-overwrite-external-owner"})
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(len(calls), 1)
        self.assertEqual(foreign_paths[0].read_bytes(), foreign)
        if subject == "pending":
            self.assertEqual(self.target.read_bytes(), self.previous)

    def test_target_overwrite_blocks_recovery_without_overwriting_new_owner(self):
        self.check_drift("target", "overwrite")

    def test_target_replacement_blocks_recovery_without_overwriting_new_owner(self):
        self.check_drift("target", "replace")

    def test_pending_overwrite_blocks_recovery_and_preserves_foreign_bytes(self):
        self.check_drift("pending", "overwrite")

    def test_pending_replacement_blocks_recovery_and_preserves_foreign_bytes(self):
        self.check_drift("pending", "replace")

    def test_same_target_thread_writer_is_rejected_without_mixing_snapshots(self):
        original_replace = Path.replace
        contention, attempts, failures = [], [], []

        def competing_writer():
            try:
                _atomic_json(self.target, {"synthetic": "competing-writer"})
            except GovernedError as error:
                contention.append((error.code, getattr(error, "ledger_io_failure", None)))
            except BaseException as error:
                failures.append(type(error).__name__)

        def hold_first_writer(path, destination):
            attempts.append(path.read_bytes())
            if len(attempts) == 1:
                worker = threading.Thread(target=competing_writer)
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "competing writer did not terminate within bound")
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "replace", hold_first_writer):
            recovered = _atomic_json(self.target, {"synthetic": "first-owner"})
        self.assertEqual(failures, [])
        self.assertEqual(contention, [("requery_ledger_writer_busy", None)])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(recovered["replace_attempts"], 2)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"synthetic": "first-owner"})

    def test_same_target_process_writer_is_rejected_without_mixing_snapshots(self):
        original_replace = Path.replace
        attempts, results = [], []
        child = r'''
import json, socket, sys
from pathlib import Path
socket.socket.connect = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external network forbidden"))
socket.socket.connect_ex = socket.socket.connect
sys.path.insert(0, sys.argv[1])
from indeces.active_requery import _atomic_json
from indeces.contracts import GovernedError
try:
    _atomic_json(Path(sys.argv[2]), {"synthetic": "competing-process"})
except GovernedError as error:
    print(json.dumps({"code": error.code, "io_failure": getattr(error, "ledger_io_failure", None)}))
else:
    print(json.dumps({"code": "unexpected_write_success"}))
'''

        def hold_first_writer(path, destination):
            attempts.append(path.read_bytes())
            if len(attempts) == 1:
                result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", child,
                    str(Path(__file__).resolve().parents[1]), str(self.target)], capture_output=True,
                    text=True, timeout=8, check=True)
                self.assertEqual(result.stderr, "")
                results.append(json.loads(result.stdout))
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "replace", hold_first_writer):
            _atomic_json(self.target, {"synthetic": "first-process"})
        self.assertEqual(results, [{"code": "requery_ledger_writer_busy", "io_failure": None}])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"synthetic": "first-process"})

    def test_lease_close_fault_preserves_first_error_and_releases_writer_registry(self):
        original_close = os.close
        closes = []

        def close_then_fail(descriptor):
            original_close(descriptor)
            closes.append(descriptor)
            raise os_fault(5, 6)

        with patch("indeces.ledger_io.os.close", close_then_fail), \
                patch.object(Path, "replace", side_effect=os_fault(13, 5)) as rename, \
                self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "replacement"})
        self.assertEqual(rename.call_count, 3)
        self.assertEqual(len(closes), 1)
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(self.target.read_bytes(), self.previous)
        # A separately requested synthetic write can take both leases after
        # the failed close. It is not an automatic product retry.
        _atomic_json(self.target, {"synthetic": "fresh-explicit-writer"})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         {"synthetic": "fresh-explicit-writer"})

    def test_writer_anchor_symlink_cannot_mutate_foreign_file(self):
        foreign = self.target.with_name("synthetic-foreign-anchor.txt")
        foreign.write_bytes(b"synthetic foreign owner\n")
        original = foreign.read_bytes()
        anchor = self.target.with_name(self.target.name + ".writer.lock")
        try:
            anchor.symlink_to(foreign)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314 or error.errno in {1, 13, 38, 95}:
                self.skipTest("host does not permit synthetic symlink creation")
            raise
        with patch.object(Path, "replace") as rename, self.assertRaises(GovernedError) as caught:
            _atomic_json(self.target, {"synthetic": "replacement"})
        self.assertEqual(caught.exception.code, "requery_ledger_identity_changed")
        self.assertTrue(caught.exception.ledger_ownership_lost)
        self.assertEqual(rename.call_count, 0)
        self.assertEqual(foreign.read_bytes(), original)
        self.assertEqual(self.target.read_bytes(), self.previous)

    @unittest.skipUnless(os.name == "nt", "Windows reader sharing experiment")
    def test_real_windows_read_handle_denial_recovers_after_reader_closes(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(self.target), 0x80000000, 3, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        original_replace, attempts, os_errors = Path.replace, [], []
        closed = False

        def replace_after_release(path, destination):
            nonlocal closed
            attempts.append(path.read_bytes())
            if len(attempts) == 2:
                self.assertTrue(kernel.CloseHandle(handle))
                closed = True
            try:
                return original_replace(path, destination)
            except OSError as error:
                os_errors.append((error.errno, error.winerror))
                raise

        try:
            with patch.object(Path, "replace", replace_after_release):
                recovered = _atomic_json(self.target, {"synthetic": "after-reader-release"})
        finally:
            if not closed:
                kernel.CloseHandle(handle)
        self.assertEqual(os_errors, [(13, 5)])
        self.assertEqual(recovered["replace_attempts"], 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         {"synthetic": "after-reader-release"})


class ProviderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Reuse the existing guarded mock setup without inheriting its test cases."""

    def setUp(self):
        io_fixtures.BudgetLedgerIOTests.setUp(self)
        platform = patch("indeces.ledger_io._WINDOWS_RECOVERY", True)
        platform.start()
        self.addCleanup(platform.stop)

    asyncTearDown = io_fixtures.BudgetLedgerIOTests.asyncTearDown
    transport = io_fixtures.BudgetLedgerIOTests.transport

    async def test_constructor_recovery_has_no_provider_context_and_no_provider_request(self):
        original_replace, snapshots = Path.replace, []

        def deny_new_ledger_once(path, destination):
            if destination != self.adapter.path:
                snapshots.append(path.read_bytes())
                if len(snapshots) == 1:
                    raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(Path, "replace", deny_new_ledger_once):
            adapter = io_fixtures.TurnBudgetAdapter(self.base, self.config, self.scratch,
                "b" * 32, "synthetic-constructor-recovery", "synthetic-scope")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(self.requests, [])
        self.assertEqual(adapter.calls, [])
        self.assertIsNone(adapter.halted)
        self.assertIsNone(adapter.ledger_io_failure)
        recovered = [fields for event, fields in self.scratch.events if event == "requery_ledger_io_recovered"]
        self.assertEqual(recovered, [{"trace_id": "b" * 32, "audit_event": None,
            "provider_call_id": None, "stage": None,
            "first_failure": {"operation": "replace", "errno": 13, "winerror": 5},
            "replace_attempts": 2, "intentional_wait_seconds": 0.01}])
        adapter.finish()
        ledger = json.loads(adapter.path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual(ledger["calls"], [])
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (0, 0))

    async def recover_during(self, selected_event):
        original_save, original_replace = self.adapter._save, Path.replace
        self.adapter.trace_id = "a" * 32
        context, denied_snapshots, recovered_snapshots = {}, [], []

        def track_save(**fields):
            context.clear()
            context.update(fields)
            try:
                return original_save(**fields)
            finally:
                context.clear()

        def deny_selected_write(path, destination):
            matches = destination == self.adapter.path and context.get("audit_event") == selected_event
            if selected_event == "response_received":
                matches = matches and self.adapter.input_tokens == 64
            if matches:
                if not denied_snapshots:
                    denied_snapshots.append(path.read_bytes())
                    raise os_fault(13, 5)
                if not recovered_snapshots:
                    recovered_snapshots.append(path.read_bytes())
            return original_replace(path, destination)

        with patch.object(self.adapter, "_save", track_save), patch.object(Path, "replace", deny_selected_write):
            result = await self.adapter.call("query", "Synthetic instructions",
                [{"role": "user", "content": "Synthetic input"}], "a" * 32)
        self.assertEqual(result.input_tokens, 64)
        self.assertEqual(result.output_tokens, 32)
        self.assertEqual(len(denied_snapshots), 1)
        self.assertEqual(recovered_snapshots, denied_snapshots)
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        self.assertIsNone(self.base._session)
        self.assertIsNone(self.adapter.halted)
        self.assertIsNone(self.adapter.ledger_io_failure)
        self.adapter.finish()
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot["phase"], "completed")
        self.assertEqual(snapshot["automatic_retries"], 0)
        self.assertEqual(snapshot["unknown_generation_count"], 0)
        self.assertTrue(snapshot["usage_complete"])
        self.assertEqual((snapshot["input_tokens"], snapshot["output_tokens"]), (64, 32))
        self.assertEqual(len(snapshot["calls"]), 1)
        self.assertEqual(snapshot["calls"][0]["status"], "completed")
        self.assertEqual(snapshot["calls"][0]["usage"], {"input_tokens": 64, "output_tokens": 32})
        persisted = json.loads(self.adapter.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["phase"], "completed")
        self.assertEqual(persisted["calls"], snapshot["calls"])
        provider_ends = [fields for event, fields in self.scratch.events if event == "call_end"]
        ledger_ends = [fields for event, fields in self.scratch.events
                       if event == "requery_budget_event" and fields["budget_event"] == "call_end"]
        self.assertEqual(len(provider_ends), 1)
        self.assertEqual(len(ledger_ends), 1)
        self.assertEqual(provider_ends[0]["status"], "completed")
        self.assertEqual(provider_ends[0]["known_usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual(provider_ends[0]["call_id"], ledger_ends[0]["provider_call_id"])
        self.assertEqual(provider_ends[0]["call_id"], snapshot["calls"][0]["call_id"])
        recovered = [fields for event, fields in self.scratch.events if event == "requery_ledger_io_recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0], {"trace_id": "a" * 32, "audit_event": selected_event,
            "provider_call_id": provider_ends[0]["call_id"], "stage": "query",
            "first_failure": {"operation": "replace", "errno": 13, "winerror": 5},
            "replace_attempts": 2, "intentional_wait_seconds": 0.01})
        self.assertFalse(any(event == "requery_ledger_io_failure" for event, _ in self.scratch.events))

    async def test_response_received_recovery_keeps_known_usage_and_one_generation(self):
        await self.recover_during("response_received")

    async def test_call_end_recovery_has_real_provider_end_and_completed_ledger(self):
        await self.recover_during("call_end")

    async def test_input_gate_recovery_does_not_repeat_input_count_or_generation(self):
        await self.recover_during("input_gate")

    async def test_permanent_call_end_denial_halts_with_known_usage_without_fabricated_provider_end(self):
        original_save, original_replace = self.adapter._save, Path.replace
        context, attempts = {}, []

        def track_save(**fields):
            context.clear()
            context.update(fields)
            try:
                return original_save(**fields)
            finally:
                context.clear()

        def permanently_deny_call_end(path, destination):
            if destination == self.adapter.path and context.get("audit_event") == "call_end":
                attempts.append(path.read_bytes())
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(self.adapter, "_save", track_save), \
                patch.object(Path, "replace", permanently_deny_call_end), self.assertRaises(GovernedError) as caught:
            await self.adapter.call("query", "Synthetic instructions", [], "a" * 32)
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertTrue(caught.exception.adapter_audit_failure)
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(value == attempts[0] for value in attempts))
        self.assertEqual(self.adapter.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(self.adapter.halted, "requery_ledger_audit_failed")
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        self.assertFalse(any(event == "call_end" for event, _ in self.scratch.events))
        self.assertFalse(any(event == "requery_budget_event" and fields["budget_event"] == "call_end"
                             for event, fields in self.scratch.events))
        self.assertFalse(any(event == "requery_ledger_io_recovered" for event, _ in self.scratch.events))
        with self.assertRaisesRegex(GovernedError, "requery_ledger_audit_failed"):
            await self.adapter.call("reply", "Synthetic instructions", [], "a" * 32)
        self.adapter.finish()
        persisted = json.loads(self.adapter.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["phase"], "halted")
        self.assertEqual(persisted["calls"][0]["usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual((persisted["input_tokens"], persisted["output_tokens"]), (64, 32))
        self.assertTrue(persisted["usage_complete"])
        self.assertEqual(persisted["unknown_generation_count"], 0)
        self.assertEqual(persisted["automatic_retries"], 0)

    async def test_after_sticky_failure_cleanup_denial_is_not_retried(self):
        first = {"operation": "write", "errno": 5, "winerror": 6}
        self.adapter.ledger_io_failure = deepcopy(first)
        with patch.object(Path, "replace", side_effect=os_fault(13, 5)) as rename, \
                self.assertRaises(GovernedError):
            self.adapter._save()
        self.assertEqual(rename.call_count, 1)
        self.assertEqual(self.adapter.ledger_io_failure, first)
        self.assertEqual(self.requests, [])

    async def preserve_foreign_after_provider_response(self, subject):
        original_save, original_replace = self.adapter._save, Path.replace
        self.adapter.trace_id = "a" * 32
        context, attempts, foreign_paths, original_targets = {}, [], [], []
        foreign = b'{"synthetic":"external-owner-after-provider-response"}\n'

        def track_save(**fields):
            context.clear()
            context.update(fields)
            try:
                return original_save(**fields)
            finally:
                context.clear()

        def change_after_response(path, destination):
            attempts.append(path)
            if (context.get("audit_event") == "response_received" and self.adapter.input_tokens == 64
                    and not foreign_paths):
                original_targets.append(self.adapter.path.read_bytes())
                changed = self.adapter.path if subject == "target" else path
                changed.write_bytes(foreign)
                foreign_paths.append(changed)
                raise os_fault(13, 5)
            return original_replace(path, destination)

        with patch.object(self.adapter, "_save", track_save), patch.object(Path, "replace", change_after_response), \
                self.assertRaises(GovernedError) as caught:
            await self.adapter.call("query", "Synthetic instructions", [], "a" * 32)
        error = caught.exception
        self.assertEqual(error.code, "requery_ledger_write_failed")
        self.assertTrue(error.ledger_ownership_lost)
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(error.known_usage, {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual(self.adapter.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 5})
        self.assertEqual(len(foreign_paths), 1)
        before = len(attempts)
        with patch.object(Path, "replace", wraps=original_replace) as rename:
            for cleanup in (self.adapter._save, lambda: self.adapter.halt("synthetic_outer_failure"), self.adapter.finish):
                with self.assertRaises(GovernedError) as repeated:
                    cleanup()
                self.assertIs(repeated.exception, error)
                self.assertEqual(foreign_paths[0].read_bytes(), foreign)
            self.assertEqual(rename.call_count, 0)
        self.assertEqual(len(attempts), before)
        if subject == "pending":
            self.assertEqual(self.adapter.path.read_bytes(), original_targets[0])
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot["halted"], "requery_ledger_audit_failed")
        self.assertEqual((snapshot["input_tokens"], snapshot["output_tokens"]), (64, 32))
        self.assertEqual(snapshot["unknown_generation_count"], 0)
        self.assertTrue(snapshot["usage_complete"])
        self.assertEqual(snapshot["automatic_retries"], 0)
        self.assertEqual(snapshot["calls"][0]["usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        with self.assertRaises(GovernedError):
            await self.adapter.call("reply", "Synthetic instructions", [], "a" * 32)
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        self.assertFalse(any(event == "call_end" for event, _ in self.scratch.events))
        self.assertFalse(any(event == "requery_budget_event" and fields["budget_event"] == "call_end"
                             for event, fields in self.scratch.events))
        self.assertFalse(any(event == "requery_ledger_io_recovered" for event, _ in self.scratch.events))

    async def test_target_drift_after_known_usage_cannot_be_overwritten_by_cleanup_or_finish(self):
        await self.preserve_foreign_after_provider_response("target")

    async def test_pending_drift_after_known_usage_cannot_be_overwritten_by_cleanup_or_finish(self):
        await self.preserve_foreign_after_provider_response("pending")

    async def test_writer_busy_after_provider_response_never_overwrites_competing_commit(self):
        original_replace = Path.replace
        held, release = threading.Event(), threading.Event()
        failures = []

        def competing_writer():
            try:
                _atomic_json(self.adapter.path, {"synthetic": "competing-commit"})
            except BaseException as error:
                failures.append(type(error).__name__)

        worker = threading.Thread(target=competing_writer)

        def hold_competing_commit(path, destination):
            if threading.current_thread() is worker:
                held.set()
                if not release.wait(3):
                    raise AssertionError("synthetic competing writer release timed out")
            return original_replace(path, destination)

        async def transport_with_competitor(path, payload):
            result = await self.transport(path, payload)
            if path == "/responses":
                worker.start()
                self.assertTrue(held.wait(3), "synthetic competing writer did not obtain lease")
            return result

        self.base._request_override = transport_with_competitor
        try:
            with patch.object(Path, "replace", hold_competing_commit), self.assertRaises(GovernedError) as caught:
                await self.adapter.call("query", "Synthetic instructions", [], "a" * 32)
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        error = caught.exception
        self.assertEqual(error.code, "requery_ledger_writer_busy")
        self.assertTrue(error.ledger_ownership_lost)
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(error.known_usage, {"input_tokens": 64, "output_tokens": 32})
        self.assertIsNone(self.adapter.ledger_io_failure)
        competing = self.adapter.path.read_bytes()
        self.assertEqual(json.loads(competing), {"synthetic": "competing-commit"})
        with patch.object(Path, "replace", wraps=original_replace) as rename:
            for cleanup in (self.adapter._save, lambda: self.adapter.halt("synthetic_outer_failure"), self.adapter.finish):
                with self.assertRaises(GovernedError) as repeated:
                    cleanup()
                self.assertIs(repeated.exception, error)
                self.assertEqual(self.adapter.path.read_bytes(), competing)
            self.assertEqual(rename.call_count, 0)
        snapshot = self.adapter.snapshot()
        self.assertEqual((snapshot["input_tokens"], snapshot["output_tokens"]), (64, 32))
        self.assertTrue(snapshot["usage_complete"])
        self.assertEqual(snapshot["unknown_generation_count"], 0)
        self.assertEqual(snapshot["automatic_retries"], 0)
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        self.assertFalse(any(event == "call_end" for event, _ in self.scratch.events))

    async def test_external_change_between_successful_saves_cannot_be_adopted_as_new_baseline(self):
        await self.adapter.call("query", "Synthetic instructions", [], "a" * 32)
        foreign = b'{"synthetic":"foreign-between-successful-saves"}\n'
        self.adapter.path.write_bytes(foreign)
        with patch.object(Path, "replace") as rename, self.assertRaises(GovernedError) as caught:
            self.adapter._save()
        error = caught.exception
        self.assertEqual(error.code, "requery_ledger_identity_changed")
        self.assertTrue(error.ledger_ownership_lost)
        self.assertFalse(error.remote_usage_unknown)
        self.assertIsNone(self.adapter.ledger_io_failure)
        self.assertEqual(rename.call_count, 0)
        with patch.object(Path, "replace") as rename:
            for cleanup in (self.adapter._save, lambda: self.adapter.halt("synthetic_outer_failure"), self.adapter.finish):
                with self.assertRaises(GovernedError) as repeated:
                    cleanup()
                self.assertIs(repeated.exception, error)
                self.assertEqual(self.adapter.path.read_bytes(), foreign)
            self.assertEqual(rename.call_count, 0)
        snapshot = self.adapter.snapshot()
        self.assertEqual((snapshot["input_tokens"], snapshot["output_tokens"]), (64, 32))
        self.assertEqual(snapshot["calls"][0]["usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual(snapshot["automatic_retries"], 0)
        self.assertEqual(snapshot["unknown_generation_count"], 0)
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        provider_ends = [fields for event, fields in self.scratch.events if event == "call_end"]
        self.assertEqual(len(provider_ends), 1)
        self.assertEqual(provider_ends[0]["status"], "completed")
        self.assertEqual(provider_ends[0]["known_usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertFalse(any(event == "requery_budget_event" and fields["budget_event"] == "turn_end"
                             for event, fields in self.scratch.events))


if __name__ == "__main__":
    unittest.main()
