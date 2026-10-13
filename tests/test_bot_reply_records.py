from __future__ import annotations

from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import discord

from indeces.adapter import OpenAIAdapter
from indeces.bot_conversations import BotConversationGate
from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, RuntimeConfig
from indeces.contracts import DeliveryReceipt, FailureNotice, IncomingMessage
from indeces.discord_bridge import DiscordBridge, discord_reply_text, select_messages
from indeces.memory import MemoryGraph
from indeces.observer_data import _scratch_view
from indeces import prompts
from indeces.run_records import answer_record, verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store
from indeces.user_notices import user_notice


class BotReplyRecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "state")
        self.config = SimpleNamespace(name="Indeces", knowledge_dir=self.root / "knowledge",
            knowledge=KnowledgeConfig(), discord=DiscordConfig("10"),
            runtime=RuntimeConfig(summary_max_bytes=256, turn_seconds=5),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {
                stage: Budget(16384, 2048, 2, reasoning="low") for stage in ("reply", "summary", "label")}))
        MemoryGraph(self.store.db).add("10:knowledge", "synthetic-source", "prior synthetic source", [
            {"text": "alpha topic documented source", "quote": "alpha topic documented source", "marks": ["alpha", "topic"]}], 1.0)
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def run_turn(self, action="skip", text="", *, raw=None):
        self.counter += 1
        log = ScratchLog(self.root / "scratch" / str(self.counter))
        output = json.dumps({"action": action, "text": text}, ensure_ascii=False) if raw is None else raw
        requests, deliveries = [], []

        async def request(path, payload):
            requests.append((path, deepcopy(payload)))
            if path == "/responses/input_tokens":
                return {"input_tokens": 10}
            return {"id": "synthetic-bot-decision", "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": output}]}]}

        async def deliver(reply):
            if isinstance(reply, FailureNotice):
                reply = reply.text
            deliveries.append(reply)
            return DeliveryReceipt(("synthetic-delivery",), discord_reply_text(reply))

        adapter = OpenAIAdapter(self.config.adapter, log, request=request)
        message = IncomingMessage("bot-message-" + str(self.counter), "20", "10", "30", "Other Bot",
                                  "alpha topic", "2026-09-30T12:00:00+00:00", author_is_bot=True)
        try:
            await Runtime(self.config, self.store, adapter, log).process(message, deliver)
        finally:
            await adapter.close()
            log.close()
        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
        verify(log.path)
        entries = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        return log.path, entries, deliveries, message

    @staticmethod
    def event(entries, event):
        return next(item["fields"] for item in entries if item["event"] == event)

    def rewritten(self, entries, name, *, edit=None, omit=(), extra=()):
        log = ScratchLog(self.root / "rewritten" / name)
        try:
            for item in deepcopy(entries):
                if item["event"] in omit:
                    continue
                if edit:
                    edit(item["event"], item["fields"])
                if item["event"] == "turn_end":
                    for event, fields in extra:
                        log.write(event, **fields)
                log.write(item["event"], **item["fields"])
        finally:
            log.close()
        verify(log.path)
        return log.path

    def retained(self, entries, name, first_event):
        base = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        now = [base]
        log = ScratchLog(self.root / "retained" / name, clock=lambda: now[0])
        boundary = next(index for index, item in enumerate(entries) if item["event"] == first_event)
        try:
            for index, item in enumerate(entries):
                now[0] = base + timedelta(seconds=index)
                log.write(item["event"], **item["fields"])
            now[0] = base + timedelta(days=1, seconds=boundary - 0.5)
            log.prune()
        finally:
            log.close()
        verify(log.path)
        return log.path

    def assert_invalid(self, path, reason=None):
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        if reason:
            self.assertTrue(any(reason in issue["reason"] for issue in result["issues"]), result)
        return result

    async def test_skip_uses_one_governed_reply_call_and_no_delivery_or_assistant_history(self):
        path, entries, deliveries, message = await self.run_turn()
        report = verify_runs([path])
        self.assertEqual(report["counts"]["skipped"], 1, report)
        self.assertEqual(report["counts"]["complete"], 0)
        self.assertEqual(report["call_counts"]["complete"], 1)
        self.assertEqual(report["issues"], [])
        self.assertEqual(deliveries, [])
        self.assertFalse(set(item["event"] for item in entries)
                         & {"answer_generated", "delivery_start", "answer_delivered", "failure_notice_delivered"})
        self.assertEqual(self.store.db.execute("SELECT status FROM turns WHERE message_id=?", (message.message_id,)).fetchone()[0], "skipped")
        self.assertEqual([row["role"] for row in self.store.history(message.scope)], ["user"])
        self.assertIsNone(self.store.db.execute("SELECT reply FROM turns WHERE message_id=?", (message.message_id,)).fetchone()[0])
        decision = self.event(entries, "bot_reply_decision")
        self.assertEqual((decision["action"], decision["text"]), ("skip", ""))
        self.assertEqual(json.loads(decision["model_result"]["text"]), {"action": "skip", "text": ""})
        context = self.event(entries, "reply_context")
        self.assertEqual(context["response_schema"], prompts.BOT_REPLY_SCHEMA)
        requests = [item["fields"]["payload"] for item in entries if item["event"] == "http_request"]
        self.assertEqual(requests[0]["text"]["format"]["schema"], prompts.BOT_REPLY_SCHEMA)
        self.assertEqual(requests[1]["reasoning"]["effort"], "low")
        self.assertEqual(requests[1]["text"]["verbosity"], "high")

    async def test_reply_binds_raw_decision_and_decoded_citation_receipts(self):
        text = "Documented conclusion [M1]."
        path, entries, deliveries, _ = await self.run_turn("reply", text)
        report = verify_runs([path])
        self.assertEqual(report["counts"]["complete"], 1, report)
        self.assertEqual(report["issues"], [])
        self.assertEqual(deliveries, [text])
        generated = self.event(entries, "answer_generated")
        self.assertEqual(generated["record"]["text"], text)
        self.assertEqual(generated["model_result"], self.event(entries, "bot_reply_decision")["model_result"])
        self.assertEqual(generated["record"]["citations"][0]["source_id"], "synthetic-source")
        self.assertEqual(self.event(entries, "delivery_start")["text"], text)

    async def test_truncation_validates_decoded_generated_and_actual_delivered_text(self):
        text = "Documented [M1]. " + "a" * 2100 + " Later [M1]."
        path, entries, _, _ = await self.run_turn("reply", text)
        report = verify_runs([path])
        self.assertEqual(report["counts"]["complete"], 1, report)
        self.assertEqual(report["issues"], [])
        self.assertEqual(self.event(entries, "answer_generated")["record"]["text"], text)
        self.assertEqual(self.event(entries, "answer_delivered")["record"]["text"], discord_reply_text(text))

    async def test_fabricated_decision_or_usage_is_rejected_despite_valid_hashes(self):
        _, entries, _, _ = await self.run_turn()
        for mutation in ("action", "text", "usage", "link"):
            with self.subTest(mutation=mutation):
                def change(event, fields):
                    if event != "bot_reply_decision":
                        return
                    if mutation == "action":
                        fields["action"] = "reply"
                    elif mutation == "text":
                        fields["text"] = "synthetic forged private answer"
                    elif mutation == "usage":
                        fields["model_result"]["input_tokens"] += 1
                    else:
                        fields["retrieval_sha256"] = "0" * 64
                result = self.assert_invalid(self.rewritten(entries, mutation, edit=change))
                self.assertNotIn("synthetic forged private answer", repr(result))

    async def test_provider_json_must_be_strict_even_when_decision_receipt_agrees(self):
        _, entries, _, _ = await self.run_turn()
        outputs = ('{"action":"reply","action":"skip","text":""}',
                   '{"action":"skip","text":"","extra":true}',
                   '{"action":"skip","text":"closing message"}',
                   '{"action":"reply","text":" "}',
                   '{"action":"skip","text":"","extra":NaN}')
        for index, raw in enumerate(outputs):
            with self.subTest(raw=raw):
                def change(event, fields):
                    if event == "http_response" and fields["path"] == "/responses":
                        fields["payload"]["output"][0]["content"][0]["text"] = raw
                    elif event == "call_end":
                        fields["result"]["text"] = raw
                    elif event == "bot_reply_decision":
                        fields["model_result"]["text"] = raw
                self.assert_invalid(self.rewritten(entries, "raw-" + str(index), edit=change), "bot decision")

    async def test_missing_decision_call_transport_or_end_cannot_be_complete_skip(self):
        _, entries, _, _ = await self.run_turn()
        for event in ("bot_reply_decision", "call_end", "http_request", "http_response"):
            with self.subTest(event=event):
                self.assert_invalid(self.rewritten(entries, "missing-" + event, omit=(event,)))
        report = verify_runs([self.rewritten(entries, "missing-end", omit=("turn_end",))])
        self.assertEqual(report["counts"]["incomplete"], 1, report)
        self.assertEqual(report["counts"]["skipped"], 0)
        self.assertEqual(report["issues"], [])

    async def test_skip_without_bot_author_declaration_is_rejected(self):
        _, entries, _, _ = await self.run_turn()
        for value in (False, "true", None):
            with self.subTest(value=value):
                def change(event, fields):
                    if event == "turn_start":
                        if value is None:
                            fields["input"].pop("author_is_bot")
                        else:
                            fields["input"]["author_is_bot"] = value
                self.assert_invalid(self.rewritten(entries, "author-" + str(value), edit=change), "bot")

    async def test_context_and_actual_provider_schema_both_bind_the_decision(self):
        _, entries, _, _ = await self.run_turn()
        for mutation in ("context", "request", "both"):
            with self.subTest(mutation=mutation):
                def change(event, fields):
                    if event == "reply_context" and mutation in {"context", "both"}:
                        fields["response_schema"] = {"type": "object"}
                    elif event == "http_request" and mutation in {"request", "both"}:
                        fields["payload"]["text"]["format"]["schema"] = {"type": "object"}
                self.assert_invalid(self.rewritten(entries, "schema-" + mutation, edit=change), "schema")

    async def test_skip_rejects_any_generated_delivery_or_failure_notice_evidence(self):
        _, entries, _, _ = await self.run_turn()
        decision = self.event(entries, "bot_reply_decision")
        retrieval = self.event(entries, "retrieval_record")["record"]
        for event in ("answer_generated", "delivery_start", "answer_delivered", "failure_notice_delivered", "failure_notice_unknown"):
            with self.subTest(event=event):
                fields = {"trace_id": decision["trace_id"], "retrieval_sha256": decision["retrieval_sha256"],
                          "record": answer_record("fabricated reply [M1]", retrieval), "text": "fabricated reply [M1]",
                          "model_result": decision["model_result"], "receipt_ids": ["fabricated-receipt"]}
                self.assert_invalid(self.rewritten(entries, "illegal-" + event, extra=((event, fields),)))

    async def test_decoded_answer_and_delivery_start_cannot_substitute_model_text(self):
        _, entries, _, _ = await self.run_turn("reply", "Documented answer [M1].")
        for mutation in ("generated", "delivery"):
            with self.subTest(mutation=mutation):
                def change(event, fields):
                    if event == "answer_generated" and mutation == "generated":
                        fields["record"] = answer_record("substituted answer [M1]", self.event(entries, "retrieval_record")["record"])
                    elif event == "delivery_start" and mutation == "delivery":
                        fields["text"] = "substituted answer [M1]"
                self.assert_invalid(self.rewritten(entries, "decoded-" + mutation, edit=change), "output mismatch")

    async def test_retained_skip_remains_partial_with_or_without_decision(self):
        _, entries, _, _ = await self.run_turn()
        for boundary in ("memory_observation", "bot_reply_decision", "turn_end"):
            with self.subTest(boundary=boundary):
                report = verify_runs([self.retained(entries, "skip-" + boundary, boundary)])
                self.assertEqual(report["counts"]["retention_partial"], 1, report)
                self.assertEqual(report["counts"]["skipped"], 0)
                self.assertEqual(report["issues"], [])
                if boundary == "turn_end":
                    self.assertIn("expired_bot_reply_decision", report["turns"][0]["warnings"])

    async def test_retained_decoded_reply_remains_partial_after_decision_expires(self):
        _, entries, _, _ = await self.run_turn("reply", "Documented answer [M1].")
        for boundary in ("bot_reply_decision", "answer_generated", "answer_delivered"):
            with self.subTest(boundary=boundary):
                report = verify_runs([self.retained(entries, "reply-" + boundary, boundary)])
                self.assertEqual(report["counts"]["retention_partial"], 1, report)
                self.assertEqual(report["counts"]["complete"], 0)
                self.assertEqual(report["issues"], [])

    async def test_observer_lists_skip_distinctly_from_failed_generation(self):
        path, _, _, _ = await self.run_turn()
        config = SimpleNamespace(scratch_dir=path.parent)
        view, _, _, warnings = _scratch_view(config, datetime.now(timezone.utc) + timedelta(seconds=1))
        self.assertEqual(warnings, [])
        self.assertEqual(view["traces"][0]["status"], "skipped")

    async def test_invalid_decision_returns_fixed_failure_notice_without_fabricated_skip(self):
        path, entries, deliveries, _ = await self.run_turn(raw='{"action":"skip","text":"not empty"}')
        report = verify_runs([path])
        self.assertEqual(report["counts"]["failed"], 1, report)
        self.assertEqual(report["counts"]["skipped"], 0)
        self.assertEqual(report["issues"], [])
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0], user_notice("invalid_bot_reply_decision"))
        self.assertNotIn("invalid_bot_reply_decision", deliveries[0])
        self.assertNotIn("<@", deliveries[0])
        self.assertEqual(self.event(entries, "failure_notice_delivered")["receipt"]["text"], deliveries[0])
        self.assertNotIn("bot_reply_decision", {item["event"] for item in entries})

    async def test_bridge_runtime_adapter_five_rounds_include_one_skip_and_reject_sixth(self):
        requests = []
        generations = []
        sent = []
        log = ScratchLog(self.root / "integrated")

        class Message:
            def __init__(self, round_number):
                self.id = 91000 + round_number
                self.guild = SimpleNamespace(id=10)
                self.channel = SimpleNamespace(id=221)
                self.author = SimpleNamespace(id=331, display_name="Synthetic Peer", bot=True)
                self.content = "<@991> alpha topic " + ("thanks, conversation complete" if round_number == 3 else "question " + str(round_number))
                self.webhook_id = None
                self.created_at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
                self.sent = []

            async def reply(self, text, **options):
                self.sent.append((text, options))
                sent.append((self.id, text, options))
                return SimpleNamespace(id=100000 + self.id)

        async def request(path, payload):
            requests.append((path, deepcopy(payload)))
            if path == "/responses/input_tokens":
                return {"input_tokens": 10}
            round_number = len(generations) + 1
            decision = {"action": "skip", "text": ""} if round_number == 3 else {
                "action": "reply", "text": f"Documented answer {round_number} [M1]. <@331>"}
            generations.append(decision)
            return {"id": "synthetic-integrated-" + str(round_number), "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "role": "assistant", "content": [{
                    "type": "output_text", "text": json.dumps(decision)}]}]}

        adapter = OpenAIAdapter(self.config.adapter, log, request=request)
        runtime = Runtime(self.config, self.store, adapter, log)
        bridge = DiscordBridge(self.config, runtime, log)
        bridge._discord = discord
        bridge._allowed_mentions = discord.AllowedMentions.none()
        sources = [Message(round_number) for round_number in range(1, 7)]
        bridge._start_worker()
        try:
            for source in sources[:5]:
                self.assertTrue(bridge.enqueue(source, "991"))
            self.assertFalse(bridge.enqueue(sources[5], "991"))
            await asyncio.wait_for(bridge._queue.join(), timeout=5)
            self.assertFalse(bridge._worker.done())
        finally:
            await bridge.close()
            await adapter.close()
            log.close()

        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"] * 5)
        self.assertEqual(len(generations), 5)
        self.assertEqual([identity for identity, _, _ in sent], [91001, 91002, 91004, 91005])
        self.assertEqual(sources[2].sent, [])
        self.assertEqual(sources[5].sent, [])
        for identity, text, options in sent:
            round_number = identity - 91000
            decoded = generations[round_number - 1]["text"].replace("<@331>", "").strip()
            expected = "<@331> " + decoded if round_number < 5 else decoded
            self.assertEqual(text, expected)
            self.assertFalse(text.startswith("{"))
            self.assertFalse(options["mention_author"])
            allowed = options["allowed_mentions"].to_dict()
            self.assertEqual(allowed.get("parse"), [])
            self.assertEqual(allowed.get("users", []), [331] if round_number < 5 else [])
            if round_number == 5:
                self.assertNotIn("<@331>", text)

        verify(log.path)
        report = verify_runs([log.path])
        self.assertEqual(report["counts"]["complete"], 4, report)
        self.assertEqual(report["counts"]["skipped"], 1, report)
        self.assertEqual(report["call_counts"]["complete"], 5, report)
        self.assertEqual(report["issues"], [])
        entries = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
        calls = [item["fields"] for item in entries if item["event"] == "call_end"]
        self.assertEqual(sum(call["known_usage"]["input_tokens"] for call in calls), 50)
        self.assertEqual(sum(call["known_usage"]["output_tokens"] for call in calls), 25)
        decisions = [item["fields"] for item in entries if item["event"] == "bot_reply_decision"]
        self.assertEqual([decision["action"] for decision in decisions], ["reply", "reply", "skip", "reply", "reply"])
        rejected = [item["fields"] for item in entries if item["event"] == "discord_message_rejected"]
        self.assertEqual([(item["message_id"], item["reason"]) for item in rejected], [("91006", "round_limit")])
        history = self.store.history("10:221")
        self.assertEqual([row["role"] for row in history], ["user", "assistant", "user", "assistant", "user", "user", "assistant", "user", "assistant"])
        self.assertEqual([row["content"] for row in history if row["role"] == "assistant"], [text for _, text, _ in sent])
        self.assertEqual([row["role"] for row in history if row["turn_id"] == "91003"], ["user"])

        reopened = Store(self.root / "state")
        try:
            gate = BotConversationGate(reopened.db)
            admission = gate.prepare(select_messages(sources[5], "991", self.config.discord))
            self.assertFalse(admission.allowed)
            self.assertEqual(admission.reason, "round_limit")
            self.assertEqual(reopened.db.execute("SELECT rounds FROM bot_conversation_peers WHERE scope=? AND author_id=?",
                                               ("10:221", "331")).fetchone()[0], 5)
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM bot_conversation_accepted").fetchone()[0], 5)
            self.assertIsNone(reopened.db.execute("SELECT message_id FROM turns WHERE message_id='91006'").fetchone())
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
