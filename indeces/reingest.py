"""Explicit four-document maintenance with a separate, bounded accounting ledger.

This path never resets a failed knowledge version or its original audit row.
It requires an operator grant, an existing converted snapshot and exclusive
state ownership. It does not discover files, start Discord or retry a call.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .adapter import OpenAIAdapter
from .config import Budget, load_config
from .context import encode
from .contracts import GovernedError, validated_token_usage
from .credentials import load_openai_key, validate_api_key
from .knowledge import KnowledgeService, _console_notice
from .lock import InstanceLock
from .memory import MemoryGraph
from .path_policy import validate_managed_path, validate_runtime_paths, validate_knowledge_root
from . import pdf_import
from .prompts import LABEL, LABEL_SCHEMA
from .runtime import label_data
from .scratch import ScratchLog


CONTRACT = {"documents": 4, "pilot_blocks_per_document": 3,
            "call_input_tokens": 4096, "call_output_tokens": 512, "call_seconds": 45,
            "document_input_tokens": 65536, "document_output_tokens": 16384,
            "document_seconds": 1800, "batch_input_tokens": 262144,
            "batch_output_tokens": 65536, "batch_seconds": 7200,
            "automatic_retries": 0, "chunk_characters": 400}
APPROVAL = "operator_confirmed_old_four_unknown_usage_loss_closed_without_exact_reconciliation"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def backup_database(db, state_dir):
    """SQLite backup includes committed WAL content; never overwrite a backup."""
    if db.in_transaction:
        raise ValueError("backup requires a committed connection")
    directory = validate_managed_path(Path(state_dir) / "backups", "maintenance backup")
    directory.mkdir(parents=True, exist_ok=True)
    target = validate_managed_path(directory / ("before-reingest-" + uuid.uuid4().hex + ".sqlite3"), "maintenance backup")
    with target.open("xb"):
        pass
    destination = sqlite3.connect(target)
    try:
        db.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("maintenance backup failed integrity check")
    finally:
        destination.close()
    return target


class BoundedReingest:
    """Caller owns InstanceLock for this object's entire lifetime."""

    def __init__(self, config, db, graph, adapter, scratch, *, initialize=True):
        validate_runtime_paths(config)
        if config.knowledge.chunk_characters != 400:
            raise ValueError("maintenance requires the unchanged 400-character baseline")
        budget = config.adapter.budgets["label"]
        if (budget.input_tokens, budget.output_tokens, budget.reasoning) != (4096, 512, "low"):
            raise ValueError("maintenance requires the unchanged label baseline")
        if config.adapter.model != "gpt-6-luna" or config.adapter.verbosity != "high":
            raise ValueError("maintenance requires the unchanged model/verbosity baseline")
        self.config, self.db, self.graph = config, db, graph
        self.adapter, self.scratch = adapter, scratch
        self._execution = None
        self._active_interval = None
        self.scope = f"{config.discord.guild_id}:knowledge"
        self.root = validate_knowledge_root(config)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "knowledge_versions" not in tables:
            raise ValueError("maintenance needs existing knowledge snapshots")
        if not initialize:
            return
        if "reingest_batches" not in tables:
            backup_database(db, config.state_dir)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reingest_batches(
                batch_id TEXT PRIMARY KEY, grant_key TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                approval TEXT NOT NULL, contract_json TEXT NOT NULL,
                old_snapshot_sha256 TEXT NOT NULL, checkpoint_through_id INTEGER NOT NULL,
                pilot_passed INTEGER NOT NULL DEFAULT 0,
                error TEXT);
            CREATE TABLE IF NOT EXISTS reingest_documents(
                attempt_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, scope TEXT NOT NULL,
                path TEXT NOT NULL, digest TEXT NOT NULL, old_source_id TEXT NOT NULL,
                new_source_id TEXT NOT NULL UNIQUE, total_chunks INTEGER NOT NULL,
                published INTEGER NOT NULL DEFAULT 0,
                UNIQUE(scope,path,digest), FOREIGN KEY(batch_id) REFERENCES reingest_batches(batch_id));
            CREATE TABLE IF NOT EXISTS reingest_calls(
                call_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                adapter_call_id TEXT, status TEXT NOT NULL, phase TEXT NOT NULL,
                started_at REAL NOT NULL, seconds_limit REAL NOT NULL, elapsed_seconds REAL,
                reserved_input_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
                actual_input_tokens INTEGER, actual_output_tokens INTEGER,
                provider_request_id TEXT, response_id TEXT, error TEXT,
                FOREIGN KEY(attempt_id) REFERENCES reingest_documents(attempt_id));
            CREATE TABLE IF NOT EXISTS reingest_requests(
                client_request_id TEXT PRIMARY KEY, call_key TEXT NOT NULL,
                path TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                provider_request_id TEXT, response_id TEXT, usage_json TEXT,
                FOREIGN KEY(call_key) REFERENCES reingest_calls(call_key));
            CREATE TABLE IF NOT EXISTS reingest_labels(
                attempt_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                marks_json TEXT NOT NULL, call_key TEXT NOT NULL,
                PRIMARY KEY(attempt_id,chunk_index),
                FOREIGN KEY(attempt_id) REFERENCES reingest_documents(attempt_id));
            CREATE TABLE IF NOT EXISTS reingest_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
                attempt_id TEXT, call_key TEXT, event TEXT NOT NULL,
                recorded_at REAL NOT NULL, details_json TEXT NOT NULL);
        """)

    def _event(self, batch_id, event, *, attempt_id=None, call_key=None, **details):
        self.db.execute("INSERT INTO reingest_events(batch_id,attempt_id,call_key,event,recorded_at,details_json) VALUES(?,?,?,?,?,?)",
                        (batch_id, attempt_id, call_key, event, time.time(), _json(details)))

    def _batch(self, batch_id):
        row = self.db.execute("SELECT * FROM reingest_batches WHERE batch_id=? AND scope=?", (batch_id, self.scope)).fetchone()
        if row is None or row["contract_json"] != _json(CONTRACT) or row["approval"] != APPROVAL:
            raise ValueError("unknown or incompatible maintenance grant")
        return row

    def _documents(self, batch_id):
        return self.db.execute("SELECT * FROM reingest_documents WHERE batch_id=? ORDER BY path", (batch_id,)).fetchall()

    def _snapshot(self, version):
        # Reuse the pure snapshot validator, without running KnowledgeService's
        # constructor, schema migrations or legacy failure updates.
        reader = object.__new__(KnowledgeService)
        reader.config, reader.root, reader.pdf_limits = self.config, self.root, self.config.pdf
        relative = Path(version["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0].casefold() in {".indeces", "_staging"}:
            raise GovernedError("knowledge_path_outside_root")
        snapshot = reader._snapshot_file(self.root / relative)
        if snapshot is None or snapshot[1] != version["digest"]:
            raise GovernedError("reingest_source_changed")
        return snapshot

    def _validate_version(self, version):
        self._snapshot(version)
        expected = [(i // 400, version["raw_text"][i:i + 400], i)
                    for i in range(0, len(version["raw_text"]), 400) if version["raw_text"][i:i + 400].strip()]
        chunks = self.db.execute("SELECT chunk_index,text,start_character FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index",
                                 (version["source_id"],)).fetchall()
        if [tuple(c) for c in chunks] != expected or len(chunks) < 3:
            raise GovernedError("reingest_snapshot_chunks_invalid")
        if Path(version["path"]).suffix.lower() == ".pdf":
            pdf = self.db.execute("SELECT * FROM knowledge_pdf_versions WHERE source_id=?", (version["source_id"],)).fetchone()
            if pdf is None or hashlib.sha256(pdf["pdf_bytes"]).hexdigest() != version["digest"]:
                raise GovernedError("pdf_frozen_snapshot_invalid")
            pdf_import.validate_conversion(version["raw_text"], json.loads(pdf["metadata_json"]), version["digest"])
        return chunks

    def _old_hash(self, source_ids, checkpoint_through_id=None):
        result = hashlib.sha256()
        for source_id in sorted(source_ids):
            for table in ("knowledge_versions", "knowledge_chunks", "knowledge_pdf_versions", "knowledge_file_audit", "knowledge_root_quarantine", "memory_records"):
                rows = self.db.execute(f"SELECT * FROM {table} WHERE source_id=? ORDER BY rowid", (source_id,)).fetchall()
                result.update(table.encode())
                for row in rows:
                    values = [dict(blob_sha256=hashlib.sha256(v).hexdigest()) if isinstance(v, bytes) else v for v in tuple(row)]
                    result.update(_json(values).encode("utf-8"))
        if checkpoint_through_id is None:
            checkpoint_through_id = self.db.execute("SELECT COALESCE(MAX(id),0) FROM checkpoints").fetchone()[0]
        for row in self.db.execute("SELECT * FROM checkpoints WHERE id<=? ORDER BY id", (checkpoint_through_id,)):
            result.update(_json(tuple(row)).encode("utf-8"))
        return result.hexdigest()

    def _assert_old(self, batch_id):
        batch = self._batch(batch_id)
        if self._old_hash([d["old_source_id"] for d in self._documents(batch_id)], batch["checkpoint_through_id"]) != batch["old_snapshot_sha256"]:
            raise GovernedError("reingest_old_records_changed")

    def grant(self, source_ids, *, operator_confirmed):
        if operator_confirmed is not True:
            raise ValueError("operator must explicitly close the old four unknown calls as loss")
        if not isinstance(source_ids, (list, tuple)) or len(source_ids) != 4 or len(set(source_ids)) != 4:
            raise ValueError("grant requires exactly four distinct existing sources")
        versions = []
        for source_id in source_ids:
            row = self.db.execute("SELECT * FROM knowledge_versions WHERE source_id=? AND scope=?", (source_id, self.scope)).fetchone()
            if row is None:
                raise ValueError("source not owned by this knowledge scope")
            versions.append(dict(row))
        key = hashlib.sha256(_json(sorted((v["path"], v["digest"], v["source_id"]) for v in versions)).encode()).hexdigest()
        existing = self.db.execute("SELECT batch_id FROM reingest_batches WHERE scope=? AND grant_key=?", (self.scope, key)).fetchone()
        if existing:
            self._assert_old(existing[0])
            return existing[0]
        binding = self.db.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (self.scope,)).fetchone()
        if binding is None or binding[0] != KnowledgeService._root_identity(self.root):
            raise GovernedError("reingest_source_root_unverified")
        for version in versions:
            desired = self.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=?", (self.scope, version["path"])).fetchone()
            audit = self.db.execute("SELECT remote_usage_unknown FROM knowledge_file_audit WHERE scope=? AND path=? AND source_id=?",
                                    (self.scope, version["path"], version["source_id"])).fetchone()
            if version["status"] != "failed" or not desired or desired[0] != version["source_id"] or not audit or audit[0] != 1:
                raise ValueError("grant requires four current failed unknown-usage versions")
            self._validate_version(version)
        # Also back up when the schema already exists but a new grant is added.
        backup_database(self.db, self.config.state_dir)
        batch_id = uuid.uuid4().hex
        checkpoint_through_id = self.db.execute("SELECT COALESCE(MAX(id),0) FROM checkpoints").fetchone()[0]
        with self.db:
            self.db.execute("INSERT INTO reingest_batches(batch_id,grant_key,scope,status,created_at,approval,contract_json,old_snapshot_sha256,checkpoint_through_id) VALUES(?,?,?,?,?,?,?,?,?)",
                            (batch_id, key, self.scope, "approved", time.time(), APPROVAL, _json(CONTRACT), self._old_hash(source_ids, checkpoint_through_id), checkpoint_through_id))
            for version in versions:
                count = self.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=?", (version["source_id"],)).fetchone()[0]
                self.db.execute("INSERT INTO reingest_documents(attempt_id,batch_id,scope,path,digest,old_source_id,new_source_id,total_chunks) VALUES(?,?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, batch_id, self.scope, version["path"], version["digest"], version["source_id"], "kb:" + uuid.uuid4().hex, count))
            self._event(batch_id, "operator_grant", approval=APPROVAL, contract=CONTRACT, old_source_ids=sorted(source_ids),
                        old_usage_remains_unknown=True, old_records_reset=False)
        return batch_id

    def _usage(self, *, attempt_id=None, batch_id=None):
        where, params = ("c.attempt_id=?", (attempt_id,)) if attempt_id else ("d.batch_id=?", (batch_id,))
        row = self.db.execute(f"""SELECT COALESCE(SUM(actual_input_tokens),0),COALESCE(SUM(actual_output_tokens),0),
            COALESCE(SUM(elapsed_seconds),0),COALESCE(SUM(reserved_input_tokens),0),COALESCE(SUM(reserved_output_tokens),0),
            SUM(CASE WHEN c.status='usage_unknown' THEN 1 ELSE 0 END)
            FROM reingest_calls c JOIN reingest_documents d ON d.attempt_id=c.attempt_id WHERE {where}""", params).fetchone()
        result = dict(zip(("input_tokens", "output_tokens", "elapsed_seconds", "reserved_input_tokens", "reserved_output_tokens", "unknown_calls"), row))
        event_where, event_params = ("attempt_id=?", (attempt_id,)) if attempt_id else ("batch_id=?", (batch_id,))
        local_seconds = self.db.execute(f"SELECT COALESCE(SUM(json_extract(details_json,'$.elapsed_seconds')),0) FROM reingest_events WHERE event='local_execution_time' AND {event_where}", event_params).fetchone()[0]
        result["elapsed_seconds"] += local_seconds
        result["unknown_calls"] = result["unknown_calls"] or 0
        return result

    def summary(self, batch_id):
        batch = self._batch(batch_id)
        docs = []
        for document in self._documents(batch_id):
            data = dict(document)
            data.update(self._usage(attempt_id=document["attempt_id"]))
            labels = self.db.execute("SELECT marks_json FROM reingest_labels WHERE attempt_id=?", (document["attempt_id"],)).fetchall()
            marks = [m for row in labels for m in json.loads(row[0])]
            data.update(labelled_chunks=len(labels), mark_occurrences=len(marks), unique_marks=len(set(marks)))
            docs.append(data)
        failures = [dict(r) for r in self.db.execute("""SELECT c.* FROM reingest_calls c JOIN reingest_documents d ON d.attempt_id=c.attempt_id
            WHERE d.batch_id=? AND c.status NOT IN ('completed','running') ORDER BY c.started_at""", (batch_id,))]
        return {"batch_id": batch_id, "status": batch["status"], "error": batch["error"], "pilot_passed": bool(batch["pilot_passed"]),
                "documents": docs, "totals": self._usage(batch_id=batch_id), "failures": failures,
                "old_records_preserved": self._old_hash([d["old_source_id"] for d in docs], batch["checkpoint_through_id"]) == batch["old_snapshot_sha256"]}

    def _stop(self, batch_id, code, *, unknown=False):
        with self.db:
            unknown = bool(unknown or self._batch(batch_id)["status"] == "usage_unknown" or self._usage(batch_id=batch_id)["unknown_calls"])
            self.db.execute("UPDATE reingest_batches SET status=?,error=? WHERE batch_id=?",
                            ("usage_unknown" if unknown else "failed", code, batch_id))
            self._event(batch_id, "batch_stopped", code=code, usage_unknown=unknown, automatic_retry=False)
        _console_notice(f"Maintenance {batch_id}: stopped: {code}; usage_unknown={unknown}; no automatic retry.")

    def recover_interrupted(self, batch_id):
        self._batch(batch_id)
        rows = self.db.execute("""SELECT c.* FROM reingest_calls c JOIN reingest_documents d ON d.attempt_id=c.attempt_id
            WHERE d.batch_id=? AND (c.elapsed_seconds IS NULL OR
                (c.actual_input_tokens IS NULL AND EXISTS(SELECT 1 FROM reingest_requests r WHERE r.call_key=c.call_key AND r.path='/responses')))""", (batch_id,)).fetchall()
        for call in rows:
            generation = self.db.execute("SELECT 1 FROM reingest_requests WHERE call_key=? AND path='/responses'", (call["call_key"],)).fetchone()
            unknown = generation is not None and call["actual_input_tokens"] is None
            # Crash recovery cannot measure the missing monotonic interval.
            # Charge a separately identified conservative elapsed allowance,
            # never invent actual token usage; no automatic replay follows.
            code = call["error"] or ("interrupted_unknown_usage" if unknown else "interrupted")
            charged_seconds = call["seconds_limit"] if call["elapsed_seconds"] is None else call["elapsed_seconds"]
            with self.db:
                self.db.execute("UPDATE reingest_calls SET status=?,error=?,elapsed_seconds=?,reserved_input_tokens=?,reserved_output_tokens=? WHERE call_key=?",
                                ("usage_unknown" if unknown else "failed", code, charged_seconds,
                                 call["reserved_input_tokens"] if unknown else 0, call["reserved_output_tokens"] if unknown else 0, call["call_key"]))
                self._event(batch_id, "crash_recovery", call_key=call["call_key"], attempt_id=call["attempt_id"],
                            code=code, elapsed_is_conservative=call["elapsed_seconds"] is None, elapsed_allowance=charged_seconds, token_usage_invented=False)
            self._stop(batch_id, code, unknown=unknown)

    def _budget_usage(self, document):
        used = self._usage(attempt_id=document["attempt_id"])
        total = self._usage(batch_id=document["batch_id"])
        if self._active_interval and self._active_interval[0] == document["attempt_id"]:
            used["elapsed_seconds"] += time.perf_counter() - self._active_interval[1]
        if self._execution and self._execution[0] == document["batch_id"]:
            total["elapsed_seconds"] = max(total["elapsed_seconds"], self._execution[2] + time.perf_counter() - self._execution[1])
        return used, total

    def _audit(self, document, call_key):
        batch_id, attempt_id = document["batch_id"], document["attempt_id"]
        def audit(event, **fields):
            with self.db:
                if event == "call_start":
                    self.db.execute("UPDATE reingest_calls SET adapter_call_id=?,phase='slot_wait' WHERE call_key=?", (fields["call_id"], call_key))
                elif event == "input_gate" and fields["admitted"]:
                    tokens = fields["input_tokens"]
                    self.db.execute("UPDATE reingest_calls SET reserved_input_tokens=?,reserved_output_tokens=512,phase='input_gate' WHERE call_key=?", (tokens, call_key))
                elif event == "request_intent":
                    if fields["path"] == "/responses":
                        self._current(document)
                        call = self.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
                        if call["reserved_output_tokens"] != 512 or call["reserved_input_tokens"] > 4096:
                            raise GovernedError("reingest_generation_not_reserved")
                        self._assert_budget(document, include_reserved=True)
                    self.db.execute("INSERT INTO reingest_requests(client_request_id,call_key,path,status,created_at) VALUES(?,?,?,?,?)",
                                    (fields["client_request_id"], call_key, fields["path"], "intent", time.time()))
                    self.db.execute("UPDATE reingest_calls SET phase=? WHERE call_key=?", (fields["phase"], call_key))
                elif event == "response_headers":
                    self.db.execute("UPDATE reingest_requests SET status='headers',provider_request_id=? WHERE client_request_id=?",
                                    (fields.get("provider_request_id"), fields["client_request_id"]))
                    if fields["path"] == "/responses":
                        self.db.execute("UPDATE reingest_calls SET provider_request_id=? WHERE call_key=?", (fields.get("provider_request_id"), call_key))
                elif event == "response_received":
                    usage = validated_token_usage(fields.get("known_usage"))
                    self.db.execute("UPDATE reingest_requests SET status='received',response_id=?,usage_json=? WHERE client_request_id=?",
                                    (fields.get("response_id"), _json(usage) if usage else None, fields["client_request_id"]))
                    if fields["path"] == "/responses":
                        self.db.execute("UPDATE reingest_calls SET response_id=? WHERE call_key=?", (fields.get("response_id"), call_key))
                        if usage is not None:
                            self.db.execute("UPDATE reingest_calls SET actual_input_tokens=?,actual_output_tokens=?,reserved_input_tokens=0,reserved_output_tokens=0 WHERE call_key=?",
                                            (usage["input_tokens"], usage["output_tokens"], call_key))
                elif event == "call_end":
                    self.db.execute("UPDATE reingest_calls SET phase=? WHERE call_key=?", (fields.get("phase", "call_end"), call_key))
                self._event(batch_id, event, attempt_id=attempt_id, call_key=call_key, **fields)
        return audit

    def _assert_budget(self, document, *, include_reserved=False):
        used, total = self._budget_usage(document)
        if total["unknown_calls"]:
            raise GovernedError("reingest_unresolved_usage")
        for usage, prefix in ((used, "document"), (total, "batch")):
            extra_in = usage["reserved_input_tokens"] if include_reserved else 0
            extra_out = usage["reserved_output_tokens"] if include_reserved else 0
            if usage["input_tokens"] + extra_in > CONTRACT[prefix + "_input_tokens"] or usage["output_tokens"] + extra_out > CONTRACT[prefix + "_output_tokens"]:
                raise GovernedError("reingest_" + prefix + "_token_limit")
            if usage["elapsed_seconds"] >= CONTRACT[prefix + "_seconds"]:
                raise GovernedError("reingest_" + prefix + "_time_limit")

    def _current(self, document):
        version = self.db.execute("SELECT * FROM knowledge_versions WHERE source_id=?", (document["old_source_id"],)).fetchone()
        self._snapshot(version)
        desired = self.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=?", (self.scope, document["path"])).fetchone()
        allowed = document["new_source_id"] if document["published"] else document["old_source_id"]
        if not desired or desired[0] != allowed:
            raise GovernedError("reingest_desired_source_changed")
        return version

    async def _label(self, document, chunk):
        self._assert_budget(document)
        used, total = self._budget_usage(document)
        available_in = min(65536 - used["input_tokens"], 262144 - total["input_tokens"], 4096)
        available_out = min(16384 - used["output_tokens"], 65536 - total["output_tokens"])
        seconds = min(45, 1800 - used["elapsed_seconds"], 7200 - total["elapsed_seconds"])
        if available_in <= 0 or available_out < 512 or seconds <= 0:
            raise GovernedError("reingest_budget_exhausted")
        call_key = uuid.uuid4().hex
        started = time.perf_counter()
        self._active_interval = (document["attempt_id"], started)
        with self.db:
            self.db.execute("INSERT INTO reingest_calls(call_key,attempt_id,chunk_index,status,phase,started_at,seconds_limit) VALUES(?,?,?,?,?,?,?)",
                            (call_key, document["attempt_id"], chunk["chunk_index"], "running", "prepared", time.time(), seconds))
        error = None
        try:
            self._current(document)
            remaining_seconds = seconds - (time.perf_counter() - started)
            if remaining_seconds <= 0:
                raise GovernedError("stage_timeout")
            result = await self.adapter.call("label", LABEL, [{"role": "user", "content": encode({
                "source_id": document["new_source_id"], "path": document["path"], "digest": document["digest"],
                "chunk_index": chunk["chunk_index"], "start_character": chunk["start_character"], "text": chunk["text"]})}],
                document["attempt_id"], LABEL_SCHEMA, input_limit=int(available_in),
                budget_override=Budget(4096, 512, remaining_seconds, self.config.adapter.budgets["label"].reasoning),
                audit=self._audit(document, call_key))
            with self.db:
                self.db.execute("UPDATE reingest_calls SET phase='usage_verification' WHERE call_key=?", (call_key,))
            receipt = self.db.execute("SELECT actual_input_tokens,actual_output_tokens,response_id FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
            if (receipt[0], receipt[1], receipt[2]) != (result.input_tokens, result.output_tokens, result.response_id):
                raise GovernedError("reingest_usage_receipt_mismatch")
            with self.db:
                self.db.execute("UPDATE reingest_calls SET phase='label_validation' WHERE call_key=?", (call_key,))
            marks = label_data(result.text)
            self._current(document)
            with self.db:
                self.db.execute("UPDATE reingest_calls SET phase='label_persistence' WHERE call_key=?", (call_key,))
                self.db.execute("INSERT INTO reingest_labels VALUES(?,?,?,?)", (document["attempt_id"], chunk["chunk_index"], _json(marks), call_key))
            self.scratch.write("reingest_chunk_labelled", batch_id=document["batch_id"], attempt_id=document["attempt_id"],
                               source_id=document["new_source_id"], call_key=call_key, chunk_index=chunk["chunk_index"], marks=marks, quote=chunk["text"])
        except BaseException as caught:
            error = caught
        elapsed = time.perf_counter() - started
        row = self.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
        recovered_usage = validated_token_usage(getattr(error, "known_usage", None)) if error is not None else None
        if row["actual_input_tokens"] is None and recovered_usage is not None:
            with self.db:
                self.db.execute("UPDATE reingest_calls SET actual_input_tokens=?,actual_output_tokens=?,reserved_input_tokens=0,reserved_output_tokens=0 WHERE call_key=?",
                                (recovered_usage["input_tokens"], recovered_usage["output_tokens"], call_key))
                self._event(document["batch_id"], "usage_preserved_from_transport_error", attempt_id=document["attempt_id"], call_key=call_key,
                            known_usage=recovered_usage)
            row = self.db.execute("SELECT * FROM reingest_calls WHERE call_key=?", (call_key,)).fetchone()
        generation = self.db.execute("SELECT 1 FROM reingest_requests WHERE call_key=? AND path='/responses'", (call_key,)).fetchone()
        unknown = bool(generation) and row["actual_input_tokens"] is None
        code = getattr(error, "code", "interrupted" if isinstance(error, asyncio.CancelledError) else type(error).__name__) if error else None
        with self.db:
            self.db.execute("UPDATE reingest_calls SET status=?,elapsed_seconds=?,error=?,reserved_input_tokens=?,reserved_output_tokens=? WHERE call_key=?",
                            ("usage_unknown" if unknown else "failed" if error else "completed", elapsed, code,
                             row["reserved_input_tokens"] if unknown else 0, row["reserved_output_tokens"] if unknown else 0, call_key))
            self._event(document["batch_id"], "call_settled", attempt_id=document["attempt_id"], call_key=call_key,
                        code=code, elapsed_seconds=elapsed, usage_unknown=unknown, actual_usage_received=row["actual_input_tokens"] is not None)
            if error is not None:
                self.db.execute("UPDATE reingest_batches SET status=?,error=? WHERE batch_id=?",
                                ("usage_unknown" if unknown else "failed", code, document["batch_id"]))
        self._active_interval = None
        if error is not None:
            self._stop(document["batch_id"], code, unknown=unknown)
            if isinstance(error, asyncio.CancelledError):
                raise error
            return False
        self._assert_budget(document)
        return True

    def _publish(self, document):
        started = time.perf_counter()
        self._active_interval = (document["attempt_id"], started)
        try:
            return self._publish_transaction(document, started)
        except BaseException as error:
            with self.db:
                self._event(document["batch_id"], "publication_failed", attempt_id=document["attempt_id"],
                            phase="publication", code=getattr(error, "code", type(error).__name__))
            raise
        finally:
            with self.db:
                self._event(document["batch_id"], "local_execution_time", attempt_id=document["attempt_id"],
                            phase="publication", elapsed_seconds=time.perf_counter() - started)
            self._active_interval = None

    def _publish_transaction(self, document, started):
        self._assert_old(document["batch_id"])
        version = self._current(document)
        labels = self.db.execute("""SELECT c.*,l.marks_json AS new_marks FROM knowledge_chunks c
            JOIN reingest_labels l ON l.chunk_index=c.chunk_index AND l.attempt_id=?
            WHERE c.source_id=? ORDER BY c.chunk_index""", (document["attempt_id"], document["old_source_id"])).fetchall()
        if len(labels) != document["total_chunks"]:
            raise GovernedError("reingest_incomplete_publication")
        self._assert_budget(document)
        usage, total = self._budget_usage(document)
        remaining = min(1800 - usage["elapsed_seconds"], 7200 - total["elapsed_seconds"])
        deadline = time.perf_counter() + remaining
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._current(document)
            self.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,input_tokens,output_tokens,elapsed_seconds,scope) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (document["new_source_id"], document["path"], document["digest"], version["raw_text"], "ready", time.time(), usage["input_tokens"], usage["output_tokens"], usage["elapsed_seconds"], self.scope))
            self.db.executemany("INSERT INTO knowledge_chunks(source_id,chunk_index,text,start_character,marks_json) VALUES(?,?,?,?,?)",
                                [(document["new_source_id"], c["chunk_index"], c["text"], c["start_character"], c["new_marks"]) for c in labels])
            self.db.execute("INSERT INTO knowledge_pdf_versions SELECT ?,pdf_bytes,metadata_json,markdown_path FROM knowledge_pdf_versions WHERE source_id=?", (document["new_source_id"], document["old_source_id"]))
            previous = self.db.execute("SELECT source_id FROM knowledge_published WHERE scope=? AND path=?", (self.scope, document["path"])).fetchone()
            if previous and previous[0] != document["new_source_id"]:
                self.graph.deactivate_source(self.scope, previous[0], commit=False)
            facts = [{"text": c["text"], "quote": c["text"], "marks": json.loads(c["new_marks"])} for c in labels]
            self.graph.add(self.scope, document["new_source_id"], "knowledge:" + document["path"], facts, time.time(), commit=False)
            self._snapshot(version)
            if time.perf_counter() >= deadline:
                raise GovernedError("reingest_publication_time_limit")
            for table in ("knowledge_desired", "knowledge_published"):
                self.db.execute(f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(scope,path) DO UPDATE SET source_id=excluded.source_id", (self.scope, document["path"], document["new_source_id"]))
            self.db.execute("UPDATE reingest_documents SET published=1 WHERE attempt_id=?", (document["attempt_id"],))
            self._event(document["batch_id"], "published", attempt_id=document["attempt_id"], new_source_id=document["new_source_id"], previous_source_id=previous[0] if previous else None)
        self._assert_old(document["batch_id"])

    async def run(self, batch_id, *, phase="pilot", resume_known=False):
        if phase not in {"pilot", "complete"}:
            raise ValueError("phase must be pilot or complete")
        self._assert_old(batch_id)
        self.recover_interrupted(batch_id)
        batch = self._batch(batch_id)
        if batch["status"] in {"usage_unknown", "completed"}:
            return self.summary(batch_id)
        if batch["status"] == "failed" and not resume_known:
            return self.summary(batch_id)
        if phase == "complete" and not batch["pilot_passed"]:
            raise ValueError("complete requires a passed twelve-block pilot")
        invocation_started = time.perf_counter()
        previously_accounted_seconds = self._usage(batch_id=batch_id)["elapsed_seconds"]
        self._execution = (batch_id, invocation_started, previously_accounted_seconds)
        with self.db:
            self.db.execute("UPDATE reingest_batches SET status=?,error=NULL WHERE batch_id=?", ("pilot_running" if phase == "pilot" else "running", batch_id))
            self._event(batch_id, "execution_start", phase=phase, explicit_known_failure_resume=resume_known)
        try:
            documents = self._documents(batch_id)
            if phase == "pilot":
                # Three passes through all four documents; no paper can consume
                # the pilot allocation intended to sample another paper.
                selections = [(d, self.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index LIMIT 1 OFFSET ?", (d["old_source_id"], i)).fetchone()) for i in range(3) for d in documents]
            else:
                selections = [(d, c) for d in documents if not d["published"] for c in self.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (d["old_source_id"],))]
            for document, chunk in selections:
                if not self.db.execute("SELECT 1 FROM reingest_labels WHERE attempt_id=? AND chunk_index=?", (document["attempt_id"], chunk["chunk_index"])).fetchone():
                    if not await self._label(document, chunk):
                        return self.summary(batch_id)
                    if phase == "complete" and self.db.execute("SELECT COUNT(*) FROM reingest_labels WHERE attempt_id=?", (document["attempt_id"],)).fetchone()[0] == document["total_chunks"]:
                        self._publish(document)
                    await asyncio.sleep(0)
            self._assert_old(batch_id)
            with self.db:
                if phase == "pilot":
                    self.db.execute("UPDATE reingest_batches SET status='pilot_passed',pilot_passed=1 WHERE batch_id=?", (batch_id,))
                    self._event(batch_id, "pilot_passed", labelled_blocks=12)
                else:
                    for document in self._documents(batch_id):
                        if not document["published"]:
                            self._publish(document)
                    self.db.execute("UPDATE reingest_batches SET status='completed' WHERE batch_id=?", (batch_id,))
                    self._event(batch_id, "completed")
        except asyncio.CancelledError:
            if self._batch(batch_id)["status"] != "usage_unknown":
                self._stop(batch_id, "interrupted")
            raise
        except Exception as error:
            self._stop(batch_id, getattr(error, "code", type(error).__name__))
        finally:
            accounted = self._usage(batch_id=batch_id)["elapsed_seconds"] - previously_accounted_seconds
            remaining_local_time = max(0, time.perf_counter() - invocation_started - accounted)
            with self.db:
                self._event(batch_id, "local_execution_time", phase=phase, elapsed_seconds=remaining_local_time)
            self._execution = None
            self._active_interval = None
        return self.summary(batch_id)


async def maintenance(config_path, *, source_ids=None, batch_id=None, phase="pilot", operator_confirmed=False, resume_known=False):
    config = load_config(config_path)
    validate_runtime_paths(config)
    lease = InstanceLock(config.state_dir)
    db = adapter = scratch = None
    try:
        if load_config(config_path) != config:
            raise ValueError("configuration changed before maintenance acquired its lease")
        path = validate_managed_path(config.state_dir / "memory.sqlite3", "knowledge database")
        if not path.is_file():
            raise ValueError("maintenance needs an existing database")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if phase == "status" else sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        if phase == "status":
            if source_ids or not batch_id:
                raise ValueError("read-only status requires an existing batch ID")
            service = BoundedReingest(config, db, None, None, None, initialize=False)
        else:
            backup_database(db, config.state_dir)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            graph = MemoryGraph(db, self_marks=(config.name,))
            scratch = ScratchLog(config.scratch_dir)
            key = os.environ.get("OPENAI_API_KEY", "").strip() or load_openai_key(config_path)
            if not key:
                raise ValueError("OpenAI key unavailable")
            key = validate_api_key(key)
            adapter = OpenAIAdapter(config.adapter, scratch, key)
            service = BoundedReingest(config, db, graph, adapter, scratch)
        if source_ids:
            batch_id = service.grant(source_ids, operator_confirmed=operator_confirmed)
        if not batch_id:
            raise ValueError("supply the four source IDs or an existing batch ID")
        result = service.summary(batch_id) if phase == "status" else await service.run(batch_id, phase=phase, resume_known=resume_known)
        _console_notice(_json(result))
        return result
    finally:
        async def cleanup():
            try:
                if adapter:
                    await adapter.close()
            finally:
                try:
                    if scratch:
                        scratch.close()
                finally:
                    try:
                        if db:
                            db.close()
                    finally:
                        lease.close()
        finalizer = asyncio.create_task(cleanup())
        cancelled_again = False
        while True:
            try:
                await asyncio.shield(finalizer)
                break
            except asyncio.CancelledError:
                cancelled_again = True
                if finalizer.done():
                    finalizer.result()
                    break
        if cancelled_again:
            raise asyncio.CancelledError()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-id", action="append")
    parser.add_argument("--batch-id")
    parser.add_argument("--phase", choices=("pilot", "complete", "status"), default="pilot")
    parser.add_argument("--confirm-old-unknown-loss", action="store_true")
    parser.add_argument("--resume-known-failure", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(maintenance(args.config, source_ids=args.source_id, batch_id=args.batch_id,
                         phase=args.phase, operator_confirmed=args.confirm_old_unknown_loss, resume_known=args.resume_known_failure))
        return 0 if result["status"] in {"approved", "pilot_passed", "completed"} else 1
    except (ValueError, GovernedError, RuntimeError) as error:
        _console_notice("Maintenance refused: " + str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
