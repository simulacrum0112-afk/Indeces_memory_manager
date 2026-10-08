"""Synthetic I/O metadata bindings; no saved questions or external calls."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage
from indeces.requery_records import _validate_ledger_io_records, verify_active_turn
from indeces.run_records import validate_ledger_io_failure, verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store
from tests import test_requery_records as record_fixtures


FAILURE = {"operation": "replace", "errno": 13, "winerror": 5}


def io_event(**updates):
    fields = {"trace_id": "synthetic-trace", "audit_event": None,
              "provider_call_id": None, "stage": None, "io_failure": deepcopy(FAILURE)}
    fields.update(updates)
    return "requery_ledger_io_failure", fields


class LedgerIORecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = record_fixtures.ActiveTurnVerificationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def host_turn(self):
        scratch = record_fixtures.Scratch()
        start = self.fixture.start(scratch)
        bundle = self.fixture.initial_events(scratch)
        self.fixture.host_finish(scratch, bundle)
        return scratch.events, start

    def test_exact_numeric_schema_accepts_six_operations_and_returns_copy(self):
        for operation in ("open", "write", "flush", "fsync", "close", "replace"):
            value = {"operation": operation, "errno": None, "winerror": 5}
            accepted = validate_ledger_io_failure(value)
            self.assertEqual(accepted, value)
            self.assertIsNot(accepted, value)

    def test_schema_refuses_text_paths_extra_fields_bool_and_unknown_operation(self):
        cases = [None, [], {**FAILURE, "path": "synthetic-private-path"},
                 {**FAILURE, "errno": True}, {**FAILURE, "winerror": "private-error"},
                 {**FAILURE, "operation": "synthetic-private-path"},
                 {"operation": "replace", "errno": 13}]
        for value in cases:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as caught:
                validate_ledger_io_failure(value)
            self.assertNotIn("synthetic-private", str(caught.exception))
            self.assertNotIn("private-error", str(caught.exception))

    def test_first_failure_binds_stop_and_real_terminal_event_can_confirm_close(self):
        events, start = self.host_turn()
        stop_at = next(i for i, (e, _) in enumerate(events) if e == "requery_stop")
        events.insert(stop_at, io_event())
        stop = next(f for e, f in events if e == "requery_stop")
        stop["ledger_io_failure"] = deepcopy(FAILURE)
        stop["ledger"]["halted"] = stop["reason"]
        closed = deepcopy(stop["ledger"])
        closed["phase"] = "stopped"
        events.append(("requery_budget_event", {"trace_id": "synthetic-trace",
            "provider_call_id": None, "budget_event": "turn_end", "ledger": closed}))
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")
        stop["ledger_io_failure"]["errno"] = 28
        with self.assertRaisesRegex(ValueError, "first failure binding mismatch"):
            verify_active_turn(events, start)

    def test_post_end_cleanup_failure_does_not_prove_ledger_closure(self):
        events, start = self.host_turn()
        events.append(io_event())
        report = verify_active_turn(events, start)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("ledger_io_terminal_save_unconfirmed", report["warnings"])
        events[-1][1]["audit_event"] = "call_end"
        with self.assertRaisesRegex(ValueError, "not cleanup metadata"):
            verify_active_turn(events, start)

    def test_later_cleanup_error_does_not_replace_first_stop_diagnostic(self):
        events, start = self.host_turn()
        stop_at = next(i for i, (e, _) in enumerate(events) if e == "requery_stop")
        events.insert(stop_at, io_event())
        stop = next(f for e, f in events if e == "requery_stop")
        stop["ledger_io_failure"] = deepcopy(FAILURE)
        events.append(io_event(io_failure={"operation": "flush", "errno": 28, "winerror": None}))
        report = verify_active_turn(events, start)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(stop["ledger_io_failure"], FAILURE)

    def test_provider_end_cannot_be_replaced_by_failure_metadata(self):
        events, start = self.host_turn()
        events.insert(1, ("call_start", {"trace_id": "synthetic-trace", "call_id": "a" * 32,
                                        "stage": "query"}))
        stop_at = next(i for i, (e, _) in enumerate(events) if e == "requery_stop")
        events.insert(stop_at, io_event(provider_call_id="a" * 32, audit_event="call_end", stage="query"))
        next(f for e, f in events if e == "requery_stop")["ledger_io_failure"] = deepcopy(FAILURE)
        with self.assertRaisesRegex(ValueError, "missing provider end"):
            verify_active_turn(events, start)

    def test_completed_ledger_requires_provider_end_even_for_partial_diagnostic(self):
        events = [io_event(), ("requery_stop", {"trace_id": "synthetic-trace",
            "ledger_io_failure": deepcopy(FAILURE), "ledger": {"calls": [
                {"call_id": "a" * 32, "status": "completed"}]}})]
        with self.assertRaisesRegex(ValueError, "completed call has no provider end"):
            _validate_ledger_io_records(events, {}, {"warnings": []}, partial=True)

    def test_malformed_event_metadata_and_valid_but_wrong_provider_stage_are_rejected(self):
        for updates in ({"audit_event": "private-error"}, {"provider_call_id": "private-path"},
                        {"stage": "private-error"}, {"io_failure": {**FAILURE, "errno": False}}):
            with self.subTest(keys=tuple(updates)), self.assertRaises(ValueError):
                _validate_ledger_io_records([io_event(**updates)], {}, {"warnings": []})
        provider = {"a" * 32: [("call_start", {"stage": "query"})]}
        with self.assertRaisesRegex(ValueError, "provider stage mismatch"):
            _validate_ledger_io_records([io_event(provider_call_id="a" * 32, stage="reply")],
                                        provider, {"warnings": []})

    def test_missing_trace_io_event_is_not_silently_ignored_by_verify_runs(self):
        log = ScratchLog(self.fixture.root / "missing-trace")
        event, fields = io_event()
        del fields["trace_id"]
        try:
            log.write(event, **fields)
        finally:
            log.close()
        verify(log.path)
        self.assertTrue(verify_runs([log.path])["issues"])

    def test_attached_diagnostic_missing_trace_is_not_silently_ignored(self):
        for event in ("turn_end", "requery_stop"):
            log = ScratchLog(self.fixture.root / ("missing-attached-trace-" + event))
            try:
                log.write(event, status="failed", code="requery_ledger_write_failed",
                          ledger_io_failure=deepcopy(FAILURE))
            finally:
                log.close()
            verify(log.path)
            report = verify_runs([log.path])
            self.assertTrue(report["issues"], report)
            self.assertIn("diagnostic trace invalid", report["issues"][0]["reason"])

    def test_constructor_metadata_without_best_effort_event_is_failed_and_explicitly_unbound(self):
        scratch = record_fixtures.Scratch()
        start = self.fixture.start(scratch)
        scratch.write("turn_end", trace_id="synthetic-trace", status="failed",
                      code="requery_ledger_write_failed", ledger_io_failure=deepcopy(FAILURE))
        report = verify_active_turn(scratch.events, start)
        self.assertEqual(report["status"], "failed")
        self.assertIn("ledger_io_failure_event_unavailable", report["warnings"])
        scratch.events[-1][1]["code"] = "synthetic_other_failure"
        with self.assertRaisesRegex(ValueError, "failed turn declaration mismatch"):
            verify_active_turn(scratch.events, start)

    def test_only_io_retained_suffix_routes_v3_with_checkpoint_and_warning(self):
        timestamp = [datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)]
        log = ScratchLog(self.fixture.root / "retained-io", clock=lambda: timestamp[0])
        start = {"trace_id": "synthetic-trace", "message_id": "synthetic-message",
                 "run_record_version": 3, "input": {"text": "synthetic query"}}
        try:
            log.write("turn_start", **start)
            timestamp[0] += timedelta(hours=23)
            event, fields = io_event()
            log.write(event, **fields)
            log.prune(now=datetime(2026, 10, 9, 0, 1, 1, tzinfo=timezone.utc))
        finally:
            log.close()
        report = verify_runs(list((self.fixture.root / "retained-io").glob("*.jsonl")))
        self.assertEqual(report["counts"]["retention_partial"], 1, report)
        self.assertEqual(report["issues"], [])
        self.assertIn("ledger_io_terminal_save_unconfirmed", report["turns"][0]["warnings"])
        self.assertIn("expired_ledger_io_first_failure_binding", report["turns"][0]["warnings"])

    def test_io_suffix_without_retention_declaration_is_invalid_and_legacy_is_unchanged(self):
        events, start = self.host_turn()
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")
        log = ScratchLog(self.fixture.root / "undeclared-suffix")
        try:
            event, fields = io_event()
            log.write(event, **fields)
        finally:
            log.close()
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["invalid"], 1, report)


class RuntimeLedgerIOFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "state")
        self.scratch = ScratchLog(self.root / "scratch")
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            discord=DiscordConfig("10"), runtime=RuntimeConfig(active_requery_enabled=True,
                model_selection_enabled=False, retrieval_policy="legacy_v1", turn_seconds=30),
            adapter=AdapterConfig("gpt-6.1-sol", "https://synthetic.invalid", {
                stage: Budget(4096, 512, 15, "medium")
                for stage in ("label", "summary", "selection", "query", "reply")}))
        self.requests = []
        async def transport(path, payload):
            self.requests.append(path)
            raise AssertionError("no transport allowed in constructor failure")
        self.adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=transport)
        self.adapter.offline_mock = True
        self.runtime = Runtime(self.config, self.store, self.adapter, self.scratch)
        self.deliveries = []

    async def asyncTearDown(self):
        await self.adapter.close()
        self.scratch.close()
        self.store.close()
        self.directory.cleanup()

    async def deliver(self, text):
        self.deliveries.append(text)
        return DeliveryReceipt(("synthetic-receipt",), text)

    async def test_constructor_failure_keeps_safe_metadata_and_original_failure_code(self):
        for identity, metadata in enumerate((FAILURE, {**FAILURE, "path": "private-path"}), 1):
            error = GovernedError("requery_ledger_write_failed", remote_usage_unknown=False)
            error.ledger_io_failure = deepcopy(metadata)
            message = IncomingMessage(str(identity), "20", "10", "30", "Synthetic",
                                      "synthetic question", "2026-10-08T00:00:00+00:00")
            with patch("indeces.active_requery._atomic_json", side_effect=error), redirect_stdout(io.StringIO()):
                await self.runtime.process(message, self.deliver)
            rows = [json.loads(line) for line in self.scratch.path.read_text(encoding="utf-8").splitlines()]
            start = next(r["fields"] for r in reversed(rows) if r["event"] == "turn_start")
            end = next(r["fields"] for r in reversed(rows) if r["event"] == "turn_end")
            self.assertEqual(end["code"], "requery_ledger_write_failed")
            self.assertEqual(end["status"], "failed")
            if identity == 1:
                self.assertEqual(end["ledger_io_failure"], FAILURE)
            else:
                self.assertNotIn("ledger_io_failure", end)
            self.assertNotIn("private-path", json.dumps(rows))
            self.assertNotIn(start["trace_id"], self.deliveries[-1])
            self.assertNotIn("requery_ledger_write_failed", self.deliveries[-1])
        self.assertEqual(self.requests, [])
        report = verify_runs([self.scratch.path])
        self.assertEqual(report["counts"]["failed"], 2, report)
        self.assertEqual(report["issues"], [])


if __name__ == "__main__":
    unittest.main()
