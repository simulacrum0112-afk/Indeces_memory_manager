"""Passive versioned file ingestion; no Discord conversation enters this path."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

from .contracts import GovernedError
from .context import encode
from .prompts import LABEL, LABEL_SCHEMA
from .runtime import label_data


class KnowledgeService:
    def __init__(self, config, store, graph, adapter, scratch):
        self.config, self.store, self.graph, self.adapter, self.scratch = config, store, graph, adapter, scratch
        self.scope = f"{config.discord.guild_id}:knowledge"
        self.root = config.knowledge_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self._wake = asyncio.Event()
        self._tasks = []
        self._errors = {}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                source_id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT NOT NULL,
                raw_text TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS knowledge_heads (
                path TEXT PRIMARY KEY, source_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, start_character INTEGER NOT NULL,
                marks_json TEXT, PRIMARY KEY(source_id,chunk_index));
        """)
        # An interrupted HTTP request has unknown usage; do not retry it silently.
        with store.db:
            store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE status='labelling'")
        # Reconcile any interrupted/older publication before opening Discord.
        # Current publication is one transaction, but recovery also handles an
        # existing partial snapshot without deleting raw knowledge or usage.
        orphaned = store.db.execute("""SELECT DISTINCT r.scope,r.source_id FROM memory_records r
            LEFT JOIN knowledge_versions v ON v.source_id=r.source_id
            LEFT JOIN knowledge_heads h ON h.source_id=r.source_id
            WHERE r.active=1 AND r.source_id LIKE 'kb:%'
              AND (v.status IS NULL OR v.status!='ready' OR h.source_id IS NULL)""").fetchall()
        for row in orphaned:
            archived = graph.deactivate_source(row[0], row[1])
            scratch.write("knowledge_recovery_archived", scope=row[0], source_id=row[1],
                          archived_records=archived, reason="non_ready_or_non_current_source", automatically_replayed=False)

    def _retire(self, path, reason):
        head = self.store.db.execute("SELECT source_id FROM knowledge_heads WHERE path=?", (path,)).fetchone()
        if head is None:
            return
        source_id = head[0]
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            archived = self.graph.deactivate_source(self.scope, source_id, commit=False)
            self.store.db.execute("DELETE FROM knowledge_heads WHERE path=?", (path,))
            self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error=? WHERE source_id=?", (reason, source_id))
        self.scratch.write("knowledge_retired", path=path, source_id=source_id, archived_records=archived, reason=reason)

    def scan_once(self):
        """Detect stable UTF-8 snapshots, then durably queue the changed version.

        Poll latency is bounded by poll_seconds while the event loop is available.
        File changes during a read are deferred to the next poll.
        """
        files = sorted(p for p in self.root.rglob("*") if p.suffix.lower() in {".md", ".markdown", ".txt"} and p.is_file())
        if len(files) > self.config.knowledge.max_files:
            # A failed global scan cannot keep old, possibly changed/deleted
            # sources silently eligible. Preserve them as archived versions.
            heads = self.store.db.execute("SELECT path FROM knowledge_heads").fetchall()
            for head in heads:
                self._retire(head[0], "knowledge_file_count_limit")
            if heads:
                self.scratch.write("knowledge_scope_suspended", scope=self.scope,
                                   reason="knowledge_file_count_limit", retired_heads=len(heads))
            raise GovernedError("knowledge_file_count_limit")
        seen = set()
        for path in files:
            relative = path.relative_to(self.root).as_posix()
            seen.add(relative)
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                    raise GovernedError("knowledge_path_outside_root")
                before = path.stat()
                if before.st_size > self.config.knowledge.max_file_bytes:
                    raise GovernedError("knowledge_file_size_limit")
                with path.open("rb") as stream:
                    raw = stream.read(self.config.knowledge.max_file_bytes + 1)
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    continue
                if len(raw) > self.config.knowledge.max_file_bytes:
                    raise GovernedError("knowledge_file_size_limit")
                text = raw.decode("utf-8-sig")
                digest = hashlib.sha256(raw).hexdigest()
                head = self.store.db.execute("SELECT v.digest FROM knowledge_heads h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.path=?", (relative,)).fetchone()
                if head and head[0] == digest:
                    continue
                self._retire(relative, "content_replaced")
                source_id = "kb:" + uuid.uuid4().hex
                width = self.config.knowledge.chunk_characters
                chunks = [(i // width, text[i:i + width], i) for i in range(0, len(text), width) if text[i:i + width].strip()]
                with self.store.db:
                    self.store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at) VALUES(?,?,?,?,?,?)",
                        (source_id, relative, digest, text, "pending" if chunks else "ready", time.time()))
                    self.store.db.execute("INSERT INTO knowledge_heads VALUES(?,?)", (relative, source_id))
                    self.store.db.executemany("INSERT INTO knowledge_chunks(source_id,chunk_index,text,start_character) VALUES(?,?,?,?)",
                        [(source_id, i, chunk, start) for i, chunk, start in chunks])
                self.scratch.write("knowledge_update", source_id=source_id, path=relative, digest=digest,
                                   raw_text=text, chunk_count=len(chunks), scope=self.scope)
                self._wake.set()
                self._errors.pop(relative, None)
                print(f"[Indeces] knowledge queued: {relative} ({len(chunks)} chunks)", flush=True)
            except (OSError, UnicodeError, GovernedError) as error:
                code = error.code if isinstance(error, GovernedError) else type(error).__name__
                self._retire(relative, code)
                if self._errors.get(relative) != code:
                    self.scratch.write("knowledge_update_rejected", path=relative, code=code)
                    print(f"[Indeces] knowledge rejected: {relative}; {code}", flush=True)
                    self._errors[relative] = code
        heads = self.store.db.execute("SELECT path FROM knowledge_heads").fetchall()
        for head in heads:
            if head[0] not in seen:
                self._retire(head[0], "source_file_removed")

    def _is_current(self, source_id):
        row = self.store.db.execute("SELECT v.path,v.digest FROM knowledge_heads h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.source_id=?", (source_id,)).fetchone()
        if row is None:
            return False
        path = self.root / row[0]
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                return False
            before = path.stat()
            with path.open("rb") as stream:
                raw = stream.read(self.config.knowledge.max_file_bytes + 1)
            after = path.stat()
            return (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size) and hashlib.sha256(raw).hexdigest() == row[1]
        except OSError:
            return False

    async def label_next(self) -> bool:
        row = self.store.db.execute("SELECT v.* FROM knowledge_versions v JOIN knowledge_heads h ON h.source_id=v.source_id WHERE v.status='pending' ORDER BY v.created_at LIMIT 1").fetchone()
        if row is None:
            return False
        version = dict(row)
        source_id = version["source_id"]
        trace_id = uuid.uuid4().hex
        started = time.monotonic()
        limits = self.config.knowledge
        budget = self.config.adapter.budgets["label"]
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling' WHERE source_id=?", (source_id,))
        self.scratch.write("label_job_start", trace_id=trace_id, source_id=source_id, path=version["path"], digest=version["digest"],
                           input_limit=limits.version_input_tokens, output_limit=limits.version_output_tokens, seconds=limits.version_seconds)
        try:
            remaining_time = limits.version_seconds - version["elapsed_seconds"]
            if remaining_time <= 0:
                raise GovernedError("knowledge_version_time_limit")
            deadline = started + remaining_time
            async with asyncio.timeout(remaining_time):
                chunks = self.store.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
                for chunk in chunks:
                    if not self._is_current(source_id):
                        raise GovernedError("knowledge_version_superseded")
                    if chunk["marks_json"] is not None:
                        continue
                    if version["input_tokens"] + budget.input_tokens > limits.version_input_tokens or version["output_tokens"] + budget.output_tokens > limits.version_output_tokens:
                        raise GovernedError("knowledge_version_token_limit")
                    result = await self.adapter.call("label", LABEL, [{"role": "user", "content": encode({
                        "source_id": source_id, "path": version["path"], "digest": version["digest"],
                        "chunk_index": chunk["chunk_index"], "start_character": chunk["start_character"], "text": chunk["text"]})}], trace_id, LABEL_SCHEMA)
                    version["input_tokens"] += result.input_tokens
                    version["output_tokens"] += result.output_tokens
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_versions SET input_tokens=?,output_tokens=? WHERE source_id=?", (version["input_tokens"], version["output_tokens"], source_id))
                    marks = label_data(result.text)
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_chunks SET marks_json=? WHERE source_id=? AND chunk_index=?", (json.dumps(marks, ensure_ascii=False), source_id, chunk["chunk_index"]))
                    self.scratch.write("knowledge_chunk_labelled", trace_id=trace_id, source_id=source_id,
                                       chunk_index=chunk["chunk_index"], marks=marks, quote=chunk["text"])
                if not self._is_current(source_id):
                    raise GovernedError("knowledge_version_superseded")
                if time.monotonic() >= deadline:
                    raise GovernedError("knowledge_version_time_limit")
                chunks = self.store.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
                facts = [{"text": c["text"], "quote": c["text"], "marks": json.loads(c["marks_json"])} for c in chunks]
                with self.store.db:
                    self.store.db.execute("BEGIN IMMEDIATE")
                    records = self.graph.add(self.scope, source_id, "knowledge:" + version["path"], facts, time.time(), commit=False)
                    if not self._is_current(source_id) or time.monotonic() >= deadline:
                        raise GovernedError("knowledge_publication_snapshot_or_deadline_changed")
                    self.store.db.execute("UPDATE knowledge_versions SET status='ready' WHERE source_id=?", (source_id,))
                self.scratch.write("knowledge_version_ready", trace_id=trace_id, source_id=source_id, path=version["path"],
                                   digest=version["digest"], records=records)
                print(f"[Indeces] knowledge ready: {version['path']}", flush=True)
        except asyncio.CancelledError:
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE source_id=? AND status='labelling'", (source_id,))
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="cancelled", remote_usage_unknown=True)
            raise
        except Exception as error:
            code = error.code if isinstance(error, GovernedError) else "knowledge_version_time_limit" if isinstance(error, TimeoutError) else type(error).__name__
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=? WHERE source_id=? AND status='labelling'", (code, source_id))
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="failed", code=code)
            print(f"[Indeces] labelling failed: {version['path']}; {code}", flush=True)
        else:
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="ready")
        finally:
            elapsed = time.monotonic() - started
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET elapsed_seconds=elapsed_seconds+? WHERE source_id=?", (elapsed, source_id))
            accounting = self.store.db.execute("SELECT status,input_tokens,output_tokens,elapsed_seconds FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
            self.scratch.write("label_job_accounting", trace_id=trace_id, source_id=source_id,
                               status=accounting[0], input_tokens=accounting[1], output_tokens=accounting[2],
                               total_elapsed_seconds=accounting[3], invocation_elapsed_seconds=elapsed)
        return True

    async def _watch(self):
        while True:
            try:
                self.scan_once()
            except Exception as error:
                code = error.code if isinstance(error, GovernedError) else type(error).__name__
                if self._errors.get("<scan>") != code:
                    self.scratch.write("knowledge_scan_failed", code=code)
                    print(f"[Indeces] knowledge scan failed: {code}", flush=True)
                    self._errors["<scan>"] = code
            await asyncio.sleep(self.config.knowledge.poll_seconds)

    async def _label_worker(self):
        while True:
            self._wake.clear()
            if await self.label_next():
                continue
            await self._wake.wait()

    def start(self):
        if self._tasks:
            raise RuntimeError("knowledge service already started")
        self._tasks = [asyncio.create_task(self._watch(), name="indeces-knowledge-watch"),
                       asyncio.create_task(self._label_worker(), name="indeces-passive-label")]

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
