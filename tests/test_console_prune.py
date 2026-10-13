"""Console prune wiring on synthetic graph, receipt, ledger and native wire.

The production live guard has no fixture switch. These tests replace its
constructor at the test boundary with the same real loop in offline mode.
No real graph, grant, key, remote response or production directory is read.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing, redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import aiohttp

from indeces import console, console_instance, console_session, console_prune
from indeces import claude_prune_loop as loop_module
from indeces.claude_prune_budget import PruneBudget
from indeces.claude_prune_loop import PruneToolLoop as RealPruneToolLoop
from indeces.config import Budget
from indeces.contracts import GovernedError
from indeces.model_provider import create_model_adapter as real_create_adapter

try:
    from test_portable_claude_prune import (
        CHARGE, PRICE, SyntheticGraphFixture, ScriptedTransport,
        canonical, config as adapter_config, legacy_fixture, native_message, tool,
    )
except ModuleNotFoundError:
    from tests.test_portable_claude_prune import (
        CHARGE, PRICE, SyntheticGraphFixture, ScriptedTransport,
        canonical, config as adapter_config, legacy_fixture, native_message, tool,
    )


class ConsolePruneFixture(SyntheticGraphFixture):
    def fixture(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="console-prune-synthetic-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.path = self.directory / "fixture.toml"
        self.path.write_text("# synthetic; loader is patched by Console tests\n", encoding="utf-8")
        self.config = SimpleNamespace(adapter=adapter_config(), scratch_dir=self.directory / "scratch",
                                      state_dir=self.directory / "state")
        self.binding = legacy_fixture(self.directory)
        self.make_graph()
        binding = asdict(self.binding)
        for field in ("ledger_path", "receipt_path"):
            binding[field] = str(binding[field])
        self.packet = {"graph_dir": "graph", "evidence_dir": "sources",
                       "binding": binding, "price": asdict(PRICE)}
        self.sidecar = self.path.with_name(self.path.stem + ".prune.local.json")
        self.write_packet()
        self.created_adapters = []
        self.sessions = []
        self.live_flags = []

    def write_packet(self):
        self.sidecar.write_text(canonical(self.packet), encoding="utf-8")

    def prepare(self, values=None, config=None):
        values = iter(values if values is not None else ["Synthetic question", "1", "2", "coarse", "target"])
        with redirect_stdout(io.StringIO()):
            return console_prune.prepare_prune(config or self.config, self.path,
                                                prompt=lambda _: next(values))

    @staticmethod
    def messages():
        blocks = [
            [{"type": "thinking", "thinking": "private-synthetic-thought", "signature": "synthetic-signature"},
             tool("view", "toolu_view")],
            [tool("prune", "toolu_prune", removals=[{"node_id": 3, "reason": "synthetic", "kind": "logic"}])],
            [tool("answer", "toolu_answer", choice="none")],
            [{"type": "text", "text": "DONE"}],
        ]
        return [native_message(block, reason="end_turn" if index == 3 else "tool_use", ident=f"msg_console_{index}")
                for index, block in enumerate(blocks)]

    def transport_context(self, transport):
        stack = ExitStack()
        def create(config, scratch, key):
            self.assertEqual(key, "synthetic-not-a-credential")
            adapter = real_create_adapter(config, scratch, key)
            adapter._request_override = transport
            self.created_adapters.append(adapter)
            return adapter
        def loop(adapter, session, budget, *args, **kwargs):
            self.live_flags.append(kwargs.pop("live", False))
            self.sessions.append(session)
            return RealPruneToolLoop(adapter, session, budget, *args, live=False, **kwargs)
        stack.enter_context(patch.object(console_prune, "resolve_model_key", return_value="synthetic-not-a-credential"))
        stack.enter_context(patch.object(console_prune, "create_model_adapter", side_effect=create))
        stack.enter_context(patch.object(loop_module, "PruneToolLoop", side_effect=loop))
        stack.enter_context(patch.object(aiohttp.ClientSession, "post", side_effect=AssertionError("unexpected HTTP")))
        return stack

    def snapshot(self):
        with closing(sqlite3.connect(self.binding.ledger_path)) as db:
            spent = db.execute("SELECT spent FROM session").fetchone()[0]
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rounds = db.execute("SELECT count(*) FROM prune_rounds").fetchone()[0] if "prune_rounds" in names else 0
            return spent, rounds

    def wait(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("finite synthetic Console task did not finish")
            time.sleep(.01)


class PrepareConsolePruneTests(ConsolePruneFixture, unittest.TestCase):
    def setUp(self):
        self.fixture()

    def test_prepare_numeric_original_ids_keeps_exact_graph_and_existing_binding(self):
        result = self.prepare()
        self.assertEqual((result.question, result.seeds_A, result.seeds_B, result.level, result.target),
                         ("Synthetic question", (1,), (2,), "coarse", "target"))
        self.assertEqual(result.binding, self.binding)
        self.assertEqual(result.price, PRICE)
        self.assertEqual(set(result.graph.nodes), {1, 2, 3})
        self.assertEqual(self.snapshot(), (CHARGE, 0))

    def test_prepare_exact_names_and_optional_b_have_visible_pool_summary(self):
        output = io.StringIO()
        values = iter(["Synthetic question", "synthetic-1", "", "fine", "target"])
        result = console_prune.prepare_prune(self.config, self.path, prompt=lambda _: next(values), output=output.write)
        self.assertEqual((result.seeds_A, result.seeds_B, result.level), ((1,), (), "fine"))
        self.assertTrue(result.seed_summary)
        self.assertTrue(result.pool_summary)
        self.assertIn("synthetic", output.getvalue())

    def test_provider_model_and_effort_decline_before_prompt_or_key(self):
        for field, value in (("provider", "openai"), ("model", "claude-other"),
                             ("base_url", "https://example.invalid"), ("budgets", {"reply": Budget(4096, 64, 2, "medium")})):
            with self.subTest(field=field), patch.object(console_prune, "resolve_model_key") as key:
                adapter = deepcopy(self.config.adapter)
                setattr(adapter, field, value)
                prompt = Mock(side_effect=AssertionError("unsupported configuration prompted"))
                with self.assertRaises(GovernedError):
                    console_prune.prepare_prune(replace_namespace(self.config, adapter=adapter), self.path, prompt=prompt)
                key.assert_not_called()
                prompt.assert_not_called()

    def test_missing_sidecar_declines_without_creating_ledger_or_resolving_key(self):
        self.sidecar.unlink()
        with patch.object(console_prune, "resolve_model_key") as key, self.assertRaises(GovernedError):
            self.prepare()
        key.assert_not_called()
        self.assertEqual(self.snapshot(), (CHARGE, 0))

    def test_malformed_duplicate_and_extra_sidecar_fields_are_closed(self):
        for raw in ("{", '{"graph_dir":"graph","graph_dir":"graph"}', canonical({**self.packet, "unexpected": True})):
            with self.subTest(raw=raw[:20]):
                self.sidecar.write_text(raw, encoding="utf-8")
                with self.assertRaises(GovernedError):
                    self.prepare()
                self.assertEqual(self.snapshot(), (CHARGE, 0))

    def test_expired_or_zero_price_and_wrong_grant_reject_before_model_factory(self):
        for section, field, value in (("price", "valid_until", "2000-01-01T00:00:00+00:00"),
                                      ("price", "input_nusd_per_token", 0), ("binding", "grant_id", "wrong")):
            packet = deepcopy(self.packet)
            packet[section][field] = value
            self.sidecar.write_text(canonical(packet), encoding="utf-8")
            with self.subTest(field=field), patch.object(console_prune, "create_model_adapter") as factory:
                with self.assertRaises(GovernedError):
                    self.prepare()
                factory.assert_not_called()
                self.assertEqual(self.snapshot(), (CHARGE, 0))

    def test_unknown_seed_invalid_target_and_level_reject_locally(self):
        for values in (["Q", "999", "", "coarse", "target"], ["Q", "unknown-name", "", "coarse", "target"],
                       ["Q", "1", "2", "invalid", "target"], ["Q", "1", "2", "fine", "unknown-target"]):
            with self.subTest(values=values), patch.object(console_prune, "resolve_model_key") as key:
                with self.assertRaises(GovernedError):
                    self.prepare(values)
                key.assert_not_called()

    def test_ambiguous_exact_name_and_empty_required_seed_decline(self):
        self.graph.nodes[2]["name"] = "synthetic-1"
        with patch("indeces._vendor.npmi_prune.Graph", return_value=self.graph):
            with self.assertRaises(GovernedError):
                self.prepare(["Q", "synthetic-1", "", "coarse", "target"])
        with self.assertRaises(GovernedError):
            self.prepare(["Q", "", "", "coarse", "target"])


def replace_namespace(value, **fields):
    return SimpleNamespace(**{**vars(value), **fields})


class ExecuteConsolePruneTests(ConsolePruneFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()

    async def test_execute_real_native_loop_closes_clients_and_keeps_nonzero_existing_grant(self):
        prepared = self.prepare()
        transport = ScriptedTransport(self.messages())
        with self.transport_context(transport):
            result = await console_prune.execute_prune(prepared, self.config)
        self.assertEqual(result.text, "DONE")
        self.assertEqual(result.tool_actions, ("view", "prune", "answer"))
        self.assertEqual((result.rounds, result.input_tokens, result.output_tokens), (4, 40, 32))
        self.assertEqual((result.spent_nusd, result.request_cost_nusd, result.pending_nusd), (5 * CHARGE, 4 * CHARGE, 0))
        self.assertEqual(result.answer_choice, "none")
        self.assertEqual(self.live_flags, [True])
        self.assertTrue(all(adapter._session is None for adapter in self.created_adapters))
        self.assertEqual(self.snapshot(), (5 * CHARGE, 4))
        self.assertEqual([row["action"] for row in self.sessions[0].log], ["start", "view", "prune", "answer"])
        with closing(sqlite3.connect(self.binding.ledger_path)) as db:
            self.assertEqual(db.execute("SELECT policy FROM session").fetchone(), (self.binding.policy_json,))
            self.assertEqual(db.execute("SELECT * FROM attempt").fetchall(), [self.binding.completed_attempt])

    async def test_second_explicit_entry_continues_same_balance_without_reset(self):
        for index in range(2):
            prepared = self.prepare()
            transport = ScriptedTransport(self.messages())
            with self.transport_context(transport):
                result = await console_prune.execute_prune(prepared, self.config)
            self.assertEqual(result.spent_nusd, CHARGE + (index + 1) * 4 * CHARGE)
            self.assertEqual(len(transport.requests), 8)
        self.assertEqual(self.snapshot(), (9 * CHARGE, 8))

    async def test_same_prepared_request_cannot_send_twice(self):
        prepared = self.prepare()
        transport = ScriptedTransport(self.messages())
        with self.transport_context(transport):
            await console_prune.execute_prune(prepared, self.config)
        with patch.object(console_prune, "resolve_model_key") as key, self.assertRaises(GovernedError) as caught:
            await console_prune.execute_prune(prepared, self.config)
        self.assertEqual(caught.exception.code, "console_prune_request_already_used")
        key.assert_not_called()
        self.assertEqual(self.snapshot(), (5 * CHARGE, 4))
        self.assertEqual(len(transport.requests), 8)

    async def test_known_usage_malformed_tool_charges_once_and_closes(self):
        prepared = self.prepare()
        transport = ScriptedTransport([native_message([tool("unknown", "toolu_invalid")])])
        with self.transport_context(transport), self.assertRaises(GovernedError) as caught:
            await console_prune.execute_prune(prepared, self.config)
        snapshot = caught.exception.budget_snapshot
        self.assertEqual(snapshot["spent_nusd"], 2 * CHARGE)
        self.assertEqual(snapshot["pending_nusd"], 0)
        self.assertTrue(snapshot["halted"])
        self.assertEqual(len(transport.requests), 2)
        self.assertTrue(all(adapter._session is None for adapter in self.created_adapters))

    async def test_unknown_usage_keeps_hold_and_blocks_later_entry_before_key(self):
        prepared = self.prepare()
        response = native_message([tool("view", "toolu_unknown")])
        response.pop("usage")
        transport = ScriptedTransport([response])
        with self.transport_context(transport), self.assertRaises(GovernedError) as caught:
            await console_prune.execute_prune(prepared, self.config)
        snapshot = caught.exception.budget_snapshot
        self.assertEqual(snapshot["spent_nusd"], CHARGE)
        self.assertGreater(snapshot["pending_nusd"], 0)
        self.assertTrue(snapshot["halted"])
        self.assertEqual(len(transport.requests), 2)
        with patch.object(console_prune, "resolve_model_key") as key, self.assertRaises(GovernedError):
            await console_prune.execute_prune(prepared, self.config)
        key.assert_not_called()
        self.assertEqual(self.snapshot(), (CHARGE, 1))

    async def test_task_cancellation_keeps_primary_hold_and_physically_closes_client_after_secondary_close_fault(self):
        prepared = self.prepare()
        entered = asyncio.Event()
        primary = []
        requests = []
        async def transport(path, payload):
            requests.append(path)
            if path == "/messages/count_tokens":
                return {"input_tokens": 10}
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                primary.append(error)
                raise
        async def failed_close():
            raise RuntimeError("synthetic-secondary-close-error")
        with self.transport_context(transport):
            task = asyncio.create_task(console_prune.execute_prune(prepared, self.config))
            await asyncio.wait_for(entered.wait(), timeout=3)
            self.created_adapters[0].close = failed_close
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertIs(caught.exception, primary[0])
        self.assertTrue(task.cancelled())
        self.assertEqual(caught.exception.budget_snapshot["spent_nusd"], CHARGE)
        self.assertGreater(caught.exception.budget_snapshot["pending_nusd"], 0)
        self.assertTrue(self.created_adapters[0]._session.closed)
        self.assertEqual(requests, ["/messages/count_tokens", "/messages"])


class ConsolePruneTaskTests(ConsolePruneFixture, unittest.TestCase):
    def setUp(self):
        self.fixture()
        self.instance = SimpleNamespace(commands=queue.Queue())
        self.console_session = console_session.ConsoleSession(self.path, self.instance)
        self.addCleanup(self.console_session.close)
        def cleanup():
            task = self.console_session.service
            if task is not None and task.active:
                task.stop()
                self.wait(lambda: not task.active)
        self.addCleanup(cleanup)

    def test_cli_prune_routes_to_existing_console_flow(self):
        self.assertIn("prune", console_instance.COMMANDS)
        self.assertIn("prune", console_session.HELP)
        with patch("sys.argv", ["indeces", "prune", "--config", str(self.path)]), \
                patch.object(console, "load_config", return_value=self.config), \
                patch.object(console, "route_existing", return_value=None), \
                patch.object(console_session, "run_console") as run:
            console.main()
        run.assert_called_once_with(self.path, "prune")

    def test_dispatch_real_finite_closure_runs_tool_chain_without_gateway_or_maintenance(self):
        transport = ScriptedTransport(self.messages())
        prepared = self.prepare()
        with self.transport_context(transport), redirect_stdout(io.StringIO()) as output, \
                patch.object(console, "load_config", return_value=self.config), \
                patch.object(console_prune, "prepare_prune", return_value=prepared), \
                patch.object(console, "serve") as gateway, \
                patch("indeces.ingest.retry_ingestion") as maintenance:
            self.assertTrue(self.console_session.dispatch("prune"))
            task = self.console_session.service
            self.assertEqual(task.kind, "prune")
            self.wait(lambda: not task.active)
            self.console_session.dispatch("service")
        self.assertIsNone(task.error)
        self.assertEqual(task.result.text, "DONE")
        self.assertIn("DONE", output.getvalue())
        self.assertNotIn("private-synthetic-thought", output.getvalue())
        self.assertNotIn("synthetic-not-a-credential", output.getvalue())
        self.assertNotIn(str(self.binding.ledger_path), output.getvalue())
        gateway.assert_not_called()
        maintenance.assert_not_called()
        self.assertEqual(len(transport.requests), 8)

    def active_prune(self):
        entered, closed = threading.Event(), threading.Event()
        async def execute(prepared, config):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(console, "load_config", return_value=self.config))
        stack.enter_context(patch.object(console_prune, "prepare_prune", return_value=self.prepare()))
        call = stack.enter_context(patch.object(console_prune, "execute_prune", side_effect=execute))
        self.console_session.dispatch("prune")
        self.wait(entered.is_set)
        return call, closed

    def test_active_prune_blocks_duplicate_start_retry_and_configuration_changes(self):
        with redirect_stdout(io.StringIO()):
            call, _ = self.active_prune()
            original = self.console_session.service
            with patch.object(console, "serve") as gateway, patch.object(console, "configure_model") as model, \
                    patch.object(console, "configure_api_key") as apikey, \
                    patch("indeces.ingest.retry_ingestion") as maintenance:
                for command in ("prune", "start", "retry", "model", "apikey"):
                    self.console_session.dispatch(command)
                    self.assertIs(self.console_session.service, original)
                gateway.assert_not_called(); model.assert_not_called(); apikey.assert_not_called(); maintenance.assert_not_called()
            self.assertEqual(call.call_count, 1)

    def test_stop_cancels_finite_worker_and_releases_owned_task(self):
        with redirect_stdout(io.StringIO()):
            _, closed = self.active_prune()
            task = self.console_session.service
            self.console_session.dispatch("stop")
            self.wait(lambda: not task.active)
            self.assertTrue(closed.is_set())
            self.assertEqual(task.state, "stopped")
            self.assertIsNone(task.error)
            self.assertFalse(self.console_session.dispatch("quit"))

    def test_failure_code_and_fee_snapshot_display_without_private_exception_text(self):
        error = GovernedError("prune_synthetic_failed")
        error.budget_snapshot = {"spent_nusd": CHARGE, "remaining_nusd": 5_000_000_000 - CHARGE,
                                 "pending_nusd": 800_000, "halted": "prune_synthetic_failed"}
        async def fail(*args):
            raise error
        with redirect_stdout(io.StringIO()) as output, patch.object(console, "load_config", return_value=self.config), \
                patch.object(console_prune, "prepare_prune", return_value=self.prepare()), \
                patch.object(console_prune, "execute_prune", side_effect=fail):
            self.console_session.dispatch("prune")
            self.wait(lambda: not self.console_session.service.active)
            self.console_session.dispatch("service")
        self.assertEqual(self.console_session.service.error, "prune_synthetic_failed")
        self.assertIn("prune_synthetic_failed", output.getvalue())
        self.assertIn("pending", output.getvalue())
        self.assertNotIn(str(self.binding.ledger_path), output.getvalue())

    def test_ipc_prune_waits_for_existing_input_then_prompts_once(self):
        self.instance.commands.put("prune")
        class Reader:
            busy = True
            count = 0
            def poll(inner):
                inner.count += 1
                inner.busy = False
                return "" if inner.count == 1 else "quit"
            def before_prompt(inner):
                return not inner.busy
            def invalidate(inner):
                pass
        self.console_session.reader = Reader()
        calls = []
        def dispatch(command):
            calls.append(command)
            return command != "quit"
        with redirect_stdout(io.StringIO()), patch.object(self.console_session, "_safe_dispatch", side_effect=dispatch), \
                patch.object(self.console_session, "_refresh"), patch.object(console_session.time, "sleep"):
            self.console_session.run("console")
        self.assertEqual(calls, ["console", "prune", "", "quit"])
        self.assertEqual(list(self.console_session._pending), [])


if __name__ == "__main__":
    unittest.main()
