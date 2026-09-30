from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.discord_bridge import DiscordBridge, _Envelope, discord_reply_text, select_messages


def configuration(*, capacity: int = 2, seconds: float = 0.05, channels: tuple[str, ...] = (), queue_seconds: float = 120.0) -> SimpleNamespace:
    return SimpleNamespace(
        discord=SimpleNamespace(
            guild_id="10", channel_ids=channels, queue_capacity=capacity, delivery_seconds=seconds,
        ),
        runtime=SimpleNamespace(queue_wait_seconds=queue_seconds),
    )


class Scratch:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class Message:
    def __init__(self, identity: int = 1, text: str = "<@99> hello") -> None:
        self.id = identity
        self.content = text
        self.guild = SimpleNamespace(id=10)
        self.channel = SimpleNamespace(id=20)
        self.author = SimpleNamespace(id=30, display_name="Alice", bot=False)
        self.webhook_id = None
        self.created_at = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self.sent: list[tuple[str, dict]] = []
        self.delay = 0.0
        self.error: Exception | None = None

    async def reply(self, text: str, **options):
        self.sent.append((text, options))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return SimpleNamespace(id=1000 + self.id)


class HTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP status {status}")
        self.status = status
        self.code = 1234


class Runtime:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.started = asyncio.Event()
        self.messages = []
        self.receipts = []
        self.active = 0
        self.max_active = 0

    async def process(self, message, deliver) -> None:
        self.messages.append(message)
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            self.receipts.append(await deliver("reply to " + message.message_id))
        finally:
            self.active -= 1


class SelectionTests(unittest.TestCase):
    def test_explicit_user_mention_normalizes_and_preserves_source(self):
        message = Message(text="<@99> hello <@40> <@!99>")
        selected = select_messages(message, "99", configuration().discord)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.text, "hello <@40>")
        self.assertEqual(selected.raw_text, message.content)
        self.assertEqual(selected.message_id, "1")
        self.assertEqual(selected.guild_id, "10")
        self.assertEqual(selected.channel_id, "20")
        self.assertEqual(selected.author_name, "Alice")
        self.assertEqual(selected.created_at, "2026-09-30T12:00:00+00:00")

    def test_no_implicit_reply_or_everyone_trigger(self):
        for text in ("hello", "@everyone hello", "<@999> hello", "<@&99> hello"):
            with self.subTest(text=text):
                message = Message(text=text)
                message.mentions = [SimpleNamespace(id=99)]
                message.reference = SimpleNamespace(message_id=99)
                self.assertIsNone(select_messages(message, "99", configuration().discord))

    def test_empty_mention_is_ignored(self):
        self.assertIsNone(select_messages(Message(text="  <@99> <@!99> "), "99", configuration().discord))

    def test_human_guild_and_channel_filters(self):
        for field, value in (("guild", None), ("guild", SimpleNamespace(id=11)), ("webhook_id", 123)):
            with self.subTest(field=field, value=value):
                message = Message()
                setattr(message, field, value)
                self.assertIsNone(select_messages(message, "99", configuration().discord))
        message = Message()
        message.author.bot = True
        self.assertIsNone(select_messages(message, "99", configuration().discord))
        self.assertIsNone(select_messages(Message(), "99", configuration(channels=("21",)).discord))
        self.assertIsNotNone(select_messages(Message(), "99", configuration(channels=("20",)).discord))

    def test_reply_cap_counts_supplementary_unicode_without_breaking_characters(self):
        text = "😀" * 1500
        sent = discord_reply_text(text)
        self.assertLessEqual(len(sent.encode("utf-16-le")) // 2, 2000)
        self.assertIn("回复已截断", sent)
        self.assertEqual(discord_reply_text("😀" * 1000), "😀" * 1000)
        self.assertEqual(discord_reply_text("x" * 2000), "x" * 2000)

    def test_empty_delivery_is_rejected(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    discord_reply_text(text)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self, config=None, runtime=None):
        scratch = Scratch()
        bridge = DiscordBridge(config or configuration(), runtime or Runtime(), scratch)
        bridge._discord = SimpleNamespace(HTTPException=HTTPError)
        bridge._allowed_mentions = object()
        return bridge, scratch

    async def test_worker_serializes_runtime_and_disables_output_mentions(self):
        gate = asyncio.Event()
        runtime = Runtime(gate)
        bridge, scratch = self.make_bridge(runtime=runtime)
        bridge._start_worker()
        first, second = Message(1), Message(2)
        try:
            self.assertTrue(bridge.enqueue(first, "99"))
            await asyncio.wait_for(runtime.started.wait(), timeout=1)
            self.assertTrue(bridge.enqueue(second, "99"))
            self.assertEqual([m.message_id for m in runtime.messages], ["1"])
            gate.set()
            await asyncio.wait_for(bridge._queue.join(), timeout=1)
            self.assertEqual(runtime.max_active, 1)
            self.assertEqual([m.message_id for m in runtime.messages], ["1", "2"])
            self.assertEqual(runtime.receipts[0].message_ids, ("1001",))
            self.assertEqual(runtime.receipts[0].text, first.sent[0][0])
            self.assertFalse(first.sent[0][1]["mention_author"])
            self.assertIs(first.sent[0][1]["allowed_mentions"], bridge._allowed_mentions)
            self.assertTrue(any(event == "discord_delivery_finished" for event, _ in scratch.events))
        finally:
            await bridge.close()

    async def test_overflow_is_audited_without_runtime_or_reply(self):
        gate = asyncio.Event()
        runtime = Runtime(gate)
        bridge, scratch = self.make_bridge(configuration(capacity=1), runtime)
        bridge._start_worker()
        rejected = Message(3)
        try:
            bridge.enqueue(Message(1), "99")
            await asyncio.wait_for(runtime.started.wait(), timeout=1)
            self.assertTrue(bridge.enqueue(Message(2), "99"))
            with self.assertLogs("indeces.discord_bridge", level="WARNING"):
                self.assertFalse(bridge.enqueue(rejected, "99"))
            self.assertEqual(rejected.sent, [])
            self.assertEqual(len(runtime.messages), 1)
            self.assertTrue(any(event == "discord_message_rejected" and fields["reason"] == "queue_full" for event, fields in scratch.events))
        finally:
            await bridge.close()

    async def test_expired_queue_input_is_rejected_before_runtime_and_worker_continues(self):
        bridge, scratch = self.make_bridge(configuration(queue_seconds=10))
        bridge._start_worker()
        expired, fresh = Message(1), Message(2)
        try:
            self.assertTrue(bridge.enqueue(expired, "99"))
            # Age only the envelope; avoid changing the event loop clock or
            # sleeping in a test of the independent queue wait deadline.
            queued = bridge._queue.get_nowait()
            bridge._queue.task_done()
            bridge._queue.put_nowait(replace(queued, queued_at=queued.queued_at - 11))
            self.assertTrue(bridge.enqueue(fresh, "99"))
            with self.assertLogs("indeces.discord_bridge", level="WARNING"):
                await asyncio.wait_for(bridge._queue.join(), timeout=1)
            self.assertEqual(expired.sent, [])
            self.assertEqual([message.message_id for message in bridge.runtime.messages], ["2"])
            self.assertEqual(len(fresh.sent), 1)
            rejected = [fields for event, fields in scratch.events if event == "discord_message_rejected"]
            self.assertEqual(rejected[0]["message_id"], "1")
            self.assertEqual(rejected[0]["reason"], "queue_wait_expired")
            self.assertGreaterEqual(rejected[0]["wait_seconds"], 10)
            self.assertEqual(rejected[0]["queue_wait_seconds"], 10)
            self.assertFalse(any(event == "discord_runtime_started" and fields["message_id"] == "1" for event, fields in scratch.events))
        finally:
            await bridge.close()

    async def test_intake_before_start_and_after_close_is_rejected(self):
        bridge, scratch = self.make_bridge()
        self.assertFalse(bridge.enqueue(Message(), "99"))
        bridge._start_worker()
        await bridge.close()
        self.assertFalse(bridge.enqueue(Message(), "99"))
        self.assertEqual(bridge._queue.qsize(), 0)
        self.assertEqual([fields["reason"] for event, fields in scratch.events if event == "discord_message_rejected"], ["not_running", "not_running"])

    async def test_shutdown_cancels_current_and_audits_pending_messages(self):
        runtime = Runtime(asyncio.Event())
        bridge, scratch = self.make_bridge(runtime=runtime)
        bridge._start_worker()
        first, second = Message(1), Message(2)
        bridge.enqueue(first, "99")
        await asyncio.wait_for(runtime.started.wait(), timeout=1)
        bridge.enqueue(second, "99")
        await bridge.close()
        self.assertIsNone(bridge._worker)
        self.assertEqual(runtime.active, 0)
        self.assertEqual(first.sent + second.sent, [])
        self.assertTrue(any(event == "discord_runtime_cancelled" and fields["message_id"] == "1" for event, fields in scratch.events))
        self.assertTrue(any(event == "discord_message_dropped" and fields["message_id"] == "2" for event, fields in scratch.events))
        await asyncio.wait_for(bridge._queue.join(), timeout=1)

    async def test_delivery_timeout_is_uncertain_and_never_resent(self):
        bridge, scratch = self.make_bridge(configuration(seconds=0.001))
        source = Message()
        source.delay = 0.1
        incoming = select_messages(source, "99", bridge.config)
        with self.assertRaises(TimeoutError):
            await bridge._deliver(_Envelope(incoming, source), "hello")
        self.assertEqual(len(source.sent), 1)
        failed = [fields for event, fields in scratch.events if event == "discord_delivery_failed"]
        self.assertEqual(failed[0]["status"], "delivery_unknown")
        self.assertFalse(failed[0]["retry"])

    async def test_http_failures_are_classified_and_worker_continues(self):
        bridge, scratch = self.make_bridge()
        for code, status in ((403, "rejected"), (404, "rejected"), (503, "delivery_unknown")):
            with self.subTest(code=code):
                source = Message(code)
                source.error = HTTPError(code)
                incoming = select_messages(source, "99", bridge.config)
                with self.assertRaises(HTTPError):
                    await bridge._deliver(_Envelope(incoming, source), "hello")
                failed = [fields for event, fields in scratch.events if event == "discord_delivery_failed"][-1]
                self.assertEqual(failed["status"], status)
                self.assertEqual(failed["http_status"], code)
                self.assertEqual(len(source.sent), 1)
        bridge._start_worker()
        try:
            broken, good = Message(10), Message(11)
            broken.error = HTTPError(403)
            bridge.enqueue(broken, "99")
            bridge.enqueue(good, "99")
            with self.assertLogs("indeces.discord_bridge", level="ERROR"):
                await asyncio.wait_for(bridge._queue.join(), timeout=1)
            self.assertEqual(len(good.sent), 1)
        finally:
            await bridge.close()

    async def test_full_and_actual_output_are_both_audited(self):
        bridge, scratch = self.make_bridge()
        source = Message()
        incoming = select_messages(source, "99", bridge.config)
        output = "😀" * 2000
        receipt = await bridge._deliver(_Envelope(incoming, source), output)
        started = [fields for event, fields in scratch.events if event == "discord_delivery_started"][0]
        self.assertEqual(started["model_output"], output)
        self.assertEqual(started["sent_text"], receipt.text)
        self.assertTrue(started["truncated"])
        self.assertLessEqual(len(receipt.text.encode("utf-16-le")) // 2, 2000)

    async def test_shutdown_wait_is_bounded_if_runtime_suppresses_cancellation(self):
        release = asyncio.Event()
        started = asyncio.Event()

        class StubbornRuntime:
            async def process(self, message, deliver):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()

        bridge, scratch = self.make_bridge(configuration(seconds=0.001), StubbornRuntime())
        bridge._start_worker()
        worker = bridge._worker
        bridge.enqueue(Message(), "99")
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            with self.assertLogs("indeces.discord_bridge", level="ERROR"):
                await asyncio.wait_for(bridge.close(), timeout=0.1)
            self.assertTrue(any(event == "discord_shutdown_incomplete" for event, _ in scratch.events))
            self.assertFalse(bridge._accepting)
        finally:
            release.set()
            await asyncio.sleep(0)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, timeout=1)

    async def test_run_has_one_lazy_client_and_one_worker_across_ready_events(self):
        bridge, scratch = self.make_bridge()
        clients = []
        workers = []
        source = Message()
        no_mentions = object()

        class EmptyFlags:
            @staticmethod
            def none():
                return SimpleNamespace()

        class AllowedMentions:
            @staticmethod
            def none():
                return no_mentions

        class FakeClient:
            def __init__(self, **options):
                self.options = options
                self.user = SimpleNamespace(id=99)
                self.closes = 0
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                await self.close()

            async def start(self, token):
                self.token = token
                await self.setup_hook()
                workers.append(bridge._worker)
                await self.on_ready()
                await self.on_ready()
                workers.append(bridge._worker)
                await self.on_message(source)
                await bridge._queue.join()

            async def close(self):
                self.closes += 1

        fake = SimpleNamespace(
            Client=FakeClient, Intents=EmptyFlags, AllowedMentions=AllowedMentions,
            MemberCacheFlags=EmptyFlags, HTTPException=HTTPError,
        )
        with patch.dict(sys.modules, {"discord": fake}):
            await bridge.run("test-token-never-log")
        self.assertEqual(len(clients), 1)
        self.assertIs(workers[0], workers[1])
        self.assertTrue(clients[0].options["intents"].guild_messages)
        self.assertTrue(clients[0].options["intents"].guilds)
        self.assertFalse(getattr(clients[0].options["intents"], "message_content", False))
        self.assertIs(clients[0].options["allowed_mentions"], no_mentions)
        self.assertEqual(len(source.sent), 1)
        self.assertEqual(clients[0].closes, 1)
        self.assertIsNone(bridge._worker)
        self.assertNotIn("test-token-never-log", repr(scratch.events))


if __name__ == "__main__":
    unittest.main()
