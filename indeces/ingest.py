"""Explicit failed-only maintenance, using the existing governed ingestion path.

No Discord connection or new source discovery. Successful versions and known
per-version accounting remain unchanged. Console runs this as an owned task.
"""
from __future__ import annotations

import asyncio
import os
import sys

from .adapter import OpenAIAdapter
from .credentials import load_openai_key, validate_api_key
from .knowledge import KnowledgeService
from .lock import InstanceLock
from .memory import MemoryGraph
from .path_policy import validate_runtime_paths
from .scratch import ScratchLog
from .store import Store


async def retry_ingestion(config, config_path, *, paths=None, key=None, lease=None, knowledge_ready=None):
    validate_runtime_paths(config)
    if not config.discord.guild_id:
        raise ValueError("configure a Guild before retrying its knowledge")
    if paths is not None and (not isinstance(paths, (list, tuple)) or
                              any(not isinstance(path, str) or not path for path in paths)):
        raise ValueError("paths must be relative source paths")
    owns_lease = lease is None
    if owns_lease:
        lease = InstanceLock(config.state_dir)
    store = scratch = adapter = knowledge = None
    try:
        # Verify config while owning the same lease used by service/wizards.
        from .config import load_config
        if load_config(config_path) != config:
            raise ValueError("configuration changed before maintenance acquired its lease")
        if key is None:
            key = os.environ.get("OPENAI_API_KEY", "").strip() or load_openai_key(config_path)
        if not key:
            raise ValueError("OpenAI key unavailable; use apikey or OPENAI_API_KEY")
        key = validate_api_key(key)
        store = Store(config.state_dir)
        graph = MemoryGraph(store.db)
        scratch = ScratchLog(config.scratch_dir)
        adapter = OpenAIAdapter(config.adapter, scratch, key)
        knowledge = KnowledgeService(config, store, graph, adapter, scratch)
        if knowledge_ready is not None:
            knowledge_ready(knowledge)
        requested = list(dict.fromkeys(paths)) if paths is not None else [None]
        receipt = {"queued": [], "blocked": [], "unchanged": []}
        for path in requested:
            response = knowledge.retry_failed(path, include_incomplete=True)
            for kind in receipt:
                receipt[kind].extend(response[kind])
        target_ids = {row["source_id"] for row in receipt["queued"]}
        # Explicit failed-only retry leaves unrelated ready/pending versions
        # alone. No watcher, so adding files never expands this maintenance job.
        scratch.write("knowledge_retry_maintenance_start", selected_source_ids=sorted(target_ids),
                      queued=len(receipt["queued"]), blocked=len(receipt["blocked"]), discord_started=False)
        print(f"Maintenance retry: queued={len(receipt['queued'])}; blocked={len(receipt['blocked'])}; no Discord connection.", flush=True)
        while target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            rows = store.db.execute(f"SELECT source_id,status FROM knowledge_versions WHERE source_id IN ({placeholders})", tuple(target_ids)).fetchall()
            pending = {row["source_id"] for row in rows if row["status"] in {"pending", "converting"}}
            if not pending:
                break
            # Existing selectors process a single earliest desired job. Provide
            # an explicit selection to keep unrelated pending input untouched.
            converted = await knowledge.convert_next(source_ids=pending)
            labelled = await knowledge.label_next(source_ids=pending)
            if not converted and not labelled:
                raise RuntimeError("selected ingestion jobs made no progress")
            await asyncio.sleep(0)
        receipt["final"] = []
        for source_id in sorted(target_ids):
            row = store.db.execute("""SELECT path,source_id,status,error,input_tokens,output_tokens,elapsed_seconds,
                (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.source_id=v.source_id) AS total_chunks,
                (SELECT COUNT(marks_json) FROM knowledge_chunks c WHERE c.source_id=v.source_id) AS labelled_chunks
                FROM knowledge_versions v WHERE source_id=?""", (source_id,)).fetchone()
            if row is not None:
                receipt["final"].append(dict(row))
        scratch.write("knowledge_retry_maintenance_end", final=receipt["final"], discord_started=False)
        return receipt
    finally:
        # Keep the lease through HTTP cleanup and durable failure accounting.
        active_error = sys.exc_info()[0] is not None
        cleanup_errors = []
        async def cleanup():
            for resource in (knowledge, adapter):
                if resource is not None:
                    try:
                        await resource.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
            for resource in (scratch, store, lease if owns_lease else None):
                if resource is not None:
                    try:
                        resource.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
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
        if cleanup_errors and not active_error:
            raise cleanup_errors[0]
