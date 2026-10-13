"""Independent terminal-session gates with a real bridge and fake Gateway.

All application factories, SQLite, ranker, adapter accounting and bridge.run
execute. Only Discord SDK/network, provider requests, observer/retention and the
pinned archive loader are synthetic. Prices are test assumptions, never live
provider certification. No production state or credential is used.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import io
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, reingest
from indeces.adapter import OpenAIAdapter
from indeces.config import Budget
from indeces.contracts import ConfirmedDeliveryPersistenceError, GovernedError
from indeces.discord_bridge import DiscordBridge
from indeces.knowledge import KnowledgeService
from indeces.richard_v11_ranker import FrozenRanker
from indeces.session_budget import SessionBudgetAdapter, SessionBudgetLedger
from tests import test_richard_chat_runtime as authored
from tests.test_discord_bridge import HTTPError, Message
from tests.test_run_records import RecordFixture
from tests import test_session_budget_factory as factory_fixture


OBSERVATIONS = []


class SessionBudgetLifecycleTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        factory_fixture.SessionBudgetFactoryTests.setUp(self)

    def tearDown(self):
        factory_fixture.SessionBudgetFactoryTests.tearDown(self)

    def seed_bound_record(self, *args, **kwargs):
        return factory_fixture.SessionBudgetFactoryTests.seed_bound_record(self, *args, **kwargs)

    def assert_leases_released(self):
        return factory_fixture.SessionBudgetFactoryTests.assert_leases_released(self)

    async def run_native_gateway(self, *, unknown=True, prequeued=False,
                                 late=False, late_bot=False, halt_before=None,
                                 fault=None, cancel_at=None, ordinary=False):
        """Call actual Console.serve and actual DiscordBridge.run once."""
        capture, requests, lifecycle, messages, processed, begun = {}, [], [], [], [], []
        admissions, bot_calls, ranks, loop_errors = [], [], [], []
        generation_entered, notice_entered = asyncio.Event(), asyncio.Event()
        cfg = self.config
        if ordinary:
            cfg = replace(cfg, session_budget=replace(cfg.session_budget, enabled=False))
        if fault == "ceiling":
            cfg = replace(cfg, session_budget=replace(cfg.session_budget, ceiling_usd="0.02"))
        self.config = cfg
        query = authored.DUAL_QUERY
        plan = authored.grounded_plan(query)
        native_cls, ledger_cls = OpenAIAdapter, SessionBudgetLedger
        runtime_actual, knowledge_cls = console.create_runtime, KnowledgeService
        rank_actual = FrozenRanker.rank

        async def transport(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            requests.append({"path": path, "stage": stage})
            self.assertIn(stage, {"selection", "reply"})
            self.assertFalse(capture["store"].db.in_transaction)
            if path == "/responses/input_tokens":
                return {"input_tokens": 43 if stage == "selection" else 47}
            self.assertEqual(path, "/responses")
            if cancel_at == "generation":
                generation_entered.set()
                await asyncio.Event().wait()
            if stage == "selection":
                model_input = json.loads(payload["input"][0]["content"])
                ids = [row["record_id"] for row in model_input["candidates"]
                       [:model_input["required_selection_count"]]]
                text = json.dumps({"selected_record_ids": ids, "richard_plan": plan})
            else:
                text = "Synthetic controlled reply [M1]."
            return authored.completion(text, stage, unknown_usage=unknown and stage == "selection")

        def native_factory(config, scratch, key):
            native = native_cls(config, scratch, key, request=transport)
            fake_session = SimpleNamespace(close=AsyncMock())
            native._session = fake_session
            actual_close = native.close

            async def close():
                lifecycle.append("native")
                await actual_close()

            native.close = close
            capture.update(native=native, http_session=fake_session, scratch=scratch)
            return native

        def ledger_factory(*args, **kwargs):
            ledger = ledger_cls(*args, **kwargs)
            actual_close = ledger.close

            def close():
                lifecycle.append("ledger")
                actual_close()

            ledger.close = close
            capture["ledger"] = ledger
            return ledger

        def runtime_factory(config, store, adapter, scratch, **kwargs):
            runtime = runtime_actual(config, store, adapter, scratch, **kwargs)
            capture.update(runtime=runtime, store=store, wrapper=adapter)
            if not ordinary:
                self.assertIs(type(adapter), SessionBudgetAdapter)
                self.assertIs(adapter.ledger, capture["ledger"])
            self.assertIs(runtime.model_selector.adapter, adapter)
            actual_process, actual_begin = runtime.process, store.begin

            async def process(message, deliver):
                processed.append(message.message_id)
                try:
                    return await actual_process(message, deliver)
                finally:
                    lifecycle.append("runtime_settled:" + message.message_id)

            def begin(message):
                begun.append(message.message_id)
                return actual_begin(message)

            runtime.process, store.begin = process, begin
            actual_write = scratch.write

            def write(event, **fields):
                if event == "controlled_session_stopped":
                    capture.setdefault("stop_events", []).append(deepcopy(fields))
                if fault == "stop_sink" and event in {"controlled_session_stopped", "discord_message_dropped"}:
                    raise OSError("synthetic terminal diagnostic sink failure")
                if fault == "confirmed_notice_audit" and event == "failure_notice_delivered":
                    raise OSError("synthetic confirmed notice audit failure")
                return actual_write(event, **fields)

            scratch.write = write
            return runtime

        def knowledge_factory(*args):
            knowledge = knowledge_cls(*args)
            capture["knowledge"] = knowledge
            self.assertIs(knowledge.adapter, capture["wrapper"])
            actual_close = knowledge.close

            async def close():
                lifecycle.append("knowledge")
                await actual_close()

            knowledge.close = close
            return knowledge

        def bridge_factory(*args, **kwargs):
            bridge = DiscordBridge(*args, **kwargs)
            capture["bridge"] = bridge
            original_prepare, original_commit = bridge._bot_gate.prepare, bridge._bot_gate.commit

            def prepare(*args, **kwargs):
                bot_calls.append("prepare")
                return original_prepare(*args, **kwargs)

            def commit(*args, **kwargs):
                bot_calls.append("commit")
                return original_commit(*args, **kwargs)

            bridge._bot_gate.prepare, bridge._bot_gate.commit = prepare, commit
            original_enqueue = bridge.enqueue

            def enqueue(message, bot_id):
                previous = len(bot_calls)
                accepted = original_enqueue(message, bot_id)
                admissions.append({"id": str(message.id), "accepted": accepted,
                    "bot_calls_delta": len(bot_calls) - previous,
                    "accepting": bridge._accepting})
                return accepted

            bridge.enqueue = enqueue
            actual_close = bridge.close

            async def close():
                lifecycle.append("bridge")
                await actual_close()

            bridge.close = close
            return bridge

        class PeerMessage(Message):
            async def reply(message, text, **options):
                message.sent.append((text, options))
                if unknown or fault == "ceiling":
                    notice_entered.set()
                    if fault == "unknown_config_drift":
                        capture["native"].config = replace(capture["native"].config,
                            model="synthetic-different-model-after-unknown")
                    if late and len(messages) == 1:
                        after = Message(811, text="<@99> synthetic after halt")
                        after.author.bot = late_bot
                        messages.append(after)
                        bridge = capture["bridge"]
                        capture["halt_at_late"] = capture["ledger"].snapshot()["halted"]
                        capture["late_accepted"] = bridge.enqueue(after, "99")
                    if cancel_at == "notice":
                        await asyncio.Event().wait()
                    if fault == "notice_halt_race":
                        await asyncio.Event().wait()
                return SimpleNamespace(id=1000 + message.id)

        async def driver(client):
            bridge = capture["bridge"]
            first = PeerMessage(810, text="<@99> " + query)
            messages.append(first)
            if halt_before == "intake":
                capture["ledger"].halt("synthetic_terminal_halt")
            if fault == "snapshot_io":
                primary = OSError("synthetic snapshot I/O failure")
                capture["primary_io"] = primary
                capture["ledger"].snapshot = Mock(side_effect=primary)
            if fault == "config":
                capture["native"].config = replace(capture["native"].config, model="synthetic-different-model")
            if fault == "callback_missing":
                capture["runtime"].session_stop_error = None
            if fault == "callback_invalid":
                capture["runtime"].session_stop_error = lambda: "invalid-healthy-result"
            await client.on_message(first)
            if prequeued:
                pending = Message(99001, text="<@99> synthetic prequeued")
                pending.author.bot = True
                messages.append(pending)
                await client.on_message(pending)
            if halt_before == "consume":
                capture["ledger"].halt("synthetic_terminal_halt")
            if ordinary:
                await bridge._queue.join()
                second = Message(811, text="<@99> " + query)
                messages.append(second)
                await client.on_message(second)
                await bridge._queue.join()
                return
            await asyncio.Event().wait()

        class FakeClient:
            def __init__(client, **kwargs):
                client.user = SimpleNamespace(id=99)
                capture["discord_client"] = client
                capture["discord_options"] = kwargs

            async def __aenter__(client):
                await client.setup_hook()
                return client

            async def __aexit__(client, *_):
                await client.close()

            async def start(client, token):
                self.assertEqual(token, "synthetic-offline-token")
                try:
                    await driver(client)
                finally:
                    lifecycle.append("gateway_joined")

            async def close(client):
                lifecycle.append("discord_client_closed")
                if fault == "client_close":
                    raise OSError("synthetic Discord client close failure")

        fake_discord = SimpleNamespace(Client=FakeClient,
            Intents=SimpleNamespace(none=lambda: SimpleNamespace()),
            AllowedMentions=authored.SyntheticAllowedMentions,
            MemberCacheFlags=SimpleNamespace(none=lambda: object()),
            HTTPException=HTTPError, Object=lambda identity: SimpleNamespace(id=identity))
        import_actual = __import__("importlib").import_module

        def import_module(name, *args, **kwargs):
            return fake_discord if name == "discord" else import_actual(name, *args, **kwargs)

        async def retention(*_):
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append("retention_joined")

        def rank(ranker, *args, **kwargs):
            result = rank_actual(ranker, *args, **kwargs)
            ranks.append([row.cid for row in result])
            return result

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: loop_errors.append(context))
        error = None
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(console, "create_model_adapter", side_effect=native_factory))
                stack.enter_context(patch.object(console, "SessionBudgetLedger", side_effect=ledger_factory))
                stack.enter_context(patch.object(console, "create_runtime", side_effect=runtime_factory))
                stack.enter_context(patch.object(console, "KnowledgeService", side_effect=knowledge_factory))
                stack.enter_context(patch.object(console, "DiscordBridge", side_effect=bridge_factory))
                stack.enter_context(patch.object(console, "ObserverServer", return_value=SimpleNamespace(
                    start=Mock(return_value="synthetic observer disabled"), close=Mock())))
                stack.enter_context(patch.object(console, "_maintain_scratch", side_effect=retention))
                stack.enter_context(patch("indeces.discord_bridge.importlib.import_module", side_effect=import_module))
                stack.enter_context(patch("indeces.richard_v11_data.load_release", return_value=self.richard_graph))
                stack.enter_context(patch.object(FrozenRanker, "rank", autospec=True, side_effect=rank))
                if not ordinary:
                    stack.enter_context(patch.object(knowledge_cls, "start", side_effect=AssertionError("worker forbidden")))
                else:
                    stack.enter_context(patch.object(knowledge_cls, "start", return_value=None))
                if fault == "stop_logger":
                    stack.enter_context(patch("indeces.discord_bridge._LOGGER.error", side_effect=OSError("synthetic logger fault")))
                stack.enter_context(redirect_stdout(io.StringIO()))
                task = asyncio.create_task(console.serve(cfg, "synthetic-offline-key", "synthetic-offline-token"))
                if cancel_at:
                    await asyncio.wait_for((generation_entered if cancel_at == "generation" else notice_entered).wait(), 5)
                    task.cancel()
                try:
                    await asyncio.wait_for(task, 8)
                except BaseException as caught:
                    error = caught
                await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
        self.assertEqual(self.network_attempts, [])
        bridge = capture["bridge"]
        self.assertFalse(bridge._accepting)
        self.assertIsNone(bridge._client)
        self.assertFalse(bridge._running)
        self.assertTrue(bridge._worker is None or bridge._worker.done())
        self.assertEqual(bridge._queue.qsize(), 0)
        self.assertEqual(bridge._queue._unfinished_tasks, 0)
        self.assertTrue(capture["scratch"].stream.closed)
        self.assertEqual(capture["knowledge"]._tasks, [])
        capture["http_session"].close.assert_awaited_once()
        self.assertIsNone(capture["native"]._session)
        with self.assertRaises(sqlite3.ProgrammingError):
            capture["store"].db.execute("SELECT 1")
        for a, b in (("gateway_joined", "native"), ("retention_joined", "native"),
                     ("bridge", "knowledge"), ("knowledge", "native")):
            self.assertLess(lifecycle.index(a), lifecycle.index(b))
        state = None if ordinary else self.assert_leases_released()
        if not ordinary:
            self.assertTrue(capture["ledger"]._closed)
            self.assertLess(lifecycle.index("native"), lifecycle.index("ledger"))
        record = {"case": self._testMethodName, "requests": requests,
            "processed": processed, "store_begin": begun, "admissions": admissions,
            "delivery_counts": [len(m.sent) for m in messages], "bot_calls": bot_calls,
            "ranks": ranks, "lifecycle": lifecycle, "snapshot": state,
            "error_type": type(error).__name__ if error else None,
            "worker_error_type": type(bridge._worker_error).__name__,
            "worker_error_code": getattr(bridge._worker_error, "code", None),
            "worker_error_remote_usage_unknown": getattr(bridge._worker_error, "remote_usage_unknown", False),
            "stop_events": capture.get("stop_events", []),
            "loop_diagnostic_errors": [type(c.get("exception")).__name__ for c in loop_errors]}
        OBSERVATIONS.append(deepcopy(record))
        return capture, record, error

    def assert_unknown_hold(self, record):
        state = record["snapshot"]
        self.assertEqual(state["halted"], "session_usage_unknown")
        self.assertEqual(len(state["calls"]), 1)
        self.assertEqual(len(state["turns"]), 1)
        self.assertEqual(state["spent_usd"], "0")
        self.assertEqual(state["reserved_usd"], "0.0173728")
        self.assertEqual(state["calls"][0]["status"], "unknown")
        self.assertEqual(state["calls"][0]["generation_intents"], 1)
        self.assertEqual(state["automatic_retries"], 0)
        self.assertEqual(len(record["requests"]), 2)

    async def test_new_message_after_unknown_halt_rejected_before_bot_admission(self):
        capture, r, error = await self.run_native_gateway(late=True, late_bot=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse(capture["late_accepted"])
        self.assertEqual(capture["halt_at_late"], "session_usage_unknown")
        self.assertEqual(r["admissions"][1]["bot_calls_delta"], 0)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertEqual(r["processed"], ["810"])
        self.assertEqual(r["store_begin"], ["810"])
        self.assertIsInstance(error.__cause__, GovernedError)
        self.assertNotIsInstance(error.__cause__, ConfirmedDeliveryPersistenceError)
        self.assert_unknown_hold(r)

    async def test_prequeued_message_dropped_after_unknown_halt_without_turn_or_notice(self):
        _, r, error = await self.run_native_gateway(prequeued=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual([x["accepted"] for x in r["admissions"]], [True, True])
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertEqual(r["processed"], ["810"])
        self.assertEqual(r["store_begin"], ["810"])
        self.assert_unknown_hold(r)

    async def test_halted_intake_does_not_prepare_commit_process_or_send(self):
        _, r, error = await self.run_native_gateway(halt_before="intake")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["admissions"][0]["accepted"], False)
        self.assertEqual(r["bot_calls"], [])
        self.assertEqual(r["processed"], [])
        self.assertEqual(r["store_begin"], [])
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["delivery_counts"], [0])

    async def test_halt_between_enqueue_and_consume_skips_both_queued_turns(self):
        _, r, error = await self.run_native_gateway(halt_before="consume", prequeued=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["processed"], [])
        self.assertEqual(r["store_begin"], [])
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["delivery_counts"], [0, 0])

    async def test_ceiling_halt_stops_gateway_with_known_selection_usage(self):
        _, r, error = await self.run_native_gateway(unknown=False, fault="ceiling", prequeued=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(r["requests"]), 2)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertEqual(len(r["snapshot"]["calls"]), 1)
        self.assertEqual(r["snapshot"]["reserved_usd"], "0")
        self.assertGreater(Decimal(r["snapshot"]["spent_usd"]), 0)
        self.assertEqual(r["snapshot"]["halted"], "session_cost_budget")

    async def test_snapshot_io_failure_closes_before_admission_preserves_primary(self):
        capture, r, error = await self.run_native_gateway(fault="snapshot_io")
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error.__cause__, capture["primary_io"])
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["bot_calls"], [])
        self.assertEqual(r["store_begin"], [])

    async def test_config_drift_closes_before_admission_without_provider(self):
        _, r, error = await self.run_native_gateway(fault="config")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["worker_error_code"], "session_policy_mismatch")
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["bot_calls"], [])

    async def test_failed_stop_sink_cannot_keep_intake_or_queue_open(self):
        _, r, error = await self.run_native_gateway(prequeued=True, fault="stop_sink")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertEqual(r["processed"], ["810"])
        self.assert_unknown_hold(r)

    async def test_failed_stop_logger_cannot_prevent_service_cleanup(self):
        _, r, error = await self.run_native_gateway(prequeued=True, fault="stop_logger")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertIn("OSError", r["loop_diagnostic_errors"])
        self.assert_unknown_hold(r)

    async def test_client_close_failure_still_releases_native_and_ledger(self):
        capture, r, error = await self.run_native_gateway(fault="client_close", prequeued=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error.__cause__, capture["bridge"]._worker_error)
        self.assertIsInstance(error.__cause__, GovernedError)
        self.assertIsInstance(capture["bridge"]._shutdown_error, OSError)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assert_unknown_hold(r)

    async def test_confirmed_first_notice_audit_failure_keeps_f1_priority(self):
        capture, r, error = await self.run_native_gateway(fault="confirmed_notice_audit", prequeued=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, ConfirmedDeliveryPersistenceError)
        self.assertEqual(error.__cause__.receipt_ids, ("1810",))
        self.assertEqual(capture["runtime"]._post_delivery_failure, ("810", ("1810",)))
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assert_unknown_hold(r)

    async def test_generation_cancel_joins_then_preserves_unknown_hold_no_notice(self):
        _, r, error = await self.run_native_gateway(cancel_at="generation", prequeued=True)
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(r["delivery_counts"], [0, 0])
        self.assertEqual(r["processed"], ["810"])
        self.assert_unknown_hold(r)

    async def test_notice_cancel_sends_at_most_once_and_keeps_unknown_hold(self):
        _, r, error = await self.run_native_gateway(cancel_at="notice", prequeued=True)
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assert_unknown_hold(r)

    async def test_new_message_while_original_notice_waits_stops_without_f1_reclassification(self):
        capture, r, error = await self.run_native_gateway(late=True, late_bot=True,
            fault="notice_halt_race")
        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, GovernedError)
        self.assertNotIsInstance(error.__cause__, ConfirmedDeliveryPersistenceError)
        self.assertFalse(capture["late_accepted"])
        self.assertEqual(r["admissions"][1]["bot_calls_delta"], 0)
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assertEqual(r["processed"], ["810"])
        self.assertIsNone(capture["runtime"]._post_delivery_failure)
        self.assert_unknown_hold(r)

    async def test_missing_controlled_stop_callback_fails_closed(self):
        _, r, error = await self.run_native_gateway(fault="callback_missing")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(r["worker_error_code"], "session_lifecycle_required")
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["bot_calls"], [])

    async def test_invalid_controlled_stop_result_fails_closed(self):
        _, r, error = await self.run_native_gateway(fault="callback_invalid")
        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, TypeError)
        self.assertEqual(r["requests"], [])
        self.assertEqual(r["bot_calls"], [])

    async def test_prior_unknown_halt_beats_later_config_drift_and_marks_aggregate_unknown(self):
        capture, r, error = await self.run_native_gateway(late=True, late_bot=True,
            fault="unknown_config_drift")
        self.assertIsInstance(error, RuntimeError)
        self.assertFalse(capture["late_accepted"])
        self.assertEqual(r["worker_error_code"], "session_halted")
        self.assertTrue(r["worker_error_remote_usage_unknown"])
        self.assertTrue(capture["runtime"].session_usage_unknown)
        self.assertTrue(r["stop_events"])
        self.assertTrue(all(event["session_usage_unknown"] is True for event in r["stop_events"]))
        self.assertTrue(all(event["stop_error_remote_usage_unknown"] is True for event in r["stop_events"]))
        self.assertEqual(r["delivery_counts"], [1, 0])
        self.assert_unknown_hold(r)

    async def test_snapshot_io_stop_marks_aggregate_unassessed_and_keeps_original_error(self):
        capture, r, error = await self.run_native_gateway(fault="snapshot_io")
        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error.__cause__, capture["primary_io"])
        self.assertIsNone(capture["runtime"].session_usage_unknown)
        self.assertTrue(r["stop_events"])
        self.assertTrue(all(event["session_usage_unknown"] is None for event in r["stop_events"]))
        self.assertTrue(all(event["stop_error_remote_usage_unknown"] is False for event in r["stop_events"]))
        self.assertEqual(r["bot_calls"], [])
        self.assertEqual(r["requests"], [])

    async def test_session_disabled_two_turns_keep_ordinary_native_route(self):
        _, r, error = await self.run_native_gateway(unknown=False, ordinary=True)
        self.assertIsNone(error)
        self.assertEqual(r["processed"], ["810", "811"])
        self.assertEqual(r["delivery_counts"], [1, 1])
        self.assertEqual(len(r["requests"]), 8)
        self.assertEqual(len(r["ranks"]), 2)
        self.assertIsNone(r["snapshot"])

    async def test_direct_reingest_run_initialize_false_is_rejected_before_work(self):
        budgets = dict(self.config.adapter.budgets)
        budgets["label"] = Budget(4096, 512, 5, reasoning="medium")
        config = replace(self.config, adapter=replace(self.config.adapter, budgets=budgets))
        adapter = SimpleNamespace(call=AsyncMock(side_effect=AssertionError("raw paid call forbidden")))
        with sqlite3.connect((config.state_dir / "memory.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            before = list(db.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))
            instance = reingest.BoundedReingest(config, db, self.graph, adapter,
                self.scratch, initialize=False)
            work = []
            db.set_trace_callback(work.append)
            with self.assertRaises(GovernedError) as rejected:
                await instance.run("synthetic-unused-batch")
            self.assertEqual(rejected.exception.code, "session_maintenance_forbidden")
            self.assertEqual(work, [])
            db.set_trace_callback(None)
            self.assertEqual([tuple(row) for row in db.execute("SELECT name, sql FROM sqlite_master ORDER BY name")],
                [tuple(row) for row in before])
        db.close()
        adapter.call.assert_not_awaited()
