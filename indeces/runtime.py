from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict

from .adapter import reservation
from .context import compaction_prefix, encode, groups, history_cost, history_data, raw_capacity, reply_messages
from .contracts import GovernedError
from .memory import MemoryGraph
from .run_records import answer_record, digest, freeze_retrieval
from . import prompts


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def label_data(text):
    try:
        data = strict_json(text)
        if not isinstance(data, dict) or set(data) != {"marks"}:
            raise ValueError()
        marks = data["marks"]
        if not isinstance(marks, list) or not 1 <= len(marks) <= 8:
            raise ValueError()
        def valid_mark(mark):
            return isinstance(mark, str) and 0 < len(mark.strip()) <= 40
        if any(not valid_mark(mark) for mark in marks):
            raise ValueError()
        return sorted({mark.strip() for mark in marks})
    except (ValueError, TypeError, KeyError):
        raise GovernedError("invalid_labels") from None


class Runtime:
    def __init__(self, config, store, adapter, scratch):
        self.config, self.store, self.adapter, self.scratch = config, store, adapter, scratch
        self.graph = MemoryGraph(store.db, self_marks=(config.name,))
        self.knowledge_scope = f"{config.discord.guild_id}:knowledge"
        self._serial = asyncio.Lock()

    async def _summary(self, message, knowledge, trace_id):
        checkpoint = self.store.checkpoint(message.scope)
        rows = self.store.history(message.scope, checkpoint["through_seq"], exclude_turn=message.message_id)
        instructions = prompts.reply_instructions(self.config.name)
        capacity = raw_capacity(self.config, instructions, message, knowledge)
        prefix = compaction_prefix(rows, capacity, self.config.runtime.low_watermark)
        self.scratch.write("context_watermark", trace_id=trace_id, raw_reservation=history_cost(rows),
                           raw_capacity=capacity, low_watermark=self.config.runtime.low_watermark,
                           checkpoint_id=checkpoint["id"], planned_source_seqs=[r["seq"] for r in prefix])
        if prefix:
            admitted = []
            budget = self.config.adapter.budgets["summary"]
            def summary_input(selected):
                return [{"role": "user", "content": encode({"previous_summary": checkpoint["summary"],
                    "source_prefix": history_data(selected), "summary_max_utf8_bytes": self.config.runtime.summary_max_bytes})}]
            for interaction in groups(prefix):
                proposed = admitted + interaction
                if reservation(prompts.SUMMARY, summary_input(proposed), prompts.SUMMARY_SCHEMA) > budget.input_tokens:
                    break
                admitted = proposed
            if not admitted:
                raise GovernedError("summary_prefix_too_large")
            result = await self.adapter.call("summary", prompts.SUMMARY, summary_input(admitted), trace_id, prompts.SUMMARY_SCHEMA)
            try:
                data = strict_json(result.text)
                if not isinstance(data, dict) or set(data) != {"summary"} or not isinstance(data["summary"], str):
                    raise ValueError()
                summary = data["summary"].strip()
                if not summary or len(summary.encode("utf-8")) > self.config.runtime.summary_max_bytes:
                    raise ValueError()
            except (ValueError, TypeError):
                raise GovernedError("invalid_summary") from None
            checkpoint = self.store.save_checkpoint(message.scope, admitted, summary, trace_id)
            self.scratch.write("checkpoint_saved", trace_id=trace_id, checkpoint=checkpoint)
            rows = self.store.history(message.scope, checkpoint["through_seq"], exclude_turn=message.message_id)
            if history_cost(rows) > capacity:
                raise GovernedError("context_backlog_requires_next_turn")
        return reply_messages(checkpoint["summary"], rows, knowledge, message)

    async def process(self, message, deliver):
        # Discord bridge also serializes. This lock preserves the invariant for
        # direct callers/tests without adding another conversation surface.
        async with self._serial:
            turn_deadline = time.monotonic() + self.config.runtime.turn_seconds
            trace_id = uuid.uuid4().hex
            if not self.store.begin(message):
                self.scratch.write("duplicate_ignored", trace_id=trace_id, message_id=message.message_id)
                return
            self.scratch.write("turn_start", trace_id=trace_id, message_id=message.message_id, scope=message.scope,
                               input={"text": message.text, "raw_text": message.raw_text, "author_id": message.author_id},
                               deadline_seconds=self.config.runtime.turn_seconds, run_record_version=1,
                               knowledge_scope=self.knowledge_scope)
            delivered = False
            confirmed = False
            try:
                async with asyncio.timeout(self.config.runtime.turn_seconds):
                    started = time.monotonic()
                    # sqlite is cooperatively interrupted during expensive scans;
                    # graph methods also bound their lookup/expansion outputs.
                    self.store.db.set_progress_handler(lambda: int(time.monotonic() - started > self.config.runtime.local_seconds), 1000)
                    try:
                        graph_audit = {}
                        records = self.graph.retrieve(self.knowledge_scope, [], message.text, time.time(),
                                                      event_id=message.message_id, audit=graph_audit)
                        self.scratch.write("memory_observation", trace_id=trace_id, audit=graph_audit,
                                           audit_sha256=digest(graph_audit))
                        retrieval = freeze_retrieval(self.store.db, self.knowledge_scope, message.message_id,
                                                     message.text, records, graph_audit)
                        knowledge = retrieval["model_materials"]
                    finally:
                        self.store.db.set_progress_handler(None, 0)
                    if time.monotonic() - started > self.config.runtime.local_seconds:
                        raise GovernedError("local_memory_timeout")
                    retrieval_hash = digest(retrieval)
                    self.scratch.write("retrieval_record", trace_id=trace_id, record=retrieval, record_sha256=retrieval_hash)
                    self.scratch.write("knowledge_retrieved", trace_id=trace_id, input_marks=[],
                                       query=message.text, records=knowledge, retrieval_sha256=retrieval_hash,
                                       elapsed_seconds=time.monotonic() - started)
                    messages = await self._summary(message, knowledge, trace_id)
                    instructions = prompts.reply_instructions(self.config.name)
                    self.scratch.write("reply_context", trace_id=trace_id, retrieval_sha256=retrieval_hash,
                                       instructions=instructions, messages=messages)
                    result = await self.adapter.call("reply", instructions, messages, trace_id)
                    self.scratch.write("answer_generated", trace_id=trace_id, retrieval_sha256=retrieval_hash,
                                       record=answer_record(result.text, retrieval), model_result=asdict(result))
                    self.store.generated(message.message_id, result.text)
                    self.scratch.write("delivery_start", trace_id=trace_id, message_id=message.message_id, text=result.text)
                    # Mark attempt before crossing Discord boundary. A timeout
                    # can mean delivered remotely; never automatically retry.
                    delivered = True
                    async with asyncio.timeout(self.config.discord.delivery_seconds):
                        receipt = await deliver(result.text)
                    confirmed = True
                    self.store.finish(message, receipt)
                    self.scratch.write("answer_delivered", trace_id=trace_id, retrieval_sha256=retrieval_hash,
                                       record=answer_record(receipt.text, retrieval), receipt_ids=receipt.message_ids)
                    self.scratch.write("turn_end", trace_id=trace_id, status="delivered", receipt={"ids": receipt.message_ids, "text": receipt.text})
                    return
            except asyncio.CancelledError:
                if confirmed:
                    print(f"[{self.config.name}] Discord delivery confirmed; post-delivery recording interrupted; trace={trace_id}", flush=True)
                    raise
                code = "delivery_unknown" if delivered else "cancelled"
                self.store.fail(message.message_id, code)
                self.scratch.write("turn_end", trace_id=trace_id, status=code)
                raise
            except Exception as error:
                if confirmed:
                    # A local audit failure cannot erase a confirmed Discord
                    # receipt or trigger another delivery. Preserve history.
                    print(f"[{self.config.name}] Discord delivery confirmed; post-delivery storage/audit failed: {type(error).__name__}; trace={trace_id}; inspect records before restarting.", flush=True)
                    return
                code = error.code if isinstance(error, GovernedError) else "turn_timeout" if isinstance(error, TimeoutError) else type(error).__name__
                if delivered:
                    code = "delivery_unknown:" + code
                self.store.fail(message.message_id, code)
                self.scratch.write("turn_end", trace_id=trace_id, status="failed", code=code)
                print(f"[{self.config.name}] turn failed: {code}; trace={trace_id}", flush=True)
                if not delivered:
                    # A fixed failure receipt adds no model request. It is traced
                    # and kept separate from assistant conversational history.
                    try:
                        remaining = turn_deadline - time.monotonic()
                        if remaining <= 0:
                            self.scratch.write("failure_notice_skipped", trace_id=trace_id, reason="turn_time_exhausted")
                            return
                        notice = f"本轮未完成（{code}）。审计编号：{trace_id[:12]}"
                        async with asyncio.timeout(min(self.config.discord.delivery_seconds, remaining)):
                            receipt = await deliver(notice)
                        self.scratch.write("failure_notice_delivered", trace_id=trace_id, receipt={"ids": receipt.message_ids, "text": receipt.text})
                    except Exception as error:
                        self.scratch.write("failure_notice_unknown", trace_id=trace_id, error_type=type(error).__name__)
