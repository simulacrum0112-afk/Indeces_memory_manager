"""Offline production chat-chain coverage with synthetic graph and native wire.

The actual Discord on_message handler, queue, Runtime, graph grounding, native
planning/continuation, SQLite accounting and delivery code execute. No real
Discord connection, paid call, production graph, receipt or credential is read.
This test evidence does not claim user-interface acceptance against Discord.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces.config import DiscordConfig, KnowledgeConfig, RuntimeConfig
from indeces.contracts import ConfirmedDeliveryPersistenceError, GovernedError, IncomingMessage
from indeces.discord_bridge import DiscordBridge
from indeces.model_provider import create_model_adapter
from indeces.prune_question import prepare_grounding, resolve_grounded_plan
from indeces.claude_prune_budget import PruneBudget
from indeces.runtime import Runtime
from indeces.store import Store

try:
    from test_portable_claude_prune import (
        CHARGE, PRICE, Scratch, ScriptedTransport, SyntheticGraphFixture,
        canonical, config as adapter_config, legacy_fixture, native_message, tool,
    )
except ModuleNotFoundError:
    from tests.test_portable_claude_prune import (
        CHARGE, PRICE, Scratch, ScriptedTransport, SyntheticGraphFixture,
        canonical, config as adapter_config, legacy_fixture, native_message, tool,
    )


QUESTION = "Which coarse target applies to synthetic-1?"


class Message:
    def __init__(self, identity=1, question=QUESTION):
        self.id, self.content = identity, "<@99> " + question
        self.guild, self.channel = SimpleNamespace(id=10), SimpleNamespace(id=20)
        self.author = SimpleNamespace(id=30, display_name="Synthetic human", bot=False)
        self.webhook_id = None
        self.created_at = datetime(2026, 10, 13, tzinfo=timezone.utc)
        self.sent = []

    async def reply(self, text, **options):
        self.sent.append((text, options))
        return SimpleNamespace(id=1000 + self.id)


class ProductionPruneFixture(SyntheticGraphFixture):
    def fixture(self):
        self.temp = tempfile.TemporaryDirectory(prefix="production-prune-offline-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.make_graph()
        self.binding = legacy_fixture(self.directory)
        self.budget = PruneBudget.bind_existing_grant(self.binding, price=PRICE, offline_accepted=True)
        self.addCleanup(self.budget.close)
        self.scratch = Scratch()
        self.config = SimpleNamespace(name="Indeces", adapter=adapter_config(),
            runtime=RuntimeConfig(turn_seconds=5), knowledge=KnowledgeConfig(),
            discord=DiscordConfig(guild_id="10", delivery_seconds=1),
            scratch_dir=self.directory / "scratch", state_dir=self.directory / "state")
        self.config.adapter.budgets["selection"] = self.config.adapter.budgets["reply"]
        self.adapter = create_model_adapter(self.config.adapter, self.scratch, "synthetic-not-a-key")
        self.addAsyncCleanup(self.adapter.close)

    def grounding_plan(self, question=QUESTION):
        grounded = prepare_grounding(self.graph, question)
        target = grounded.targets[0]
        return {"mode": "single", "confidence": "high", "seeds_A": [dict(grounded.catalog[0])],
                "seeds_B": [], "target": target["target"], "level": target["level"],
                "target_start": target["start"], "target_end": target["end"]}

    def replies(self, text="A synthetic grounded answer"):
        return [native_message([{"type": "text", "text": canonical(self.grounding_plan())}], reason="end_turn"),
                native_message([tool("view", "toolu_view")]),
                native_message([tool("prune", "toolu_prune", removals=[{"node_id": 3, "reason": "synthetic", "kind": "logic"}])]),
                native_message([tool("answer", "toolu_answer", choice="none")]),
                native_message([{"type": "text", "text": text}], reason="end_turn")]

    def service(self, transport):
        from indeces.production_prune import PruneQuestionService
        self.adapter._request_override = transport
        return PruneQuestionService(self.config, self.directory / "fixture.toml", self.adapter,
                                    budget=self.budget, graph=self.graph, live=False)

    def incoming(self, identity="1", text=QUESTION, *, bot=False):
        return IncomingMessage(identity, "20", "10", "30", "Synthetic human", text,
                               "2026-10-13T00:00:00+00:00", text, bot)

    def runtime(self, service):
        store = Store(self.directory / "runtime-state")
        self.addCleanup(store.close)
        return Runtime(self.config, store, self.adapter, self.scratch, question_service=service)


class ProductionGroundingTests(ProductionPruneFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()

    async def test_literal_name_maps_to_real_original_member_without_cid_in_model_catalog(self):
        # Different original member proves this is a member mapping, not CID guessing.
        self.graph.m2c = {41: 1, 42: 2, 43: 3}
        grounding = prepare_grounding(self.graph, QUESTION)
        plan = resolve_grounded_plan(self.graph, QUESTION, self.grounding_plan(), grounding)
        self.assertEqual(plan.seeds_A, (41,))
        self.assertEqual(plan.seeds_B, ())
        self.assertTrue(all(set(row) == {"term", "start", "end"} for row in grounding.catalog))
        self.assertNotIn('"cid"', grounding.user)

    async def test_unmatched_question_reaches_model_and_can_abstain_without_fabricated_graph(self):
        plan = {"mode": "abstain", "confidence": "low", "seed_terms_A": [], "seed_terms_B": [],
                "target": "", "level": "", "reply_text": "I need a graph-related question to retrieve evidence."}
        transport = ScriptedTransport([native_message([{"type": "text", "text": canonical(plan)}], reason="end_turn")])
        service = self.service(transport)
        result = await service.answer(self.incoming(text="What is the weather today?"), "unmatched", seconds=2)
        self.assertNotEqual(result.text, plan["reply_text"])
        self.assertIn("graph", result.text)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 2 * CHARGE)
        self.assertEqual(service.last_receipt["status"], "abstain")
        self.assertFalse(service.last_receipt["prune_executed"])

    async def test_conflicting_target_qualifiers_decline_before_native_request(self):
        transport = ScriptedTransport([])
        service = self.service(transport)
        question = "For synthetic-1 use coarse target and fine target."
        with self.assertRaises(GovernedError):
            prepare_grounding(self.graph, question)
        result = await service.answer(self.incoming(text=question), "ambiguous", seconds=2)
        self.assertTrue(result.text.strip())
        self.assertEqual((result.input_tokens, result.output_tokens), (0, 0))
        self.assertEqual(transport.requests, [])

    async def test_model_invented_seed_anchor_is_rejected_after_only_known_plan_charge(self):
        plan = self.grounding_plan()
        plan["seeds_A"][0]["start"] += 1
        transport = ScriptedTransport([native_message([{"type": "text", "text": canonical(plan)}], reason="end_turn")])
        service = self.service(transport)
        with self.assertRaises(GovernedError):
            await service.answer(self.incoming(), "invented", seconds=2)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 2 * CHARGE)
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)


class ProductionChatChainTests(ProductionPruneFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()

    async def test_actual_discord_on_message_to_runtime_native_prune_to_plain_reply(self):
        transport = ScriptedTransport(self.replies())
        service, source = self.service(transport), Message()
        runtime = self.runtime(service)
        bridge = DiscordBridge(self.config, runtime, self.scratch)
        owner = bridge

        class Client:
            def __init__(self, **kwargs):
                self.user = SimpleNamespace(id=99)
            async def __aenter__(self):
                await self.setup_hook()
                return self
            async def __aexit__(self, *args):
                await self.close()
            async def start(self, token):
                self.assert_token = token
                await self.on_message(source)
                await asyncio.wait_for(owner._queue.join(), 2)
            async def close(self):
                pass

        null = SimpleNamespace(none=lambda: SimpleNamespace())
        discord = SimpleNamespace(Client=Client, Intents=null, AllowedMentions=null,
                                  MemberCacheFlags=null, HTTPException=RuntimeError)
        with patch("indeces.discord_bridge.importlib.import_module", return_value=discord):
            await bridge.run("synthetic-not-a-bot-token")
        self.assertEqual([text for text, _ in source.sent], ["A synthetic grounded answer"])
        self.assertEqual(len(transport.requests), 10)
        generated = [payload for path, payload in transport.requests if path == "/messages"]
        self.assertEqual(generated[2]["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertFalse(source.sent[0][1]["mention_author"])
        self.assertEqual(runtime.store.history("10:20")[-1]["content"], source.sent[0][0])
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 6 * CHARGE)
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)

    async def test_chinese_question_is_projected_to_real_graph_before_native_tool_loop(self):
        literal = self.grounding_plan()
        projected = {"mode": "single", "confidence": "high", "seed_terms_A": [literal["seeds_A"][0]["term"]],
                     "seed_terms_B": [], "target": literal["target"], "level": "coarse", "reply_text": ""}
        replies = [native_message([{"type": "text", "text": canonical(projected)}], reason="end_turn"), *self.replies()[1:]]
        transport = ScriptedTransport(replies)
        service = self.service(transport)
        runtime = self.runtime(service)
        delivered = []
        async def deliver(text):
            from indeces.contracts import DeliveryReceipt
            delivered.append(text)
            return DeliveryReceipt(("1001",), text)
        await runtime.process(self.incoming(text="请检索这个概念相关的条件"), deliver)
        self.assertEqual(delivered, ["A synthetic grounded answer"])
        self.assertEqual(len(transport.requests), 10)
        self.assertEqual(service.last_receipt["mapping_provenance"], "semantic_model_projection_v1")
        self.assertIn("prune", service.last_receipt["tools"])
        self.assertEqual(self.budget.snapshot()["pending_nusd"], 0)

    async def test_greeting_gets_model_social_response_without_claiming_prune(self):
        plan = {"mode": "chat", "confidence": "high", "seed_terms_A": [], "seed_terms_B": [],
                "target": "", "level": "", "reply_text": "A model social response"}
        transport = ScriptedTransport([native_message([{"type": "text", "text": canonical(plan)}], reason="end_turn")])
        service = self.service(transport)
        result = await service.answer(self.incoming(text="hi"), "greeting", seconds=2)
        self.assertEqual(result.text, plan["reply_text"])
        self.assertEqual(service.last_receipt["status"], "chat")
        self.assertFalse(service.last_receipt["prune_executed"])
        self.assertEqual(len(transport.requests), 2)

    async def test_native_calls_keep_independent_caps_with_shared_turn_fuse(self):
        from indeces.config import Budget
        self.config.adapter.budgets["selection"] = Budget(4096, 512, 15, "high")
        self.config.adapter.budgets["reply"] = Budget(16384, 2048, 45, "high")
        transport = ScriptedTransport(self.replies())
        service = self.service(transport)
        await service.answer(self.incoming(), "native-output-pool", seconds=2)
        generation = [payload for path, payload in transport.requests if path == "/messages"]
        self.assertTrue(all(call["max_tokens"] == 2048 for call in generation))
        self.assertEqual(self.config.adapter.budgets["selection"].output_tokens, 512)
        self.assertEqual(self.config.adapter.budgets["reply"].output_tokens, 2048)
        self.assertLessEqual(service.last_turn_budget["spent_tokens"], 65536)

    async def test_replayed_discord_message_does_not_repeat_calls_or_delivery(self):
        transport = ScriptedTransport(self.replies())
        runtime = self.runtime(self.service(transport))
        delivered = []
        async def deliver(text):
            from indeces.contracts import DeliveryReceipt
            delivered.append(text)
            return DeliveryReceipt(("1001",), text)
        await runtime.process(self.incoming(), deliver)
        await runtime.process(self.incoming(), deliver)
        self.assertEqual(delivered, ["A synthetic grounded answer"])
        self.assertEqual(len(transport.requests), 10)

    async def test_bot_reply_json_is_decoded_before_same_channel_delivery(self):
        reply = canonical({"action": "reply", "text": "A grounded bot reply"})
        runtime = self.runtime(self.service(ScriptedTransport(self.replies(reply))))
        delivered = []
        async def deliver(text):
            from indeces.contracts import DeliveryReceipt
            delivered.append(text)
            return DeliveryReceipt(("1001",), text)
        await runtime.process(self.incoming(bot=True), deliver)
        self.assertEqual(delivered, ["A grounded bot reply"])

    async def test_bot_skip_does_not_send_or_create_assistant_history(self):
        runtime = self.runtime(self.service(ScriptedTransport(self.replies(canonical({"action": "skip", "text": ""})))))
        async def deliver(text):
            self.fail("skip must not cross delivery boundary")
        await runtime.process(self.incoming(bot=True), deliver)
        self.assertEqual(runtime.store.db.execute("SELECT status FROM turns").fetchone()[0], "skipped")
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 6 * CHARGE)

    async def test_missing_usage_keeps_hold_halts_following_turn_and_never_retries(self):
        message = native_message([{"type": "text", "text": canonical(self.grounding_plan())}], reason="end_turn")
        del message["usage"]
        transport = ScriptedTransport([message])
        service = self.service(transport)
        with self.assertRaises(GovernedError):
            await service.answer(self.incoming(), "unknown", seconds=2)
        snap = self.budget.snapshot()
        self.assertEqual(snap["spent_nusd"], CHARGE)
        self.assertGreater(snap["pending_nusd"], 0)
        self.assertIsNotNone(snap["halted"])
        with self.assertRaises(GovernedError):
            await service.answer(self.incoming("2"), "later", seconds=2)
        self.assertEqual(len(transport.requests), 2)

    async def test_cancel_during_generation_preserves_primary_unknown_hold_without_retry(self):
        reached = asyncio.Event()
        calls = []
        async def transport(path, payload):
            calls.append(path)
            if path == "/messages/count_tokens":
                return {"input_tokens": 10}
            reached.set()
            await asyncio.Event().wait()
        service = self.service(transport)
        task = asyncio.create_task(service.answer(self.incoming(), "cancel", seconds=2))
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())
        self.assertEqual(calls, ["/messages/count_tokens", "/messages"])
        self.assertEqual(self.budget.snapshot()["spent_nusd"], CHARGE)
        self.assertGreater(self.budget.snapshot()["pending_nusd"], 0)
        self.assertIsNotNone(self.budget.snapshot()["halted"])

    async def test_known_plan_charge_survives_cancellation_before_continuation_generation(self):
        plan_transport = ScriptedTransport(self.replies()[:1])
        reached, calls = asyncio.Event(), []
        async def transport(path, payload):
            calls.append(path)
            if len(calls) <= 2:
                return await plan_transport(path, payload)
            reached.set()
            await asyncio.Event().wait()
        service = self.service(transport)
        task = asyncio.create_task(service.answer(self.incoming(), "known-cancel", seconds=2))
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 2 * CHARGE)
        self.assertIsNotNone(self.budget.snapshot()["halted"])
        self.assertEqual(calls, ["/messages/count_tokens", "/messages", "/messages/count_tokens"])

    async def test_confirmed_delivery_audit_failure_latches_without_duplicate_send(self):
        transport = ScriptedTransport(self.replies())
        runtime = self.runtime(self.service(transport))
        original_write = self.scratch.write
        def write(event, **fields):
            if event == "answer_delivered":
                raise OSError("synthetic post-delivery audit failure")
            return original_write(event, **fields)
        self.scratch.write = write
        sent = []
        async def deliver(text):
            from indeces.contracts import DeliveryReceipt
            sent.append(text)
            return DeliveryReceipt(("1001",), text)
        with self.assertRaises(ConfirmedDeliveryPersistenceError) as raised:
            await runtime.process(self.incoming(), deliver)
        self.assertEqual(raised.exception.receipt_ids, ("1001",))
        with self.assertRaises(ConfirmedDeliveryPersistenceError):
            await runtime.process(self.incoming("2"), deliver)
        self.assertEqual(sent, ["A synthetic grounded answer"])
        self.assertEqual(len(transport.requests), 10)
        self.assertEqual(self.budget.snapshot()["spent_nusd"], 6 * CHARGE)
        self.assertEqual(runtime.store.history("10:20")[-1]["content"], sent[0])


class ProductionOwnershipAndFactoryTests(ProductionPruneFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()

    async def test_maintenance_guard_supports_partial_legacy_config_and_still_closes_prune(self):
        from indeces.config import guard_trial_maintenance
        guard_trial_maintenance(SimpleNamespace())
        guard_trial_maintenance(SimpleNamespace(runtime=RuntimeConfig()))
        for config in (SimpleNamespace(runtime=SimpleNamespace(prune_profile_config="selected.toml")),
                       SimpleNamespace(session_budget=SimpleNamespace(enabled=True))):
            with self.assertRaises(GovernedError) as raised:
                guard_trial_maintenance(config)
            self.assertEqual(raised.exception.code, "session_maintenance_forbidden")

    async def test_owned_budget_snapshot_fault_closes_once_and_preserves_primary(self):
        from indeces import production_prune
        primary = OSError("synthetic snapshot fault")
        owned = SimpleNamespace(snapshot=Mock(side_effect=primary), close=Mock())
        with patch.object(production_prune, "_sidecar", return_value=("graph", "evidence", object(), PRICE)), \
                patch.object(production_prune, "Graph", return_value=self.graph), \
                patch.object(production_prune.PruneBudget, "bind_existing_grant", return_value=owned):
            with self.assertRaises(OSError) as raised:
                production_prune.PruneQuestionService(self.config, self.directory / "fixture.toml", self.adapter, live=False)
        self.assertIs(raised.exception, primary)
        owned.close.assert_called_once_with()

    def write_profiles(self, *, recursive=False):
        template = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
        production = template.replace('guild_id = ""', 'guild_id = "111"').replace('state_dir = "state"', 'state_dir = "production-state"')
        production = production.replace("[runtime]", '[runtime]\nprune_profile_config = "selected.toml"')
        selected = template.replace('guild_id = ""', 'guild_id = "222"').replace('state_dir = "state"', 'state_dir = "profile-state"')
        selected = selected.replace("[adapter]", '[adapter]\nprovider = "anthropic"').replace('model = "gpt-6.1-sol"', 'model = "claude-opus-5-5"')
        selected = selected.replace("https://api.openai.com/v1", "https://api.anthropic.com/v1").replace('reasoning = "medium"', 'reasoning = "high"')
        if recursive:
            selected = selected.replace("[runtime]", '[runtime]\nprune_profile_config = "production.toml"')
        path = self.directory / "production.toml"
        path.write_text(production, encoding="utf-8")
        (self.directory / "selected.toml").write_text(selected, encoding="utf-8")
        return path

    async def test_profile_binding_changes_selected_adapter_preserves_production_discord_and_stores(self):
        from indeces.config import load_config
        config = load_config(self.write_profiles())
        self.assertEqual(config.discord.guild_id, "111")
        self.assertEqual(config.state_dir, self.directory / "production-state")
        self.assertEqual(config.scratch_dir, self.directory / "scratch")
        self.assertEqual(config.knowledge_dir, self.directory / "knowledge")
        self.assertEqual((config.adapter.provider, config.adapter.model), ("anthropic", "claude-opus-5-5"))
        self.assertTrue(all(budget.reasoning == "high" for budget in config.adapter.budgets.values()))
        self.assertEqual(Path(config.runtime.prune_profile_config), self.directory / "selected.toml")

    async def test_recursive_profile_binding_rejected_before_adapter_creation(self):
        from indeces.config import load_config
        with patch("indeces.console.create_model_adapter") as create:
            with self.assertRaisesRegex(ValueError, "recursive prune profile"):
                load_config(self.write_profiles(recursive=True))
        create.assert_not_called()

    async def test_console_serve_shares_closed_stage_adapter_and_never_starts_knowledge_worker(self):
        from indeces import console, production_prune
        self.config.runtime = replace(self.config.runtime, prune_profile_config=str(self.directory / "selected.toml"))
        raw = SimpleNamespace(close=AsyncMock())
        question_service = SimpleNamespace(close=AsyncMock(), halt=Mock())
        store = SimpleNamespace(recover=Mock(return_value=0), close=Mock())
        scratch = SimpleNamespace(path=self.directory / "scratch", write=Mock(), close=Mock(), startup_retention=None)
        runtime = SimpleNamespace(graph=object(), model_selector=None)
        knowledge = SimpleNamespace(start=Mock(), close=AsyncMock())
        bridge = SimpleNamespace(run=AsyncMock(), close=AsyncMock())
        observer = SimpleNamespace(start=Mock(return_value="http://127.0.0.1:1"), close=Mock())
        lease = SimpleNamespace(close=Mock())
        with ExitStack() as stack:
            for name in ("_check_model_start_config", "validate_runtime_paths", "prepare_knowledge_directory", "prepare_scratch_directory"):
                stack.enter_context(patch.object(console, name))
            stack.enter_context(patch.object(console, "checked_session_envelope", return_value=None))
            stack.enter_context(patch.object(console, "ScratchLog", return_value=scratch))
            stack.enter_context(patch.object(console, "Store", return_value=store))
            stack.enter_context(patch.object(console, "create_model_adapter", return_value=raw))
            factory = stack.enter_context(patch.object(console, "create_runtime", return_value=runtime))
            knowledge_factory = stack.enter_context(patch.object(console, "KnowledgeService", return_value=knowledge))
            stack.enter_context(patch.object(console, "DiscordBridge", return_value=bridge))
            stack.enter_context(patch.object(console, "ObserverServer", return_value=observer))
            service_factory = stack.enter_context(patch.object(production_prune, "PruneQuestionService", return_value=question_service))
            await console.serve(self.config, "synthetic-key", "synthetic-token", lease=lease)
        guarded = factory.call_args.args[2]
        self.assertIs(type(guarded), production_prune.ClosedStageAdapter)
        self.assertIs(guarded, knowledge_factory.call_args.args[3])
        self.assertIs(factory.call_args.kwargs["question_service"], question_service)
        self.assertIs(service_factory.call_args.args[2], raw)
        knowledge.start.assert_not_called()
        knowledge.close.assert_awaited_once_with()
        raw.close.assert_awaited_once_with()
        question_service.close.assert_awaited_once_with()
        bridge.close.assert_awaited_once_with()
        store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
