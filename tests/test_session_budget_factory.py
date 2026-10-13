"""Independent controlled Console factory/lifetime gates, entirely synthetic.

Real Console.serve, Store, KnowledgeService, Runtime, DiscordBridge, native
OpenAIAdapter and frozen numerical ranker run. Only provider/Gateway/observer
entry points and the pinned loader are replaced. Tariffs are synthetic upper
assumptions; this suite makes no paid, live Discord or relevance claim.
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

from indeces import console, console_session, ingest, reingest
from indeces.adapter import OpenAIAdapter
from indeces.config import Config, PdfConfig, SessionBudgetConfig
from indeces.contracts import GovernedError, IncomingMessage
from indeces.discord_bridge import DiscordBridge
from indeces.knowledge import KnowledgeService
from indeces.lock import InstanceLock
from indeces.richard_v11_ranker import FrozenRanker
from indeces.session_budget import SessionBudgetAdapter, SessionBudgetLedger
from indeces.store import Store
from tests import test_richard_chat_runtime as authored
from tests.test_discord_bridge import HTTPError, Message
from tests.test_run_records import RecordFixture


OBSERVATIONS = []


class SessionBudgetFactoryTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        authored.RichardChatRuntimeTests.setUp(self)
        old = self.config
        self.config = Config(old.name, self.root / "state", self.root / "scratch",
            old.knowledge_dir, old.discord,
            replace(old.runtime, model_selection_enabled=True,
                richard_selection_enabled=False, richard_chat_enabled=True,
                richard_graph_zip=str(self.root / "graph.zip"),
                richard_source_zip=str(self.root / "source.zip"), turn_seconds=30),
            old.adapter, old.knowledge, PdfConfig(),
            session_budget=SessionBudgetConfig(enabled=True, allow_real_calls=True,
                session_id="independent-synthetic-controlled-session",
                ceiling_usd="5.00", model=old.adapter.model,
                base_url=old.adapter.base_url, input_usd_per_million="0.1",
                output_usd_per_million="0.2", per_call_usd="0.01",
                reference="synthetic-upper-tariffs-not-provider-certification",
                valid_until_utc="2099-01-01T00:00:00+00:00",
                count_fee_verified=True,
                count_fee_reference="synthetic-fake-count-cost-included-in-per-call-upper"))
        # serve must create and own its actual Store, not inherit the fixture's
        # connection. Native records are committed and remain in this C fixture.
        self.store.close()

    def tearDown(self):
        authored.RichardChatRuntimeTests.tearDown(self)

    def seed_bound_record(self, *args, **kwargs):
        return authored.RichardChatRuntimeTests.seed_bound_record(self, *args, **kwargs)

    def assert_leases_released(self):
        lease = InstanceLock(self.config.state_dir)
        lease.close()
        budget = self.config.session_budget
        from indeces.session_budget import SessionPriceEnvelope
        envelope = SessionPriceEnvelope(**{key: getattr(budget, key) for key in
            ("model", "base_url", "input_usd_per_million", "output_usd_per_million",
             "per_call_usd", "reference", "valid_until_utc")})
        ledger = SessionBudgetLedger(self.config.state_dir / "controlled-session-budget.sqlite3",
            session_id=budget.session_id, envelope=envelope, ceiling_usd=budget.ceiling_usd)
        snapshot = ledger.snapshot()
        ledger.close()
        return snapshot

    async def run_serve(self, *, mode="dual", turns=1, duplicate=False,
                        unknown_usage=False, fail=None, block_gateway=False,
                        cancel=False, exercise_summary=False):
        requests, ranks, lifecycle, capture, deliveries = [], [], [], {}, []
        entered = asyncio.Event()
        self.turn_counter += 1
        start_id = 800 + self.turn_counter * 10
        self.config = replace(self.config, runtime=replace(self.config.runtime,
            richard_chat_algorithm="beta" if mode == "single" else "alpha"))
        query = authored.SINGLE_QUERY if mode == "single" else authored.DUAL_QUERY
        plan = authored.grounded_plan(query, mode=mode)
        native_class, ledger_class, knowledge_class = OpenAIAdapter, SessionBudgetLedger, KnowledgeService
        knowledge_start_actual = knowledge_class.start
        knowledge_retry_actual = knowledge_class.retry_failed
        bridge_class, create_actual = DiscordBridge, console.create_runtime
        rank_actual = FrozenRanker.rank

        async def transport(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            self.assertIn(stage, {"selection", "reply"})
            requests.append({"path": path, "stage": stage, "payload": deepcopy(payload)})
            self.assertFalse(capture["store"].db.in_transaction)
            if path == "/responses/input_tokens":
                return {"input_tokens": 43 if stage == "selection" else 47}
            self.assertEqual(path, "/responses")
            if stage == "selection":
                model_input = json.loads(payload["input"][0]["content"])
                ids = [row["record_id"] for row in model_input["candidates"]
                       [:model_input["required_selection_count"]]]
                response = json.dumps({"selected_record_ids": ids, "richard_plan": plan})
            else:
                response = "Controlled synthetic evidence [M1]."
            return authored.completion(response, stage,
                unknown_usage=unknown_usage and stage == "selection")

        def native_factory(config, scratch, key):
            native = native_class(config, scratch, key, request=transport)
            fake_client = SimpleNamespace(close=AsyncMock())
            native._session = fake_client
            capture.update(native=native, client=fake_client, scratch=scratch)
            actual_close = native.close
            async def close():
                lifecycle.append("native")
                await actual_close()
                if fail == "native_close":
                    raise OSError("synthetic native close failure")
            native.close = close
            return native

        def ledger_factory(*args, **kwargs):
            ledger = ledger_class(*args, **kwargs)
            capture["ledger"] = ledger
            actual_close = ledger.close
            def close():
                lifecycle.append("ledger")
                actual_close()
            ledger.close = close
            return ledger

        def runtime_factory(config, store, adapter, scratch, **kwargs):
            capture.update(store=store, wrapper=adapter)
            self.assertIs(type(adapter), SessionBudgetAdapter)
            self.assertIs(adapter.base, capture["native"])
            self.assertIs(adapter.ledger, capture["ledger"])
            if fail == "runtime":
                raise OSError("synthetic runtime construction failure")
            runtime = create_actual(config, store, adapter, scratch, **kwargs)
            capture["runtime"] = runtime
            self.assertIs(runtime.adapter, adapter)
            self.assertIs(runtime.model_selector.adapter, adapter)
            self.assertIs(runtime.model_selector.ranker.graph, self.richard_graph)
            return runtime

        def knowledge_factory(config, store, graph, adapter, scratch):
            self.assertIs(adapter, capture["wrapper"])
            if fail == "knowledge":
                raise OSError("synthetic knowledge construction failure")
            knowledge = knowledge_class(config, store, graph, adapter, scratch)
            capture["knowledge"] = knowledge
            self.assertIs(knowledge.adapter, capture["runtime"].adapter)
            self.assertEqual(knowledge._tasks, [])
            actual_close = knowledge.close
            async def close():
                lifecycle.append("knowledge")
                await actual_close()
                if fail in {"knowledge_close", "gateway_close"}:
                    raise OSError("synthetic secondary close failure")
            knowledge.close = close
            return knowledge

        async def gateway(bridge, token):
            self.assertEqual(token, "synthetic-offline-token")
            self.assertIs(bridge.runtime, capture["runtime"])
            self.assertEqual(capture["knowledge"]._tasks, [])
            for maintenance in (knowledge_start_actual, knowledge_retry_actual):
                with self.assertRaises(GovernedError) as closed:
                    maintenance(capture["knowledge"])
                self.assertEqual(closed.exception.code, "session_maintenance_forbidden")
            self.assertFalse(any(task.get_name() in {"indeces-knowledge-watch",
                "indeces-passive-label", "indeces-pdf-convert"} for task in asyncio.all_tasks()))
            # Both lifetime leases must remain owned while transport can run.
            with self.assertRaises(RuntimeError):
                InstanceLock(self.config.state_dir)
            with self.assertRaises(GovernedError) as busy:
                ledger_class(capture["ledger"].path,
                    session_id=self.config.session_budget.session_id,
                    envelope=capture["ledger"].envelope,
                    ceiling_usd=self.config.session_budget.ceiling_usd)
            self.assertEqual(busy.exception.code, "requery_ledger_writer_busy")
            entered.set()
            if block_gateway:
                await asyncio.Event().wait()
            if fail in {"gateway", "gateway_close"}:
                raise ValueError("synthetic primary gateway failure")
            if exercise_summary:
                before = len(requests)
                guarded = capture["wrapper"].begin_turn("synthetic-summary-turn")
                with self.assertRaises(GovernedError) as rejected:
                    message = IncomingMessage("summary-fixture", "20", "10", "30",
                        "Synthetic Human", query, "2026-10-11T06:00:00+00:00")
                    # Force one existing contiguous interaction through the
                    # actual Runtime summary method, without a large fixture.
                    store = capture["store"]
                    with store.db:
                        store.db.execute("INSERT INTO messages(scope,turn_id,role,author_id,content,created_at) VALUES(?,?,?,?,?,?)",
                            (message.scope, "previous", "user", "30", "synthetic previous observation", message.created_at))
                    rows = store.history(message.scope)
                    with patch("indeces.runtime.compaction_prefix", return_value=rows):
                        await capture["runtime"]._summary(message, [], "synthetic-trace",
                            adapter_override=guarded)
                self.assertEqual(rejected.exception.code, "session_stage_not_allowed")
                self.assertEqual(len(requests), before)
            else:
                bridge._discord = SimpleNamespace(HTTPException=HTTPError,
                    AllowedMentions=SimpleNamespace(none=lambda: object()))
                bridge._allowed_mentions = object()
                bridge._start_worker()
                for index in range(turns):
                    message = Message(start_id + index, text="<@99> " + query)
                    self.assertTrue(bridge.enqueue(message, "99"))
                    await asyncio.wait_for(bridge._queue.join(), 5)
                    deliveries.append(deepcopy(message.sent))
                    if duplicate:
                        previous = len(requests), len(ranks), len(message.sent)
                        self.assertFalse(bridge.enqueue(message, "99"))
                        self.assertEqual((len(requests), len(ranks), len(message.sent)), previous)
            capture["snapshot"] = capture["ledger"].snapshot()

        def bridge_factory(*args, **kwargs):
            bridge = bridge_class(*args, **kwargs)
            capture["bridge"] = bridge
            bridge.run = lambda token: gateway(bridge, token)
            return bridge

        def rank(ranker, *args, **kwargs):
            result = rank_actual(ranker, *args, **kwargs)
            ranks.append({"args": deepcopy(args), "kwargs": deepcopy(kwargs),
                "results": [{"cid": row.cid, "score": row.score} for row in result]})
            return result

        async def inert_retention(scratch):
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append("retention_joined")

        observer = SimpleNamespace(start=Mock(return_value="synthetic observer disabled"), close=Mock())
        error = None
        with ExitStack() as stack:
            stack.enter_context(patch.object(console, "create_model_adapter", side_effect=native_factory))
            stack.enter_context(patch.object(console, "SessionBudgetLedger", side_effect=ledger_factory))
            stack.enter_context(patch.object(console, "create_runtime", side_effect=runtime_factory))
            stack.enter_context(patch.object(console, "KnowledgeService", side_effect=knowledge_factory))
            stack.enter_context(patch.object(console, "DiscordBridge", side_effect=bridge_factory))
            stack.enter_context(patch.object(console, "ObserverServer", return_value=observer))
            stack.enter_context(patch.object(console, "_maintain_scratch", side_effect=inert_retention))
            stack.enter_context(patch.object(knowledge_class, "start",
                side_effect=AssertionError("controlled trial must not start knowledge workers")))
            for name in ("scan_once", "convert_next", "label_next", "retry_failed"):
                stack.enter_context(patch.object(knowledge_class, name,
                    side_effect=AssertionError("controlled trial maintenance forbidden")))
            stack.enter_context(patch("indeces.richard_v11_data.load_release", return_value=self.richard_graph))
            stack.enter_context(patch.object(FrozenRanker, "rank", autospec=True, side_effect=rank))
            stack.enter_context(redirect_stdout(io.StringIO()))
            task = asyncio.create_task(console.serve(self.config, "synthetic-offline-key",
                "synthetic-offline-token"))
            if cancel:
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
            try:
                await asyncio.wait_for(task, 8)
            except BaseException as caught:
                error = caught
        self.assertEqual(self.network_attempts, [])
        if "native" in capture:
            capture["client"].close.assert_awaited_once()
            self.assertIsNone(capture["native"]._session)
            self.assertTrue(capture["scratch"].stream.closed)
        if "store" in capture:
            with self.assertRaises(sqlite3.ProgrammingError):
                capture["store"].db.execute("SELECT 1")
        if "bridge" in capture:
            self.assertFalse(capture["bridge"]._accepting)
            self.assertTrue(capture["bridge"]._worker is None or capture["bridge"]._worker.done())
        if "knowledge" in capture:
            self.assertTrue(capture["knowledge"]._closing)
            self.assertEqual(capture["knowledge"]._tasks, [])
            self.assertEqual(capture["knowledge"]._filesystem_tasks, set())
            self.assertLess(lifecycle.index("knowledge"), lifecycle.index("native"))
        if "ledger" in capture:
            self.assertTrue(capture["ledger"]._closed)
            if "native" in capture:
                self.assertLess(lifecycle.index("native"), lifecycle.index("ledger"))
        reopened_snapshot = self.assert_leases_released()
        record = {"case": self._testMethodName, "requests": requests, "rank_calls": ranks,
            "deliveries": deliveries, "lifecycle": lifecycle,
            "snapshot": capture.get("snapshot", reopened_snapshot),
            "error": type(error).__name__ if error else None,
            "error_code": getattr(error, "code", None),
            "shared_wrapper": "knowledge" in capture,
            "native_client_closed": "native" in capture}
        OBSERVATIONS.append(record)
        return capture, record, error

    async def test_actual_serve_shared_wrapper_dual_alpha_two_turns_and_duplicate(self):
        capture, record, error = await self.run_serve(turns=2, duplicate=True)
        self.assertIsNone(error)
        self.assertEqual(len(record["requests"]), 8)
        self.assertEqual(len(record["rank_calls"]), 2)
        self.assertTrue(all(len(rows) == 1 for rows in record["deliveries"]))
        self.assertTrue(all(call["kwargs"]["variant"] == "alpha_meet" for call in record["rank_calls"]))
        state = record["snapshot"]
        self.assertEqual(len(state["calls"]), 4)
        self.assertEqual(len(state["turns"]), 2)
        self.assertIsNone(state["halted"])
        self.assertTrue(all(row["status"] == "completed" for row in state["calls"]))
        self.assertGreater(Decimal(state["spent_usd"]), 0)
        self.assertLessEqual(Decimal(state["liability_usd"]), Decimal("5"))
        self.assertEqual(state["automatic_retries"], 0)
        reopened = self.assert_leases_released()
        self.assertEqual(reopened, state)

    async def test_actual_serve_single_beta_invokes_real_ranker_and_one_reply(self):
        _, record, error = await self.run_serve(mode="single")
        self.assertIsNone(error)
        self.assertEqual(len(record["rank_calls"]), 1)
        self.assertEqual(record["rank_calls"][0]["kwargs"]["variant"], "beta_nomeet")
        self.assertEqual(len(record["rank_calls"][0]["results"]), 10)
        self.assertEqual(len(record["deliveries"][0]), 1)
        self.assertEqual([row["stage"] for row in record["snapshot"]["calls"]], ["selection", "reply"])

    async def test_unknown_selection_usage_halts_reopened_same_session_without_new_http(self):
        _, first, error = await self.run_serve(unknown_usage=True)
        self.assertIsNone(error)
        self.assertEqual(len(first["requests"]), 2)
        self.assertEqual(first["rank_calls"], [])
        self.assertEqual(first["snapshot"]["halted"], "session_usage_unknown")
        _, second, error = await self.run_serve()
        self.assertIsInstance(error, GovernedError)
        self.assertEqual(error.code, "session_halted")
        self.assertEqual(second["requests"], [])
        self.assertEqual(second["rank_calls"], [])
        self.assertEqual(second["lifecycle"], ["ledger"])
        self.assertFalse(second["native_client_closed"])
        self.assertFalse(second["shared_wrapper"])
        self.assertEqual(second["snapshot"]["calls"], first["snapshot"]["calls"])
        self.assertEqual(second["snapshot"]["turns"], first["snapshot"]["turns"])

    async def test_controlled_summary_route_refuses_before_transport(self):
        _, record, error = await self.run_serve(exercise_summary=True)
        self.assertIsNone(error)
        self.assertEqual(record["requests"], [])
        self.assertEqual(record["snapshot"]["halted"], "session_stage_not_allowed")
        self.assertEqual(record["snapshot"]["calls"], [])

    async def test_partial_boot_failures_close_native_ledger_and_owned_instance(self):
        for failure in ("runtime", "knowledge"):
            with self.subTest(failure=failure):
                _, record, error = await self.run_serve(fail=failure)
                self.assertIsInstance(error, OSError)
                self.assertEqual(record["requests"], [])
                self.assertEqual(record["lifecycle"], ["native", "ledger"])

    async def test_gateway_primary_error_survives_secondary_cleanup_failure(self):
        # Separate both faults without replacing the actual cleanup resources.
        _, record, error = await self.run_serve(fail="gateway_close")
        self.assertIsInstance(error, ValueError)
        self.assertEqual(record["requests"], [])
        self.assertIn("knowledge", record["lifecycle"])

    async def test_knowledge_close_error_still_closes_native_and_ledger(self):
        _, record, error = await self.run_serve(fail="knowledge_close")
        self.assertIsInstance(error, OSError)
        self.assertEqual(record["lifecycle"][-3:], ["knowledge", "native", "ledger"])

    async def test_native_close_failure_still_releases_ledger_and_instance(self):
        _, record, error = await self.run_serve(fail="native_close")
        self.assertIsInstance(error, OSError)
        self.assertEqual(record["lifecycle"][-3:], ["knowledge", "native", "ledger"])

    async def test_cancelled_serve_joins_workers_and_releases_both_lifetimes(self):
        _, record, error = await self.run_serve(block_gateway=True, cancel=True)
        self.assertIsInstance(error, asyncio.CancelledError)
        self.assertEqual(record["requests"], [])
        self.assertIn("retention_joined", record["lifecycle"])

    async def test_permission_missing_expired_mismatch_price_reject_before_native_or_http(self):
        changes = ({"allow_real_calls": False}, {"reference": ""},
            {"input_usd_per_million": ""}, {"valid_until_utc": "2000-01-01T00:00:00+00:00"},
            {"model": "synthetic-wrong-model"}, {"base_url": "https://synthetic.invalid/v1"},
            {"count_fee_verified": False}, {"count_fee_reference": ""}, {"ceiling_usd": "5.01"},
            # Exact fractional nanoUSD must fail before lease/ledger/client.
            # Default Decimal context would round the first value to 5 USD.
            {"ceiling_usd": "4.999999999999999999999999999999999999"},
            {"ceiling_usd": "0.0000000001"},
            {"ceiling_usd": "0." + "0" * 63 + "1"})
        for change in changes:
            with self.subTest(change=change), \
                    patch.object(console, "InstanceLock", side_effect=AssertionError("no lease before approval")) as lease, \
                    patch.object(console, "create_model_adapter", side_effect=AssertionError("no native adapter before approval")) as native, \
                    patch.object(console, "Store", side_effect=AssertionError("no Store before approval")) as store:
                config = replace(self.config, session_budget=replace(self.config.session_budget, **change))
                with self.assertRaises((ValueError, GovernedError)):
                    await console.serve(config, "synthetic-offline-key", "synthetic-offline-token")
                lease.assert_not_called()
                native.assert_not_called()
                store.assert_not_called()

    async def test_default_off_is_compatible_and_controlled_status_stays_read_only(self):
        defaults = SessionBudgetConfig()
        self.assertFalse(defaults.enabled)
        self.assertFalse(defaults.allow_real_calls)
        self.assertFalse(defaults.count_fee_verified)
        ordinary = replace(self.config, session_budget=defaults)
        self.assertFalse(console.controlled_session_enabled(ordinary))
        self.assertIsNone(console.checked_session_envelope(ordinary))
        self.assertIsNone(console.guard_trial_maintenance(ordinary))
        before = (self.config.state_dir / "memory.sqlite3").read_bytes()
        with patch.object(console, "InstanceLock", side_effect=AssertionError("status cannot own service")) as lease, \
                patch.object(console, "create_model_adapter", side_effect=AssertionError("status cannot call provider")) as native, \
                patch.object(console, "SessionBudgetLedger", side_effect=AssertionError("status cannot write ledger")) as ledger, \
                redirect_stdout(io.StringIO()) as output:
            # A price/permission refusal is a launch gate, not a read-only status
            # gate. status reads its synthetic SQLite through mode=ro.
            config = replace(self.config, session_budget=replace(self.config.session_budget,
                allow_real_calls=False, count_fee_verified=False))
            console.status(config)
        for sentinel in (lease, native, ledger):
            sentinel.assert_not_called()
        self.assertEqual((self.config.state_dir / "memory.sqlite3").read_bytes(), before)
        self.assertIn("CLOSED for controlled session", output.getvalue())

    async def test_retry_ingestion_rejects_before_credentials_lease_or_database(self):
        with patch.object(ingest, "InstanceLock", side_effect=AssertionError("lease mutation forbidden")) as lease, \
                patch("indeces.model_provider.load_openai_key", side_effect=AssertionError("credential read forbidden")) as key, \
                patch.object(ingest, "Store", side_effect=AssertionError("Store mutation forbidden")) as store, \
                patch.object(ingest, "create_model_adapter", side_effect=AssertionError("raw adapter forbidden")) as raw:
            with self.assertRaises((ValueError, GovernedError)):
                await ingest.retry_ingestion(self.config, self.root / "not-read.toml")
        for sentinel in (lease, key, store, raw):
            sentinel.assert_not_called()

    async def test_reingest_rejects_before_lease_credentials_database_and_backup(self):
        with patch.object(reingest, "load_config", return_value=self.config), \
                patch.object(reingest, "InstanceLock", side_effect=AssertionError("lease mutation forbidden")) as lease, \
                patch.object(reingest, "load_openai_key", side_effect=AssertionError("credential read forbidden")) as key, \
                patch.object(reingest.sqlite3, "connect", side_effect=AssertionError("database mutation forbidden")) as db, \
                patch.object(reingest, "backup_database", side_effect=AssertionError("backup mutation forbidden")) as backup:
            with self.assertRaises((ValueError, GovernedError)):
                await reingest.maintenance(self.root / "not-read.toml")
        for sentinel in (lease, key, db, backup):
            sentinel.assert_not_called()
        database = Mock()
        with self.assertRaises((ValueError, GovernedError)):
            reingest.BoundedReingest(self.config, database, Mock(), Mock(), Mock())
        self.assertEqual(database.mock_calls, [])

    async def test_console_retry_active_and_stopped_reject_before_service_or_raw_setup(self):
        for active in (False, True):
            session = object.__new__(console_session.ConsoleSession)
            service = SimpleNamespace(active=True, kind="discord", retry=Mock()) if active else None
            session.service, session.path = service, self.root / "not-read.toml"
            session._retries = []
            with self.subTest(active=active), \
                    patch.object(console, "InstanceLock", side_effect=AssertionError("lease mutation forbidden")) as lease, \
                    patch("indeces.model_provider.load_openai_key", side_effect=AssertionError("credential read forbidden")) as key, \
                    patch.object(console, "_check_start_config", side_effect=AssertionError("config read forbidden")) as check:
                with self.assertRaises((ValueError, GovernedError)):
                    session._retry(self.config, None)
                for sentinel in (lease, key, check):
                    sentinel.assert_not_called()
                if service:
                    service.retry.assert_not_called()
                self.assertEqual(session._retries, [])


if __name__ == "__main__":
    unittest.main()
