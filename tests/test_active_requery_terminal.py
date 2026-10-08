"""Terminal admission regressions using synthetic provider responses only."""
from copy import deepcopy
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.active_requery import TurnBudgetAdapter
from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, RuntimeConfig
from indeces.contracts import GovernedError
from indeces.scratch import ScratchLog


class TerminalAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.scratch = ScratchLog(self.root / "scratch")
        self.config = SimpleNamespace(state_dir=self.root / "state",
            runtime=RuntimeConfig(active_requery_enabled=True),
            adapter=AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1",
                {s: Budget(4096, 512, 15) for s in ("label", "summary", "reply")}))
        self.requests = []
        self.lose_usage = False
        self.base = OpenAIAdapter(self.config.adapter, self.scratch, request=self.transport)
        self.base.offline_mock = True
        self.adapter = TurnBudgetAdapter(self.base, self.config, self.scratch,
                                        "synthetic-terminal", "synthetic-message", "10:20")
        self.guards = [patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
                       patch("socket.socket.connect_ex", side_effect=AssertionError("network forbidden")),
                       patch("aiohttp.ClientSession", side_effect=AssertionError("HTTP forbidden"))]
        for guard in self.guards:
            guard.start()

    async def asyncTearDown(self):
        await self.base.close()
        self.scratch.close()
        for guard in reversed(self.guards):
            guard.stop()
        self.directory.cleanup()

    async def transport(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        if path == "/responses/input_tokens":
            return {"input_tokens": 64}
        if self.lose_usage:
            raise GovernedError("synthetic-response-lost", remote_usage_unknown=True)
        return {"id": "synthetic-completion", "status": "completed",
                "usage": {"input_tokens": 64, "output_tokens": 32},
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Synthetic terminal regression."}]}]}

    async def call(self):
        return await self.adapter.call("reply", "Synthetic instruction", [], "synthetic-terminal")

    async def assert_closed_unchanged(self):
        prior = self.adapter.path.read_bytes()
        calls = deepcopy(self.adapter.calls)
        requests = len(self.requests)
        for _ in range(2):
            with self.assertRaisesRegex(GovernedError, "active_requery_run_closed"):
                await self.call()
        with self.assertRaisesRegex(GovernedError, "active_requery_run_closed"):
            self.adapter.check()
        with self.assertRaisesRegex(GovernedError, "active_requery_run_closed"):
            self.adapter.reject("late-error")
        self.adapter.halt("late-error")
        self.adapter.deadline = 0
        self.adapter.finish()
        self.assertEqual(self.adapter.path.read_bytes(), prior)
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(len(self.requests), requests)
        events = [json.loads(line) for p in (self.root / "scratch").glob("*.jsonl")
                  for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(e["event"] == "requery_budget_event" and
                             e["fields"]["budget_event"] == "turn_end" for e in events), 1)

    async def test_completed_success_cannot_resume_or_change_known_usage(self):
        await self.call()
        self.adapter.finish()
        await self.assert_closed_unchanged()
        saved = json.loads(self.adapter.path.read_text())
        self.assertEqual(saved["phase"], "completed")
        self.assertIsNone(saved["halted"])
        self.assertEqual((saved["input_tokens"], saved["output_tokens"]), (64, 32))

    async def test_finished_without_calls_is_also_closed(self):
        self.adapter.finish()
        await self.assert_closed_unchanged()
        self.assertEqual(self.requests, [])

    async def test_stopped_run_preserves_original_reason(self):
        self.adapter.halt("synthetic-stop")
        self.adapter.finish()
        await self.assert_closed_unchanged()
        self.assertEqual(self.adapter.phase, "stopped")
        self.assertEqual(self.adapter.halted, "synthetic-stop")

    async def test_unknown_usage_keeps_reservation_without_refund_or_retry(self):
        self.lose_usage = True
        with self.assertRaises(GovernedError):
            await self.call()
        self.adapter.finish()
        await self.assert_closed_unchanged()
        saved = json.loads(self.adapter.path.read_text())
        self.assertEqual(saved["phase"], "halted")
        self.assertEqual(saved["halted"], "requery_usage_unknown")
        self.assertEqual(saved["unknown_generation_count"], 1)
        self.assertIsNone(saved["calls"][0]["usage"])
        self.assertEqual(saved["calls"][0]["reserved_output_tokens"], 512)
        self.assertEqual(saved["automatic_retries"], 0)
        with self.assertRaises(GovernedError) as caught:
            self.adapter.check()
        self.assertTrue(caught.exception.remote_usage_unknown)

    async def test_completed_sidecar_cannot_be_reopened_by_new_object(self):
        await self.call()
        self.adapter.finish()
        with self.assertRaisesRegex(GovernedError, "already_recorded"):
            TurnBudgetAdapter(self.base, self.config, self.scratch,
                              "another-trace", "synthetic-message", "10:20")
        self.assertEqual(len(self.requests), 2)

    async def test_inflight_call_cannot_be_finished_or_admit_a_second_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.transport
        async def paused(path, payload):
            if path == "/responses":
                entered.set()
                await release.wait()
            return await original(path, payload)
        self.base._request_override = paused
        task = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            prior = self.adapter.path.read_bytes()
            with self.assertRaisesRegex(GovernedError, "call_in_progress"):
                self.adapter.finish()
            with self.assertRaisesRegex(GovernedError, "call_in_progress"):
                await self.call()
            self.assertEqual(self.adapter.path.read_bytes(), prior)
            self.assertEqual(self.adapter.phase, "open")
        finally:
            release.set()
            await task
        self.adapter.finish()
        await self.assert_closed_unchanged()
        self.assertEqual(len(self.requests), 2)

    async def test_external_audit_cannot_reentrantly_finish_admitted_call(self):
        attempts = []
        def audit(event, **fields):
            if event == "call_start":
                with self.assertRaisesRegex(GovernedError, "call_in_progress"):
                    self.adapter.finish()
                attempts.append(event)
        await self.adapter.call("reply", "Synthetic instruction", [], "synthetic-terminal", audit=audit)
        self.assertEqual(attempts, ["call_start"])
        self.adapter.finish()
        await self.assert_closed_unchanged()
