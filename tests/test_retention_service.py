from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console
from indeces.config import load_config


def summary(*, changed=False):
    return {"files_examined": 1, "files_changed": int(changed), "records_removed": 2 if changed else 0,
            "files_removed": 0, "cutoff": "2026-09-29T12:00:00+00:00"}


class ScratchFixture:
    def __init__(self, order, startup=None):
        self.order, self.records = order, []
        self.path = Path("synthetic-scratch.jsonl")
        self.startup_retention = startup or summary()
        self.closed = False
        self.prune_count = 0
        self.on_prune = lambda: summary()
        self.fail_event = None

    def write(self, event, **fields):
        if self.closed:
            raise AssertionError("write after scratch close")
        if event == self.fail_event:
            raise OSError("synthetic-private-I/O-detail")
        self.order.append(event)
        self.records.append((event, fields))

    def prune(self):
        if self.closed:
            raise AssertionError("prune after scratch close")
        self.order.append("prune")
        self.prune_count += 1
        return self.on_prune()

    def close(self):
        self.order.append("scratch_close")
        self.closed = True


class RetentionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        base = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(base, state_dir=root / "state", scratch_dir=root / "scratch",
                              knowledge_dir=root / "knowledge",
                              discord=replace(base.discord, guild_id="123456789012345678"))
        self.order = []
        self.scratch = ScratchFixture(self.order)
        self.lease, self.store = Mock(), Mock()
        self.store.recover.return_value = []
        self.adapter, self.knowledge, self.bridge = Mock(), Mock(), Mock()
        for name, resource in (("adapter", self.adapter), ("knowledge", self.knowledge), ("bridge", self.bridge)):
            resource.close = AsyncMock(side_effect=lambda name=name: self.order.append(name + "_close"))
        self.store.close.side_effect = lambda: self.order.append("store_close")
        self.lease.close.side_effect = lambda: self.order.append("lease_close")
        self.bridge.run = AsyncMock()

    def patched_service(self, *, interval=0.001):
        stack = ExitStack()
        def acquire(_):
            self.order.append("lease_acquired")
            return self.lease
        def scratch(_):
            self.assertEqual(self.order[0], "lease_acquired")
            self.order.append("scratch_open")
            return self.scratch
        stack.enter_context(patch.object(console, "InstanceLock", side_effect=acquire))
        stack.enter_context(patch.object(console, "ScratchLog", side_effect=scratch))
        stack.enter_context(patch.object(console, "Store", return_value=self.store))
        stack.enter_context(patch.object(console, "OpenAIAdapter", return_value=self.adapter))
        stack.enter_context(patch.object(console, "Runtime", return_value=Mock(graph=object())))
        stack.enter_context(patch.object(console, "KnowledgeService", return_value=self.knowledge))
        stack.enter_context(patch.object(console, "DiscordBridge", return_value=self.bridge))
        stack.enter_context(patch.object(console, "RETENTION_MAINTENANCE_SECONDS", interval))
        self.output = stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    async def serve(self):
        await asyncio.wait_for(console.serve(self.config, "synthetic-key", "synthetic-token"), timeout=2)

    def assert_cleanup(self):
        for resource in (self.bridge, self.knowledge, self.adapter):
            resource.close.assert_awaited_once()
        self.store.close.assert_called_once()
        self.lease.close.assert_called_once()
        self.assertTrue(self.scratch.closed)
        self.assertLess(self.order.index("service_stop"), self.order.index("scratch_close"))

    async def test_idle_service_prunes_without_chat_or_model_calls_and_only_changed_receipt(self):
        stopped = asyncio.Event()
        tick_one, tick_two, never_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sleeping = asyncio.Queue()
        sleep_count = 0
        async def controlled_sleep(seconds):
            nonlocal sleep_count
            self.assertEqual(seconds, 60)
            sleep_count += 1
            sleeping.put_nowait(sleep_count)
            await {1: tick_one, 2: tick_two}.get(sleep_count, never_release).wait()
        async def gateway(_):
            await stopped.wait()
        self.bridge.run.side_effect = gateway
        def prune():
            if self.scratch.prune_count == 2:
                stopped.set()
                return summary(changed=True)
            return summary()
        self.scratch.on_prune = prune
        with self.patched_service(interval=60), patch.object(console.asyncio, "sleep", side_effect=controlled_sleep):
            task = asyncio.create_task(self.serve())
            self.assertEqual(await asyncio.wait_for(sleeping.get(), timeout=2), 1)
            tick_one.set()
            self.assertEqual(await asyncio.wait_for(sleeping.get(), timeout=2), 2)
            self.assertEqual(self.scratch.prune_count, 1)
            tick_two.set()
            await task
        self.assertEqual(self.scratch.prune_count, 2)
        receipts = [fields for event, fields in self.scratch.records if event == "scratch_retention"]
        self.assertEqual(receipts, [{"phase": "maintenance", "summary": summary(changed=True)}])
        self.assertEqual(self.output.getvalue().count("Scratch retention (maintenance):"), 1)
        self.adapter.call.assert_not_called()
        self.assert_cleanup()

    async def test_retention_failure_stops_gateway_reports_only_type_and_closes_all_resources(self):
        async def gateway(_):
            try:
                await asyncio.Future()
            finally:
                self.order.append("gateway_stopped")
        self.bridge.run.side_effect = gateway
        def prune():
            raise OSError("synthetic-private-I/O-detail")
        self.scratch.on_prune = prune
        with self.patched_service(), self.assertRaises(OSError):
            await self.serve()
        self.assertIn("Scratch retention failed: OSError; service stopped.", self.output.getvalue())
        self.assertNotIn("synthetic-private-I/O-detail", self.output.getvalue())
        self.assertLess(self.order.index("gateway_stopped"), self.order.index("bridge_close"))
        self.assert_cleanup()

    async def test_retention_receipt_failure_also_stops_service(self):
        async def gateway(_):
            await asyncio.Future()
        self.bridge.run.side_effect = gateway
        self.scratch.on_prune = lambda: summary(changed=True)
        self.scratch.fail_event = "scratch_retention"
        with self.patched_service(), self.assertRaises(OSError):
            await self.serve()
        self.assertIn("Scratch retention failed: OSError; service stopped.", self.output.getvalue())
        self.assert_cleanup()

    async def test_startup_receipt_is_logged_before_model_and_gateway_start(self):
        self.scratch.startup_retention = summary(changed=True)
        with self.patched_service(interval=60):
            await self.serve()
        receipts = [fields for event, fields in self.scratch.records if event == "scratch_retention"]
        self.assertEqual(receipts, [{"phase": "startup", "summary": summary(changed=True)}])
        self.assertLess(self.order.index("scratch_retention"), self.order.index("service_start"))
        self.assertIn("Scratch retention (startup): removed 2 records", self.output.getvalue())
        self.assertEqual(self.scratch.prune_count, 0)
        self.assert_cleanup()

    async def test_no_changes_produce_no_retention_receipt(self):
        with self.patched_service(interval=60):
            await self.serve()
        self.assertFalse(any(event == "scratch_retention" for event, _ in self.scratch.records))
        self.assertNotIn("Scratch retention (", self.output.getvalue())
        started = next(fields for event, fields in self.scratch.records if event == "service_start")
        self.assertEqual(started["scratch_retention_seconds"], 86400)
        self.assertEqual(started["scratch_maintenance_seconds"], 60)
        self.assert_cleanup()

    async def test_startup_orphan_cleanup_alone_has_an_explicit_receipt(self):
        self.scratch.startup_retention = {**summary(), "temporary_files_removed": 1}
        with self.patched_service(interval=60):
            await self.serve()
        receipts = [fields for event, fields in self.scratch.records if event == "scratch_retention"]
        self.assertEqual(receipts, [{"phase": "startup", "summary": self.scratch.startup_retention}])
        self.assertIn("1 temporary files", self.output.getvalue())
        self.assert_cleanup()

    async def test_shutdown_awaits_both_tasks_before_stop_record_and_scratch_close(self):
        ready = asyncio.Event()
        async def gateway(_):
            try:
                ready.set()
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                self.scratch.write("gateway_finalized")
        async def maintenance(scratch):
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                scratch.write("maintenance_finalized")
        self.bridge.run.side_effect = gateway
        with self.patched_service(), patch.object(console, "_maintain_scratch", side_effect=maintenance):
            task = asyncio.create_task(console.serve(self.config, "synthetic-key", "synthetic-token"))
            await asyncio.wait_for(ready.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        for event in ("gateway_finalized", "maintenance_finalized"):
            self.assertLess(self.order.index(event), self.order.index("service_stop"))
        self.assert_cleanup()

    async def test_instance_lease_failure_never_opens_or_prunes_scratch(self):
        with patch.object(console, "InstanceLock", side_effect=RuntimeError("synthetic-contention")), \
                patch.object(console, "ScratchLog") as scratch, self.assertRaises(RuntimeError):
            await console.serve(self.config, "synthetic-key", "synthetic-token")
        scratch.assert_not_called()

    async def test_startup_prune_failure_releases_lease_without_opening_other_resources(self):
        with patch.object(console, "InstanceLock", return_value=self.lease), \
                patch.object(console, "ScratchLog", side_effect=OSError("synthetic-private-I/O-detail")), \
                patch.object(console, "Store") as store, redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(OSError):
            await console.serve(self.config, "synthetic-key", "synthetic-token")
        self.lease.close.assert_called_once()
        store.assert_not_called()
        self.assertIn("Scratch retention/startup failed: OSError; service stopped.", output.getvalue())
        self.assertNotIn("synthetic-private-I/O-detail", output.getvalue())

    async def test_unexpected_maintenance_exit_stops_service(self):
        async def gateway(_):
            await asyncio.Future()
        self.bridge.run.side_effect = gateway
        with self.patched_service(), patch.object(console, "_maintain_scratch", new_callable=AsyncMock), \
                self.assertRaisesRegex(RuntimeError, "retention task stopped"):
            await self.serve()
        self.assertIn("Scratch retention failed: RuntimeError; service stopped.", self.output.getvalue())
        self.assert_cleanup()

    async def test_gateway_failure_cancels_maintenance_and_preserves_original_error(self):
        self.bridge.run.side_effect = RuntimeError("synthetic-gateway-failure")
        with self.patched_service(interval=60), self.assertRaisesRegex(RuntimeError, "synthetic-gateway-failure"):
            await self.serve()
        self.assertEqual(self.scratch.prune_count, 0)
        self.assert_cleanup()

    def test_scratch_verification_is_read_only_and_reports_checkpoint_partial_window(self):
        self.config.scratch_dir.mkdir()
        path = self.config.scratch_dir / "synthetic.jsonl"
        contents = b"synthetic-private-record-data\n"
        path.write_bytes(contents)
        checkpoint = {"cutoff": "2026-09-29T12:00:00+00:00", "pruned_at": "2026-09-30T12:00:00+00:00",
                      "removed_through_sequence": 12, "removed_head_hash": "a" * 64}
        report = {"counts": {"retention_partial": 1}, "call_counts": {"retention_partial": 1},
                  "calls": [{"call_id": "synthetic-call", "trace_id": "synthetic-trace", "status": "retention_partial"}],
                  "turns": [{"trace_id": "synthetic-trace", "status": "retention_partial", "warnings": []}], "issues": []}
        with patch.object(console, "verify", return_value=(3, "b" * 64)), \
                patch.object(console, "retention_checkpoint", return_value=checkpoint), \
                patch.object(console, "verify_runs", return_value=report), \
                patch.object(console, "ScratchLog") as scratch, redirect_stdout(io.StringIO()) as output:
            console.check_scratch(self.config)
        scratch.assert_not_called()
        self.assertEqual(path.read_bytes(), contents)
        self.assertIn("3 records, hash chain OK", output.getvalue())
        self.assertIn("removed_through_sequence=12", output.getvalue())
        self.assertIn("retention_partial", output.getvalue())
        self.assertNotIn("synthetic-private-record-data", output.getvalue())

    def test_status_describes_in_service_policy_without_touching_scratch(self):
        with patch.object(console, "ScratchLog") as scratch, redirect_stdout(io.StringIO()) as output:
            console.status(self.config)
        scratch.assert_not_called()
        self.assertIn("rolling 24 hours", output.getvalue())
        self.assertIn("Stopped services do not clean logs", output.getvalue())


if __name__ == "__main__":
    unittest.main()
