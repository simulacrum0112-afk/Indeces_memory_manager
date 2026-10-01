"""Product-name migration preserves durable evidence and protocol identity."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from indeces import credentials, prompts, scratch
from indeces.config import load_config
from indeces.memory import MemoryGraph
from indeces.run_records import validate_graph_audit
from indeces.runtime import Runtime
from indeces.store import Store


GUILD = "123456789012345678"
TOKEN = "synthetic-name-migration-token-offline-only"


def legacy_mutex_owner(directory, ready, release):
    """Use the literal 0.6 protocol name, independent of the current helper."""
    import ctypes
    from ctypes import wintypes
    identity = os.path.normcase(str(Path(directory).resolve()))
    name = "Local\\IndicesScratch-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    unlock = kernel.ReleaseMutex
    unlock.argtypes = [wintypes.HANDLE]
    unlock.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(None, True, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ready.set()
        if not release.wait(timeout=10):
            raise RuntimeError("synthetic legacy mutex release timeout")
    finally:
        unlock(handle)
        close(handle)


class NameMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.example = (Path(__file__).resolve().parents[1] / "config.example.toml").read_bytes()
        self.path = self.root / "config.local.toml"

    def write_config(self, name="Indices"):
        content = re.sub(rb'^name\s*=\s*"[^"]*"[^\r\n]*',
                         "name = ".encode() + json.dumps(name).encode(), self.example, count=1, flags=re.MULTILINE)
        content = re.sub(rb'^guild_id\s*=\s*"[^"]*"', b'guild_id = "' + GUILD.encode() + b'"',
                         content, count=1, flags=re.MULTILINE)
        self.path.write_bytes(content)
        return content

    def test_legacy_name_loads_as_new_identity_without_editing_configuration(self):
        for name in ("Indices", "indices", "INDICES", "  Indices  "):
            with self.subTest(name=name):
                original = self.write_config(name)
                config = load_config(self.path)
                self.assertEqual(config.name, "Indeces")
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(config.discord.guild_id, GUILD)

    def test_old_and_new_configuration_have_identical_nonidentity_values(self):
        self.write_config("Indices")
        old = load_config(self.path)
        self.write_config("Indeces")
        new = load_config(self.path)
        self.assertEqual(asdict(old), asdict(new))

    def test_absent_name_defaults_to_new_identity(self):
        contents = self.write_config("Indices")
        self.path.write_bytes(re.sub(rb'^name[^\r\n]*\r?\n', b"", contents, count=1, flags=re.MULTILINE))
        self.assertEqual(load_config(self.path).name, "Indeces")

    def test_custom_name_retains_existing_custom_configuration_behavior(self):
        contents = self.write_config("Research Archivist")
        self.assertEqual(load_config(self.path).name, "Research Archivist")
        self.assertEqual(self.path.read_bytes(), contents)

    def test_loaded_legacy_configuration_generates_new_persona_identity(self):
        self.write_config("Indices")
        instructions = prompts.reply_instructions(load_config(self.path).name)
        self.assertIn("You are Indeces,", instructions)
        self.assertNotIn("You are Indices,", instructions)

    def test_default_and_explicit_product_aliases_exclude_both_spellings(self):
        for names in (None, ("Indeces",), ("Indices",), ("INDICES", "botname")):
            with self.subTest(names=names):
                db = sqlite3.connect(":memory:")
                self.addCleanup(db.close)
                graph = MemoryGraph(db) if names is None else MemoryGraph(db, self_marks=names)
                self.assertTrue({"indices", "indeces"} <= graph.self_marks)
                fact = graph.add("scope", "source", "author", [{"text": "alpha material", "quote": "alpha material",
                    "marks": ["Indices", "Indeces", "alpha"]}], 1)[0]
                self.assertEqual(fact["marks"], ["alpha"])
                audit = {}
                self.assertEqual(graph.retrieve("scope", [], "Indices Indeces", 2, event_id="self-only", audit=audit), [])
                self.assertEqual(audit["match"]["direct_hits"], [])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0], 0)
                validate_graph_audit(audit)

    def test_custom_self_marks_do_not_gain_unrequested_product_aliases(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        graph = MemoryGraph(db, self_marks=("CustomArchivist",))
        self.assertEqual(graph.self_marks, {"customarchivist"})
        graph.add("scope", "source", "author", [{"text": "Indices topic", "quote": "Indices topic",
                  "marks": ["Indices", "CustomArchivist"]}], 1)
        result = graph.retrieve("scope", [], "Indices", 2, event_id="custom-identity")
        self.assertEqual(result[0]["direct_marks"], ["indices"])

    @staticmethod
    def durable_rows(db, table):
        return list(db.execute(f"SELECT * FROM {table} ORDER BY 1,2"))

    def test_legacy_cached_alias_edges_migrate_without_rewriting_historical_evidence(self):
        # Before 0.2 the stock identity was Indeces; 0.2-0.6 used Indices.
        # Simulate those actual indexing policies, rather than editing facts.
        for old_self, previously_indexed_alias in (("indices", "indeces"), ("indeces", "indices")):
            with self.subTest(old_self=old_self):
                db = sqlite3.connect(":memory:")
                self.addCleanup(db.close)
                legacy = MemoryGraph(db)
                legacy.self_marks = {old_self}
                legacy.add("scope", "legacy-source", "author", [{"text": "original Indices and Indeces material",
                    "quote": "original Indices and Indeces material", "marks": ["alpha", "beta", previously_indexed_alias]}], 1)
                old_audit = {}
                legacy.retrieve("scope", [], "alpha beta", 2, event_id="legacy-event", audit=old_audit)
                validate_graph_audit(old_audit)
                before = {table: self.durable_rows(db, table) for table in
                          ("memory_records", "memory_dynamic", "memory_events", "memory_cycles", "memory_event_audits")}
                self.assertTrue(db.execute("SELECT 1 FROM memory_support WHERE a=? OR b=?",
                                          (previously_indexed_alias, previously_indexed_alias)).fetchone())
                migrated = MemoryGraph(db)
                for table, rows in before.items():
                    self.assertEqual(self.durable_rows(db, table), rows, table)
                for table in ("memory_support", "memory_static"):
                    cached = list(db.execute(f"SELECT a,b,context_json FROM {table}"))
                    self.assertTrue(cached)
                    for a, b, context in cached:
                        self.assertFalse({a, b} & {"indices", "indeces"})
                        self.assertFalse(set(json.loads(context)) & {"indices", "indeces"})
                no_hit = {}
                self.assertEqual(migrated.retrieve("scope", [], "Indices Indeces", 3,
                                                  event_id="new-self-only", audit=no_hit), [])
                validate_graph_audit(no_hit)
                self.assertEqual(self.durable_rows(db, "memory_dynamic"), before["memory_dynamic"])
                audit = {}
                selected = migrated.retrieve("scope", [], "alpha beta", 4, event_id="new-query", audit=audit)
                self.assertEqual(selected[0]["source_id"], "legacy-source")
                self.assertEqual(selected[0]["direct_marks"], ["alpha", "beta"])
                validate_graph_audit(audit)
                archived_alias_changes = [edge for edge in audit["observation"]["changed_edges"]
                                          if previously_indexed_alias in (edge["a"], edge["b"])]
                self.assertTrue(archived_alias_changes)
                self.assertTrue(all(not edge["active_source_support"] for edge in archived_alias_changes))
                self.assertEqual(db.execute("SELECT payload_json FROM memory_event_audits WHERE event_id='legacy-event'").fetchone()[0],
                                 before["memory_event_audits"][0][2])

    def test_runtime_guild_scope_and_store_path_do_not_depend_on_product_name(self):
        self.write_config("Indices")
        config = load_config(self.path)
        store = Store(config.state_dir)
        self.addCleanup(store.close)
        adapter, log = Mock(), Mock()
        runtime = Runtime(config, store, adapter, log)
        self.assertEqual(runtime.knowledge_scope, GUILD + ":knowledge")
        self.assertTrue({"indices", "indeces"} <= runtime.graph.self_marks)
        self.assertTrue((config.state_dir / "memory.sqlite3").is_file())
        adapter.call.assert_not_called()

    def test_legacy_self_alias_only_in_cached_context_triggers_rebuild(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        graph = MemoryGraph(db)
        graph.add("scope", "source", "author", [{"text": "original alpha beta source", "quote": "original alpha beta source",
                  "marks": ["alpha", "beta"]}], 1)
        # A partial historical cache migration can remove alias endpoints while
        # leaving a self-name in the context gate of an ordinary alpha/beta edge.
        with db:
            for table in ("memory_static", "memory_support"):
                db.execute(f"UPDATE {table} SET context_json=? WHERE a='alpha' AND b='beta'",
                           (json.dumps(["indices", "indeces"]),))
        records = self.durable_rows(db, "memory_records")
        migrated = MemoryGraph(db)
        for table in ("memory_static", "memory_support"):
            context = db.execute(f"SELECT context_json FROM {table} WHERE a='alpha' AND b='beta'").fetchone()[0]
            self.assertEqual(json.loads(context), [])
        self.assertEqual(self.durable_rows(db, "memory_records"), records)
        audit = {}
        result = migrated.retrieve("scope", [], "alpha beta", 2, event_id="context-rebuilt", audit=audit)
        self.assertEqual(result[0]["ranking_score"], 1.0)
        self.assertEqual(result[0]["dynamic_score"], 1.99)
        validate_graph_audit(audit)

    def test_saved_credentials_are_name_neutral_and_do_not_need_reencryption(self):
        self.write_config("Indices")
        with patch.object(credentials, "_protect", return_value=b"synthetic-opaque-ciphertext") as protect:
            credentials.save_discord_token(self.path, GUILD, TOKEN)
        original_payload = protect.call_args.args[0]
        saved = credentials.secret_path(self.path)
        ciphertext = saved.read_bytes()
        self.write_config("Indeces")
        with patch.object(credentials, "_unprotect", return_value=original_payload):
            self.assertEqual(credentials.load_discord_token(self.path, GUILD), TOKEN)
        self.assertEqual(saved.name, "config.local.discord.secret")
        self.assertEqual(saved.read_bytes(), ciphertext)
        self.assertNotIn(TOKEN.encode(), ciphertext)
        self.assertEqual(credentials._MAGIC, b"INDECES-DISCORD-DPAPI\x00\x01")
        self.assertEqual(credentials._ENTROPY, b"Indeces_memory_manager.discord.credentials.v1")

    @unittest.skipUnless(os.name == "nt", "Windows 0.6 mutex interoperability proof")
    def test_new_observer_uses_same_mutex_as_an_old_runtime_process(self):
        now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        log = scratch.ScratchLog(self.root / "scratch", clock=lambda: now)
        self.addCleanup(log.close)
        log.write("observation", value="synthetic-runtime-record")
        log.close()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        old = context.Process(target=legacy_mutex_owner, args=(str(log.path.parent), ready, release))
        old.start()
        try:
            self.assertTrue(ready.wait(timeout=5))
            with patch.object(scratch, "_READER_WAIT_MILLISECONDS", 20), \
                    self.assertRaisesRegex(OSError, "temporarily busy"):
                scratch.read_snapshot(log.path, max_bytes=1024 * 1024)
        finally:
            release.set()
            old.join(timeout=5)
            if old.is_alive():
                old.terminate()
                old.join(timeout=5)
        self.assertEqual(old.exitcode, 0)
        self.assertEqual(scratch.read_snapshot(log.path, max_bytes=1024 * 1024)["records"][0]["fields"]["value"],
                         "synthetic-runtime-record")


if __name__ == "__main__":
    unittest.main()
