"""Independent parser/admission/maintenance boundaries with zero transports.

Real TOML loading, Runtime/Knowledge constructors, persistent local ledger,
read-only SQLite and Console loop ownership are exercised. All prices and
credentials are synthetic; this suite certifies no live provider tariff.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from indeces import console, console_session, reingest
from indeces.adapter import OpenAIAdapter
from indeces.config import SessionBudgetConfig, load_config
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.runtime import Runtime
from indeces.session_budget import SessionBudgetAdapter, SessionBudgetLedger
from indeces.store import Store


def session_settings(**changes):
    values = dict(enabled=True, allow_real_calls=True,
        session_id="independent-config-boundary-synthetic",
        ceiling_usd="5.00", model="gpt-6.1-sol",
        base_url="https://api.openai.com/v1", input_usd_per_million="0.1",
        output_usd_per_million="0.2", per_call_usd="0.01",
        reference="synthetic-upper-prices-no-provider-certification",
        valid_until_utc="2099-01-01T00:00:00+00:00",
        count_fee_verified=True,
        count_fee_reference="synthetic-count-fee-in-per-call-upper")
    values.update(changes)
    return values


def write_config(root, *, settings=None, runtime=None, adapter_url=None, label_reasoning='low'):
    """A genuine legacy three-stage public fixture, no production config read."""
    def literal(value):
        return json.dumps(value, ensure_ascii=False)
    text = 'name = "Indeces"\nstate_dir = "state"\nscratch_dir = "scratch"\nknowledge_dir = "knowledge"\n'
    text += '[discord]\nguild_id = "123"\nchannel_ids = ["456"]\n'
    if runtime:
        text += '[runtime]\n' + ''.join(f'{key} = {literal(value)}\n' for key, value in runtime.items())
    text += '[adapter]\nmodel = "gpt-6.1-sol"\nbase_url = ' + literal(adapter_url or "https://api.openai.com/v1") + '\n'
    for stage in ('label', 'summary', 'reply'):
        reasoning = label_reasoning if stage == 'label' else 'low'
        text += f'[adapter.budgets.{stage}]\ninput_tokens = 4096\noutput_tokens = 512\nseconds = 15\nreasoning = {literal(reasoning)}\n'
    if settings is not None:
        text += '[session_budget]\n' + ''.join(f'{key} = {literal(value)}\n' for key, value in settings.items())
    target = root / 'synthetic-config.toml'
    target.write_text(text, encoding='utf-8')
    return target


class ConfigFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='session-config-boundary-')
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def load(self, **kwargs):
        return load_config(write_config(self.root, **kwargs))


class SessionConfigParsingTests(ConfigFixture, unittest.TestCase):
    def test_legacy_three_stage_file_loads_without_session_rewrite(self):
        path = write_config(self.root)
        before = path.read_bytes()
        config = load_config(path)
        self.assertEqual(config.session_budget, SessionBudgetConfig())
        self.assertIsNone(console.checked_session_envelope(config))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(config.adapter.budgets), {'label', 'summary', 'reply', 'selection', 'query'})
        self.assertEqual(config.adapter.budgets['reply'].input_tokens, 4096)

    def test_optional_empty_and_explicit_off_table_preserve_legacy_config(self):
        original = self.load()
        for settings in ({}, {'enabled': False}):
            with self.subTest(settings=settings):
                loaded = self.load(settings=settings)
                self.assertEqual(loaded, original)
                self.assertFalse(console.controlled_session_enabled(loaded))
                self.assertIsNone(console.guard_trial_maintenance(loaded))

    def test_disabled_gate_does_not_require_tariffs_or_real_call_permission(self):
        loaded = self.load(settings={'enabled': False, 'model': 'unreviewed-placeholder',
            'valid_until_utc': 'not-an-expiry', 'ceiling_usd': 'not-a-price'})
        with patch.object(console, 'SessionPriceEnvelope', side_effect=AssertionError('disabled gate must not parse prices')) as prices:
            self.assertIsNone(console.checked_session_envelope(loaded))
        prices.assert_not_called()

    def test_session_booleans_are_not_toml_integer_or_string_truthiness(self):
        for key in ('enabled', 'allow_real_calls', 'count_fee_verified'):
            for wrong in (0, 1, 'true', 'false'):
                with self.subTest(key=key, wrong=wrong), self.assertRaises(ValueError):
                    self.load(settings={key: wrong})

    def test_money_and_identity_fields_require_strings_even_when_off(self):
        for key, value in (('ceiling_usd', 5.0), ('per_call_usd', 0),
                           ('session_id', True), ('input_usd_per_million', 1)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(settings={'enabled': False, key: value})

    def test_unknown_session_field_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            self.load(settings={'enabled': False, 'allow_provider_call': True})

    def test_valid_real_parser_preserves_exact_envelope_and_native_caps(self):
        config = self.load(settings=session_settings())
        envelope = console.checked_session_envelope(config)
        self.assertEqual(envelope.model, config.adapter.model)
        self.assertEqual(envelope.base_url, config.adapter.base_url)
        self.assertEqual(envelope.per_call_usd, '0.01')
        self.assertEqual(config.adapter.budgets['selection'].input_tokens, 4096)
        self.assertEqual(config.adapter.budgets['reply'].output_tokens, 512)

    def test_untyped_enabled_alias_in_programmatic_config_also_refuses(self):
        original = self.load()
        config = replace(original, session_budget=SimpleNamespace(enabled=1))
        with self.assertRaises(ValueError):
            console.checked_session_envelope(config)


class SessionConfigAdmissionTests(ConfigFixture, unittest.IsolatedAsyncioTestCase):
    async def assert_closed_before_startup(self, settings, expected_code, *, adapter_url=None, runtime=None):
        config = self.load(settings=settings, adapter_url=adapter_url, runtime=runtime)
        with ExitStack() as stack:
            forbidden = [stack.enter_context(patch.object(console, name,
                side_effect=AssertionError('startup side effect before approval: ' + name)))
                for name in ('InstanceLock', 'create_model_adapter', 'Store', 'SessionBudgetLedger', 'ScratchLog')]
            with self.assertRaises(GovernedError) as caught:
                await console.serve(config, 'synthetic-key', 'synthetic-token')
            self.assertEqual(caught.exception.code, expected_code)
            for sentinel in forbidden:
                sentinel.assert_not_called()
        self.assertFalse(config.state_dir.exists())

    async def test_real_call_permission_is_required_before_raw_and_lease(self):
        await self.assert_closed_before_startup(session_settings(allow_real_calls=False), 'session_permission_required')

    async def test_count_fee_needs_both_exact_boolean_and_nonblank_attestation(self):
        for delta in ({'count_fee_verified': False}, {'count_fee_reference': ''}, {'count_fee_reference': '   '}):
            with self.subTest(delta=delta):
                await self.assert_closed_before_startup(session_settings(**delta), 'session_count_fee_unverified')

    async def test_price_model_and_url_match_exactly_before_raw(self):
        for delta in ({'model': 'gpt-6.1-SOL'}, {'base_url': 'https://api.openai.com/v1/'},
                      {'base_url': 'https://synthetic.invalid/v1'}):
            with self.subTest(delta=delta):
                await self.assert_closed_before_startup(session_settings(**delta), 'session_price_mismatch')
        # load_config permits the native adapter's existing trailing-slash form,
        # but admission still binds the envelope to that exact configured form.
        await self.assert_closed_before_startup(session_settings(), 'session_price_mismatch',
                                                adapter_url='https://api.openai.com/v1/')

    async def test_expired_or_non_utc_price_does_not_construct_raw_adapter(self):
        await self.assert_closed_before_startup(session_settings(valid_until_utc='2000-01-01T00:00:00+00:00'), 'session_price_expired')
        for expiry in ('2099-01-01T00:00:00', '2099-01-01T00:00:00+01:00'):
            with self.subTest(expiry=expiry):
                await self.assert_closed_before_startup(session_settings(valid_until_utc=expiry), 'session_price_unverified')

    async def test_missing_identity_tariff_and_ceiling_fail_before_raw(self):
        await self.assert_closed_before_startup(session_settings(session_id='   '), 'session_identity_required')
        for delta in ({'reference': ''}, {'input_usd_per_million': 'NaN'},
                      {'output_usd_per_million': '-1'}, {'ceiling_usd': '5.01'},
                      {'ceiling_usd': '5.00000000000000000000000000000000001'},
                      {'ceiling_usd': '4.99999999999999999999999999999999999'},
                      {'ceiling_usd': '0'}, {'ceiling_usd': 'Infinity'},
                      {'ceiling_usd': '0.' + '0' * 64 + '1'}):
            with self.subTest(delta=delta):
                await self.assert_closed_before_startup(session_settings(**delta), 'session_price_unverified')

    async def test_extra_model_stage_flag_is_not_added_to_controlled_session(self):
        for key in ('active_requery_enabled', 'path_requery_loop_enabled', 'communicability_enabled', 'path_hypotheses_enabled'):
            with self.subTest(key=key):
                await self.assert_closed_before_startup(session_settings(), 'session_extra_stages_forbidden', runtime={key: True})

    async def test_direct_runtime_and_knowledge_reject_bare_native_adapter_before_graph_or_paths(self):
        config = self.load(settings=session_settings())
        transport = AsyncMock(side_effect=AssertionError('no transport in boundary tests'))
        native = OpenAIAdapter(config.adapter, Mock(), 'synthetic-key', request=transport)
        with patch('indeces.runtime.MemoryGraph', side_effect=AssertionError('no graph before wrapper')) as graph:
            with self.assertRaises(GovernedError) as caught:
                Runtime(config, Mock(), native, Mock())
            self.assertEqual(caught.exception.code, 'session_adapter_required')
            graph.assert_not_called()
        with patch('indeces.knowledge.validate_knowledge_root', side_effect=AssertionError('no paths before wrapper')) as paths:
            with self.assertRaises(GovernedError) as caught:
                KnowledgeService(config, Mock(), Mock(), native, Mock())
            self.assertEqual(caught.exception.code, 'session_adapter_required')
            paths.assert_not_called()
        transport.assert_not_awaited()
        await native.close()

    async def wrapped_knowledge(self):
        config = self.load(settings=session_settings())
        transport = AsyncMock(side_effect=AssertionError('no transport in maintenance boundary tests'))
        scratch = Mock()
        native = OpenAIAdapter(config.adapter, scratch, 'synthetic-key', request=transport)
        envelope = console.checked_session_envelope(config)
        ledger = SessionBudgetLedger(config.state_dir / 'synthetic-session.sqlite', session_id=config.session_budget.session_id,
                                     envelope=envelope, ceiling_usd=config.session_budget.ceiling_usd)
        self.addCleanup(ledger.close)
        wrapper = SessionBudgetAdapter(native, ledger)
        store = Store(config.state_dir)
        self.addCleanup(store.close)
        graph = MemoryGraph(store.db, self_marks=(config.name,))
        knowledge = KnowledgeService(config, store, graph, wrapper, scratch)
        self.addAsyncCleanup(knowledge.close)
        self.addAsyncCleanup(native.close)
        return config, native, wrapper, ledger, store, knowledge, transport

    async def test_existing_wrapped_knowledge_start_and_retry_are_closed_without_reservation(self):
        config, native, wrapper, ledger, store, knowledge, transport = await self.wrapped_knowledge()
        self.assertIs(knowledge.adapter, wrapper)
        self.assertIs(wrapper.base, native)
        before = ledger.snapshot()
        with patch.object(knowledge, '_check_root', side_effect=AssertionError('retry must refuse before database/root scan')) as scan:
            for operation in (knowledge.start, lambda: knowledge.retry_failed('literal.md', include_incomplete=True)):
                with self.assertRaises(GovernedError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, 'session_maintenance_forbidden')
            scan.assert_not_called()
        self.assertEqual(knowledge._tasks, [])
        self.assertEqual(ledger.snapshot(), before)
        transport.assert_not_awaited()

    async def test_paid_reingest_phases_fail_real_config_before_lease_or_credentials(self):
        self.load(settings=session_settings())
        path = self.root / 'synthetic-config.toml'
        for phase in ('pilot', 'complete'):
            with self.subTest(phase=phase), ExitStack() as stack:
                forbidden = [stack.enter_context(patch.object(reingest, name,
                    side_effect=AssertionError('paid maintenance side effect: '+name)))
                    for name in ('InstanceLock', 'load_openai_key', 'OpenAIAdapter', 'backup_database')]
                with self.assertRaises(GovernedError) as caught:
                    await reingest.maintenance(path, phase=phase, batch_id='synthetic-batch', operator_confirmed=True)
                self.assertEqual(caught.exception.code, 'session_maintenance_forbidden')
                for sentinel in forbidden:
                    sentinel.assert_not_called()
        self.assertFalse((self.root / 'state').exists())

    async def test_reingest_status_allows_real_read_only_database_with_no_price_permission(self):
        config = self.load(settings=session_settings(allow_real_calls=False, count_fee_verified=False),
                           label_reasoning='medium')
        historical_config = replace(config, session_budget=SessionBudgetConfig())
        database = config.state_dir / 'memory.sqlite3'
        transport = AsyncMock(side_effect=AssertionError('no historical fixture transport'))
        native = OpenAIAdapter(config.adapter, Mock(), 'synthetic-key', request=transport)
        # Seed an empty historical batch through the real schema constructors.
        # This is a local status fixture, not a claim that an empty grant can be
        # created by the operational grant command.
        with closing(Store(config.state_dir)) as store:
            graph = MemoryGraph(store.db, self_marks=(config.name,))
            schema = KnowledgeService(historical_config, store, graph, native, Mock())
            await schema.close()
            reingest.BoundedReingest(historical_config, store.db, graph, native, Mock())
            with store.db:
                store.db.execute('CREATE TABLE synthetic_marker (value TEXT)')
                store.db.execute('INSERT INTO synthetic_marker VALUES (?)', ('unchanged-public-fixture',))
                store.db.execute('''INSERT INTO reingest_batches
                    (batch_id,grant_key,scope,status,created_at,approval,contract_json,
                     old_snapshot_sha256,checkpoint_through_id) VALUES(?,?,?,?,?,?,?,?,?)''',
                    ('synthetic-batch', 'synthetic-empty-history', '123:knowledge',
                     'approved', 1.0, reingest.APPROVAL,
                     json.dumps(reingest.CONTRACT, sort_keys=True, separators=(',', ':'), ensure_ascii=False),
                     hashlib.sha256(b'').hexdigest(), 0))
        await native.close()
        transport.assert_not_awaited()
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        observed = []
        actual_constructor = reingest.BoundedReingest
        def status_reader(actual_config, db, graph, adapter, scratch, *, initialize):
            self.assertEqual(actual_config, config)
            self.assertFalse(initialize)
            self.assertIsNone(adapter)
            observed.append(db.execute('SELECT value FROM synthetic_marker').fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('INSERT INTO synthetic_marker VALUES ("must-refuse-write")')
            service = actual_constructor(actual_config, db, graph, adapter, scratch, initialize=initialize)
            self.assertIs(type(service), actual_constructor)
            return service
        with patch.object(reingest, 'BoundedReingest', side_effect=status_reader), \
             patch.object(reingest, 'OpenAIAdapter', side_effect=AssertionError('no raw in status')) as raw, \
             patch.object(reingest, 'load_openai_key', side_effect=AssertionError('no credentials in status')) as key, \
             patch.object(reingest, 'backup_database', side_effect=AssertionError('no backup in status')) as backup, \
             redirect_stdout(io.StringIO()):
            result = await reingest.maintenance(self.root / 'synthetic-config.toml', phase='status', batch_id='synthetic-batch')
        self.assertEqual(result['batch_id'], 'synthetic-batch')
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(result['documents'], [])
        self.assertTrue(result['old_records_preserved'])
        self.assertEqual(result['totals']['unknown_calls'], 0)
        self.assertEqual(observed, ['unchanged-public-fixture'])
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        for sentinel in (raw, key, backup):
            sentinel.assert_not_called()

    async def test_console_retry_keeps_running_controlled_knowledge_when_caller_config_is_off(self):
        config, native, wrapper, ledger, store, knowledge, transport = await self.wrapped_knowledge()
        ready = threading.Event()
        lease = Mock()
        async def fake_serve(actual_config, key, token, *, lease, knowledge_ready):
            self.assertIs(actual_config, config)
            knowledge_ready(knowledge)
            ready.set()
            await asyncio.Event().wait()
        service = console_session.ServiceTask(config, 'synthetic-key', 'synthetic-token', lease, fake_serve)
        try:
            self.assertTrue(await asyncio.to_thread(ready.wait, 3), 'synthetic service loop did not become ready')
            caller_config = replace(config, session_budget=SessionBudgetConfig())
            session = object.__new__(console_session.ConsoleSession)
            session.service, session.path, session._retries = service, self.root / 'synthetic-config.toml', []
            before = ledger.snapshot()
            with patch.object(knowledge, 'retry_failed', wraps=knowledge.retry_failed) as retry, redirect_stdout(io.StringIO()):
                session._retry(caller_config, 'literal.md')
                self.assertEqual(len(session._retries), 1)
                with self.assertRaises(GovernedError) as caught:
                    await asyncio.wrap_future(session._retries[0])
                self.assertEqual(caught.exception.code, 'session_maintenance_forbidden')
                retry.assert_called_once_with('literal.md', include_incomplete=True)
            self.assertIs(knowledge.config, config)
            self.assertIs(knowledge.adapter, wrapper)
            self.assertEqual(ledger.snapshot(), before)
            transport.assert_not_awaited()
        finally:
            service.stop()
            self.assertTrue(await asyncio.to_thread(service._done.wait, 3), 'synthetic service loop did not join')
            service._thread.join(timeout=1)
            self.assertFalse(service._thread.is_alive())
            lease.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
