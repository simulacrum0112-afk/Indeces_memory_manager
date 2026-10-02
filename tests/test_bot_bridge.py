from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

from indeces.discord_bridge import DiscordBridge, select_messages
from indeces.store import Store


def configuration(*, capacity: int = 20, queue_seconds: float = 120.0):
    return SimpleNamespace(
        discord=SimpleNamespace(
            guild_id="10", channel_ids=(), queue_capacity=capacity, delivery_seconds=1.0,
        ),
        runtime=SimpleNamespace(queue_wait_seconds=queue_seconds),
    )


class Scratch:
    def __init__(self):
        self.events = []
        self.fail_next_queued = False
        self.fail_next_delivery_finished = False

    def write(self, event, **fields):
        if event == "discord_message_queued" and self.fail_next_queued:
            self.fail_next_queued = False
            raise OSError("synthetic input audit failure")
        if event == "discord_delivery_finished" and self.fail_next_delivery_finished:
            self.fail_next_delivery_finished = False
            raise OSError("synthetic confirmed-delivery audit failure")
        self.events.append((event, fields))


class Message:
    def __init__(self, identity, *, bot=True, author=30, channel=20, text="<@99> hello"):
        self.id = identity
        self.content = text
        self.guild = SimpleNamespace(id=10)
        self.channel = SimpleNamespace(id=channel)
        self.author = SimpleNamespace(id=author, display_name="Peer" if bot else "Human", bot=bot)
        self.webhook_id = None
        self.created_at = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self.sent = []

    async def reply(self, text, **options):
        self.sent.append((text, options))
        return SimpleNamespace(id=1000 + self.id)


class Runtime:
    def __init__(self, store, *, outcome="reply", gate=None, output=None):
        self.store = store
        self.outcome = outcome
        self.gate = gate
        self.started = asyncio.Event()
        self.messages = []
        self.receipts = []
        self.output = output

    async def process(self, message, deliver):
        self.messages.append(message)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.outcome == "skip":
            return
        if self.outcome == "fail":
            raise ValueError("synthetic model failure")
        output = self.output if self.output is not None else "reply " + message.message_id + " <@40> @everyone"
        self.receipts.append(await deliver(output))


class BotSelectionTests(unittest.TestCase):
    def test_other_bot_requires_explicit_nonempty_mention_and_preserves_identity(self):
        selected = select_messages(Message(1), "99", configuration().discord)
        self.assertIsNotNone(selected)
        self.assertTrue(selected.author_is_bot)
        self.assertEqual(selected.author_id, "30")
        self.assertEqual(selected.text, "hello")
        self.assertEqual(selected.raw_text, "<@99> hello")
        human = select_messages(Message(2, bot=False), "99", configuration().discord)
        self.assertFalse(human.author_is_bot)
        for text in ("hello", "@everyone hello", "<@999> hello", "<@&99> hello", " <@99> "):
            with self.subTest(text=text):
                message = Message(3, text=text)
                message.mentions = [SimpleNamespace(id=99)]
                message.reference = SimpleNamespace(message_id=99)
                self.assertIsNone(select_messages(message, "99", configuration().discord))

    def test_self_and_webhooks_are_never_selected(self):
        for bot in (True, False):
            with self.subTest(bot=bot):
                self.assertIsNone(select_messages(Message(1, author=99, bot=bot), "99", configuration().discord))
                message = Message(2, bot=bot)
                message.webhook_id = 123
                self.assertIsNone(select_messages(message, "99", configuration().discord))


class BotBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.state = Path(self.directory.name) / "state"
        self.store = Store(self.state)
        self.bridges = []

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.close()
        self.store.close()
        self.directory.cleanup()

    def make_bridge(self, *, config=None, outcome="reply", gate=None, output=None):
        scratch = Scratch()
        runtime = Runtime(self.store, outcome=outcome, gate=gate, output=output)
        bridge = DiscordBridge(config or configuration(), runtime, scratch)
        bridge._discord = discord
        bridge._allowed_mentions = discord.AllowedMentions.none()
        bridge._start_worker()
        self.bridges.append(bridge)
        return bridge, runtime, scratch

    async def settle(self, bridge):
        await asyncio.wait_for(bridge._queue.join(), timeout=2)

    def rejected_reasons(self, scratch):
        return [fields["reason"] for event, fields in scratch.events if event == "discord_message_rejected"]

    async def test_five_admissions_and_mentions_only_to_peer_during_first_four_rounds(self):
        bridge, runtime, scratch = self.make_bridge()
        sources = [Message(i) for i in range(1, 7)]
        for source in sources[:5]:
            self.assertTrue(bridge.enqueue(source, "99"))
        self.assertFalse(bridge.enqueue(sources[5], "99"))
        await self.settle(bridge)
        self.assertEqual([message.message_id for message in runtime.messages], [str(i) for i in range(1, 6)])
        self.assertEqual(sources[5].sent, [])
        self.assertIn("round_limit", self.rejected_reasons(scratch))
        for source, receipt in zip(sources[:4], runtime.receipts[:4]):
            text, options = source.sent[0]
            self.assertTrue(text.startswith("<@30> "))
            self.assertEqual(receipt.text, text)
            self.assertFalse(options["mention_author"])
            allowed = options["allowed_mentions"].to_dict()
            self.assertEqual(allowed.get("users"), [30])
            self.assertEqual(allowed.get("parse"), [])
            self.assertFalse(allowed.get("replied_user", False))
        text, options = sources[4].sent[0]
        self.assertFalse(text.startswith("<@30> "))
        self.assertEqual(options["allowed_mentions"].to_dict().get("parse"), [])
        self.assertFalse(options["allowed_mentions"].users)
        self.assertFalse(options["mention_author"])

    async def test_burst_before_consumer_starts_cannot_reserve_more_than_five(self):
        bridge, runtime, _ = self.make_bridge(config=configuration(capacity=50))
        accepted = [bridge.enqueue(Message(i), "99") for i in range(1, 31)]
        self.assertEqual(accepted, [True] * 5 + [False] * 25)
        self.assertEqual(bridge._queue.qsize(), 5)
        self.assertEqual(runtime.messages, [])
        await self.settle(bridge)
        self.assertEqual(len(runtime.messages), 5)

    async def test_bot_model_mentions_are_removed_before_controlled_prefix_and_human_text_is_preserved(self):
        output = "thanks <@30> and <@!30>, <@40> @everyone"
        bridge, runtime, _ = self.make_bridge(output=output)
        sources = [Message(identity) for identity in range(1, 6)]
        for source in sources:
            self.assertTrue(bridge.enqueue(source, "99"))
        human = Message(10, bot=False)
        self.assertTrue(bridge.enqueue(human, "99"))
        await self.settle(bridge)
        for source in sources[:4]:
            text, options = source.sent[0]
            self.assertTrue(text.startswith("<@30> "))
            self.assertEqual(text.count("<@30>"), 1)
            self.assertNotIn("<@!30>", text)
            self.assertIn("<@40>", text)
            self.assertEqual(options["allowed_mentions"].to_dict().get("users"), [30])
        final_text, final_options = sources[4].sent[0]
        self.assertNotIn("<@30>", final_text)
        self.assertNotIn("<@!30>", final_text)
        self.assertFalse(final_options["allowed_mentions"].users)
        self.assertEqual(human.sent[0][0], output)
        self.assertFalse(human.sent[0][1]["allowed_mentions"].users)
        for source, receipt in zip(sources + [human], runtime.receipts):
            self.assertEqual(receipt.text, source.sent[0][0])

    async def test_peer_prefix_and_unicode_truncation_share_one_discord_limit_and_actual_receipt(self):
        output = "<@30> <@!30> " + "😀" * 1600
        bridge, runtime, scratch = self.make_bridge(output=output)
        source = Message(1)
        self.assertTrue(bridge.enqueue(source, "99"))
        await self.settle(bridge)
        sent_text = source.sent[0][0]
        self.assertTrue(sent_text.startswith("<@30> "))
        self.assertEqual(sent_text.count("<@30>"), 1)
        self.assertNotIn("<@!30>", sent_text)
        self.assertLessEqual(len(sent_text.encode("utf-16-le")) // 2, 2000)
        self.assertIn("回复已截断", sent_text)
        self.assertEqual(runtime.receipts[0].text, sent_text)
        self.assertEqual(runtime.receipts[0].message_ids, ("1001",))
        started = next(fields for event, fields in scratch.events if event == "discord_delivery_started")
        self.assertEqual(started["model_output"], output)
        self.assertEqual(started["sent_text"], sent_text)
        self.assertTrue(started["truncated"])

    async def test_new_explicit_human_resets_all_peer_counts_in_same_channel_at_admission(self):
        bridge, runtime, _ = self.make_bridge()
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        self.assertTrue(bridge.enqueue(Message(6, author=31), "99"))
        human = Message(10, bot=False)
        self.assertTrue(bridge.enqueue(human, "99"))
        self.assertTrue(bridge.enqueue(Message(11), "99"))
        self.assertTrue(bridge.enqueue(Message(12, author=31), "99"))
        await self.settle(bridge)
        self.assertEqual(len(runtime.messages), 9)
        self.assertFalse(human.sent[0][0].startswith("<@30> "))
        self.assertFalse(human.sent[0][1]["allowed_mentions"].users)

    async def test_other_channel_or_implicit_human_does_not_reset_and_peer_counts_are_independent(self):
        bridge, runtime, _ = self.make_bridge()
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        self.assertTrue(bridge.enqueue(Message(10, bot=False, channel=21), "99"))
        self.assertFalse(bridge.enqueue(Message(11, bot=False, text="thank you"), "99"))
        self.assertFalse(bridge.enqueue(Message(12), "99"))
        self.assertTrue(bridge.enqueue(Message(13, channel=21), "99"))
        for identity in range(20, 25):
            self.assertTrue(bridge.enqueue(Message(identity, author=31), "99"))
        self.assertFalse(bridge.enqueue(Message(25, author=31), "99"))
        await self.settle(bridge)
        self.assertEqual(len(runtime.messages), 12)

    async def test_duplicate_human_cannot_reset_and_duplicate_bot_does_not_take_new_round(self):
        bridge, runtime, scratch = self.make_bridge()
        human = Message(1, bot=False)
        self.assertTrue(bridge.enqueue(human, "99"))
        first = Message(2)
        self.assertTrue(bridge.enqueue(first, "99"))
        self.assertFalse(bridge.enqueue(first, "99"))
        for identity in range(3, 7):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        self.assertFalse(bridge.enqueue(human, "99"))
        self.assertFalse(bridge.enqueue(Message(7), "99"))
        await self.settle(bridge)
        self.assertEqual(len(runtime.messages), 6)
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(self.rejected_reasons(scratch)), 3)

    async def test_skip_consumes_attempt_and_does_not_deliver_any_message(self):
        bridge, runtime, _ = self.make_bridge(outcome="skip")
        sources = [Message(identity) for identity in range(1, 7)]
        for source in sources[:5]:
            self.assertTrue(bridge.enqueue(source, "99"))
        await self.settle(bridge)
        self.assertFalse(bridge.enqueue(sources[5], "99"))
        self.assertEqual(len(runtime.messages), 5)
        self.assertTrue(all(source.sent == [] for source in sources))

    async def test_failed_attempt_is_not_refunded(self):
        bridge, runtime, _ = self.make_bridge(outcome="fail")
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        with self.assertLogs("indeces.discord_bridge", level="ERROR"):
            await self.settle(bridge)
        self.assertFalse(bridge.enqueue(Message(6), "99"))
        self.assertEqual(len(runtime.messages), 5)

    async def test_expired_queue_message_is_not_refunded(self):
        bridge, runtime, scratch = self.make_bridge(config=configuration(queue_seconds=10))
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        envelope = bridge._queue.get_nowait()
        bridge._queue.task_done()
        bridge._queue.put_nowait(replace(envelope, queued_at=envelope.queued_at - 11))
        with self.assertLogs("indeces.discord_bridge", level="WARNING"):
            await self.settle(bridge)
        self.assertEqual(len(runtime.messages), 4)
        self.assertIn("queue_wait_expired", self.rejected_reasons(scratch))
        self.assertFalse(bridge.enqueue(Message(6), "99"))

    async def test_full_queue_does_not_consume_bot_round_or_reset_human(self):
        bridge, _, scratch = self.make_bridge(config=configuration(capacity=1))
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
            await self.settle(bridge)
        self.assertTrue(bridge.enqueue(Message(10, bot=False, channel=21), "99"))
        with self.assertLogs("indeces.discord_bridge", level="WARNING"):
            self.assertFalse(bridge.enqueue(Message(11, bot=False), "99"))
        await self.settle(bridge)
        self.assertFalse(bridge.enqueue(Message(12), "99"))
        self.assertTrue(bridge.enqueue(Message(20, bot=False, channel=21), "99"))
        with self.assertLogs("indeces.discord_bridge", level="WARNING"):
            self.assertFalse(bridge.enqueue(Message(21, author=31), "99"))
        await self.settle(bridge)
        for identity in range(21, 26):
            self.assertTrue(bridge.enqueue(Message(identity, author=31), "99"))
            await self.settle(bridge)
        self.assertFalse(bridge.enqueue(Message(26, author=31), "99"))
        self.assertEqual(self.rejected_reasons(scratch).count("queue_full"), 2)

    async def test_failed_intake_audit_closes_intake_without_consuming_round_and_retry_survives_restart(self):
        bridge, runtime, scratch = self.make_bridge()
        first = Message(1)
        scratch.fail_next_queued = True
        with self.assertLogs("indeces.discord_bridge", level="ERROR"):
            with self.assertRaises(OSError):
                bridge.enqueue(first, "99")
        self.assertFalse(bridge._accepting)
        self.assertEqual(bridge._queue.qsize(), 0)
        self.assertEqual(runtime.messages, [])
        await bridge.close()
        restarted, next_runtime, _ = self.make_bridge()
        for identity in range(1, 6):
            self.assertTrue(restarted.enqueue(Message(identity), "99"))
        self.assertFalse(restarted.enqueue(Message(6), "99"))
        await self.settle(restarted)
        self.assertEqual(len(next_runtime.messages), 5)

    async def test_failed_human_intake_audit_cannot_reset_limit(self):
        bridge, _, scratch = self.make_bridge()
        for identity in range(1, 6):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        await self.settle(bridge)
        scratch.fail_next_queued = True
        human = Message(10, bot=False)
        with self.assertLogs("indeces.discord_bridge", level="ERROR"):
            with self.assertRaises(OSError):
                bridge.enqueue(human, "99")
        self.assertFalse(bridge._accepting)
        await bridge.close()
        restarted, _, _ = self.make_bridge()
        self.assertFalse(restarted.enqueue(Message(11), "99"))
        self.assertTrue(restarted.enqueue(human, "99"))
        self.assertTrue(restarted.enqueue(Message(12), "99"))
        await self.settle(restarted)

    async def test_admission_commit_failure_closes_intake_without_queue_or_counter_progress(self):
        bridge, runtime, scratch = self.make_bridge()
        source = Message(1)
        incoming = select_messages(source, "99", bridge.config)
        original_admission = bridge._bot_gate.prepare(incoming)
        with patch.object(bridge._bot_gate, "commit", side_effect=OSError("synthetic gate commit failure")):
            with self.assertLogs("indeces.discord_bridge", level="ERROR"):
                with self.assertRaises(OSError):
                    bridge.enqueue(source, "99")
        self.assertFalse(bridge._accepting)
        self.assertEqual(bridge._queue.qsize(), 0)
        self.assertEqual(runtime.messages, [])
        self.assertEqual(source.sent, [])
        self.assertEqual(bridge._bot_gate.prepare(incoming), original_admission)
        self.assertTrue(any(event == "discord_admission_failed" for event, _ in scratch.events))
        self.assertFalse(bridge.enqueue(Message(2), "99"))
        self.assertIn("not_running", self.rejected_reasons(scratch))

    async def test_confirmed_bot_delivery_audit_failure_preserves_receipt_and_slot_without_resend(self):
        bridge, runtime, scratch = self.make_bridge()
        scratch.fail_next_delivery_finished = True
        source = Message(1)
        with self.assertLogs("indeces.discord_bridge", level="ERROR"):
            self.assertTrue(bridge.enqueue(source, "99"))
            await asyncio.wait_for(bridge._worker_failed.wait(), timeout=2)
        await self.settle(bridge)
        self.assertEqual(len(source.sent), 1)
        self.assertEqual(len(runtime.receipts), 1)
        self.assertEqual(runtime.receipts[0].text, source.sent[0][0])
        self.assertEqual(runtime.receipts[0].message_ids, ("1001",))
        self.assertFalse(bridge._accepting)
        await bridge.close()
        restarted, next_runtime, _ = self.make_bridge()
        self.assertFalse(restarted.enqueue(source, "99"))
        for identity in range(2, 6):
            self.assertTrue(restarted.enqueue(Message(identity), "99"))
        self.assertFalse(restarted.enqueue(Message(6), "99"))
        await self.settle(restarted)
        self.assertEqual(len(source.sent), 1)
        self.assertEqual(len(next_runtime.messages), 4)

    async def test_limit_and_duplicate_human_survive_store_reopen(self):
        bridge, _, _ = self.make_bridge()
        human = Message(1, bot=False)
        self.assertTrue(bridge.enqueue(human, "99"))
        for identity in range(2, 7):
            self.assertTrue(bridge.enqueue(Message(identity), "99"))
        await self.settle(bridge)
        await bridge.close()
        self.store.close()
        self.store = Store(self.state)
        restarted, runtime, _ = self.make_bridge()
        self.assertFalse(restarted.enqueue(human, "99"))
        self.assertFalse(restarted.enqueue(Message(7), "99"))
        self.assertTrue(restarted.enqueue(Message(10, bot=False), "99"))
        self.assertTrue(restarted.enqueue(Message(11), "99"))
        await self.settle(restarted)
        self.assertEqual([message.message_id for message in runtime.messages], ["10", "11"])


if __name__ == "__main__":
    unittest.main()
