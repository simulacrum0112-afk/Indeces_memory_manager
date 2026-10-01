import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.config import load_config
from indeces.contracts import ModelResult
from indeces.ingest import retry_ingestion
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.store import Store
from tests.test_knowledge import LabelAdapter, Scratch, labels


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cleanup_cancellation_keeps_lease_until_resources_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        events = []
        async def knowledge_close():
            entered.set()
            await release.wait()
            events.append("knowledge_closed")
        async def adapter_close():
            events.append("adapter_closed")
        config = SimpleNamespace(discord=SimpleNamespace(guild_id="10"), state_dir=Path("unused"),
                                 scratch_dir=Path("unused"), adapter=SimpleNamespace())
        lease = SimpleNamespace(close=lambda: events.append("lease_closed"))
        store = SimpleNamespace(db=None, close=lambda: events.append("store_closed"))
        scratch = SimpleNamespace(write=lambda *args, **kwargs: None, close=lambda: events.append("scratch_closed"))
        knowledge = SimpleNamespace(retry_failed=lambda *args, **kwargs: {"queued": [], "blocked": [], "unchanged": []}, close=knowledge_close)
        with patch("indeces.config.load_config", return_value=config), patch("indeces.ingest.InstanceLock", return_value=lease), \
             patch("indeces.ingest.Store", return_value=store), patch("indeces.ingest.MemoryGraph"), \
             patch("indeces.ingest.ScratchLog", return_value=scratch), patch("indeces.ingest.OpenAIAdapter", return_value=SimpleNamespace(close=adapter_close)), \
             patch("indeces.ingest.KnowledgeService", return_value=knowledge):
            task = asyncio.create_task(retry_ingestion(config, Path("unused"), key="synthetic-maintenance-key"))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertNotIn("lease_closed", events)
            self.assertNotIn("store_closed", events)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(events, ["knowledge_closed", "adapter_closed", "scratch_closed", "store_closed", "lease_closed"])

    async def test_retry_selected_failure_keeps_success_and_unrelated_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            text = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
            path.write_text(text.replace('guild_id = ""', 'guild_id = "10"'), encoding="utf-8")
            config = load_config(path)
            store = Store(config.state_dir)
            graph = MemoryGraph(store.db)
            adapter = LabelAdapter()
            service = KnowledgeService(config, store, graph, adapter, Scratch())
            (config.knowledge_dir / "old.md").write_text("alpha old published evidence", encoding="utf-8")
            service.scan_once()
            await service.label_next()
            old = store.db.execute("SELECT source_id FROM knowledge_desired WHERE path='old.md'").fetchone()[0]
            old_record = tuple(store.db.execute("SELECT id,text,quote,marks_json FROM memory_records WHERE source_id=?", (old,)).fetchone())
            (config.knowledge_dir / "failed.md").write_text("alpha failed evidence", encoding="utf-8")
            service.scan_once()
            adapter.outcomes.append(labels(text='{"marks":["' + "x" * 41 + '"]}'))
            await service.label_next()
            failed = store.db.execute("SELECT source_id FROM knowledge_desired WHERE path='failed.md'").fetchone()[0]
            (config.knowledge_dir / "pending.md").write_text("pending should not run", encoding="utf-8")
            service.scan_once()
            await service.close()
            store.close()
            replacement = LabelAdapter()
            async def close():
                pass
            replacement.close = close
            with patch("indeces.ingest.OpenAIAdapter", return_value=replacement):
                result = await retry_ingestion(config, path, paths=["failed.md"], key="synthetic-maintenance-key")
                second = await retry_ingestion(config, path, paths=["failed.md"], key="synthetic-maintenance-key")
            self.assertEqual(len(replacement.calls), 1)
            self.assertEqual(result["final"][0]["status"], "ready")
            self.assertEqual(result["final"][0]["source_id"], failed)
            self.assertEqual(second["queued"], [])
            db = sqlite3.connect(config.state_dir / "memory.sqlite3")
            try:
                self.assertEqual(tuple(db.execute("SELECT id,text,quote,marks_json FROM memory_records WHERE source_id=?", (old,)).fetchone()), old_record)
                self.assertEqual(db.execute("SELECT status FROM knowledge_versions WHERE path='pending.md'").fetchone()[0], "pending")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
