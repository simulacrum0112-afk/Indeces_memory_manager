"""Offline regressions for local SQLite deadlines and safe failure metadata."""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from indeces.contracts import DeliveryReceipt, FailureNotice, IncomingMessage
from indeces.runtime import Runtime
from tests.test_context import SummaryAdapter
from tests.test_run_records import RecordFixture


_SCAN = """
    WITH RECURSIVE synthetic_scan(value) AS (
        VALUES(0) UNION ALL SELECT value+1 FROM synthetic_scan WHERE value<20000
    ) SELECT SUM(value) FROM synthetic_scan
"""
_SCAN_TOTAL = 20000 * 20001 // 2
_PRIVATE_SQL_NAME = "synthetic_do_not_log_table_name"


class LocalClock:
    """Replace Runtime's time reference without changing asyncio's clock."""
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def time(self):
        return time.time()


class LocalRetrievalDeadlineTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        # Leave synthetic turn time after the local cap so the fixed failure
        # receipt can be exercised independently of turn-time exhaustion.
        self.config.runtime = replace(self.config.runtime, turn_seconds=30)
        self.seed()
        self.adapter = SummaryAdapter([
            "A synthetic human answer [M1].",
            json.dumps({"action": "reply", "text": "A synthetic bot answer [M1]."}),
        ])
        self.runtime = Runtime(self.config, self.store, self.adapter, self.scratch)
        self.clock = LocalClock()
        self.deliveries = []
        self.sqlite_failures = []

    def tearDown(self):
        self.teardown_fixture()

    def message(self, identity="failed-bot", *, bot=True):
        return IncomingMessage(identity, "20", "10", "30", "Synthetic Peer" if bot else "Human",
                               "alpha topic", "2026-09-30T12:00:00+00:00", author_is_bot=bot)

    async def deliver(self, text):
        if isinstance(text, FailureNotice):
            text = text.text
        self.deliveries.append(text)
        return DeliveryReceipt(("synthetic-" + str(len(self.deliveries)),), text)

    async def run_turn(self, message=None):
        with redirect_stdout(io.StringIO()) as output:
            await self.runtime.process(message or self.message(), self.deliver)
        return output.getvalue()

    def last_failure(self):
        return next(fields for event, fields in reversed(self.scratch.events)
                    if event == "turn_end" and fields["status"] == "failed")

    def query(self, sql):
        try:
            return self.store.db.execute(sql).fetchone()[0]
        except sqlite3.OperationalError as error:
            self.sqlite_failures.append(error)
            raise

    def deadline_scan(self, *args, **kwargs):
        self.clock.elapsed = self.config.runtime.local_seconds + 1.0
        return self.query(_SCAN)

    def assert_bot_failed_with_notice(self, *, code, phase=None, sqlite_code=None, sqlite_name=None):
        fields = self.last_failure()
        self.assertEqual(fields["code"], code)
        self.assertEqual(fields["status"], "failed")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.deliveries), 1)
        self.assertIn(code, self.deliveries[0])
        self.assertNotIn("<@", self.deliveries[0])
        self.assertTrue(any(event == "failure_notice_delivered"
                            and item["receipt"]["text"] == self.deliveries[0]
                            for event, item in self.scratch.events))
        if phase is not None:
            self.assertEqual(fields["phase"], phase)
            self.assertEqual(fields["local_seconds"], self.config.runtime.local_seconds)
        if sqlite_code is not None:
            self.assertEqual(fields["error_type"], "OperationalError")
            self.assertEqual(fields["sqlite_errorcode"], sqlite_code)
            self.assertEqual(fields["sqlite_errorname"], sqlite_name)
        return fields

    async def test_real_progress_handler_interrupt_during_retrieval_is_governed_timeout(self):
        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=self.deadline_scan):
            await self.run_turn()
        self.assertEqual(len(self.sqlite_failures), 1)
        self.assertEqual(self.sqlite_failures[0].sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assert_bot_failed_with_notice(code="local_memory_timeout", phase="retrieval",
                                       sqlite_code=sqlite3.SQLITE_INTERRUPT, sqlite_name="SQLITE_INTERRUPT")
        self.assertEqual(self.store.db.execute("SELECT status FROM turns").fetchone()[0], "local_memory_timeout")

    async def test_real_progress_handler_interrupt_during_material_freeze_preserves_phase(self):
        with patch("indeces.runtime.time", self.clock), \
                patch("indeces.runtime.freeze_retrieval", side_effect=self.deadline_scan):
            await self.run_turn()
        self.assert_bot_failed_with_notice(code="local_memory_timeout", phase="freeze_retrieval",
                                       sqlite_code=sqlite3.SQLITE_INTERRUPT, sqlite_name="SQLITE_INTERRUPT")
        self.assertTrue(any(event == "memory_observation" for event, _ in self.scratch.events))

    async def test_real_progress_handler_interrupt_during_observation_preserves_phase(self):
        original_write = self.scratch.write

        def observe(event, **fields):
            if event == "memory_observation":
                self.deadline_scan()
            original_write(event, **fields)

        with patch("indeces.runtime.time", self.clock), patch.object(self.scratch, "write", side_effect=observe):
            await self.run_turn()
        self.assert_bot_failed_with_notice(code="local_memory_timeout", phase="memory_observation",
                                       sqlite_code=sqlite3.SQLITE_INTERRUPT, sqlite_name="SQLITE_INTERRUPT")

    async def test_external_sqlite_interrupt_before_deadline_keeps_operational_error(self):
        self.store.db.create_function("synthetic_external_interrupt", 0, self.store.db.interrupt)

        def externally_interrupted(*args, **kwargs):
            return self.query("SELECT synthetic_external_interrupt()")

        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=externally_interrupted):
            await self.run_turn()
        self.assertEqual(self.clock.elapsed, 0.0)
        self.assert_bot_failed_with_notice(code="OperationalError", sqlite_code=sqlite3.SQLITE_INTERRUPT,
                                       sqlite_name="SQLITE_INTERRUPT")

    async def test_other_sqlite_failure_reports_code_without_logging_exception_text(self):
        def bad_sql(*args, **kwargs):
            return self.query("SELECT * FROM " + _PRIVATE_SQL_NAME)

        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=bad_sql):
            output = await self.run_turn()
        self.assert_bot_failed_with_notice(code="OperationalError", sqlite_code=sqlite3.SQLITE_ERROR,
                                       sqlite_name="SQLITE_ERROR")
        self.assertNotIn(_PRIVATE_SQL_NAME, repr(self.scratch.events))
        self.assertNotIn(_PRIVATE_SQL_NAME, output)

    async def test_non_interrupt_sqlite_error_is_not_mislabeled_when_deadline_handler_previously_fired(self):
        def interrupt_then_sql_error(*args, **kwargs):
            try:
                self.deadline_scan()
            except sqlite3.OperationalError:
                return self.query("SELECT * FROM " + _PRIVATE_SQL_NAME)

        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=interrupt_then_sql_error):
            await self.run_turn()
        self.assertEqual([error.sqlite_errorcode for error in self.sqlite_failures],
                         [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_ERROR])
        self.assert_bot_failed_with_notice(code="OperationalError", sqlite_code=sqlite3.SQLITE_ERROR,
                                       sqlite_name="SQLITE_ERROR")

    async def test_non_integer_interrupt_code_cannot_convert_failure_or_enter_sqlite_metadata(self):
        def invalid_sqlite_metadata(*args, **kwargs):
            try:
                self.deadline_scan()
            except sqlite3.OperationalError as error:
                # A float compares equal to SQLITE_INTERRUPT, so checking only
                # equality would misclassify invalid metadata as a deadline.
                error.sqlite_errorcode = float(sqlite3.SQLITE_INTERRUPT)
                error.sqlite_errorname = "invalid private diagnostic " + _PRIVATE_SQL_NAME
                raise

        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=invalid_sqlite_metadata):
            output = await self.run_turn()
        fields = self.assert_bot_failed_with_notice(code="OperationalError")
        self.assertEqual(fields["error_type"], "OperationalError")
        self.assertNotIn("sqlite_errorcode", fields)
        self.assertNotIn("sqlite_errorname", fields)
        self.assertNotIn(_PRIVATE_SQL_NAME, repr(self.scratch.events))
        self.assertNotIn(_PRIVATE_SQL_NAME, output)

    async def test_elapsed_limit_alone_cannot_relabel_external_sqlite_interrupt(self):
        self.store.db.create_function("synthetic_external_interrupt", 0, self.store.db.interrupt)

        def externally_interrupted_after_elapsed(*args, **kwargs):
            self.clock.elapsed = self.config.runtime.local_seconds + 1.0
            # This simple statement has fewer than 1000 VM operations: the
            # external UDF interrupts it before the deadline callback runs.
            return self.query("SELECT synthetic_external_interrupt()")

        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=externally_interrupted_after_elapsed):
            await self.run_turn()
        self.assert_bot_failed_with_notice(code="OperationalError", sqlite_code=sqlite3.SQLITE_INTERRUPT,
                                       sqlite_name="SQLITE_INTERRUPT")

    async def test_python_work_past_local_deadline_is_rejected_without_sqlite_interrupt(self):
        from indeces.runtime import freeze_retrieval as original_freeze

        def slow_python_freeze(*args, **kwargs):
            result = original_freeze(*args, **kwargs)
            self.clock.elapsed = self.config.runtime.local_seconds + 1.0
            return result

        with patch("indeces.runtime.time", self.clock), \
                patch("indeces.runtime.freeze_retrieval", side_effect=slow_python_freeze):
            await self.run_turn()
        fields = self.assert_bot_failed_with_notice(code="local_memory_timeout", phase="freeze_retrieval")
        self.assertEqual(fields["error_type"], "GovernedError")
        self.assertNotIn("sqlite_errorcode", fields)
        self.assertNotIn("sqlite_errorname", fields)
        self.assertEqual(self.sqlite_failures, [])

    async def test_progress_handler_is_cleared_and_following_human_and_bot_turns_succeed(self):
        with patch("indeces.runtime.time", self.clock), \
                patch.object(self.runtime.graph, "retrieve", side_effect=self.deadline_scan):
            await self.run_turn()
        self.assert_bot_failed_with_notice(code="local_memory_timeout", phase="retrieval",
                                       sqlite_code=sqlite3.SQLITE_INTERRUPT, sqlite_name="SQLITE_INTERRUPT")
        # The old callback would immediately abort this statement at its first
        # progress checkpoint because its synthetic elapsed time remains > cap.
        self.assertEqual(self.store.db.execute(_SCAN).fetchone()[0], _SCAN_TOTAL)
        await self.run_turn(self.message("healthy-human", bot=False))
        await self.run_turn(self.message("healthy-bot"))
        self.assertEqual([call["stage"] for call in self.adapter.calls], ["reply", "reply"])
        self.assertEqual(self.deliveries[1:], ["A synthetic human answer [M1].", "A synthetic bot answer [M1]."])
        turns = dict(self.store.db.execute("SELECT message_id,status FROM turns"))
        self.assertEqual(turns, {"failed-bot": "local_memory_timeout", "healthy-human": "delivered",
                                 "healthy-bot": "delivered"})
        self.assertEqual([row["role"] for row in self.store.history("10:20")],
                         ["user", "user", "assistant", "user", "assistant"])


if __name__ == "__main__":
    unittest.main()
