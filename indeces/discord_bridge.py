"""One Discord Gateway connection feeding one bounded, serial runtime queue.

The Discord dependency is imported only by ``run``. Message selection and the
runtime contract can therefore be tested without installing it or using a token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import logging
import re
import time
from typing import Any

from .bot_conversations import BotConversationGate, ROUND_LIMIT
from .contracts import DeliveryReceipt, GovernedError, IncomingMessage


_LOGGER = logging.getLogger(__name__)
_DISCORD_CONTENT_LIMIT = 2000
_TRUNCATION_NOTICE = "\n[回复已截断；完整输出保存在 scratch log。]"


def select_messages(message: Any, bot_id: str, config: Any) -> IncomingMessage | None:
    """Select a human or other bot's explicit textual @mention in the guild.

    ``Message.mentions`` also contains information associated with replies, and
    ``mentioned_in`` can accept @everyone. The literal bot token is the trigger.
    This function deliberately does not fetch messages, attachments or history.
    """
    author = getattr(message, "author", None)
    guild = getattr(message, "guild", None)
    if author is None or guild is None:
        return None
    if str(author.id) == str(bot_id) or getattr(message, "webhook_id", None) is not None:
        return None
    guild_id = str(guild.id)
    channel_id = str(message.channel.id)
    if guild_id != str(config.guild_id):
        return None
    if config.channel_ids and channel_id not in config.channel_ids:
        return None
    raw_text = getattr(message, "content", "")
    if not isinstance(raw_text, str):
        return None
    mention = re.compile(r"<@!?" + re.escape(str(bot_id)) + r">")
    if mention.search(raw_text) is None:
        return None
    text = mention.sub("", raw_text).strip()
    if not text:
        return None
    created_at = getattr(message, "created_at", "")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    return IncomingMessage(
        message_id=str(message.id),
        channel_id=channel_id,
        guild_id=guild_id,
        author_id=str(author.id),
        author_name=str(getattr(author, "display_name", None) or getattr(author, "name", author.id)),
        text=text,
        created_at=str(created_at),
        raw_text=raw_text,
        author_is_bot=bool(getattr(author, "bot", False)),
    )


def discord_reply_text(text: str) -> str:
    """Produce one bounded reply, conservatively counting UTF-16 code units."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Discord reply must contain nonempty text")
    # Discord documents a character cap without a Unicode counting contract.
    # UTF-16 units are conservative for supplementary-plane characters.
    units = len(text.encode("utf-16-le")) // 2
    if units <= _DISCORD_CONTENT_LIMIT:
        return text
    remaining = _DISCORD_CONTENT_LIMIT - len(_TRUNCATION_NOTICE.encode("utf-16-le")) // 2
    prefix: list[str] = []
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if width > remaining:
            break
        prefix.append(character)
        remaining -= width
    return "".join(prefix) + _TRUNCATION_NOTICE


@dataclass(frozen=True)
class _Envelope:
    incoming: IncomingMessage
    source: Any
    queued_at: float = field(default_factory=time.monotonic)
    bot_round: int | None = None


class DiscordBridge:
    """Own the transport, intake queue and sole queue consumer.

    The runtime receives plain contracts and a delivery callback. No transport
    objects enter model prompts. This bridge performs no application-level send
    retry; a timeout remains uncertain because Discord may have accepted it.
    """

    def __init__(self, config: Any, runtime: Any, scratch: Any, *, bot_gate: BotConversationGate | None = None) -> None:
        self.config = config.discord
        self._queue_wait_seconds = config.runtime.queue_wait_seconds
        self.runtime = runtime
        self.scratch = scratch
        self._bot_gate = bot_gate if bot_gate is not None else BotConversationGate(runtime.store.db)
        self._queue: asyncio.Queue[_Envelope] = asyncio.Queue(maxsize=self.config.queue_capacity)
        self._worker: asyncio.Task[None] | None = None
        self._client: Any = None
        self._discord: Any = None
        self._allowed_mentions: Any = None
        self._accepting = False
        self._running = False
        self._shutdown_requested = False
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._worker_failed = asyncio.Event()
        self._worker_error: BaseException | None = None
        self._delivery_audit_error: Exception | None = None

    def enqueue(self, message: Any, bot_id: str) -> bool:
        """Admit one selected message without blocking the Gateway event loop."""
        incoming = select_messages(message, bot_id, self.config)
        if incoming is None:
            return False
        if not self._accepting or self._worker is None or self._worker.done():
            self.scratch.write("discord_message_rejected", message_id=incoming.message_id, reason="not_running")
            return False
        if self._queue.full():
            self.scratch.write(
                "discord_message_rejected", message_id=incoming.message_id,
                channel_id=incoming.channel_id, reason="queue_full",
                queue_capacity=self.config.queue_capacity,
            )
            _LOGGER.warning("Discord queue full; rejected message %s", incoming.message_id)
            return False
        try:
            admission = self._bot_gate.prepare(incoming)
        except Exception as error:
            self._fail_intake(error, incoming.message_id)
            raise
        if not admission.allowed:
            self.scratch.write(
                "discord_message_rejected", message_id=incoming.message_id,
                channel_id=incoming.channel_id, author_id=incoming.author_id,
                author_is_bot=incoming.author_is_bot, reason=admission.reason,
                bot_round_limit=ROUND_LIMIT,
            )
            return False
        # Write the accepted input before it becomes available to the consumer.
        # There is no await between capacity checking and enqueueing.
        try:
            self.scratch.write(
                "discord_message_queued", message_id=incoming.message_id,
                guild_id=incoming.guild_id, channel_id=incoming.channel_id,
                author_id=incoming.author_id, raw_text=incoming.raw_text,
                text=incoming.text, queue_depth=self._queue.qsize() + 1,
                author_is_bot=incoming.author_is_bot, bot_round=admission.round,
                bot_epoch=admission.epoch,
            )
            # Persist before exposing work. Skips, failures and uncertain sends
            # never refund a round or restart an automated loop.
            self._bot_gate.commit(incoming, admission)
        except Exception as error:
            self._fail_intake(error, incoming.message_id)
            raise
        self._queue.put_nowait(_Envelope(incoming, message, bot_round=admission.round))
        return True

    def _fail_intake(self, error: Exception, message_id: str) -> None:
        self._accepting = False
        self._worker_error = error
        self._worker_failed.set()
        _LOGGER.error("Discord intake failed (%s); message intake closed", type(error).__name__)
        try:
            self.scratch.write("discord_admission_failed", message_id=message_id,
                               error_type=type(error).__name__, intake_closed=True)
        except Exception:
            # Preserve the original storage/audit failure when the sink failed.
            pass

    def _start_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            raise RuntimeError("Discord worker is already running")
        self._shutdown_requested = False
        self._shutdown_task = None
        self._worker_failed.clear()
        self._worker_error = None
        self._delivery_audit_error = None
        self._accepting = True
        self._worker = asyncio.create_task(self._consume(), name="indeces-discord-worker")
        self._worker.add_done_callback(self._worker_done)

    def _worker_done(self, worker: asyncio.Task[None]) -> None:
        if worker is not self._worker or self._shutdown_requested:
            return
        self._accepting = False
        error = None if worker.cancelled() else worker.exception()
        self._worker_error = error or RuntimeError("Discord worker stopped unexpectedly")
        self._worker_failed.set()
        # The sink that caused the worker to stop may itself be unavailable.
        _LOGGER.error("Discord worker stopped unexpectedly (%s); message intake closed",
                      type(self._worker_error).__name__)

    async def _consume(self) -> None:
        while not self._shutdown_requested:
            envelope = await self._queue.get()
            incoming = envelope.incoming
            try:
                wait_seconds = time.monotonic() - envelope.queued_at
                if wait_seconds >= self._queue_wait_seconds:
                    self.scratch.write(
                        "discord_message_rejected", message_id=incoming.message_id,
                        channel_id=incoming.channel_id, reason="queue_wait_expired",
                        wait_seconds=wait_seconds, queue_wait_seconds=self._queue_wait_seconds,
                    )
                    _LOGGER.warning(
                        "Discord message %s expired in queue after %.3f seconds (limit %.3f)",
                        incoming.message_id, wait_seconds, self._queue_wait_seconds,
                    )
                    continue
                self.scratch.write("discord_runtime_started", message_id=incoming.message_id)

                async def deliver(text: str, _envelope: _Envelope = envelope) -> DeliveryReceipt:
                    return await self._deliver(_envelope, text)

                await self.runtime.process(incoming, deliver)
                self.scratch.write("discord_runtime_finished", message_id=incoming.message_id)
            except asyncio.CancelledError:
                self.scratch.write("discord_runtime_cancelled", message_id=incoming.message_id, reason="shutdown")
                raise
            except Exception as error:
                self.scratch.write(
                    "discord_runtime_failed", message_id=incoming.message_id,
                    error_type=type(error).__name__, error=str(error),
                )
                _LOGGER.exception("Discord runtime failed for message %s", incoming.message_id)
            finally:
                self._queue.task_done()
            if self._delivery_audit_error is not None:
                # Return the confirmed receipt to Runtime before stopping the
                # transport. A local audit failure cannot undo remote delivery.
                raise RuntimeError("Discord confirmed-delivery audit failed") from self._delivery_audit_error

    async def _deliver(self, envelope: _Envelope, text: str) -> DeliveryReceipt:
        if self._shutdown_requested:
            raise GovernedError("discord_stopping")
        incoming = envelope.incoming
        allowed_mentions = self._allowed_mentions
        mention_peer = False
        transport_text = text
        if incoming.author_is_bot:
            if type(envelope.bot_round) is not int or not 1 <= envelope.bot_round <= ROUND_LIMIT:
                raise GovernedError("bot_round_missing")
            mention_peer = envelope.bot_round < ROUND_LIMIT
            # A disallowed literal mention can still trigger a raw-text peer.
            # Keep the continuation token under transport control.
            transport_text = re.sub(r"<@!?" + re.escape(incoming.author_id) + r">", "", text).strip()
            if not transport_text:
                raise GovernedError("empty_bot_reply")
            if mention_peer:
                transport_text = f"<@{incoming.author_id}> {transport_text}"
                allowed_mentions = self._discord.AllowedMentions(
                    users=[self._discord.Object(id=int(incoming.author_id))],
                    roles=False, everyone=False, replied_user=False,
                )
        sent_text = discord_reply_text(transport_text)
        self.scratch.write(
            "discord_delivery_started", message_id=incoming.message_id,
            channel_id=incoming.channel_id, model_output=text, sent_text=sent_text,
            truncated=sent_text != transport_text, timeout_seconds=self.config.delivery_seconds,
            author_is_bot=incoming.author_is_bot, bot_round=envelope.bot_round,
            mentioned_peer_id=incoming.author_id if mention_peer else None,
        )
        try:
            async with asyncio.timeout(self.config.delivery_seconds):
                sent = await envelope.source.reply(
                    sent_text, allowed_mentions=allowed_mentions, mention_author=False,
                )
        except TimeoutError:
            self.scratch.write(
                "discord_delivery_failed", message_id=incoming.message_id,
                channel_id=incoming.channel_id, status="delivery_unknown",
                error_type="TimeoutError", retry=False,
            )
            raise
        except asyncio.CancelledError:
            self.scratch.write(
                "discord_delivery_failed", message_id=incoming.message_id,
                channel_id=incoming.channel_id, status="delivery_unknown",
                error_type="CancelledError", retry=False,
            )
            raise
        except Exception as error:
            is_http_error = self._discord is not None and isinstance(error, self._discord.HTTPException)
            status_code = getattr(error, "status", None) if is_http_error else None
            # A 5xx may arrive after a server-side effect; 403/404 are known rejections.
            status = "rejected" if is_http_error and status_code is not None and 400 <= status_code < 500 else "delivery_unknown"
            self.scratch.write(
                "discord_delivery_failed", message_id=incoming.message_id,
                channel_id=incoming.channel_id, status=status,
                error_type=type(error).__name__, error=str(error),
                http_status=status_code, discord_code=getattr(error, "code", None), retry=False,
            )
            raise
        receipt = DeliveryReceipt(message_ids=(str(sent.id),), text=sent_text)
        try:
            self.scratch.write(
                "discord_delivery_finished", message_id=incoming.message_id,
                channel_id=incoming.channel_id, sent_message_ids=list(receipt.message_ids),
                sent_text=sent_text,
            )
        except Exception as error:
            self._delivery_audit_error = error
            self._accepting = False
            _LOGGER.error("Discord delivery confirmed; audit failed (%s); message intake closed",
                          type(error).__name__)
        return receipt

    async def _shutdown_worker(self) -> None:
        # All callers share one physical cleanup. Cancelling a caller must not
        # release the service's resources while the consumer still owns them.
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_worker_impl(), name="indeces-discord-shutdown")
        interrupted = False
        while not self._shutdown_task.done():
            try:
                await asyncio.shield(self._shutdown_task)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        try:
            self._shutdown_task.result()
        finally:
            if interrupted:
                raise asyncio.CancelledError

    async def _shutdown_worker_impl(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True
            self._accepting = False
            audit_errors = []
            while True:
                try:
                    envelope = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    self.scratch.write(
                        "discord_message_dropped", message_id=envelope.incoming.message_id,
                        channel_id=envelope.incoming.channel_id, reason="shutdown_before_processing",
                    )
                except Exception as error:
                    audit_errors.append(error)
                finally:
                    self._queue.task_done()
            if self._worker is None:
                if audit_errors:
                    raise audit_errors[0]
                return
            worker = self._worker
            if not worker.done():
                worker.cancel()
                timeout = min(5.0, self.config.delivery_seconds)
                _, pending = await asyncio.wait({worker}, timeout=timeout)
                if pending:
                    try:
                        self.scratch.write("discord_shutdown_incomplete", reason="worker_ignored_cancellation", timeout_seconds=timeout)
                    except Exception as error:
                        audit_errors.append(error)
                    _LOGGER.error("Discord worker did not stop within %.3f seconds; waiting for physical exit before releasing resources", timeout)
                    # This is a diagnostic checkpoint, not a hard preemption.
                    # Retain the storage lock and underlying resources until
                    # the actual worker exits, even if it ignores cancellation.
                    await asyncio.wait({worker})
            # Retrieve exceptions to avoid silently losing a crashed worker.
            if not worker.cancelled() and worker.exception() is not None:
                error = worker.exception()
                try:
                    self.scratch.write("discord_worker_failed", error_type=type(error).__name__, error=str(error))
                except Exception as audit_error:
                    audit_errors.append(audit_error)
            self._worker = None
            if audit_errors:
                raise audit_errors[0]

    async def close(self) -> None:
        """Stop intake and audit pending work before closing the connection."""
        if self._client is not None:
            await self._client.close()
        else:
            await self._shutdown_worker()

    async def run(self, token: str) -> None:
        if self._running or (self._worker is not None and not self._worker.done()):
            raise RuntimeError("Only one Discord connection may run at a time")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("A nonempty Discord bot token is required")
        discord = importlib.import_module("discord")
        owner = self

        class Client(discord.Client):
            async def setup_hook(self) -> None:
                owner._start_worker()

            async def on_ready(self) -> None:
                owner.scratch.write("discord_ready", bot_id=str(self.user.id), guild_id=owner.config.guild_id)
                _LOGGER.info("Discord connected as %s", self.user)

            async def on_message(self, message: Any) -> None:
                if self.user is not None:
                    owner.enqueue(message, str(self.user.id))

            async def close(self) -> None:
                try:
                    await owner._shutdown_worker()
                finally:
                    await super().close()

        # Mention-only content is available without privileged MESSAGE_CONTENT.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        self._allowed_mentions = discord.AllowedMentions.none()
        client = Client(
            intents=intents, allowed_mentions=self._allowed_mentions,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False, max_messages=None,
        )
        self._discord = discord
        self._client = client
        self._running = True
        self._worker_failed.clear()
        self._worker_error = None
        gateway_task = failure_task = None
        try:
            async with client:
                self.scratch.write("discord_connecting", guild_id=self.config.guild_id)
                gateway_task = asyncio.create_task(client.start(token), name="indeces-discord-client")
                failure_task = asyncio.create_task(self._worker_failed.wait(), name="indeces-discord-worker-monitor")
                try:
                    done, _ = await asyncio.wait({gateway_task, failure_task}, return_when=asyncio.FIRST_COMPLETED)
                    if failure_task in done:
                        raise RuntimeError("Discord worker stopped unexpectedly") from self._worker_error
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
                self._client = None
                self._running = False
