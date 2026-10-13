"""Offline transport regressions for failed bot turns and fixed notices."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from indeces.adapter import OpenAIAdapter
from indeces.contracts import DeliveryReceipt, FailureNotice, GovernedError, IncomingMessage
from indeces.discord_bridge import DiscordBridge
from indeces.run_records import verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.user_notices import NOTICE_POLICY, user_notice
from tests.test_bot_bridge import Message
from tests.test_run_records import RecordFixture


class ErrorMessage(Message):
    def __init__(self, identity, *, error=None, **kwargs):
        super().__init__(identity, **kwargs)
        self.error = error

    async def reply(self, text, **options):
        self.sent.append((text, options))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=1000 + self.id)


class BotFailureNoticeTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.seed()
        self.resources = []
        self.log_number = 0

    async def asyncTearDown(self):
        for bridge, adapter, log in reversed(self.resources):
            await bridge.close()
            await adapter.close()
            log.close()

    def tearDown(self):
        self.teardown_fixture()

    def make_bridge(self, *, action="reply", answer="A synthetic answer [M1].", model_error=None,
                    counted_input=10):
        self.log_number += 1
        log = ScratchLog(self.root / "notices" / str(self.log_number))
        requests = []

        async def request(path, payload):
            requests.append((path, deepcopy(payload)))
            if path == "/responses/input_tokens":
                return {"input_tokens": counted_input}
            if model_error is not None:
                raise model_error
            output = json.dumps({"action": action, "text": "" if action == "skip" else answer})
            return {"id": "synthetic-notice-model", "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "output": [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": output}]}]}

        adapter = OpenAIAdapter(self.config.adapter, log, request=request)
        runtime = Runtime(self.config, self.store, adapter, log)
        bridge = DiscordBridge(self.config, runtime, log)
        bridge._discord = discord
        bridge._allowed_mentions = discord.AllowedMentions.none()
        bridge._start_worker()
        self.resources.append((bridge, adapter, log))
        return bridge, runtime, requests, log

    async def settle(self, bridge):
        await asyncio.wait_for(bridge._queue.join(), timeout=2)

    def entries(self, log):
        verify(log.path)
        return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]

    def event(self, entries, event):
        return next(entry["fields"] for entry in entries if entry["event"] == event)

    def assert_notice(self, source, code):
        self.assertEqual(len(source.sent), 1)
        text, options = source.sent[0]
        self.assertEqual(text, user_notice(code))
        self.assertNotIn(code, text)
        self.assertNotIn("审计编号", text)
        self.assertNotIn("trace", text)
        self.assertNotIn("unknown", text)
        self.assertNotIn("<@", text)
        self.assertNotIn("@everyone", text)
        self.assertFalse(options["mention_author"])
        allowed = options["allowed_mentions"].to_dict()
        self.assertEqual(allowed.get("parse"), [])
        self.assertFalse(options["allowed_mentions"].users)
        self.assertFalse(options["allowed_mentions"].roles)
        self.assertFalse(options["allowed_mentions"].everyone)
        self.assertFalse(options["allowed_mentions"].replied_user)
        return text

    async def test_local_memory_failure_sends_fixed_notice_without_model_or_assistant_history(self):
        bridge, runtime, requests, log = self.make_bridge()
        source = ErrorMessage(1, text="<@99> alpha topic")
        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(bridge.enqueue(source, "99"))
            await self.settle(bridge)
        notice = self.assert_notice(source, "local_memory_timeout")
        self.assertEqual(requests, [])
        turn = self.store.db.execute("SELECT status,reply,delivered FROM turns WHERE message_id='1'").fetchone()
        self.assertEqual(tuple(turn), ("local_memory_timeout", None, None))
        self.assertEqual([row["role"] for row in self.store.history("10:20")], ["user"])
        entries = self.entries(log)
        delivered = self.event(entries, "failure_notice_delivered")
        self.assertEqual(delivered["notice_policy"], NOTICE_POLICY)
        self.assertEqual(delivered["reason"], "local_memory_timeout")
        self.assertEqual(delivered["receipt"], {"ids": ["1001"], "text": notice})
        self.assertEqual(self.event(entries, "turn_end")["status"], "failed")
        started = self.event(entries, "discord_delivery_started")
        self.assertEqual(started["delivery_kind"], "failure_notice")
        self.assertEqual(started["notice_text"], notice)
        self.assertEqual(started["sent_text"], notice)
        self.assertIsNone(started["mentioned_peer_id"])
        self.assertNotIn("model_output", started)
        self.assertEqual(self.event(entries, "discord_delivery_finished")["delivery_kind"], "failure_notice")
        self.assertFalse({entry["event"] for entry in entries} & {"answer_generated", "answer_delivered", "delivery_start"})
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["counts"]["complete"], 0)
        self.assertEqual(report["counts"]["skipped"], 0)
        self.assertEqual(report["issues"], [])

    async def test_model_failure_notice_adds_no_retry_or_extra_model_call(self):
        bridge, _, requests, log = self.make_bridge(model_error=GovernedError("stage_timeout"))
        source = ErrorMessage(1, text="<@99> alpha topic")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(bridge.enqueue(source, "99"))
            await self.settle(bridge)
        self.assert_notice(source, "stage_timeout")
        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["issues"], [])
        entries = self.entries(log)
        self.assertEqual(len([entry for entry in entries if entry["event"] == "call_start"]), 1)
        self.assertEqual(len([entry for entry in entries if entry["event"] == "failure_notice_delivered"]), 1)

    async def test_true_model_skip_remains_silent_and_has_no_failure_notice(self):
        bridge, _, requests, log = self.make_bridge(action="skip")
        source = ErrorMessage(1, text="<@99> thanks, conversation complete")
        self.assertTrue(bridge.enqueue(source, "99"))
        await self.settle(bridge)
        self.assertEqual(source.sent, [])
        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
        self.assertFalse(any(entry["event"].startswith("failure_notice_") for entry in self.entries(log)))
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["skipped"], 1, report)
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertEqual(report["issues"], [])

    async def test_five_failures_keep_slots_and_sixth_is_silent_across_bridge_restart(self):
        bridge, runtime, requests, log = self.make_bridge()
        sources = [ErrorMessage(identity, text="<@99> alpha topic") for identity in range(1, 7)]
        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                redirect_stdout(io.StringIO()):
            for source in sources[:5]:
                self.assertTrue(bridge.enqueue(source, "99"))
                await self.settle(bridge)
            self.assertFalse(bridge.enqueue(sources[5], "99"))
        self.assertEqual(requests, [])
        for source in sources[:5]:
            self.assert_notice(source, "local_memory_timeout")
        self.assertEqual(sources[5].sent, [])
        self.assertEqual(self.store.db.execute("SELECT rounds FROM bot_conversation_peers").fetchone()[0], 5)
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["failed"], 5, report)
        self.assertEqual(report["issues"], [])
        await bridge.close()
        restarted, _, restart_requests, _ = self.make_bridge()
        self.assertFalse(restarted.enqueue(sources[0], "99"))
        self.assertFalse(restarted.enqueue(ErrorMessage(7), "99"))
        self.assertEqual(restart_requests, [])
        self.assertTrue(all(len(source.sent) == 1 for source in sources[:5]))

    async def test_new_human_input_resets_failed_bot_slots_without_mentioning_error_receipts(self):
        bridge, runtime, requests, _ = self.make_bridge()
        human = ErrorMessage(10, bot=False, text="<@99> alpha topic")
        renewed = ErrorMessage(11, text="<@99> alpha topic")
        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                redirect_stdout(io.StringIO()):
            for identity in range(1, 6):
                self.assertTrue(bridge.enqueue(ErrorMessage(identity), "99"))
                await self.settle(bridge)
            self.assertFalse(bridge.enqueue(ErrorMessage(6), "99"))
            self.assertTrue(bridge.enqueue(human, "99"))
            self.assertTrue(bridge.enqueue(renewed, "99"))
            await self.settle(bridge)
        self.assert_notice(human, "local_memory_timeout")
        self.assert_notice(renewed, "local_memory_timeout")
        self.assertEqual(requests, [])
        self.assertEqual(self.store.db.execute("SELECT rounds FROM bot_conversation_peers").fetchone()[0], 1)

    async def test_exhausted_turn_deadline_cannot_add_notice_time_or_send(self):
        bridge, runtime, requests, log = self.make_bridge()
        source = ErrorMessage(1)
        elapsed = [0.0]
        clock = SimpleNamespace(monotonic=lambda: elapsed[0], time=time.time)

        def exhausted(*args, **kwargs):
            elapsed[0] = self.config.runtime.turn_seconds + 1.0
            raise GovernedError("local_memory_timeout")

        with patch("indeces.runtime.time", clock), patch.object(runtime.graph, "retrieve", side_effect=exhausted), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(bridge.enqueue(source, "99"))
            await self.settle(bridge)
        self.assertEqual(source.sent, [])
        self.assertEqual(requests, [])
        entries = self.entries(log)
        self.assertEqual(self.event(entries, "failure_notice_skipped")["reason"], "turn_time_exhausted")
        self.assertFalse(any(entry["event"] == "failure_notice_delivered" for entry in entries))
        self.assertEqual(self.store.db.execute("SELECT rounds FROM bot_conversation_peers").fetchone()[0], 1)

    async def test_uncertain_notice_delivery_is_not_resent_and_slot_remains_consumed(self):
        bridge, runtime, requests, log = self.make_bridge()
        source = ErrorMessage(1, error=TimeoutError(), text="<@99> alpha topic")
        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(bridge.enqueue(source, "99"))
            await self.settle(bridge)
        self.assert_notice(source, "local_memory_timeout")
        self.assertEqual(requests, [])
        entries = self.entries(log)
        self.assertEqual(self.event(entries, "failure_notice_unknown")["error_type"], "TimeoutError")
        self.assertEqual(self.event(entries, "discord_delivery_failed")["delivery_kind"], "failure_notice")
        self.assertFalse(any(entry["event"] == "failure_notice_delivered" for entry in entries))
        self.assertFalse(bridge.enqueue(source, "99"))
        self.assertEqual(len(source.sent), 1)
        self.assertEqual(self.store.db.execute("SELECT rounds FROM bot_conversation_peers").fetchone()[0], 1)
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["issues"], [])

    async def test_uncertain_normal_answer_delivery_does_not_add_error_notice(self):
        bridge, _, requests, log = self.make_bridge()
        source = ErrorMessage(1, error=TimeoutError(), text="<@99> alpha topic")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(bridge.enqueue(source, "99"))
            await self.settle(bridge)
        self.assertEqual(len(source.sent), 1)
        self.assertTrue(source.sent[0][0].startswith("<@30> "))
        self.assertIn("A synthetic answer", source.sent[0][0])
        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
        entries = self.entries(log)
        self.assertFalse(any(entry["event"].startswith("failure_notice_") for entry in entries))
        self.assertTrue(self.event(entries, "turn_end")["code"].startswith("delivery_unknown:"))
        started = self.event(entries, "discord_delivery_started")
        self.assertEqual(started["model_output"], "A synthetic answer [M1].")
        self.assertNotIn("delivery_kind", started)
        self.assertNotIn("notice_text", started)
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["issues"], [])

    async def test_confirmed_notice_survives_transport_audit_failure_without_resend(self):
        bridge, runtime, requests, log = self.make_bridge()
        source = ErrorMessage(1, text="<@99> alpha topic")
        original_write = log.write

        def audit(event, **fields):
            if event == "discord_delivery_finished":
                raise OSError("synthetic confirmed-notice audit failure")
            original_write(event, **fields)

        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                patch.object(log, "write", side_effect=audit), redirect_stdout(io.StringIO()), \
                self.assertLogs("indeces.discord_bridge", level="ERROR"):
            self.assertTrue(bridge.enqueue(source, "99"))
            await asyncio.wait_for(bridge._worker_failed.wait(), timeout=2)
        await self.settle(bridge)
        notice = self.assert_notice(source, "local_memory_timeout")
        self.assertEqual(requests, [])
        self.assertFalse(bridge._accepting)
        entries = self.entries(log)
        self.assertEqual(self.event(entries, "failure_notice_delivered")["receipt"],
                         {"ids": ["1001"], "text": notice})
        self.assertEqual(self.store.db.execute("SELECT rounds FROM bot_conversation_peers").fetchone()[0], 1)
        await bridge.close()
        restarted, _, _, _ = self.make_bridge()
        self.assertFalse(restarted.enqueue(source, "99"))
        self.assertEqual(len(source.sent), 1)

    async def test_runtime_uses_typed_failure_notice_and_callback_preserves_text_receipt(self):
        _, runtime, requests, log = self.make_bridge()
        normal, notices = [], []

        async def deliver(payload):
            if isinstance(payload, FailureNotice):
                notices.append(payload.text)
                return DeliveryReceipt(("synthetic-notice",), payload.text)
            normal.append(payload)
            return DeliveryReceipt(("synthetic-normal",), payload)

        message = IncomingMessage("separate-notice", "20", "10", "30", "Synthetic Peer",
                                  "alpha topic", "2026-09-30T12:00:00+00:00", author_is_bot=True)
        with patch.object(runtime.graph, "retrieve", side_effect=GovernedError("local_memory_timeout")), \
                redirect_stdout(io.StringIO()):
            await runtime.process(message, deliver)
        self.assertEqual(normal, [])
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0], user_notice("local_memory_timeout"))
        self.assertNotIn("local_memory_timeout", notices[0])
        self.assertNotIn("<@", notices[0])
        self.assertEqual(requests, [])
        entries = self.entries(log)
        self.assertEqual(self.event(entries, "failure_notice_delivered")["receipt"],
                         {"ids": ["synthetic-notice"], "text": notices[0]})

    async def test_capacity_timeout_and_service_notices_keep_internal_codes_and_call_counts(self):
        cases = (
            ("input_token_limit", None, self.config.adapter.budgets["reply"].input_tokens + 1,
             ["/responses/input_tokens"]),
            ("stage_timeout", GovernedError("stage_timeout"), 10,
             ["/responses/input_tokens", "/responses"]),
            ("provider_http_503", GovernedError("provider_http_503"), 10,
             ["/responses/input_tokens", "/responses"]),
        )
        texts = []
        for identity, (code, error, count, expected_paths) in enumerate(cases, 1):
            with self.subTest(code=code):
                bridge, _, requests, log = self.make_bridge(model_error=error, counted_input=count)
                source = ErrorMessage(identity, text="<@99> alpha topic")
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(bridge.enqueue(source, "99"))
                    await self.settle(bridge)
                texts.append(self.assert_notice(source, code))
                self.assertEqual([path for path, _ in requests], expected_paths)
                entries = self.entries(log)
                start = self.event(entries, "turn_start")
                self.assertEqual(start["notice_policy"], NOTICE_POLICY)
                self.assertNotIn(start["trace_id"], texts[-1])
                self.assertNotIn(start["trace_id"][:12], texts[-1])
                self.assertEqual(self.event(entries, "turn_end")["code"], code)
                notice = self.event(entries, "failure_notice_delivered")
                self.assertEqual(notice["reason"], code)
                self.assertEqual(notice["notice_policy"], NOTICE_POLICY)
                self.assertEqual(len([e for e in entries if e["event"] == "call_start"]), 1)
                self.assertFalse(any(e["event"] in {"answer_generated", "answer_delivered"}
                                     for e in entries))
                report = verify_runs([log.path])
                self.assertEqual(report["counts"]["failed"], 1, report)
                self.assertEqual(report["issues"], [])
        self.assertEqual(len(set(texts)), len(cases))


if __name__ == "__main__":
    unittest.main()
