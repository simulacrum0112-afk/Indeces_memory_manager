"""Explicit chat factory activation, using local state and no provider calls."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console
from indeces.active_requery import ActiveRequery
from indeces.config import RuntimeConfig, load_config
from indeces.model_selection import ModelNPMILabelSelector
from indeces.path_policy import PathPolicyError
from indeces.richard_chat_binding import NativeEvidenceBinder
from indeces.richard_chat_selection import RichardChatSelector
from indeces.richard_data import from_rows
from indeces.richard_selector import RichardGraphSelector
from indeces.richard_v11_ranker import FrozenRanker
from indeces.runtime import Runtime
from indeces.store import Store


ROOT = Path(__file__).resolve().parents[1]


def synthetic_graph():
    return from_rows([
        {"cid": cid, "name": name, "members": [cid], "record_frequency": 0,
         "coarse": "synthetic", "fine": "synthetic"}
        for cid, name in ((1, "alpha"), (2, "beta"))], [])


class OfflineFixture:
    def setUp(self):
        self.network = ExitStack()
        self.addCleanup(self.network.close)
        for target in ("socket.getaddrinfo", "socket.create_connection", "socket.socket.connect"):
            self.network.enter_context(patch(target, side_effect=AssertionError("network denied")))
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        base = load_config(ROOT / "config.example.toml")
        self.config = replace(base, state_dir=self.root / "state", scratch_dir=self.root / "scratch",
                              knowledge_dir=self.root / "knowledge")


class RichardChatConfigTests(OfflineFixture, unittest.TestCase):
    def load_settings(self, settings):
        names = {"richard_chat_enabled", "richard_chat_algorithm", "richard_selection_enabled",
                 "richard_graph_zip", "richard_source_zip", "model_selection_enabled"}
        lines = [line for line in (ROOT / "config.example.toml").read_text(encoding="utf-8").splitlines()
                 if line.split("=", 1)[0].strip() not in names]
        position = lines.index("[runtime]") + 1
        lines[position:position] = [f"{key} = {value}" for key, value in settings.items()]
        path = self.root / "synthetic.toml"
        path.write_text("\n".join(lines), encoding="utf-8")
        return load_config(path)

    def test_default_and_old_configuration_leave_chat_off(self):
        for config in (self.config, self.load_settings({})):
            self.assertIs(config.runtime.richard_chat_enabled, False)
            self.assertEqual(config.runtime.richard_chat_algorithm, "alpha")
            self.assertIs(config.runtime.model_selection_enabled, True)
            self.assertEqual(config.runtime.richard_graph_zip, "")
            self.assertEqual(config.runtime.richard_source_zip, "")

    def test_enabled_paths_are_explicit_and_resolve_beside_config(self):
        config = self.load_settings({"richard_chat_enabled": "true", "richard_chat_algorithm": '"beta"',
            "richard_graph_zip": '"assets/graph.zip"', "richard_source_zip": '"assets/source.zip"'})
        self.assertEqual(config.runtime.richard_chat_algorithm, "beta")
        self.assertEqual(Path(config.runtime.richard_graph_zip), self.root / "assets" / "graph.zip")
        self.assertEqual(Path(config.runtime.richard_source_zip), self.root / "assets" / "source.zip")

    def test_chat_flag_is_an_exact_boolean(self):
        for value in (0, 1, "true", None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a boolean"):
                RuntimeConfig(richard_chat_enabled=value)

    def test_algorithm_is_a_fixed_alpha_or_beta_configuration(self):
        for value in (None, True, 1, [], "", "frequency", "alpha_meet", "ALPHA"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "alpha or beta"):
                RuntimeConfig(richard_chat_algorithm=value)

    def test_chat_requires_the_existing_selection_model_stage(self):
        with self.assertRaisesRegex(ValueError, "model_selection_enabled=true"):
            RuntimeConfig(richard_chat_enabled=True, model_selection_enabled=False,
                          richard_graph_zip="graph.zip", richard_source_zip="source.zip")

    def test_chat_and_zero_call_legacy_route_are_mutually_exclusive(self):
        for model_enabled in (True, False):
            with self.subTest(model_enabled=model_enabled), self.assertRaisesRegex(ValueError, "mutually exclusive"):
                RuntimeConfig(richard_chat_enabled=True, richard_selection_enabled=True,
                              model_selection_enabled=model_enabled,
                              richard_graph_zip="graph.zip", richard_source_zip="source.zip")

    def test_both_archives_are_required_without_ambient_discovery(self):
        for paths in ({}, {"richard_graph_zip": "graph.zip"}, {"richard_source_zip": "source.zip"}):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, "both pinned ZIP paths"):
                RuntimeConfig(richard_chat_enabled=True, **paths)


class RichardChatFactoryTests(OfflineFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.adapter, self.scratch = Mock(), Mock()
        self.graph = synthetic_graph()

    def enabled(self, **values):
        runtime = replace(self.config.runtime, richard_chat_enabled=True,
                          richard_graph_zip=str(self.root / "graph.zip"),
                          richard_source_zip=str(self.root / "source.zip"), **values)
        return replace(self.config, runtime=runtime)

    def create(self, config):
        # Exercise the actual console path; never supply a trusted selector override.
        return console.create_runtime(config, self.store, self.adapter, self.scratch)

    def assert_bound(self, runtime, config, graph):
        self.assertIs(type(runtime), Runtime)
        self.assertIs(type(runtime.model_selector), RichardChatSelector)
        self.assertIs(runtime.graph.selector, runtime.model_selector)
        selector = runtime.model_selector
        self.assertIs(type(selector.ranker), FrozenRanker)
        self.assertIs(type(selector.binder), NativeEvidenceBinder)
        self.assertIs(selector.ranker.graph, graph)
        self.assertIs(selector.binder.graph, graph)
        self.assertIs(selector.binder.db, self.store.db)
        self.assertEqual(selector.graph_identity, graph.identity_sha256)
        self.assertEqual(selector.algorithm, config.runtime.richard_chat_algorithm)
        self.assertIs(selector.adapter, self.adapter)
        self.assertIs(selector.scratch, self.scratch)
        self.assertIs(selector.budget, config.adapter.budgets["selection"])
        self.assertFalse(self.store.db.in_transaction)
        self.adapter.call.assert_not_called()
        self.scratch.write.assert_not_called()

    def test_actual_factory_binds_native_components_and_original_stage_budget(self):
        for algorithm in ("alpha", "beta"):
            config = self.enabled(richard_chat_algorithm=algorithm)
            with self.subTest(algorithm=algorithm), patch("indeces.richard_v11_data.load_release", return_value=self.graph) as load:
                runtime = self.create(config)
            load.assert_called_once_with(self.root / "graph.zip", self.root / "source.zip")
            self.assert_bound(runtime, config, self.graph)

    def test_off_preserves_both_historical_factory_origins_without_loading(self):
        with patch("indeces.richard_v11_data.load_release", side_effect=AssertionError("chat graph load")) as v11, \
                patch("indeces.richard_data.load_release", side_effect=AssertionError("legacy graph load")) as legacy:
            runtime = self.create(self.config)
            self.assertIs(type(runtime.model_selector), ModelNPMILabelSelector)
            self.assertIs(runtime.graph.selector, runtime.model_selector)
            config = replace(self.config, runtime=replace(self.config.runtime, model_selection_enabled=False))
            runtime = self.create(config)
            self.assertIsNone(runtime.model_selector)
            self.assertIsNone(runtime.graph.selector)
        v11.assert_not_called()
        legacy.assert_not_called()
        self.adapter.call.assert_not_called()

    def test_legacy_route_remains_separate_and_zero_call(self):
        config = replace(self.config, runtime=replace(self.config.runtime,
            model_selection_enabled=False, richard_selection_enabled=True,
            richard_graph_zip=str(self.root / "graph.zip"), richard_source_zip=str(self.root / "source.zip")))
        with patch("indeces.richard_data.load_release", return_value=self.graph), \
                patch("indeces.richard_v11_data.load_release", side_effect=AssertionError("chat graph load")):
            runtime = self.create(config)
        self.assertIs(type(runtime.graph.selector), RichardGraphSelector)
        self.assertIsNone(runtime.model_selector)
        self.adapter.call.assert_not_called()

    def test_loading_occurs_outside_transaction_and_before_runtime(self):
        observed = []

        def load(*paths):
            self.assertFalse(self.store.db.in_transaction)
            self.assertEqual(observed, [])
            observed.append("loaded")
            return self.graph

        def construct(*args, **kwargs):
            self.assertEqual(observed, ["loaded"])
            observed.append("runtime")
            return Runtime(*args, **kwargs)

        with patch("indeces.richard_v11_data.load_release", side_effect=load), \
                patch.object(console, "Runtime", side_effect=construct):
            runtime = self.create(self.enabled())
        self.assertEqual(observed, ["loaded", "runtime"])
        self.assert_bound(runtime, self.enabled(), self.graph)

    def test_transaction_refusal_precedes_loader_and_runtime(self):
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            with patch("indeces.richard_v11_data.load_release") as load, patch.object(console, "Runtime") as runtime:
                with self.assertRaisesRegex(ValueError, "outside a SQLite transaction"):
                    self.create(self.enabled())
                load.assert_not_called()
                runtime.assert_not_called()
        finally:
            self.store.db.rollback()

    def test_loader_failure_prevents_runtime_and_any_provider_call(self):
        with patch("indeces.richard_v11_data.load_release", side_effect=ValueError("pinned digest mismatch")), \
                patch.object(console, "Runtime") as runtime:
            with self.assertRaisesRegex(ValueError, "pinned digest mismatch"):
                self.create(self.enabled())
            runtime.assert_not_called()
        self.adapter.call.assert_not_called()

    def test_direct_factory_rechecks_flags_algorithm_and_required_paths(self):
        base = vars(self.enabled().runtime)
        for delta, reason in (({"richard_chat_enabled": 1}, "must be a boolean"),
                ({"richard_selection_enabled": True}, "mutually exclusive"),
                ({"model_selection_enabled": False}, "model_selection_enabled=true"),
                ({"model_selection_enabled": 1}, "model_selection_enabled=true"),
                ({"richard_chat_algorithm": "frequency"}, "alpha or beta"),
                ({"richard_graph_zip": ""}, "both pinned ZIP paths"),
                ({"richard_source_zip": " "}, "both pinned ZIP paths")):
            config = replace(self.config, runtime=SimpleNamespace(**{**base, **delta}))
            with self.subTest(delta=delta), patch("indeces.richard_v11_data.load_release") as load:
                with self.assertRaisesRegex(ValueError, reason):
                    self.create(config)
                load.assert_not_called()

    def test_direct_relative_paths_use_project_root(self):
        config = replace(self.enabled(), runtime=replace(self.enabled().runtime,
            richard_graph_zip="assets/graph.zip", richard_source_zip="assets/source.zip"))
        with patch("indeces.richard_v11_data.load_release", return_value=self.graph) as load:
            self.create(config)
        load.assert_called_once_with(self.root / "assets" / "graph.zip", self.root / "assets" / "source.zip")

    def test_managed_path_rejection_precedes_loader_and_runtime(self):
        config = replace(self.enabled(), runtime=replace(self.enabled().runtime,
            richard_graph_zip=str(self.root / "OneDrive" / "graph.zip")))
        with patch("indeces.richard_v11_data.load_release") as load, patch.object(console, "Runtime") as runtime:
            with self.assertRaises(PathPolicyError):
                self.create(config)
            load.assert_not_called()
            runtime.assert_not_called()

    def test_later_requery_query_graph_has_no_chat_ranker(self):
        with patch("indeces.richard_v11_data.load_release", return_value=self.graph):
            runtime = self.create(self.enabled())
        requery = ActiveRequery(runtime, object(), "synthetic-trace", object())
        self.assertIsNone(requery.query_graph.selector)
        self.assertIs(type(runtime.model_selector), RichardChatSelector)
        self.adapter.call.assert_not_called()

    def test_real_pinned_factory_loads_v302_without_loader_patch(self):
        graph_path = os.environ.get("INDECES_RICHARD_V11_GRAPH_ZIP")
        source_path = os.environ.get("INDECES_RICHARD_V11_SOURCE_ZIP")
        if not graph_path and not source_path:
            self.skipTest("pinned graph/source inputs supplied by mandatory full-gate runner")
        self.assertTrue(graph_path and source_path, "both pinned input environment variables are required")
        config = replace(self.enabled(), runtime=replace(self.enabled().runtime,
            richard_graph_zip=graph_path, richard_source_zip=source_path))
        runtime = self.create(config)
        graph = runtime.model_selector.ranker.graph
        self.assertEqual(graph.verification_status, "verified_pinned_release_and_rebuilt")
        self.assertEqual(graph.source_manifest["artifact_version"], "v3.0.2")
        self.assertEqual(len(graph.identity_sha256), 64)
        self.assertGreater(len(graph.nodes), 10)
        self.assert_bound(runtime, config, graph)
        for component in (RichardChatSelector, NativeEvidenceBinder, FrozenRanker):
            module = __import__(component.__module__, fromlist=["__file__"])
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(ROOT / "indeces"))


class RichardChatServiceStartTests(OfflineFixture, unittest.IsolatedAsyncioTestCase):
    async def test_startup_receipt_distinguishes_enabled_from_called_and_initial_scope(self):
        config = replace(self.config, discord=replace(self.config.discord, guild_id="123456789012345678"),
            runtime=replace(self.config.runtime, richard_chat_enabled=True, richard_chat_algorithm="beta",
                richard_graph_zip=str(self.root / "graph.zip"), richard_source_zip=str(self.root / "source.zip")))
        store = Store(config.state_dir)
        self.addCleanup(store.close)
        adapter, scratch, lease, knowledge, bridge, observer = (Mock() for _ in range(6))
        scratch.startup_retention = None
        scratch.path = self.root / "synthetic.jsonl"
        for resource in (adapter, knowledge, bridge):
            resource.close = AsyncMock()
        bridge.run = AsyncMock()
        receipts = []
        scratch.write.side_effect = lambda event, **fields: receipts.append((event, fields))
        with ExitStack() as stack:
            for name, resource in (("Store", store), ("ScratchLog", scratch), ("InstanceLock", lease),
                    ("create_model_adapter", adapter), ("KnowledgeService", knowledge), ("DiscordBridge", bridge),
                    ("ObserverServer", observer)):
                stack.enter_context(patch.object(console, name, return_value=resource))
            stack.enter_context(patch.object(console, "prepare_knowledge_directory"))
            stack.enter_context(patch.object(console, "prepare_scratch_directory"))
            stack.enter_context(patch("indeces.richard_v11_data.load_release", return_value=synthetic_graph()))
            stack.enter_context(redirect_stdout(io.StringIO()))
            await asyncio.wait_for(console.serve(config, "synthetic-key", "synthetic-token"), timeout=2)
        starts = [fields for event, fields in receipts if event == "service_start"]
        self.assertEqual(len(starts), 1)
        receipt = starts[0]
        self.assertEqual(receipt["evidence_selection"], "richard_chat_v11")
        self.assertIs(receipt["richard_chat_enabled"], True)
        self.assertIs(receipt["model_selection_enabled"], True)
        self.assertEqual(receipt["richard_chat_algorithm"], "beta")
        self.assertEqual(receipt["richard_chat_policy"], {"id": "richard_chat_v11", "version": "v2"})
        self.assertEqual(receipt["richard_chat_scope"], "initial_native_eligible_candidates_only")
        self.assertIs(receipt["richard_chat_ranker_called"], False)
        self.assertIs(receipt["richard_chat_ranker_applied"], False)
        self.assertEqual(receipt["richard_chat_call_evidence"], "per_turn_richard_chat_selection_receipt")
        self.assertNotIn("richard_selection_policy", receipt)
        adapter.call.assert_not_called()
