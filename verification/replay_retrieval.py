"""Explicit diagnostic replay from retained local scratch; never Discord.

Reads only requested traces. Acquires the state lease before derived migration,
retrieval audit writes and optional single-call reply trials. Writes private
evidence only to the existing managed scratch; creates no private export.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indeces.adapter import OpenAIAdapter
from indeces.config import load_config
from indeces.context import reply_messages
from indeces.contracts import IncomingMessage
from indeces.lock import InstanceLock
from indeces.memory import MemoryGraph
from indeces.path_policy import validate_managed_path, validate_runtime_paths
from indeces.prompts import reply_instructions
from indeces.run_records import answer_record, digest, freeze_retrieval, validate_graph_audit
from indeces.scratch import ScratchLog, read_snapshot
from indeces.credentials import load_openai_key, validate_api_key


async def replay(config_path, trace_ids, call_model=False):
    cfg = load_config(config_path)
    validate_runtime_paths(cfg)
    lease = InstanceLock(cfg.state_dir)
    db = adapter = scratch = None
    try:
        if load_config(config_path) != cfg:
            raise ValueError('configuration changed before diagnostic lease')
        retained = {}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for path in sorted(cfg.scratch_dir.glob('????-??-??.jsonl')):
            for record in read_snapshot(path, max_bytes=128 * 1024 * 1024)['records']:
                trace = record['fields'].get('trace_id')
                if trace in trace_ids and datetime.fromisoformat(record['timestamp']) >= cutoff:
                    retained.setdefault(trace, []).append(record)
        ordered = sorted(trace_ids, key=lambda trace: retained[trace][0]['timestamp'])
        db_path = validate_managed_path(cfg.state_dir / 'memory.sqlite3', 'diagnostic database')
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        graph = MemoryGraph(db, self_marks=(cfg.name,), retrieval_policy=cfg.runtime.retrieval_policy)
        if call_model:
            scratch = ScratchLog(cfg.scratch_dir)
            key = os.environ.get('OPENAI_API_KEY', '').strip() or load_openai_key(config_path)
            adapter = OpenAIAdapter(cfg.adapter, scratch, validate_api_key(key))
        previous_query = ''
        for old_trace in ordered:
            rows = retained[old_trace]
            start = next(r['fields'] for r in rows if r['event'] == 'turn_start')
            old = next(r['fields']['record'] for r in rows if r['event'] == 'retrieval_record')
            validate_graph_audit(old['graph_audit'], old)
            trace = 'retrieval-replay-' + uuid.uuid4().hex
            event_id = 'diagnostic:' + uuid.uuid4().hex
            audit = {}
            started = time.perf_counter()
            selected = graph.retrieve(old['scope'], [], old['query'], time.time(), event_id=event_id,
                                      audit=audit, context_query=previous_query)
            frozen = freeze_retrieval(db, old['scope'], event_id, old['query'], selected, audit)
            validate_graph_audit(frozen['graph_audit'], frozen)
            elapsed = time.perf_counter() - started
            previous_query = old['query']
            before = {c['record_id']: c['rank'] for c in old['graph_audit']['selection']['ranked_candidates']}
            after = {c['record_id']: c['rank'] for c in audit['selection']['ranked_candidates']}
            comparison = {'original_trace': old_trace, 'diagnostic_trace': trace,
                          'original_retrieval_sha256': digest(old), 'new_retrieval_sha256': digest(frozen),
                          'selected_ids': [r['id'] for r in selected], 'local_seconds': elapsed,
                          'rank_before': before, 'rank_after': after}
            if not call_model:
                print(json.dumps(comparison, ensure_ascii=False), flush=True)
                continue
            scratch.write('diagnostic_trial_start', trace_id=trace, kind='retrieval_reply',
                          operator_approved=True, automatic_retries=0, checkpoint_update=False,
                          model=cfg.adapter.model, original_trace=old_trace)
            scratch.write('diagnostic_retrieval_comparison', trace_id=trace, comparison=comparison)
            scratch.write('memory_observation', trace_id=trace, audit=audit, audit_sha256=digest(audit))
            scratch.write('retrieval_record', trace_id=trace, record=frozen, record_sha256=digest(frozen))
            current = IncomingMessage(event_id, 'diagnostic', cfg.discord.guild_id,
                                      start['input']['author_id'], 'Diagnostic replay', old['query'],
                                      datetime.now(timezone.utc).isoformat())
            messages = reply_messages('', [], frozen['model_materials'], current)
            instructions = reply_instructions(cfg.name)
            scratch.write('reply_context', trace_id=trace, retrieval_sha256=digest(frozen),
                          messages=messages, instructions=instructions)
            try:
                result = await adapter.call('reply', instructions, messages, trace)
                receipt = answer_record(result.text, frozen)
                scratch.write('answer_generated', trace_id=trace, retrieval_sha256=digest(frozen),
                              record=receipt, model_result=asdict(result))
                scratch.write('diagnostic_trial_end', trace_id=trace, status='completed',
                              checkpoint_updated=False, input_tokens=result.input_tokens,
                              output_tokens=result.output_tokens)
                print(json.dumps({'original_trace': old_trace, 'diagnostic_trace': trace,
                      'selected_ids': comparison['selected_ids'], 'local_seconds': elapsed,
                      'reply_seconds': result.elapsed_seconds, 'answer': result.text,
                      'unresolved_markers': receipt['unresolved_markers']}, ensure_ascii=False), flush=True)
            except BaseException:
                scratch.write('diagnostic_trial_end', trace_id=trace, status='failed', checkpoint_updated=False)
                raise  # Stop the diagnostic batch; no automatic replay.
    finally:
        if adapter:
            await adapter.close()
        if scratch:
            scratch.close()
        if db:
            db.close()
        lease.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--trace-id', action='append', required=True)
    parser.add_argument('--call-model', action='store_true', help='Explicitly authorize one reply per requested trace, without retries')
    args = parser.parse_args()
    asyncio.run(replay(args.config, args.trace_id, args.call_model))


if __name__ == '__main__':
    main()
