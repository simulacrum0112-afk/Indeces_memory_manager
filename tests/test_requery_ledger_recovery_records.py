"""Synthetic local-recovery receipts; no saved content or external requests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from indeces.requery_records import verify_active_turn
from indeces.run_records import _validate_ledger_io_fields, verify_runs
from indeces.scratch import ScratchLog
from tests import test_requery_records as record_fixtures


FIRST_FAILURE = {"operation": "replace", "errno": 13, "winerror": 5}


def recovered(**updates):
    fields = {"trace_id": "synthetic-trace", "audit_event": None,
              "provider_call_id": None, "stage": None,
              "first_failure": deepcopy(FIRST_FAILURE), "replace_attempts": 2,
              "intentional_wait_seconds": 0.01}
    fields.update(updates)
    return "requery_ledger_io_recovered", fields


class LedgerRecoveryRecordTests(unittest.TestCase):
    def setUp(self):
        self.fixture = record_fixtures.ActiveTurnVerificationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def host_turn(self, *, query=False):
        scratch = record_fixtures.Scratch()
        start = self.fixture.start(scratch)
        bundle = self.fixture.initial_events(scratch)
        if query:
            bundle = self.fixture.query_round(scratch, bundle)
        ledger = self.fixture.synthetic_ledger(scratch)
        self.fixture.host_finish(scratch, bundle)
        return scratch.events, start, ledger

    def terminal_budget(self, ledger):
        closed = deepcopy(ledger)
        closed["phase"] = "stopped"
        closed["halted"] = "query_budget"
        return "requery_budget_event", {"trace_id": "synthetic-trace",
            "provider_call_id": None, "budget_event": "turn_end", "ledger": closed}

    def provider_recovery(self):
        events, start, ledger = self.host_turn(query=True)
        at = next(i for i, (event, _) in enumerate(events) if event == "call_end")
        identity = events[at][1]["call_id"]
        events.insert(at, recovered(audit_event="call_end", provider_call_id=identity, stage="query"))
        events.insert(at + 1, ("requery_budget_event", {"trace_id": "synthetic-trace",
            "provider_call_id": identity, "budget_event": "call_end", "ledger": ledger}))
        return events, start, at, identity

    def test_successful_call_end_recovery_needs_real_provider_and_budget_receipts(self):
        events, start, _, _ = self.provider_recovery()
        report = verify_active_turn(events, start)
        self.assertEqual(report["status"], "complete", report)
        self.assertNotIn("ledger_io_terminal_save_unconfirmed", report["warnings"])
        self.assertEqual(sum(event == "call_end" for event, _ in events), 1)
        self.assertEqual(next(value["ledger"] for event, value in events
                              if event == "requery_stop")["automatic_retries"], 0)

    def test_call_start_recovery_can_precede_the_real_provider_start(self):
        events, start, ledger = self.host_turn(query=True)
        at = next(i for i, (event, _) in enumerate(events) if event == "call_start")
        identity = events[at][1]["call_id"]
        admission = deepcopy(ledger)
        entry = admission["calls"][0]
        entry.update(status="admitted", generation_started=False, usage=None, transport_intents=[])
        admission["input_tokens"] = admission["output_tokens"] = 0
        admission["halted"] = None
        events[at:at] = [recovered(audit_event="call_start", provider_call_id=identity, stage="query"),
            ("requery_budget_event", {"trace_id": "synthetic-trace", "provider_call_id": identity,
                                    "budget_event": "call_start", "ledger": admission})]
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")

    def test_recovery_count_and_intentional_wait_are_strictly_bound(self):
        for attempts, wait in ((2, 0.01), (3, 0.03)):
            _validate_ledger_io_fields([recovered(replace_attempts=attempts, intentional_wait_seconds=wait)])
        for attempts, wait in ((True, 0.01), (1, 0), (4, 0.03), (2.0, 0.01),
                               (2, True), (2, 0), (2, 0.03), (3, 0.01),
                               (3, 0.031), (3, float("inf")), (3, float("nan")),
                               (2, 10 ** 500)):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                _validate_ledger_io_fields([recovered(replace_attempts=attempts, intentional_wait_seconds=wait)])

    def test_schema_refuses_private_text_other_errors_unknown_phase_and_partial_context(self):
        cases = [{"path": "synthetic-private-path"}, {"call_id": "a" * 32},
            {"first_failure": {**FIRST_FAILURE, "message": "synthetic-private-error"}},
            {"first_failure": {**FIRST_FAILURE, "operation": "write"}},
            {"first_failure": {**FIRST_FAILURE, "errno": True}},
            {"first_failure": {**FIRST_FAILURE, "errno": 28}},
            {"first_failure": {**FIRST_FAILURE, "winerror": None}},
            {"audit_event": "unknown-phase", "provider_call_id": "a" * 32, "stage": "query"},
            {"audit_event": "call_end", "provider_call_id": "a" * 32, "stage": "unknown-stage"},
            {"audit_event": "call_end", "provider_call_id": "A" * 32, "stage": "query"},
            {"audit_event": "call_end"}, {"provider_call_id": "a" * 32}, {"stage": "query"}]
        for updates in cases:
            with self.subTest(keys=tuple(updates)), self.assertRaises(ValueError) as caught:
                _validate_ledger_io_fields([recovered(**updates)])
            self.assertNotIn("synthetic-private", str(caught.exception))
        for key in recovered()[1]:
            event, fields = recovered()
            del fields[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                _validate_ledger_io_fields([(event, fields)])

    def test_same_context_budget_event_cannot_be_missing_mismatched_or_reused(self):
        for partial in (False, True):
            events, start, at, _ = self.provider_recovery()
            del events[at + 1]
            with self.subTest(partial=partial), self.assertRaisesRegex(ValueError, "budget binding"):
                verify_active_turn(events, start, partial=partial)
        for key, value in (("trace_id", "other-trace"), ("provider_call_id", "b" * 32),
                           ("budget_event", "input_gate")):
            events, start, at, _ = self.provider_recovery()
            events[at + 1][1][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "budget binding mismatch"):
                verify_active_turn(events, start)
        events, start, at, _ = self.provider_recovery()
        events.insert(at, deepcopy(events[at]))
        with self.assertRaisesRegex(ValueError, "budget binding mismatch"):
            verify_active_turn(events, start)

    def test_recovery_cannot_fill_missing_provider_end_even_for_partial_records(self):
        for partial in (False, True):
            events, start, _, _ = self.provider_recovery()
            events[:] = [(event, value) for event, value in events if event != "call_end"]
            with self.subTest(partial=partial), self.assertRaisesRegex(ValueError, "provider end missing"):
                verify_active_turn(events, start, partial=partial)

    def test_terminal_recovery_cannot_bind_a_stale_admitted_snapshot_or_changed_usage(self):
        for partial in (False, True):
            events, start, at, _ = self.provider_recovery()
            bound = deepcopy(events[at + 1][1]["ledger"])
            bound["calls"][0]["status"] = "admitted"
            events[at + 1][1]["ledger"] = bound
            with self.subTest(partial=partial), self.assertRaisesRegex(ValueError, "terminal usage/provider mismatch"):
                verify_active_turn(events, start, partial=partial)
        events, start, at, _ = self.provider_recovery()
        bound = deepcopy(events[at + 1][1]["ledger"])
        bound["calls"][0]["usage"]["input_tokens"] += 1
        bound["input_tokens"] += 1
        events[at + 1][1]["ledger"] = bound
        with self.assertRaisesRegex(ValueError, "terminal usage/provider mismatch"):
            verify_active_turn(events, start)

    def test_recovery_cannot_be_moved_after_its_provider_terminal_or_start_record(self):
        events, start, at, _ = self.provider_recovery()
        provider_end = events.pop(at + 2)
        events.insert(at, provider_end)
        with self.assertRaisesRegex(ValueError, "provider end ordering mismatch"):
            verify_active_turn(events, start)
        events, start, at, _ = self.provider_recovery()
        events[at][1]["audit_event"] = "call_start"
        events[at + 1][1]["budget_event"] = "call_start"
        with self.assertRaisesRegex(ValueError, "provider start ordering mismatch"):
            verify_active_turn(events, start)

    def test_expired_provider_start_is_explicit_but_future_start_cannot_be_missing(self):
        events, start, _, _ = self.provider_recovery()
        events[:] = [(event, value) for event, value in events if event != "call_start"]
        report = verify_active_turn(events, start, partial=True)
        self.assertEqual(report["status"], "retention_partial")
        self.assertIn("expired_ledger_io_recovery_provider_binding", report["warnings"])
        next(value for event, value in events if event == "requery_ledger_io_recovered")["audit_event"] = "call_start"
        with self.assertRaisesRegex(ValueError, "provider start missing"):
            verify_active_turn(events, start, partial=True)

    def test_recovery_provider_stage_and_turn_trace_tampering_are_rejected(self):
        for key, value in (("stage", "reply"), ("trace_id", "other-trace")):
            events, start, at, _ = self.provider_recovery()
            events[at][1][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "stage/trace mismatch|trace binding mismatch"):
                verify_active_turn(events, start)
        events, start, at, _ = self.provider_recovery()
        events[at + 1][1]["ledger"]["calls"][0]["stage"] = "reply"
        with self.assertRaisesRegex(ValueError, "budget stage binding mismatch"):
            verify_active_turn(events, start)

    def test_post_turn_recovery_needs_independent_terminal_budget_event(self):
        events, start, ledger = self.host_turn()
        events.append(recovered(replace_attempts=3, intentional_wait_seconds=0.03))
        report = verify_active_turn(events, start)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("ledger_io_recovery_terminal_save_unconfirmed", report["warnings"])
        events.append(self.terminal_budget(ledger))
        report = verify_active_turn(events, start)
        self.assertEqual(report["status"], "complete", report)
        events[-1][1]["ledger"]["phase"] = "unknown-phase"
        with self.assertRaises(ValueError):
            verify_active_turn(events, start)

    def test_post_turn_provider_recovery_is_rejected(self):
        events, start, at, _ = self.provider_recovery()
        moved = events.pop(at)
        events.append(moved)
        with self.assertRaisesRegex(ValueError, "not cleanup metadata"):
            verify_active_turn(events, start)

    def test_post_turn_cleanup_recovery_cannot_reuse_one_terminal_receipt(self):
        events, start, ledger = self.host_turn()
        events.extend([recovered(), recovered(), self.terminal_budget(ledger)])
        with self.assertRaisesRegex(ValueError, "cleanup budget binding reused"):
            verify_active_turn(events, start)

    def test_multiple_preterminal_lifecycle_recoveries_can_share_real_finish_confirmation(self):
        events, start, ledger = self.host_turn()
        events[1:1] = [recovered(), recovered()]
        events.append(self.terminal_budget(ledger))
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")

    def test_permanent_failure_cannot_be_cleared_by_later_recovery(self):
        events, start, ledger = self.host_turn()
        at = next(i for i, (event, _) in enumerate(events) if event == "requery_stop")
        events.insert(at, ("requery_ledger_io_failure", {"trace_id": "synthetic-trace",
            "audit_event": None, "provider_call_id": None, "stage": None,
            "io_failure": deepcopy(FIRST_FAILURE)}))
        next(value for event, value in events if event == "requery_stop")["ledger_io_failure"] = deepcopy(FIRST_FAILURE)
        events.extend([recovered(), self.terminal_budget(ledger)])
        with self.assertRaisesRegex(ValueError, "after permanent failure"):
            verify_active_turn(events, start)

    def test_recovered_prefix_does_not_erase_later_permanent_first_cause(self):
        events, start, ledger = self.host_turn()
        events.insert(1, recovered())
        at = next(i for i, (event, _) in enumerate(events) if event == "requery_stop")
        permanent = {"operation": "write", "errno": 28, "winerror": None}
        events.insert(at, ("requery_ledger_io_failure", {"trace_id": "synthetic-trace",
            "audit_event": None, "provider_call_id": None, "stage": None, "io_failure": permanent}))
        stop = next(value for event, value in events if event == "requery_stop")
        stop["ledger_io_failure"] = deepcopy(permanent)
        events.append(self.terminal_budget(ledger))
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")
        stop["ledger_io_failure"] = deepcopy(FIRST_FAILURE)
        with self.assertRaisesRegex(ValueError, "first failure binding mismatch"):
            verify_active_turn(events, start)

    def test_missing_trace_recovery_is_not_silently_skipped(self):
        log = ScratchLog(self.fixture.root / "missing-recovery-trace")
        event, fields = recovered()
        del fields["trace_id"]
        try:
            log.write(event, **fields)
        finally:
            log.close()
        self.assertTrue(verify_runs([log.path])["issues"])

    def test_recovery_only_retained_suffix_is_explicitly_partial_without_claiming_closure(self):
        timestamp = [datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)]
        log = ScratchLog(self.fixture.root / "retained-recovery", clock=lambda: timestamp[0])
        try:
            log.write("turn_start", trace_id="synthetic-trace", message_id="synthetic-message",
                      run_record_version=3, input={"text": "synthetic query"})
            timestamp[0] += timedelta(hours=23)
            event, fields = recovered()
            log.write(event, **fields)
            log.prune(now=datetime(2026, 10, 9, 0, 1, 1, tzinfo=timezone.utc))
        finally:
            log.close()
        report = verify_runs(list((self.fixture.root / "retained-recovery").glob("*.jsonl")))
        self.assertEqual(report["counts"]["retention_partial"], 1, report)
        self.assertEqual(report["issues"], [])
        self.assertIn("ledger_io_recovery_terminal_save_unconfirmed", report["turns"][0]["warnings"])

    def test_new_recovery_event_needs_v3_or_retention_evidence(self):
        for version in (None, 1, 2):
            log = ScratchLog(self.fixture.root / ("old-recovery-version-" + str(version)))
            try:
                log.write("turn_start", trace_id="synthetic-trace", message_id="synthetic-message",
                          run_record_version=version, input={"text": "synthetic query"})
                event, fields = recovered()
                log.write(event, **fields)
            finally:
                log.close()
            report = verify_runs([log.path])
            self.assertEqual(report["counts"]["invalid"], 1, report)
        log = ScratchLog(self.fixture.root / "undeclared-recovery-suffix")
        try:
            event, fields = recovered()
            log.write(event, **fields)
        finally:
            log.close()
        self.assertEqual(verify_runs([log.path])["counts"]["invalid"], 1)

    def test_legacy_synthetic_turn_without_new_metadata_remains_complete(self):
        events, start, _ = self.host_turn(query=True)
        self.assertEqual(verify_active_turn(events, start)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
