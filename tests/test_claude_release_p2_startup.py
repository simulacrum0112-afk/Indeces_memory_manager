"""Synthetic startup counterexamples: unsupported Claude must have no effects."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import asyncio
import io
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, console_session
from indeces.config import load_config
from indeces.contracts import GovernedError


ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-claude-startup-key-offline-only"
TOKEN = "synthetic-discord-startup-token-offline-only"
MODELS = ("claude-opus-5-5", "claude-fable-5-1")
FLAGS = ((True, False), (False, True), (True, True))


class StartupFixtures:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "config.local.toml"
        source = (ROOT / "config.claude.example.toml").read_text(encoding="utf-8")
        self.path.write_text(source.replace('guild_id = ""', 'guild_id = "10"'), encoding="utf-8")
        self.config = load_config(self.path)

    def configured(self, model, active, path):
        return replace(self.config, model_profiles={}, adapter=replace(self.config.adapter, model=model),
                       runtime=replace(self.config.runtime, model_selection_enabled=False,
                                       active_requery_enabled=active, path_requery_loop_enabled=path))

    def assert_refused(self, action, seams):
        caught = None
        try:
            action()
        except BaseException as error:
            caught = error
        self.assertIsInstance(caught, GovernedError)
        self.assertEqual(caught.code, "claude_requery_unsupported")
        self.assertIs(caught.remote_usage_unknown, False)
        self.assertIsNone(caught.known_usage)
        self.assertEqual({name: mock.call_count for name, mock in seams.items()},
                         {name: 0 for name in seams})

    def startup_seams(self, stack, config):
        seams = {}
        for name, value in (("checked_session_envelope", None), ("load_config", config),
                            ("resolve_model_key", KEY), ("load_discord_token", TOKEN),
                            ("prompt_secret", TOKEN), ("validate_token", TOKEN),
                            ("validate_runtime_paths", None), ("validate_config_path", self.path),
                            ("InstanceLock", Mock(close=Mock())), ("_run_with_lease", None)):
            seams[name] = stack.enter_context(patch.object(console, name, return_value=value))
        # This is a synthetic, empty environment; real credential references
        # are never read by the tests or used to authorize a model call.
        stack.enter_context(patch.dict("os.environ", {}, clear=True))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return seams


class ClaudeStartupRefusalTests(StartupFixtures, unittest.TestCase):
    def test_real_startup_credentials_both_models_each_flag_and_both(self):
        for model in MODELS:
            for active, path in FLAGS:
                with self.subTest(model=model, active=active, path=path), ExitStack() as stack:
                    config = self.configured(model, active, path)
                    seams = self.startup_seams(stack, config)
                    self.assert_refused(lambda: console._startup_credentials(config, self.path), seams)

    def test_public_run_refuses_before_path_validation_or_state_lease(self):
        for model in MODELS:
            for active, path in FLAGS:
                with self.subTest(model=model, active=active, path=path), ExitStack() as stack:
                    config = self.configured(model, active, path)
                    seams = self.startup_seams(stack, config)
                    self.assert_refused(lambda: console.run(config, self.path), seams)

    def test_console_start_refuses_before_guild_state_lease_and_credentials(self):
        session = console_session.ConsoleSession(self.path, SimpleNamespace(commands=queue.Queue()))
        try:
            for model in MODELS:
                for active, path in FLAGS:
                    with self.subTest(model=model, active=active, path=path), ExitStack() as stack:
                        config = self.configured(model, active, path)
                        seams = self.startup_seams(stack, config)
                        seams["_startup_credentials"] = stack.enter_context(patch.object(console, "_startup_credentials", return_value=(KEY, TOKEN)))
                        seams["ServiceTask"] = stack.enter_context(patch.object(console_session, "ServiceTask", return_value=None))
                        self.assert_refused(lambda: session._start(config), seams)
                        self.assertIsNone(session.service)
        finally:
            session.service = None
            session.close()

    def test_openai_startup_retains_token_then_model_key_and_exact_values(self):
        adapter = replace(self.config.adapter, provider="openai", model="gpt-6.1-sol",
                          base_url="https://api.openai.com/v1",
                          budgets={stage: replace(budget, reasoning="medium")
                                   for stage, budget in self.config.adapter.budgets.items()})
        config = replace(self.configured(MODELS[0], True, True), adapter=adapter)
        order = []
        with ExitStack() as stack:
            seams = self.startup_seams(stack, config)
            seams["load_discord_token"].side_effect = lambda *_: (order.append("token"), TOKEN)[1]
            seams["resolve_model_key"].side_effect = lambda *_args, **_kwargs: (order.append("model_key"), KEY)[1]
            self.assertEqual(console._startup_credentials(config, self.path), (KEY, TOKEN))
        self.assertEqual(order, ["token", "model_key"])
        seams["prompt_secret"].assert_not_called()
        seams["InstanceLock"].assert_not_called()


class ClaudeServeRefusalTests(StartupFixtures, unittest.IsolatedAsyncioTestCase):
    def service_seams(self, stack, config, *, synthetic_runtime=False):
        seams = {}
        scratch = SimpleNamespace(path="synthetic-scratch-no-file", startup_retention=None,
                                  write=Mock(), close=Mock())
        store = SimpleNamespace(db=SimpleNamespace(in_transaction=False), recover=Mock(return_value=[]), close=Mock())
        adapter = SimpleNamespace(close=AsyncMock())
        knowledge = SimpleNamespace(start=Mock(), close=AsyncMock())
        gateway = SimpleNamespace(run=AsyncMock(), close=AsyncMock())
        observer = SimpleNamespace(start=Mock(return_value="http://127.0.0.1/synthetic-no-server"), close=Mock())
        for name, value in (("checked_session_envelope", None), ("validate_runtime_paths", None),
                            ("InstanceLock", Mock(close=Mock())), ("ScratchLog", scratch),
                            ("prepare_knowledge_directory", None), ("Store", store),
                            ("create_model_adapter", adapter), ("KnowledgeService", knowledge),
                            ("DiscordBridge", gateway), ("prepare_scratch_directory", None),
                            ("ObserverServer", observer), ("SessionBudgetLedger", None)):
            seams[name] = stack.enter_context(patch.object(console, name, return_value=value))
        seams["scratch.write"] = scratch.write
        seams["Store.recover"] = store.recover
        seams["Gateway.run"] = gateway.run
        if synthetic_runtime:
            runtime = SimpleNamespace(graph=object(), model_selector=None)
            seams["create_runtime"] = stack.enter_context(patch.object(console, "create_runtime", return_value=runtime))
        else:
            # Before the startup fix, the real Runtime guard declines only
            # after scratch, Store.recover and the adapter were constructed.
            seams["create_runtime"] = stack.enter_context(patch.object(console, "create_runtime", wraps=console.create_runtime))
        async def idle_retention(_scratch):
            await asyncio.Event().wait()
        stack.enter_context(patch.object(console, "_maintain_scratch", side_effect=idle_retention))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return seams, gateway

    async def test_real_serve_both_models_each_flag_and_both_before_all_effects(self):
        for model in MODELS:
            for active, path in FLAGS:
                with self.subTest(model=model, active=active, path=path), ExitStack() as stack:
                    config = self.configured(model, active, path)
                    seams, _ = self.service_seams(stack, config)
                    caught = None
                    try:
                        await console.serve(config, KEY, TOKEN)
                    except BaseException as error:
                        caught = error
                    self.assertIsInstance(caught, GovernedError)
                    self.assertEqual(caught.code, "claude_requery_unsupported")
                    self.assertIs(caught.remote_usage_unknown, False)
                    self.assertIsNone(caught.known_usage)
                    self.assertEqual({name: mock.call_count for name, mock in seams.items()},
                                     {name: 0 for name in seams})

    async def test_openai_service_still_starts_synthetic_resources_and_exact_token(self):
        adapter = replace(self.config.adapter, provider="openai", model="gpt-6.1-sol",
                          base_url="https://api.openai.com/v1",
                          budgets={stage: replace(budget, reasoning="medium")
                                   for stage, budget in self.config.adapter.budgets.items()})
        config = replace(self.configured(MODELS[0], True, True), adapter=adapter)
        with ExitStack() as stack:
            seams, gateway = self.service_seams(stack, config, synthetic_runtime=True)
            await console.serve(config, KEY, TOKEN)
        seams["checked_session_envelope"].assert_called_once_with(config)
        seams["Store.recover"].assert_called_once_with()
        seams["create_model_adapter"].assert_called_once()
        self.assertIs(seams["create_model_adapter"].call_args.args[0], adapter)
        self.assertEqual(seams["create_model_adapter"].call_args.args[2], KEY)
        gateway.run.assert_awaited_once_with(TOKEN)
        seams["SessionBudgetLedger"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
