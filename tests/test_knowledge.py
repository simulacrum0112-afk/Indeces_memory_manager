from __future__ import annotations

import asyncio
from collections import deque
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter
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

    async def call(self, stage, instructions, messages, trace_id, schema=None, *, input_limit=None):
        self.calls.append({"stage": stage, "instructions": instructions, "messages": deepcopy(messages),
                           "trace_id": trace_id, "schema": deepcopy(schema), "input_limit": input_limit})
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
        if input_limit is not None and outcome.input_tokens > input_limit:
            raise GovernedError("input_token_limit", remote_usage_unknown=False)
        return outcome


class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = SimpleNamespace(name="Indeces", knowledge_dir=root / "knowledge", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(poll_seconds=0.01, max_file_bytes=4096, max_files=8, chunk_characters=100,
                                      version_seconds=1.0, version_input_tokens=16384, version_output_tokens=256),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(2048, 32, 1.0)}))
        self.store = Store(root / "state")
        self.graph = MemoryGraph(self.store.db)
        self.scratch = Scratch()
        self.adapter = LabelAdapter()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.quiet = patch("builtins.print")
        self.printed = self.quiet.start()

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
        row = self.store.db.execute("SELECT v.* FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.path=?", (self.service.scope, name)).fetchone()
        return dict(row) if row else None

    def published(self, name="source.md", *, scope=None):
        row = self.store.db.execute("SELECT v.* FROM knowledge_published p JOIN knowledge_versions v ON v.source_id=p.source_id WHERE p.scope=? AND p.path=?", (scope or self.service.scope, name)).fetchone()
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

    async def ready_source(self, text="alpha topic stable snapshot", *, name="source.md"):
        self.file(text, name=name)
        self.service.scan_once()
        source_id = self.head(name)["source_id"]
        self.assertTrue(await self.service.label_next())
        self.assertEqual(self.version(source_id)["status"], "ready")
        self.assertEqual(self.published(name)["source_id"], source_id)
        return source_id

    def legacy_source(self, *, status="ready", active=True, scope=None, complete=True):
        path = self.file("alpha topic legacy snapshot")
        source_id = "kb:legacy-immutable-version"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.db:
            self.store.db.execute("DELETE FROM knowledge_schema_meta WHERE key='published_snapshots'")
            self.store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,input_tokens,output_tokens,elapsed_seconds,scope) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source_id, "source.md", digest, "alpha topic legacy snapshot", status, 1.0, 25, 8, 0.01, ""))
            self.store.db.execute("INSERT INTO knowledge_heads VALUES('source.md',?)", (source_id,))
            self.store.db.execute("INSERT INTO knowledge_chunks VALUES(?,0,?,0,?)",
                (source_id, "alpha topic legacy snapshot", encode(["alpha", "topic"]) if complete else None))
        if active is not None:
            owner = scope or self.service.scope
            self.graph.add(owner, source_id, "knowledge:source.md", [{
                "text": "alpha topic legacy snapshot", "quote": "alpha topic legacy snapshot", "marks": ["alpha", "topic"]}], 1.0)
            if not active:
                self.graph.deactivate_source(owner, source_id)
        return source_id, digest

    def receipts(self, source_id=None, kind=None):
        return [fields for event, fields in self.scratch.events if event == "knowledge_receipt"
                and (source_id is None or fields["source_id"] == source_id)
                and (kind is None or fields["receipt"] == kind)]

    async def test_file_snapshot_is_pending_until_complete_labels_publish(self):
        path = self.file("alpha topic original source\nexact second line")
        self.service.scan_once()
        pending = self.head()
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(self.published())
        self.assertEqual(pending["raw_text"], path.read_bytes().decode("utf-8-sig"))
        self.assertEqual(pending["digest"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha topic", 1.0, event_id="before-ready"), [])
        self.assertTrue(await self.service.label_next())
        ready = self.head()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(self.published()["source_id"], ready["source_id"])
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
        self.assertEqual(self.version(first)["status"], "ready")
        self.assertEqual(len(self.records(first, active=True)), 1)
        self.assertEqual(self.published()["source_id"], first)
        await self.service.label_next()
        self.assertEqual(self.version(first)["status"], "superseded")
        self.assertEqual(len(self.records(first, active=False)), 1)
        self.assertEqual(self.records(first, active=True), [])
        self.assertEqual(self.published()["source_id"], second)
        self.file("alpha original A")
        self.service.scan_once()
        third = self.head()["source_id"]
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.published()["source_id"], second)
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], third)
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
        self.assertIsNone(self.published())
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

    async def test_input_budget_smaller_than_stage_cap_admits_actual_request(self):
        self.config.knowledge = replace(self.config.knowledge, version_input_tokens=2047)
        self.file()
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0]["input_limit"], 2047)
        self.assertEqual(self.head()["status"], "ready")

    async def test_output_cap_rejects_before_first_dispatch(self):
        self.config.knowledge = replace(self.config.knowledge, version_output_tokens=31)
        self.file()
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.head()["error"], "knowledge_version_token_limit")

    async def test_cumulative_input_counts_use_remaining_allowance(self):
        self.config.knowledge = replace(self.config.knowledge, version_input_tokens=2048, version_output_tokens=256)
        self.file("alpha " * 30)
        self.service.scan_once()
        await self.service.label_next()
        self.assertEqual(len(self.adapter.calls), 2)
        self.assertEqual(self.adapter.calls[1]["input_limit"], 2023)
        self.assertEqual(self.head()["input_tokens"], 50)
        self.assertEqual(self.head()["output_tokens"], 16)
        self.assertEqual(self.head()["status"], "ready")

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
        self.assertIsNone(self.published())
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

    async def test_background_watcher_continuously_publishes_same_file_updates_without_chat(self):
        self.service.start()
        identities = []
        for text in ("alpha topic first background revision", "alpha topic second background revision", "alpha topic third background revision"):
            self.file(text)
            async with asyncio.timeout(2):
                while True:
                    head = self.head()
                    if head is not None and head["status"] == "ready" and head["source_id"] not in identities:
                        break
                    await asyncio.sleep(0.005)
            identities.append(head["source_id"])
            self.assertEqual(self.published()["source_id"], head["source_id"])
            self.assertEqual(head["raw_text"], text)
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual([call["stage"] for call in self.adapter.calls], ["label", "label", "label"])
        self.assertEqual([record["source_id"] for record in self.records(active=True)], [identities[-1]])
        self.assertEqual(len(self.receipts(kind="completed")), 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    async def test_unsupported_and_invalid_files_never_reach_model(self):
        self.file("alpha ignored", name="ignored.json")
        path = self.file(name="bad.txt")
        path.write_bytes(b"\xff\xfe\xff")
        self.service.scan_once()
        self.assertIsNone(self.head("ignored.json"))
        self.assertIsNone(self.head("bad.txt"))
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_last_completed_snapshot_serves_while_replacement_labels_are_waiting(self):
        old = await self.ready_source("alpha topic completed snapshot")
        self.file("beta topic unfinished replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.adapter = LabelAdapter([labels(text=encode({"marks": ["beta", "topic"]}))], gate=asyncio.Event())
        self.service.adapter = self.adapter
        task = asyncio.create_task(self.service.label_next())
        try:
            async with asyncio.timeout(1):
                await self.adapter.entered.wait()
            self.assertEqual(self.head()["status"], "labelling")
            self.assertEqual(self.published()["source_id"], old)
            self.assertEqual(self.version(old)["status"], "ready")
            found = self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="old-during-update")
            self.assertEqual([record["source_id"] for record in found], [old])
            self.assertEqual(found[0]["quote"], "alpha topic completed snapshot")
            self.assertEqual(self.graph.retrieve(self.service.scope, [], "beta", 2.0, event_id="new-unpublished"), [])
            self.assertEqual(self.records(new), [])
        finally:
            self.adapter.gate.set()
            await task
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual(self.version(old)["status"], "superseded")
        self.assertEqual(self.records(old, active=True), [])
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 3.0, event_id="old-after-swap"), [])
        found = self.graph.retrieve(self.service.scope, [], "beta", 3.0, event_id="new-after-swap")
        self.assertEqual([record["source_id"] for record in found], [new])

    async def test_failed_replacement_keeps_completed_snapshot_without_automatic_retry(self):
        old = await self.ready_source()
        self.file("alpha topic failed replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.adapter.outcomes.append(GovernedError("provider_network_error"))
        self.assertTrue(await self.service.label_next())
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.version(old)["status"], "ready")
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.records(new), [])
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(len(self.adapter.calls), 2)

    async def test_newer_update_supersedes_incomplete_work_but_keeps_last_completed_snapshot(self):
        old = await self.ready_source()
        self.file("alpha topic replacement B")
        self.service.scan_once()
        second = self.head()["source_id"]
        self.adapter = LabelAdapter(gate=asyncio.Event())
        self.service.adapter = self.adapter
        task = asyncio.create_task(self.service.label_next())
        try:
            async with asyncio.timeout(1):
                await self.adapter.entered.wait()
            self.file("alpha topic replacement C")
            self.service.scan_once()
            third = self.head()["source_id"]
            self.assertEqual(self.version(second)["status"], "superseded")
            self.assertEqual(self.published()["source_id"], old)
            self.assertEqual(self.version(old)["status"], "ready")
        finally:
            self.adapter.gate.set()
            await task
        self.assertEqual((self.version(second)["input_tokens"], self.version(second)["output_tokens"]), (25, 8))
        self.assertEqual(self.records(second), [])
        self.assertEqual(self.published()["source_id"], old)
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], third)
        self.assertEqual([record["source_id"] for record in self.records(active=True)], [third])

    async def test_partial_labels_never_replace_completed_snapshot_when_later_chunk_fails(self):
        old = await self.ready_source()
        self.file("alpha topic " * 20)
        self.service.scan_once()
        new = self.head()["source_id"]
        self.adapter.outcomes.extend([labels(), GovernedError("provider_network_error")])
        await self.service.label_next()
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual((self.version(new)["input_tokens"], self.version(new)["output_tokens"]), (25, 8))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=? AND marks_json IS NOT NULL", (new,)).fetchone()[0], 1)
        self.assertEqual(self.records(new), [])
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)

    async def test_swap_ready_commit_failure_restores_old_pointer_records_and_graph(self):
        old = await self.ready_source()
        static_before = [tuple(row) for row in self.store.db.execute("SELECT * FROM memory_static ORDER BY scope,a,b")]
        support_before = [tuple(row) for row in self.store.db.execute("SELECT * FROM memory_support ORDER BY scope,a,b")]
        self.file("beta topic replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.adapter.outcomes.append(labels(text=encode({"marks": ["beta", "topic"]})))
        self.store.db.executescript("""
            CREATE TRIGGER reject_new_ready BEFORE UPDATE ON knowledge_versions
            WHEN NEW.status='ready'
            BEGIN SELECT RAISE(ABORT, 'synthetic publication rejection'); END;
        """)
        await self.service.label_next()
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.version(old)["status"], "ready")
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.records(new), [])
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM memory_static ORDER BY scope,a,b")], static_before)
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM memory_support ORDER BY scope,a,b")], support_before)

    async def test_physical_change_during_swap_rolls_back_old_deactivation(self):
        old = await self.ready_source()
        path = self.file("alpha topic desired B")
        self.service.scan_once()
        new = self.head()["source_id"]
        original_add = self.graph.add
        injected = False

        def change_file_during_add(*args, **kwargs):
            nonlocal injected
            result = original_add(*args, **kwargs)
            path.write_text("alpha topic newer C", encoding="utf-8")
            injected = True
            return result

        with patch.object(self.graph, "add", side_effect=change_file_during_add):
            await self.service.label_next()
        self.assertTrue(injected)
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.version(old)["status"], "ready")
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.records(new), [])
        self.assertNotEqual(self.version(new)["status"], "ready")

    async def test_empty_valid_replacement_publishes_zero_records_without_model_call(self):
        old = await self.ready_source()
        self.file(" \n\t")
        self.service.scan_once()
        new = self.head()["source_id"]
        if self.head()["status"] == "pending":
            await self.service.label_next()
        self.assertNotEqual(new, old)
        self.assertEqual(self.head()["status"], "ready")
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual((self.head()["input_tokens"], self.head()["output_tokens"]), (0, 0))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.records(active=True), [])
        self.assertEqual(self.version(old)["status"], "superseded")

    async def test_deletion_retires_both_desired_and_completed_snapshots(self):
        old = await self.ready_source()
        path = self.file("alpha topic pending replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        path.unlink()
        self.service.scan_once()
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.assertEqual(self.version(old)["status"], "superseded")
        self.assertEqual(self.version(new)["status"], "superseded")
        self.assertEqual(self.records(active=True), [])
        self.assertFalse(await self.service.label_next())

    async def test_invalid_update_retires_completed_snapshot_without_erasing_versions(self):
        old = await self.ready_source()
        path = self.file("alpha topic pending replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        path.write_bytes(b"\xff\xfe")
        self.service.scan_once()
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.assertEqual(self.records(active=True), [])
        self.assertEqual(self.version(old)["raw_text"], "alpha topic stable snapshot")
        self.assertEqual(self.version(new)["raw_text"], "alpha topic pending replacement")
        self.assertFalse(await self.service.label_next())

    async def test_restart_preserves_old_publication_with_failed_interrupted_replacement(self):
        old = await self.ready_source()
        self.file("alpha topic interrupted replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling',input_tokens=17 WHERE source_id=?", (new,))
        restarted = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.service = restarted
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.version(new)["error"], "interrupted_unknown_usage")
        self.assertEqual(self.version(new)["input_tokens"], 17)
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)
        restarted.scan_once()
        self.assertFalse(await restarted.label_next())
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_restart_can_finish_owned_pending_replacement_without_archiving_old(self):
        old = await self.ready_source()
        self.file("alpha topic queued replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertEqual(self.head()["status"], "pending")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], new)

    async def test_same_files_in_new_guild_get_independent_owned_versions(self):
        old = await self.ready_source()
        old_scope = self.service.scope
        other_config = SimpleNamespace(**vars(self.config))
        other_config.discord = DiscordConfig("20")
        other_adapter = LabelAdapter()
        other_service = KnowledgeService(other_config, self.store, self.graph, other_adapter, self.scratch)
        try:
            self.assertEqual(self.graph.retrieve(other_service.scope, [], "alpha", 2.0, event_id="other-before-ingest"), [])
            other_service.scan_once()
            pending = self.store.db.execute("SELECT v.* FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.path='source.md'", (other_service.scope,)).fetchone()
            self.assertIsNotNone(pending)
            self.assertNotEqual(pending["source_id"], old)
            self.assertEqual(pending["scope"], other_service.scope)
            await other_service.label_next()
            self.assertEqual(self.published(scope=old_scope)["source_id"], old)
            self.assertEqual(len(self.records(old, active=True)), 1)
            found = self.graph.retrieve(other_service.scope, [], "alpha", 3.0, event_id="other-after-ingest")
            self.assertEqual([record["source_id"] for record in found], [pending["source_id"]])
            self.assertEqual(len(other_adapter.calls), 1)
        finally:
            await other_service.close()

    async def test_recovery_and_retirement_in_new_guild_do_not_mutate_other_guild(self):
        old = await self.ready_source()
        self.file("alpha topic owned pending replacement")
        self.service.scan_once()
        pending_id = self.head()["source_id"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling' WHERE source_id=?", (pending_id,))
        other_config = SimpleNamespace(**vars(self.config))
        other_config.discord = DiscordConfig("20")
        other_service = KnowledgeService(other_config, self.store, self.graph, LabelAdapter(), self.scratch)
        try:
            self.assertEqual(self.version(pending_id)["status"], "labelling")
            self.assertEqual(self.published()["source_id"], old)
            self.file().write_bytes(b"\xff")
            other_service.scan_once()
            self.assertEqual(self.published()["source_id"], old)
            self.assertEqual(len(self.records(old, active=True)), 1)
            self.assertEqual(self.version(pending_id)["status"], "labelling")
        finally:
            await other_service.close()

    async def test_legacy_complete_active_snapshot_is_adopted_without_model_call(self):
        source_id, digest = self.legacy_source()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertEqual(self.head()["source_id"], source_id)
        self.assertEqual(self.head()["scope"], self.service.scope)
        self.assertEqual(self.published()["source_id"], source_id)
        self.assertEqual(self.published()["digest"], digest)
        self.assertEqual(len(self.records(source_id, active=True)), 1)
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_old_schema_without_scope_column_migrates_and_preserves_ready_snapshot(self):
        source_id, _ = self.legacy_source()
        with self.store.db:
            self.store.db.execute("ALTER TABLE knowledge_versions DROP COLUMN scope")
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertIn("scope", {row[1] for row in self.store.db.execute("PRAGMA table_info(knowledge_versions)")})
        self.assertEqual(self.version(source_id)["scope"], self.service.scope)
        self.assertEqual(self.published()["source_id"], source_id)
        self.assertEqual(len(self.records(source_id, active=True)), 1)
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_legacy_inactive_snapshot_is_not_silently_reactivated(self):
        source_id, _ = self.legacy_source(active=False)
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertIsNone(self.published())
        self.assertEqual(self.version(source_id)["error"], "legacy_publication_incomplete")
        self.assertEqual(self.records(source_id, active=True), [])
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_unknown_legacy_request_is_held_until_file_content_changes(self):
        source_id, digest = self.legacy_source(status="labelling", active=None)
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        held = self.store.db.execute("SELECT * FROM knowledge_migration_hold WHERE scope=? AND path='source.md'", (self.service.scope,)).fetchone()
        self.assertEqual(held["source_id"], source_id)
        self.assertEqual(held["digest"], digest)
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.version(source_id)["raw_text"], "alpha topic legacy snapshot")
        self.assertEqual(self.version(source_id)["input_tokens"], 25)
        self.file("alpha topic explicit new snapshot")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.assertNotEqual(new, source_id)
        await self.service.label_next()
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.records(source_id), [])

    async def test_legacy_ready_without_complete_labels_is_not_published(self):
        source_id, _ = self.legacy_source(complete=False)
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertIsNone(self.published())
        self.assertEqual(self.version(source_id)["error"], "legacy_publication_incomplete")
        self.assertEqual(self.records(source_id, active=True), [])
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.assertFalse(await self.service.label_next())

    async def test_legacy_chunks_matching_records_cannot_hide_unlabelled_raw_tail(self):
        source_id, _ = self.legacy_source()
        raw = "alpha topic legacy snapshot\nnonwhitespace unlabelled tail"
        path = self.file(raw)
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET raw_text=?,digest=? WHERE source_id=?",
                (raw, hashlib.sha256(path.read_bytes()).hexdigest(), source_id))
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertIsNone(self.published())
        self.assertEqual(self.version(source_id)["error"], "legacy_publication_incomplete")
        self.assertEqual(self.version(source_id)["raw_text"], raw)
        self.assertEqual(self.records(source_id, active=True), [])
        self.assertEqual(len(self.records(source_id, active=False)), 1)
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(self.adapter.calls, [])

    async def test_legacy_other_guild_snapshot_is_preserved_without_cross_guild_transfer(self):
        other_scope = "20:knowledge"
        source_id, _ = self.legacy_source(scope=other_scope)
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertIsNone(self.head())
        self.assertIsNone(self.published())
        self.assertEqual(self.published(scope=other_scope)["source_id"], source_id)
        self.assertEqual(self.version(source_id)["scope"], other_scope)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2.0, event_id="legacy-other-guild"), [])
        self.service.scan_once()
        new = self.head()["source_id"]
        self.assertNotEqual(new, source_id)
        await self.service.label_next()
        self.assertEqual(self.published(scope=other_scope)["source_id"], source_id)
        self.assertEqual(self.published()["source_id"], new)
        self.assertEqual(len(self.records(source_id, active=True)), 1)

    async def test_completion_receipt_matches_durable_published_version_and_accounting(self):
        output = StringIO()
        self.quiet.stop()
        try:
            with redirect_stdout(output):
                source_id = await self.ready_source()
        finally:
            self.printed = self.quiet.start()
        self.assertEqual(output.getvalue(), "")
        version = self.version(source_id)
        receipts = self.receipts(source_id)
        self.assertEqual([receipt["receipt"] for receipt in receipts], ["queued", "started", "progress", "completed"])
        done = receipts[-1]
        self.assertEqual(done["scope"], self.service.scope)
        self.assertEqual(done["path"], version["path"])
        self.assertEqual(done["digest"], version["digest"])
        self.assertEqual(done["reply_source_id"], self.published()["source_id"])
        self.assertEqual((done["labelled_chunks"], done["total_chunks"], done["records"]), (1, 1, 1))
        self.assertEqual((done["input_tokens"], done["output_tokens"], done["elapsed_seconds"]),
                         (version["input_tokens"], version["output_tokens"], version["elapsed_seconds"]))

    async def test_partial_label_failure_keeps_audit_and_old_publication_without_console_receipts(self):
        old = await self.ready_source()
        self.file("alpha topic replacement material " * 8)
        self.adapter.outcomes.extend([labels(), GovernedError("provider_network_error")])
        calls_before = len(self.adapter.calls)
        output = StringIO()
        self.quiet.stop()
        try:
            with redirect_stdout(output):
                self.service.scan_once()
                new = self.head()["source_id"]
                self.assertTrue(await self.service.label_next())
        finally:
            self.printed = self.quiet.start()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual((self.version(new)["status"], self.version(new)["error"]),
                         ("failed", "provider_network_error"))
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.records(new), [])
        self.assertEqual(len(self.adapter.calls) - calls_before, 2)
        receipts = self.receipts(new)
        self.assertEqual([receipt["receipt"] for receipt in receipts],
                         ["queued", "started", "progress", "failed"])
        failed = receipts[-1]
        self.assertEqual((failed["reply_source_id"], failed["labelled_chunks"], failed["records"]),
                         (old, 1, 0))
        self.assertGreater(failed["total_chunks"], 1)
        self.assertEqual((failed["input_tokens"], failed["output_tokens"]), (25, 8))
        self.assertEqual(failed["code"], "provider_network_error")
        self.assertTrue(failed["remote_usage_unknown"])
        labelled = [fields for event, fields in self.scratch.events
                    if event == "knowledge_chunk_labelled" and fields["source_id"] == new]
        self.assertEqual(len(labelled), 1)
        self.assertIn("replacement material", labelled[0]["quote"])
        self.assertEqual(labelled[0]["marks"], ["alpha", "topic"])

    async def test_adapter_error_metadata_preserves_known_absence_of_generation_usage(self):
        old = await self.ready_source()
        for code in ("model_slot_timeout", "stage_timeout"):
            with self.subTest(code=code):
                self.file("alpha topic replacement " + code)
                self.adapter.outcomes.append(GovernedError(code, remote_usage_unknown=False))
                self.service.scan_once()
                new = self.head()["source_id"]
                self.assertTrue(await self.service.label_next())
                self.assertEqual((self.version(new)["status"], self.version(new)["error"]), ("failed", code))
                self.assertEqual(self.published()["source_id"], old)
                self.assertEqual(self.records(new), [])
                failed = self.receipts(new, "failed")[-1]
                self.assertFalse(failed["remote_usage_unknown"])
                self.assertEqual((failed["input_tokens"], failed["output_tokens"]), (0, 0))

    async def test_cancelled_model_slot_wait_does_not_claim_unknown_generation_usage(self):
        old = await self.ready_source()
        self.file("alpha topic cancelled before generation")
        self.service.scan_once()
        new = self.head()["source_id"]
        requests = []

        async def transport(path, payload):
            requests.append(path)
            return {"input_tokens": 10}

        adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=transport)
        self.service.adapter = adapter
        await adapter._lock.acquire()
        try:
            task = asyncio.create_task(self.service.label_next())
            await asyncio.sleep(0)
            self.assertTrue(any(event == "call_start" for event, _ in self.scratch.events))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            adapter._lock.release()
            await adapter.close()
        self.assertEqual(requests, [])
        self.assertEqual((self.version(new)["status"], self.version(new)["error"]), ("failed", "interrupted"))
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.records(new), [])
        self.assertFalse(self.receipts(new, "cancelled")[-1]["remote_usage_unknown"])
        job_end = [fields for event, fields in self.scratch.events
                   if event == "label_job_end" and fields["source_id"] == new]
        self.assertFalse(job_end[-1]["remote_usage_unknown"])

    async def test_rejected_generation_accounts_confirmed_usage_once_without_publication(self):
        old = await self.ready_source()
        for case, code, out in (("incomplete", "incomplete_response", 6),
                                ("breach", "provider_token_limit_breach", 33),
                                ("invalid_output", "invalid_output", 6)):
            with self.subTest(case=case):
                self.file("alpha topic rejected generation " + case)
                self.service.scan_once()
                new = self.head()["source_id"]
                requests = []

                async def transport(path, payload):
                    requests.append(path)
                    if path.endswith("input_tokens"):
                        return {"input_tokens": 17}
                    return {"id": "synthetic-rejected", "status": "incomplete" if case == "incomplete" else "completed",
                            "usage": {"input_tokens": 17, "output_tokens": out},
                            "output": None if case == "invalid_output" else [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": '{"marks":["alpha"]}'}]}]}

                adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=transport)
                self.service.adapter = adapter
                try:
                    self.assertTrue(await self.service.label_next())
                    self.assertFalse(await self.service.label_next())
                finally:
                    await adapter.close()
                version = self.version(new)
                self.assertEqual((version["status"], version["error"]), ("failed", code))
                self.assertEqual((version["input_tokens"], version["output_tokens"]), (17, out))
                self.assertEqual(self.published()["source_id"], old)
                self.assertEqual(self.records(new), [])
                self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
                receipt = self.receipts(new, "failed")[-1]
                self.assertEqual((receipt["input_tokens"], receipt["output_tokens"]), (17, out))
                self.assertFalse(receipt["remote_usage_unknown"])
                accounting = [fields for event, fields in self.scratch.events
                              if event == "label_job_accounting" and fields["source_id"] == new][-1]
                self.assertEqual((accounting["input_tokens"], accounting["output_tokens"]), (17, out))

    async def test_confirmed_usage_survives_original_adapter_audit_exception(self):
        old = await self.ready_source()
        original_write = self.scratch.write
        for fail_event in ("http_response", "call_end"):
            with self.subTest(fail_event=fail_event):
                self.file("alpha topic adapter audit failure " + fail_event)
                self.service.scan_once()
                new = self.head()["source_id"]
                failure = OSError("synthetic adapter audit failure")
                failures = 0

                def write(event, **fields):
                    nonlocal failures
                    if event == fail_event and (event != "http_response" or fields["path"] == "/responses"):
                        failures += 1
                        raise failure
                    original_write(event, **fields)

                async def transport(path, payload):
                    return {"input_tokens": 17} if path.endswith("input_tokens") else {
                        "id": "synthetic-audit", "status": "completed",
                        "usage": {"input_tokens": 17, "output_tokens": 6},
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": '{"marks":["alpha"]}'}]}]}

                adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=transport)
                self.service.adapter = adapter
                self.scratch.write = write
                try:
                    self.assertTrue(await self.service.label_next())
                finally:
                    self.scratch.write = original_write
                    await adapter.close()
                self.assertEqual(failures, 1)
                self.assertEqual(failure.known_usage, {"input_tokens": 17, "output_tokens": 6})
                self.assertFalse(failure.remote_usage_unknown)
                self.assertEqual(adapter._circuits["label"], {"failures": 0, "until": 0.0})
                version = self.version(new)
                self.assertEqual((version["status"], version["error"]), ("failed", "OSError"))
                self.assertEqual((version["input_tokens"], version["output_tokens"]), (17, 6))
                self.assertEqual(self.published()["source_id"], old)
                self.assertEqual(self.records(new), [])
                self.assertFalse(self.receipts(new, "failed")[-1]["remote_usage_unknown"])

    async def test_cancellation_after_generation_preserves_confirmed_version_usage(self):
        old = await self.ready_source()
        self.file("alpha topic cancelled after confirmed usage")
        self.service.scan_once()
        new = self.head()["source_id"]
        original_write = self.scratch.write

        def write(event, **fields):
            original_write(event, **fields)
            if event == "http_response" and fields["path"] == "/responses":
                raise asyncio.CancelledError()

        async def transport(path, payload):
            return {"input_tokens": 17} if path.endswith("input_tokens") else {
                "id": "synthetic-cancelled", "status": "completed",
                "usage": {"input_tokens": 17, "output_tokens": 6},
                "output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": '{"marks":["alpha"]}'}]}]}

        adapter = OpenAIAdapter(self.config.adapter, self.scratch, request=transport)
        self.service.adapter = adapter
        self.scratch.write = write
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.label_next()
        finally:
            self.scratch.write = original_write
            await adapter.close()
        version = self.version(new)
        self.assertEqual((version["status"], version["error"]), ("failed", "interrupted"))
        self.assertEqual((version["input_tokens"], version["output_tokens"]), (17, 6))
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.records(new), [])
        receipt = self.receipts(new, "cancelled")[-1]
        self.assertEqual((receipt["input_tokens"], receipt["output_tokens"]), (17, 6))
        self.assertFalse(receipt["remote_usage_unknown"])

    async def test_failed_publication_has_only_failed_receipt_with_prior_reply_version(self):
        old = await self.ready_source()
        self.file("alpha topic rejected replacement")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.store.db.executescript("""
            CREATE TRIGGER reject_receipt_ready BEFORE UPDATE ON knowledge_versions
            WHEN NEW.status='ready'
            BEGIN SELECT RAISE(ABORT, 'synthetic rejection before completion receipt'); END;
        """)
        await self.service.label_next()
        self.assertEqual(self.receipts(new, "completed"), [])
        failed = self.receipts(new, "failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reply_source_id"], old)
        self.assertEqual(failed[0]["input_tokens"], 25)
        self.assertEqual(failed[0]["output_tokens"], 8)
        self.assertEqual(self.published()["source_id"], old)

    async def test_cancelled_replacement_preserves_prior_snapshot_and_reports_unknown_request_usage(self):
        old = await self.ready_source()
        self.file("alpha topic cancelled replacement")
        self.adapter = LabelAdapter(gate=asyncio.Event())
        self.service.adapter = self.adapter
        output = StringIO()
        self.quiet.stop()
        try:
            with redirect_stdout(output):
                self.service.scan_once()
                new = self.head()["source_id"]
                task = asyncio.create_task(self.service.label_next())
                async with asyncio.timeout(1):
                    await self.adapter.entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            self.printed = self.quiet.start()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.version(new)["error"], "interrupted_unknown_usage")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.records(new), [])
        cancelled = self.receipts(new, "cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["reply_source_id"], old)
        self.assertTrue(cancelled[0]["remote_usage_unknown"])
        job_end = [fields for event, fields in self.scratch.events if event == "label_job_end" and fields["source_id"] == new]
        self.assertTrue(job_end[-1]["remote_usage_unknown"])
        self.assertEqual(self.receipts(new, "completed"), [])

    async def test_queued_audit_failure_does_not_retire_completed_publication(self):
        old = await self.ready_source()
        self.file("alpha topic queued audit failure")
        write = self.scratch.write
        injected = False

        def fail_queued_receipt(event, **fields):
            nonlocal injected
            if event == "knowledge_receipt" and fields["receipt"] == "queued":
                injected = True
                raise OSError("synthetic queued audit failure")
            return write(event, **fields)

        with patch.object(self.scratch, "write", side_effect=fail_queued_receipt):
            with self.assertRaises(OSError):
                self.service.scan_once()
        self.assertTrue(injected)
        new = self.head()["source_id"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.head()["status"], "pending")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.version(old)["status"], "ready")
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.receipts(new, "completed"), [])
        self.assertFalse(any(event == "knowledge_retired" for event, _ in self.scratch.events))

    async def test_fatal_worker_receipt_failure_pauses_watcher_and_preserves_old_publication(self):
        old = await self.ready_source()
        self.file("alpha topic started audit failure")
        self.service.scan_once()
        new = self.head()["source_id"]
        write = self.scratch.write
        injected = False

        def fail_started_receipt(event, **fields):
            nonlocal injected
            if event == "knowledge_receipt" and fields["receipt"] == "started":
                injected = True
                raise OSError("synthetic private diagnostic never printed")
            return write(event, **fields)

        with patch.object(self.scratch, "write", side_effect=fail_started_receipt):
            self.service.start()
            async with asyncio.timeout(1):
                while self.service.background_error is None or not all(task.done() for task in self.service._tasks):
                    await asyncio.sleep(0.005)
        self.assertTrue(injected)
        self.assertEqual(self.service.background_error, "OSError")
        watcher = next(task for task in self.service._tasks if task.get_name() == "indeces-knowledge-watch")
        self.assertTrue(watcher.cancelled())
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.receipts(new, "completed"), [])
        stopped = [fields for event, fields in self.scratch.events if event == "knowledge_background_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["code"], "OSError")
        self.assertFalse(stopped[0]["automatically_replayed"])
        printed = "\n".join(str(call.args[0]) for call in self.printed.call_args_list)
        self.assertIn("后台已暂停", printed)
        self.assertNotIn("synthetic private diagnostic", printed)
        self.assertEqual(len(self.adapter.calls), 1)
        await self.service.close()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.assertEqual(self.version(new)["error"], "interrupted_unknown_usage")
        self.assertFalse(await self.service.label_next())
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.published()["source_id"], old)

    async def test_fatal_watcher_log_failure_cancels_unknown_request_without_replaying_it(self):
        old = await self.ready_source()
        self.file("alpha topic interrupted background request")
        self.service.scan_once()
        new = self.head()["source_id"]
        self.adapter = LabelAdapter(gate=asyncio.Event())
        self.service.adapter = self.adapter
        self.service.start()
        async with asyncio.timeout(1):
            await self.adapter.entered.wait()
        write = self.scratch.write
        injected = False

        def fail_scan_error_receipt(event, **fields):
            nonlocal injected
            if event == "knowledge_scan_failed":
                injected = True
                raise OSError("synthetic watcher private diagnostic")
            return write(event, **fields)

        with patch.object(self.service, "scan_once", side_effect=RuntimeError("synthetic scan private diagnostic")), \
                patch.object(self.scratch, "write", side_effect=fail_scan_error_receipt):
            async with asyncio.timeout(1):
                while self.service.background_error is None or not all(task.done() for task in self.service._tasks):
                    await asyncio.sleep(0.005)
        self.assertTrue(injected)
        self.assertEqual(self.service.background_error, "OSError")
        self.assertTrue(self.adapter.cancelled)
        self.assertEqual(self.version(new)["status"], "failed")
        self.assertEqual(self.version(new)["error"], "interrupted_unknown_usage")
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)
        self.assertEqual(self.receipts(new, "completed"), [])
        self.assertTrue(self.receipts(new, "cancelled")[0]["remote_usage_unknown"])
        printed = "\n".join(str(call.args[0]) for call in self.printed.call_args_list)
        self.assertNotIn("private diagnostic", printed)
        await self.service.close()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.service.scan_once()
        self.assertFalse(await self.service.label_next())
        self.assertEqual(len(self.adapter.calls), 1)

    async def test_expected_background_shutdown_is_not_reported_as_supervisor_failure(self):
        old = await self.ready_source()
        self.file("alpha topic pending normal shutdown")
        self.service.scan_once()
        self.adapter = LabelAdapter(gate=asyncio.Event())
        self.service.adapter = self.adapter
        self.service.start()
        async with asyncio.timeout(1):
            await self.adapter.entered.wait()
        await self.service.close()
        self.assertIsNone(self.service.background_error)
        self.assertFalse(any(event == "knowledge_background_stopped" for event, _ in self.scratch.events))
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(len(self.records(old, active=True)), 1)

    async def test_empty_snapshot_changed_during_publication_keeps_previous_complete_snapshot(self):
        old = await self.ready_source()
        path = self.file("")
        original_add = self.graph.add
        injected = False

        def change_empty_file_during_add(*args, **kwargs):
            nonlocal injected
            result = original_add(*args, **kwargs)
            path.write_text("alpha topic newer nonempty snapshot", encoding="utf-8")
            injected = True
            return result

        with patch.object(self.graph, "add", side_effect=change_empty_file_during_add):
            self.service.scan_once()
        self.assertTrue(injected)
        self.assertEqual(self.published()["source_id"], old)
        self.assertEqual(self.version(old)["status"], "ready")
        self.assertEqual(len(self.records(old, active=True)), 1)
        new = self.head()["source_id"]
        self.assertEqual(self.receipts(new, "completed"), [])
        self.assertEqual(self.receipts(new, "failed")[0]["reply_source_id"], old)


if __name__ == "__main__":
    unittest.main()
