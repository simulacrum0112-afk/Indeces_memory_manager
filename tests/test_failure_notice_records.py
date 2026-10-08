"""Offline bindings for natural failure notices and unchanged legacy history."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from indeces.run_records import _validate_failure_notices, verify_runs
from indeces.scratch import ScratchLog, verify
from indeces.user_notices import NOTICE_POLICY, user_notice


class FailureNoticeRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.events = [
            ("turn_start", {"trace_id": "synthetic-trace", "message_id": "synthetic-message",
                            "scope": "10:20", "knowledge_scope": "10:knowledge",
                            "input": {"text": "synthetic query", "author_id": "30"},
                            "run_record_version": 1, "notice_policy": NOTICE_POLICY}),
            ("turn_end", {"trace_id": "synthetic-trace", "status": "failed",
                          "code": "input_token_limit", "error_type": "GovernedError"}),
            ("failure_notice_delivered", {"trace_id": "synthetic-trace",
                                          "notice_policy": NOTICE_POLICY,
                                          "reason": "input_token_limit",
                                          "receipt": {"ids": ["synthetic-receipt"],
                                                      "text": user_notice("input_token_limit")}}),
        ]

    def write_report(self, events, name):
        log = ScratchLog(self.root / name)
        try:
            for event, fields in events:
                log.write(event, **fields)
        finally:
            log.close()
        verify(log.path)
        return verify_runs([log.path])

    def test_versioned_natural_notice_is_bound_to_failed_turn(self):
        report = self.write_report(self.events, "valid")
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["issues"], [])

    def test_hash_valid_notice_tampering_is_rejected(self):
        for case in ("text", "reason", "end_code", "policy", "removed_policy",
                     "removed_start_policy", "ids", "duplicate", "ordering"):
            with self.subTest(case=case):
                events = deepcopy(self.events)
                start, end, notice = (f for _, f in events)
                if case == "text":
                    notice["receipt"]["text"] += " input_token_limit synthetic-trace"
                elif case == "reason":
                    notice["reason"] = "stage_timeout"
                    notice["receipt"]["text"] = user_notice("stage_timeout")
                elif case == "end_code":
                    end["code"] = "stage_timeout"
                elif case == "policy":
                    notice["notice_policy"] = "unknown_notice_policy"
                elif case == "removed_policy":
                    del notice["notice_policy"]
                elif case == "removed_start_policy":
                    del start["notice_policy"]
                elif case == "ids":
                    notice["receipt"]["ids"] = []
                elif case == "duplicate":
                    events.append(deepcopy(events[-1]))
                else:
                    events[1], events[2] = events[2], events[1]
                report = self.write_report(events, "tampered-" + case)
                self.assertEqual(report["counts"]["invalid"], 1, report)
                self.assertTrue(report["issues"], report)

    def test_historical_unmarked_notice_text_remains_readable(self):
        events = deepcopy(self.events)
        del events[0][1]["notice_policy"]
        del events[-1][1]["notice_policy"]
        del events[-1][1]["reason"]
        events[-1][1]["receipt"]["text"] = "Legacy fixed receipt (input_token_limit); trace=old"
        report = self.write_report(events, "legacy")
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["issues"], [])

    def test_retained_suffix_checks_template_but_reports_missing_reason_binding(self):
        report = {"warnings": []}
        _validate_failure_notices(self.events[-1:], report, partial=True)
        self.assertEqual(report["warnings"], ["expired_failure_notice_binding"])
        changed = deepcopy(self.events[-1:])
        changed[0][1]["receipt"]["text"] = "Forged natural-looking answer."
        with self.assertRaisesRegex(ValueError, "failure notice text mismatch"):
            _validate_failure_notices(changed, {"warnings": []}, partial=True)
        with self.assertRaisesRegex(ValueError, "failed turn end missing"):
            _validate_failure_notices(self.events[-1:])


if __name__ == "__main__":
    unittest.main()
