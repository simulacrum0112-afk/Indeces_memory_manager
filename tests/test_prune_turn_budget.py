"""Question fuses and native seams; all graph, wire and USD data are synthetic."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from indeces.claude_prune_budget import PruneBudget
from indeces.claude_prune_loop import PruneToolLoop
from indeces.contracts import GovernedError
from indeces.model_provider import create_model_adapter
from indeces.prune_native_plan import select_native_plan
from indeces.prune_turn_budget import PruneTurnBudget

try:
    from test_portable_claude_prune import (
        PRICE, Scratch, ScriptedTransport, SyntheticGraphFixture, config, native_message, tool,
    )
except ModuleNotFoundError:
    from tests.test_portable_claude_prune import (
        PRICE, Scratch, ScriptedTransport, SyntheticGraphFixture, config, native_message, tool,
    )


class TurnBudgetTests(unittest.TestCase):
    def test_absent_thinking_detail_is_unknown_without_double_counting_output(self):
        budget = PruneTurnBudget(100, 10)
        permit = budget.reserve("plan", 20, 20, 5)
        record = budget.settle(permit, 10, 5, native_usage={"input_tokens": 10, "output_tokens": 5})
        self.assertIsNone(record["thinking_tokens"])
        self.assertEqual(budget.snapshot()["spent_tokens"], 15)

    def test_unused_capacity_returns_and_next_call_gets_its_own_output_cap(self):
        budget = PruneTurnBudget(100, 10)
        first = budget.reserve("plan", 60, 20, 2)
        budget.settle(first, 10, 8)
        second = budget.reserve("prune", 60, 20, 2)
        self.assertEqual((second.input_cap, second.output_cap), (60, 20))
        self.assertEqual(budget.snapshot()["spent_tokens"], 18)
        self.assertEqual(budget.snapshot()["pending_tokens"], 80)

    def test_effective_caps_fit_remaining_without_allowing_zero_input(self):
        budget = PruneTurnBudget(30, 10)
        permit = budget.reserve("plan", 100, 20, 2)
        self.assertEqual((permit.input_cap, permit.output_cap), (10, 20))
        self.assertEqual(budget.snapshot()["remaining_tokens"], 0)
        with self.assertRaisesRegex(GovernedError, "prune_turn_token_limit"):
            budget.reserve("parallel", 100, 20, 2)

    def test_concurrent_reservations_never_overbook_remaining_pool(self):
        budget = PruneTurnBudget(200, 10)
        ready = threading.Barrier(3)
        def reserve():
            ready.wait()
            try:
                return budget.reserve("parallel", 80, 20, 2)
            except GovernedError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda _: reserve(), range(3)))
        self.assertEqual(sum(type(v) is str for v in values), 1)
        self.assertEqual(budget.snapshot()["pending_tokens"], 200)
        self.assertEqual(budget.snapshot()["closed"], "prune_turn_token_limit")

    def test_thinking_is_validated_and_is_already_in_output_counter(self):
        budget = PruneTurnBudget(100, 10)
        permit = budget.reserve("plan", 60, 20, 2)
        record = budget.settle(permit, 10, 8, native_usage={"input_tokens": 6,
            "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1, "output_tokens": 8,
            "output_tokens_details": {"thinking_tokens": 5}})
        self.assertEqual(record["thinking_tokens"], 5)
        self.assertEqual(budget.snapshot()["spent_tokens"], 18)

    def test_counter_breach_is_accounted_before_stop(self):
        budget = PruneTurnBudget(30, 10)
        permit = budget.reserve("plan", 10, 20, 2)
        with self.assertRaisesRegex(GovernedError, "provider_token_limit_breach"):
            budget.settle(permit, 20, 20)
        snapshot = budget.snapshot()
        self.assertEqual((snapshot["spent_tokens"], snapshot["pending_tokens"]), (40, 0))
        self.assertEqual(snapshot["closed"], "provider_token_limit_breach")

    def test_invalid_native_thinking_preserves_hold_and_closes(self):
        budget = PruneTurnBudget(100, 10)
        permit = budget.reserve("plan", 60, 20, 2)
        with self.assertRaisesRegex(GovernedError, "prune_turn_invalid_usage"):
            budget.fail(permit, known_usage={"input_tokens": 10, "output_tokens": 8},
                native_usage={"input_tokens": 10, "output_tokens": 8,
                              "output_tokens_details": {"thinking_tokens": 9}},
                code="invalid_usage")
        self.assertEqual(budget.snapshot()["pending_tokens"], 80)
        self.assertEqual(budget.snapshot()["closed"], "invalid_usage")

    def test_unknown_generation_retains_hold_and_known_failure_charges_once(self):
        unknown = PruneTurnBudget(100, 10)
        permit = unknown.reserve("plan", 60, 20, 2)
        unknown.fail(permit, generation_intent=True, code="missing_usage")
        self.assertEqual((unknown.snapshot()["spent_tokens"], unknown.snapshot()["pending_tokens"]), (0, 80))
        known = PruneTurnBudget(100, 10)
        permit = known.reserve("plan", 60, 20, 2)
        known.settle(permit, 10, 8)
        known.fail(permit, known_usage={"input_tokens": 10, "output_tokens": 8}, code="invalid_output")
        self.assertEqual((known.snapshot()["spent_tokens"], known.snapshot()["pending_tokens"]), (18, 0))
        self.assertEqual(len(known.snapshot()["records"]), 1)

    def test_pre_generation_failure_releases_tokens_but_closes_turn(self):
        budget = PruneTurnBudget(100, 10)
        permit = budget.reserve("plan", 60, 20, 2)
        budget.fail(permit, code="input_token_limit")
        self.assertEqual((budget.snapshot()["spent_tokens"], budget.snapshot()["pending_tokens"]), (0, 0))
        self.assertEqual(budget.snapshot()["closed"], "input_token_limit")

    def test_foreign_or_replaced_permit_cannot_settle(self):
        first, second = PruneTurnBudget(100, 10), PruneTurnBudget(100, 10)
        permit = first.reserve("plan", 60, 20, 2)
        for ledger, candidate in ((second, permit), (first, replace(permit))):
            with self.assertRaisesRegex(GovernedError, "prune_turn_invalid_permit"):
                ledger.settle(candidate, 10, 8)

    def test_call_limit_has_no_new_reservation(self):
        budget = PruneTurnBudget(100, 10, max_calls=1)
        budget.settle(budget.reserve("plan", 60, 20, 2), 10, 8)
        with self.assertRaisesRegex(GovernedError, "prune_turn_call_limit"):
            budget.reserve("prune", 60, 20, 2)
        self.assertEqual((budget.snapshot()["calls"], budget.snapshot()["pending_tokens"]), (1, 0))

    def test_whole_deadline_is_never_renewed_by_next_call(self):
        with patch("indeces.prune_turn_budget.time.monotonic", return_value=100):
            budget = PruneTurnBudget(100, 10)
            permit = budget.reserve("plan", 60, 20, 30)
            self.assertEqual(permit.deadline, 110)
        with patch("indeces.prune_turn_budget.time.monotonic", return_value=110):
            with self.assertRaisesRegex(GovernedError, "prune_turn_time_limit"):
                budget.check_call(permit)
        self.assertEqual(budget.snapshot()["pending_tokens"], 80)


class NativeTurnFuseTests(SyntheticGraphFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-turn-offline-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.make_graph()
        self.budget = PruneBudget(self.directory / "offline.sqlite3", "offline_fixture:turn", price=PRICE)
        self.addCleanup(self.budget.close)
        self.scratch = Scratch()
        self.adapter = create_model_adapter(config(), self.scratch, "synthetic-not-a-key")
        self.addAsyncCleanup(self.adapter.close)

    async def plan(self, tracker, *, seconds=2, output_cap=20, input_cap=60):
        return await select_native_plan(self.adapter, self.budget, "Synthetic plan", "Synthetic question",
            {"type": "object", "properties": {"ok": {"type": "boolean"}},
             "required": ["ok"], "additionalProperties": False},
            "synthetic-plan", input_cap=input_cap, output_cap=output_cap, seconds=seconds,
            live=False, turn_budget=tracker)

    async def test_plan_and_loop_share_pool_but_each_output_cap_is_independent(self):
        messages = [native_message([{"type": "text", "text": '{"ok":true}'}], reason="end_turn"),
                    native_message([tool("answer", "toolu_answer", choice="none")]),
                    native_message([{"type": "text", "text": "Synthetic final"}], reason="end_turn")]
        transport = ScriptedTransport(messages)
        self.adapter._request_override = transport
        tracker = PruneTurnBudget(100, 5)
        await self.plan(tracker)
        loop = PruneToolLoop(self.adapter, self.session, self.budget)
        result = await loop.run("Synthetic system", "Synthetic question", "synthetic-loop",
            input_cap=60, output_cap=20, seconds=2, turn_budget=tracker)
        self.assertEqual(result.text, "Synthetic final")
        self.assertEqual([body["max_tokens"] for path, body in transport.requests if path == "/messages"], [20, 20, 20])
        self.assertEqual((tracker.snapshot()["spent_tokens"], tracker.snapshot()["pending_tokens"]), (54, 0))
        self.assertEqual(tracker.snapshot()["calls"], 3)

    async def test_plan_semantic_failure_is_known_and_settles_before_rejection(self):
        self.adapter._request_override = ScriptedTransport([
            native_message([{"type": "text", "text": '{"wrong":true}'}], reason="end_turn")])
        tracker = PruneTurnBudget(100, 5)
        with self.assertRaisesRegex(GovernedError, "invalid_structured_output"):
            await self.plan(tracker)
        self.assertEqual((tracker.snapshot()["spent_tokens"], tracker.snapshot()["pending_tokens"]), (18, 0))
        self.assertEqual(tracker.snapshot()["closed"], "invalid_structured_output")
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)

    async def test_count_denial_never_generates_when_remaining_input_shrinks(self):
        transport = ScriptedTransport([])
        transport.responses.append({"input_tokens": 10})
        self.adapter._request_override = transport
        tracker = PruneTurnBudget(25, 5)
        with self.assertRaisesRegex(GovernedError, "prune_turn_token_limit"):
            await self.plan(tracker)
        self.assertEqual([path for path, _ in transport.requests], ["/messages/count_tokens"])
        self.assertEqual(tracker.snapshot()["spent_tokens"], 0)
        self.assertEqual(tracker.snapshot()["pending_tokens"], 0)
        # Financial count-only failures keep the original conservative hold.
        self.assertGreater(self.budget.snapshot()["pending_nusd"], 0)

    async def test_cancel_unknown_keeps_primary_and_both_holds_without_retry(self):
        entered, wait = asyncio.Event(), asyncio.Event()
        paths = []
        async def transport(path, payload):
            paths.append(path)
            if path == "/messages/count_tokens":
                return {"input_tokens": 10}
            entered.set()
            await wait.wait()
        self.adapter._request_override = transport
        tracker = PruneTurnBudget(100, 5)
        task = asyncio.create_task(self.plan(tracker))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        self.assertEqual(paths, ["/messages/count_tokens", "/messages"])
        self.assertEqual(tracker.snapshot()["pending_tokens"], 80)
        self.assertGreater(self.budget.snapshot()["pending_nusd"], 0)

    async def test_known_usage_sink_failure_is_settled_without_masking_primary(self):
        class SinkFault(Scratch):
            def write(self, event, **fields):
                super().write(event, **fields)
                if event == "http_response" and fields.get("path") == "/messages":
                    raise RuntimeError("synthetic confirmed sink fault")
        self.adapter.scratch = SinkFault()
        self.adapter._request_override = ScriptedTransport([
            native_message([{"type": "text", "text": '{"ok":true}'}], reason="end_turn")])
        tracker = PruneTurnBudget(100, 5)
        with self.assertRaisesRegex(RuntimeError, "synthetic confirmed sink fault"):
            await self.plan(tracker)
        self.assertEqual((tracker.snapshot()["spent_tokens"], tracker.snapshot()["pending_tokens"]), (18, 0))
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)

    async def test_confirmed_usage_cancellation_object_survives_terminal_fault(self):
        primary = asyncio.CancelledError("synthetic primary cancellation")
        class CancelSink(Scratch):
            def write(self, event, **fields):
                if event == "http_response" and fields.get("path") == "/messages":
                    raise primary
                if event == "call_end":
                    raise RuntimeError("synthetic secondary terminal sink")
                super().write(event, **fields)
        self.adapter.scratch = CancelSink()
        transport = ScriptedTransport([
            native_message([{"type": "text", "text": '{"ok":true}'}], reason="end_turn")])
        self.adapter._request_override = transport
        tracker = PruneTurnBudget(100, 5)
        with self.assertRaises(asyncio.CancelledError) as raised:
            await self.plan(tracker)
        self.assertIs(raised.exception, primary)
        self.assertEqual((tracker.snapshot()["spent_tokens"], tracker.snapshot()["pending_tokens"]), (18, 0))
        self.assertEqual([path for path, _ in transport.requests], ["/messages/count_tokens", "/messages"])
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)

    async def test_loop_per_call_timeout_uses_shared_turn_stop_and_preserves_unknown(self):
        paths = []
        async def transport(path, payload):
            paths.append(path)
            if path == "/messages/count_tokens":
                return {"input_tokens": 10}
            await asyncio.Event().wait()
        self.adapter._request_override = transport
        tracker = PruneTurnBudget(100, 5)
        loop = PruneToolLoop(self.adapter, self.session, self.budget)
        with self.assertRaisesRegex(GovernedError, "prune_turn_time_limit"):
            await loop.run("Synthetic system", "Synthetic question", "synthetic-loop",
                input_cap=60, output_cap=20, seconds=1, turn_budget=tracker)
        self.assertEqual(paths, ["/messages/count_tokens", "/messages"])
        self.assertEqual(tracker.snapshot()["pending_tokens"], 80)
        self.assertEqual(tracker.snapshot()["closed"], "prune_turn_time_limit")
