from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import getpass
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3

from . import __version__
from .adapter import OpenAIAdapter
from .config import load_config
from .discord_bridge import DiscordBridge
from .knowledge import KnowledgeService
from .lock import InstanceLock
from .runtime import Runtime
from .scratch import ScratchLog, verify
from .store import Store


async def serve(config, key, token):
    if not config.discord.guild_id:
        raise ValueError("set discord.guild_id before starting")
    lease = InstanceLock(config.state_dir)
    scratch = store = adapter = bridge = knowledge = None
    try:
        scratch = ScratchLog(config.scratch_dir)
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
                      knowledge_limits=asdict(config.knowledge), model_concurrency=1, chat_labelling=False)
        print(f"Indeces {__version__}: one Discord connection; model=gpt-6-luna; scratch={scratch.path}")
        print("Knowledge file updates trigger background labels. Chat labelling is closed. Ctrl+C stops the service.")
        knowledge.start()
        await bridge.run(token)
    finally:
        if bridge is not None:
            await bridge.close()
        if knowledge is not None:
            await knowledge.close()
        if adapter is not None:
            await adapter.close()
        if scratch is not None:
            scratch.write("service_stop")
            scratch.close()
        if store is not None:
            store.close()
        lease.close()


def initialize(path):
    if path.exists():
        print(f"Configuration already exists: {path}")
        return
    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    if not example.exists():
        raise FileNotFoundError("use config.example.toml from the repository checkout")
    shutil.copyfile(example, path)
    print(f"Created {path}. Set discord.guild_id, then start. Credentials are prompted privately or read from environment.")


def status(config):
    print(f"Indeces {__version__}; model {config.adapter.model}; one model request slot")
    print(f"Discord guild={config.discord.guild_id or '<not configured>'}; channels={config.discord.channel_ids or 'all explicitly mentioned channels'}")
    print(f"Knowledge: {config.knowledge_dir}; scratch: {config.scratch_dir}")
    print("Chat labelling: CLOSED; passive knowledge-update labelling: ENABLED while service runs")
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


def run(config):
    if not config.discord.guild_id:
        raise ValueError("set discord.guild_id in config.local.toml first")
    key = os.environ.get("OPENAI_API_KEY") or getpass.getpass("OpenAI API key (not saved): ")
    token = os.environ.get("DISCORD_BOT_TOKEN") or getpass.getpass("Discord bot token (not saved): ")
    if not key.strip() or not token.strip():
        raise ValueError("both credentials are required")
    try:
        asyncio.run(serve(config, key, token))
    except KeyboardInterrupt:
        print("Indeces stopped.")


def check_scratch(config):
    files = sorted(config.scratch_dir.glob("*.jsonl"))
    for path in files:
        count, digest = verify(path)
        print(f"{path.name}: {count} records, hash chain OK; head={digest}")
    if not files:
        print("No runtime scratch logs yet.")


def main():
    parser = argparse.ArgumentParser(description="Indeces: minimal Discord runtime console")
    parser.add_argument("command", nargs="?", choices=["console", "init", "start", "status", "check", "scratch"], default="console")
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
            print(f"Offline configuration/import check OK; Indeces {__version__}; discord.py {discord.__version__}.")
            print("Live Discord and model calls were not made.")
            return
        if not args.config.exists():
            initialize(args.config)
        if args.command != "console":
            config = load_config(args.config)
            {"start": run, "status": status, "scratch": check_scratch}[args.command](config)
            return
        print(f"Indeces {__version__} console: start | status | scratch | quit")
        while True:
            try:
                command = input("indeces> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if command in {"quit", "exit"}:
                break
            if not command:
                continue
            try:
                config = load_config(args.config)
                action = {"start": run, "status": status, "scratch": check_scratch}.get(command)
                if action:
                    action(config)
                else:
                    print("Commands: start | status | scratch | quit")
            except Exception as error:
                # Never print arbitrary authentication transport exceptions.
                print(f"Command failed: {type(error).__name__}. Check configuration and scratch records.")
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Startup failed: {type(error).__name__}. Check configuration and local file permissions.")
        raise SystemExit(2) from None
