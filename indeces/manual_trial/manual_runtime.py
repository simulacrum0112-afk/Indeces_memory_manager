"""Finite, explicit manual sessions; importing this file reads no private data.

This is a separate session using the existing configuration target.  It cannot
inject a Runtime into an already running Console.  The original state lease
must be available; neither this module nor its callers may stop that owner.
Runtime factories must return isolated, freshly seeded Stores, one per item.
The model adapter remains responsible for the approved cumulative cost gate.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
from types import MappingProxyType


class ManualRuntimeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _items(bundle):
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("items"), list):
        raise ManualRuntimeError("manual_approved_items_required")
    rows = bundle["items"]
    if not 1 <= len(rows) <= 2:
        raise ManualRuntimeError("manual_item_limit")
    result, ids, texts = [], set(), set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ManualRuntimeError("manual_invalid_item")
        item_id, text = row.get("id"), row.get("text")
        if (not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128
                or not isinstance(text, str) or not text.strip() or text != text.strip()
                or item_id in ids or text in texts):
            raise ManualRuntimeError("manual_invalid_item")
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if row.get("text_sha256", text_hash) != text_hash:
            raise ManualRuntimeError("manual_item_digest_mismatch")
        result.append(MappingProxyType({"id": item_id, "text": text,
                                        "text_sha256": text_hash}))
        ids.add(item_id)
        texts.add(text)
    return tuple(result)


@dataclass
class PreparedContext:
    config_path: Path
    config: object = field(repr=False)
    channel_id: str
    account_descriptor: str
    approved_items: tuple = field(repr=False)
    approved_bundle_sha256: str
    lease: object = field(repr=False)
    config_loader: object = field(default=None, repr=False)
    source_digest_reader: object = field(default=None, repr=False)
    loaded_source_sha256: str | None = None
    network_preflight: object = field(default=None, repr=False)
    account_binding: str = "operator_asserted_not_verified"
    closed: bool = False
    session_started: bool = False

    def require_open(self):
        if self.closed:
            raise ManualRuntimeError("manual_context_closed")

    def revalidate(self):
        self.require_open()
        before = self.source_digest_reader(self.config_path) if self.source_digest_reader is not None else None
        if before != self.loaded_source_sha256:
            raise ManualRuntimeError("manual_configuration_changed")
        if self.config_loader is not None and self.config_loader(self.config_path) != self.config:
            raise ManualRuntimeError("manual_configuration_changed")
        after = self.source_digest_reader(self.config_path) if self.source_digest_reader is not None else None
        if after != before:
            raise ManualRuntimeError("manual_configuration_changed")

    def require_ready_for_network(self):
        self.revalidate()
        if not callable(self.network_preflight):
            raise ManualRuntimeError("manual_live_preflight_required")
        result = self.network_preflight()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ManualRuntimeError("manual_async_preflight_forbidden")
        if not isinstance(result, Mapping) or result.get("ready") is not True:
            raise ManualRuntimeError("manual_live_preflight_rejected")

    def describe(self):
        self.require_open()
        return {"config_path": str(self.config_path),
                "guild_id": self.config.discord.guild_id,
                "channel_id": self.channel_id,
                "model": self.config.adapter.model,
                "endpoint": self.config.adapter.base_url,
                "account_descriptor": self.account_descriptor,
                "account_binding": self.account_binding,
                "approved_bundle_sha256": self.approved_bundle_sha256,
                "loaded_config_source_sha256": self.loaded_source_sha256,
                "item_ids": [item["id"] for item in self.approved_items],
                "execution": "independent_finite_session_not_existing_console",
                "production_store_opened": False,
                "knowledge_workers_started": False}

    def close(self):
        if not self.closed:
            self.closed = True
            self.lease.close()

    def claim_session(self):
        self.require_open()
        if self.session_started:
            raise ManualRuntimeError("manual_context_already_used_no_retry")
        self.session_started = True

    def __enter__(self):
        self.require_open()
        return self

    def __exit__(self, *unused):
        self.close()


def prepare_context(config_path, channel_id, account_descriptor, approved_bundle,
                    *, config_loader=None, path_validator=None,
                    lease_factory=None, id_validator=None, network_preflight=None,
                    source_digest_reader=None):
    """Late-bound configuration and original state lease, without credentials.

    Tests must inject all readers/factories.  A real caller must first validate
    its approval, frozen bundle and tariff through the launcher cost gate.
    This function never routes Console IPC or opens the original Store.
    """
    items = _items(approved_bundle)  # Invalid inputs fail before any private read.
    if (not isinstance(account_descriptor, str) or not account_descriptor.strip()
            or len(account_descriptor) > 128):
        raise ManualRuntimeError("manual_account_descriptor_required")
    try:
        bundle_bytes = json.dumps(dict(approved_bundle), ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise ManualRuntimeError("manual_bundle_not_json") from None
    if path_validator is None:
        from indeces.path_policy import validate_config_path
        path_validator = validate_config_path
    if config_loader is None or id_validator is None:
        from indeces.config import load_config, snowflake
        config_loader = config_loader or load_config
        id_validator = id_validator or snowflake
    if lease_factory is None:
        from indeces.lock import InstanceLock
        lease_factory = InstanceLock
    path = Path(path_validator(Path(config_path)))
    if source_digest_reader is None:
        source_digest_reader = lambda value: hashlib.sha256(Path(value).read_bytes()).hexdigest()
    source_hash = source_digest_reader(path)
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise ManualRuntimeError("manual_invalid_configuration_fingerprint")
    config = config_loader(path)
    if source_digest_reader(path) != source_hash:
        raise ManualRuntimeError("manual_configuration_changed")
    channel = id_validator(channel_id)
    if not config.discord.guild_id:
        raise ManualRuntimeError("manual_guild_required")
    if config.discord.channel_ids and channel not in config.discord.channel_ids:
        raise ManualRuntimeError("manual_channel_not_allowed")
    try:
        lease = lease_factory(config.state_dir)
    except RuntimeError:
        raise ManualRuntimeError("manual_state_owned_no_takeover") from None
    try:
        if source_digest_reader(path) != source_hash:
            raise ManualRuntimeError("manual_configuration_changed")
        if config_loader(path) != config:
            raise ManualRuntimeError("manual_configuration_changed")
        if source_digest_reader(path) != source_hash:
            raise ManualRuntimeError("manual_configuration_changed")
        return PreparedContext(path, config, channel, account_descriptor.strip(), items,
                               hashlib.sha256(bundle_bytes).hexdigest(), lease,
                               config_loader=config_loader, network_preflight=network_preflight,
                               source_digest_reader=source_digest_reader, loaded_source_sha256=source_hash)
    except BaseException:
        lease.close()
        raise


def _credential(context, kind, environment, stored_loader, private_prompt, validator):
    context.require_ready_for_network()
    if environment is None:
        import os
        environment = os.environ
    if stored_loader is None or private_prompt is None or validator is None:
        from indeces.credentials import (load_discord_token, load_openai_key,
                                          prompt_secret, validate_api_key, validate_token)
        stored_loader = stored_loader or (load_openai_key if kind == "openai" else load_discord_token)
        private_prompt = private_prompt or prompt_secret
        validator = validator or (validate_api_key if kind == "openai" else validate_token)
    value = environment.get("OPENAI_API_KEY" if kind == "openai" else "DISCORD_BOT_TOKEN")
    if not value:
        value = (stored_loader(context.config_path) if kind == "openai" else
                 stored_loader(context.config_path, context.config.discord.guild_id))
    if not value:
        value = private_prompt("OpenAI API key (manual session, not saved): " if kind == "openai"
                               else "Discord bot token (manual session, not saved): ")
    return validator(value)


def load_openai_credential(context, *, environment=None, stored_loader=None,
                           private_prompt=None, validator=None):
    return _credential(context, "openai", environment, stored_loader, private_prompt, validator)


def load_discord_credential(context, *, environment=None, stored_loader=None,
                            private_prompt=None, validator=None):
    return _credential(context, "discord", environment, stored_loader, private_prompt, validator)


def _deadlines(context, session_seconds, delivery_seconds):
    for value in (session_seconds, delivery_seconds):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ManualRuntimeError("manual_invalid_deadline")
    if delivery_seconds > min(10.0, context.config.discord.delivery_seconds):
        raise ManualRuntimeError("manual_delivery_deadline_expansion")
    if session_seconds > 90.0:
        raise ManualRuntimeError("manual_session_deadline_expansion")


def _path_overlaps(candidate, protected):
    candidate, protected = Path(candidate).resolve(), Path(protected).resolve()
    return candidate.is_relative_to(protected) or protected.is_relative_to(candidate)


class _ReplyOnlyAdapter:
    """Reject unexpected stages before the underlying count/generation gate."""
    def __init__(self, adapter):
        self.adapter = adapter
        self.blocked = False

    async def call(self, stage, *args, **kwargs):
        if stage != "reply":
            self.blocked = True
            from indeces.contracts import GovernedError
            raise GovernedError("manual_non_reply_stage_forbidden", remote_usage_unknown=False)
        if self.blocked:
            from indeces.contracts import GovernedError
            raise GovernedError("manual_batch_stopped", remote_usage_unknown=False)
        return await self.adapter.call(stage, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.adapter, name)


class FiniteRuntimeFacade:
    """Two approved human items, serialized, each using a fresh isolated Store."""
    def __init__(self, context, runtime_factory, scratch=None, *, mode="api", delivery_seconds=None):
        context.require_open()
        if mode not in {"api", "discord"}:
            raise ManualRuntimeError("manual_invalid_mode")
        self.context, self.scratch, self.mode = context, scratch, mode
        self.delivery_seconds = (min(10.0, context.config.discord.delivery_seconds)
                                 if delivery_seconds is None else delivery_seconds)
        self.done = asyncio.Event()
        self._serial = asyncio.Lock()
        self._items_by_text = {item["text"]: item for item in context.approved_items}
        self._admitted, self._message_ids, self._processed = {}, set(), set()
        self.outcomes = []
        self.stopped, self.stop_reason = False, None
        self.persistent_stop_errors = []
        self.runtimes = {}
        session_scratch_path = getattr(scratch, "path", None)
        if session_scratch_path is not None and _path_overlaps(session_scratch_path, context.config.scratch_dir):
            raise ManualRuntimeError("manual_production_scratch_forbidden")
        database_ids = set()
        for item in context.approved_items:
            runtime = (runtime_factory[item["id"]] if isinstance(runtime_factory, Mapping)
                       else runtime_factory(item, context))
            if inspect.isawaitable(runtime):
                if inspect.iscoroutine(runtime):
                    runtime.close()
                raise ManualRuntimeError("manual_async_runtime_factory_forbidden")
            db = runtime.store.db
            if id(db) in database_ids:
                raise ManualRuntimeError("manual_distinct_fresh_stores_required")
            database_ids.add(id(db))
            db_name = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
            if db_name and _path_overlaps(Path(db_name).parent, context.config.state_dir):
                raise ManualRuntimeError("manual_production_store_forbidden")
            runtime_config = getattr(runtime, "config", None)
            if runtime_config is not None:
                if _path_overlaps(runtime_config.state_dir, context.config.state_dir):
                    raise ManualRuntimeError("manual_production_store_forbidden")
                if _path_overlaps(runtime_config.scratch_dir, context.config.scratch_dir):
                    raise ManualRuntimeError("manual_production_scratch_forbidden")
            runtime_scratch_path = getattr(getattr(runtime, "scratch", None), "path", None)
            if (runtime_scratch_path is not None
                    and _path_overlaps(runtime_scratch_path, context.config.scratch_dir)):
                raise ManualRuntimeError("manual_production_scratch_forbidden")
            scope = f"{context.config.discord.guild_id}:{context.channel_id}"
            for table in ("turns", "messages", "checkpoints"):
                if db.execute(f"SELECT COUNT(*) FROM {table} WHERE scope=?", (scope,)).fetchone()[0]:
                    raise ManualRuntimeError("manual_fresh_history_required")
            runtime.adapter = _ReplyOnlyAdapter(runtime.adapter)
            preflight = getattr(runtime.adapter.adapter, "require_ready_for_network", None)
            if not callable(preflight):
                raise ManualRuntimeError("manual_adapter_preflight_required")
            preflight()
            self.runtimes[item["id"]] = runtime
        self.store = next(iter(self.runtimes.values())).store  # Bridge's gate is isolated too.

    def stop(self, reason):
        if not self.stopped:
            self.stopped, self.stop_reason = True, reason
            gates = set()
            for runtime in self.runtimes.values():
                gate = getattr(runtime.adapter, "gate", None)
                halt = getattr(gate, "halt", None)
                if not callable(halt) or id(gate) in gates:
                    continue
                gates.add(id(gate))
                try:
                    halt(reason)
                except Exception as error:
                    # PilotGate.halt persists and then raises its first sticky
                    # code. Keep the original Runtime/transport exception;
                    # record other persistence failures without exception text.
                    ledger = getattr(gate, "ledger", {})
                    if (type(error).__name__ != "PilotBlocked" or not isinstance(ledger, Mapping)
                            or not ledger.get("stop_code")):
                        self.persistent_stop_errors.append(type(error).__name__)
            if self.scratch is not None:
                self.scratch.write("manual_session_stopped", code=reason, automatically_replayed=False,
                    persistent_stop_errors=list(self.persistent_stop_errors))
        self.done.set()

    def reserve(self, incoming, bot_id=None):
        """Reserve synchronously before Bridge exposes the accepted input."""
        if (self.stopped or incoming.author_is_bot
                or incoming.guild_id != self.context.config.discord.guild_id
                or incoming.channel_id != self.context.channel_id
                or incoming.message_id in self._message_ids):
            return False
        if self.mode == "discord":
            if not isinstance(bot_id, str) or re.search(r"<@!?" + re.escape(bot_id) + r">", incoming.raw_text) is None:
                return False
        item = self._items_by_text.get(incoming.text)
        if item is None or item["id"] in self._admitted or len(self._admitted) >= 2:
            return False
        self._admitted[item["id"]] = incoming.message_id
        self._message_ids.add(incoming.message_id)
        return True

    async def process(self, incoming, deliver):
        async with self._serial:
            if self.stopped:
                return
            item = self._items_by_text.get(incoming.text)
            if item is None or self._admitted.get(item["id"]) != incoming.message_id or item["id"] in self._processed:
                self.stop("manual_unreserved_input")
                return
            runtime = self.runtimes[item["id"]]
            runtime.adapter.adapter.item_id = item["id"]
            self.context.require_ready_for_network()
            runtime.adapter.adapter.require_ready_for_network()
            async def bounded_deliver(text):
                async with asyncio.timeout(self.delivery_seconds):
                    return await deliver(text)
            try:
                await runtime.process(incoming, bounded_deliver)
                row = runtime.store.db.execute("SELECT status FROM turns WHERE message_id=?", (incoming.message_id,)).fetchone()
                delivered = row is not None and row[0] == "delivered" and not runtime.adapter.blocked
                self.outcomes.append({"item_id": item["id"], "message_id": incoming.message_id,
                                      "mode": self.mode, "status": "completed" if delivered else "failed"})
                if not delivered:
                    self.stop("manual_runtime_turn_failed")
            except BaseException:
                self.stop("manual_runtime_interrupted")
                raise
            finally:
                self._processed.add(item["id"])
                if len(self._processed) == len(self.context.approved_items):
                    self.done.set()


def create_finite_bridge(config, facade, scratch, *, bridge_class=None, selector=None,
                         discord_module_factory=None):
    if bridge_class is None or selector is None:
        from indeces.discord_bridge import DiscordBridge, select_messages
        bridge_class = bridge_class or DiscordBridge
        selector = selector or select_messages

    class FiniteBridge(bridge_class):
        def enqueue(self, message, bot_id):
            incoming = selector(message, bot_id, self.config)
            if incoming is None or not facade.reserve(incoming, str(bot_id)):
                return False
            accepted = super().enqueue(message, bot_id)
            if not accepted:
                facade.stop("manual_bridge_admission_failed")
            elif len(facade._admitted) == len(facade.context.approved_items):
                self._accepting = False
            return accepted

        async def run(self, token):
            # Reuse the base dispatch, worker and delivery contracts, but disable
            # automatic Gateway reconnect; the outer session owns the deadline.
            if self._running or (self._worker is not None and not self._worker.done()):
                raise ManualRuntimeError("manual_gateway_already_running")
            if not isinstance(token, str) or not token.strip():
                raise ManualRuntimeError("manual_discord_token_required")
            if discord_module_factory is None:
                import importlib
                discord = importlib.import_module("discord")
            else:
                discord = discord_module_factory()
            owner = self

            class Client(discord.Client):
                async def setup_hook(self):
                    owner._start_worker()

                async def on_ready(self):
                    owner.scratch.write("discord_ready", bot_id=str(self.user.id),
                                        guild_id=owner.config.guild_id, manual_session=True)

                async def on_message(self, message):
                    if self.user is not None:
                        owner.enqueue(message, str(self.user.id))

                async def close(self):
                    try:
                        await owner._shutdown_worker()
                    finally:
                        await super().close()

            intents = discord.Intents.none()
            intents.guilds = intents.guild_messages = True
            self._allowed_mentions = discord.AllowedMentions.none()
            client = Client(intents=intents, allowed_mentions=self._allowed_mentions,
                member_cache_flags=discord.MemberCacheFlags.none(),
                chunk_guilds_at_startup=False, max_messages=None)
            self._discord, self._client, self._running = discord, client, True
            self._worker_failed.clear()
            self._worker_error = None
            gateway_task = failure_task = None
            try:
                async with client:
                    self.scratch.write("discord_connecting", guild_id=self.config.guild_id,
                                       manual_session=True, gateway_reconnect=False)
                    gateway_task = asyncio.create_task(client.start(token, reconnect=False),
                        name="indeces-finite-manual-client")
                    failure_task = asyncio.create_task(self._worker_failed.wait(),
                        name="indeces-finite-manual-worker-monitor")
                    try:
                        done, _ = await asyncio.wait({gateway_task, failure_task},
                            return_when=asyncio.FIRST_COMPLETED)
                        if failure_task in done:
                            raise ManualRuntimeError("manual_discord_worker_failed")
                        await gateway_task
                    finally:
                        for task in (gateway_task, failure_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(gateway_task, failure_task, return_exceptions=True)
            finally:
                try:
                    await self._shutdown_worker()
                finally:
                    self._client, self._running = None, False

    return FiniteBridge(config, facade, scratch)


async def run_api_only(context, runtime_factory, *, session_seconds, delivery_seconds,
                       on_output=None, incoming_factory=None, receipt_factory=None, scratch=None):
    context.require_ready_for_network()
    _deadlines(context, session_seconds, delivery_seconds)
    context.claim_session()
    if incoming_factory is None or receipt_factory is None:
        from indeces.contracts import IncomingMessage, DeliveryReceipt
        incoming_factory = incoming_factory or IncomingMessage
        receipt_factory = receipt_factory or DeliveryReceipt
    facade = FiniteRuntimeFacade(context, runtime_factory, scratch, mode="api",
                                 delivery_seconds=delivery_seconds)
    try:
        async with asyncio.timeout(session_seconds):
            for item in context.approved_items:
                incoming = incoming_factory(message_id="manual-api-" + item["id"],
                    channel_id=context.channel_id, guild_id=context.config.discord.guild_id,
                    author_id="manual-operator", author_name="manual-operator", text=item["text"],
                    created_at=datetime.now(timezone.utc).isoformat(), raw_text=item["text"], author_is_bot=False)
                if not facade.reserve(incoming):
                    facade.stop("manual_api_admission_failed")
                    break
                async def deliver(text, item_id=item["id"]):
                    from indeces.contracts import FailureNotice
                    if isinstance(text, FailureNotice):
                        raise ManualRuntimeError("manual_model_failure_no_delivery")
                    async with asyncio.timeout(delivery_seconds):
                        if on_output is not None:
                            result = on_output(item_id, text)
                            if inspect.isawaitable(result):
                                await result
                    return receipt_factory(("local-manual-" + item_id,), text)
                await facade.process(incoming, deliver)
                if facade.stopped:
                    break
    except BaseException:
        facade.stop("manual_api_session_interrupted")
        raise
    return {"mode": "api_only_local_delivery", "outcomes": facade.outcomes,
            "stopped": facade.stopped, "stop_reason": facade.stop_reason,
            "persistent_stop_errors": list(facade.persistent_stop_errors),
            "actual_discord_delivery": False}


async def run_discord(context, runtime_factory, scratch, *, session_seconds,
                      delivery_seconds, bridge_factory=None, token_loader=None):
    context.require_ready_for_network()
    _deadlines(context, session_seconds, delivery_seconds)
    context.claim_session()
    facade = FiniteRuntimeFacade(context, runtime_factory, scratch, mode="discord",
                                 delivery_seconds=delivery_seconds)
    bridge = (bridge_factory or create_finite_bridge)(context.config, facade, scratch)
    gateway_task = done_task = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + session_seconds
    try:
        async with asyncio.timeout(session_seconds):
            token = (token_loader or load_discord_credential)(context)
            gateway_task = asyncio.create_task(bridge.run(token), name="indeces-finite-manual-discord")
            done_task = asyncio.create_task(facade.done.wait(), name="indeces-finite-manual-completion")
            done, _ = await asyncio.wait({gateway_task, done_task}, return_when=asyncio.FIRST_COMPLETED)
            if gateway_task in done:
                await gateway_task
                if not facade.done.is_set():
                    facade.stop("manual_gateway_ended_before_cases")
    except BaseException:
        facade.stop("manual_discord_session_interrupted")
        raise
    finally:
        for task in (gateway_task, done_task):
            if task is not None and not task.done():
                task.cancel()
        # Cooperative asyncio cleanup is bounded too; this is not OS preemption.
        remaining = min(delivery_seconds, max(0.001, deadline - loop.time()))
        try:
            async with asyncio.timeout(remaining):
                await bridge.close()
                await asyncio.gather(*(task for task in (gateway_task, done_task)
                                       if task is not None), return_exceptions=True)
        except BaseException:
            facade.stop("manual_discord_cleanup_incomplete")
            raise
    return {"mode": "finite_discord_human_only", "outcomes": facade.outcomes,
            "stopped": facade.stopped, "stop_reason": facade.stop_reason,
            "persistent_stop_errors": list(facade.persistent_stop_errors),
            "existing_console_reused": False}
