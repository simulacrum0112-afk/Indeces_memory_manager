"""A question's shared token and wall-clock fuse, separate from USD accounting.

Reservations cover every concurrent call's maximum local input and output.
Actual provider counters release unused token capacity; output already includes
thinking tokens. Unknown generation outcomes retain their reservation and stop
the question. This object is deliberately ephemeral and cannot reset a ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
import uuid

from .contracts import GovernedError


@dataclass(frozen=True, repr=False)
class TurnPermit:
    identity: str
    stage: str
    input_cap: int
    output_cap: int
    deadline: float
    started: float


class PruneTurnBudget:
    def __init__(self, total_tokens, seconds, max_calls=9):
        if (type(total_tokens) is not int or total_tokens < 2
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0
                or type(max_calls) is not int or not 1 <= max_calls <= 256):
            raise GovernedError("prune_turn_invalid_budget")
        self.total_tokens, self.max_calls = total_tokens, max_calls
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self._lock = threading.RLock()
        self._pending = {}
        self._records = {}
        self._calls = self._spent = 0
        self._closed = None

    def _check(self):
        if self._closed is not None:
            raise GovernedError(self._closed)
        if time.monotonic() >= self.deadline:
            self._closed = "prune_turn_time_limit"
            raise GovernedError(self._closed)

    def check(self):
        with self._lock:
            self._check()

    def reserve(self, stage, input_cap, output_cap, seconds):
        if (type(stage) is not str or not stage or len(stage) > 80
                or type(input_cap) is not int or input_cap <= 0
                or type(output_cap) is not int or output_cap <= 0
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
            raise GovernedError("prune_turn_invalid_call")
        with self._lock:
            self._check()
            if self._calls >= self.max_calls:
                self._closed = "prune_turn_call_limit"
                raise GovernedError(self._closed)
            occupied = sum(p.input_cap + p.output_cap for p in self._pending.values())
            remaining = self.total_tokens - self._spent - occupied
            if remaining < 2:
                self._closed = "prune_turn_token_limit"
                raise GovernedError(self._closed)
            # Each call receives its configured output cap if it fits. No
            # planner-specific slice is taken out of a later call's allowance.
            output = min(output_cap, remaining - 1)
            inp = min(input_cap, remaining - output)
            now = time.monotonic()
            permit = TurnPermit(uuid.uuid4().hex, stage, inp, output,
                                min(self.deadline, now + seconds), now)
            self._pending[permit.identity] = permit
            self._calls += 1
            return permit

    def check_call(self, permit):
        with self._lock:
            self._permit(permit)
            self._check()
            if time.monotonic() >= permit.deadline:
                self._closed = "prune_turn_time_limit"
                raise GovernedError(self._closed)

    def _permit(self, permit):
        if type(permit) is not TurnPermit or self._pending.get(permit.identity) is not permit:
            raise GovernedError("prune_turn_invalid_permit")

    @staticmethod
    def _usage(inp, out, native_usage):
        if type(inp) is not int or inp < 0 or type(out) is not int or out < 0:
            raise GovernedError("prune_turn_invalid_usage")
        if native_usage is not None:
            if type(native_usage) is not dict:
                raise GovernedError("prune_turn_invalid_usage")
            counts = [native_usage.get("input_tokens"), native_usage.get("cache_read_input_tokens", 0),
                      native_usage.get("cache_creation_input_tokens", 0), native_usage.get("output_tokens")]
            details = native_usage.get("output_tokens_details")
            thinking = details.get("thinking_tokens") if type(details) is dict else None
            if (any(type(c) is not int or c < 0 for c in counts)
                    or sum(counts[:3]) != inp or counts[3] != out
                    or (details is not None and type(details) is not dict)
                    or (thinking is not None and (type(thinking) is not int or not 0 <= thinking <= out))):
                raise GovernedError("prune_turn_invalid_usage")
            return thinking
        return None

    def _settle(self, permit, inp, out, native_usage, status):
        self._permit(permit)
        thinking = self._usage(inp, out, native_usage)
        self._pending.pop(permit.identity)
        self._spent += inp + out
        record = {"stage": permit.stage, "status": status, "input_cap": permit.input_cap,
                  "output_cap": permit.output_cap, "input_tokens": inp, "output_tokens": out,
                  "thinking_tokens": thinking, "elapsed_seconds": max(0.0, time.monotonic() - permit.started)}
        self._records[permit.identity] = record
        if inp > permit.input_cap or out > permit.output_cap or self._spent > self.total_tokens:
            self._closed = "provider_token_limit_breach"
            raise GovernedError(self._closed)
        return dict(record)

    def settle(self, permit, input_tokens, output_tokens, *, native_usage=None):
        with self._lock:
            try:
                return self._settle(permit, input_tokens, output_tokens, native_usage, "completed")
            except BaseException as error:
                if self._closed is None:
                    self._closed = getattr(error, "code", "prune_turn_local_failure")
                raise

    def fail(self, permit, *, known_usage=None, native_usage=None, generation_intent=False,
             code="prune_turn_local_failure"):
        """Close a question while retaining confirmed usage and unknown holds.

        A settled permit is recognized without charging it twice. No exception
        from this secondary bookkeeping should replace a caller's primary error.
        """
        with self._lock:
            if type(permit) is not TurnPermit:
                raise GovernedError("prune_turn_invalid_permit")
            if permit.identity in self._records:
                self._records[permit.identity]["status"] = "failed_known"
                if self._closed is None:
                    self._closed = code
                return
            self._permit(permit)
            try:
                if known_usage is not None:
                    if type(known_usage) is not dict:
                        raise GovernedError("prune_turn_invalid_usage")
                    self._settle(permit, known_usage.get("input_tokens"), known_usage.get("output_tokens"),
                                 native_usage, "failed_known")
                elif not generation_intent:
                    self._settle(permit, 0, 0, None, "failed_before_generation")
            finally:
                if self._closed is None:
                    self._closed = code

    def snapshot(self):
        with self._lock:
            occupied = sum(p.input_cap + p.output_cap for p in self._pending.values())
            return {"schema": "prune_turn_budget_v1", "total_token_limit": self.total_tokens,
                    "spent_tokens": self._spent, "pending_tokens": occupied,
                    "remaining_tokens": max(0, self.total_tokens - self._spent - occupied),
                    "calls": self._calls, "max_calls": self.max_calls, "closed": self._closed,
                    "elapsed_seconds": max(0.0, time.monotonic() - self.started),
                    "remaining_seconds": max(0.0, self.deadline - time.monotonic()),
                    "records": [dict(value) for value in self._records.values()]}
