from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import reservation
from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
from indeces.context import compaction_prefix, encode, groups, history_cost, history_data, raw_capacity, reply_messages
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage, ModelResult
from indeces.runtime import Runtime
from indeces.store import Store
from indeces import prompts


def message(identity, *, text="a new question", channel="20", author="30"):
    return IncomingMessage(identity, channel, "10", author, "Alice", text, "2026-09-30T12:00:00+00:00")


def rows(count=4):
    result = []
    for index in range(count):
        for role in ("user", "assistant"):
            result.append({"seq": len(result) + 1, "turn_id": "turn-" + str(index), "role": role,
                           "author_id": "Alice" if role == "user" else "bot", "content": "evidence " * 20})
    return result


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class SummaryAdapter:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []

    async def call(self, stage, instructions, messages, trace_id, schema=None):
        self.calls.append({"stage": stage, "instructions": instructions, "messages": deepcopy(messages),
                           "trace_id": trace_id, "schema": deepcopy(schema)})
        value = self.outcomes.popleft()
        if isinstance(value, Exception):
            raise value
        return ModelResult(value, 10, 5, "summary-response", 0.001)


class ContextPolicyTests(unittest.TestCase):
    def test_between_low_and_high_watermarks_does_not_trigger(self):
        history = rows()
        cost = history_cost(history)
        self.assertGreater(cost, int((cost + 1) * 0.70))
        self.assertEqual(compaction_prefix(history, cost + 1, 0.70), [])
        self.assertEqual(compaction_prefix(history, cost, 0.70), [])

    def test_overflow_compacts_whole_oldest_turns_to_low_watermark(self):
        history = rows()
        capacity = history_cost(history) - 1
        prefix = compaction_prefix(history, capacity, 0.70)
        retained = history[len(prefix):]
        self.assertTrue(prefix)
        self.assertEqual(prefix, history[:len(prefix)])
        self.assertLessEqual(history_cost(retained), int(capacity * 0.70))
        self.assertEqual({r["turn_id"] for r in prefix} & {r["turn_id"] for r in retained}, set())
        self.assertEqual(len(prefix) % 2, 0)
        previous_prefix = prefix[:-len(groups(prefix)[-1])]
        self.assertGreater(history_cost(history[len(previous_prefix):]), int(capacity * 0.70))

    def test_atomic_oversize_turn_is_not_partially_retained(self):
        history = rows(1)
        history[0]["content"] = "中" * 2000
        self.assertEqual(compaction_prefix(history, 1000, 0.70), history)
        self.assertEqual(compaction_prefix([], 1000, 0.70), [])

    def test_history_metadata_and_current_author_are_preserved_separately(self):
        history = rows(1)
        current = message("new", text="current words", author="person-99")
        result = reply_messages("source-attributed summary", history, [{"source_id": "file-1", "text": "assertion"}], current)
        self.assertEqual([item["role"] for item in result], ["user", "user"])
        data = json.loads(result[0]["content"].split("\n", 1)[1])
        self.assertEqual(data["recent_observations"], history_data(history))
        self.assertEqual(data["recent_observations"][0]["author_id"], "Alice")
        self.assertEqual(data["recent_observations"][1]["role"], "assistant")
        self.assertEqual(data["memory_citations"][0]["source_id"], "file-1")
        self.assertEqual(json.loads(result[1]["content"])["current_author_id"], "person-99")

    def test_raw_capacity_reserves_fixed_input_knowledge_and_full_checkpoint(self):
        current = message("current")
        config = SimpleNamespace(adapter=SimpleNamespace(budgets={"reply": Budget(10000, 100, 1)}),
                                 runtime=RuntimeConfig(summary_max_bytes=1024))
        capacity = raw_capacity(config, "instructions", current, [])
        self.assertEqual(capacity, 10000 - reservation("instructions", reply_messages("", [], [], current)) - 1024)
        self.assertLess(raw_capacity(config, "instructions", current, [{"text": "x" * 1000}]), capacity)
        config.adapter.budgets["reply"] = Budget(100, 100, 1)
        with self.assertRaises(GovernedError) as caught:
            raw_capacity(config, "instructions", current, [])
        self.assertEqual(caught.exception.code, "fixed_context_too_large")


class SummaryCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.current = message("current")
        for index in range(4):
            old = message("old-" + str(index), text="source " * 30)
            self.store.begin(old)
            self.store.finish(old, DeliveryReceipt(("reply-" + str(index),), "reply " * 30))
        self.store.begin(self.current)
        self.runtime = Runtime.__new__(Runtime)
        self.runtime.config = SimpleNamespace(name="Indeces", runtime=RuntimeConfig(summary_max_bytes=256),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {
                "reply": Budget(10000, 100, 1), "summary": Budget(10000, 100, 1), "label": Budget(10000, 100, 1)}))
        self.runtime.store = self.store
        self.runtime.scratch = Scratch()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def old_rows(self):
        return self.store.history(self.current.scope, exclude_turn=self.current.message_id)

    async def summarize(self, capacity):
        with patch("indeces.runtime.raw_capacity", return_value=capacity):
            return await self.runtime._summary(self.current, [], "trace-summary")

    async def test_valid_summary_checkpoint_covers_exact_whole_prefix_and_retains_raw(self):
        original = self.old_rows()
        capacity = history_cost(original) - 1
        expected = compaction_prefix(original, capacity, 0.70)
        self.runtime.adapter = SummaryAdapter([encode({"summary": "Alice's previous discussion remains unresolved."})])
        messages = await self.summarize(capacity)
        checkpoint = self.store.checkpoint(self.current.scope)
        self.assertEqual(json.loads(checkpoint["source_ids"]), [r["seq"] for r in expected])
        self.assertEqual(checkpoint["through_seq"], expected[-1]["seq"])
        self.assertIsNone(checkpoint["previous_id"])
        request = json.loads(self.runtime.adapter.calls[0]["messages"][0]["content"])
        self.assertEqual(request["source_prefix"], history_data(expected))
        self.assertEqual(request["previous_summary"], "")
        self.assertNotIn("current", {r["message_id"] for r in request["source_prefix"]})
        recent = json.loads(messages[0]["content"].split("\n", 1)[1])["recent_observations"]
        self.assertEqual(recent, history_data(original[len(expected):]))
        self.assertEqual(self.old_rows(), original)  # archival originals are retained
        self.assertEqual(len(self.runtime.adapter.calls), 1)

    async def test_summary_failure_keeps_previous_frontier_and_all_raw_evidence(self):
        original = self.old_rows()
        previous = self.store.save_checkpoint(self.current.scope, original[:2], "prior attributed summary", "old-trace")
        pending = original[2:]
        self.runtime.adapter = SummaryAdapter([GovernedError("stage_timeout")])
        with self.assertRaises(GovernedError) as caught:
            await self.summarize(history_cost(pending) - 1)
        self.assertEqual(caught.exception.code, "stage_timeout")
        self.assertEqual(self.store.checkpoint(self.current.scope), previous)
        self.assertEqual(self.old_rows(), original)
        self.assertEqual(len(self.runtime.adapter.calls), 1)
        self.assertFalse(any(event == "checkpoint_saved" for event, _ in self.runtime.scratch.events))

    async def test_invalid_summary_never_advances_coverage_or_retries(self):
        original = self.old_rows()
        for value in ('{"summary":"a","summary":"b"}', '{"summary":""}', '{"summary":7}',
                      encode({"summary": "中" * 86}), '{"summary":"valid","extra":true}'):
            with self.subTest(value=value):
                self.runtime.adapter = SummaryAdapter([value])
                with self.assertRaises(GovernedError) as caught:
                    await self.summarize(history_cost(original) - 1)
                self.assertEqual(caught.exception.code, "invalid_summary")
                self.assertEqual(self.store.checkpoint(self.current.scope)["through_seq"], 0)
                self.assertEqual(self.old_rows(), original)
                self.assertEqual(len(self.runtime.adapter.calls), 1)

    async def test_summary_budget_admits_whole_turn_and_reports_remaining_backlog(self):
        original = self.old_rows()
        first_turn = groups(original)[0]
        input_message = [{"role": "user", "content": encode({"previous_summary": "",
            "source_prefix": history_data(first_turn), "summary_max_utf8_bytes": 256})}]
        limit = reservation(prompts.SUMMARY, input_message, prompts.SUMMARY_SCHEMA)
        self.runtime.config.adapter.budgets["summary"] = Budget(limit, 100, 1)
        self.runtime.adapter = SummaryAdapter([encode({"summary": "One whole turn was summarized."})])
        capacity = history_cost(groups(original)[-1]) + 1
        with self.assertRaises(GovernedError) as caught:
            await self.summarize(capacity)
        self.assertEqual(caught.exception.code, "context_backlog_requires_next_turn")
        checkpoint = self.store.checkpoint(self.current.scope)
        self.assertEqual(json.loads(checkpoint["source_ids"]), [r["seq"] for r in first_turn])
        self.assertEqual(checkpoint["through_seq"], first_turn[-1]["seq"])
        self.assertEqual(self.old_rows(), original)
        self.assertEqual(len(self.runtime.adapter.calls), 1)

    async def test_oversize_first_turn_leaves_no_checkpoint(self):
        original = self.old_rows()
        self.runtime.config.adapter.budgets["summary"] = Budget(10, 100, 1)
        self.runtime.adapter = SummaryAdapter([])
        with self.assertRaises(GovernedError) as caught:
            await self.summarize(history_cost(original) - 1)
        self.assertEqual(caught.exception.code, "summary_prefix_too_large")
        self.assertEqual(self.store.checkpoint(self.current.scope)["through_seq"], 0)
        self.assertEqual(self.runtime.adapter.calls, [])

    async def test_no_maintenance_below_high_watermark(self):
        original = self.old_rows()
        self.runtime.adapter = SummaryAdapter([])
        messages = await self.summarize(history_cost(original))
        self.assertEqual(self.runtime.adapter.calls, [])
        self.assertEqual(self.store.checkpoint(self.current.scope)["through_seq"], 0)
        self.assertEqual(json.loads(messages[0]["content"].split("\n", 1)[1])["recent_observations"], history_data(original))


class RuntimeConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.scratch = Scratch()
        self.config = SimpleNamespace(name="Indeces", runtime=RuntimeConfig(summary_max_bytes=256),
            discord=DiscordConfig(guild_id="10"),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {
                stage: Budget(10000, 100, 1) for stage in ("reply", "summary", "label")}))
        self.deliveries = []

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def deliver(self, text):
        self.deliveries.append(text)
        return DeliveryReceipt(("delivery-1",), text)

    async def test_normal_message_has_only_reply_call_and_never_labels_chat(self):
        adapter = SummaryAdapter(["a final reply"])
        runtime = Runtime(self.config, self.store, adapter, self.scratch)
        current = message("chat-message")
        await runtime.process(current, self.deliver)
        self.assertEqual([call["stage"] for call in adapter.calls], ["reply"])
        self.assertEqual(self.deliveries, ["a final reply"])
        self.assertEqual([row["role"] for row in self.store.history(current.scope)], ["user", "assistant"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)

    async def test_duplicate_source_is_not_replayed_or_labeled(self):
        adapter = SummaryAdapter(["a final reply"])
        runtime = Runtime(self.config, self.store, adapter, self.scratch)
        current = message("chat-message")
        await runtime.process(current, self.deliver)
        await runtime.process(current, self.deliver)
        self.assertEqual([call["stage"] for call in adapter.calls], ["reply"])
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(len(self.store.history(current.scope)), 2)
        self.assertTrue(any(event == "duplicate_ignored" for event, _ in self.scratch.events))

    async def test_runtime_explicitly_selects_static_ranking_and_records_basis(self):
        adapter = SummaryAdapter(["a final reply"])
        runtime = Runtime(self.config, self.store, adapter, self.scratch)
        runtime.graph.add(runtime.knowledge_scope, "synthetic-source", "synthetic-author", [
            {"text": "alpha beta", "quote": "alpha beta", "marks": ["alpha", "beta"]}], 1)
        current = message("static-runtime", text="alpha beta")
        with patch.object(runtime.graph, "retrieve", wraps=runtime.graph.retrieve) as retrieve:
            await runtime.process(current, self.deliver)
        self.assertEqual(retrieve.call_args.kwargs["ranking_mode"], "static")
        observations = [fields["audit"] for event, fields in self.scratch.events if event == "memory_observation"]
        self.assertEqual(observations[0]["selection"]["weight_basis"], "static_npmi")
        self.assertEqual(observations[0]["selection"]["edge_statistics"][0]["effective_score"], 1.0)
        self.assertEqual(observations[0]["selection"]["edge_statistics"][0]["dynamic_score"], 1.99)
        self.assertEqual(self.deliveries, ["a final reply"])

    async def test_failed_reply_notice_does_not_enter_assistant_history(self):
        adapter = SummaryAdapter([GovernedError("stage_timeout")])
        runtime = Runtime(self.config, self.store, adapter, self.scratch)
        current = message("failed-message")
        with patch("builtins.print"):
            await runtime.process(current, self.deliver)
        self.assertEqual([call["stage"] for call in adapter.calls], ["reply"])
        self.assertEqual([row["role"] for row in self.store.history(current.scope)], ["user"])
        self.assertEqual(len(self.deliveries), 1)
        self.assertIn("stage_timeout", self.deliveries[0])
        self.assertEqual(self.store.db.execute("SELECT status FROM turns WHERE message_id=?", (current.message_id,)).fetchone()[0], "stage_timeout")


if __name__ == "__main__":
    unittest.main()
