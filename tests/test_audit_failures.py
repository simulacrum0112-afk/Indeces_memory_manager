"""Fault regressions at the SQLite / scratch / confirmed-delivery boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console
from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, RuntimeConfig
from indeces.contracts import DeliveryReceipt, IncomingMessage, ModelResult
from indeces.run_records import verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store


def configuration():
    return SimpleNamespace(name="Indices", runtime=RuntimeConfig(summary_max_bytes=256),
                           knowledge=KnowledgeConfig(), discord=DiscordConfig(guild_id="10"),
                           adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1",
                                                 {s: Budget(10000, 100, 1) for s in ("reply", "summary", "label")}))


class FailureScratch:
    def __init__(self, fail_event):
        self.events, self.fail_event = [], fail_event

    def write(self, event, **fields):
        if event == self.fail_event:
            raise OSError("synthetic audit failure")
        self.events.append((event, deepcopy(fields)))


class ReplyAdapter:
    def __init__(self):
        self.calls = []

    async def call(self, stage, instructions, messages, trace_id, schema=None):
        self.calls.append(stage)
        return ModelResult("A grounded answer [M1]", 10, 5, "offline", 0)


class AuditFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state")
        self.addCleanup(self.store.close)
        self.message = IncomingMessage("event-1", "20", "10", "30", "Alice", "alpha beta", "2026-09-30T12:00:00+00:00")
        self.sent = []

    async def deliver(self, text):
        self.sent.append(text)
        return DeliveryReceipt(("remote-receipt",), text)

    def runtime(self, scratch):
        adapter = ReplyAdapter()
        runtime = Runtime(configuration(), self.store, adapter, scratch)
        runtime.graph.add(runtime.knowledge_scope, "synthetic-source", "author",
                          [{"text": "alpha beta fact", "quote": "alpha beta fact", "marks": ["alpha", "beta"]}], 1)
        return runtime, adapter

    async def test_confirmed_receipt_is_not_overwritten_after_turn_end_audit_failure(self):
        runtime, adapter = self.runtime(FailureScratch("turn_end"))
        with redirect_stdout(io.StringIO()) as output:
            await runtime.process(self.message, self.deliver)
        turn = self.store.db.execute("SELECT status,delivered,receipt FROM turns").fetchone()
        self.assertEqual(turn["status"], "delivered")
        self.assertEqual(turn["delivered"], self.sent[0])
        self.assertIn("remote-receipt", turn["receipt"])
        self.assertEqual([r["role"] for r in self.store.history(self.message.scope)], ["user", "assistant"])
        self.assertEqual(adapter.calls, ["reply"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("delivery confirmed", output.getvalue())

    async def test_confirmed_receipt_is_preserved_when_citation_receipt_write_fails(self):
        runtime, _ = self.runtime(FailureScratch("answer_delivered"))
        with redirect_stdout(io.StringIO()):
            await runtime.process(self.message, self.deliver)
        self.assertEqual(self.store.db.execute("SELECT status FROM turns").fetchone()[0], "delivered")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.history(self.message.scope)[-1]["content"], self.sent[0])

    async def test_failed_material_freeze_still_records_committed_weight_transition(self):
        scratch = ScratchLog(Path(self.directory.name) / "scratch")
        self.addCleanup(scratch.close)
        runtime, adapter = self.runtime(scratch)
        with patch("indeces.runtime.freeze_retrieval", side_effect=ValueError("synthetic freeze failure")), redirect_stdout(io.StringIO()):
            await runtime.process(self.message, self.deliver)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(self.store.db.execute("SELECT weight FROM memory_dynamic").fetchone()[0], 1.99)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM memory_event_audits").fetchone()[0], 1)
        verify(scratch.path)
        report = verify_runs([scratch.path])
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["issues"], [])
        self.assertEqual(len(self.sent), 1)  # fixed failure notice only

    async def test_failed_generated_audit_never_crosses_delivery_boundary(self):
        runtime, _ = self.runtime(FailureScratch("answer_generated"))
        with redirect_stdout(io.StringIO()):
            await runtime.process(self.message, self.deliver)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("A grounded answer", self.sent[0])
        self.assertEqual([r["role"] for r in self.store.history(self.message.scope)], ["user"])


class CleanupFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_audit_failure_still_closes_all_resources_and_releases_lock(self):
        for fail in ("service_stop", "bridge_close"):
            with self.subTest(fail=fail):
                config = configuration()
                config.state_dir, config.scratch_dir = Path("unused-state"), Path("unused-scratch")
                lease, scratch, store = Mock(), Mock(), Mock()
                store.recover.return_value = []
                scratch.path = Path("unused-scratch.jsonl")
                if fail == "service_stop":
                    scratch.write.side_effect = lambda event, **_: (_ for _ in ()).throw(OSError()) if event == "service_stop" else None
                adapter, knowledge, bridge = Mock(), Mock(), Mock()
                adapter.close, knowledge.close, bridge.close = AsyncMock(), AsyncMock(), AsyncMock()
                bridge.run = AsyncMock()
                if fail == "bridge_close":
                    bridge.close.side_effect = OSError()
                with patch.object(console, "InstanceLock", return_value=lease), patch.object(console, "ScratchLog", return_value=scratch), \
                        patch.object(console, "Store", return_value=store), patch.object(console, "OpenAIAdapter", return_value=adapter), \
                        patch.object(console, "Runtime", return_value=SimpleNamespace(graph=object())), \
                        patch.object(console, "KnowledgeService", return_value=knowledge), patch.object(console, "DiscordBridge", return_value=bridge), \
                        patch.object(console, "prepare_scratch_directory"), patch.object(console, "ObserverServer"), \
                        redirect_stdout(io.StringIO()), self.assertRaises(OSError):
                    await console.serve(config, "synthetic-key", "synthetic-token")
                for resource in (bridge, knowledge, adapter):
                    resource.close.assert_awaited_once()
                scratch.close.assert_called_once()
                store.close.assert_called_once()
                lease.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
