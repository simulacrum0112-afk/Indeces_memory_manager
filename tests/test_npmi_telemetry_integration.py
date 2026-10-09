"""Initial-query telemetry across synthetic selector, ledger and retained runs.

All stores and logs are temporary. Existing HTTP-shaped fixtures dispatch only
request overrides; no keys, private materials or external services are read.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import patch

from indeces.active_requery import TurnBudgetAdapter
from indeces.contracts import GovernedError
from indeces.memory import MemoryGraph
from indeces.memory_index import KeyedMemoryIndex
from indeces.model_selection import ModelNPMILabelSelector
from indeces.npmi_telemetry import NPMIRetrievalTelemetry, count, validate_receipt
from indeces.run_records import _validate_npmi_execution, verify_runs
from indeces.scratch import ScratchLog, canonical, verify
from indeces.selection_policy import RankedTopThreeSelector
from tests import test_active_requery as active_fixture
from tests import test_model_selection as model_fixture
from tests.test_selection_policy_integration import SelectorFixture


def events(entries, name):
    return [entry["fields"] for entry in entries if entry["event"] == name]


def pairs(entries):
    return [(entry["event"], deepcopy(entry["fields"])) for entry in entries]


class RetainedLogMixin:
    def rewritten(self, entries, name, *, change=None, omit=(), duplicate=False):
        log = ScratchLog(self.root / "rewritten" / name)
        try:
            for event, fields in pairs(entries):
                if event in omit:
                    continue
                if change:
                    change(event, fields)
                log.write(event, **fields)
                if duplicate and event == "npmi_retrieval":
                    log.write(event, **fields)
        finally:
            log.close()
        verify(log.path)
        return [log.path]

    def window(self, entries, name, boundary, *, change=None):
        # This is an actual removed prefix/checkpoint, rather than a rewritten
        # orphan trace labelled partial without retention evidence.
        now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        cutoff = now - timedelta(hours=24)
        clock = SimpleNamespace(value=cutoff - timedelta(seconds=1))
        log = ScratchLog(self.root / "retained" / name, clock=lambda: clock.value)
        try:
            for index, (event, fields) in enumerate(pairs(entries)):
                if index == boundary:
                    clock.value = cutoff + timedelta(seconds=1)
                if change:
                    change(event, fields)
                log.write(event, **fields)
            clock.value = now
            log.prune()
        finally:
            log.close()
        paths = sorted((self.root / "retained" / name).glob("*.jsonl"))
        for path in paths:
            verify(path)
        return paths

    def assert_invalid(self, paths):
        report = verify_runs(paths)
        self.assertTrue(report["issues"], report)
        self.assertGreaterEqual(report["counts"]["invalid"], 1, report)
        return report

    @staticmethod
    def changes():
        return {
            "event_identity": lambda f: f.update(event_id="synthetic-other-initial"),
            "counter": lambda f: f["telemetry"]["counters"].update(scored_candidates=9999),
            "stage": lambda f: f["telemetry"]["stages"]["precomputed_lookup"].update(completed_calls=0),
            "path": lambda f: f["telemetry"].update(path="not_entered", ranking_mode=None),
            "status": lambda f: f["telemetry"].update(status="failed", error_type="ValueError"),
            "flag": lambda f: f["telemetry"].update(formula_recomputed=True),
            "version": lambda f: f["telemetry"].update(version=True),
        }


class SelectorTelemetryIntegrationTests(RetainedLogMixin, SelectorFixture,
                                        unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_selector_fixture()
        self.seed_candidates()

    def tearDown(self):
        self.teardown_selector_fixture()

    async def test_default_async_selector_counts_one_completed_initial_query(self):
        result = await model_fixture.run_synthetic_console_trial(self)
        fields = events(result["entries"], "npmi_retrieval")
        self.assertEqual(len(fields), 1)
        receipt = fields[0]["telemetry"]
        graph = events(result["entries"], "retrieval_record")[0]["record"]["graph_audit"]
        validate_receipt(receipt, graph=graph)
        self.assertEqual(receipt["retrieval_calls"], 1)
        self.assertEqual(receipt["counters"]["scored_candidates"], 6)
        self.assertEqual(result["call_counts"]["prepare"], 1)
        self.assertEqual(result["call_counts"]["finish"], 1)
        self.assertEqual(fields[0]["event_id"], graph["event_id"])
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_invalid_model_choice_keeps_completed_scoring_and_failed_receipt(self):
        result = await model_fixture.run_synthetic_console_trial(self, invalid=True)
        receipt = events(result["entries"], "npmi_retrieval")[0]["telemetry"]
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_type"], "GovernedError")
        self.assertEqual(receipt["retrieval_calls"], 1)
        self.assertEqual(receipt["stages"]["candidate_scoring"]["completed_calls"], 1)
        self.assertEqual(result["call_counts"]["finish"], 0)
        self.assertEqual(events(result["entries"], "retrieval_record"), [])
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_prepare_lookup_failure_records_started_stage_without_model_call(self):
        with patch.object(KeyedMemoryIndex, "incident_edges",
                          side_effect=sqlite3.OperationalError("synthetic private lookup detail")):
            result = await model_fixture.run_synthetic_console_trial(self)
        receipt = events(result["entries"], "npmi_retrieval")[0]["telemetry"]
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["path"], "keyed_static_neighborhood_v1")
        self.assertEqual(receipt["stages"]["precomputed_lookup"]["calls"], 1)
        self.assertEqual(receipt["stages"]["precomputed_lookup"]["completed_calls"], 0)
        self.assertEqual(result["requests"], [])
        self.assertEqual(events(result["entries"], "retrieval_record"), [])
        self.assertNotIn("synthetic private lookup detail", repr(result["entries"]))
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_selection_wait_is_excluded_and_query_context_is_suspended(self):
        clock = SimpleNamespace(value=1.0)

        def tick():
            clock.value += 0.001
            return clock.value

        original = ModelNPMILabelSelector.choose

        async def waiting(selector, request, trace_id, **kwargs):
            clock.value += 100.0
            # An unrelated operation while the selector awaits cannot become an
            # initial native scoring count in this query-local receipt.
            count("scored_candidates", 99)
            await asyncio.sleep(0)
            return await original(selector, request, trace_id, **kwargs)

        # Replace only this module's clock object; runtime deadlines and the
        # event loop keep their real clocks and their existing budgets.
        with patch("indeces.npmi_telemetry.time", SimpleNamespace(perf_counter=tick)), \
                patch.object(ModelNPMILabelSelector, "choose", waiting):
            result = await model_fixture.run_synthetic_console_trial(self)
        receipt = events(result["entries"], "npmi_retrieval")[0]["telemetry"]
        graph = events(result["entries"], "retrieval_record")[0]["record"]["graph_audit"]
        validate_receipt(receipt, graph=graph)
        self.assertGreater(clock.value, 100)
        self.assertLess(receipt["elapsed_seconds"], 1.0)
        self.assertEqual(receipt["counters"]["scored_candidates"], 6)
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_selection_cancel_emits_failed_receipt_without_graph_or_reply(self):
        async def cancelled(*args, **kwargs):
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        outer = NPMIRetrievalTelemetry()
        with outer:
            with patch.object(ModelNPMILabelSelector, "choose", cancelled), \
                    self.assertRaises(asyncio.CancelledError):
                await model_fixture.run_synthetic_console_trial(self)
            # Cancellation restores the prior ContextVar owner, rather than
            # leaving the failed initial trace active in subsequent operations.
            count("scored_candidates", 7)
        self.assertEqual(outer.receipt()["counters"]["scored_candidates"], 7)
        path = next((self.root / "scratch" / "model-selection").glob("*.jsonl"))
        from indeces.scratch import read_records
        entries = list(read_records(path))
        receipt = events(entries, "npmi_retrieval")[0]["telemetry"]
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_type"], "CancelledError")
        self.assertEqual(receipt["retrieval_calls"], 1)
        self.assertEqual(receipt["counters"]["scored_candidates"], 6)
        self.assertEqual(events(entries, "retrieval_record"), [])
        self.assertEqual(events(entries, "answer_generated"), [])
        self.assertEqual(events(entries, "turn_end")[0]["status"], "cancelled")
        self.assertEqual(verify_runs([path])["issues"], [])

    async def test_count_gate_blocks_generation_without_ranked_fallback_or_retry(self):
        result = await model_fixture.run_synthetic_console_trial(self, count_limit=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection")])
        self.assertEqual(events(result["entries"], "answer_generated"), [])
        receipt = events(result["entries"], "npmi_retrieval")[0]["telemetry"]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(result["call_counts"]["finish"], 0)
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_v1_hash_valid_telemetry_mutations_are_rejected(self):
        result = await model_fixture.run_synthetic_console_trial(self)
        entries = result["entries"]
        self.assert_invalid(self.rewritten(entries, "v1-missing", omit=("npmi_retrieval",)))
        self.assert_invalid(self.rewritten(entries, "v1-duplicate", duplicate=True))
        for name, change in self.changes().items():
            with self.subTest(change=name):
                def mutate(event, fields, operation=change):
                    if event == "npmi_retrieval":
                        operation(fields)
                self.assert_invalid(self.rewritten(entries, "v1-" + name, change=mutate))
        for name, change in (
                ("trace", lambda e, f: f.update(trace_id="synthetic-orphan-telemetry")
                 if e == "npmi_retrieval" else None),
                ("declaration", lambda e, f: f.update(npmi_telemetry_version=True)
                 if e == "turn_start" else None)):
            self.assert_invalid(self.rewritten(entries, "v1-" + name, change=change))

    async def test_v1_retained_initial_telemetry_is_still_bound_after_start_expires(self):
        result = await model_fixture.run_synthetic_console_trial(self)
        entries = result["entries"]
        paths = self.window(entries, "v1-valid", 1)
        report = verify_runs(paths)
        self.assertEqual(report["counts"]["retention_partial"], 1, report)
        self.assertEqual(report["issues"], [], report)
        for name, change in self.changes().items():
            with self.subTest(change=name):
                def mutate(event, fields, operation=change):
                    if event == "npmi_retrieval":
                        operation(fields)
                self.assert_invalid(self.window(entries, "v1-" + name, 1, change=mutate))

    def test_off_flags_and_telemetry_preserve_native_graph_bytes(self):
        self.assertFalse(self.config.runtime.active_requery_enabled)
        self.assertFalse(self.config.runtime.path_requery_loop_enabled)
        self.assertFalse(self.config.runtime.communicability_enabled)
        self.assertFalse(self.config.runtime.path_hypotheses_enabled)
        graph = MemoryGraph(self.store.db, selector=RankedTopThreeSelector())
        expected_audit, actual_audit = {}, {}
        # Freeze the event first so both subsequent paths have the same replay
        # state. Comparing a first execution with its replay would mix the
        # legitimate observation/replay metadata with telemetry effects.
        graph.retrieve(self.scope, [], "alpha topic", 2.0,
                       event_id="synthetic-off-equivalence")
        expected = graph.retrieve(self.scope, [], "alpha topic", 2.0,
                                 event_id="synthetic-off-equivalence", audit=expected_audit)
        trace = NPMIRetrievalTelemetry()
        with trace:
            prepared = graph.prepare_retrieval(self.scope, [], "alpha topic", 2.0,
                                              event_id="synthetic-off-equivalence")
            decision = RankedTopThreeSelector().select(prepared.request)
            actual = graph.finish_retrieval(prepared, decision, audit=actual_audit)
        self.assertEqual(actual, expected)
        self.assertEqual(canonical(actual_audit), canonical(expected_audit))
        validate_receipt(trace.receipt(), graph=actual_audit)


class ActiveTelemetryIntegrationTests(RetainedLogMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Compose the fixture without inheriting its existing test methods.
        self.fixture = active_fixture.ActiveConsoleTests("test_actual_console_two_query_rounds_return_new_evidence_to_final_model")
        self.fixture.setUp()
        self.root = self.fixture.root

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def run_active(self):
        with redirect_stdout(io.StringIO()):
            await self.fixture.run_turn()
        path, entries = self.fixture.entries()
        return path, entries

    async def test_v3_multiple_queries_bind_exactly_one_receipt_to_initial_graph(self):
        path, entries = await self.run_active()
        telemetry = events(entries, "npmi_retrieval")
        self.assertEqual(len(telemetry), 1)
        initial = events(entries, "retrieval_record")[0]["record"]
        validate_receipt(telemetry[0]["telemetry"], graph=initial["graph_audit"])
        self.assertEqual(telemetry[0]["telemetry"]["counters"]["scored_candidates"], 0)
        self.assertEqual(len(events(entries, "requery_retrieval")), 3)
        self.assertEqual(verify_runs([path])["issues"], [])

    async def test_v3_full_and_partial_reject_initial_telemetry_tampering(self):
        _, entries = await self.run_active()
        self.assert_invalid(self.rewritten(entries, "v3-missing", omit=("npmi_retrieval",)))
        self.assert_invalid(self.rewritten(entries, "v3-duplicate", duplicate=True))
        for name, change in self.changes().items():
            with self.subTest(change=name):
                def mutate(event, fields, operation=change):
                    if event == "npmi_retrieval":
                        operation(fields)
                self.assert_invalid(self.rewritten(entries, "v3-full-" + name, change=mutate))
                self.assert_invalid(self.window(entries, "v3-partial-" + name, 1, change=mutate))
        for name, change in (
                ("trace", lambda e, f: f.update(trace_id="synthetic-orphan-telemetry")
                 if e == "npmi_retrieval" else None),
                ("declaration", lambda e, f: f.update(npmi_telemetry_version=True)
                 if e == "turn_start" else None)):
            self.assert_invalid(self.rewritten(entries, "v3-" + name, change=change))

    async def test_partial_bundle_still_binds_receipt_to_initial_not_later_graph(self):
        _, entries = await self.run_active()
        initial = events(entries, "retrieval_record")[0]["record"]
        later = events(entries, "requery_retrieval")[1]["record"]["graph_audit"]
        receipt = deepcopy(events(entries, "npmi_retrieval")[0]["telemetry"])
        selection = later["selection"]
        receipt["counters"].update(
            precomputed_edges_loaded=len(selection["edge_statistics"]),
            precomputed_weight_reads=len(selection["edge_statistics"]),
            neighbor_ordering_weight_uses=2 * sum(e["static_score"] > 0 for e in selection["edge_statistics"]),
            candidate_edge_weight_uses=sum(len(c["evidence"]) for c in selection["ranked_candidates"]),
            scored_candidates=len(selection["ranked_candidates"]))
        receipt["precomputed_npmi_used"] = any(receipt["counters"][key] for key in (
            "precomputed_weight_reads", "neighbor_ordering_weight_uses", "candidate_edge_weight_uses"))
        validate_receipt(receipt, graph=later)
        with self.assertRaises(ValueError):
            validate_receipt(receipt, graph=initial["graph_audit"])

        def changed(event, fields):
            if event == "npmi_retrieval":
                fields["telemetry"] = deepcopy(receipt)
        self.assert_invalid(self.window(entries, "v3-later-receipt", 1, change=changed))

    async def test_retention_expired_telemetry_reports_partial_without_reconstructing_it(self):
        _, entries = await self.run_active()
        boundary = next(index for index, entry in enumerate(entries)
                        if entry["event"] == "requery_action")
        paths = self.window(entries, "v3-expired-initial", boundary)
        report = verify_runs(paths)
        self.assertEqual(report["counts"]["retention_partial"], 1, report)
        self.assertEqual(report["issues"], [], report)
        self.assertIn("expired_turn_start", report["turns"][0]["warnings"])

    def test_retained_start_cannot_hide_initial_receipt_after_downstream_execution(self):
        start = {"trace_id": "synthetic-retained-initial", "message_id": "synthetic-initial",
                 "run_record_version": 3, "npmi_telemetry_version": 1}
        for event, fields in (
                ("requery_evidence", {}),
                ("call_start", {"stage": "query"})):
            with self.subTest(downstream=event):
                fields = dict(fields, trace_id=start["trace_id"])
                report = {"trace_id": start["trace_id"], "warnings": []}
                with self.assertRaisesRegex(ValueError, "NPMI.*missing"):
                    _validate_npmi_execution([("turn_start", start), (event, fields)],
                                             report, start=start, partial=True)
        # A prefix stopped inside initial selection has not completed the
        # initial native query yet; it must remain checkable as incomplete.
        report = {"trace_id": start["trace_id"], "warnings": []}
        _validate_npmi_execution([
            ("turn_start", start),
            ("model_selection_input", {"trace_id": start["trace_id"]})],
            report, start=start, partial=True)

    async def test_unknown_usage_remains_sticky_with_completed_initial_telemetry(self):
        self.fixture.query_error = GovernedError("synthetic_network_loss")
        path, entries = await self.run_active()
        self.assertEqual(len(self.fixture.requests), 2)
        receipt = events(entries, "npmi_retrieval")[0]["telemetry"]
        self.assertEqual(receipt["status"], "completed")
        ledger = self.fixture.saved_ledger()
        self.assertEqual(ledger["phase"], "halted")
        self.assertEqual(ledger["halted"], "requery_usage_unknown")
        self.assertEqual(ledger["automatic_retries"], 0)
        with self.assertRaises(GovernedError):
            TurnBudgetAdapter(self.fixture.adapter, self.fixture.config, self.fixture.scratch,
                              "synthetic-reopen", self.fixture.message.message_id,
                              self.fixture.message.scope)
        self.assertEqual(len(self.fixture.requests), 2)
        self.assertEqual(verify_runs([path])["issues"], [])

    async def test_ledger_creation_failure_blocks_transport_before_initial_lookup(self):
        with patch("indeces.active_requery._atomic_json",
                   side_effect=GovernedError("requery_ledger_write_failed")):
            path, entries = await self.run_active()
        self.assertEqual(self.fixture.requests, [])
        telemetry = events(entries, "npmi_retrieval")
        self.assertEqual(len(telemetry), 1)
        receipt = telemetry[0]["telemetry"]
        validate_receipt(receipt)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["path"], "not_entered")
        self.assertEqual(receipt["retrieval_calls"], 0)
        self.assertEqual(receipt["elapsed_seconds"], 0.0)
        self.assertTrue(all(value == 0 for value in receipt["counters"].values()))
        self.assertEqual(events(entries, "retrieval_record"), [])
        self.assertEqual(events(entries, "turn_end")[0]["code"], "requery_ledger_write_failed")
        self.assertEqual(verify_runs([path])["issues"], [])
        self.assert_invalid(self.rewritten(entries, "v3-init-failed-missing",
                                           omit=("npmi_retrieval",)))

    async def test_secondary_telemetry_io_failure_preserves_original_initialization_error(self):
        attempts = []
        original = self.fixture.scratch.write

        def write(event, **fields):
            if event == "npmi_retrieval":
                attempts.append(event)
                raise OSError("synthetic private telemetry IO detail")
            return original(event, **fields)

        with patch("indeces.active_requery._atomic_json",
                   side_effect=GovernedError("requery_ledger_write_failed")), \
                patch.object(self.fixture.scratch, "write", write):
            path, entries = await self.run_active()
        self.assertEqual(attempts, ["npmi_retrieval"])
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(events(entries, "turn_end")[0]["code"], "requery_ledger_write_failed")
        saved = self.fixture.store.db.execute("SELECT status FROM turns WHERE message_id=?",
                                              (self.fixture.message.message_id,)).fetchone()
        self.assertEqual(saved[0], "requery_ledger_write_failed")
        self.assertEqual(len(self.fixture.deliveries), 1)
        self.assertEqual(events(entries, "npmi_retrieval"), [])
        self.assertNotIn("synthetic private telemetry IO detail", repr(entries))
        # Losing the telemetry write cannot produce a complete audited turn.
        self.assert_invalid([path])

    async def test_initial_receipt_io_failure_stops_before_query_or_reply_generation(self):
        attempts = []
        original = self.fixture.scratch.write

        def write(event, **fields):
            if event == "npmi_retrieval":
                attempts.append(event)
                raise OSError("synthetic private telemetry IO detail")
            return original(event, **fields)

        with patch.object(self.fixture.scratch, "write", write):
            path, entries = await self.run_active()
        self.assertEqual(attempts, ["npmi_retrieval"])
        self.assertEqual(self.fixture.requests, [])
        self.assertEqual(events(entries, "turn_end")[0]["code"], "OSError")
        self.assertEqual(events(entries, "answer_generated"), [])
        self.assertEqual(self.fixture.saved_ledger()["phase"], "stopped")
        self.assert_invalid([path])


if __name__ == "__main__":
    unittest.main()
