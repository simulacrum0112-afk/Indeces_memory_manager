"""Retention boundaries preserve honest, locally checkable run receipts.

All texts, model responses, times, and Discord receipts are synthetic. No
credentials, network transport, or existing runtime data are read.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
from indeces.contracts import DeliveryReceipt, IncomingMessage
from indeces.memory import MemoryGraph
from indeces.run_records import verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, read_records, retention_checkpoint, verify
from indeces.store import Store


UTC = timezone.utc
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
OLD = CUTOFF - timedelta(seconds=1)
LIVE = CUTOFF + timedelta(seconds=1)


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class CollectingScratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class RetentionRecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / "state")
        self.scratch = CollectingScratch()
        self.config = SimpleNamespace(
            name="Indeces", discord=DiscordConfig("10"),
            runtime=RuntimeConfig(summary_max_bytes=256, turn_seconds=5),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {
                stage: Budget(16384, 2048, 1) for stage in ("reply", "summary", "label")}),
        )
        graph = MemoryGraph(self.store.db, self_marks=("Indeces",))
        graph.add("10:knowledge", "synthetic-source", "fixture", [
            {"text": "alpha topic source", "quote": "alpha topic source", "marks": ["alpha", "topic"]}], 1.0)

        async def request(path, payload):
            if path == "/responses/input_tokens":
                return {"input_tokens": 10}
            return {"id": "synthetic-response", "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Synthetic conclusion [M1]."}]}]}

        self.adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=request)
        self.runtime = Runtime(self.config, self.store, self.adapter, self.scratch)
        self.turns = []
        for index in (1, 2):
            start = len(self.scratch.events)

            async def deliver(text, identity=index):
                return DeliveryReceipt((f"synthetic-receipt-{identity}",), text)

            await self.runtime.process(IncomingMessage(
                f"synthetic-message-{index}", "20", "10", "30", "Synthetic user", "alpha topic",
                "2026-10-01T12:00:01+00:00"), deliver)
            self.turns.append(deepcopy(self.scratch.events[start:]))
        # Establish that fixture events pass the ordinary full-record contract.
        path = self.write_unpruned(self.turns[0] + self.turns[1], "fixture-check")
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 2, result)
        self.assertEqual(result["issues"], [])

    async def asyncTearDown(self):
        await self.adapter.close()
        self.store.close()
        self.temporary.cleanup()

    def write_unpruned(self, events, name):
        clock = Clock(LIVE)
        log = ScratchLog(self.root / name, clock=clock)
        try:
            for event, fields in events:
                log.write(event, **fields)
            path = log.path
        finally:
            log.close()
        verify(path)
        return path

    def write_window(self, expired, retained, name="window"):
        clock = Clock(OLD)
        log = ScratchLog(self.root / name, clock=clock)
        try:
            for event, fields in expired:
                log.write(event, **fields)
            clock.value = LIVE
            for event, fields in retained:
                log.write(event, **fields)
            path = log.path
            clock.value = NOW
            log.prune()
        finally:
            log.close()
        paths = sorted((self.root / name).glob("*.jsonl"))
        self.assertIn(path, paths)
        for item in paths:
            verify(item)
        return paths

    @staticmethod
    def boundary(events, name, *, after=False):
        return next(index + int(after) for index, (event, _) in enumerate(events) if event == name)

    def assert_partial(self, paths, *, turns=1, calls=0):
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["retention_partial"], turns, result)
        self.assertEqual(result["call_counts"]["retention_partial"], calls, result)
        self.assertEqual(result["counts"]["complete"], 0, result)
        self.assertEqual(result["counts"]["invalid"], 0, result)
        self.assertEqual(result["issues"], [], result)
        return result

    async def test_partial_turn_and_call_are_distinct_from_complete_live_turn(self):
        first, second = self.turns
        boundary = self.boundary(first, "call_start", after=True)
        paths = self.write_window(first[:boundary], first[boundary:] + second)
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["retention_partial"], 1, result)
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["call_counts"]["retention_partial"], 1, result)
        self.assertEqual(result["call_counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [], result)

    async def test_turn_missing_only_expired_start_is_reported_instead_of_skipped(self):
        first = self.turns[0]
        paths = self.write_window(first[:1], first[1:])
        result = self.assert_partial(paths)
        self.assertEqual(result["call_counts"]["complete"], 1, result)
        trace = first[0][1]["trace_id"]
        self.assertEqual(result["turns"][0]["trace_id"], trace)

    async def test_only_delivered_answer_tail_is_partial_without_fabricated_dependencies(self):
        first = self.turns[0]
        boundary = self.boundary(first, "answer_delivered")
        paths = self.write_window(first[:boundary], first[boundary:])
        result = self.assert_partial(paths)
        self.assertTrue(result["turns"][0]["warnings"], result)
        self.assertTrue(all(count == 0 for count in result["call_counts"].values()), result)

    async def test_passive_label_tail_is_partial_call_without_conversation_turn(self):
        start = len(self.scratch.events)
        await self.adapter.call("label", "Synthetic label prompt", [{"role": "user", "content": "Synthetic source"}], "synthetic-passive-trace")
        events = self.scratch.events[start:]
        boundary = self.boundary(events, "call_start", after=True)
        paths = self.write_window(events[:boundary], events[boundary:])
        result = self.assert_partial(paths, turns=0, calls=1)
        self.assertEqual(result["turns"], [], result)

    async def test_unplanned_orphan_call_and_turn_without_checkpoint_are_invalid(self):
        first = self.turns[0]
        boundary = self.boundary(first, "call_start", after=True)
        path = self.write_unpruned(first[boundary:], "unplanned-orphans")
        self.assertIsNone(retention_checkpoint(path))
        result = verify_runs([path])
        self.assertEqual(result["counts"]["retention_partial"], 0, result)
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertEqual(result["call_counts"]["invalid"], 1, result)
        self.assertTrue(result["issues"], result)

    async def test_partial_turn_retained_retrieval_digest_tampering_remains_invalid(self):
        first = deepcopy(self.turns[0])
        for event, fields in first:
            if event == "retrieval_record":
                fields["record_sha256"] = "0" * 64
        paths = self.write_window(first[:1], first[1:])
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertEqual(result["counts"]["retention_partial"], 0, result)
        self.assertTrue(result["issues"], result)

    async def test_partial_turn_retained_answer_citation_tampering_remains_invalid(self):
        first = deepcopy(self.turns[0])
        for event, fields in first:
            if event == "answer_delivered":
                fields["record"]["citations"][0]["start_character"] += 1
        boundary = self.boundary(first, "answer_delivered")
        paths = self.write_window(first[:boundary], first[boundary:])
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertEqual(result["counts"]["retention_partial"], 0, result)
        self.assertTrue(result["issues"], result)

    async def test_partial_turn_retained_answer_binding_mismatch_remains_invalid(self):
        first = deepcopy(self.turns[0])
        for event, fields in first:
            if event == "answer_generated":
                fields["retrieval_sha256"] = "0" * 64
        paths = self.write_window(first[:1], first[1:])
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertTrue(result["issues"], result)

    async def test_partial_call_retained_transport_contradiction_remains_invalid(self):
        first = deepcopy(self.turns[0])
        for event, fields in first:
            if event == "http_response" and fields["path"] == "/responses":
                fields["payload"]["id"] = "contradictory-response"
        boundary = self.boundary(first, "call_start", after=True)
        paths = self.write_window(first[:boundary], first[boundary:])
        result = verify_runs(paths)
        self.assertEqual(result["call_counts"]["invalid"], 1, result)
        self.assertEqual(result["call_counts"]["retention_partial"], 0, result)
        self.assertTrue(result["issues"], result)

    async def test_partial_call_completed_despite_retained_rejected_input_gate_is_invalid(self):
        for field, value in (("admitted", False), ("limit", 9)):
            with self.subTest(field=field):
                first = deepcopy(self.turns[0])
                for event, fields in first:
                    if event == "input_gate":
                        fields[field] = value
                boundary = self.boundary(first, "call_start", after=True)
                paths = self.write_window(first[:boundary], first[boundary:], name="gate-" + field)
                result = verify_runs(paths)
                self.assertEqual(result["call_counts"]["invalid"], 1, result)
                self.assertEqual(result["call_counts"]["retention_partial"], 0, result)
                self.assertTrue(result["issues"], result)

    async def test_partial_call_output_usage_cannot_exceed_retained_request_limit(self):
        first = deepcopy(self.turns[0])
        for event, fields in first:
            if event == "http_request" and fields["path"] == "/responses":
                fields["payload"]["max_output_tokens"] = 2
        boundary = self.boundary(first, "call_start", after=True)
        paths = self.write_window(first[:boundary], first[boundary:])
        result = verify_runs(paths)
        self.assertEqual(result["call_counts"]["invalid"], 1, result)
        self.assertEqual(result["call_counts"]["retention_partial"], 0, result)
        self.assertTrue(result["issues"], result)

    async def test_checkpoint_exposes_utc_window_and_only_live_partial_identifiers(self):
        first = self.turns[0]
        boundary = self.boundary(first, "call_start", after=True)
        expired_only = [("synthetic_past_observation", {"trace_id": "fully-expired-trace", "call_id": "fully-expired-call"})]
        paths = self.write_window(expired_only + first[:boundary], first[boundary:])
        checkpoint = retention_checkpoint(paths[0])
        self.assertIsNotNone(checkpoint)
        self.assertEqual(datetime.fromisoformat(checkpoint["cutoff"]), CUTOFF)
        self.assertEqual(datetime.fromisoformat(checkpoint["pruned_at"]), NOW)
        self.assertEqual(checkpoint["removed_through_sequence"], boundary + 1)
        self.assertNotIn("fully-expired-trace", checkpoint["partial_trace_ids"])
        self.assertNotIn("fully-expired-call", checkpoint["partial_call_ids"])
        self.assertIn(first[0][1]["trace_id"], checkpoint["partial_trace_ids"])
        call_id = next(fields["call_id"] for event, fields in first if event == "call_start")
        self.assertIn(call_id, checkpoint["partial_call_ids"])
        rows = list(read_records(paths[0]))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["sequence"], checkpoint["removed_through_sequence"] + 1)
        self.assertEqual(rows[0]["previous_hash"], checkpoint["removed_head_hash"])
        self.assertTrue(all(datetime.fromisoformat(row["timestamp"]) > CUTOFF for row in rows))
        serialized = paths[0].read_text(encoding="utf-8")
        self.assertNotIn("fully-expired-trace", serialized)
        self.assertNotIn("fully-expired-call", serialized)

    async def test_full_window_runs_stay_complete_without_partial_checkpoint(self):
        paths = self.write_window([], self.turns[0] + self.turns[1])
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["complete"], 2, result)
        self.assertEqual(result["counts"]["retention_partial"], 0, result)
        self.assertEqual(result["call_counts"]["complete"], 2, result)
        self.assertEqual(result["issues"], [], result)

    async def test_expired_auxiliary_event_does_not_make_retained_full_turn_partial(self):
        first = self.turns[0]
        auxiliary = [("synthetic_past_observation", {"trace_id": first[0][1]["trace_id"]})]
        paths = self.write_window(auxiliary, first)
        self.assertIsNotNone(retention_checkpoint(paths[0]))
        result = verify_runs(paths)
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["counts"]["retention_partial"], 0, result)
        self.assertEqual(result["call_counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [], result)

    async def test_deleted_previous_daily_file_declares_partial_tail_in_surviving_file(self):
        first = self.turns[0]
        boundary = self.boundary(first, "call_start", after=True)
        directory = self.root / "daily-boundary"
        prior_time = datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC)
        tail_time = datetime(2026, 10, 2, 0, 0, 2, tzinfo=UTC)
        now = datetime(2026, 10, 3, 0, 0, 1, tzinfo=UTC)
        prior = ScratchLog(directory, clock=Clock(prior_time))
        try:
            for event, fields in first[:boundary]:
                prior.write(event, **fields)
            prior_path = prior.path
        finally:
            prior.close()
        tail = ScratchLog(directory, clock=Clock(tail_time))
        try:
            for event, fields in first[boundary:]:
                tail.write(event, **fields)
            tail_path = tail.path
        finally:
            tail.close()
        cleanup = ScratchLog(directory, clock=Clock(now))
        cleanup.close()
        self.assertFalse(prior_path.exists())
        self.assertTrue(tail_path.exists())
        checkpoint = retention_checkpoint(tail_path)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["removed_through_sequence"], 0)
        self.assertEqual(checkpoint["removed_head_hash"], "0" * 64)
        self.assert_partial(sorted(directory.glob("*.jsonl")), calls=1)


if __name__ == "__main__":
    unittest.main()
