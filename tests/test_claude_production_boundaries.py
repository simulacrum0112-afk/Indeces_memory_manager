"""Independent production-port admission boundaries, synthetic and offline.

Root owns execution. These cases neither read production config/credentials nor
claim a Claude tariff, paid call, service launch, or cross-provider ledger port.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, reingest
from indeces.config import SessionBudgetConfig, load_config
from indeces.contracts import GovernedError
from indeces.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]
MODELS = ("claude-opus-5-5", "claude-fable-5-1")


def configuration(root, *, model=MODELS[0], controlled=False, **runtime):
    base = load_config(ROOT / "config.example.toml")
    adapter = replace(base.adapter, provider="anthropic", model=model,
        base_url="https://api.anthropic.com/v1",
        budgets={stage: replace(budget, reasoning="high")
                 for stage, budget in base.adapter.budgets.items()})
    return replace(base, state_dir=root / "state", scratch_dir=root / "scratch",
        knowledge_dir=root / "knowledge", adapter=adapter,
        discord=replace(base.discord, guild_id="10"),
        runtime=replace(base.runtime, **runtime),
        session_budget=SessionBudgetConfig(enabled=controlled))


class NoDatabase:
    @property
    def db(self):
        raise AssertionError("database must not be reached before rejection")


# A minimal synthetic existing maintenance grant, not a schema migration test.
# Both known charge and unknown holds are intentionally retained in its rows.
STATUS_SCHEMA = """
CREATE TABLE knowledge_versions(source_id TEXT PRIMARY KEY,status TEXT);
CREATE TABLE knowledge_chunks(source_id TEXT,chunk_index INTEGER);
CREATE TABLE knowledge_pdf_versions(source_id TEXT);
CREATE TABLE knowledge_file_audit(source_id TEXT,remote_usage_unknown INTEGER);
CREATE TABLE knowledge_root_quarantine(source_id TEXT,reason TEXT);
CREATE TABLE memory_records(source_id TEXT,text TEXT);
CREATE TABLE checkpoints(id INTEGER PRIMARY KEY);
CREATE TABLE reingest_batches(batch_id TEXT PRIMARY KEY,grant_key TEXT,scope TEXT,
 status TEXT,created_at REAL,approval TEXT,contract_json TEXT,
 old_snapshot_sha256 TEXT,checkpoint_through_id INTEGER,pilot_passed INTEGER,error TEXT);
CREATE TABLE reingest_documents(attempt_id TEXT PRIMARY KEY,batch_id TEXT,scope TEXT,
 path TEXT,digest TEXT,old_source_id TEXT,new_source_id TEXT,total_chunks INTEGER,published INTEGER);
CREATE TABLE reingest_calls(call_key TEXT PRIMARY KEY,attempt_id TEXT,chunk_index INTEGER,
 status TEXT,phase TEXT,started_at REAL,elapsed_seconds REAL,actual_input_tokens INTEGER,
 actual_output_tokens INTEGER,reserved_input_tokens INTEGER,reserved_output_tokens INTEGER,error TEXT);
CREATE TABLE reingest_requests(client_request_id TEXT,call_key TEXT,path TEXT);
CREATE TABLE reingest_labels(attempt_id TEXT,chunk_index INTEGER,marks_json TEXT,call_key TEXT);
CREATE TABLE reingest_events(seq INTEGER PRIMARY KEY,batch_id TEXT,attempt_id TEXT,
 call_key TEXT,event TEXT,recorded_at REAL,details_json TEXT);
"""


def existing_status_database(config):
    config.state_dir.mkdir()
    config.knowledge_dir.mkdir()
    path = config.state_dir / "memory.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.executescript(STATUS_SCHEMA)
        db.execute("INSERT INTO knowledge_versions VALUES ('old-source','failed')")
        db.execute("INSERT INTO knowledge_file_audit VALUES ('old-source',1)")
        db.execute("INSERT INTO knowledge_root_quarantine VALUES ('old-source','old-unknown')")
        db.execute("INSERT INTO memory_records VALUES ('old-source','synthetic immutable record')")
        reader = reingest.BoundedReingest(config, db, None, None, None, initialize=False)
        old_hash = reader._old_hash(["old-source"], 0)
        db.execute("INSERT INTO reingest_batches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("batch", "grant", "10:knowledge", "usage_unknown", 1.0,
             reingest.SCOPED_APPROVAL, json.dumps(reingest.CONTRACT, ensure_ascii=False,
                 sort_keys=True, separators=(",", ":")), old_hash, 0, 0, "missing_usage"))
        db.execute("INSERT INTO reingest_documents VALUES (?,?,?,?,?,?,?,?,?)",
            ("attempt", "batch", "10:knowledge", "synthetic.pdf", "a" * 64,
             "old-source", "new-source", 3, 0))
        db.execute("INSERT INTO reingest_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("known", "attempt", 0, "completed", "generation", 1.0, 1.25,
             25, 8, 0, 0, None))
        db.execute("INSERT INTO reingest_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("unknown", "attempt", 1, "usage_unknown", "generation", 2.0, 8.25,
             None, None, 4096, 512, "missing_usage"))
        db.execute("INSERT INTO reingest_labels VALUES (?,?,?,?)",
            ("attempt", 0, '["synthetic"]', "known"))
        db.commit()
    return path


class RuntimeProviderBoundaryTests(unittest.TestCase):
    def test_claude_requery_modes_reject_before_graph_database_or_transport(self):
        combinations = ({"active_requery_enabled": True},
                        {"path_requery_loop_enabled": True},
                        {"active_requery_enabled": True, "path_requery_loop_enabled": True})
        with TemporaryDirectory() as directory:
            for model in MODELS:
                for flags in combinations:
                    config = configuration(Path(directory).resolve(), model=model, **flags)
                    adapter = SimpleNamespace(call=AsyncMock())
                    with self.subTest(model=model, flags=flags), \
                            patch("indeces.runtime.MemoryGraph", side_effect=AssertionError("graph entered")) as graph:
                        with self.assertRaises(GovernedError) as raised:
                            Runtime(config, NoDatabase(), adapter, Mock())
                        self.assertEqual(raised.exception.code, "claude_requery_unsupported")
                        self.assertIs(raised.exception.remote_usage_unknown, False)
                        self.assertIsNone(raised.exception.known_usage)
                        graph.assert_not_called()
                        adapter.call.assert_not_called()

    def test_ordinary_claude_constructor_accepts_local_diagnostics_without_a_model_call(self):
        with TemporaryDirectory() as directory:
            for model in MODELS:
                config = configuration(Path(directory).resolve(), model=model,
                    communicability_enabled=True, path_hypotheses_enabled=True)
                adapter = SimpleNamespace(call=AsyncMock())
                with self.subTest(model=model), closing(sqlite3.connect(":memory:")) as db:
                    runtime = Runtime(config, SimpleNamespace(db=db), adapter, Mock())
                    self.assertIs(runtime.adapter, adapter)
                    self.assertIs(runtime.graph.connection, db)
                    self.assertIsNone(runtime.session_stop_error())
                    adapter.call.assert_not_called()

    def test_openai_active_query_constructor_keeps_the_original_non_path_behavior(self):
        with TemporaryDirectory() as directory:
            base = load_config(ROOT / "config.example.toml")
            config = replace(base, runtime=replace(base.runtime, active_requery_enabled=True))
            adapter = SimpleNamespace(call=AsyncMock())
            with closing(sqlite3.connect(":memory:")) as db:
                runtime = Runtime(config, SimpleNamespace(db=db), adapter, Mock())
                self.assertIs(runtime.adapter, adapter)
                adapter.call.assert_not_called()

    def test_raw_claude_cannot_bypass_existing_controlled_runtime_wrapper_contract(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve(), controlled=True)
            with patch("indeces.runtime.MemoryGraph", side_effect=AssertionError("graph entered")) as graph:
                with self.assertRaises(GovernedError) as raised:
                    Runtime(config, NoDatabase(), SimpleNamespace(call=AsyncMock()), Mock())
            self.assertEqual(raised.exception.code, "session_adapter_required")
            graph.assert_not_called()


class ControlledClaudeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_controlled_claude_serve_rejects_before_all_owned_resources(self):
        with TemporaryDirectory() as directory:
            for model in MODELS:
                config = configuration(Path(directory).resolve(), model=model, controlled=True)
                before = config.session_budget
                with self.subTest(model=model), ExitStack() as stack:
                    effects = [stack.enter_context(patch.object(console, name,
                        side_effect=AssertionError(name + " entered"))) for name in
                        ("validate_runtime_paths", "InstanceLock", "SessionBudgetLedger", "ScratchLog",
                         "Store", "create_model_adapter", "KnowledgeService", "DiscordBridge")]
                    with self.assertRaises(GovernedError) as raised:
                        await console.serve(config, "synthetic-unused-key", "synthetic-unused-token")
                    self.assertEqual(raised.exception.code, "session_provider_unsupported")
                    self.assertIs(config.session_budget, before)
                    for effect in effects:
                        effect.assert_not_called()

    async def test_uncontrolled_claude_envelope_gate_does_not_construct_or_reset_a_ledger(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve())
            with patch.object(console, "SessionBudgetLedger") as ledger:
                self.assertIsNone(console.checked_session_envelope(config))
                ledger.assert_not_called()
            self.assertEqual(config.session_budget, SessionBudgetConfig())


class MaintenanceProviderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_paid_phases_decline_before_credentials_lease_database_and_backup(self):
        with TemporaryDirectory() as directory:
            for model in MODELS:
                config = configuration(Path(directory).resolve(), model=model)
                for phase in ("pilot", "complete", "invalid-paid-phase"):
                    with self.subTest(model=model, phase=phase), ExitStack() as stack:
                        stack.enter_context(patch.object(reingest, "load_config", return_value=config))
                        effects = [stack.enter_context(patch.object(reingest, name,
                            side_effect=AssertionError(name + " entered"))) for name in
                            ("validate_runtime_paths", "InstanceLock", "validate_managed_path",
                             "load_openai_key", "OpenAIAdapter", "MemoryGraph", "ScratchLog", "backup_database")]
                        connect = stack.enter_context(patch.object(reingest.sqlite3, "connect",
                            side_effect=AssertionError("database entered")))
                        with self.assertRaisesRegex(ValueError, "^claude_paid_maintenance_unsupported$"):
                            await reingest.maintenance(Path(directory) / "unused.toml", phase=phase,
                                source_ids=["synthetic-source"], operator_confirmed=True, resume_known=True)
                        for effect in effects:
                            effect.assert_not_called()
                        connect.assert_not_called()

    async def test_direct_claude_mutating_constructor_declines_before_path_or_database(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve())
            with patch.object(reingest, "validate_runtime_paths", side_effect=AssertionError("paths entered")) as paths:
                with self.assertRaisesRegex(ValueError, "^claude_paid_maintenance_unsupported$"):
                    reingest.BoundedReingest(config, Mock(), Mock(), Mock(), Mock())
                paths.assert_not_called()

    async def test_controlled_maintenance_preserves_existing_denial_precedence(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve(), controlled=True)
            with patch.object(reingest, "load_config", return_value=config), \
                    patch.object(reingest, "InstanceLock") as lease:
                with self.assertRaises(GovernedError) as raised:
                    await reingest.maintenance(Path(directory) / "unused.toml", phase="pilot")
                self.assertEqual(raised.exception.code, "session_maintenance_forbidden")
                lease.assert_not_called()

    async def test_claude_status_reads_known_charge_and_unknown_hold_without_mutation(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve())
            path = existing_status_database(config)
            original = path.read_bytes()
            original_connect = sqlite3.connect
            openings = []

            def read_only_connect(database, *args, **kwargs):
                openings.append((database, kwargs.copy()))
                self.assertEqual(database, path.as_uri() + "?mode=ro")
                self.assertIs(kwargs.get("uri"), True)
                return original_connect(database, *args, **kwargs)

            lease = SimpleNamespace(close=Mock())
            with ExitStack() as stack:
                stack.enter_context(patch.object(reingest, "load_config", return_value=config))
                lock = stack.enter_context(patch.object(reingest, "InstanceLock", return_value=lease))
                stack.enter_context(patch.object(reingest.sqlite3, "connect", side_effect=read_only_connect))
                stack.enter_context(patch.object(reingest, "_console_notice"))
                effects = [stack.enter_context(patch.object(reingest, name,
                    side_effect=AssertionError(name + " entered"))) for name in
                    ("load_openai_key", "OpenAIAdapter", "MemoryGraph", "ScratchLog", "backup_database")]
                report = await reingest.maintenance(Path(directory) / "unused.toml", phase="status", batch_id="batch")
                lock.assert_called_once_with(config.state_dir)
                for effect in effects:
                    effect.assert_not_called()
            self.assertEqual(len(openings), 1)
            lease.close.assert_called_once_with()
            self.assertEqual(report["status"], "usage_unknown")
            self.assertTrue(report["old_records_preserved"])
            self.assertEqual((report["totals"]["input_tokens"], report["totals"]["output_tokens"]), (25, 8))
            self.assertEqual((report["totals"]["reserved_input_tokens"], report["totals"]["reserved_output_tokens"]), (4096, 512))
            self.assertEqual(report["totals"]["unknown_calls"], 1)
            self.assertEqual(report["totals"]["elapsed_seconds"], 9.5)
            self.assertEqual(path.read_bytes(), original)

    async def test_read_only_service_cannot_run_even_with_writable_fixture_connection(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve())
            path = existing_status_database(config)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with closing(sqlite3.connect(path)) as db:
                reader = reingest.BoundedReingest(config, db, None, None, None, initialize=False)
                with patch.object(reader, "_assert_old", side_effect=AssertionError("batch entered")) as old:
                    for phase in ("pilot", "complete"):
                        with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "^maintenance_read_only$"):
                            await reader.run("batch", phase=phase, resume_known=True)
                    old.assert_not_called()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    async def test_existing_paid_object_cannot_switch_to_claude_and_continue(self):
        with TemporaryDirectory() as directory:
            config = configuration(Path(directory).resolve())
            path = existing_status_database(config)
            before = path.read_bytes()
            with closing(sqlite3.connect(path)) as db:
                native_adapter = replace(config.adapter, provider="openai", model="gpt-6.1-sol",
                    base_url="https://api.openai.com/v1",
                    budgets={stage: replace(budget, reasoning="medium")
                             for stage, budget in config.adapter.budgets.items()})
                reader = reingest.BoundedReingest(replace(config, adapter=native_adapter),
                    db, None, Mock(), Mock())
                # Simulate a retained paid object receiving a changed config:
                # the run gate must check its current provider before recovery.
                reader.config = config
                with patch.object(reader, "_assert_old", side_effect=AssertionError("batch entered")) as old, \
                        patch.object(reader, "recover_interrupted", side_effect=AssertionError("recovery entered")) as recover:
                    with self.assertRaisesRegex(ValueError, "^claude_paid_maintenance_unsupported$"):
                        await reader.run("batch", resume_known=True)
                    old.assert_not_called()
                    recover.assert_not_called()
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
