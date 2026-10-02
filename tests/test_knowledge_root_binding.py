"""Root provenance migration uses only synthetic local files and model stubs."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, PdfConfig
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.store import Store
from indeces.path_policy import PathPolicyError
from tests.test_knowledge import LabelAdapter, Scratch


class KnowledgeRootBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(name="Indeces", state_dir=self.root / "state",
            knowledge_dir=self.root / "knowledge", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(max_files=8), pdf=PdfConfig(),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1",
                                  {"label": Budget(4096, 512, 15)}))
        self.store = Store(self.config.state_dir)
        self.graph = MemoryGraph(self.store.db)
        self.adapter, self.scratch = LabelAdapter(), Scratch()
        self.services = []
        self.service = self.make_service()

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        self.store.close()
        self.temp.cleanup()

    def make_service(self, config=None):
        service = KnowledgeService(config or self.config, self.store, self.graph, self.adapter, self.scratch)
        self.services.append(service)
        return service

    async def ready(self, raw=b"alpha synthetic evidence", name="source.md"):
        path = self.config.knowledge_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.service.scan_once()
        await self.service.label_next()
        source_id = self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=? AND path=?",
                                         (self.service.scope, name)).fetchone()[0]
        return path, source_id

    def unbind(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM knowledge_root_bindings WHERE scope=?", (self.service.scope,))

    async def test_nested_onedrive_is_pruned_before_descent_or_source_read(self):
        forbidden = self.config.knowledge_dir / "OneDrive" / "private.md"
        forbidden.parent.mkdir()
        forbidden.write_bytes(b"synthetic forbidden material")
        scandir = os.scandir
        def inspected(path):
            self.assertNotEqual(Path(path), forbidden.parent)
            return scandir(path)
        with patch("indeces.knowledge.os.scandir", side_effect=inspected), \
                patch.object(Path, "open", side_effect=AssertionError("forbidden source must not open")):
            self.assertNotIn(forbidden, self.service._scan_files())
            with self.assertRaises(GovernedError) as error:
                self.service._snapshot_file(forbidden)
        self.assertEqual(error.exception.code, "onedrive_path_forbidden")

    async def test_directory_inspection_failure_preserves_complete_publication(self):
        _, source = await self.ready()
        inaccessible = self.config.knowledge_dir / "topic"
        inaccessible.mkdir()
        lstat = Path.lstat
        def failed(path, *args, **kwargs):
            if path == inaccessible:
                raise PermissionError("synthetic inaccessible directory")
            return lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", new=failed), self.assertRaises(GovernedError) as error:
            self.service.scan_once()
        self.assertEqual(error.exception.code, "knowledge_directory_scan_failed")
        self.assertGreater(self.active(source), 0)
        self.assertEqual(self.version(source)["status"], "ready")

    async def test_migration_backup_reparse_is_refused_without_archiving_or_export(self):
        _, source = await self.ready()
        self.unbind()
        directory = self.config.state_dir / "migration_backups"
        directory.mkdir(exist_ok=True)
        lstat = Path.lstat
        def reparse(path, *args, **kwargs):
            metadata = lstat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400) if path == directory else metadata
        with patch.object(Path, "lstat", new=reparse), self.assertRaises(PathPolicyError):
            self.make_service()
        self.assertGreater(self.active(source), 0)
        self.assertEqual(list(directory.iterdir()), [])

    def version(self, source_id):
        return dict(self.store.db.execute("SELECT * FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone())

    def active(self, source_id):
        return self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE source_id=? AND active=1", (source_id,)).fetchone()[0]

    def pointers(self, source_id):
        return sum(self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE scope=? AND source_id=?",
                                        (self.service.scope, source_id)).fetchone()[0]
                   for table in ("knowledge_desired", "knowledge_published", "knowledge_migration_hold"))

    async def test_unbound_matching_source_keeps_publication_labels_and_accounting(self):
        path, source_id = await self.ready()
        before = self.version(source_id)
        chunks = [tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_chunks")]
        self.unbind()
        calls = len(self.adapter.calls)
        restarted = self.make_service()
        self.assertEqual(self.version(source_id), before)
        self.assertEqual(self.pointers(source_id), 2)
        self.assertEqual(self.active(source_id), 1)
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_chunks")], chunks)
        self.assertFalse(await restarted.label_next())
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(path.read_bytes(), b"alpha synthetic evidence")
        bound = self.store.db.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (restarted.scope,)).fetchone()[0]
        self.assertEqual(bound, os.path.normcase(str(self.config.knowledge_dir)))

    async def test_old_database_without_root_tables_is_backed_up_and_bound(self):
        _, source_id = await self.ready()
        with self.store.db:
            self.store.db.execute("DROP TABLE knowledge_root_bindings")
            self.store.db.execute("DROP TABLE knowledge_root_quarantine")
        restarted = self.make_service()
        self.assertEqual((self.active(source_id), self.pointers(source_id)), (1, 2))
        self.assertFalse(await restarted.label_next())
        backups = list((self.config.state_dir / "migration_backups").glob("memory.before-root-binding-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as copy:
            self.assertIsNone(copy.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_root_bindings'").fetchone())
            self.assertEqual(copy.execute("SELECT status FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()[0], "ready")

    async def test_unbound_digest_mismatch_archives_without_removing_snapshots_or_usage(self):
        path, source_id = await self.ready()
        before = self.version(source_id)
        chunks = [tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_chunks")]
        path.write_bytes(b"different current evidence")
        self.unbind()
        restarted = self.make_service()
        after = self.version(source_id)
        self.assertEqual((after["status"], after["error"]), ("superseded", "knowledge_root_unverified"))
        for field in ("raw_text", "digest", "input_tokens", "output_tokens", "elapsed_seconds"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.active(source_id), 0)
        self.assertEqual(self.pointers(source_id), 0)
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_chunks")], chunks)
        self.assertEqual(self.graph.retrieve(restarted.scope, [], "alpha", 1, event_id="after-unbind"), [])
        self.assertEqual(path.read_bytes(), b"different current evidence")

    async def test_unbound_missing_source_archives_before_any_scan_or_model_call(self):
        path, source_id = await self.ready()
        path.unlink()
        self.unbind()
        calls = len(self.adapter.calls)
        self.make_service()
        self.assertEqual((self.active(source_id), self.pointers(source_id)), (0, 0))
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(self.version(source_id)["raw_text"], "alpha synthetic evidence")

    async def test_unbound_active_orphan_is_archived_without_deleting_the_record(self):
        self.graph.add(self.service.scope, "kb:unknown-origin", "knowledge:unknown.md",
                       [{"text": "alpha orphan", "quote": "alpha orphan", "marks": ["alpha"]}], 1)
        self.unbind()
        self.make_service()
        row = self.store.db.execute("SELECT text,quote,active FROM memory_records WHERE source_id='kb:unknown-origin'").fetchone()
        self.assertEqual(tuple(row), ("alpha orphan", "alpha orphan", 0))

    async def test_root_change_never_inherits_identical_relative_path_or_bytes(self):
        path, source_id = await self.ready()
        next_root = self.root / "another-project" / "knowledge"
        next_root.mkdir(parents=True)
        target = next_root / path.name
        target.write_bytes(path.read_bytes())
        next_config = SimpleNamespace(**{**vars(self.config), "knowledge_dir": next_root})
        restarted = self.make_service(next_config)
        self.assertEqual((self.active(source_id), self.pointers(source_id)), (0, 0))
        self.assertEqual(self.version(source_id)["error"], "knowledge_root_changed")
        backups = list((self.config.state_dir / "migration_backups").glob("memory.before-root-binding-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as copy:
            self.assertEqual(copy.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (self.service.scope,)).fetchone()[0],
                             os.path.normcase(str(self.config.knowledge_dir)))
            self.assertEqual(copy.execute("SELECT status FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()[0], "ready")
        self.assertFalse(await restarted.label_next())
        restarted.scan_once()
        new_source = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=?", (restarted.scope,)).fetchone()[0]
        self.assertNotEqual(new_source, source_id)
        self.assertEqual(target.read_bytes(), path.read_bytes())

    async def test_root_binding_isolated_by_guild(self):
        path, source_id = await self.ready()
        other_config = SimpleNamespace(**{**vars(self.config), "discord": DiscordConfig("20")})
        other = self.make_service(other_config)
        other.scan_once()
        await other.label_next()
        other_source = self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=?", (other.scope,)).fetchone()[0]
        self.unbind()
        path.unlink()
        self.make_service()
        self.assertEqual(self.active(source_id), 0)
        self.assertEqual(self.active(other_source), 1)
        self.assertEqual(self.version(other_source)["status"], "ready")

    async def test_unbound_current_snapshot_verified_independently_of_previous_publication(self):
        path, old = await self.ready()
        path.write_bytes(b"alpha legitimate pending replacement")
        self.service.scan_once()
        new = self.store.db.execute("SELECT source_id FROM knowledge_desired WHERE scope=?", (self.service.scope,)).fetchone()[0]
        self.unbind()
        self.make_service()
        self.assertEqual((self.active(old), self.pointers(old)), (0, 0))
        self.assertEqual(self.pointers(new), 1)
        self.assertEqual(self.version(new)["status"], "pending")

    async def test_unbound_pdf_bytes_and_derived_text_survive_archival(self):
        path, source_id = await self.ready()
        raw_pdf = b"%PDF-1.4 synthetic frozen private bytes"
        with self.store.db:
            self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                                  (source_id, raw_pdf, '{"synthetic":true}', "synthetic-review.md"))
        path.unlink()
        self.unbind()
        self.make_service()
        self.assertEqual(self.store.db.execute("SELECT pdf_bytes,metadata_json,markdown_path FROM knowledge_pdf_versions WHERE source_id=?",
                                              (source_id,)).fetchone()[0], raw_pdf)
        self.assertEqual(self.version(source_id)["raw_text"], "alpha synthetic evidence")
        self.assertEqual(self.active(source_id), 0)

    async def test_root_migration_backup_precedes_archival_and_is_not_repeated(self):
        path, source_id = await self.ready()
        pdf = b"%PDF-1.4 synthetic backup original bytes"
        with self.store.db:
            self.store.db.execute("INSERT INTO knowledge_pdf_versions VALUES(?,?,?,?)",
                                  (source_id, pdf, None, None))
        self.unbind()
        before = self.version(source_id)
        path.unlink()
        self.make_service()
        backups = list((self.config.state_dir / "migration_backups").glob("memory.before-root-binding-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as copy:
            copy.row_factory = sqlite3.Row
            self.assertEqual(dict(copy.execute("SELECT * FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()), before)
            self.assertEqual(copy.execute("SELECT pdf_bytes FROM knowledge_pdf_versions WHERE source_id=?", (source_id,)).fetchone()[0], pdf)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM memory_records WHERE source_id=? AND active=1", (source_id,)).fetchone()[0], 1)
            self.assertIsNone(copy.execute("SELECT root FROM knowledge_root_bindings WHERE scope=?", (self.service.scope,)).fetchone())
        self.make_service()
        self.assertEqual(len(list((self.config.state_dir / "migration_backups").glob("memory.before-root-binding-*.sqlite3"))), 1)

    async def test_unknown_usage_missing_and_returning_bytes_cannot_create_fresh_budget(self):
        path, source_id = await self.ready()
        raw = path.read_bytes()
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='failed',error='interrupted_unknown_usage' WHERE source_id=?", (source_id,))
            self.store.db.execute("UPDATE knowledge_file_audit SET remote_usage_unknown=1 WHERE source_id=?", (source_id,))
        before = self.version(source_id)
        path.unlink()
        self.unbind()
        restarted = self.make_service()
        restarted.scan_once()
        path.write_bytes(raw)
        restarted.scan_once()
        self.assertEqual(self.pointers(source_id), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)
        self.assertFalse(await restarted.label_next())
        self.assertEqual(self.version(source_id)["input_tokens"], before["input_tokens"])
        self.assertEqual(self.store.db.execute("SELECT digest FROM knowledge_root_quarantine WHERE source_id=?", (source_id,)).fetchone()[0], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.store.db.execute("SELECT error FROM knowledge_file_audit WHERE source_id=?", (source_id,)).fetchone()[0], "knowledge_root_quarantined_unknown_usage")
        path.write_bytes(b"alpha explicit changed new file")
        restarted.scan_once()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 2)

    async def test_matching_unknown_guild_hold_is_kept_as_no_replay_quarantine(self):
        path, source_id = await self.ready()
        digest = self.version(source_id)["digest"]
        with self.store.db:
            self.store.db.execute("DELETE FROM knowledge_desired WHERE source_id=?", (source_id,))
            self.store.db.execute("DELETE FROM knowledge_published WHERE source_id=?", (source_id,))
            self.store.db.execute("UPDATE knowledge_versions SET scope='',status='failed',error='legacy_scope_unknown' WHERE source_id=?", (source_id,))
            self.graph.deactivate_source(self.service.scope, source_id, commit=False)
            self.store.db.execute("INSERT INTO knowledge_migration_hold VALUES(?,?,?,?)", (self.service.scope, path.name, digest, source_id))
        self.unbind()
        restarted = self.make_service()
        self.assertEqual(self.pointers(source_id), 1)
        self.assertEqual(self.active(source_id), 0)
        self.assertEqual(self.version(source_id)["error"], "legacy_scope_unknown")
        restarted.scan_once()
        self.assertFalse(await restarted.label_next())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)

    async def test_unbound_reserved_file_cannot_keep_a_published_source_active(self):
        path, source_id = await self.ready()
        reserved = self.config.knowledge_dir / "_staging" / "source.md"
        reserved.parent.mkdir()
        reserved.write_bytes(path.read_bytes())
        with self.store.db:
            for table in ("knowledge_versions", "knowledge_desired", "knowledge_published", "knowledge_file_audit"):
                self.store.db.execute(f"UPDATE {table} SET path='_staging/source.md' WHERE source_id=?", (source_id,))
        self.unbind()
        self.make_service()
        self.assertEqual((self.active(source_id), self.pointers(source_id)), (0, 0))
        self.assertTrue(reserved.exists())

    async def test_active_service_revalidates_root_before_scan_snapshot_and_retry(self):
        path, _ = await self.ready()
        self.config.knowledge_dir = self.root / "unexpected-external-folder"
        with patch("indeces.knowledge.os.walk", side_effect=AssertionError("must not scan unsafe root")):
            for action in (self.service._scan_files, self.service.scan_once, self.service.retry_failed,
                           lambda: self.service._snapshot_file(path)):
                with self.assertRaises(GovernedError):
                    action()


if __name__ == "__main__":
    unittest.main()
