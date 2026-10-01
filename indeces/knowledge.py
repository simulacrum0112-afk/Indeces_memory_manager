"""Passive versioned file ingestion; no Discord conversation enters this path."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .contracts import GovernedError
from .config import PdfConfig
from .knowledge_directory import RESERVED_ROOT_DIRECTORIES, SUPPORTED_SUFFIXES
from .context import encode
from . import pdf_import
from .prompts import LABEL, LABEL_SCHEMA
from .runtime import label_data


class KnowledgeService:
    def __init__(self, config, store, graph, adapter, scratch):
        self.config, self.store, self.graph, self.adapter, self.scratch = config, store, graph, adapter, scratch
        self.scope = f"{config.discord.guild_id}:knowledge"
        self.root = config.knowledge_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self._wake = asyncio.Event()
        self._convert_wake = asyncio.Event()
        self._convert_serial = asyncio.Lock()
        self.pdf_limits = getattr(config, "pdf", None) or PdfConfig()
        self._tasks = []
        self._errors = {}
        self._closing = False
        self.background_error = None
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_versions (
                source_id TEXT PRIMARY KEY, path TEXT NOT NULL, digest TEXT NOT NULL,
                raw_text TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0, error TEXT,
                scope TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS knowledge_heads (
                path TEXT PRIMARY KEY, source_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                source_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL, start_character INTEGER NOT NULL,
                marks_json TEXT, PRIMARY KEY(source_id,chunk_index));
            CREATE TABLE IF NOT EXISTS knowledge_desired (
                scope TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT NOT NULL,
                PRIMARY KEY(scope,path));
            CREATE TABLE IF NOT EXISTS knowledge_published (
                scope TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT NOT NULL,
                PRIMARY KEY(scope,path));
            CREATE TABLE IF NOT EXISTS knowledge_migration_hold (
                scope TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                source_id TEXT NOT NULL, PRIMARY KEY(scope,path));
            CREATE TABLE IF NOT EXISTS knowledge_schema_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_pdf_versions (
                source_id TEXT PRIMARY KEY, pdf_bytes BLOB NOT NULL,
                metadata_json TEXT, markdown_path TEXT);
        """)
        if "scope" not in {r[1] for r in store.db.execute("PRAGMA table_info(knowledge_versions)")}:
            store.db.execute("ALTER TABLE knowledge_versions ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        self._migrate_legacy()
        # An interrupted HTTP request has unknown usage; do not retry it silently.
        with store.db:
            store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE status='labelling' AND scope=?", (self.scope,))
        # Reconcile any interrupted/older publication before opening Discord.
        # Current publication is one transaction, but recovery also handles an
        # existing partial snapshot without deleting raw knowledge or usage.
        orphaned = store.db.execute("""SELECT DISTINCT r.scope,r.source_id FROM memory_records r
            LEFT JOIN knowledge_versions v ON v.source_id=r.source_id
            LEFT JOIN knowledge_published p ON p.source_id=r.source_id AND p.scope=r.scope
            WHERE r.active=1 AND r.source_id LIKE 'kb:%' AND r.scope=?
              AND (v.status IS NULL OR v.status!='ready' OR v.scope!=r.scope OR p.source_id IS NULL)""", (self.scope,)).fetchall()
        for row in orphaned:
            archived = graph.deactivate_source(row[0], row[1])
            scratch.write("knowledge_recovery_archived", scope=row[0], source_id=row[1],
                          archived_records=archived, reason="non_ready_or_non_current_source", automatically_replayed=False)

    def _migrate_legacy(self):
        if self.store.db.execute("SELECT 1 FROM knowledge_schema_meta WHERE key='published_snapshots'").fetchone():
            return
        notices = []
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            for version in self.store.db.execute("SELECT source_id FROM knowledge_versions WHERE scope=''").fetchall():
                scopes = self.store.db.execute("SELECT DISTINCT scope FROM memory_records WHERE source_id=?", (version[0],)).fetchall()
                if len(scopes) == 1:
                    self.store.db.execute("UPDATE knowledge_versions SET scope=? WHERE source_id=?", (scopes[0][0], version[0]))
            legacy = self.store.db.execute("SELECT v.* FROM knowledge_heads h JOIN knowledge_versions v ON v.source_id=h.source_id").fetchall()
            for row in legacy:
                version = dict(row)
                owner, path, source_id = version["scope"], version["path"], version["source_id"]
                if not owner:
                    # No evidence identifies the old request's Guild. Quarantine
                    # its digest so a restart cannot silently replay unknown usage.
                    self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=COALESCE(error,'legacy_scope_unknown') WHERE source_id=?", (source_id,))
                    self.store.db.execute("INSERT OR IGNORE INTO knowledge_migration_hold VALUES(?,?,?,?)", (self.scope, path, version["digest"], source_id))
                    notices.append((path, source_id, "legacy_scope_unknown"))
                    continue
                self.store.db.execute("INSERT OR IGNORE INTO knowledge_desired VALUES(?,?,?)", (owner, path, source_id))
                if version["status"] == "ready" and self._legacy_complete(owner, source_id):
                    self.store.db.execute("INSERT OR IGNORE INTO knowledge_published VALUES(?,?,?)", (owner, path, source_id))
                elif version["status"] == "ready":
                    self.store.db.execute("UPDATE knowledge_versions SET status='failed',error='legacy_publication_incomplete' WHERE source_id=?", (source_id,))
                    notices.append((path, source_id, "legacy_publication_incomplete"))
                elif version["status"] in {"pending", "labelling"}:
                    reason = version["error"] or ("interrupted_unknown_usage" if version["status"] == "labelling" else "legacy_incomplete_request")
                    self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=? WHERE source_id=?", (reason, source_id))
                    notices.append((path, source_id, reason))
            self.store.db.execute("INSERT INTO knowledge_schema_meta VALUES('published_snapshots','1')")
        for path, source_id, reason in notices:
            self.scratch.write("knowledge_migration_hold", path=path, source_id=source_id, reason=reason,
                               automatically_replayed=False)
            print(f"[{self.config.name}] 知识迁移需更新文件内容：{path} | version={source_id} | {reason}；旧请求不会自动重发。", flush=True)

    def _snapshot_file(self, path):
        if (path.is_symlink() or path.is_junction()
                or not path.resolve().is_relative_to(self.root.resolve())):
            raise GovernedError("knowledge_path_outside_root")
        for parent in path.parents:
            if parent == self.root:
                break
            if parent.is_symlink() or parent.is_junction():
                raise GovernedError("knowledge_path_outside_root")
        limit = self.pdf_limits.max_file_bytes if path.suffix.lower() == ".pdf" else self.config.knowledge.max_file_bytes
        before = path.stat()
        if before.st_size > limit:
            raise GovernedError("knowledge_file_size_limit")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            return None
        if len(raw) > limit:
            raise GovernedError("knowledge_file_size_limit")
        if path.suffix.lower() == ".pdf" and not raw:
            raise GovernedError("knowledge_pdf_empty")
        return (raw if path.suffix.lower() == ".pdf" else raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()

    def _complete_empty(self, version):
        started = time.monotonic()
        try:
            records, previous = self._publish(version, [], deadline=None)
        except Exception as error:
            elapsed = time.monotonic() - started
            code = error.code if isinstance(error, GovernedError) else type(error).__name__
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=?,elapsed_seconds=? WHERE source_id=? AND status='pending'", (code, elapsed, version["source_id"]))
            self._receipt("failed", version, labelled_chunks=0, total_chunks=0, records=0,
                          input_tokens=0, output_tokens=0, elapsed_seconds=elapsed, code=code)
        else:
            elapsed = time.monotonic() - started
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET elapsed_seconds=? WHERE source_id=?", (elapsed, version["source_id"]))
            self.scratch.write("knowledge_version_ready", scope=self.scope, source_id=version["source_id"], path=version["path"],
                               digest=version["digest"], records=records, previous_source_id=previous)
            self._receipt("completed", version, labelled_chunks=0, total_chunks=0, records=0,
                          input_tokens=0, output_tokens=0, elapsed_seconds=elapsed)

    def _legacy_complete(self, scope, source_id):
        chunks = self.store.db.execute("SELECT chunk_index,start_character,text,marks_json FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
        version = self.store.db.execute("SELECT raw_text FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
        try:
            if version is None or any(c["marks_json"] is None for c in chunks):
                return False
            raw, through = version[0], 0
            for chunk in chunks:
                start, text = chunk["start_character"], chunk["text"]
                if (chunk["chunk_index"] < 0 or start < through or not text.strip()
                        or raw[through:start].strip() or raw[start:start + len(text)] != text):
                    return False
                through = start + len(text)
            if raw[through:].strip():
                return False
            expected = {(c["text"], c["text"], tuple(self.graph._marks(json.loads(c["marks_json"])))) for c in chunks}
            actual = {(r["text"], r["quote"], tuple(json.loads(r["marks_json"]))) for r in self.store.db.execute(
                "SELECT text,quote,marks_json FROM memory_records WHERE scope=? AND source_id=? AND active=1", (scope, source_id))}
            return bool(chunks) and expected == actual
        except (ValueError, TypeError):
            return False

    def _published(self, path):
        row = self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=? AND path=?", (self.scope, path)).fetchone()
        return row[0] if row else None

    def _receipt(self, event, version, **fields):
        current = self._published(version["path"])
        self.scratch.write("knowledge_receipt", receipt=event, scope=self.scope,
                           path=version["path"], source_id=version["source_id"], digest=version["digest"],
                           reply_source_id=current, **fields)
        titles = {"queued": "知识更新已接收", "started": "后台标词开始", "progress": "后台标词进度",
                  "completed": "知识库更新完成，检索已切换", "failed": "标词未完成，继续使用已发布版本",
                  "cancelled": "标词已中断，继续使用已发布版本"}
        if current is None and event in {"failed", "cancelled"}:
            titles[event] = "标词未完成，无已完成版本可用于回复"
        detail = " | ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{self.config.name}] {titles[event]}：{version['path']} | version={version['source_id']} | digest={version['digest']} | 回复版本={current or '无已完成版本'} | {detail}", flush=True)

    def _conversion_receipt(self, event, version, **fields):
        current = self._published(version["path"])
        self.scratch.write("knowledge_pdf_receipt", receipt=event, scope=self.scope,
                           path=version["path"], source_id=version["source_id"], digest=version["digest"],
                           reply_source_id=current, **fields)
        titles = {"queued": "PDF 更新已接收，等待后台转换", "started": "PDF 后台转换开始",
                  "converted": "PDF 转换完成，等待被动标词", "failed": "PDF 转换未完成，已发布版本保留",
                  "cancelled": "PDF 转换已中断，已发布版本保留"}
        detail = " | ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{self.config.name}] {titles[event]}：{version['path']} | version={version['source_id']} | digest={version['digest']} | 回复版本={current or '无已完成版本'} | {detail}", flush=True)

    def _supersede_desired(self, path):
        row = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=?", (self.scope, path)).fetchone()
        if row and row[0] != self._published(path):
            self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error='newer_file_snapshot' WHERE source_id=? AND scope=?", (row[0], self.scope))

    def _retire(self, path, reason):
        rows = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=? UNION SELECT source_id FROM knowledge_published WHERE scope=? AND path=?", (self.scope, path, self.scope, path)).fetchall()
        held = self.store.db.execute("SELECT 1 FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, path)).fetchone()
        if not rows and held is None:
            return
        archived = 0
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            for row in rows:
                archived += self.graph.deactivate_source(self.scope, row[0], commit=False)
                self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error=? WHERE source_id=? AND scope=?", (reason, row[0], self.scope))
            for table in ("knowledge_desired", "knowledge_published", "knowledge_migration_hold"):
                self.store.db.execute(f"DELETE FROM {table} WHERE scope=? AND path=?", (self.scope, path))
        self.scratch.write("knowledge_retired", scope=self.scope, path=path, source_ids=[r[0] for r in rows], archived_records=archived, reason=reason)
        print(f"[{self.config.name}] 知识来源已撤下：{path} | reason={reason} | archived={archived}；不再用于回复。", flush=True)

    def _scan_files(self):
        files = []
        def scan_failed(error):
            # An incomplete census must not look like source deletion. Keep
            # the previous published snapshots until a complete scan succeeds.
            raise GovernedError("knowledge_directory_scan_failed") from None

        for directory, subdirectories, names in os.walk(self.root, topdown=True, followlinks=False,
                                                       onerror=scan_failed):
            parent = Path(directory)
            # Prune storage/management roots before descent, so a large staging
            # collection is not enumerated on every passive scan. Nested names
            # remain ordinary source folders. Never traverse directory aliases.
            subdirectories[:] = [name for name in subdirectories
                                 if not (parent == self.root and name.casefold() in RESERVED_ROOT_DIRECTORIES)
                                 and not (parent / name).is_symlink()
                                 and not (parent / name).is_junction()]
            for name in names:
                path = parent / name
                if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if (stat.S_ISREG(info.st_mode) and not path.is_symlink()
                        and not (getattr(info, "st_file_attributes", 0)
                                 & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))):
                    files.append(path)
                    # One extra candidate proves overflow; do not read contents
                    # or silently truncate the eligible knowledge scope.
                    if len(files) > self.config.knowledge.max_files:
                        return sorted(files)
        return sorted(files)

    def _check_file_count(self, files):
        if len(files) > self.config.knowledge.max_files:
            # A failed global scan cannot keep old, possibly changed/deleted
            # sources silently eligible. Preserve them as archived versions.
            heads = self.store.db.execute("SELECT path FROM knowledge_desired WHERE scope=? UNION SELECT path FROM knowledge_published WHERE scope=? UNION SELECT path FROM knowledge_migration_hold WHERE scope=?", (self.scope, self.scope, self.scope)).fetchall()
            for head in heads:
                self._retire(head[0], "knowledge_file_count_limit")
            if heads:
                self.scratch.write("knowledge_scope_suspended", scope=self.scope,
                                   reason="knowledge_file_count_limit", retired_heads=len(heads))
            raise GovernedError("knowledge_file_count_limit")
    def _apply_snapshot(self, path, snapshot, error=None):
        relative = path.relative_to(self.root).as_posix()
        if error is not None:
            code = error.code if isinstance(error, GovernedError) else type(error).__name__
            self._retire(relative, code)
            if self._errors.get(relative) != code:
                self.scratch.write("knowledge_update_rejected", path=relative, code=code)
                print(f"[{self.config.name}] knowledge rejected: {relative}; {code}", flush=True)
                self._errors[relative] = code
            return
        if snapshot is None:
            return
        content, digest = snapshot
        head = self.store.db.execute("SELECT v.digest FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.path=?", (self.scope, relative)).fetchone()
        if head and head[0] == digest:
            return
        hold = self.store.db.execute("SELECT digest FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, relative)).fetchone()
        if hold and hold[0] == digest:
            return
        is_pdf = path.suffix.lower() == ".pdf"
        text = "" if is_pdf else content
        source_id = "kb:" + uuid.uuid4().hex
        chunks = [] if is_pdf else self._chunks(text)
        with self.store.db:
            self._supersede_desired(relative)
            self.store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                (source_id, relative, digest, text, "converting" if is_pdf else "pending", time.time(), self.scope))
            self.store.db.execute("INSERT INTO knowledge_desired VALUES(?,?,?) ON CONFLICT(scope,path) DO UPDATE SET source_id=excluded.source_id", (self.scope, relative, source_id))
            self.store.db.execute("DELETE FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, relative))
            if is_pdf:
                self.store.db.execute("INSERT INTO knowledge_pdf_versions(source_id,pdf_bytes) VALUES(?,?)", (source_id, content))
            else:
                self._insert_chunks(source_id, chunks)
        self._errors.pop(relative, None)
        self.scratch.write("knowledge_update", source_id=source_id, path=relative, digest=digest,
                           raw_text=text, chunk_count=len(chunks), scope=self.scope,
                           source_format="pdf" if is_pdf else "text")
        version = {"path": relative, "source_id": source_id, "digest": digest}
        if is_pdf:
            self._convert_wake.set()
            self._conversion_receipt("queued", version, original_bytes=len(content))
        else:
            self._wake.set()
            self._receipt("queued", version, chunks=len(chunks))
            if not chunks:
                self._complete_empty(version)

    def _chunks(self, text):
        width = self.config.knowledge.chunk_characters
        return [(i // width, text[i:i + width], i) for i in range(0, len(text), width) if text[i:i + width].strip()]

    def _insert_chunks(self, source_id, chunks):
        self.store.db.executemany("INSERT INTO knowledge_chunks(source_id,chunk_index,text,start_character) VALUES(?,?,?,?)",
                                 [(source_id, i, chunk, start) for i, chunk, start in chunks])

    def _retire_missing(self, seen):
        heads = self.store.db.execute("SELECT path FROM knowledge_desired WHERE scope=? UNION SELECT path FROM knowledge_published WHERE scope=? UNION SELECT path FROM knowledge_migration_hold WHERE scope=?", (self.scope, self.scope, self.scope)).fetchall()
        for head in heads:
            if head[0] not in seen:
                self._retire(head[0], "source_file_removed")

    def scan_once(self, *, prepared=None):
        """Queue stable source snapshots; direct callers run synchronously.

        The watcher supplies one prepared file at a time so filesystem reads
        happen in a thread while all SQLite mutations remain on its owner loop.
        No prepared batch retains more than one original PDF in memory.
        """
        if prepared is not None:
            action, value = prepared
            if action == "begin":
                self._check_file_count(value)
            elif action == "file":
                self._apply_snapshot(*value)
            elif action == "end":
                self._retire_missing(value)
            else:
                raise ValueError("invalid prepared knowledge scan")
            return
        files = self._scan_files()
        self._check_file_count(files)
        seen = set()
        for path in files:
            seen.add(path.relative_to(self.root).as_posix())
            try:
                snapshot = self._snapshot_file(path)
            except (OSError, UnicodeError, GovernedError) as error:
                self._apply_snapshot(path, None, error)
            else:
                self._apply_snapshot(path, snapshot)
        self._retire_missing(seen)

    def _is_current(self, source_id):
        row = self.store.db.execute("SELECT v.path,v.digest FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.source_id=? AND v.scope=?", (self.scope, source_id, self.scope)).fetchone()
        if row is None:
            return False
        path = self.root / row[0]
        try:
            snapshot = self._snapshot_file(path)
            return snapshot is not None and snapshot[1] == row[1]
        except (OSError, UnicodeError, GovernedError):
            return False

    def _state_directory(self):
        configured = getattr(self.config, "state_dir", None)
        if configured is not None:
            return Path(configured)
        filename = next(row[2] for row in self.store.db.execute("PRAGMA database_list") if row[1] == "main")
        if not filename:
            raise GovernedError("pdf_export_state_unavailable")
        return Path(filename).parent

    async def convert_next(self) -> bool:
        """Convert one frozen PDF without taking the model adaptor's slot."""
        async with self._convert_serial:
            row = self.store.db.execute("""SELECT v.*,length(p.pdf_bytes) AS pdf_bytes_length,
                substr(p.pdf_bytes,1,?) AS pdf_bytes FROM knowledge_versions v
                JOIN knowledge_pdf_versions p ON p.source_id=v.source_id
                JOIN knowledge_desired h ON h.source_id=v.source_id
                WHERE h.scope=? AND v.scope=? AND v.status='converting'
                ORDER BY v.created_at LIMIT 1""", (self.pdf_limits.max_file_bytes + 1, self.scope, self.scope)).fetchone()
            if row is None:
                return False
            version = dict(row)
            source_id = version["source_id"]
            started = time.monotonic()
            self._conversion_receipt("started", version, original_bytes=version["pdf_bytes_length"],
                                     budget=asdict(self.pdf_limits), model_requests=0, input_tokens=0, output_tokens=0)
            try:
                if not self._is_current(source_id):
                    raise GovernedError("knowledge_version_superseded")
                raw = version.pop("pdf_bytes")
                if (version["pdf_bytes_length"] > self.pdf_limits.max_file_bytes
                        or hashlib.sha256(raw).hexdigest() != version["digest"]):
                    raise GovernedError("pdf_frozen_snapshot_invalid")
                async with asyncio.timeout(self.pdf_limits.seconds):
                    result = await pdf_import.convert_pdf(raw, self.pdf_limits)
                markdown, metadata = result["markdown"], result["metadata"]
                if len(markdown.encode("utf-8")) > self.pdf_limits.max_markdown_bytes:
                    raise GovernedError("pdf_output_limit")
                pdf_import.validate_conversion(markdown, metadata, pdf_digest=version["digest"])
                if metadata["page_count"] > self.pdf_limits.max_pages:
                    raise GovernedError("pdf_page_limit")
                if time.monotonic() - started >= self.pdf_limits.seconds:
                    raise GovernedError("pdf_time_limit")
                if not self._is_current(source_id):
                    raise GovernedError("knowledge_version_superseded")
                state_directory = self._state_directory()
                if (state_directory / "pdf_markdown").resolve().is_relative_to(self.root.resolve()):
                    raise GovernedError("pdf_export_inside_knowledge")
                output = pdf_import.publish_markdown(state_directory, source_id, markdown)
                chunks = self._chunks(markdown)
                if not chunks:
                    raise GovernedError("pdf_needs_ocr_or_review")
                with self.store.db:
                    self.store.db.execute("BEGIN IMMEDIATE")
                    if not self._is_current(source_id):
                        raise GovernedError("knowledge_version_superseded")
                    if time.monotonic() - started >= self.pdf_limits.seconds:
                        raise GovernedError("pdf_time_limit")
                    self.store.db.execute("UPDATE knowledge_pdf_versions SET metadata_json=?,markdown_path=? WHERE source_id=?",
                                          (json.dumps(metadata, ensure_ascii=False, sort_keys=True), str(output), source_id))
                    self.store.db.execute("UPDATE knowledge_versions SET raw_text=?,status='pending',error=NULL WHERE source_id=? AND status='converting'",
                                          (markdown, source_id))
                    self._insert_chunks(source_id, chunks)
            except asyncio.CancelledError:
                # Local conversion is safe to recover from frozen bytes after a
                # restart. No model request has begun, so usage is still known.
                with self.store.db:
                    self.store.db.execute("UPDATE knowledge_versions SET error='pdf_conversion_interrupted' WHERE source_id=? AND status='converting'", (source_id,))
                self._conversion_receipt("cancelled", version, code="pdf_conversion_interrupted",
                                         elapsed_seconds=time.monotonic() - started, model_requests=0,
                                         input_tokens=0, output_tokens=0)
                raise
            except Exception as error:
                code = error.code if isinstance(error, GovernedError) else "pdf_time_limit" if isinstance(error, TimeoutError) else type(error).__name__
                with self.store.db:
                    self.store.db.execute("UPDATE knowledge_versions SET status=?,error=? WHERE source_id=? AND status='converting'",
                                          ("superseded" if code == "knowledge_version_superseded" else "failed", code, source_id))
                self._conversion_receipt("failed", version, code=code, elapsed_seconds=time.monotonic() - started,
                                         model_requests=0, input_tokens=0, output_tokens=0)
            else:
                # Audit failures after a durable conversion are supervised as
                # background failures, never mislabeled as parser failures.
                self.scratch.write("knowledge_pdf_converted", scope=self.scope, source_id=source_id, path=version["path"],
                                   digest=version["digest"], raw_text=markdown, metadata=metadata, markdown_path=str(output),
                                   chunk_count=len(chunks), elapsed_seconds=time.monotonic() - started,
                                   original_bytes=version["pdf_bytes_length"], markdown_bytes=len(markdown.encode("utf-8")),
                                   budget=asdict(self.pdf_limits), model_requests=0, input_tokens=0, output_tokens=0)
                self._conversion_receipt("converted", version, pages=metadata["page_count"], chunks=len(chunks),
                                         markdown_path=str(output), elapsed_seconds=time.monotonic() - started,
                                         model_requests=0, input_tokens=0, output_tokens=0)
                self._wake.set()
            return True

    def _publish(self, version, facts, deadline):
        previous = self._published(version["path"])
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            if not self._is_current(version["source_id"]):
                raise GovernedError("knowledge_version_superseded")
            if previous and previous != version["source_id"]:
                self.graph.deactivate_source(self.scope, previous, commit=False)
            records = self.graph.add(self.scope, version["source_id"], "knowledge:" + version["path"], facts, time.time(), commit=False)
            if not self._is_current(version["source_id"]) or (deadline is not None and time.monotonic() >= deadline):
                raise GovernedError("knowledge_publication_snapshot_or_deadline_changed")
            self.store.db.execute("INSERT INTO knowledge_published VALUES(?,?,?) ON CONFLICT(scope,path) DO UPDATE SET source_id=excluded.source_id", (self.scope, version["path"], version["source_id"]))
            if previous and previous != version["source_id"]:
                self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error='completed_replacement' WHERE source_id=? AND scope=?", (previous, self.scope))
            self.store.db.execute("UPDATE knowledge_versions SET status='ready',error=NULL WHERE source_id=? AND scope=?", (version["source_id"], self.scope))
        return records, previous

    async def label_next(self) -> bool:
        row = self.store.db.execute("SELECT v.* FROM knowledge_versions v JOIN knowledge_desired h ON h.source_id=v.source_id WHERE h.scope=? AND v.scope=? AND v.status='pending' ORDER BY v.created_at LIMIT 1", (self.scope, self.scope)).fetchone()
        if row is None:
            return False
        version = dict(row)
        source_id = version["source_id"]
        trace_id = uuid.uuid4().hex
        started = time.monotonic()
        limits = self.config.knowledge
        budget = self.config.adapter.budgets["label"]
        outcome = "failed"
        error_code = None
        request_failed = False
        records = []
        chunks = self.store.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='labelling' WHERE source_id=?", (source_id,))
        self.scratch.write("label_job_start", trace_id=trace_id, source_id=source_id, path=version["path"], digest=version["digest"],
                           input_limit=limits.version_input_tokens, output_limit=limits.version_output_tokens, seconds=limits.version_seconds)
        self._receipt("started", version, total_chunks=len(chunks), trace_id=trace_id)
        try:
            remaining_time = limits.version_seconds - version["elapsed_seconds"]
            if remaining_time <= 0:
                raise GovernedError("knowledge_version_time_limit")
            deadline = started + remaining_time
            async with asyncio.timeout(remaining_time):
                for index, chunk in enumerate(chunks, 1):
                    if not self._is_current(source_id):
                        raise GovernedError("knowledge_version_superseded")
                    if chunk["marks_json"] is not None:
                        continue
                    if version["input_tokens"] + budget.input_tokens > limits.version_input_tokens or version["output_tokens"] + budget.output_tokens > limits.version_output_tokens:
                        raise GovernedError("knowledge_version_token_limit")
                    try:
                        result = await self.adapter.call("label", LABEL, [{"role": "user", "content": encode({
                            "source_id": source_id, "path": version["path"], "digest": version["digest"],
                            "chunk_index": chunk["chunk_index"], "start_character": chunk["start_character"], "text": chunk["text"]})}], trace_id, LABEL_SCHEMA)
                    except BaseException:
                        request_failed = True
                        raise
                    version["input_tokens"] += result.input_tokens
                    version["output_tokens"] += result.output_tokens
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_versions SET input_tokens=?,output_tokens=? WHERE source_id=?", (version["input_tokens"], version["output_tokens"], source_id))
                    marks = label_data(result.text)
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_chunks SET marks_json=? WHERE source_id=? AND chunk_index=?", (json.dumps(marks, ensure_ascii=False), source_id, chunk["chunk_index"]))
                    self.scratch.write("knowledge_chunk_labelled", trace_id=trace_id, source_id=source_id,
                                       chunk_index=chunk["chunk_index"], marks=marks, quote=chunk["text"])
                    self._receipt("progress", version, labelled_chunks=index, total_chunks=len(chunks), trace_id=trace_id)
                if not self._is_current(source_id):
                    raise GovernedError("knowledge_version_superseded")
                if time.monotonic() >= deadline:
                    raise GovernedError("knowledge_version_time_limit")
                chunks = self.store.db.execute("SELECT * FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
                facts = [{"text": c["text"], "quote": c["text"], "marks": json.loads(c["marks_json"])} for c in chunks]
                records, previous = self._publish(version, facts, deadline)
                outcome = "completed"
        except asyncio.CancelledError:
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE source_id=? AND status='labelling'", (source_id,))
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="cancelled", remote_usage_unknown=True)
            outcome = "cancelled"
            raise
        except Exception as error:
            code = error.code if isinstance(error, GovernedError) else "knowledge_version_time_limit" if isinstance(error, TimeoutError) else type(error).__name__
            error_code = code
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=? WHERE source_id=? AND status='labelling'", (code, source_id))
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="failed", code=code)
        else:
            self.scratch.write("knowledge_version_ready", trace_id=trace_id, source_id=source_id, path=version["path"],
                               scope=self.scope, digest=version["digest"], records=records, previous_source_id=previous)
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="ready")
        finally:
            elapsed = time.monotonic() - started
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET elapsed_seconds=elapsed_seconds+? WHERE source_id=?", (elapsed, source_id))
            accounting = self.store.db.execute("SELECT status,input_tokens,output_tokens,elapsed_seconds FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
            self.scratch.write("label_job_accounting", trace_id=trace_id, source_id=source_id,
                               status=accounting[0], input_tokens=accounting[1], output_tokens=accounting[2],
                               total_elapsed_seconds=accounting[3], invocation_elapsed_seconds=elapsed)
            labelled = self.store.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=? AND marks_json IS NOT NULL", (source_id,)).fetchone()[0]
            self._receipt(outcome, version, labelled_chunks=labelled, total_chunks=len(chunks), records=len(records),
                          input_tokens=accounting[1], output_tokens=accounting[2], elapsed_seconds=accounting[3],
                          code=error_code, trace_id=trace_id,
                          remote_usage_unknown=request_failed and error_code not in {
                              "input_token_limit", "circuit_open", "missing_openai_key", "invalid_input_token_count"})
        return True

    async def _watch(self):
        while True:
            try:
                files = await asyncio.to_thread(self._scan_files)
                self.scan_once(prepared=("begin", files))
                seen = set()
                for path in files:
                    seen.add(path.relative_to(self.root).as_posix())
                    try:
                        snapshot = await asyncio.to_thread(self._snapshot_file, path)
                    except (OSError, UnicodeError, GovernedError) as error:
                        self.scan_once(prepared=("file", (path, None, error)))
                    else:
                        self.scan_once(prepared=("file", (path, snapshot)))
                        # Release this file's frozen bytes before reading the
                        # next file; only the database retains prior versions.
                        del snapshot
                self.scan_once(prepared=("end", seen))
                self._errors.pop("<scan>", None)
            except Exception as error:
                code = error.code if isinstance(error, GovernedError) else type(error).__name__
                if self._errors.get("<scan>") != code:
                    self.scratch.write("knowledge_scan_failed", code=code)
                    print(f"[{self.config.name}] knowledge scan failed: {code}", flush=True)
                    self._errors["<scan>"] = code
            await asyncio.sleep(self.config.knowledge.poll_seconds)

    async def _label_worker(self):
        while True:
            self._wake.clear()
            if await self.label_next():
                continue
            await self._wake.wait()

    async def _convert_worker(self):
        while True:
            self._convert_wake.clear()
            if await self.convert_next():
                continue
            await self._convert_wake.wait()

    def _background_done(self, task):
        if self._closing or self.background_error is not None:
            return
        error = None if task.cancelled() else task.exception()
        code = "unexpected_cancel" if task.cancelled() else type(error).__name__ if error else "unexpected_exit"
        self.background_error = code
        # Keep replies on the last complete snapshot, but stop accepting a
        # growing update queue with a dead label worker. Never replay its call.
        print(f"[{self.config.name}] 知识库后台已暂停：{code}；更新与标词无法继续，已发布知识版本保留。请检查本地文件/日志后停机重启。", flush=True)
        try:
            self.scratch.write("knowledge_background_stopped", scope=self.scope, task=task.get_name(),
                               code=code, automatically_replayed=False)
        except Exception:
            print(f"[{self.config.name}] 后台故障回执无法写入 scratch，请检查日志目录权限。", flush=True)
        for pending in self._tasks:
            if pending is not task and not pending.done():
                pending.cancel()

    def start(self):
        if self._tasks:
            raise RuntimeError("knowledge service already started")
        self._closing = False
        self.background_error = None
        self._tasks = [asyncio.create_task(self._watch(), name="indeces-knowledge-watch"),
                       asyncio.create_task(self._label_worker(), name="indeces-passive-label"),
                       asyncio.create_task(self._convert_worker(), name="indeces-pdf-convert")]
        for task in self._tasks:
            task.add_done_callback(self._background_done)

    async def close(self):
        self._closing = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
