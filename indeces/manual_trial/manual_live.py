"""Finite opt-in manual acceptance; default execution is an offline mock.

No private configuration or credential is read merely by importing this file.
Independent evaluators provide their own frozen input at manual run time.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

MODULE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = (MODULE_ROOT.parents[1] if MODULE_ROOT.parent.name == 'indeces'
               else MODULE_ROOT / 'checkpoint-repo')
PROJECT = Path('D:/Indeces')
RUN_ROOT = PROJECT / 'build/manual-pilot-runs'

from .pilot_gate import MODEL, PilotBlocked, PilotGate, PilotLimits, Tariff, canonical_hash
from .pilot_launcher import candidate_identity, run_mock, validated_inputs


def read_json(path):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise PilotBlocked('duplicate_metadata_key')
            value[key] = item
        return value
    data = Path(path).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise PilotBlocked('metadata_too_large')
    return json.loads(data.decode('utf-8'), object_pairs_hook=unique_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(PilotBlocked('invalid_metadata_number')))


def input_contract(value):
    bundle = validated_inputs(value)
    if len({row['question'] for row in bundle['questions']}) != len(bundle['questions']):
        raise PilotBlocked('duplicate_question_text')
    for row in bundle['questions']:
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', row['item_id'])
                or row['question'] != row['question'].strip()):
            raise PilotBlocked('invalid_input_bundle')
    return bundle


def source_modules(candidate):
    candidate = Path(candidate).resolve()
    sys.path.insert(0, str(candidate))
    from indeces import __version__
    from indeces.adapter import OpenAIAdapter
    from indeces.config import Budget
    from indeces.memory import MemoryGraph
    from indeces.runtime import Runtime
    from indeces.scratch import ScratchLog
    from indeces.selection_policy import RankedTopThreeSelector
    from indeces.store import Store
    for name, module in list(sys.modules.items()):
        if name != 'indeces' and not name.startswith('indeces.'):
            continue
        filename = getattr(module, '__file__', None)
        if filename is None:
            raise PilotBlocked('candidate_import_identity_mismatch')
        location = candidate.joinpath(*name.split('.'))
        expected = location / '__init__.py' if hasattr(module, '__path__') else location.with_suffix('.py')
        if Path(filename).resolve() != expected:
            raise PilotBlocked('candidate_import_identity_mismatch')
    return __version__, OpenAIAdapter, Budget, MemoryGraph, Runtime, ScratchLog, RankedTopThreeSelector, Store


def metadata(args):
    if not all((args.config, args.channel, args.account, args.inputs, args.approval, args.tariff)):
        raise PilotBlocked('manual_required_metadata_missing')
    bundle = input_contract(read_json(args.inputs))
    if bundle['kind'] != 'approved_frozen':
        raise PilotBlocked('live_frozen_input_approval_required')
    approval = read_json(args.approval)
    if not isinstance(approval, dict):
        raise PilotBlocked('invalid_approval_descriptor')
    tariff = Tariff(**read_json(args.tariff))
    limits = PilotLimits(**approval.get('limits', {}))
    path = Path(args.config).resolve()
    if (approval.get('account_binding_confirmed') is not True
            or approval.get('background_knowledge_disabled') is not True
            or approval.get('delivery_mode') != args.delivery
            or approval.get('account_id') != args.account
            or approval.get('channel_id') != args.channel
            or Path(approval.get('config_path', '')).resolve() != path
            or approval.get('allowed_response_models') != [MODEL]):
        raise PilotBlocked('manual_target_or_account_attestation_missing')
    identity = candidate_identity(args.candidate_root)
    # Full gate validation occurs before any credential lookup; live activation
    # remains an explicit CLI choice rather than a default or implicit fallback.
    return bundle, approval, tariff, limits, identity, path


async def run_live(args):
    from .live_transport import attach_live_transport, preflight_live_transport
    from .manual_runtime import (load_discord_credential, load_openai_credential,
                                prepare_context, run_api_only, run_discord)
    if args.live is not True:
        raise PilotBlocked('explicit_live_required')
    bundle, approval, tariff, limits, identity, config_path = metadata(args)
    version, Adapter, Budget, Graph, Runtime, Scratch, Selector, Store = source_modules(args.candidate_root)
    from indeces.path_policy import validate_managed_path
    output = Path(args.output).resolve()
    if not output.is_relative_to(RUN_ROOT.resolve()) or output == RUN_ROOT.resolve():
        raise PilotBlocked('manual_output_outside_run_root')
    output = validate_managed_path(output, 'manual pilot output')
    if output.exists():
        raise PilotBlocked('manual_output_already_exists')
    context = gate = adapter = scratch = None
    claimed_output = False
    stores = []
    result = {'schema': 'indeces_manual_live_result_v1', 'mode': args.delivery,
              'candidate_version': version, 'source_identity': identity,
              'input_bundle_sha256': canonical_hash(bundle), 'background_workers_started': 0,
              'existing_console_reused': False, 'production_store_opened': False,
              'account_binding': 'operator_attested_not_provider_verified',
              'cost_basis': 'approved_tariff_calculation_not_provider_invoice',
              'held_out_scoring': 'independent_evaluator_only', 'status': 'blocked'}
    try:
        gate = PilotGate(mode='live', tariff=tariff, limits=limits, approval=approval,
                         input_bundle_sha256=canonical_hash(bundle), candidate_identity=identity,
                         live_enabled=True)
        approved_items = {'items': [{'id': row['item_id'], 'text': row['question']}
                                    for row in bundle['questions']]}
        context = prepare_context(config_path, args.channel, args.account, approved_items)
        config = context.config
        if (context.config_path.resolve() != config_path
                or config.discord.guild_id != approval['guild_id']
                or hashlib.sha256(config_path.read_bytes()).hexdigest() != approval['config_source_sha256']
                or config.adapter.model != MODEL
                or config.adapter.base_url.rstrip('/') != 'https://api.openai.com/v1'
                or config.adapter.verbosity != 'high'
                or config.adapter.budgets['reply'].reasoning != 'medium'):
            raise PilotBlocked('manual_loaded_config_mismatch')
        budget = config.adapter.budgets['reply']
        budgets = dict(config.adapter.budgets)
        budgets['reply'] = Budget(min(budget.input_tokens, limits.input_tokens),
                                  min(budget.output_tokens, limits.output_tokens),
                                  min(budget.seconds, limits.call_seconds), reasoning='medium')
        effective = replace(config, state_dir=output / 'isolated-state', scratch_dir=output / 'scratch',
                            adapter=replace(config.adapter, budgets=budgets),
                            discord=replace(config.discord, channel_ids=(args.channel,)))
        descriptor = {'model': MODEL, 'base_url': 'https://api.openai.com/v1',
                      'allowed_response_models': [MODEL], 'descriptor_sha256': canonical_hash(gate.approval),
                      'gate_identity_sha256': gate.ledger['identity_sha256']}
        def readiness():
            if hashlib.sha256(config_path.read_bytes()).hexdigest() != approval['config_source_sha256']:
                raise PilotBlocked('manual_config_changed_while_leased')
            return preflight_live_transport(effective.adapter, gate, explicit_live=True,
                                            validated_descriptor=descriptor)
        context.network_preflight = readiness
        readiness()
        output.mkdir(parents=True, exist_ok=False)
        claimed_output = True
        # These existing helpers are intentionally invoked only here, after
        # approval/tariff/source/target checks and the original state lease.
        key = load_openai_credential(context)
        token = load_discord_credential(context) if args.delivery == 'discord' else None
        scratch = Scratch(output / 'scratch')
        adapter = Adapter(effective.adapter, scratch, api_key=key)
        reply = attach_live_transport(adapter, gate, explicit_live=True, validated_descriptor=descriptor)

        def runtime_factory(item, unused_context):
            store = Store(output / 'isolated-state' / item['id'])
            stores.append(store)
            graph = Graph(store.db, retrieval_policy=effective.runtime.retrieval_policy)
            for material in bundle['materials']:
                graph.add(f'{effective.discord.guild_id}:knowledge', material['source_id'],
                          'approved-frozen-manual-input', [{'text': material['text'], 'quote': material['text'],
                                                           'marks': material['marks']}], 1)
            return Runtime(effective, store, reply, scratch, selector=Selector())

        async def output_answer(item_id, text):
            # Private answers belong to the independent evaluator's run folder,
            # never to the public build/checkpoint artifacts produced by Codex.
            with (output / 'answers.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'item_id': item_id, 'text': text}, ensure_ascii=False) + '\n')
            print(text)

        delivery_seconds = min(10.0, effective.discord.delivery_seconds)
        async with asyncio.timeout(limits.cumulative_seconds):
            if args.delivery == 'discord':
                session = await run_discord(context, runtime_factory, scratch,
                    session_seconds=limits.cumulative_seconds, delivery_seconds=delivery_seconds,
                    token_loader=lambda unused: token)
            else:
                session = await run_api_only(context, runtime_factory,
                    session_seconds=limits.cumulative_seconds, delivery_seconds=delivery_seconds,
                    on_output=output_answer, scratch=scratch)
        result['session'] = session
        if session['stopped'] or len(session['outcomes']) != len(bundle['questions']):
            gate.halt('manual_session_not_complete_no_retry')
        if gate.ledger['stop_code']:
            raise PilotBlocked(gate.ledger['stop_code'])
        result['status'] = 'completed'
    except BaseException as error:
        result['stop_code'] = getattr(error, 'code', 'manual_failed_or_timed_out_no_retry')
        if gate is not None and gate.ledger['stop_code'] is None:
            try:
                gate.halt(result['stop_code'])
            except PilotBlocked:
                pass
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
    finally:
        cleanup_errors = []
        if adapter is not None:
            try:
                async with asyncio.timeout(10):
                    await adapter.close()
            except BaseException:
                cleanup_errors.append('adapter_cleanup_incomplete')
        if scratch is not None:
            try:
                scratch.close()
            except Exception:
                cleanup_errors.append('scratch_cleanup_incomplete')
        for store in stores:
            try:
                store.close()
            except Exception:
                cleanup_errors.append('isolated_store_cleanup_incomplete')
        if context is not None:
            try:
                context.close()
            except Exception:
                cleanup_errors.append('lease_cleanup_incomplete')
        if gate is not None:
            if cleanup_errors and gate.ledger['stop_code'] is None:
                try:
                    gate.halt('manual_cleanup_incomplete_no_retry')
                except PilotBlocked:
                    pass
            if cleanup_errors:
                result['status'] = 'blocked'
                result['cleanup_errors'] = cleanup_errors
            result.update(ledger_path=str(gate.path), request_count=len(gate.ledger['requests']),
                          stop_code=gate.ledger['stop_code'], ledger_identity=gate.ledger['identity_sha256'])
            gate.close()
        if claimed_output:
            (output / 'RESULT.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    modes = value.add_mutually_exclusive_group()
    modes.add_argument('--mock', action='store_true')
    modes.add_argument('--live', action='store_true')
    value.add_argument('--delivery', choices=('api', 'discord'), default='discord')
    value.add_argument('--candidate-root', type=Path, default=SOURCE_ROOT)
    value.add_argument('--output', type=Path)
    value.add_argument('--config', type=Path)
    value.add_argument('--channel')
    value.add_argument('--account')
    value.add_argument('--inputs', type=Path)
    value.add_argument('--approval', type=Path)
    value.add_argument('--tariff', type=Path)
    value.add_argument('--describe-input', action='store_true')
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    if args.output is None:
        args.output = RUN_ROOT / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    try:
        if args.describe_input:
            if args.inputs is None:
                raise PilotBlocked('inputs_required')
            bundle = input_contract(read_json(args.inputs))
            print(json.dumps({'input_bundle_sha256': canonical_hash(bundle),
                              'candidate_identity': candidate_identity(args.candidate_root),
                              'question_count': len(bundle['questions']), 'material_count': len(bundle['materials']),
                              'proposed_limits': asdict(PilotLimits()), 'real_requests': 0}))
            return 0
        if args.live:
            result = asyncio.run(run_live(args))
            print(json.dumps({key: result[key] for key in ('status', 'mode', 'source_identity', 'cost_basis')
                              if key in result} | {'stop_code': result.get('stop_code')}))
            return 0 if result['status'] == 'completed' else 2
        bundle = input_contract(read_json(args.inputs)) if args.inputs else None
        result = asyncio.run(run_mock(args.output, bundle, candidate_root=args.candidate_root))
        print(json.dumps({'mode': 'mock', 'real_requests': 0,
                          'acceptance_passed': result['acceptance_passed'], 'result': str(args.output / 'RESULT.json')}))
        return 0 if result['acceptance_passed'] else 2
    except (PilotBlocked, ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({'status': 'blocked', 'code': getattr(error, 'code', 'invalid_manual_preflight'),
                          'real_requests': 0}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
