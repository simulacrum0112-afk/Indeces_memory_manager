"""Passive versioned file ingestion; no Discord conversation enters this path."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from .contracts import GovernedError, validated_token_usage
from .config import PdfConfig
from .knowledge_directory import RESERVED_ROOT_DIRECTORIES, SUPPORTED_SUFFIXES
from .context import encode
from . import pdf_import
from .prompts import LABEL, LABEL_SCHEMA
from .runtime import label_data
from .path_policy import validate_knowledge_root, validate_managed_path, PathPolicyError


def _console_notice(message):
    """Keep strict ANSI redirection usable; propagate ordinary I/O failures."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


class KnowledgeService:
    def __init__(self, config, store, graph, adapter, scratch):
        self.config, self.store, self.graph, self.adapter, self.scratch = config, store, graph, adapter, scratch
        self.scope = f"{config.discord.guild_id}:knowledge"
        self.root = validate_knowledge_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self._wake = asyncio.Event()
        self._convert_wake = asyncio.Event()
        self._convert_serial = asyncio.Lock()
        self.pdf_limits = getattr(config, "pdf", None) or PdfConfig()
        self._tasks = []
        self._filesystem_tasks = set()
        self._close_task = None
        self._errors = {}
        self._closing = False
        self.background_error = None
        # Back up before even additive schema/legacy migrations alter provenance.
        previous_tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing_quarantine = "knowledge_root_quarantine" not in previous_tables
        needs_root_backup = self._root_binding_needs_backup()
        audit_backup = self._backup_audit_migration()
        if needs_root_backup and audit_backup is None:
            self._backup_store("before-root-binding")
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
            CREATE TABLE IF NOT EXISTS knowledge_root_bindings (
                scope TEXT PRIMARY KEY, root TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_root_quarantine (
                scope TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                source_id TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(scope,path,digest));
            CREATE TABLE IF NOT EXISTS knowledge_pdf_versions (
                source_id TEXT PRIMARY KEY, pdf_bytes BLOB NOT NULL,
                metadata_json TEXT, markdown_path TEXT);
            CREATE TABLE IF NOT EXISTS knowledge_file_audit (
                scope TEXT NOT NULL, path TEXT NOT NULL, source_id TEXT,
                suffix TEXT NOT NULL, byte_count INTEGER, state TEXT NOT NULL,
                stage TEXT NOT NULL, error TEXT, details_json TEXT NOT NULL,
                observed_at REAL NOT NULL, remote_usage_unknown INTEGER,
                PRIMARY KEY(scope,path));
        """)
        if "scope" not in {r[1] for r in store.db.execute("PRAGMA table_info(knowledge_versions)")}:
            store.db.execute("ALTER TABLE knowledge_versions ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        self._migrate_legacy()
        self._backfill_root_quarantine(conservative_archives=missing_quarantine)
        self._bind_source_root()
        # An interrupted HTTP request has unknown usage; do not retry it silently.
        interrupted = store.db.execute("SELECT source_id,path,digest FROM knowledge_versions WHERE status='labelling' AND scope=?",
                                       (self.scope,)).fetchall()
        with store.db:
            store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE status='labelling' AND scope=?", (self.scope,))
            store.db.execute("UPDATE knowledge_file_audit SET state='failed',stage='label',error='interrupted_unknown_usage',remote_usage_unknown=1 WHERE scope=? AND source_id IN (SELECT source_id FROM knowledge_versions WHERE scope=? AND error='interrupted_unknown_usage')", (self.scope, self.scope))
        for version in interrupted:
            counts = store.db.execute("SELECT COUNT(*),COUNT(marks_json) FROM knowledge_chunks WHERE source_id=?",
                                      (version["source_id"],)).fetchone()
            self._receipt("cancelled", version, code="interrupted_unknown_usage", failure_stage="label",
                          total_chunks=counts[0], labelled_chunks=counts[1], remote_usage_unknown=True)
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

    @staticmethod
    def _root_identity(root):
        return os.path.normcase(str(root))

    def _check_root(self):
        """Recheck the configured root before every source-read entry point."""
        try:
            current = validate_knowledge_root(self.config)
        except PathPolicyError as error:
            raise GovernedError(error.code) from None
        if self._root_identity(current) != self._root_identity(self.root):
            raise GovernedError("knowledge_root_changed_while_running")

    def _bind_source_root(self):
        """Bind current sources to one root before a Gateway can serve them.

        Earlier databases recorded only a Guild and relative path. Their
        provenance cannot establish a directory identity. Retain an unbound
        source only when the current permitted file has its exact byte digest;
        archive every other source without deleting original snapshots or usage.
        A recorded root change never inherits sources, even for identical bytes.
        """
        self._check_root()
        identity = self._root_identity(self.root)
        row = self.store.db.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (self.scope,)).fetchone()
        if row is not None and row[0] == identity:
            return
        sources = {r[0] for r in self.store.db.execute("""SELECT source_id FROM knowledge_desired WHERE scope=?
            UNION SELECT source_id FROM knowledge_published WHERE scope=?
            UNION SELECT source_id FROM knowledge_migration_hold WHERE scope=?
            UNION SELECT source_id FROM memory_records WHERE scope=? AND active=1 AND source_id LIKE 'kb:%'""",
            (self.scope, self.scope, self.scope, self.scope))}
        verified, retained_holds = set(), set()
        versions = {source_id: self.store.db.execute("""SELECT v.source_id,v.path,v.digest,v.scope,
            v.status,v.error,a.remote_usage_unknown
            FROM knowledge_versions v LEFT JOIN knowledge_file_audit a
            ON a.scope=? AND a.path=v.path AND a.source_id=v.source_id WHERE v.source_id=?""",
            (self.scope, source_id)).fetchone() for source_id in sources}
        if row is None and sources:
            files = self._scan_files()
            # Overflow keeps no old evidence eligible, matching the scan policy.
            if len(files) <= self.config.knowledge.max_files:
                candidates = {path.relative_to(self.root).as_posix(): path for path in files}
                snapshots = {}
                for source_id in sources:
                    version = versions[source_id]
                    held = self.store.db.execute("SELECT digest FROM knowledge_migration_hold WHERE scope=? AND source_id=?",
                                                 (self.scope, source_id)).fetchone()
                    if version is None or version["path"] not in candidates:
                        continue
                    if version["path"] not in snapshots:
                        try:
                            snapshot = self._snapshot_file(candidates[version["path"]])
                        except (OSError, UnicodeError, GovernedError):
                            snapshot = None
                        # Retain only a digest, never a batch of original PDFs.
                        snapshots[version["path"]] = snapshot[1] if snapshot is not None else None
                    if snapshots[version["path"]] == version["digest"]:
                        if version["scope"] == self.scope:
                            verified.add(source_id)
                        elif (held is not None and held[0] == version["digest"]
                              and not version["scope"] and version["error"] == "legacy_scope_unknown"):
                            # This remains a no-replay hold, not accepted evidence.
                            retained_holds.add(source_id)
        retired = sources - verified - retained_holds
        reason = "knowledge_root_changed" if row is not None else "knowledge_root_unverified"
        archived_records = 0
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            for source_id in sources:
                version = versions[source_id]
                if version is not None and (version["remote_usage_unknown"] == 1
                        or version["status"] == "labelling"
                        or version["error"] in {"interrupted_unknown_usage", "legacy_scope_unknown", "legacy_incomplete_request"}):
                    # Archive eligibility independently of the lasting no-replay
                    # digest. A missing file or later root migration must not
                    # turn an unknown request into a fresh unmetered version.
                    self.store.db.execute("INSERT OR IGNORE INTO knowledge_root_quarantine VALUES(?,?,?,?,?)",
                                          (self.scope, version["path"], version["digest"], source_id,
                                           version["error"] or "interrupted_unknown_usage"))
            for source_id in retired:
                archived_records += self.graph.deactivate_source(self.scope, source_id, commit=False)
                self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error=? WHERE scope=? AND source_id=?",
                                      (reason, self.scope, source_id))
                for table in ("knowledge_desired", "knowledge_published", "knowledge_migration_hold"):
                    self.store.db.execute(f"DELETE FROM {table} WHERE scope=? AND source_id=?", (self.scope, source_id))
                self.store.db.execute("UPDATE knowledge_file_audit SET state='archived',stage='root_binding',error=? WHERE scope=? AND source_id=?",
                                      (reason, self.scope, source_id))
            self.store.db.execute("INSERT INTO knowledge_root_bindings VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET root=excluded.root",
                                  (self.scope, identity))
        if sources or row is not None:
            self.scratch.write("knowledge_root_bound", scope=self.scope, root=str(self.root),
                               previous_root_known=row is not None, retained_source_ids=sorted(verified),
                               retained_no_replay_hold_ids=sorted(retained_holds),
                               archived_source_ids=sorted(retired), archived_records=archived_records,
                               reason=reason, historical_origin_verified=False, automatically_replayed=False)
            if retired:
                _console_notice(f"[{self.config.name}] 知识根目录隔离：{len(retired)} 个无法沿用的来源已归档；原文、PDF、标词与累计用量保留。")

    def _root_binding_needs_backup(self):
        tables = {row[0] for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "knowledge_versions" not in tables:
            return False
        if not {"knowledge_root_bindings", "knowledge_root_quarantine", "knowledge_schema_meta"} <= tables:
            return True
        if not self.store.db.execute("SELECT 1 FROM knowledge_schema_meta WHERE key=?",
                                     (self._quarantine_migration_key(),)).fetchone():
            return True
        row = self.store.db.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (self.scope,)).fetchone()
        return row is None or row[0] != self._root_identity(self.root)

    def _quarantine_migration_key(self):
        return "root_quarantine_backfill_v2:" + self.scope

    def _quarantine_version(self, source_id, *, conservative_archives=False, hold=None):
        """Preserve no-replay evidence before a head/error can be overwritten.

        Audit flags belong to one exact version, never merely the same path.
        Early archives without a ledger lost their original error; their
        digest stays blocked conservatively instead of gaining fresh usage.
        Callers own the surrounding transaction.
        """
        version = self.store.db.execute("""SELECT v.source_id,v.path,v.digest,v.scope,v.status,v.error,
            a.source_id AS audit_source_id,a.remote_usage_unknown FROM knowledge_versions v LEFT JOIN knowledge_file_audit a
            ON a.scope=? AND a.path=v.path AND a.source_id=v.source_id WHERE v.source_id=?""",
            (self.scope, source_id)).fetchone()
        reason = None
        if version is not None:
            if version["scope"] != self.scope and hold is None:
                if version["scope"] or (version["audit_source_id"] is None and not self.store.db.execute("""
                    SELECT source_id FROM knowledge_desired WHERE scope=? AND source_id=?
                    UNION SELECT source_id FROM knowledge_published WHERE scope=? AND source_id=?
                    UNION SELECT source_id FROM memory_records WHERE scope=? AND source_id=?
                    UNION SELECT source_id FROM knowledge_migration_hold WHERE scope=? AND source_id=?""",
                    (self.scope, source_id) * 4).fetchone()):
                    return None  # Unknown ownership is not assigned to a Guild.
            if (version["remote_usage_unknown"] == 1 or version["status"] == "labelling"
                    or version["error"] in {"interrupted_unknown_usage", "legacy_scope_unknown", "legacy_incomplete_request"}):
                reason = version["error"] or "interrupted_unknown_usage"
            elif (conservative_archives and version["status"] == "superseded"
                    and version["error"] != "completed_replacement"):
                # Completed replacements came from a fully published source.
                # Other old archives may have erased an unknown request error.
                reason = "legacy_archived_usage_unverified"
            if reason is not None:
                self.store.db.execute("INSERT OR IGNORE INTO knowledge_root_quarantine VALUES(?,?,?,?,?)",
                                      (self.scope, version["path"], version["digest"], source_id, reason))
        if hold is not None:
            hold_reason = reason or (version["error"] if version is not None else None) or "legacy_scope_unknown"
            self.store.db.execute("INSERT OR IGNORE INTO knowledge_root_quarantine VALUES(?,?,?,?,?)",
                                  (self.scope, hold["path"], hold["digest"], source_id, hold_reason))
        return reason

    def _backfill_root_quarantine(self, *, conservative_archives):
        """Migrate once per Guild even when its recorded root already matches."""
        marker = self._quarantine_migration_key()
        migrated = self.store.db.execute("SELECT 1 FROM knowledge_schema_meta WHERE key=?", (marker,)).fetchone()
        if not conservative_archives and migrated:
            return
        conservative_archives = conservative_archives or not migrated
        holds = {row["source_id"]: row for row in self.store.db.execute(
            "SELECT source_id,path,digest FROM knowledge_migration_hold WHERE scope=?", (self.scope,))}
        sources = {row[0] for row in self.store.db.execute(
            "SELECT source_id FROM knowledge_versions WHERE scope=? OR scope=''", (self.scope,))} | set(holds)
        conservative = []
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            before = self.store.db.execute("SELECT COUNT(*) FROM knowledge_root_quarantine WHERE scope=?", (self.scope,)).fetchone()[0]
            for source_id in sorted(sources):
                reason = self._quarantine_version(source_id, conservative_archives=conservative_archives,
                                                   hold=holds.get(source_id))
                if reason == "legacy_archived_usage_unverified":
                    conservative.append(source_id)
            self.store.db.execute("INSERT INTO knowledge_schema_meta VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'", (marker,))
            after = self.store.db.execute("SELECT COUNT(*) FROM knowledge_root_quarantine WHERE scope=?", (self.scope,)).fetchone()[0]
        if after != before:
            self.scratch.write("knowledge_root_quarantine_migrated", scope=self.scope,
                               added_digests=after - before, conservative_source_ids=conservative,
                               historical_usage_verified=False, automatically_replayed=False)

    def _backup_audit_migration(self):
        """Back up an existing knowledge database before the additive migration."""
        tables = {row[0] for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "knowledge_versions" not in tables or "knowledge_file_audit" in tables:
            return
        return self._backup_store("before-file-audit")

    def _backup_store(self, purpose):
        """Preserve one consistent committed WAL snapshot before migration."""
        filename = next(row[2] for row in self.store.db.execute("PRAGMA database_list") if row[1] == "main")
        if not filename:  # In-memory test stores have no existing disk data.
            return
        if self.store.db.in_transaction:
            raise GovernedError("knowledge_migration_requires_committed_store")
        directory = Path(filename).parent / "migration_backups"
        validate_managed_path(directory, "knowledge migration backups")
        directory.mkdir(parents=True, exist_ok=True)
        validate_managed_path(directory, "knowledge migration backups")
        destination = directory / ("memory." + purpose + "-" + uuid.uuid4().hex + ".sqlite3")
        # The source's backup API includes a consistent WAL snapshot. An
        # exclusive placeholder prevents overwriting any existing backup.
        with destination.open("xb"):
            pass
        backup = sqlite3.connect(destination)
        try:
            self.store.db.backup(backup)
        finally:
            backup.close()
        return destination

    def _audit_file(self, path, *, source_id=None, state, stage, error=None, details=None,
                    remote_usage_unknown=None, byte_count=None):
        relative = path if isinstance(path, str) else path.relative_to(self.root).as_posix()
        if byte_count is None:
            try:
                byte_count = (self.root / relative).stat().st_size
            except OSError:
                pass
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if {"reingest_documents", "reingest_events"} <= tables:
            protected = self.store.db.execute("""SELECT m.batch_id,m.attempt_id,a.source_id,a.remote_usage_unknown
                FROM knowledge_file_audit a JOIN reingest_documents m
                ON m.scope=a.scope AND m.path=a.path AND m.old_source_id=a.source_id
                WHERE a.scope=? AND a.path=? ORDER BY m.rowid DESC LIMIT 1""",
                (self.scope, relative)).fetchone()
            if protected is not None:
                # The path-keyed failure is part of the preserved old ledger.
                # Later ordinary-file observations remain append-only and are
                # projected by readers, so new failures are still visible.
                previous = self.store.db.execute("""SELECT details_json FROM reingest_events
                    WHERE attempt_id=? AND event='knowledge_file_audit_successor'
                    ORDER BY seq DESC LIMIT 1""", (protected["attempt_id"],)).fetchone()
                last = json.loads(previous[0]) if previous is not None else None
                if remote_usage_unknown is None:
                    if last is not None and last["source_id"] == source_id:
                        remote_usage_unknown = last["remote_usage_unknown"]
                    elif protected["source_id"] == source_id:
                        remote_usage_unknown = protected["remote_usage_unknown"]
                observation = {"scope": self.scope, "path": relative, "source_id": source_id,
                    "suffix": Path(relative).suffix.lower(), "byte_count": byte_count,
                    "state": state, "stage": stage, "error": error,
                    "details": details or {}, "remote_usage_unknown": remote_usage_unknown}
                if last != observation:
                    with self.store.db:
                        if remote_usage_unknown and source_id is not None:
                            # Ordinary retry reads the original path audit.
                            # Preserve a lasting no-replay digest for later
                            # unknown calls that live in successor events.
                            self.store.db.execute("""INSERT OR IGNORE INTO knowledge_root_quarantine
                                (scope,path,digest,source_id,reason)
                                SELECT scope,path,digest,source_id,? FROM knowledge_versions
                                WHERE scope=? AND path=? AND source_id=?""",
                                (error or "interrupted_unknown_usage", self.scope, relative, source_id))
                        self.store.db.execute("""INSERT INTO reingest_events
                            (batch_id,attempt_id,call_key,event,recorded_at,details_json)
                            VALUES(?,?,NULL,'knowledge_file_audit_successor',?,?)""",
                            (protected["batch_id"], protected["attempt_id"], time.time(),
                             json.dumps(observation, sort_keys=True)))
                return
        with self.store.db:
            self.store.db.execute("""INSERT INTO knowledge_file_audit
                (scope,path,source_id,suffix,byte_count,state,stage,error,details_json,observed_at,remote_usage_unknown)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,path) DO UPDATE SET
                source_id=excluded.source_id,suffix=excluded.suffix,byte_count=excluded.byte_count,
                state=excluded.state,stage=excluded.stage,error=excluded.error,
                details_json=excluded.details_json,observed_at=excluded.observed_at,
                remote_usage_unknown=CASE WHEN knowledge_file_audit.source_id=excluded.source_id
                    THEN COALESCE(excluded.remote_usage_unknown,knowledge_file_audit.remote_usage_unknown)
                    ELSE excluded.remote_usage_unknown END""",
                (self.scope, relative, source_id, Path(relative).suffix.lower(), byte_count, state, stage,
                 error, json.dumps(details or {}, sort_keys=True), time.time(), remote_usage_unknown))

    def _audit_version(self, version, *, stage, **fields):
        row = self.store.db.execute("""SELECT v.status,v.error FROM knowledge_versions v
            JOIN knowledge_desired d ON d.source_id=v.source_id AND d.scope=v.scope
            WHERE v.source_id=? AND v.scope=? AND d.path=?""",
            (version["source_id"], self.scope, version["path"])).fetchone()
        if row is not None:
            self._audit_file(version["path"], source_id=version["source_id"], state=row[0], stage=stage,
                             error=row[1], **fields)

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
            _console_notice(f"[{self.config.name}] 知识迁移需更新文件内容：{path} | version={source_id} | {reason}；旧请求不会自动重发。")

    def _snapshot_file(self, path):
        self._check_root()
        if (path.is_symlink() or path.is_junction()
                or not path.resolve().is_relative_to(self.root.resolve())):
            raise GovernedError("knowledge_path_outside_root")
        for parent in path.parents:
            if parent == self.root:
                break
            if parent.is_symlink() or parent.is_junction():
                raise GovernedError("knowledge_path_outside_root")
        try:
            validate_managed_path(path, "knowledge material")
        except PathPolicyError as error:
            raise GovernedError(error.code) from None
        limit, config_key = self.config.knowledge.file_limit(path.suffix, self.pdf_limits)
        before = path.stat()
        if before.st_size > limit:
            error = GovernedError("knowledge_file_size_limit")
            error.file_details = {"actual_bytes": before.st_size, "limit_bytes": limit, "config_key": config_key}
            raise error
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            return None
        if len(raw) > limit:
            error = GovernedError("knowledge_file_size_limit")
            error.file_details = {"actual_bytes": len(raw), "limit_bytes": limit, "config_key": config_key}
            raise error
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
        stage = fields.get("failure_stage") or ("published" if event == "completed" else "label")
        self._audit_version(version, stage=stage, remote_usage_unknown=fields.get("remote_usage_unknown"),
                            details={key: fields[key] for key in
                                     ("failed_chunk_index", "labelled_chunks", "total_chunks") if key in fields})
        self._failure_notice(event, version, stage, fields)
        current = self._published(version["path"])
        self.scratch.write("knowledge_receipt", receipt=event, scope=self.scope,
                           path=version["path"], source_id=version["source_id"], digest=version["digest"],
                           reply_source_id=current, **fields)

    def _conversion_receipt(self, event, version, **fields):
        self._audit_version(version, stage="chunk" if event == "converted" else "conversion",
                            remote_usage_unknown=False)
        self._failure_notice(event, version, "conversion", fields)
        current = self._published(version["path"])
        self.scratch.write("knowledge_pdf_receipt", receipt=event, scope=self.scope,
                           path=version["path"], source_id=version["source_id"], digest=version["digest"],
                           reply_source_id=current, **fields)

    def _failure_notice(self, event, version, stage, fields):
        if event not in {"failed", "cancelled"}:
            return
        from .knowledge_progress import _terminal_text
        unknown = fields.get("remote_usage_unknown", False)
        progress = f"{fields.get('labelled_chunks', 0)}/{fields.get('total_chunks', 0)}"
        recovery = "远端用量未知，禁止直接 retry；须核验用量" if unknown else "请 audit 检查；retry 仍受当前版本及剩余额度限制"
        _console_notice(f"[{_terminal_text(self.config.name)}] 摄入未完成：{_terminal_text(version['path'])} | "
                        f"stage={_terminal_text(stage)} | code={_terminal_text(fields.get('code') or 'interrupted')} | "
                        f"标词块={progress} | {recovery}；本次未发布新版本。")

    def _supersede_desired(self, path):
        row = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=?", (self.scope, path)).fetchone()
        if row and row[0] != self._published(path):
            self._quarantine_version(row[0])
            self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error='newer_file_snapshot' WHERE source_id=? AND scope=?", (row[0], self.scope))

    def _retire(self, path, reason):
        rows = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=? AND path=? UNION SELECT source_id FROM knowledge_published WHERE scope=? AND path=?", (self.scope, path, self.scope, path)).fetchall()
        held = self.store.db.execute("SELECT 1 FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, path)).fetchone()
        if not rows and held is None:
            if reason == "source_file_removed" and self.store.db.execute("SELECT 1 FROM knowledge_file_audit WHERE scope=? AND path=?", (self.scope, path)).fetchone():
                self._audit_file(path, state="archived", stage="discovery", error=reason)
            return
        archived = 0
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            for row in rows:
                self._quarantine_version(row[0])
                archived += self.graph.deactivate_source(self.scope, row[0], commit=False)
                self.store.db.execute("UPDATE knowledge_versions SET status='superseded',error=? WHERE source_id=? AND scope=?", (reason, row[0], self.scope))
            for hold in self.store.db.execute("SELECT source_id,path,digest FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, path)).fetchall():
                self._quarantine_version(hold["source_id"], hold=hold)
            for table in ("knowledge_desired", "knowledge_published", "knowledge_migration_hold"):
                self.store.db.execute(f"DELETE FROM {table} WHERE scope=? AND path=?", (self.scope, path))
        self.scratch.write("knowledge_retired", scope=self.scope, path=path, source_ids=[r[0] for r in rows], archived_records=archived, reason=reason)
        self._audit_file(path, state="archived", stage="discovery", error=reason)
        _console_notice(f"[{self.config.name}] 知识来源已撤下：{path} | reason={reason} | archived={archived}；不再用于回复。")

    def _scan_files(self):
        self._check_root()
        files = []
        def scan_failed(error):
            # An incomplete census must not look like source deletion. Keep
            # the previous published snapshots until a complete scan succeeds.
            raise GovernedError("knowledge_directory_scan_failed") from None

        def permitted(path):
            try:
                if path.is_symlink() or path.is_junction():
                    return False
                validate_managed_path(path, "knowledge material")
                return True
            except PathPolicyError as error:
                if error.code in {"onedrive_path_forbidden", "linked_path_forbidden"}:
                    return False
                raise GovernedError("knowledge_directory_scan_failed") from None
            except OSError:
                raise GovernedError("knowledge_directory_scan_failed") from None

        for directory, subdirectories, names in os.walk(self.root, topdown=True, followlinks=False,
                                                       onerror=scan_failed):
            parent = Path(directory)
            # Prune storage/management roots before descent, so a large staging
            # collection is not enumerated on every passive scan. Nested names
            # remain ordinary source folders. Never traverse directory aliases.
            subdirectories[:] = [name for name in subdirectories
                                 if not (parent == self.root and name.casefold() in RESERVED_ROOT_DIRECTORIES)
                                 and permitted(parent / name)]
            for name in names:
                path = parent / name
                if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                if not permitted(path):
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
    def _certified_reingest_publication(self, path, digest, source_id):
        """Recognise only an exact, fully published maintenance source.

        This does not release an old digest quarantine or admit a new request.
        The maintenance publisher owns the additive ledger and transaction.
        """
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"reingest_documents", "reingest_batches"} <= tables:
            return False
        return self.store.db.execute("""SELECT 1 FROM reingest_documents m
            JOIN reingest_batches b ON b.batch_id=m.batch_id AND b.scope=m.scope
            JOIN knowledge_versions v ON v.source_id=m.new_source_id
                AND v.scope=m.scope AND v.path=m.path AND v.digest=m.digest
            JOIN knowledge_desired d ON d.source_id=v.source_id
                AND d.scope=v.scope AND d.path=v.path
            JOIN knowledge_published p ON p.source_id=v.source_id
                AND p.scope=v.scope AND p.path=v.path
            WHERE m.scope=? AND m.path=? AND m.digest=? AND m.new_source_id=?
                AND m.published=1 AND b.pilot_passed=1 AND v.status='ready'""",
            (self.scope, path, digest, source_id)).fetchone() is not None

    def _apply_snapshot(self, path, snapshot, error=None):
        relative = path.relative_to(self.root).as_posix()
        if error is not None:
            code = error.code if isinstance(error, GovernedError) else type(error).__name__
            self._retire(relative, code)
            details = getattr(error, "file_details", {})
            self._audit_file(path, state="rejected", stage="decode" if isinstance(error, UnicodeError) else "validation",
                             error=code, details=details)
            if self._errors.get(relative) != code:
                self.scratch.write("knowledge_update_rejected", path=relative, code=code, **details)
                limits = (f"; actual_bytes={details['actual_bytes']}; limit_bytes={details['limit_bytes']}; {details['config_key']}"
                          if details else "")
                _console_notice(f"[{self.config.name}] knowledge rejected: {relative}; {code}{limits}")
                self._errors[relative] = code
            return
        if snapshot is None:
            return
        content, digest = snapshot
        head = self.store.db.execute("SELECT v.digest,v.source_id,v.status,v.error FROM knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND h.path=?", (self.scope, relative)).fetchone()
        if head and head[0] == digest:
            # A separately authorised maintenance publication retains the old
            # failure audit at this path. Its new receipt lives in the additive
            # reingest ledger; ordinary watcher backfill must not replace it.
            if self._certified_reingest_publication(relative, digest, head[1]):
                return
            # Backfill legacy metadata without overwriting a recorded failure
            # stage or repeatedly writing unchanged observations on every poll.
            if not self.store.db.execute("SELECT 1 FROM knowledge_file_audit WHERE scope=? AND path=? AND source_id=?", (self.scope, relative, head[1])).fetchone():
                self._audit_file(path, source_id=head[1], state=head[2],
                                 stage="published" if head[2] == "ready" else "conversion" if head[2] == "converting" else "label",
                                 error=head[3], remote_usage_unknown=True if head[3] == "interrupted_unknown_usage" else None)
            return
        hold = self.store.db.execute("SELECT digest FROM knowledge_migration_hold WHERE scope=? AND path=?", (self.scope, relative)).fetchone()
        if hold and hold[0] == digest:
            return
        quarantined = self.store.db.execute("SELECT source_id,reason FROM knowledge_root_quarantine WHERE scope=? AND path=? AND digest=?",
                                           (self.scope, relative, digest)).fetchone()
        if quarantined is not None:
            # Neither disappearance nor reappearance can reset unknown usage.
            quarantine_error = ("knowledge_root_quarantined_usage_unverified"
                                if quarantined[1] == "legacy_archived_usage_unverified"
                                else "knowledge_root_quarantined_unknown_usage")
            if not self.store.db.execute("""SELECT 1 FROM knowledge_file_audit WHERE scope=? AND path=?
                    AND source_id=? AND state='archived' AND error=?""",
                    (self.scope, relative, quarantined[0], quarantine_error)).fetchone():
                self._audit_file(path, source_id=quarantined[0], state="archived", stage="root_binding",
                                 error=quarantine_error, remote_usage_unknown=True,
                                 details={"quarantine_reason": quarantined[1]})
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
        heads = self.store.db.execute("SELECT path FROM knowledge_desired WHERE scope=? UNION SELECT path FROM knowledge_published WHERE scope=? UNION SELECT path FROM knowledge_migration_hold WHERE scope=? UNION SELECT path FROM knowledge_file_audit WHERE scope=? AND state!='archived'", (self.scope, self.scope, self.scope, self.scope)).fetchall()
        for head in heads:
            if head[0] not in seen:
                self._retire(head[0], "source_file_removed")

    def scan_once(self, *, prepared=None):
        """Queue stable source snapshots; direct callers run synchronously.

        The watcher supplies one prepared file at a time so filesystem reads
        happen in a thread while all SQLite mutations remain on its owner loop.
        No prepared batch retains more than one original PDF in memory.
        """
        self._check_root()
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

    def retry_failed(self, path=None, *, include_incomplete=False):
        """Explicitly resume only failed current versions, within spent budgets.

        Call on the service's owner event loop. Completed chunks, confirmed
        usage, elapsed time and source identity are preserved. Unknown remote
        usage cannot be retried through this bounded operation.
        """
        self._check_root()
        if path is not None and (not isinstance(path, str) or not path or Path(path).is_absolute()
                                 or ".." in Path(path).parts):
            raise ValueError("retry path must be a relative knowledge path")
        if path is not None:
            path = Path(path).as_posix()
        rows = self.store.db.execute("""SELECT v.*,a.remote_usage_unknown FROM knowledge_versions v
            JOIN knowledge_desired d ON d.source_id=v.source_id AND d.scope=v.scope AND d.path=v.path
            LEFT JOIN knowledge_file_audit a ON a.scope=v.scope AND a.path=v.path AND a.source_id=v.source_id
            WHERE v.scope=? AND (? IS NULL OR v.path=?) ORDER BY v.path""", (self.scope, path, path)).fetchall()
        result = {"queued": [], "blocked": [], "unchanged": []}
        if path is not None and not rows:
            result["blocked"].append({"path": path, "source_id": None, "code": "knowledge_retry_not_current"})
        safe_legacy = {"invalid_labels", "invalid_output", "incomplete_response", "provider_token_limit_breach",
                       "knowledge_version_token_limit", "knowledge_version_time_limit", "interrupted",
                       "input_token_limit", "circuit_open", "missing_openai_key", "invalid_input_token_count", "model_slot_timeout"}
        for row in rows:
            version = dict(row)
            item = {"path": version["path"], "source_id": version["source_id"]}
            incomplete = include_incomplete and version["status"] in {"pending", "converting"}
            if version["status"] != "failed" and not incomplete:
                result["unchanged"].append({**item, "status": version["status"]})
                continue
            is_pdf = self.store.db.execute("SELECT metadata_json FROM knowledge_pdf_versions WHERE source_id=?", (version["source_id"],)).fetchone()
            conversion = is_pdf is not None and is_pdf[0] is None
            unknown = version["remote_usage_unknown"]
            quarantined = self.store.db.execute("SELECT reason FROM knowledge_root_quarantine WHERE scope=? AND path=? AND digest=?",
                                                 (self.scope, version["path"], version["digest"])).fetchone()
            code = None
            if not self._is_current(version["source_id"]):
                code = "knowledge_retry_snapshot_changed"
            elif quarantined is not None:
                code = ("knowledge_retry_usage_unverified" if quarantined[0] == "legacy_archived_usage_unverified"
                        else "knowledge_retry_unknown_usage")
            elif version["error"] in {"interrupted_unknown_usage", "legacy_scope_unknown", "legacy_incomplete_request"}:
                code = "knowledge_retry_unknown_usage"
            elif not conversion and (unknown == 1 or (not incomplete and unknown is None and version["error"] not in safe_legacy)):
                code = "knowledge_retry_unknown_usage"
            elif not conversion:
                unlabelled = self.store.db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=? AND marks_json IS NULL", (version["source_id"],)).fetchone()[0]
                budget = self.config.adapter.budgets["label"]
                if version["elapsed_seconds"] >= self.config.knowledge.version_seconds:
                    code = "knowledge_version_time_limit"
                elif unlabelled and (version["input_tokens"] >= self.config.knowledge.version_input_tokens
                                     or version["output_tokens"] + budget.output_tokens > self.config.knowledge.version_output_tokens):
                    code = "knowledge_version_token_limit"
            if code is not None:
                result["blocked"].append({**item, "code": code})
                continue
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status=?,error=NULL WHERE source_id=? AND scope=? AND status IN ('failed','pending','converting')",
                                      ("converting" if conversion else "pending", version["source_id"], self.scope))
            self._audit_version(version, stage="conversion" if conversion else "label", remote_usage_unknown=False)
            (self._convert_wake if conversion else self._wake).set()
            result["queued"].append({**item, "stage": "conversion" if conversion else "label"})
        self.scratch.write("knowledge_retry_requested", scope=self.scope, path=path, **result,
                           budgets_reset=False, automatically_replayed=False)
        return result

    @staticmethod
    def _selection(source_ids):
        if source_ids is None:
            return "", ()
        if isinstance(source_ids, (str, bytes)):
            raise ValueError("source_ids must be a collection of source IDs")
        values = set(source_ids)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("source_ids must contain nonempty strings")
        values = tuple(sorted(values))
        return " AND v.source_id IN (" + ",".join("?" for _ in values) + ")", values

    async def convert_next(self, *, source_ids=None) -> bool:
        """Convert one frozen PDF without taking the model adaptor's slot."""
        async with self._convert_serial:
            selection, selected = self._selection(source_ids)
            if source_ids is not None and not selected:
                return False
            row = self.store.db.execute("""SELECT v.*,length(p.pdf_bytes) AS pdf_bytes_length,
                substr(p.pdf_bytes,1,?) AS pdf_bytes FROM knowledge_versions v
                JOIN knowledge_pdf_versions p ON p.source_id=v.source_id
                JOIN knowledge_desired h ON h.source_id=v.source_id
                WHERE h.scope=? AND v.scope=? AND v.status='converting'""" + selection + " ORDER BY v.created_at LIMIT 1",
                (self.pdf_limits.max_file_bytes + 1, self.scope, self.scope, *selected)).fetchone()
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

    async def label_next(self, *, source_ids=None) -> bool:
        selection, selected = self._selection(source_ids)
        if source_ids is not None and not selected:
            return False
        row = self.store.db.execute("SELECT v.* FROM knowledge_versions v JOIN knowledge_desired h ON h.source_id=v.source_id WHERE h.scope=? AND v.scope=? AND v.status='pending'" + selection + " ORDER BY v.created_at LIMIT 1", (self.scope, self.scope, *selected)).fetchone()
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
        request_usage_unknown = None
        stage = "label"
        failed_chunk_index = None
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
                    failed_chunk_index = chunk["chunk_index"]
                    remaining_input = limits.version_input_tokens - version["input_tokens"]
                    if remaining_input <= 0 or version["output_tokens"] + budget.output_tokens > limits.version_output_tokens:
                        raise GovernedError("knowledge_version_token_limit")
                    stage = "label"
                    try:
                        # The adaptor already counts this exact request before
                        # generation. Constrain that admission to the remaining
                        # version allowance rather than reserving an unused full
                        # stage input cap. Output reservation is unchanged.
                        admission = {"input_limit": remaining_input} if remaining_input < budget.input_tokens else {}
                        result = await self.adapter.call("label", LABEL, [{"role": "user", "content": encode({
                            "source_id": source_id, "path": version["path"], "digest": version["digest"],
                            "chunk_index": chunk["chunk_index"], "start_character": chunk["start_character"], "text": chunk["text"]})}], trace_id, LABEL_SCHEMA, **admission)
                    except BaseException as error:
                        request_failed = True
                        request_usage_unknown = getattr(error, "remote_usage_unknown", None)
                        known_usage = validated_token_usage(getattr(error, "known_usage", None))
                        if known_usage is not None:
                            # Rejected output still consumed its confirmed tokens.
                            # This failure path is exclusive of result accounting.
                            version["input_tokens"] += known_usage["input_tokens"]
                            version["output_tokens"] += known_usage["output_tokens"]
                            with self.store.db:
                                self.store.db.execute("UPDATE knowledge_versions SET input_tokens=?,output_tokens=? WHERE source_id=?",
                                                      (version["input_tokens"], version["output_tokens"], source_id))
                        raise
                    version["input_tokens"] += result.input_tokens
                    version["output_tokens"] += result.output_tokens
                    stage = "label_persistence"
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_versions SET input_tokens=?,output_tokens=? WHERE source_id=?", (version["input_tokens"], version["output_tokens"], source_id))
                    stage = "label_validation"
                    marks = label_data(result.text)
                    stage = "label_persistence"
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
                stage = "publication"
                failed_chunk_index = None
                facts = [{"text": c["text"], "quote": c["text"], "marks": json.loads(c["marks_json"])} for c in chunks]
                records, previous = self._publish(version, facts, deadline)
                outcome = "completed"
        except asyncio.CancelledError:
            usage_unknown = request_usage_unknown if type(request_usage_unknown) is bool else request_failed
            error_code = "interrupted_unknown_usage" if usage_unknown else "interrupted"
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='failed',error=? WHERE source_id=? AND status='labelling'",
                                      (error_code, source_id))
            self.scratch.write("label_job_end", trace_id=trace_id, source_id=source_id, status="cancelled", remote_usage_unknown=usage_unknown)
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
                          failed_chunk_index=failed_chunk_index,
                          failure_stage=stage if outcome != "completed" else "published",
                          remote_usage_unknown=(request_usage_unknown if type(request_usage_unknown) is bool else
                              request_failed and error_code not in {"input_token_limit", "circuit_open", "missing_openai_key",
                                                                  "invalid_input_token_count", "model_slot_timeout"}))
        return True

    async def _filesystem_call(self, function, *args):
        # Cancelling to_thread's asyncio waiter cannot stop its running thread.
        # Keep that job alive and owned until close has joined the actual read.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._filesystem_tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._filesystem_tasks.discard(task)
                if not task.cancelled():
                    task.exception()

    async def _watch(self):
        while True:
            try:
                files = await self._filesystem_call(self._scan_files)
                self.scan_once(prepared=("begin", files))
                seen = set()
                for path in files:
                    seen.add(path.relative_to(self.root).as_posix())
                    try:
                        snapshot = await self._filesystem_call(self._snapshot_file, path)
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
                    _console_notice(f"[{self.config.name}] knowledge scan failed: {code}")
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
        _console_notice(f"[{self.config.name}] 知识库后台已暂停：{code}；更新与标词无法继续，已发布知识版本保留。请检查本地文件/日志后停机重启。")
        try:
            self.scratch.write("knowledge_background_stopped", scope=self.scope, task=task.get_name(),
                               code=code, automatically_replayed=False)
        except Exception:
            _console_notice(f"[{self.config.name}] 后台故障回执无法写入 scratch，请检查日志目录权限。")
        for pending in self._tasks:
            if pending is not task and not pending.done():
                pending.cancel()

    def start(self):
        if self._tasks or (self._close_task is not None and not self._close_task.done()):
            raise RuntimeError("knowledge service already started")
        self._closing = False
        self._close_task = None
        self.background_error = None
        self._tasks = [asyncio.create_task(self._watch(), name="indeces-knowledge-watch"),
                       asyncio.create_task(self._label_worker(), name="indeces-passive-label"),
                       asyncio.create_task(self._convert_worker(), name="indeces-pdf-convert")]
        for task in self._tasks:
            task.add_done_callback(self._background_done)

    async def close(self):
        async def finish_close():
            await asyncio.gather(*self._tasks, return_exceptions=True)
            filesystem_tasks = tuple(self._filesystem_tasks)
            if filesystem_tasks:
                await asyncio.gather(*filesystem_tasks, return_exceptions=True)
                self._filesystem_tasks.difference_update(filesystem_tasks)
            self._tasks = []

        if self._close_task is None:
            self._closing = True
            for task in self._tasks:
                task.cancel()
            self._close_task = asyncio.create_task(finish_close())
        cleanup = self._close_task
        cancellation = None
        # Repeated caller cancellation must not cancel a tracking future while
        # its thread still owns a file handle. Cleanup is cooperative I/O;
        # preserve cancellation, but deliver it only after those jobs finish.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        cleanup.result()
        if cancellation is not None:
            raise cancellation
