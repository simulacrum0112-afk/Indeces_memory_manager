from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig
from indeces.context import encode
from indeces.contracts import GovernedError, ModelResult
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.store import Store


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


def labels(*, text=None, inp=25, out=8):
    return ModelResult(text if text is not None else encode({"marks": ["alpha", "topic"]}), inp, out, "label-result", 0.001)


class LabelAdapter:
    def __init__(self, outcomes=(), *, gate=None):
        self.outcomes = deque(outcomes)
        self.gate = gate
        self.entered = asyncio.Event()
        self.calls = []
        self.cancelled = False

    async def call(self, stage, instructions, messages, trace_id, schema=None):
        self.calls.append({"stage": stage, "instructions": instructions, "messages": deepcopy(messages),
                           "trace_id": trace_id, "schema": deepcopy(schema)})
        self.entered.set()
        if self.gate is not None and len(self.calls) == 1:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        outcome = self.outcomes.popleft() if self.outcomes else labels()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = SimpleNamespace(name="Indices", knowledge_dir=root / "knowledge", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(poll_seconds=0.01, max_file_bytes=4096, max_files=8, chunk_characters=100,
                                      version_seconds=1.0, version_input_tokens=16384, version_output_tokens=256),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(2048, 32, 1.0)}))
        self.store = Store(root / "state")
        self.graph = MemoryGraph(self.store.db)
        self.scratch = Scratch()
        self.adapter = LabelAdapter()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.quiet = patch("builtins.print")
        self.quiet.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.quiet.stop()
        self.store.close()
        self.directory.cleanup()

    def file(self, text="alpha topic original source", *, name="source.md"):
        path = self.config.knowledge_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def head(self, name="source.md"):
        row = self.store.db.execute("SELECT v.* FROM knowledge_heads h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.path=?", (name,)).fetchone()
        return dict(row) if row else None

    def version(self, source_id):
        return dict(self.store.db.execute("SELECT * FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone())

    def records(self, source_id=None, *, active=None):
        query = "SELECT * FROM memory_records WHERE 1=1"
        arguments = []
        if source_id is not None:
            query += " AND source_id=?"
            arguments.append(source_id)
        if active is not None:
            query += " AND active=?"
            arguments.append(int(active))
        return [dict(row) for row in self.store.db.execute(query, arguments)]

    async def test_file_snapshot_is_pending_until_complete_labels_publish(self):
        path = self.file("alpha topic original source\nexact second line")
        self.service.scan_once()
        pending = self.head()
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["raw_text"], path.read_bytes().decode("utf-8-sig"))
        self.assertEqual(pending["digest"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha topic", 1.0, event_id="before-ready"), [])
        self.assertTrue(await self.service.label_next())
        ready = self.head()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual((ready["input_tokens"], ready["output_tokens"]), (25, 8))
        # An immediate fake transport can finish within a Windows monotonic
        # clock tick. Zero is a valid measurement, not missing accounting.
        self.assertGreaterEqual(ready["elapsed_seconds"], 0)
        self.assertEqual([call["stage"] for call in self.adapter.calls], ["label"])
        request = json.loads(self.adapter.calls[0]["messages"][0]["content"])
        self.assertEqual(request["text"], pending["raw_text"])
        self.assertEqual((request["source_id"], request["digest"]), (ready["source_id"], ready["digest"]))
        record = self.records()[0]
        self.assertEqual((record["text"], record["quote"]), (pending["raw_text"], pending["raw_text"]))
        self.assertEqual(record["author_id"], "knowledge:source.md")
        self.assertTrue(self.graph.retrieve(self.service.scope, [], "alpha topic", 2.0, event_id="after-ready"))

    async def test_unchanged_file_is_never_relabelled(self):
        self.file()
        self.service.scan_once()
        identity = self.head()["source_id"]
        await self.service.label_next()
        for _ in range(4):
            self.service.scan_once()
            self.assertFalse(await self.service.label_next())
        self.assertEqual(self.head()["source_id"], identity)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)

    async def test_replacement_and_reversion_preserve_immutable_raw_versions(self):
        self.file("alpha original A")
        self.service.scan_once()
        first = self.head()["source_id"]
        await self.service.label_next()
        self.file("alpha replacement B")
        self.service.scan_once()
        second = self.head()["source_id"]
        self.assertEqual(self.version(first)["raw_text"], "alpha original A")
        self.assertEqual(self.version(first)["status"], "superseded")
        self.assertEqual(len(self.records(first, active=False)), 1)
        self.assertEqual(self.records(first, active=True), [])
        await self.service.label_next()
        self.file("alpha original A")
        self.service.scan_once()
        third = self.head()["source_id"]
        self.assertEqual(len({first, second, third}), 3)
        await self.service.label_next()
        self.assertEqual([row["source_id"] for row in self.records(active=True)], [third])
        self.assertEqual(self.version(second)["raw_text"], "alpha replacement B")
        self.assertEqual(self.version(third)["raw_text"], "alpha original A")

    async def test_scanned_supersession_during_await_cannot_index_old_version(self):
        self.file("alpha version one")
        self.service.scan_once()
        old = self.head()["source_id"]
        gate = asyncio.Event()
        self.adapter.gate = gate
        task = asyncio.create_task(self.service.label_next())
        await self.adapter.entered.wait()
        self.file("alpha version two")
        self.service.scan_once()
        new = self.head()["source_id"]
        gate.set()
        await task
        self.assertEqual(self.version(old)["status"], "superseded")
        self.assertEqual(self.records(old), [])
        self.assertEqual(self.head()["source_id"], new)
        await self.service.label_next()
        self.assertEqual([r["source_id"] for r in self.records(active=True)], [new])

    async def test_physical_replacement_during_await_is_checked_before_publish(self):
        self.file("alpha version one")
        self.service.scan_once()
        old = self.head()["source_id"]
        gate = asyncio.Event()
        self.adapter.gate = gate
        task = asyncio.create_task(self.service.label_next())
        await self.adapter.entered.wait()
        self.file("alpha version two")  # deliberately before the next watcher poll
        gate.set()
        await task
        self.assertNotEqual(self.version(old)["status"], "ready")
        self.assertEqual(self.records(old, active=True), [])
        self.service.scan_once()
        self.assertNotEqual(self.head()["source_id"], old)

    async def test_physical_deletion_during_await_cannot_publish(self):
        path = self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        gate = asyncio.Event()
        self.adapter.gate = gate
        task = asyncio.create_task(self.service.label_next())
        await self.adapter.entered.wait()
        path.unlink()
        gate.set()
        await task
        self.assertNotEqual(self.version(source_id)["status"], "ready")
        self.assertEqual(self.records(source_id, active=True), [])

    async def test_file_deletion_archives_complete_records_without_erasing_source(self):
        path = self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        await self.service.label_next()
        path.unlink()
        self.service.scan_once()
        self.assertIsNone(self.head())
        self.assertEqual(self.version(source_id)["status"], "superseded")
        self.assertEqual(self.version(source_id)["raw_text"], "alpha topic original source")
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="after-delete"), [])

    async def test_restart_does_not_replay_unknown_http_request(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling',input_tokens=11 WHERE source_id=?", (source_id,))
        restarted = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertEqual(self.version(source_id)["status"], "failed")
        self.assertEqual(self.version(source_id)["error"], "interrupted_unknown_usage")
        self.assertEqual(self.version(source_id)["input_tokens"], 11)
        self.assertFalse(await restarted.label_next())
        restarted.scan_once()
        self.assertFalse(await restarted.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_token_cap_rejects_before_first_dispatch(self):
        self.config.knowledge = replace(self.config.knowledge, version_input_tokens=2047)
        self.file()
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.head()["error"], "knowledge_version_token_limit")
        self.assertEqual(self.records(), [])

    async def test_output_cap_rejects_before_first_dispatch(self):
        self.config.knowledge = replace(self.config.knowledge, version_output_tokens=31)
        self.file()
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.head()["error"], "knowledge_version_token_limit")

    async def test_cumulative_actual_usage_plus_reservation_stops_next_chunk(self):
        self.config.knowledge = replace(self.config.knowledge, version_input_tokens=2048, version_output_tokens=256)
        self.file("alpha " * 30)
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.head()["input_tokens"], 25)
        self.assertEqual(self.head()["output_tokens"], 8)
        self.assertEqual(self.head()["error"], "knowledge_version_token_limit")
        self.assertEqual(self.records(), [])  # partial labels never expose an incomplete document

    async def test_already_spent_time_cap_rejects_before_dispatch(self):
        self.file()
        self.service.scan_once()
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET elapsed_seconds=1.0")
        await self.service.label_next()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.head()["error"], "knowledge_version_time_limit")

    async def test_version_timeout_cancels_label_and_retains_raw_without_index(self):
        self.config.knowledge = replace(self.config.knowledge, version_seconds=0.01)
        self.file()
        self.service.scan_once()
        self.adapter.gate = asyncio.Event()
        await self.service.label_next()
        self.assertTrue(self.adapter.cancelled)
        self.assertEqual(self.head()["error"], "knowledge_version_time_limit")
        self.assertEqual(self.head()["raw_text"], "alpha topic original source")
        self.assertEqual(self.records(), [])
        self.assertFalse(await self.service.label_next())

    async def test_invalid_labels_retain_raw_and_account_known_usage(self):
        self.file()
        self.service.scan_once()
        self.adapter.outcomes.append(labels(text='{"marks":[]}'))
        await self.service.label_next()
        self.assertEqual(self.head()["status"], "failed")
        self.assertEqual(self.head()["error"], "invalid_labels")
        self.assertEqual(self.head()["raw_text"], "alpha topic original source")
        self.assertEqual((self.head()["input_tokens"], self.head()["output_tokens"]), (25, 8))
        self.assertEqual(self.records(), [])
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_provider_failure_keeps_raw_without_index_or_automatic_retry(self):
        self.file()
        self.service.scan_once()
        self.adapter.outcomes.append(GovernedError("provider_network_error"))
        await self.service.label_next()
        self.assertEqual(self.head()["status"], "failed")
        self.assertEqual(self.head()["error"], "provider_network_error")
        self.assertEqual(self.records(), [])
        self.assertFalse(await self.service.label_next())
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_replacement_during_indexing_rolls_back_entire_publication(self):
        path = self.file("alpha old snapshot")
        self.service.scan_once()
        source_id = self.head()["source_id"]
        original_add = self.graph.add

        def changing_add(*args, **kwargs):
            result = original_add(*args, **kwargs)
            path.write_text("alpha newer snapshot", encoding="utf-8")
            return result

        with patch.object(self.graph, "add", side_effect=changing_add):
            await self.service.label_next()
        self.assertNotEqual(self.version(source_id)["status"], "ready")
        self.assertEqual(self.records(source_id), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_static").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_support").fetchone()[0], 0)

    async def test_deadline_overrun_during_indexing_rolls_back_publication(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        original_add = self.graph.add
        overrun = False

        def clock():
            return 3.0 if overrun else 1.0

        def slow_add(*args, **kwargs):
            nonlocal overrun
            result = original_add(*args, **kwargs)
            overrun = True
            return result

        with patch("indeces.knowledge.time.monotonic", side_effect=clock), patch.object(self.graph, "add", side_effect=slow_add):
            await self.service.label_next()
        self.assertNotEqual(self.version(source_id)["status"], "ready")
        self.assertEqual(self.records(source_id), [])
        self.assertEqual(self.version(source_id)["elapsed_seconds"], 2.0)

    async def test_ready_state_commit_failure_rolls_back_graph_rows_atomically(self):
        self.file("alpha immutable source")
        self.service.scan_once()
        source_id = self.head()["source_id"]
        self.store.db.executescript("""
            CREATE TRIGGER reject_ready BEFORE UPDATE ON knowledge_versions
            WHEN NEW.status='ready'
            BEGIN SELECT RAISE(ABORT, 'synthetic ready commit rejection'); END;
        """)
        await self.service.label_next()
        version = self.version(source_id)
        self.assertEqual(version["status"], "failed")
        self.assertEqual(version["raw_text"], "alpha immutable source")
        self.assertEqual((version["input_tokens"], version["output_tokens"]), (25, 8))
        self.assertEqual(self.records(source_id), [])
        for table in ("memory_static", "memory_support"):
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="after-commit-failure"), [])
        self.assertFalse(await self.service.label_next())

    async def test_file_count_overflow_retires_current_heads_and_stale_retrieval(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        await self.service.label_next()
        for index in range(self.config.knowledge.max_files):
            self.file("alpha additional source", name="extra-" + str(index) + ".txt")
        with self.assertRaises(GovernedError) as caught:
            self.service.scan_once()
        self.assertEqual(caught.exception.code, "knowledge_file_count_limit")
        self.assertIsNone(self.head())
        self.assertEqual(self.version(source_id)["status"], "superseded")
        self.assertEqual(self.version(source_id)["raw_text"], "alpha topic original source")
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="after-overflow"), [])

    async def test_restart_archives_active_rows_left_by_legacy_labelling_crash(self):
        self.file()
        self.service.scan_once()
        source_id = self.head()["source_id"]
        self.graph.add(self.service.scope, source_id, "knowledge:source.md", [{
            "text": "alpha topic original source", "quote": "alpha topic original source", "marks": ["alpha", "topic"]}], 1.0)
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling',input_tokens=25,output_tokens=8 WHERE source_id=?", (source_id,))
        restarted = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        version = self.version(source_id)
        self.assertEqual(version["status"], "failed")
        self.assertEqual(version["error"], "interrupted_unknown_usage")
        self.assertEqual(version["raw_text"], "alpha topic original source")
        self.assertEqual((version["input_tokens"], version["output_tokens"]), (25, 8))
        self.assertEqual(self.records(source_id, active=True), [])
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="after-recovery"), [])
        self.assertFalse(await restarted.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_background_watcher_ingests_without_any_response_turn(self):
        self.service.start()
        await asyncio.sleep(0)
        self.file("alpha background update")
        async with asyncio.timeout(1):
            while self.head() is None or self.head()["status"] != "ready":
                await asyncio.sleep(0.005)
        self.assertEqual([call["stage"] for call in self.adapter.calls], ["label"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(len(self.records(active=True)), 1)

    async def test_watcher_observes_new_file_while_label_worker_is_waiting(self):
        self.adapter.gate = asyncio.Event()
        self.file("alpha first document")
        self.service.start()
        async with asyncio.timeout(1):
            await self.adapter.entered.wait()
            self.file("alpha second document", name="second.txt")
            while self.head("second.txt") is None:
                await asyncio.sleep(0.005)
        self.assertEqual(self.head("second.txt")["status"], "pending")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.records(), [])
        self.adapter.gate.set()
        async with asyncio.timeout(1):
            while self.head("second.txt")["status"] != "ready":
                await asyncio.sleep(0.005)
        self.assertEqual(len(self.adapter.calls), 2)

    async def test_unsupported_and_invalid_files_never_reach_model(self):
        self.file("alpha ignored", name="ignored.json")
        path = self.file(name="bad.txt")
        path.write_bytes(b"\xff\xfe\xff")
        self.service.scan_once()
        self.assertIsNone(self.head("ignored.json"))
        self.assertIsNone(self.head("bad.txt"))
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
