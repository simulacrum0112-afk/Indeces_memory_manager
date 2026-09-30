"""Observer integration uses synthetic resources, without a Bot or model call."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, observer
from indeces.config import load_config
from indeces.lock import InstanceLock
from tests.test_retention_service import ScratchFixture


class ObserverServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(base, state_dir=root / "state", scratch_dir=root / "scratch",
            knowledge_dir=root / "knowledge", discord=replace(base.discord, guild_id="123456789012345678"))
        self.order = []
        self.scratch = ScratchFixture(self.order)
        self.store, self.lease = Mock(), Mock()
        self.store.recover.return_value = []
        self.store.close.side_effect = lambda: self.order.append("store_close")
        self.lease.close.side_effect = lambda: self.order.append("lease_close")
        self.adapter, self.bridge, self.knowledge = Mock(), Mock(), Mock()
        for name, resource in (("adapter", self.adapter), ("bridge", self.bridge), ("knowledge", self.knowledge)):
            resource.close = AsyncMock(side_effect=lambda name=name: self.order.append(name + "_close"))
        self.bridge.run = AsyncMock(side_effect=lambda _: self.order.append("gateway_started"))
        self.knowledge.start.side_effect = lambda: self.order.append("knowledge_started")
        self.observer = Mock()
        self.observer.start.side_effect = lambda: self.order.append("observer_started") or "http://127.0.0.1:12345/synthetic/"
        self.observer.close.side_effect = lambda: self.order.append("observer_closed")

    def resources(self):
        stack = ExitStack()
        stack.enter_context(patch.object(console, "InstanceLock", return_value=self.lease))
        stack.enter_context(patch.object(console, "ScratchLog", return_value=self.scratch))
        stack.enter_context(patch.object(console, "Store", return_value=self.store))
        stack.enter_context(patch.object(console, "OpenAIAdapter", return_value=self.adapter))
        stack.enter_context(patch.object(console, "Runtime", return_value=Mock(graph=object())))
        stack.enter_context(patch.object(console, "KnowledgeService", return_value=self.knowledge))
        stack.enter_context(patch.object(console, "DiscordBridge", return_value=self.bridge))
        self.observer_factory = stack.enter_context(patch.object(console, "ObserverServer", return_value=self.observer))
        self.prepare = stack.enter_context(patch.object(console, "prepare_scratch_directory"))
        self.output = stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    async def serve(self):
        await asyncio.wait_for(console.serve(self.config, "synthetic-key", "synthetic-token"), timeout=2)

    def cleanup_assertions(self):
        self.assertTrue(self.scratch.closed)
        self.store.close.assert_called_once()
        self.lease.close.assert_called_once()
        for resource in (self.adapter, self.bridge, self.knowledge):
            resource.close.assert_awaited_once()
        self.adapter.call.assert_not_called()

    async def test_service_starts_one_observer_with_url_and_closes_before_runtime_resources(self):
        with self.resources():
            await self.serve()
        self.prepare.assert_called_once_with(self.config.scratch_dir)
        self.observer_factory.assert_called_once_with(self.config)
        self.observer.start.assert_called_once()
        self.observer.close.assert_called_once()
        self.assertIn("Indices read-only observer: http://127.0.0.1:12345/synthetic/", self.output.getvalue())
        self.assertLess(self.order.index("observer_started"), self.order.index("gateway_started"))
        for event in ("bridge_close", "knowledge_close", "adapter_close", "service_stop", "scratch_close", "store_close"):
            self.assertLess(self.order.index("observer_closed"), self.order.index(event))
        self.cleanup_assertions()

    async def test_observer_start_failure_is_optional_and_does_not_reveal_private_error(self):
        self.observer.start.side_effect = OSError("synthetic-private-observer-error")
        with self.resources():
            await self.serve()
        self.bridge.run.assert_awaited_once_with("synthetic-token")
        self.observer.close.assert_called_once()
        self.assertIn("Observer unavailable: OSError", self.output.getvalue())
        self.assertNotIn("synthetic-private-observer-error", self.output.getvalue())
        self.cleanup_assertions()

    async def test_directory_guide_failure_does_not_disable_gateway(self):
        with self.resources():
            self.prepare.side_effect = OSError("synthetic-private-guide-error")
            await self.serve()
        self.bridge.run.assert_awaited_once_with("synthetic-token")
        self.observer.start.assert_not_called()
        self.assertIn("Observer unavailable: OSError", self.output.getvalue())
        self.assertNotIn("synthetic-private-guide-error", self.output.getvalue())
        self.cleanup_assertions()

    async def test_failed_start_cleanup_error_does_not_disable_gateway(self):
        self.observer.start.side_effect = OSError("synthetic-private-start-error")
        self.observer.close.side_effect = RuntimeError("synthetic-private-close-error")
        with self.resources():
            await self.serve()
        self.bridge.run.assert_awaited_once_with("synthetic-token")
        self.observer.close.assert_called_once()
        self.assertNotIn("synthetic-private-start-error", self.output.getvalue())
        self.assertNotIn("synthetic-private-close-error", self.output.getvalue())
        self.cleanup_assertions()

    async def test_cancellation_stops_gateway_then_observer_before_storage(self):
        entered = asyncio.Event()

        async def gateway(_):
            try:
                entered.set()
                await asyncio.Future()
            finally:
                self.order.append("gateway_stopped")

        self.bridge.run.side_effect = gateway
        with self.resources():
            task = asyncio.create_task(console.serve(self.config, "synthetic-key", "synthetic-token"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertLess(self.order.index("gateway_stopped"), self.order.index("observer_closed"))
        self.assertLess(self.order.index("observer_closed"), self.order.index("store_close"))
        self.cleanup_assertions()

    async def test_observer_shutdown_error_still_closes_all_other_resources(self):
        self.observer.close.side_effect = OSError("synthetic-private-close-error")
        with self.resources(), self.assertRaises(OSError):
            await self.serve()
        self.assertIn("Service cleanup encountered: OSError", self.output.getvalue())
        self.assertNotIn("synthetic-private-close-error", self.output.getvalue())
        self.cleanup_assertions()

    async def test_gateway_error_is_preserved_when_observer_shutdown_also_fails(self):
        self.bridge.run.side_effect = RuntimeError("synthetic-gateway-error")
        self.observer.close.side_effect = OSError("synthetic-private-observer-close")
        with self.resources(), self.assertRaisesRegex(RuntimeError, "synthetic-gateway-error"):
            await self.serve()
        self.assertNotIn("synthetic-private-observer-close", self.output.getvalue())
        self.cleanup_assertions()


class ObserverStandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(base, state_dir=root / "state", scratch_dir=root / "scratch", knowledge_dir=root / "knowledge")
        self.observer = Mock()
        self.observer.start.return_value = "http://127.0.0.1:12345/synthetic/"
        self.observer._thread.join.side_effect = KeyboardInterrupt

    def test_observe_does_not_acquire_runtime_lease_or_require_credentials(self):
        lease = InstanceLock(self.config.state_dir)
        self.addCleanup(lease.close)
        with patch.object(observer, "ObserverServer", return_value=self.observer), \
                patch.object(observer, "prepare_scratch_directory") as prepare, \
                patch.object(console, "OpenAIAdapter") as adapter, patch.object(console, "DiscordBridge") as bridge, \
                redirect_stdout(io.StringIO()) as output:
            observer.observe(self.config)
        prepare.assert_called_once_with(self.config.scratch_dir)
        self.observer.start.assert_called_once()
        self.observer.close.assert_called_once()
        adapter.assert_not_called()
        bridge.assert_not_called()
        self.assertIn("Indices observer stopped.", output.getvalue())

    def test_observe_closes_after_start_failure_without_echoing_exception(self):
        self.observer.start.side_effect = OSError("synthetic-private-start-error")
        with patch.object(observer, "ObserverServer", return_value=self.observer), \
                patch.object(observer, "prepare_scratch_directory"), redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(OSError):
            observer.observe(self.config)
        self.observer.close.assert_called_once()
        self.assertNotIn("synthetic-private-start-error", output.getvalue())

    def test_socket_reader_has_explicit_two_second_timeout(self):
        listener = object.__new__(observer._Server)
        connection = Mock()
        address = ("127.0.0.1", 12345)
        with patch.object(observer.HTTPServer, "get_request", return_value=(connection, address)):
            self.assertEqual(listener.get_request(), (connection, address))
        connection.settimeout.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
