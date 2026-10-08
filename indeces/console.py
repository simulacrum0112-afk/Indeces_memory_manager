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
from .path_policy import PathPolicyError, validate_config_path, validate_runtime_paths, validate_managed_path
from .lock import InstanceLock
from .observer import ObserverServer, observe, prepare_scratch_directory, show_logs
from .observer_data import npmi_diagnostics
from .runtime import Runtime
from .run_records import verify_runs
from .scratch import ScratchLog, retention_checkpoint, verify
from .store import Store
from .console_instance import COMMANDS as CONSOLE_COMMANDS, route_existing


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


def create_runtime(config, store, adapter, scratch):
    """Construct the configured model or existing deterministic reply path."""
    selector = None
    if config.runtime.model_selection_enabled:
        from .model_selection import ModelNPMILabelSelector
        selector = ModelNPMILabelSelector(adapter, scratch, config.adapter.budgets["selection"])
    return Runtime(config, store, adapter, scratch, selector=selector)


async def serve(config, key, token, *, lease=None, knowledge_ready=None):
    validate_runtime_paths(config)
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
        except ValueError:
            # A rejected path is a startup failure, never permission to ingest it.
            raise
        except Exception as error:
            print(f"Knowledge directory guide unavailable: {type(error).__name__}; source ingestion still uses the configured directory.")
        store = Store(config.state_dir)
        interrupted = store.recover()
        if interrupted:
            scratch.write("restart_recovery", interrupted_turns=interrupted, automatically_replayed=False)
        adapter = OpenAIAdapter(config.adapter, scratch, key)
        runtime = create_runtime(config, store, adapter, scratch)
        selection_enabled = (config.runtime.model_selection_enabled
                             and getattr(runtime, "model_selector", None) is not None)
        knowledge = KnowledgeService(config, store, runtime.graph, adapter, scratch)
        if knowledge_ready is not None:
            knowledge_ready(knowledge)
        bridge = DiscordBridge(config, runtime, scratch)
        scratch.write("service_start", version=__version__, guild_id=config.discord.guild_id,
                      stage_budgets={k: asdict(v) for k, v in config.adapter.budgets.items()},
                      knowledge_limits=asdict(config.knowledge), model_concurrency=1, chat_labelling=False,
                      evidence_selection=("model_npmi_labels_v1" if selection_enabled
                                          else config.runtime.retrieval_policy),
                      model_selection_enabled=selection_enabled,
                      pdf_limits=asdict(getattr(config, "pdf", PdfConfig())), pdf_model_calls=0,
                      scratch_retention_seconds=RETENTION_SECONDS,
                      scratch_maintenance_seconds=RETENTION_MAINTENANCE_SECONDS)
        print(f"{config.name} {__version__}: one Discord connection; model={config.adapter.model}; scratch={scratch.path}")
        print("Knowledge PDF updates trigger local Markdown conversion, then background labels. Use the knowledge panel for progress. Chat labelling is closed. stop/Ctrl+C stops the owned service.")
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
            print(f"Observer unavailable: {type(error).__name__}; use the observe panel.", flush=True)
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
    path = validate_config_path(path)
    if path.exists():
        print(f"Configuration already exists: {path}")
        return
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    if not example.exists():
        raise FileNotFoundError("use config.example.toml from the repository checkout")
    shutil.copyfile(example, path)
    print(f"Created {path}. Use discord for the bridge and apikey for OpenAI credentials, then start.")


def status(config, config_path=None):
    validate_runtime_paths(config)
    if config_path is not None:
        config_path = validate_config_path(config_path)
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
    validate_managed_path(database, "status database")
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "knowledge_versions" in tables:
                print("Knowledge versions:", dict(db.execute("SELECT status,count(*) FROM knowledge_versions GROUP BY status")))
            if "turns" in tables:
                print("Turns:", dict(db.execute("SELECT status,count(*) FROM turns GROUP BY status")))
    print("This is configuration/storage status, not proof of a live Gateway connection.")


def run(config, config_path=None):
    validate_runtime_paths(config)
    if not config.discord.guild_id:
        raise ValueError("use discord setup to configure the Guild ID first")
    if config_path is not None:
        # Match the canonical location used by both setup wizards, including
        # when --config names a symlink or a parent path alias.
        config_path = validate_config_path(config_path)
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
    credentials = _startup_credentials(config, config_path)
    if credentials is None:
        return
    key, token = credentials
    _check_start_config(config, config_path)
    try:
        asyncio.run(serve(config, key, token, lease=lease))
    except KeyboardInterrupt:
        print(f"{config.name} stopped.")


def _startup_credentials(config, config_path):
    """Called on the input-owning main thread while the runtime lease is held."""
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
        return None
    return key, token


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


def npmi(config):
    """Inspect the saved static corpus without touching model or scratch slots."""
    report = npmi_diagnostics(config)
    print("静态 NPMI：当前回复按静态边扩展和排序；动态权重仅作观察历史。")
    stats = report["statistics"]
    if stats is None:
        if "graph_not_initialized" in report["warnings"]:
            print("尚无可读取的知识图；材料完成发布后才进入统计。")
        else:
            print("只读快照暂不可用或查询达到限额；本次无法判断图状态。")
        return
    print(f"完整范围：{stats['active_records']} 个有效块；{stats['active_source_versions']} 个来源版本；"
          f"{stats.get('annotation_occurrences', '未统计')} 次标注出现；{stats['active_marks']} 个去重词节点；"
          f"{stats['positive_edges']} 条正 NPMI 边。")
    print(f"共现词对：{stats['supported_pairs']}；未保存可用正权重的词对：{stats['supported_without_positive']}；"
          f"无共现支持的孤立词：{stats['isolated_marks']}；无正边的词：{stats['marks_without_positive_edges']}。")
    total = stats["positive_edges"]
    def ratio(count):
        return f"{count}（{count / total:.1%}）" if total else "0（无正边）"
    print(f"仅一个块支持的正边：{ratio(stats['single_support_positive_edges'])}；"
          f"NPMI=1 的边：{ratio(stats['unit_weight_edges'])}。")
    if "single_source_positive_edges" in stats:
        print(f"仅一个来源版本支持的正边：{ratio(stats['single_source_positive_edges'])}。")
    if total:
        print(f"正边 NPMI 最小/中位/最大：{stats['min_positive_weight']} / "
              f"{stats.get('median_positive_weight', '未统计')} / {stats['max_positive_weight']}。")
    print("NPMI 是块共现关联，不是置信度；少量共现也可能得到高分。召回相关性和语义支持尚需评测。")
    print("这是持久化快照，不证明服务或 Gateway 在线。observe 可查看图和来源详情。")


def open_command_window(command, config_path):
    """Compatibility entry: route to the single console; never open a window."""
    if command not in {"start", "observe", "knowledge"}:
        raise ValueError("unsupported console panel")
    from .console_session import run_console
    run_console(Path(config_path), command)


def open_knowledge_window(config_path):
    open_command_window("knowledge", config_path)


def offline_check(config_path):
    path = config_path if config_path.exists() else Path(__file__).resolve().parent.parent / "config.example.toml"
    config = load_config(path)
    import discord
    import pypdf
    from .pdf_import import EXTRACTOR_VERSION
    if pypdf.__version__ != EXTRACTOR_VERSION:
        raise ValueError("PDF extractor version mismatch; reinstall requirements.lock")
    print(f"Offline configuration/import check OK; {config.name} {__version__}; "
          f"discord.py {discord.__version__}; pypdf {pypdf.__version__}.")
    print("Live Discord and model calls were not made.")


def retry_knowledge(config):
    print("retry 需要当前 Console 持有运行中的服务。请显式 start 后执行 retry；此入口没有启动模型或修改检查点。")


def main():
    parser = argparse.ArgumentParser(description="Indeces: single Discord runtime console")
    parser.add_argument("command", nargs="?", choices=sorted(CONSOLE_COMMANDS), default="console")
    parser.add_argument("--config", type=Path, default=Path("config.local.toml"))
    parser.add_argument("--once", action="store_true", help="read-only command: output one snapshot/report directly and exit")
    parser.add_argument("--headless", action="store_true", help="start: explicit foreground runtime without interactive Console")
    parser.add_argument("--path", help="retry: retry only this relative material path (case is preserved)")
    parser.add_argument("--keep-window", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.once and args.command not in {"knowledge", "audit", "scratch", "npmi", "status", "logs", "check"}:
        parser.error("--once requires a read-only snapshot/report command")
    if args.headless and args.command != "start":
        parser.error("--headless is only supported by start")
    if args.path and args.command != "retry":
        parser.error("--path is only supported by retry")
    if args.keep_window and args.command not in {"start", "observe", "knowledge"}:
        parser.error("--keep-window requires start, observe or knowledge")
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        args.config = validate_config_path(args.config)
        if args.config.exists() and args.command not in {"apikey", "discord"}:
            # Validate before routing to an older Console or writing credentials.
            load_config(args.config)
        if not args.once and not args.headless:
            receipt = route_existing(args.config, args.command, path=args.path)
            if receipt is not None:
                print("已连接现有 Indeces Console；功能请求已送达。" +
                      ("已聚焦窗口。" if receipt.get("focused") else "当前终端不支持或未允许自动聚焦。"))
                return
        if args.command == "init":
            initialize(args.config)
            return
        if args.command == "check":
            offline_check(args.config)
            return
        if not args.config.exists():
            if args.command == "npmi":
                print("未找到配置；npmi 只读检查不会创建配置或启动服务。")
                return
            initialize(args.config)
        if args.command in {"console", "start", "observe", "knowledge", "retry"} and not args.once and not args.headless:
            from .console_session import run_console
            run_console(args.config, args.command + (" " + args.path if args.path else ""))
            return
        if args.command in {"discord", "apikey"}:
            if not {"discord": configure_discord, "apikey": configure_api_key}[args.command](args.config):
                raise SystemExit(2)
            return
        config = load_config(args.config)
        if args.command in {"start", "status"}:
            {"start": run, "status": status}[args.command](config, args.config)
        elif args.command == "knowledge":
            knowledge(config, once=True)
        elif args.command == "audit":
            from .knowledge_audit import audit_snapshot, render_audit
            print(render_audit(audit_snapshot(config)))
        elif args.command in {"stop", "service"}:
            print("当前配置没有可连接的 Console；未停止其他实例或进程。请先打开 console。")
        elif args.command in {"logs", "npmi", "retry"}:
            {"logs": show_logs, "npmi": npmi, "retry": retry_knowledge}[args.command](config)
        else:
            check_scratch(config)
    except PathPolicyError as error:
        print(f"Path rejected: {error.code}. Indeces knowledge must use the project's knowledge directory; OneDrive and links are forbidden.")
        raise SystemExit(2) from None
    except CredentialError as error:
        print(f"Credential error: {error.code}. Use discord setup for the Bot token, or apikey for the OpenAI key, before starting.")
        raise SystemExit(2) from None
    except Exception as error:
        print(f"Startup failed: {type(error).__name__}. Check configuration and local file permissions.")
        raise SystemExit(2) from None
