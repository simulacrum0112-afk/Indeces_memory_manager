"""Isolated bounded reingestion accounting/publication tests; no provider calls."""
from __future__ import annotations

from collections import deque
from contextlib import closing, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, PdfConfig
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.reingest import BoundedReingest
from indeces.store import Store
from tests.test_adapter import completed
from tests.test_knowledge import LabelAdapter, Scratch
from tests.test_knowledge_pdf import conversion


class ReingestTransport:
    """Count and generation results are synthetic, including deliberately lost replies."""

    def __init__(self):
        self.requests = []
        self.count_outcomes = deque()
        self.generation_outcomes = deque()

    async def __call__(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        outcomes = self.count_outcomes if path.endswith("input_tokens") else self.generation_outcomes
        value = outcomes.popleft() if outcomes else (
            {"input_tokens": 25} if path.endswith("input_tokens") else
            completed(json.dumps({"marks": ["volcano", "OOH*"]}), inp=25, out=8))
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)

    @property
    def generation_requests(self):
        return [payload for path, payload in self.requests if path == "/responses"]


class BoundedReingestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            scratch_dir=self.root / "scratch", knowledge_dir=self.root / "knowledge",
            discord=DiscordConfig("10"), knowledge=KnowledgeConfig(), pdf=PdfConfig(),
            adapter=AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1",
                                  {"label": Budget(4096, 512, 15)}))
        self.store = Store(self.config.state_dir)
        self.graph = MemoryGraph(self.store.db)
        self.scratch = Scratch()
        # Schema-only construction in an empty synthetic store. The production
        # maintenance path must never construct this mutating service.
        self.schema = KnowledgeService(self.config, self.store, self.graph, LabelAdapter(), self.scratch)
        self.transport = ReingestTransport()
        adapter_config = replace(self.config.adapter, budgets={"label": Budget(4096, 512, 45)})
        self.adapter = OpenAIAdapter(adapter_config, self.scratch, request=self.transport)
        self.source_ids = []
        self.paths = []
        self.runner = None

    async def asyncTearDown(self):
        await self.schema.close()
        await self.adapter.close()
        self.store.close()
        self.directory.cleanup()

    def fixture(self, counts=(4, 4, 4, 4)):
        for number, count in enumerate(counts):
            source_id, relative = f"kb:old-{number}", f"papers/paper-{number}_Norskov.pdf"
            raw = f"%PDF-1.4\nsynthetic immutable PDF {number}".encode()
            digest = hashlib.sha256(raw).hexdigest()
            body = "".join((f"paper {number} block {index} computational hydrogen electrode scaling relation "
                            "volcano HOO* OOH* ").ljust(400, ".") for index in range(count))
            header = "## PDF page 1\n\n"
            converted = conversion(raw, body[:count * 400 - len(header) - 2])
            markdown = converted["markdown"]
            self.assertEqual(len(markdown), count * 400)
            path = self.config.knowledge_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            with self.store.db:
                self.store.db.execute("""INSERT INTO knowledge_versions
                    (source_id,path,digest,raw_text,status,created_at,input_tokens,output_tokens,elapsed_seconds,error,scope)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (source_id, relative, digest, markdown, "failed", number + 1,
                         100 + number, 20 + number, 50.5 + number, "stage_timeout", "10:knowledge"))
                self.store.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?)", ("10:knowledge", relative, source_id))
                self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                    (source_id, raw, json.dumps(converted["metadata"], sort_keys=True), str(self.root / "old-draft.md")))
                for index in range(count):
                    self.store.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)", (source_id, index,
                        markdown[index * 400:(index + 1) * 400], index * 400,
                        json.dumps(["legacy saved label"]) if index == 0 else None))
                self.store.db.execute("""INSERT INTO knowledge_file_audit
                    (scope,path,source_id,suffix,byte_count,state,stage,error,details_json,observed_at,remote_usage_unknown)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("10:knowledge", relative, source_id, ".pdf", len(raw), "failed",
                         "label", "stage_timeout", json.dumps({"failed_chunk_index": 1, "labelled_chunks": 1,
                         "total_chunks": count}), number + 1, 1))
            self.source_ids.append(source_id)
            self.paths.append(relative)
        self.old = self.old_snapshot()
        with redirect_stdout(io.StringIO()):
            self.runner = BoundedReingest(self.config, self.store.db, self.graph, self.adapter, self.scratch)
        return self.runner

    def old_snapshot(self):
        return {table: [tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                for table in ("knowledge_versions", "knowledge_chunks", "knowledge_pdf_versions",
                              "knowledge_file_audit", "knowledge_root_quarantine", "knowledge_root_bindings")}

    def assert_old_preserved(self):
        for table, rows in self.old.items():
            current = self.old_snapshot()[table]
            for row in rows:
                self.assertIn(row, current, f"modified old row in {table}")
            if table in ("knowledge_file_audit", "knowledge_root_quarantine", "knowledge_root_bindings"):
                self.assertEqual(current, rows, f"modified old metadata table {table}")

    async def run_phase(self, batch_id, **kwargs):
        with redirect_stdout(io.StringIO()):
            return await self.runner.run(batch_id, **kwargs)

    def generation_order(self):
        return [json.loads(payload["input"][0]["content"]) for payload in self.transport.generation_requests]

    def interrupted_call(self, batch, *, generation, usage=None, seconds=45,
                         call_key="synthetic-crashed-call", document_index=0):
        attempt = self.runner.summary(batch)["documents"][document_index]["attempt_id"]
        reserved = (25, 512) if generation and usage is None else (0, 0)
        actual = usage if usage is not None else (None, None)
        with self.store.db:
            self.store.db.execute("""INSERT INTO reingest_calls
                (call_key,attempt_id,chunk_index,adapter_call_id,status,phase,started_at,seconds_limit,
                 reserved_input_tokens,reserved_output_tokens,actual_input_tokens,actual_output_tokens)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (call_key, attempt, 0, "synthetic-provider-call", "running",
                    "generation" if generation else "input_count", time.time(), seconds, *reserved, *actual))
            self.store.db.execute("""INSERT INTO reingest_requests
                (client_request_id,call_key,path,status,created_at,provider_request_id,response_id,usage_json)
                VALUES(?,?,?,?,?,?,?,?)""", ("synthetic-client-" + call_key, call_key,
                    "/responses" if generation else "/responses/input_tokens", "received" if usage else "intent",
                    time.time(), "synthetic-provider-id" if usage else None,
                    "synthetic-response-id" if usage else None,
                    json.dumps({"input_tokens": usage[0], "output_tokens": usage[1]}) if usage else None))
            self.store.db.execute("UPDATE reingest_batches SET status='pilot_running' WHERE batch_id=?", (batch,))
        return call_key

    async def test_operator_grant_required_and_duplicate_sources_rejected_without_mutation(self):
        runner = self.fixture()
        for ids, confirmed in ((self.source_ids, False), ([], True),
                               ([self.source_ids[0]] * 4, True)):
            with self.subTest(ids=ids, confirmed=confirmed):
                with self.assertRaises((ValueError, GovernedError)):
                    runner.grant(ids, operator_confirmed=confirmed)
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_one_document_grant_pilot_and_publish_preserve_old_ledger(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids[:1], operator_confirmed=True)
        pilot = await runner.run(batch, phase='pilot')
        self.assertTrue(pilot['pilot_passed'])
        self.assertEqual(pilot['documents'][0]['labelled_chunks'], 3)
        self.assertEqual(pilot['documents'][0]['published'], 0)
        done = await runner.run(batch, phase='complete')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(len(done['documents']), 1)
        self.assertEqual(done['documents'][0]['published'], 1)
        self.assert_old_preserved()

    async def test_additive_schema_is_preceded_by_recoverable_sqlite_backup(self):
        self.fixture()
        backups = list((self.config.state_dir / "backups").glob("*"))
        self.assertTrue(backups)
        import sqlite3
        readable = []
        for path in backups:
            if not path.is_file():
                continue
            with closing(sqlite3.connect(path)) as backup:
                tables = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "knowledge_versions" in tables:
                    readable.append(path)
                    self.assertNotIn("reingest_batches", tables)
                    self.assertEqual(backup.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 4)
        self.assertTrue(readable)
        self.assert_old_preserved()

    async def test_repeated_operator_grant_cannot_allocate_another_spend_envelope(self):
        runner = self.fixture()
        first = runner.grant(self.source_ids, operator_confirmed=True)
        try:
            repeated = runner.grant(list(reversed(self.source_ids)), operator_confirmed=True)
        except (ValueError, GovernedError):
            repeated = first
        self.assertEqual(repeated, first)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reingest_batches").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM reingest_documents").fetchone()[0], 4)
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_pilot_is_round_robin_and_completion_labels_all_388_fresh_once(self):
        runner = self.fixture((117, 91, 81, 99))
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        pilot = await self.run_phase(batch, phase="pilot")
        self.assertTrue(pilot["pilot_passed"])
        self.assertEqual(sum(item["labelled_chunks"] for item in pilot["documents"]), 12)
        self.assertTrue(all(not item["published"] for item in pilot["documents"]))
        order = self.generation_order()
        self.assertEqual([item["path"] for item in order], self.paths * 3)
        self.assertEqual([item["chunk_index"] for item in order], [0] * 4 + [1] * 4 + [2] * 4)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        self.assert_old_preserved()
        final = await self.run_phase(batch, phase="complete")
        self.assertTrue(all(item["published"] for item in final["documents"]))
        self.assertEqual(sum(item["labelled_chunks"] for item in final["documents"]), 388)
        self.assertEqual(len(self.transport.generation_requests), 388)
        self.assertEqual(sum(item["input_tokens"] for item in final["documents"]), 388 * 25)
        self.assertEqual(sum(item["output_tokens"] for item in final["documents"]), 388 * 8)
        for payload in self.transport.generation_requests:
            self.assertEqual(payload["model"], "gpt-6.1-sol")
            self.assertEqual(payload["reasoning"], {"effort": "medium"})
            self.assertEqual(payload["max_output_tokens"], 512)
            self.assertFalse(payload["store"])
        self.assert_old_preserved()
        published = self.store.db.execute("""SELECT v.* FROM knowledge_published p
            JOIN knowledge_versions v ON v.source_id=p.source_id WHERE p.scope='10:knowledge'""").fetchall()
        self.assertEqual(len(published), 4)
        self.assertTrue(all(row["source_id"] not in self.source_ids and row["status"] == "ready" for row in published))
        self.assertTrue(all(set(json.loads(row[0])) == {"volcano", "OOH*"} for row in self.store.db.execute(
            "SELECT marks_json FROM knowledge_chunks WHERE source_id NOT IN (?,?,?,?)", self.source_ids)))
        call_seconds = self.store.db.execute("SELECT SUM(elapsed_seconds) FROM reingest_calls").fetchone()[0]
        self.assertGreater(final["totals"]["elapsed_seconds"], call_seconds)
        local_times = self.store.db.execute("SELECT details_json FROM reingest_events WHERE event='local_execution_time'").fetchall()
        self.assertGreaterEqual(sum(json.loads(row[0]).get("phase") == "publication" for row in local_times), 4)
        fresh_adapter = LabelAdapter()
        reopened = KnowledgeService(self.config, self.store, self.graph, fresh_adapter, self.scratch)
        try:
            reopened.scan_once()
            self.assertFalse(await reopened.label_next())
            self.assertEqual(fresh_adapter.calls, [])
            self.assert_old_preserved()
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE active=1").fetchone()[0], 388)
            self.assertTrue(runner.summary(batch)["old_records_preserved"])
        finally:
            await reopened.close()
        before = len(self.transport.requests)
        await self.run_phase(batch, phase="complete")
        self.assertEqual(len(self.transport.requests), before)

    async def test_three_chunk_documents_still_require_complete_phase_before_publication(self):
        runner = self.fixture((3, 3, 3, 3))
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        pilot = await self.run_phase(batch, phase="pilot")
        self.assertTrue(pilot["pilot_passed"])
        self.assertEqual(sum(row["labelled_chunks"] for row in pilot["documents"]), 12)
        self.assertFalse(any(row["published"] for row in pilot["documents"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        final = await self.run_phase(batch, phase="complete")
        self.assertTrue(all(row["published"] for row in final["documents"]))
        self.assertEqual(len(self.transport.generation_requests), 12)
        self.assert_old_preserved()

    async def test_generation_unknown_stops_entire_batch_keeps_reservation_and_no_replay(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        self.transport.generation_outcomes.append(GovernedError("provider_network_error"))
        result = await self.run_phase(batch, phase="pilot")
        self.assertEqual(len(self.transport.generation_requests), 1)
        self.assertFalse(result["pilot_passed"])
        first = result["documents"][0]
        self.assertEqual((first["input_tokens"], first["output_tokens"]), (0, 0))
        self.assertGreater(first["reserved_input_tokens"], 0)
        self.assertEqual(first["reserved_output_tokens"], 512)
        self.assertGreater(first["elapsed_seconds"], 0)
        self.assertTrue(all(not row["published"] for row in result["documents"]))
        before = len(self.transport.requests)
        for resume_known in (False, True):
            try:
                await self.run_phase(batch, phase="pilot", resume_known=resume_known)
            except (ValueError, GovernedError):
                pass
        self.assertEqual(len(self.transport.requests), before)
        self.assert_old_preserved()

    async def test_count_failure_has_no_unknown_generation_and_requires_explicit_known_resume(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        self.transport.count_outcomes.append(GovernedError("provider_network_error"))
        result = await self.run_phase(batch, phase="pilot")
        self.assertFalse(result["pilot_passed"])
        self.assertEqual(self.transport.generation_requests, [])
        self.assertTrue(all(not row["reserved_output_tokens"] for row in result["documents"]))
        before = len(self.transport.requests)
        try:
            await self.run_phase(batch, phase="pilot")
        except (ValueError, GovernedError):
            pass
        self.assertEqual(len(self.transport.requests), before)
        recovered = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertTrue(recovered["pilot_passed"])
        self.assertEqual(len(self.transport.generation_requests), 12)
        self.assert_old_preserved()

    async def test_invalid_labels_charge_confirmed_usage_and_never_publish_partial_document(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        self.transport.generation_outcomes.append(completed('{"marks":[]}', inp=25, out=8))
        result = await self.run_phase(batch, phase="pilot")
        first = result["documents"][0]
        self.assertEqual((first["input_tokens"], first["output_tokens"], first["labelled_chunks"]), (25, 8, 0))
        self.assertEqual((first["reserved_input_tokens"], first["reserved_output_tokens"]), (0, 0))
        self.assertEqual(len(self.transport.generation_requests), 1)
        self.assertFalse(any(row["published"] for row in result["documents"]))
        response = self.store.db.execute("SELECT * FROM reingest_requests WHERE path='/responses'").fetchone()
        self.assertEqual(response["status"], "received")
        self.assertEqual(response["response_id"], "response-1")
        self.assertEqual(json.loads(response["usage_json"]), {"input_tokens": 25, "output_tokens": 8})
        failure = self.store.db.execute("SELECT phase,error FROM reingest_calls").fetchone()
        self.assertEqual(tuple(failure), ("label_validation", "invalid_labels"))
        self.assert_old_preserved()

    async def test_response_audit_failure_preserves_received_actual_usage_without_inventing_unknown(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        original = runner._audit

        def failing_audit_factory(*args):
            observer = original(*args)

            def observer_with_failure(event, **fields):
                if event == "response_received" and fields["path"] == "/responses":
                    raise RuntimeError("synthetic response ledger write failure")
                return observer(event, **fields)

            return observer_with_failure

        with patch.object(runner, "_audit", side_effect=failing_audit_factory):
            result = await self.run_phase(batch, phase="pilot")
        first = result["documents"][0]
        self.assertEqual((first["input_tokens"], first["output_tokens"]), (25, 8))
        self.assertEqual((first["reserved_input_tokens"], first["reserved_output_tokens"]), (0, 0))
        self.assertEqual((first["labelled_chunks"], result["totals"]["unknown_calls"]), (0, 0))
        self.assertEqual(len(self.transport.generation_requests), 1)
        before = len(self.transport.requests)
        await self.run_phase(batch, phase="pilot")
        self.assertEqual(len(self.transport.requests), before)
        self.assert_old_preserved()

    async def test_restart_after_generation_intent_never_replays_or_invents_usage(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        call_key = self.interrupted_call(batch, generation=True)
        self.runner = BoundedReingest(self.config, self.store.db, self.graph, self.adapter, self.scratch)
        result = await self.run_phase(batch, phase="pilot", resume_known=True)
        row = self.store.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
        self.assertEqual((row["status"], row["error"]), ("usage_unknown", "interrupted_unknown_usage"))
        self.assertEqual(row["elapsed_seconds"], 45)
        self.assertEqual((row["actual_input_tokens"], row["actual_output_tokens"]), (None, None))
        self.assertEqual((row["reserved_input_tokens"], row["reserved_output_tokens"]), (25, 512))
        self.assertEqual(result["totals"]["unknown_calls"], 1)
        self.assertEqual(self.transport.requests, [])
        await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_unknown_call_settled_before_batch_stop_is_recovered_without_replay(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        call_key = self.interrupted_call(batch, generation=True)
        with self.store.db:
            self.store.db.execute("UPDATE reingest_calls SET status='usage_unknown',elapsed_seconds=0.5,error='stage_timeout' WHERE call_key=?",
                                  (call_key,))
            self.store.db.execute("UPDATE reingest_batches SET status='running',error=NULL WHERE batch_id=?", (batch,))
        result = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertEqual(result["status"], "usage_unknown")
        self.assertEqual(result["totals"]["unknown_calls"], 1)
        self.assertEqual(result["documents"][0]["elapsed_seconds"], 0.5)
        self.assertEqual((result["documents"][0]["reserved_input_tokens"],
                          result["documents"][0]["reserved_output_tokens"]), (25, 512))
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_later_known_orphan_cannot_clear_earlier_unknown_orphan_gate(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        first = self.interrupted_call(batch, generation=True, call_key="first-unknown", document_index=0)
        later = self.interrupted_call(batch, generation=False, call_key="later-known", document_index=1)
        result = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertEqual(result["status"], "usage_unknown")
        self.assertEqual(result["totals"]["unknown_calls"], 1)
        self.assertEqual(result["totals"]["elapsed_seconds"], 90)
        rows = {row["call_key"]: row for row in self.store.db.execute("SELECT * FROM reingest_calls")}
        self.assertEqual(rows[first]["status"], "usage_unknown")
        self.assertEqual(rows[later]["status"], "failed")
        self.assertEqual((rows[first]["actual_input_tokens"], rows[first]["actual_output_tokens"]), (None, None))
        self.assertEqual((rows[first]["reserved_input_tokens"], rows[first]["reserved_output_tokens"]), (25, 512))
        self.assertEqual((rows[later]["reserved_input_tokens"], rows[later]["reserved_output_tokens"]), (0, 0))
        self.assertEqual(self.transport.requests, [])
        await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_restart_during_count_only_is_known_non_generating_and_explicitly_resumable(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        call_key = self.interrupted_call(batch, generation=False)
        self.runner = BoundedReingest(self.config, self.store.db, self.graph, self.adapter, self.scratch)
        stopped = await self.run_phase(batch, phase="pilot")
        row = self.store.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
        self.assertEqual((row["status"], row["error"], row["elapsed_seconds"]), ("failed", "interrupted", 45))
        self.assertEqual(stopped["totals"]["unknown_calls"], 0)
        self.assertEqual(self.transport.requests, [])
        resumed = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertTrue(resumed["pilot_passed"])
        self.assertEqual(len(self.transport.generation_requests), 12)
        self.assertGreaterEqual(resumed["totals"]["elapsed_seconds"], 45)
        self.assert_old_preserved()

    async def test_restart_after_usage_receipt_preserves_known_charge_before_explicit_retry(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        call_key = self.interrupted_call(batch, generation=True, usage=(25, 8))
        stopped = await self.run_phase(batch, phase="pilot")
        self.assertEqual((stopped["totals"]["input_tokens"], stopped["totals"]["output_tokens"]), (25, 8))
        self.assertEqual(stopped["totals"]["unknown_calls"], 0)
        self.assertEqual(self.transport.requests, [])
        resumed = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertTrue(resumed["pilot_passed"])
        self.assertEqual((resumed["totals"]["input_tokens"], resumed["totals"]["output_tokens"]), (13 * 25, 13 * 8))
        row = self.store.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
        self.assertEqual((row["actual_input_tokens"], row["actual_output_tokens"]), (25, 8))
        self.assertEqual((row["reserved_input_tokens"], row["reserved_output_tokens"]), (0, 0))
        self.assert_old_preserved()

    async def test_exhausted_document_time_cannot_be_reset_by_known_resume(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        call_key = self.interrupted_call(batch, generation=False, seconds=45)
        with self.store.db:
            self.store.db.execute("UPDATE reingest_calls SET status='failed',elapsed_seconds=1800,error='interrupted' WHERE call_key=?",
                                  (call_key,))
            self.store.db.execute("UPDATE reingest_batches SET status='failed',error='interrupted' WHERE batch_id=?", (batch,))
        result = await self.run_phase(batch, phase="pilot", resume_known=True)
        self.assertEqual(result["error"], "reingest_document_time_limit")
        self.assertEqual(result["documents"][0]["elapsed_seconds"], 1800)
        self.assertEqual(self.transport.requests, [])
        self.assert_old_preserved()

    async def test_document_input_cap_stops_batch_before_another_generation(self):
        runner = self.fixture((20, 20, 20, 20))
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        self.transport.count_outcomes.extend([{"input_tokens": 4096}] * 100)
        self.transport.generation_outcomes.extend([
            completed('{"marks":["volcano"]}', inp=4096, out=8)] * 100)
        await self.run_phase(batch, phase="pilot")
        result = await self.run_phase(batch, phase="complete")
        self.assertEqual(result["documents"][0]["input_tokens"], 65_536)
        self.assertEqual(result["documents"][0]["labelled_chunks"], 16)
        self.assertTrue(all(row["input_tokens"] <= 65_536 for row in result["documents"]))
        self.assertLessEqual(sum(row["input_tokens"] for row in result["documents"]), 262_144)
        self.assertFalse(any(row["published"] for row in result["documents"]))
        self.assertEqual(len(self.transport.generation_requests), sum(row["labelled_chunks"] for row in result["documents"]))
        self.assert_old_preserved()

    async def test_document_output_cap_reserves_full_next_output_before_issue(self):
        runner = self.fixture((40, 40, 40, 40))
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        self.transport.generation_outcomes.extend([
            completed('{"marks":["volcano"]}', inp=25, out=512)] * 100)
        await self.run_phase(batch, phase="pilot")
        result = await self.run_phase(batch, phase="complete")
        self.assertEqual(result["documents"][0]["output_tokens"], 16_384)
        self.assertEqual(result["documents"][0]["labelled_chunks"], 32)
        self.assertTrue(all(row["output_tokens"] <= 16_384 for row in result["documents"]))
        self.assertLessEqual(sum(row["output_tokens"] for row in result["documents"]), 65_536)
        self.assertEqual(len(self.transport.generation_requests), sum(row["labelled_chunks"] for row in result["documents"]))
        self.assertFalse(any(row["published"] for row in result["documents"]))
        self.assert_old_preserved()

    async def test_changed_pdf_does_not_get_published_or_modify_old_snapshot(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        await self.run_phase(batch, phase="pilot")
        (self.config.knowledge_dir / self.paths[0]).write_bytes(b"%PDF-1.4\nchanged after pilot")
        before = len(self.transport.requests)
        try:
            result = await self.run_phase(batch, phase="complete")
        except (ValueError, GovernedError):
            result = runner.summary(batch)
        self.assertFalse(any(row["published"] for row in result["documents"]))
        self.assertEqual(len(self.transport.requests), before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        self.assert_old_preserved()

    async def test_publication_failure_rolls_back_graph_new_version_and_pointer(self):
        runner = self.fixture()
        batch = runner.grant(self.source_ids, operator_confirmed=True)
        await self.run_phase(batch, phase="pilot")
        original_add = self.graph.add

        def fail_after_add(*args, **kwargs):
            original_add(*args, **kwargs)
            raise RuntimeError("synthetic publication failure")

        with patch.object(self.graph, "add", side_effect=fail_after_add):
            try:
                result = await self.run_phase(batch, phase="complete")
            except (ValueError, GovernedError, RuntimeError):
                result = runner.summary(batch)
        self.assertFalse(any(row["published"] for row in result["documents"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 4)
        self.assertEqual([row[0] for row in self.store.db.execute("SELECT source_id FROM knowledge_desired ORDER BY path")],
                         self.source_ids)
        failure_event = self.store.db.execute("SELECT attempt_id,details_json FROM reingest_events WHERE event='publication_failed'").fetchone()
        self.assertIsNotNone(failure_event["attempt_id"])
        self.assertEqual(json.loads(failure_event["details_json"])["phase"], "publication")
        self.assert_old_preserved()
