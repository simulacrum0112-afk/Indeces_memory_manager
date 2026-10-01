from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from . import __version__
from .adapter import OpenAIAdapter
from .config import PdfConfig, load_config
from .credentials import CredentialError, load_discord_token, prompt_secret, secret_path, validate_token
from .discord_bridge import DiscordBridge
from .discord_wizard import configure_discord
from .knowledge import KnowledgeService
from .lock import InstanceLock
from .observer import ObserverServer, observe, prepare_scratch_directory, show_logs
from .runtime import Runtime
from .run_records import verify_runs
from .scratch import ScratchLog, retention_checkpoint, verify
from .store import Store


RETENTION_SECONDS = 24 * 60 * 60
RETENTION_MAINTENANCE_SECONDS = 60.0


def _retention_receipt(scratch, summary, *, phase):
    if not isinstance(summary, dict):
        return
    temporary_removed = summary.get("temporary_files_removed", 0)
    if not (summary["files_changed"] or summary["files_removed"] or temporary_removed):
        return
    print(f"Scratch retention ({phase}): removed {summary['records_removed']} records and "
          f"{summary['files_removed']} files and {temporary_removed} temporary files; "
          f"cutoff={summary['cutoff']}", flush=True)
    scratch.write("scratch_retention", phase=phase, summary=summary)


async def _maintain_scratch(scratch):
    while True:
        await asyncio.sleep(RETENTION_MAINTENANCE_SECONDS)
        try:
            _retention_receipt(scratch, scratch.prune(), phase="maintenance")
        except Exception as error:
            # Private file contents and arbitrary I/O messages never reach Console.
            print(f"Scratch retention failed: {type(error).__name__}; service stopped.", flush=True)
            raise


async def serve(config, key, token):
    if not config.discord.guild_id:
        raise ValueError("set discord.guild_id before starting")
    lease = InstanceLock(config.state_dir)
    scratch = store = adapter = bridge = knowledge = observer = None
    maintenance_task = gateway_task = None
    try:
        try:
            scratch = ScratchLog(config.scratch_dir)
        except Exception as error:
            print(f"Scratch retention/startup failed: {type(error).__name__}; service stopped.", flush=True)
            raise
        _retention_receipt(scratch, getattr(scratch, "startup_retention", None), phase="startup")
        store = Store(config.state_dir)
        interrupted = store.recover()
        if interrupted:
            scratch.write("restart_recovery", interrupted_turns=interrupted, automatically_replayed=False)
        adapter = OpenAIAdapter(config.adapter, scratch, key)
        runtime = Runtime(config, store, adapter, scratch)
        knowledge = KnowledgeService(config, store, runtime.graph, adapter, scratch)
        bridge = DiscordBridge(config, runtime, scratch)
        scratch.write("service_start", version=__version__, guild_id=config.discord.guild_id,
                      stage_budgets={k: asdict(v) for k, v in config.adapter.budgets.items()},
                      knowledge_limits=asdict(config.knowledge), model_concurrency=1, chat_labelling=False,
                      pdf_limits=asdict(getattr(config, "pdf", PdfConfig())), pdf_model_calls=0,
                      scratch_retention_seconds=RETENTION_SECONDS,
                      scratch_maintenance_seconds=RETENTION_MAINTENANCE_SECONDS)
        print(f"{config.name} {__version__}: one Discord connection; model={config.adapter.model}; scratch={scratch.path}")
        print("Knowledge PDF updates trigger local Markdown conversion, then background labels. Chat labelling is closed. Ctrl+C stops the service.")
        print("PDF text extraction has physical page provenance; OCR is closed and academic layout is not verified.")
        print("Scratch retains a rolling 24-hour window; cleanup runs at startup and every 60 seconds while the service runs.")
        try:
            prepare_scratch_directory(config.scratch_dir)
            observer = ObserverServer(config)
            print(f"Indeces read-only observer: {observer.start()}", flush=True)
        except Exception as error:
            if observer is not None:
                try:
                    observer.close()
                except Exception:
                    pass
            observer = None
            print(f"Observer unavailable: {type(error).__name__}; use observe in a separate console.", flush=True)
        knowledge.start()
        maintenance_task = asyncio.create_task(_maintain_scratch(scratch), name="indeces-scratch-retention")
        gateway_task = asyncio.create_task(bridge.run(token), name="indeces-discord-gateway")
        done, _ = await asyncio.wait({maintenance_task, gateway_task}, return_when=asyncio.FIRST_COMPLETED)
        if maintenance_task in done:
            await maintenance_task
            print("Scratch retention failed: RuntimeError; service stopped.", flush=True)
            raise RuntimeError("scratch retention task stopped unexpectedly")
        await gateway_task
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_errors = []
        # Await both users of scratch before closing resources or its append stream.
        for task in (maintenance_task, gateway_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (maintenance_task, gateway_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except BaseException as error:
                    if not active_error:
                        cleanup_errors.append(error)
        if observer is not None:
            try:
                # Its read-only SQLite/file work has a separate thread and lifetime.
                await asyncio.to_thread(observer.close)
            except BaseException as error:
                cleanup_errors.append(error)
        for resource in (bridge, knowledge, adapter):
            if resource is not None:
                try:
                    await resource.close()
                except BaseException as error:
                    cleanup_errors.append(error)
        if scratch is not None:
            try:
                scratch.write("service_stop")
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                scratch.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if store is not None:
            try:
                store.close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            lease.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            print("Service cleanup encountered: " + ", ".join(type(e).__name__ for e in cleanup_errors), flush=True)
            if not active_error:
                raise cleanup_errors[0]


def initialize(path):
    if path.exists():
        print(f"Configuration already exists: {path}")
        return
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    if not example.exists():
        raise FileNotFoundError("use config.example.toml from the repository checkout")
    shutil.copyfile(example, path)
    print(f"Created {path}. Use discord to configure the bridge, then start.")


def status(config, config_path=None):
    print(f"{config.name} {__version__}; model {config.adapter.model}; one model request slot")
    print(f"Discord guild={config.discord.guild_id or '<not configured>'}; channels={config.discord.channel_ids or 'all explicitly mentioned channels'}")
    print(f"Knowledge: {config.knowledge_dir}; scratch: {config.scratch_dir}")
    print("Read-only graph/trace page: observe; human-readable scratch directory guide: logs.")
    print("Scratch retention: rolling 24 hours; startup cleanup and every 60 seconds while the service runs. Stopped services do not clean logs.")
    if config_path is not None:
        print(f"Saved Discord credential file: {'present (checked at start)' if secret_path(config_path).exists() else 'absent'}")
    print("Chat labelling: CLOSED; passive knowledge-update labelling: ENABLED while service runs")
    pdf = getattr(config, "pdf", PdfConfig())
    print(f"PDF -> Markdown: local worker; bytes={pdf.max_file_bytes}; pages={pdf.max_pages}; "
          f"Markdown bytes={pdf.max_markdown_bytes}; seconds={pdf.seconds}; model calls=0; OCR=CLOSED")
    print(f"Converted Markdown review files: {config.state_dir / 'pdf_markdown'}")
    for stage, budget in config.adapter.budgets.items():
        print(f"  {stage}: in={budget.input_tokens}, out={budget.output_tokens}, seconds={budget.seconds}, effort={budget.reasoning}")
    database = config.state_dir / "memory.sqlite3"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "knowledge_versions" in tables:
                print("Knowledge versions:", dict(db.execute("SELECT status,count(*) FROM knowledge_versions GROUP BY status")))
            if "turns" in tables:
                print("Turns:", dict(db.execute("SELECT status,count(*) FROM turns GROUP BY status")))
    print("This is configuration/storage status, not proof of a live Gateway connection.")


def run(config, config_path=None):
    if not config.discord.guild_id:
        raise ValueError("use discord setup to configure the Guild ID first")
    try:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token and config_path is not None:
            token = load_discord_token(config_path, config.discord.guild_id)
        token = validate_token(token or prompt_secret("Discord bot token (session only, not saved): "))
        key = os.environ.get("OPENAI_API_KEY") or prompt_secret("OpenAI API key (not saved): ")
    except (EOFError, KeyboardInterrupt):
        print("Startup cancelled; no connection was made.")
        return
    if not key.strip() or not token.strip():
        raise ValueError("both credentials are required")
    try:
        asyncio.run(serve(config, key, token))
    except KeyboardInterrupt:
        print(f"{config.name} stopped.")


def check_scratch(config):
    files = sorted(config.scratch_dir.glob("*.jsonl"))
    for path in files:
        count, digest = verify(path)
        print(f"{path.name}: {count} records, hash chain OK; head={digest}")
        checkpoint = retention_checkpoint(path)
        if checkpoint is not None:
            print(f"  retained window: cutoff={checkpoint['cutoff']}; pruned_at={checkpoint['pruned_at']}; "
                  f"removed_through_sequence={checkpoint['removed_through_sequence']}; "
                  f"removed_head_hash={checkpoint['removed_head_hash']}; "
                  f"cleanup_pending={checkpoint.get('cleanup_pending', False)}")
    if not files:
        print("No runtime scratch logs yet.")
        return
    report = verify_runs(files)
    print("Run record contracts:", report["counts"])
    print("Adaptor call records:", report["call_counts"])
    for call in report["calls"]:
        if call["status"] != "complete":
            print(f"  call={call['call_id']}; trace={call['trace_id']}: {call['status']}")
    for turn in report["turns"]:
        if turn["status"] != "complete" or turn["warnings"]:
            print(f"  trace={turn['trace_id']}: {turn['status']}; warnings={','.join(turn['warnings']) or 'none'}")
    for issue in report["issues"]:
        print(f"  trace={issue['trace_id']}: {issue['reason']}")
    print("Checks cover frozen evidence, weight arithmetic and literal citation links; semantic support is not evaluated. Local hashes have no external anchor.")
    if report["issues"]:
        raise ValueError("run record contract verification failed")


def main():
    parser = argparse.ArgumentParser(description="Indeces: minimal Discord runtime console")
    parser.add_argument("command", nargs="?", choices=["console", "init", "discord", "start", "status", "check", "scratch", "observe", "logs"], default="console")
    parser.add_argument("--config", type=Path, default=Path("config.local.toml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        if args.command == "init":
            initialize(args.config)
            return
        if args.command == "check":
            path = args.config if args.config.exists() else Path(__file__).resolve().parent.parent / "config.example.toml"
            config = load_config(path)
            import discord
            import pypdf
            from .pdf_import import EXTRACTOR_VERSION
            if pypdf.__version__ != EXTRACTOR_VERSION:
                raise ValueError("PDF extractor version mismatch; reinstall requirements.lock")
            print(f"Offline configuration/import check OK; {config.name} {__version__}; "
                  f"discord.py {discord.__version__}; pypdf {pypdf.__version__}.")
            print("Live Discord and model calls were not made.")
            return
        if not args.config.exists():
            initialize(args.config)
        if args.command == "discord":
            if not configure_discord(args.config):
                raise SystemExit(2)
            return
        if args.command != "console":
            config = load_config(args.config)
            if args.command in {"start", "status"}:
                {"start": run, "status": status}[args.command](config, args.config)
            elif args.command in {"observe", "logs"}:
                {"observe": observe, "logs": show_logs}[args.command](config)
            else:
                check_scratch(config)
            return
        print(f"Indeces {__version__} console: discord | start | status | scratch | observe | logs | quit")
        while True:
            try:
                command = input("Indeces> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if command in {"quit", "exit"}:
                break
            if not command:
                continue
            try:
                if command == "discord":
                    configure_discord(args.config)
                    continue
                config = load_config(args.config)
                action = {"start": run, "status": status, "scratch": check_scratch,
                          "observe": observe, "logs": show_logs}.get(command)
                if action:
                    if command in {"start", "status"}:
                        action(config, args.config)
                    else:
                        action(config)
                else:
                    print("Commands: discord | start | status | scratch | observe | logs | quit")
            except CredentialError as error:
                print(f"Credential error: {error.code}. Use discord setup to replace the saved token.")
            except Exception as error:
                # Never print arbitrary authentication transport exceptions.
                print(f"Command failed: {type(error).__name__}. Check configuration and scratch records.")
    except CredentialError as error:
        print(f"Credential error: {error.code}. Use discord setup before starting.")
        raise SystemExit(2) from None
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Startup failed: {type(error).__name__}. Check configuration and local file permissions.")
        raise SystemExit(2) from None
