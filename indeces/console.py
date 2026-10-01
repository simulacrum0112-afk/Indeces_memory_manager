from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys

from . import __version__
from .adapter import OpenAIAdapter
from .api_key_wizard import configure_api_key
from .config import PdfConfig, load_config
from .credentials import (CredentialError, load_discord_token, load_openai_key,
                          openai_secret_path, prompt_secret, secret_path,
                          validate_api_key, validate_token)
from .discord_bridge import DiscordBridge
from .discord_wizard import configure_discord
from .knowledge import KnowledgeService
from .knowledge_directory import prepare_knowledge_directory, show_knowledge_directory
from .knowledge_progress import show_knowledge_progress
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


async def serve(config, key, token, *, lease=None):
    if not config.discord.guild_id:
        raise ValueError("set discord.guild_id before starting")
    owns_lease = lease is None
    if owns_lease:
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
        try:
            prepare_knowledge_directory(config)
        except Exception as error:
            print(f"Knowledge directory guide unavailable: {type(error).__name__}; source ingestion still uses the configured directory.")
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
        print("Knowledge PDF updates trigger local Markdown conversion, then background labels. Use Indeces-Knowledge.cmd or knowledge in a separate terminal for progress. Chat labelling is closed. Ctrl+C stops the service.")
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
        if owns_lease:
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
    print(f"Created {path}. Use discord for the bridge and apikey for OpenAI credentials, then start.")


def status(config, config_path=None):
    if config_path is not None:
        config_path = Path(config_path).resolve()
    print(f"{config.name} {__version__}; model {config.adapter.model}; one model request slot")
    print(f"Discord guild={config.discord.guild_id or '<not configured>'}; channels={config.discord.channel_ids or 'all explicitly mentioned channels'}")
    print(f"Knowledge: {config.knowledge_dir}; scratch: {config.scratch_dir}")
    print(f"Active supported-file limit: {config.knowledge.max_files}; root _staging is storage only. Use knowledge to open the material directory.")
    print("Read-only graph/trace page: observe; human-readable scratch directory guide: logs.")
    print("Scratch retention: rolling 24 hours; startup cleanup and every 60 seconds while the service runs. Stopped services do not clean logs.")
    if config_path is not None:
        print(f"Saved Discord credential file: {'present (checked at start)' if secret_path(config_path).exists() else 'absent'}")
        print(f"Saved OpenAI credential file: {'present (checked at start)' if openai_secret_path(config_path).exists() else 'absent'}")
    print(f"Model output verbosity: {config.adapter.verbosity}")
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
    if config_path is not None:
        # Match the canonical location used by both setup wizards, including
        # when --config names a symlink or a parent path alias.
        config_path = Path(config_path).resolve()
    # The lease covers hidden credential input as well as the async service,
    # preventing setup in another Console from changing its configuration.
    try:
        lease = InstanceLock(config.state_dir)
    except RuntimeError:
        print("Startup unavailable: another instance or setup owns this state directory; no connection was made.")
        raise
    try:
        _run_with_lease(config, config_path, lease)
    finally:
        lease.close()


def _check_start_config(config, config_path):
    if config_path is not None and load_config(config_path) != config:
        print("Startup cancelled: configuration changed; start again. No connection was made.")
        raise RuntimeError("configuration changed during startup; start again")


def _run_with_lease(config, config_path, lease):
    _check_start_config(config, config_path)
    try:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token and config_path is not None:
            token = load_discord_token(config_path, config.discord.guild_id)
        token = validate_token(token or prompt_secret("Discord bot token (session only, not saved): "))
        key = os.environ.get("OPENAI_API_KEY")
        if not key and config_path is not None:
            key = load_openai_key(config_path)
        key = validate_api_key(key or prompt_secret("OpenAI API key (session only, not saved; use apikey to save): "))
    except (EOFError, KeyboardInterrupt):
        print("Startup cancelled; no connection was made.")
        return
    _check_start_config(config, config_path)
    try:
        asyncio.run(serve(config, key, token, lease=lease))
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


def knowledge(config, *, once=False):
    show_knowledge_directory(config)
    show_knowledge_progress(config, watch=not once)


def open_command_window(command, config_path):
    """Open a user-requested long-lived command without blocking Console input."""
    labels = {"start": "服务入口", "observe": "只读观察", "knowledge": "知识库进度"}
    if command not in labels:
        raise ValueError("unsupported independent command")
    label = labels[command]
    path = Path(config_path).resolve()
    code_root = Path(__file__).resolve().parent.parent
    arguments = [sys.executable, "-m", "indeces", command, "--config", str(path)]
    if os.name != "nt":
        print(f"请在另一个终端运行{label}：")
        print("cd -- " + shlex.quote(str(code_root)))
        print(shlex.join(arguments))
        return
    # Keep exit/error receipts visible, including duplicate-instance failures.
    # Credentials remain hidden stdin in the child; never put them in argv.
    arguments.append("--keep-window")
    try:
        subprocess.Popen(arguments, cwd=code_root,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as error:
        print(f"{label}窗口未打开：{type(error).__name__}；请另开终端运行 {command}。")
        return
    if command == "start":
        print("服务入口已在独立窗口打开；请在该窗口查看启动结果。Ctrl+C 在服务窗口停止服务；主 Console 的 quit 不停止服务。")
    else:
        print(f"{label}已在独立窗口打开；关闭该窗口不会停止服务。")


def open_knowledge_window(config_path):
    open_command_window("knowledge", config_path)


def _wait_for_window_close():
    try:
        input("此入口已结束；按 Enter 关闭窗口。")
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    parser = argparse.ArgumentParser(description="Indeces: minimal Discord runtime console")
    parser.add_argument("command", nargs="?", choices=["console", "init", "discord", "apikey", "knowledge", "start", "status", "check", "scratch", "observe", "logs"], default="console")
    parser.add_argument("--config", type=Path, default=Path("config.local.toml"))
    parser.add_argument("--once", action="store_true", help="knowledge: show one read-only progress snapshot and exit")
    parser.add_argument("--keep-window", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.once and args.command != "knowledge":
        parser.error("--once is only supported by knowledge")
    if args.keep_window and args.command not in {"start", "observe", "knowledge"}:
        parser.error("--keep-window requires start, observe or knowledge")
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
        if args.command in {"discord", "apikey"}:
            if not {"discord": configure_discord, "apikey": configure_api_key}[args.command](args.config):
                raise SystemExit(2)
            return
        if args.command != "console":
            config = load_config(args.config)
            if args.command in {"start", "status"}:
                {"start": run, "status": status}[args.command](config, args.config)
            elif args.command == "knowledge":
                knowledge(config, once=args.once)
            elif args.command in {"observe", "logs"}:
                {"observe": observe, "logs": show_logs}[args.command](config)
            else:
                check_scratch(config)
            return
        print(f"Indeces {__version__} console: discord | apikey | knowledge | start | status | scratch | observe | logs | quit")
        print("start、observe、knowledge 使用独立窗口/终端。主 Console 的 quit 只退出命令窗口；服务窗口内 Ctrl+C 停止服务。")
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
                if command in {"discord", "apikey"}:
                    {"discord": configure_discord, "apikey": configure_api_key}[command](args.config)
                    continue
                if command == "knowledge":
                    open_knowledge_window(args.config)
                    continue
                if command in {"start", "observe"}:
                    open_command_window(command, args.config)
                    continue
                config = load_config(args.config)
                action = {"status": status, "scratch": check_scratch,
                          "logs": show_logs}.get(command)
                if action:
                    if command == "status":
                        action(config, args.config)
                    else:
                        action(config)
                else:
                    print("Commands: discord | apikey | knowledge | start | status | scratch | observe | logs | quit")
            except CredentialError as error:
                print(f"Credential error: {error.code}. Use discord setup for the Bot token, or apikey for the OpenAI key.")
            except Exception as error:
                # Never print arbitrary authentication transport exceptions.
                print(f"Command failed: {type(error).__name__}. Check configuration and scratch records.")
    except CredentialError as error:
        print(f"Credential error: {error.code}. Use discord setup for the Bot token, or apikey for the OpenAI key, before starting.")
        raise SystemExit(2) from None
    except Exception as error:
        print(f"Startup failed: {type(error).__name__}. Check configuration and local file permissions.")
        raise SystemExit(2) from None
    finally:
        if args.keep_window:
            _wait_for_window_close()
