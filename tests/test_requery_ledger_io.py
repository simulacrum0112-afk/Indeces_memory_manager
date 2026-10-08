"""Synthetic ledger I/O faults; no user records or real model transport."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.active_requery import ActiveRequery, TurnBudgetAdapter, _atomic_json
from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, RuntimeConfig
from indeces.contracts import GovernedError, IncomingMessage


def os_fault(errno=13, winerror=32):
    error = OSError(errno, "Synthetic detail that must not enter metadata", "synthetic-sensitive-path")
    error.winerror = winerror
    return error


def governed_fault(operation="replace", errno=13, winerror=32):
    error = GovernedError("requery_ledger_write_failed", remote_usage_unknown=False)
    error.ledger_io_failure = {"operation": operation, "errno": errno, "winerror": winerror}
    return error


class Recorder:
    def __init__(self, *, fail_metadata=False):
        self.events = []
        self.fail_metadata = fail_metadata

    def write(self, event, **fields):
        if event == "requery_ledger_io_failure" and self.fail_metadata:
            raise RuntimeError("Synthetic diagnostic sink failure")
        self.events.append((event, deepcopy(fields)))


class FaultStream:
    def __init__(self, stream, operation, error, *, close_error=None):
        self.stream, self.operation, self.error = stream, operation, error
        self.close_error = close_error
        self.close_count = 0

    def write(self, value):
        if self.operation == "write":
            raise self.error
        return self.stream.write(value)

    def flush(self):
        if self.operation == "flush":
            raise self.error
        return self.stream.flush()

    def fileno(self):
        return self.stream.fileno()

    def close(self):
        self.close_count += 1
        self.stream.close()
        if self.close_error is not None:
            raise self.close_error
        if self.operation == "close":
            raise self.error


class AtomicLedgerIOTests(unittest.TestCase):
    def check_fault(self, operation, *, error=None, close_error=None):
        error = error if error is not None else os_fault()
        with TemporaryDirectory() as directory:
            target = Path(directory) / "synthetic-ledger.json"
            target.write_text('{"previous":true}\n', encoding="utf-8")
            original_bytes = target.read_bytes()
            pending = target.with_name(target.name + ".pending")
            original_open, original_replace = Path.open, Path.replace
            opened, replacements = [], []

            def open_stream(path, *args, **kwargs):
                if path != pending:
                    return original_open(path, *args, **kwargs)
                if operation == "open":
                    raise error
                stream = FaultStream(original_open(path, *args, **kwargs), operation, error,
                                     close_error=close_error)
                opened.append(stream)
                return stream

            def replace_file(path, destination):
                replacements.append(path)
                self.assertTrue(all(stream.stream.closed for stream in opened))
                if operation == "replace":
                    raise error
                return original_replace(path, destination)

            with patch.object(Path, "open", open_stream), patch.object(Path, "replace", replace_file), \
                    patch("os.fsync", side_effect=error if operation == "fsync" else None), \
                    self.assertRaises(GovernedError) as caught:
                _atomic_json(target, {"replacement": True})
            failure = caught.exception
            self.assertEqual(failure.code, "requery_ledger_write_failed")
            self.assertEqual(str(failure), "requery_ledger_write_failed")
            self.assertFalse(failure.remote_usage_unknown)
            self.assertIsNone(failure.known_usage)
            self.assertIsNone(failure.__cause__)
            self.assertTrue(failure.__suppress_context__)
            self.assertEqual(failure.ledger_io_failure, {
                "operation": operation,
                "errno": error.errno if type(error.errno) is int else None,
                "winerror": error.winerror if type(error.winerror) is int else None,
            })
            self.assertEqual(target.read_bytes(), original_bytes)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"previous": True})
            self.assertEqual(len(replacements), int(operation == "replace"))
            self.assertTrue(all(stream.stream.closed and stream.close_count == 1 for stream in opened))
            self.assertNotIn("synthetic-sensitive", json.dumps(failure.ledger_io_failure))
            self.assertNotIn("Synthetic detail", json.dumps(failure.ledger_io_failure))
            # After the injected fault is removed, a fresh explicit write works.
            _atomic_json(target, {"replacement": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"replacement": True})
            self.assertFalse(pending.exists())

    def test_open_failure_preserves_previous_json(self):
        self.check_fault("open")

    def test_write_failure_preserves_previous_json_and_closes_writer(self):
        self.check_fault("write")

    def test_flush_failure_preserves_previous_json_and_closes_writer(self):
        self.check_fault("flush")

    def test_fsync_failure_preserves_previous_json_and_closes_writer(self):
        self.check_fault("fsync")

    def test_close_failure_preserves_previous_json_without_replace(self):
        self.check_fault("close")

    def test_replace_failure_preserves_previous_json_and_closed_writer(self):
        self.check_fault("replace")

    def test_cleanup_close_failure_cannot_mask_earlier_write_failure(self):
        self.check_fault("write", close_error=os_fault(5, 6))

    def test_boolean_string_and_integer_subclass_error_codes_become_null(self):
        class IntegerSubclass(int):
            pass

        for errno, winerror in ((True, "32"), ("13", False), (IntegerSubclass(13), IntegerSubclass(32))):
            with self.subTest(errno_type=type(errno).__name__, winerror_type=type(winerror).__name__):
                error = os_fault()
                error.errno, error.winerror = errno, winerror
                self.check_fault("replace", error=error)

    def test_normal_replace_occurs_after_writer_handle_is_closed(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "synthetic-ledger.json"
            original_open, original_replace = Path.open, Path.replace
            opened = []

            def track_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                if path.name.endswith(".pending"):
                    opened.append(stream)
                return stream

            def checked_replace(path, destination):
                self.assertEqual(len(opened), 1)
                self.assertTrue(opened[0].closed)
                return original_replace(path, destination)

            with patch.object(Path, "open", track_open), patch.object(Path, "replace", checked_replace):
                _atomic_json(target, {"synthetic": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"synthetic": True})


class BudgetLedgerIOTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.scratch = Recorder()
        budget = Budget(4608, 2048, 15, "medium")
        self.config = SimpleNamespace(state_dir=self.root / "state",
            runtime=RuntimeConfig(active_requery_enabled=True),
            adapter=AdapterConfig("gpt-6.1-sol", "https://offline.invalid", {
                stage: budget for stage in ("label", "selection", "summary", "query", "reply")}))
        self.requests = []
        self.base = OpenAIAdapter(self.config.adapter, self.scratch, request=self.transport)
        self.base.offline_mock = True
        self.adapter = TurnBudgetAdapter(self.base, self.config, self.scratch,
                                         "synthetic-trace", "synthetic-message", "synthetic-scope")
        self.guards = [patch("socket.socket.connect", side_effect=AssertionError("external network forbidden")),
                       patch("socket.socket.connect_ex", side_effect=AssertionError("external network forbidden")),
                       patch("aiohttp.ClientSession", side_effect=AssertionError("real HTTP forbidden"))]
        for guard in self.guards:
            guard.start()

    async def asyncTearDown(self):
        await self.base.close()
        for guard in reversed(self.guards):
            guard.stop()
        self.directory.cleanup()

    async def transport(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        if path == "/responses/input_tokens":
            return {"input_tokens": 64}
        self.assertEqual(path, "/responses")
        return {"id": "synthetic-completed", "status": "completed",
            "usage": {"input_tokens": 64, "output_tokens": 32},
            "output": [{"type": "message", "role": "assistant", "content": [{
                "type": "output_text", "text": '{"synthetic":true}'}]}]}

    def io_events(self):
        return [fields for event, fields in self.scratch.events if event == "requery_ledger_io_failure"]

    async def test_call_end_replace_fault_retains_known_usage_and_halts_without_new_generation(self):
        original_replace = Path.replace
        old_documents, failures = [], []

        def replace_once(path, destination):
            if path == self.adapter.path.with_name(self.adapter.path.name + ".pending"):
                # Both reads release their handles before the replacement.
                document = json.loads(path.read_text(encoding="utf-8"))
                if document["calls"] and document["calls"][-1]["status"] == "completed" and not failures:
                    old_documents.append(json.loads(self.adapter.path.read_text(encoding="utf-8")))
                    failures.append(path.name)
                    raise os_fault()
            return original_replace(path, destination)

        with patch.object(Path, "replace", replace_once), self.assertRaises(GovernedError) as caught:
            await self.adapter.call("query", "Synthetic instructions", [{"role": "user", "content": "Synthetic input"}],
                                    "synthetic-trace")
        self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
        self.assertTrue(caught.exception.adapter_audit_failure)
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 64, "output_tokens": 32})
        self.assertEqual(len(failures), 1)
        self.assertEqual(old_documents[0]["calls"][-1]["status"], "admitted")
        self.assertEqual(old_documents[0]["calls"][-1]["usage"], {"input_tokens": 64, "output_tokens": 32})
        snapshot = self.adapter.snapshot()
        self.assertEqual(snapshot["halted"], "requery_ledger_audit_failed")
        self.assertTrue(snapshot["usage_complete"])
        self.assertEqual(snapshot["unknown_generation_count"], 0)
        self.assertEqual(snapshot["automatic_retries"], 0)
        self.assertEqual((snapshot["input_tokens"], snapshot["output_tokens"]), (64, 32))
        self.assertEqual(snapshot["calls"][-1]["status"], "completed")
        self.assertNotIn("ledger_io_failure", snapshot)
        self.assertFalse(any(event == "call_end" for event, _ in self.scratch.events))
        self.assertFalse(any(event == "requery_budget_event" and fields["budget_event"] == "call_end"
                             for event, fields in self.scratch.events))
        recorded = self.io_events()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0], {"trace_id": "synthetic-trace", "audit_event": "call_end",
            "provider_call_id": self.adapter.last_call_id, "stage": "query",
            "io_failure": {"operation": "replace", "errno": 13, "winerror": 32}})
        self.assertEqual(json.loads(self.adapter.path.read_text(encoding="utf-8"))["halted"],
                         "requery_ledger_audit_failed")
        with self.assertRaisesRegex(GovernedError, "requery_ledger_audit_failed"):
            await self.adapter.call("reply", "Synthetic instructions", [], "synthetic-trace")
        self.assertEqual([path for path, _ in self.requests], ["/responses/input_tokens", "/responses"])
        self.assertIsNone(self.base._session)
        runtime = SimpleNamespace(scratch=self.scratch, graph=SimpleNamespace(selector=None))
        message = IncomingMessage("synthetic-message", "20", "10", "30", "Synthetic operator",
                                  "Synthetic question", "2026-10-08T00:00:00Z")
        active = ActiveRequery(runtime, self.adapter, "synthetic-trace", message)
        active.stop(caught.exception.code)
        stop = [fields for event, fields in self.scratch.events if event == "requery_stop"][0]
        self.assertEqual(stop["reason"], "requery_ledger_write_failed")
        self.assertEqual(stop["ledger"]["halted"], "requery_ledger_audit_failed")
        self.assertEqual(stop["ledger_io_failure"], recorded[0]["io_failure"])
        self.assertNotIn("requery_ledger", active.fallback_text)
        self.adapter.finish()
        persisted = json.loads(self.adapter.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["phase"], "halted")
        self.assertEqual(persisted["calls"][-1]["usage"], {"input_tokens": 64, "output_tokens": 32})
        self.assertFalse(self.adapter.path.with_name(self.adapter.path.name + ".pending").exists())

    async def test_first_io_failure_is_preserved_while_each_fault_gets_safe_metadata(self):
        first, second = governed_fault("write", 5, 6), governed_fault("replace", 13, 32)
        for error in (first, second):
            with patch("indeces.active_requery._atomic_json", side_effect=error), self.assertRaises(GovernedError) as caught:
                self.adapter._save(audit_event="input_gate", provider_call_id="a" * 32, stage="label")
            self.assertIs(caught.exception, error)
        self.assertEqual(self.adapter.ledger_io_failure, first.ledger_io_failure)
        self.assertEqual([fields["io_failure"] for fields in self.io_events()],
                         [first.ledger_io_failure, second.ledger_io_failure])
        self.assertTrue(all(fields["audit_event"] == "input_gate" and fields["stage"] == "label"
                            for fields in self.io_events()))
        self.io_events()[0]["io_failure"]["operation"] = "open"
        self.assertEqual(self.adapter.ledger_io_failure["operation"], "write")

    async def test_failed_diagnostic_sink_never_masks_or_retries_original_io_error(self):
        error = governed_fault()
        self.scratch.fail_metadata = True
        with patch("indeces.active_requery._atomic_json", side_effect=error) as write, \
                self.assertRaises(GovernedError) as caught:
            self.adapter._save(audit_event="call_end", provider_call_id="a" * 32, stage="query")
        self.assertIs(caught.exception, error)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(self.adapter.ledger_io_failure, error.ledger_io_failure)
        self.assertEqual(self.requests, [])

    async def test_malformed_or_missing_exception_metadata_preserves_error_without_export(self):
        original_bytes = self.adapter.path.read_bytes()
        valid = {"operation": "replace", "errno": 13, "winerror": 32}
        malformed = [dict(valid, path="synthetic-private-path"),
                     {"operation": "replace", "errno": 13},
                     dict(valid, errno=True), dict(valid, operation="synthetic-private-operation"),
                     None]
        for diagnostic in malformed:
            with self.subTest(diagnostic_type=type(diagnostic).__name__):
                error = GovernedError("requery_ledger_write_failed", remote_usage_unknown=False)
                if diagnostic is not None:
                    error.ledger_io_failure = diagnostic
                with patch("indeces.active_requery._atomic_json", side_effect=error) as write, \
                        self.assertRaises(GovernedError) as caught:
                    self.adapter._save(audit_event="call_end", provider_call_id="a" * 32, stage="query")
                self.assertIs(caught.exception, error)
                self.assertEqual(caught.exception.code, "requery_ledger_write_failed")
                self.assertEqual(write.call_count, 1)
                self.assertIsNone(self.adapter.ledger_io_failure)
                self.assertEqual(self.io_events(), [])
                self.assertEqual(self.adapter.path.read_bytes(), original_bytes)
        error = governed_fault()
        with patch("indeces.active_requery._atomic_json", side_effect=error), self.assertRaises(GovernedError):
            self.adapter._save()
        self.assertEqual(self.adapter.ledger_io_failure, valid)
        self.assertEqual(len(self.io_events()), 1)
        self.assertEqual(self.requests, [])
        self.assertNotIn("synthetic-private", json.dumps(self.scratch.events))

    async def test_unknown_audit_context_is_null_and_lifecycle_stays_explicit(self):
        for event, call_id, stage in ((["untrusted"], "synthetic-sensitive-path", {"untrusted": True}),
                                      (None, None, None), ("response_headers", "b" * 32, "reply")):
            with patch("indeces.active_requery._atomic_json", side_effect=governed_fault()), \
                    self.assertRaises(GovernedError):
                self.adapter._save(audit_event=event, provider_call_id=call_id, stage=stage)
        records = self.io_events()
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["audit_event"] is None and record["provider_call_id"] is None
                            and record["stage"] is None for record in records[:2]))
        self.assertEqual((records[2]["audit_event"], records[2]["provider_call_id"], records[2]["stage"]),
                         ("response_headers", "b" * 32, "reply"))
        self.assertNotIn("synthetic-sensitive", json.dumps(records))

    async def test_constructor_write_failure_exposes_safe_metadata_without_a_call(self):
        with patch.object(Path, "replace", side_effect=os_fault()), self.assertRaises(GovernedError) as caught:
            TurnBudgetAdapter(self.base, self.config, self.scratch,
                              "synthetic-new-trace", "synthetic-new-message", "synthetic-scope")
        self.assertEqual(caught.exception.ledger_io_failure,
                         {"operation": "replace", "errno": 13, "winerror": 32})
        self.assertEqual(self.io_events()[-1], {"trace_id": "synthetic-new-trace", "audit_event": None,
            "provider_call_id": None, "stage": None, "io_failure": caught.exception.ledger_io_failure})
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
