"""Production-derived five-stage and native factory seam cases, offline only.

Authored for the exact 68fa source port; root owns guarded execution. No real
API, Discord, production config, source corpus or credentials are used here.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict, replace
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, model_wizard
from indeces.claude_adapter import ClaudeAdapter
from indeces.config import SessionBudgetConfig, load_config
from indeces.contracts import GovernedError
from indeces.model_selection import ModelNPMILabelSelector
from indeces.richard_chat_selection import RichardChatSelector
from indeces.richard_selector import RichardGraphSelector
from indeces.richard_v11_ranker import FrozenRanker
from indeces.richard_chat_binding import NativeEvidenceBinder
from indeces.store import Store
from tests import test_richard_chat_factory as factory_fixture
from tests import test_session_budget_config as session_fixture


ROOT = Path(__file__).resolve().parents[1]
STAGES = {"label", "summary", "reply", "selection", "query"}


def claude_config(config):
    return replace(config, adapter=replace(config.adapter, provider="anthropic", model="claude-opus-5-5",
        base_url="https://api.anthropic.com/v1",
        budgets={stage: replace(budget, reasoning="high") for stage, budget in config.adapter.budgets.items()}))


class FiveStageProfilePortTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_legacy_optional_stages_materialize_only_their_existing_effective_caps(self):
        path = session_fixture.write_config(self.root)
        original = path.read_bytes()
        before = load_config(path)
        self.assertEqual(set(before.adapter.budgets), STAGES)
        self.assertNotIn("selection", tomllib.loads(original.decode())["adapter"]["budgets"])
        updated = model_wizard.patch_model_config(original, before, "anthropic", "claude-opus-5-5")
        path.write_bytes(updated)
        after = load_config(path)
        self.assertEqual(set(tomllib.loads(updated.decode())["adapter"]["budgets"]), STAGES)
        self.assertEqual(set(after.model_profiles["openai"]["efforts"]), STAGES)
        self.assertEqual(set(after.model_profiles["anthropic"]["efforts"]), STAGES)
        for stage in STAGES:
            old, new = before.adapter.budgets[stage], after.adapter.budgets[stage]
            self.assertEqual((old.input_tokens, old.output_tokens, old.seconds),
                             (new.input_tokens, new.output_tokens, new.seconds))
            self.assertEqual(new.reasoning, "high")
        path.write_bytes(model_wizard.patch_model_config(updated, after, "openai", "gpt-6.1-sol"))
        self.assertEqual(load_config(path).adapter.budgets, before.adapter.budgets)

    def test_controlled_session_cross_provider_selection_declines_without_disabling_guard(self):
        path = session_fixture.write_config(self.root, settings=session_fixture.session_settings())
        original = path.read_bytes()
        config = load_config(path)
        answers = iter(("claude", "", "yes"))
        with patch.object(model_wizard, "_backup_config") as backup, \
                patch.object(model_wizard, "_atomic_write") as write:
            self.assertFalse(model_wizard.configure_model(path, input_fn=lambda _: next(answers), output=lambda _: None))
        backup.assert_not_called()
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(asdict(load_config(path).session_budget), asdict(config.session_budget))

    def test_controlled_session_same_selection_preserves_entire_price_identity_and_stage_caps(self):
        path = session_fixture.write_config(self.root, settings=session_fixture.session_settings())
        original = path.read_bytes()
        config = load_config(path)
        updated = model_wizard.patch_model_config(original, config, "openai", "gpt-6.1-sol")
        path.write_bytes(updated)
        after = load_config(path)
        self.assertEqual(after.session_budget, config.session_budget)
        self.assertEqual(after.adapter.budgets, config.adapter.budgets)
        self.assertEqual(tomllib.loads(original.decode())["session_budget"], tomllib.loads(updated.decode())["session_budget"])

    def test_controlled_session_model_change_declines_even_within_claude_provider(self):
        path = session_fixture.write_config(self.root, settings={"enabled": True})
        config = claude_config(load_config(path))
        with self.assertRaisesRegex(model_wizard.ModelWizardError, "controlled_session_model_switch_forbidden"):
            model_wizard.patch_model_config(path.read_bytes(), config, "anthropic", "claude-fable-5-1")

    def test_all_five_efforts_require_high_for_claude_direct_config(self):
        config = claude_config(load_config(session_fixture.write_config(self.root)))
        for stage in STAGES:
            budgets = dict(config.adapter.budgets)
            budgets[stage] = replace(budgets[stage], reasoning="medium")
            with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, "explicit high"):
                replace(config.adapter, budgets=budgets)


class ClaudeNativeFactoryPortTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        base = load_config(ROOT / "config.example.toml")
        self.config = claude_config(replace(base, state_dir=self.root / "state", scratch_dir=self.root / "scratch",
            knowledge_dir=self.root / "knowledge"))
        self.store = Store(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.scratch = Mock()
        self.adapter = ClaudeAdapter(self.config.adapter, self.scratch,
                                     request=AsyncMock(side_effect=AssertionError("transport not admitted")))
        self.graph = factory_fixture.synthetic_graph()

    def test_normal_model_selector_uses_same_native_claude_adapter_and_selection_budget(self):
        runtime = console.create_runtime(self.config, self.store, self.adapter, self.scratch)
        self.assertIs(type(runtime.model_selector), ModelNPMILabelSelector)
        self.assertIs(runtime.model_selector.adapter, self.adapter)
        self.assertIs(runtime.model_selector.budget, self.config.adapter.budgets["selection"])
        self.adapter._request_override.assert_not_called()

    def test_richard_chat_factory_keeps_native_binder_ranker_and_both_algorithm_choices(self):
        for algorithm in ("alpha", "beta"):
            config = replace(self.config, runtime=replace(self.config.runtime, richard_chat_enabled=True,
                richard_chat_algorithm=algorithm, richard_graph_zip=str(self.root / "graph.zip"),
                richard_source_zip=str(self.root / "source.zip")))
            with self.subTest(algorithm=algorithm), patch("indeces.richard_v11_data.load_release", return_value=self.graph):
                runtime = console.create_runtime(config, self.store, self.adapter, self.scratch)
            selector = runtime.model_selector
            self.assertIs(type(selector), RichardChatSelector)
            self.assertIs(type(selector.binder), NativeEvidenceBinder)
            self.assertIs(type(selector.ranker), FrozenRanker)
            self.assertIs(selector.binder.db, self.store.db)
            self.assertIs(selector.ranker.graph, self.graph)
            self.assertIs(selector.adapter, self.adapter)
            self.assertEqual(selector.algorithm, algorithm)
            self.assertIs(selector.budget, config.adapter.budgets["selection"])
        self.adapter._request_override.assert_not_called()

    def test_legacy_richard_zero_call_path_remains_distinct(self):
        config = replace(self.config, runtime=replace(self.config.runtime, model_selection_enabled=False,
            richard_selection_enabled=True, richard_graph_zip=str(self.root / "graph.zip"),
            richard_source_zip=str(self.root / "source.zip")))
        with patch("indeces.richard_data.load_release", return_value=self.graph):
            runtime = console.create_runtime(config, self.store, self.adapter, self.scratch)
        self.assertIs(type(runtime.graph.selector), RichardGraphSelector)
        self.assertIsNone(runtime.model_selector)
        self.adapter._request_override.assert_not_called()

    def test_ordinary_claude_does_not_parse_or_create_controlled_ledger(self):
        config = replace(self.config, session_budget=SessionBudgetConfig(enabled=False, model="not-reviewed", valid_until_utc="not-an-expiry"))
        with patch.object(console, "SessionPriceEnvelope") as prices:
            self.assertIsNone(console.checked_session_envelope(config))
        prices.assert_not_called()


class ClaudeProductionServePortTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.config = claude_config(replace(load_config(ROOT / "config.example.toml"),
            state_dir=self.root / "state", scratch_dir=self.root / "scratch", knowledge_dir=self.root / "knowledge"))
        self.config = replace(self.config, discord=replace(self.config.discord, guild_id="123"))

    async def test_resolved_key_is_forwarded_unchanged_without_a_second_resolution_for_either_provider(self):
        for config in (self.config, replace(self.config, adapter=load_config(ROOT / "config.example.toml").adapter)):
            with self.subTest(provider=config.adapter.provider), \
                    patch.object(console, "_startup_credentials", return_value=("synthetic-resolved-key", "synthetic-token")), \
                    patch.object(console, "_check_start_config"), \
                    patch.object(console, "resolve_model_key", side_effect=AssertionError("credentials cannot be resolved again")) as resolve, \
                    patch.object(console, "serve", new=AsyncMock()) as serve, \
                    patch.object(console.asyncio, "run", side_effect=lambda coroutine: coroutine.close()):
                lease = object()
                console._run_with_lease(config, None, lease)
                serve.assert_called_once_with(config, "synthetic-resolved-key", "synthetic-token", lease=lease)
                resolve.assert_not_called()

    async def test_controlled_claude_refusal_precedes_price_ledger_key_and_all_service_construction(self):
        config = replace(self.config, session_budget=SessionBudgetConfig(enabled=True))
        with patch.object(console, "SessionPriceEnvelope") as prices, \
                patch.object(console, "SessionBudgetLedger") as ledger, \
                patch.object(console, "InstanceLock") as lease, \
                patch.object(console, "ScratchLog") as scratch, patch.object(console, "Store") as store, \
                patch.object(console, "create_model_adapter") as factory, \
                patch.object(console, "resolve_model_key") as key, patch.object(console, "load_discord_token") as discord:
            with self.assertRaises(GovernedError) as admission:
                await console.serve(config, "synthetic-key", "synthetic-token")
            self.assertEqual(admission.exception.code, "session_provider_unsupported")
            with self.assertRaises(GovernedError) as startup:
                console._startup_credentials(config, None)
            self.assertEqual(startup.exception.code, "session_provider_unsupported")
        for mock in (prices, ledger, lease, scratch, store, factory, key, discord):
            mock.assert_not_called()

    async def test_ordinary_serve_shares_one_claude_adapter_across_actual_runtime_selector_and_knowledge(self):
        capture, closes = {}, []
        scratch = SimpleNamespace(write=lambda *a, **k: None, close=lambda: None, path=self.config.scratch_dir)
        actual_create = console.create_runtime
        transport = AsyncMock(side_effect=AssertionError("no transport permitted"))
        def factory(adapter_config, log, key):
            adapter = ClaudeAdapter(adapter_config, log, key, request=transport)
            capture["raw"] = adapter
            async def close():
                closes.append("native")
            adapter.close = close
            return adapter
        def runtime_factory(config, store, adapter, log):
            runtime = actual_create(config, store, adapter, log)
            capture.update(runtime=runtime, store=store)
            return runtime
        async def knowledge_close():
            closes.append("knowledge")
        knowledge = SimpleNamespace(start=Mock(), close=knowledge_close)
        def knowledge_factory(config, store, graph, adapter, log):
            self.assertIs(adapter, capture["raw"])
            self.assertIs(adapter, capture["runtime"].adapter)
            self.assertIs(adapter, capture["runtime"].model_selector.adapter)
            return knowledge
        bridge = SimpleNamespace(run=AsyncMock(return_value=None), close=AsyncMock())
        observer = SimpleNamespace(start=lambda: "offline observer", close=lambda: None)
        with patch.object(console, "ScratchLog", return_value=scratch), \
                patch.object(console, "create_model_adapter", side_effect=factory), \
                patch.object(console, "create_runtime", side_effect=runtime_factory), \
                patch.object(console, "KnowledgeService", side_effect=knowledge_factory), \
                patch.object(console, "DiscordBridge", return_value=bridge), \
                patch.object(console, "ObserverServer", return_value=observer), \
                patch.object(console, "SessionBudgetLedger") as ledger, \
                patch.object(console, "prepare_knowledge_directory"), patch.object(console, "prepare_scratch_directory"), \
                redirect_stdout(io.StringIO()):
            await console.serve(self.config, "synthetic-private-key", "synthetic-token", lease=object())
        ledger.assert_not_called()
        knowledge.start.assert_called_once()
        transport.assert_not_called()
        self.assertEqual(closes, ["knowledge", "native"])
        self.assertIsNone(capture["raw"]._session)


if __name__ == "__main__":
    unittest.main()
