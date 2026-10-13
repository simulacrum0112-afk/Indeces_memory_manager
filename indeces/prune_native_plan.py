"""One counted native Claude plan, charged to the existing prune grant.

This is a planning transport seam, not a graph or retrieval implementation.
The caller grounds the accepted JSON against the actual graph before creating a
Session. Live mode requires the same admitted ledger and no-retry native client
as the prune continuation. No request, audit, or unknown outcome is retried.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import time
import uuid

from .claude_adapter import (
    CLAUDE_BASE_URL, ClaudeAdapter, _COUNT_PATH, _GENERATE_PATH,
    _pairs, _schema_matches, _validate_json,
)
from .claude_prune_budget import NATIVE_INPUT_RESERVATION, PruneBudget
from .claude_prune_loop import (
    PruneToolLoop, _NativePostView, _PruneProgress, _copy,
)
from .contracts import GovernedError
from .prune_turn_budget import PruneTurnBudget


_MODEL = "claude-opus-5-5"
_CONTEXT = 1_048_576
_STAGE = "npmi_prune_plan"


@dataclass(frozen=True)
class NativePlanResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str


class _PlanDriver:
    """Reuse the continuation's sealed native driver without a graph Session."""

    _check_live_session = staticmethod(PruneToolLoop._check_live_session)

    def __init__(self, adapter, budget, live):
        if type(adapter) is not ClaudeAdapter or type(budget) is not PruneBudget:
            raise GovernedError("prune_native_adapter_required")
        if type(live) is not bool:
            raise GovernedError("prune_invalid_activation")
        if live:
            if ((budget.existing_grant_bound is not True and budget.campaign_grant_bound is not True)
                    or budget.path.resolve() != budget.bound_path):
                raise GovernedError("prune_live_existing_grant_required")
            opening = budget.snapshot()
            if (opening["halted"] is not None or opening["pending_nusd"] != 0
                    or type(opening["remaining_nusd"]) is not int
                    or opening["remaining_nusd"] <= 0):
                raise GovernedError("prune_live_budget_not_ready")
            if adapter._request_override is not None:
                raise GovernedError("prune_live_native_transport_required")
            self._check_live_session(adapter._session)
            if not isinstance(adapter._key, str) or not adapter._key:
                raise GovernedError("prune_live_key_required")
        elif not callable(getattr(adapter, "_request_override", None)):
            raise GovernedError("prune_live_not_authorized")
        self.adapter, self.budget, self._live = adapter, budget, live
        self._config = adapter.config
        self._transport = adapter._request_override
        self._native_post = ClaudeAdapter._post
        self._native_end = ClaudeAdapter._record_end
        self._native_messages = ClaudeAdapter._messages
        self._lock = adapter._lock
        self._post_view = _NativePostView(
            self._transport, adapter._key if live else "",
            adapter._session if live else None, adapter.scratch, adapter._audit,
        )
        self.check()

    def check(self):
        PruneToolLoop._check_adapter(self)
        if (self.adapter._lock is not self._lock
                or ClaudeAdapter._record_end is not self._native_end
                or getattr(self.adapter._record_end, "__func__", None) is not self._native_end
                or ClaudeAdapter._messages is not self._native_messages
                or getattr(self.adapter._messages, "__func__", None) is not self._native_messages):
            raise GovernedError("prune_native_adapter_changed")


async def select_native_plan(adapter, budget, instructions, user, schema, trace_id,
                             *, input_cap, output_cap, seconds, live=True, turn_budget=None):
    """Return one strictly validated JSON plan after known-usage settlement.

    ``live=False`` only admits an explicit injected transport for offline tests;
    product callers use the default live gate. The supplied local caps bound
    execution, while the durable hold uses the native input liability ceiling.
    Ownership and closure of the adapter/client/ledger remain with the caller.
    """
    started = time.monotonic()
    if turn_budget is not None and type(turn_budget) is not PruneTurnBudget:
        raise GovernedError("prune_turn_invalid_budget")
    if (not isinstance(instructions, str) or not isinstance(user, str) or not user.strip()
            or not isinstance(trace_id, str) or not trace_id.strip() or len(trace_id) > 240
            or type(schema) is not dict
            or type(input_cap) is not int or not 0 < input_cap < _CONTEXT
            or type(output_cap) is not int or not 0 < output_cap <= _CONTEXT - NATIVE_INPUT_RESERVATION
            or input_cap + output_cap > _CONTEXT
            or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
        raise GovernedError("prune_invalid_input")
    try:
        instructions, user, schema, trace_id = _copy([instructions, user, schema, trace_id])
    except (ValueError, UnicodeError, RecursionError):
        raise GovernedError("prune_invalid_input") from None
    driver = _PlanDriver(adapter, budget, live)
    declared_input_cap = input_cap
    # Native schema lowering and text-only messages are reused unchanged. This
    # immutable request snapshot is made before any user-provided audit runs.
    common = driver._native_messages(adapter, instructions,
                                     [{"role": "user", "content": user}], schema)
    common = _copy({**common, "thinking": {"type": "adaptive"}})
    deadline = min(started + seconds, turn_budget.deadline) if turn_budget is not None else started + seconds
    permit = progress = call_id = None
    turn_permit = None
    completed = terminal_attempted = False
    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            async with driver._lock:
                driver.check()
                if turn_budget is not None:
                    turn_permit = turn_budget.reserve(_STAGE, input_cap, output_cap, seconds)
                    input_cap, output_cap = turn_permit.input_cap, turn_permit.output_cap
                    deadline = min(deadline, turn_permit.deadline)
                    turn_budget.check_call(turn_permit)
                if time.monotonic() >= deadline:
                    raise GovernedError("prune_turn_time_limit" if turn_budget is not None else "stage_timeout")
                permit = budget.reserve(f"{trace_id}:plan", NATIVE_INPUT_RESERVATION, output_cap)
                call_id = uuid.uuid4().hex
                progress = _PruneProgress(started, deadline=deadline)
                progress.phase = "slot_acquired"
                progress.slot_wait_seconds = time.monotonic() - started

                def audit(event, **fields):
                    if event == "response_received" and fields.get("path") == _GENERATE_PATH:
                        fields = {**fields, "response_model": progress.response_model}
                    budget.observe(permit, event, fields)

                driver._post_view._audit(
                    audit, "call_start", progress, trace_id=trace_id, call_id=call_id,
                    stage=_STAGE, model=_MODEL, input_cap=input_cap, output_cap=output_cap,
                    provider="anthropic", protocol="messages", base_url=CLAUDE_BASE_URL,
                )
                counted = await driver._native_post(
                    driver._post_view, _COUNT_PATH, common, trace_id, call_id, progress, audit,
                )
                tokens = counted.get("input_tokens")
                if type(tokens) is not int or tokens < 0:
                    raise GovernedError("invalid_input_token_count")
                if tokens > input_cap:
                    raise GovernedError("prune_turn_token_limit" if turn_permit is not None
                                        and input_cap < declared_input_cap else "input_token_limit")
                driver.check()
                if turn_budget is not None:
                    turn_budget.check_call(turn_permit)
                if time.monotonic() >= deadline:
                    raise GovernedError("prune_turn_time_limit" if turn_budget is not None else "stage_timeout")
                payload = _copy({**common, "max_tokens": output_cap, "stream": False,
                                 "service_tier": "standard_only", "inference_geo": "global"})
                data = await driver._native_post(
                    driver._post_view, _GENERATE_PATH, payload, trace_id, call_id, progress, audit,
                )
                driver.check()
                if progress.known_usage is None:
                    raise GovernedError("missing_usage" if not isinstance(data.get("usage"), dict)
                                        else "invalid_usage")
                # observe() has already verified actual model, billing metadata
                # and known counters, then charged them before semantic parsing.
                terminal_attempted = True
                driver._native_end(
                    driver._post_view, progress, audit, trace_id=trace_id, call_id=call_id,
                    stage=_STAGE, status="completed", elapsed_seconds=time.monotonic() - started,
                )
                budget.complete(permit)
                completed = True
                inp, out = progress.known_usage["input_tokens"], progress.known_usage["output_tokens"]
                if turn_budget is not None:
                    turn_budget.settle(turn_permit, inp, out, native_usage=progress.native_usage)
                    turn_budget.check()
                if budget.snapshot()["halted"] is not None:
                    raise GovernedError("prune_budget_halted")
                data = _copy(data)
                if inp > input_cap or out > output_cap:
                    raise GovernedError("provider_token_limit_breach")
                if any((progress.native_usage.get("server_tool_use") or {}).values()):
                    raise GovernedError("unexpected_provider_item")
                _, tools, text = PruneToolLoop._content(driver, data)
                if tools:
                    raise GovernedError("unexpected_provider_item")
                if not text.strip():
                    raise GovernedError("empty_output")
                try:
                    value = json.loads(
                        text, object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
                    )
                    _validate_json(value)
                    if not _schema_matches(value, schema):
                        raise ValueError("schema mismatch")
                except (ValueError, UnicodeError, RecursionError):
                    raise GovernedError("invalid_structured_output") from None
                if time.monotonic() >= deadline:
                    raise GovernedError("prune_turn_time_limit" if turn_budget is not None else "stage_timeout")
                return NativePlanResult(text, inp, out, data["id"])
    except BaseException as original:
        error = GovernedError("prune_turn_time_limit" if turn_budget is not None else "stage_timeout") if isinstance(original, TimeoutError) else original
        if turn_budget is not None and getattr(error, "code", None) == "stage_timeout":
            error = GovernedError("prune_turn_time_limit")
        if turn_permit is not None:
            try:
                turn_budget.fail(turn_permit, known_usage=progress.known_usage if progress is not None else None,
                                 native_usage=progress.native_usage if progress is not None else None,
                                 generation_intent=bool(progress is not None and progress.generation_request_started),
                                 code=getattr(error, "code", "prune_turn_cancelled" if isinstance(error, asyncio.CancelledError)
                                              else "prune_turn_local_failure"))
            except BaseException:
                pass
        if progress is not None:
            try:
                progress.annotate_error(error)
            except BaseException:
                pass
            if not completed and not terminal_attempted:
                try:
                    def failed_audit(event, **fields):
                        if event == "response_received" and fields.get("path") == _GENERATE_PATH:
                            fields = {**fields, "response_model": progress.response_model}
                        budget.observe(permit, event, fields)
                    driver._native_end(
                        driver._post_view, progress, failed_audit,
                        trace_id=trace_id, call_id=call_id, stage=_STAGE, status="failed",
                        code=getattr(error, "code", "prune_local_failure"),
                        elapsed_seconds=time.monotonic() - started,
                    )
                except BaseException:
                    pass
        if permit is not None:
            try:
                budget.fail(permit, error)
            except BaseException:
                pass
        try:
            budget.halt(getattr(error, "code", "prune_local_failure"))
        except BaseException:
            pass
        # A failed terminal sink or ledger never masks the primary cancellation
        # or causes replay of either count or generation.
        if error is original:
            raise
        raise error from None
