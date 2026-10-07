"""Finite manual-pilot request gate; live activation defaults to disabled.

The caller supplies an owner-approved descriptor and tariff evidence. Fee
checks establish consistency with that declared tariff, not a provider invoice.
No network request or credential acquisition occurs in this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import math
import uuid


LIVE_ENABLED = False
MODEL = "gpt-6.1-sol"
COUNT_ENDPOINT = "https://api.openai.com/v1/responses/input_tokens"
GENERATION_ENDPOINT = "https://api.openai.com/v1/responses"
LIVE_LEDGER_ROOT = Path(r"D:\Indeces\build\manual-pilot-approval-ledgers")
_MONEY = re.compile(r"\d{1,18}(?:\.\d{1,24})?\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class PilotBlocked(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def money(value):
    if not isinstance(value, str) or not _MONEY.fullmatch(value):
        raise PilotBlocked("invalid_money")
    return Decimal(value)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PilotLimits:
    input_tokens: int = 16384
    output_tokens: int = 2048
    max_count_requests: int = 2
    max_generations: int = 2
    hard_cap_usd: str = "1.00"
    cumulative_input_tokens: int = 32768
    cumulative_output_tokens: int = 4096
    cumulative_seconds: int = 90
    call_seconds: int = 45

    def validate(self):
        if (type(self.input_tokens) is not int or self.input_tokens != 16384
                or type(self.output_tokens) is not int or self.output_tokens != 2048
                or type(self.max_count_requests) is not int or not 1 <= self.max_count_requests <= 2
                or type(self.max_generations) is not int or not 1 <= self.max_generations <= 2
                or type(self.cumulative_input_tokens) is not int or not 1 <= self.cumulative_input_tokens <= 32768
                or type(self.cumulative_output_tokens) is not int or not 1 <= self.cumulative_output_tokens <= 4096
                or type(self.cumulative_seconds) is not int or not 1 <= self.cumulative_seconds <= 90
                or type(self.call_seconds) is not int or not 1 <= self.call_seconds <= 45
                or not Decimal(0) < money(self.hard_cap_usd) <= Decimal(1)):
            raise PilotBlocked("invalid_limits")


@dataclass(frozen=True)
class Tariff:
    model: str
    generation_input_per_million: str
    generation_output_per_million: str
    generation_flat: str
    count_input_per_million: str
    count_flat: str
    count_billing_basis: str
    provenance: dict

    def validate(self, mode):
        if (self.model != MODEL or self.count_billing_basis != "verified_flat_fee"
                or money(self.count_input_per_million) != 0):
            raise PilotBlocked("unverified_tariff")
        for name in ("generation_input_per_million", "generation_output_per_million", "generation_flat",
                     "count_input_per_million", "count_flat"):
            if money(getattr(self, name)).as_tuple().exponent < -12:
                raise PilotBlocked("unverified_tariff")
        p = self.provenance
        if (not isinstance(p, dict) or p.get("verified") is not True
                or p.get("kind") != ("synthetic" if mode == "mock" else "trusted_provider")
                or not isinstance(p.get("source"), str) or not p["source"].strip()):
            raise PilotBlocked("unverified_tariff")

    def cost(self, kind, input_tokens, output_tokens=0):
        if kind == "count":
            return money(self.count_flat) + Decimal(input_tokens) * money(self.count_input_per_million) / 1000000
        return (money(self.generation_flat)
                + Decimal(input_tokens) * money(self.generation_input_per_million) / 1000000
                + Decimal(output_tokens) * money(self.generation_output_per_million) / 1000000)


def synthetic_tariff():
    # Explicit virtual amounts for arithmetic tests. These are NOT model prices.
    return Tariff(MODEL, "1", "1", "0", "0", "0", "verified_flat_fee",
                  {"kind": "synthetic", "source": "offline_mock_fixture", "verified": True})


class PilotGate:
    def __init__(self, path=None, *, mode="mock", tariff=None, limits=None, approval=None,
                 input_bundle_sha256="", candidate_identity="", live_enabled=False,
                 mock_ledger_root=None):
        self.mode, self.tariff, self.limits = mode, tariff, limits or PilotLimits()
        self.approval = approval
        self._mutex = threading.Lock()
        self._lockfile = None
        self._request_clocks = {}
        self._run_clock = time.monotonic()
        self.limits.validate()
        if mode not in {"mock", "live"}:
            raise PilotBlocked("invalid_mode")
        if type(live_enabled) is not bool:
            raise PilotBlocked("invalid_activation")
        if tariff is None:
            raise PilotBlocked("unverified_tariff")
        tariff.validate(mode)
        self._approval_scoped = mode == "live" or mock_ledger_root is not None
        if mock_ledger_root is not None and mode != "mock":
            raise PilotBlocked("mock_ledger_root_forbidden")
        if self._approval_scoped:
            self._validate_live_approval(input_bundle_sha256, candidate_identity)
        if mode == "live" and not live_enabled:
            # Descriptor validation precedes activation. No filesystem or
            # credential operation is needed for the disabled path.
            raise PilotBlocked("live_disabled")
        if self._approval_scoped:
            ledger_root = (LIVE_LEDGER_ROOT if mode == "live" else Path(mock_ledger_root))
            expected_root = ledger_root.absolute()
            ledger_root = ledger_root.resolve()
            if ledger_root != expected_root:
                raise PilotBlocked("ledger_root_redirected")
            approval_key = hashlib.sha256(self.approval["approval_id"].encode()).hexdigest()
            self.path = ledger_root / (approval_key + ".json")
            if path is not None and Path(path).resolve() != self.path:
                raise PilotBlocked("live_ledger_path_forbidden")
        else:
            if path is None:
                raise PilotBlocked("mock_ledger_path_required")
            self.path = Path(path).resolve()
        identity = {"mode": mode, "tariff": asdict(tariff), "limits": asdict(self.limits),
                    "input_bundle_sha256": input_bundle_sha256, "candidate_identity": candidate_identity,
                    "approval": self.approval if self._approval_scoped else None}
        identity_hash = canonical_hash(identity)
        self.approval_sha256 = canonical_hash(self.approval) if self._approval_scoped else None
        if self.path.resolve() != self.path:
            raise PilotBlocked("ledger_path_redirected")
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        if lock_path.resolve() != lock_path:
            raise PilotBlocked("ledger_path_redirected")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lockfile = lock_path.open("a+b")
        try:
            self._acquire_lock()
            if self.path.exists():
                self.ledger = json.loads(self.path.read_text(encoding="utf-8"))
                if self.ledger.get("schema") != "finite_pilot_ledger_v1" or self.ledger.get("identity_sha256") != identity_hash:
                    raise PilotBlocked("ledger_identity_mismatch")
                unresolved = any(row.get("status") in {"pending", "unknown", "failed"}
                                 for row in self.ledger["requests"])
                if unresolved:
                    self.ledger["stop_code"] = self.ledger.get("stop_code") or "pending_usage_or_billing_unknown"
                    self._save()
                # UTC elapsed includes downtime on reopen: restarting cannot
                # replenish the approved 90-second cumulative time window.
                now = datetime.now(timezone.utc)
                previous = self.ledger.get("last_seen_at")
                if previous and now < self._utc(previous):
                    self.ledger["stop_code"] = self.ledger.get("stop_code") or "clock_regressed"
                    self._save()
            else:
                self.ledger = {"schema": "finite_pilot_ledger_v1", "identity_sha256": identity_hash,
                               "mode": mode, "model": MODEL, "limits": asdict(self.limits),
                               "tariff_sha256": canonical_hash(asdict(tariff)),
                               "tariff_provenance_kind": tariff.provenance["kind"],
                               "approval_id": self.approval["approval_id"] if self._approval_scoped else None,
                               "approval_sha256": self.approval_sha256,
                               "approval_manifest": self.approval if self._approval_scoped else None,
                               "candidate_identity": candidate_identity, "input_bundle_sha256": input_bundle_sha256,
                               "started_at": None, "deadline_at": None, "last_seen_at": None,
                               "item_deadlines": {}, "elapsed_seconds": 0.0,
                               "requests": [], "stop_code": None}
                self._save()
            self._run_clock = time.monotonic()
            self._run_started_utc = datetime.now(timezone.utc)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _utc(value):
        if type(value) is not str:
            raise PilotBlocked("approval_missing_or_mismatched")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise PilotBlocked("approval_missing_or_mismatched") from None
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise PilotBlocked("approval_missing_or_mismatched")
        return result.astimezone(timezone.utc)

    def _validate_live_approval(self, bundle, candidate):
        a = self.approval
        fields = {"approval_id", "status", "project", "account_id", "project_id", "guild_id", "channel_id",
                  "config_path", "config_source_sha256", "model", "owner_evidence", "allowed_stages",
                  "input_bundle_sha256", "candidate_identity", "limits", "tariff_sha256", "expires_at",
                  "allowed_response_models", "account_binding_confirmed",
                  "background_knowledge_disabled", "delivery_mode"}
        if (not isinstance(a, dict) or not fields <= set(a) or a.get("status") != "approved"
                or a.get("project") != "Indeces" or a.get("model") != MODEL
                or any(type(a.get(key)) is not str or not _SAFE_ID.fullmatch(a[key])
                       for key in ("approval_id", "account_id", "project_id"))
                or any(type(a.get(key)) is not str or not re.fullmatch(r"[0-9]{1,32}", a[key])
                       for key in ("guild_id", "channel_id"))
                or not isinstance(a.get("owner_evidence"), str) or not a["owner_evidence"].strip()
                or a.get("allowed_stages") != ["reply"]
                or a.get("allowed_response_models") != [MODEL]
                or a.get("account_binding_confirmed") is not True
                or a.get("background_knowledge_disabled") is not True
                or a.get("delivery_mode") not in {"api", "discord"}
                or not isinstance(a.get("config_path"), str) or not Path(a["config_path"]).is_absolute()
                or any(type(a.get(key)) is not str or not re.fullmatch(r"[0-9a-f]{64}", a[key])
                       for key in ("config_source_sha256", "input_bundle_sha256", "candidate_identity", "tariff_sha256"))
                or not bundle or a.get("input_bundle_sha256") != bundle
                or not candidate or a.get("candidate_identity") != candidate
                or canonical_hash(a.get("limits")) != canonical_hash(asdict(self.limits))
                or a.get("tariff_sha256") != canonical_hash(asdict(self.tariff))):
            raise PilotBlocked("approval_missing_or_mismatched")
        config_path = Path(a["config_path"]).resolve()
        if self.mode == "live":
            try:
                config_path.relative_to(Path(r"D:\Indeces").resolve())
            except ValueError:
                raise PilotBlocked("approval_missing_or_mismatched") from None
        expires = self._utc(a["expires_at"])
        if expires <= datetime.now(timezone.utc):
            raise PilotBlocked("approval_expired")
        # Retain only declared, noncredential approval fields. An unrecognized
        # caller field never becomes a persistent credential/log sink.
        self.approval = json.loads(json.dumps({key: a[key] for key in fields}, ensure_ascii=False,
                                             allow_nan=False))
        self.approval["config_path"] = str(config_path)

    def _acquire_lock(self):
        self._lockfile.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                if self._lockfile.read(1) == b"":
                    self._lockfile.write(b"0")
                    self._lockfile.flush()
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise PilotBlocked("ledger_locked") from None
        else:
            import fcntl
            try:
                fcntl.flock(self._lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise PilotBlocked("ledger_locked") from None

    def _save(self):
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.ledger, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def _stop(self, code):
        if type(code) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            code = "invalid_stop_code"
        # The public halt operation cannot clear or replace the first sticky
        # stop reason, including an empty caller-provided reason.
        self.ledger["stop_code"] = self.ledger.get("stop_code") or code
        self._save()
        raise PilotBlocked(self.ledger["stop_code"])

    def _ensure_open(self):
        if self._lockfile is None:
            raise PilotBlocked("gate_closed")
        try:
            unchanged = (canonical_hash(asdict(self.limits)) == canonical_hash(self.ledger["limits"])
                         and canonical_hash(asdict(self.tariff)) == self.ledger["tariff_sha256"]
                         and (not self._approval_scoped or canonical_hash(self.approval)
                              == self.ledger["approval_sha256"]))
        except (TypeError, ValueError, KeyError):
            unchanged = False
        if not unchanged:
            self._stop("ledger_identity_mismatch")

    def require_ready_for_network(self):
        """Recheck current approval time before credentials/Gateway setup.

        This does not reserve a request or start a new time window. Transport
        admission still owns request, token, fee and endpoint quota checks.
        """
        with self._mutex:
            self._ensure_open()
            if self.ledger["stop_code"]:
                raise PilotBlocked(self.ledger["stop_code"])
            now = self._now()
            if (self.ledger.get("deadline_at") is not None
                    and (now >= self._utc(self.ledger["deadline_at"])
                         or self.ledger["elapsed_seconds"] >= self.limits.cumulative_seconds)):
                self._stop("cumulative_time_limit")
            generations = {row["item_id"] for row in self.ledger["requests"]
                           if row["kind"] == "generation"}
            for row in self.ledger["requests"]:
                if (row["kind"] == "count" and row["status"] == "completed"
                        and row["item_id"] not in generations
                        and now >= self._utc(self.ledger["item_deadlines"][row["item_id"]])):
                    self._stop("call_time_limit")
            self._save()
            return True

    def _now(self):
        wall = datetime.now(timezone.utc)
        previous = self.ledger.get("last_seen_at")
        if previous and wall < self._utc(previous):
            self._stop("clock_regressed")
        # A backwards wall clock cannot extend deadlines within this process.
        monotonic_now = self._run_started_utc + timedelta(seconds=max(0, time.monotonic() - self._run_clock))
        now = max(wall, monotonic_now)
        self.ledger["last_seen_at"] = wall.isoformat()
        if self._approval_scoped and now >= self._utc(self.approval["expires_at"]):
            self._stop("approval_expired")
        return now

    def admit(self, item_id, stage, endpoint, payload_sha256, *, prompt_sha256=None,
              max_output_tokens=None, timeout_seconds=None):
        with self._mutex:
            self._ensure_open()
            if self.ledger["stop_code"]:
                raise PilotBlocked(self.ledger["stop_code"])
            if stage != "reply":
                self._stop("stage_forbidden")
            if endpoint not in {COUNT_ENDPOINT, GENERATION_ENDPOINT}:
                self._stop("endpoint_forbidden")
            if (not isinstance(item_id, str) or not _SAFE_ID.fullmatch(item_id)
                    or not isinstance(payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)):
                self._stop("invalid_request_identity")
            if ((self.mode == "live" and prompt_sha256 is None)
                    or (prompt_sha256 is not None and (type(prompt_sha256) is not str
                        or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256)))):
                self._stop("invalid_prompt_identity")
            rows = self.ledger["requests"]
            if any(row["status"] == "pending" for row in rows):
                self._stop("request_already_pending")
            if any(row["status"] in {"unknown", "failed"} for row in rows):
                self._stop("pending_usage_or_billing_unknown")
            kind = "count" if endpoint == COUNT_ENDPOINT else "generation"
            if any(row["item_id"] == item_id and row["kind"] == kind for row in rows):
                self._stop("retry_forbidden")
            limit = self.limits.max_count_requests if kind == "count" else self.limits.max_generations
            if sum(row["kind"] == kind for row in rows) >= limit:
                self._stop("request_limit")
            counted = next((row for row in rows if row["item_id"] == item_id
                            and row["kind"] == "count" and row["status"] == "completed"), None)
            if kind == "generation":
                if counted is None:
                    self._stop("count_required")
                if counted["usage"]["input_tokens"] > self.limits.input_tokens:
                    self._stop("count_input_limit")
                if (prompt_sha256 is not None or counted.get("prompt_sha256") is not None):
                    if prompt_sha256 != counted.get("prompt_sha256"):
                        self._stop("count_prompt_mismatch")
                if max_output_tokens is None and self.mode == "live":
                    self._stop("output_limit")
                requested_output = self.limits.output_tokens if max_output_tokens is None else max_output_tokens
                if type(requested_output) is not int or not 1 <= requested_output <= self.limits.output_tokens:
                    self._stop("output_limit")
            else:
                requested_output = 0
            if len({row["item_id"] for row in rows} | {item_id}) > 2:
                self._stop("item_limit")
            reserved_input = self.limits.input_tokens if kind == "generation" else 0
            reserved_output = self.limits.output_tokens if kind == "generation" else 0
            charged_input = sum(row["usage"]["input_tokens"] if row.get("usage") is not None
                                else row.get("reserved_input_tokens", 0)
                                for row in rows if row["kind"] == "generation")
            charged_output = sum(row["usage"]["output_tokens"] if row.get("usage") is not None
                                 else row.get("reserved_output_tokens", 0)
                                 for row in rows if row["kind"] == "generation")
            if (charged_input + reserved_input > self.limits.cumulative_input_tokens
                    or charged_output + reserved_output > self.limits.cumulative_output_tokens):
                self._stop("cumulative_token_limit")
            requested_seconds = self.limits.call_seconds if timeout_seconds is None else timeout_seconds
            if (type(requested_seconds) not in (int, float) or not math.isfinite(requested_seconds)
                    or not 0 < requested_seconds <= self.limits.call_seconds):
                self._stop("call_time_limit")
            now = self._now()
            if self.ledger.get("started_at") is None:
                self.ledger["started_at"] = now.isoformat()
                self.ledger["deadline_at"] = (now + timedelta(seconds=self.limits.cumulative_seconds)).isoformat()
            cumulative_left = (self._utc(self.ledger["deadline_at"]) - now).total_seconds()
            if cumulative_left <= 0 or self.ledger["elapsed_seconds"] >= self.limits.cumulative_seconds:
                self._stop("cumulative_time_limit")
            if kind == "count":
                self.ledger["item_deadlines"][item_id] = (now + timedelta(seconds=self.limits.call_seconds)).isoformat()
            item_left = (self._utc(self.ledger["item_deadlines"][item_id]) - now).total_seconds()
            if item_left <= 0:
                self._stop("call_time_limit")
            reserved_seconds = min(requested_seconds, item_left, cumulative_left,
                                   self.limits.cumulative_seconds - self.ledger["elapsed_seconds"])
            if self._approval_scoped:
                reserved_seconds = min(reserved_seconds, (self._utc(self.approval["expires_at"]) - now).total_seconds())
            reserved = self.tariff.cost(kind, self.limits.input_tokens,
                                        self.limits.output_tokens if kind == "generation" else 0)
            spent = sum((money(row["actual_cost_usd"]) for row in rows if row.get("actual_cost_usd") is not None), Decimal(0))
            if spent + reserved > money(self.limits.hard_cap_usd):
                self._stop("money_cap")
            token = uuid.uuid4().hex
            rows.append({"reservation_id": token, "item_id": item_id, "stage": stage, "kind": kind,
                         "endpoint": endpoint, "payload_sha256": payload_sha256, "prompt_sha256": prompt_sha256,
                         "reserved_usd": format(reserved, "f"), "reserved_input_tokens": reserved_input,
                         "reserved_output_tokens": reserved_output, "max_output_tokens": requested_output,
                         "reserved_seconds": reserved_seconds,
                         "call_deadline_at": (now + timedelta(seconds=reserved_seconds)).isoformat(),
                         "status": "pending", "actual_cost_usd": None, "usage": None,
                         "elapsed_seconds": None, "cost_basis": None,
                         "released_usd": None, "admitted_at": now.isoformat(), "settled_at": None})
            self._save()  # Token, money and time admission precede transport.
            self._request_clocks[token] = time.monotonic()
            return token

    def transport_timeout(self, token):
        with self._mutex:
            self._ensure_open()
            if self.ledger["stop_code"]:
                raise PilotBlocked(self.ledger["stop_code"])
            row = next((row for row in self.ledger["requests"] if row["reservation_id"] == token), None)
            if row is None or row["status"] != "pending":
                self._stop("invalid_request_identity")
            remaining = (self._utc(row["call_deadline_at"]) - self._now()).total_seconds()
            if remaining <= 0:
                self._stop("call_time_limit")
            return remaining

    def settle(self, token, *, status, usage=None, billing=None, elapsed_seconds=None):
        with self._mutex:
            self._ensure_open()
            if self.ledger["stop_code"]:
                raise PilotBlocked(self.ledger["stop_code"])
            row = next((row for row in self.ledger["requests"] if row["reservation_id"] == token), None)
            if row is None or row["status"] != "pending":
                self._stop("invalid_settlement")
            row["status"] = "unknown"
            received_at = datetime.now(timezone.utc)
            row["settled_at"] = received_at.isoformat()
            valid_usage = (isinstance(usage, dict) and set(usage) == {"input_tokens", "output_tokens"}
                           and all(type(usage[key]) is int and usage[key] >= 0 for key in usage))
            if valid_usage:
                row["usage"] = dict(usage)
            if not valid_usage or (row["kind"] == "count" and usage["output_tokens"] != 0):
                self._stop("usage_unknown")
            if (not isinstance(billing, dict) or billing.get("verified") is not True
                    or billing.get("status") != "known" or not isinstance(billing.get("source"), str)
                    or not billing["source"].strip()):
                self._stop("billing_unknown")
            try:
                actual = money(billing.get("amount_usd"))
            except PilotBlocked:
                self._stop("billing_unknown")
            row["actual_cost_usd"] = format(actual, "f")
            row["cost_basis"] = "approved_tariff_calculation" if self.mode == "live" else "synthetic_tariff_calculation"
            row["provider_invoice_verified"] = False
            expected = self.tariff.cost(row["kind"], usage["input_tokens"], usage["output_tokens"])
            if actual != expected:
                self._stop("billing_tariff_mismatch")
            if (usage["input_tokens"] > self.limits.input_tokens
                    or usage["output_tokens"] > row["max_output_tokens"]
                    or actual > money(row["reserved_usd"])):
                self._stop("reservation_breached")
            measured = max(0.0, (received_at - self._utc(row["admitted_at"])).total_seconds())
            if token in self._request_clocks:
                measured = max(measured, time.monotonic() - self._request_clocks[token])
            if elapsed_seconds is not None:
                if type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
                    self._stop("time_unknown")
                measured = max(measured, elapsed_seconds)
            row["elapsed_seconds"] = measured
            self.ledger["elapsed_seconds"] = sum(r["elapsed_seconds"] or 0.0 for r in self.ledger["requests"])
            # Preserve valid received usage and calculated fee before expiry,
            # clock-regression or deadline checks can stop the batch.
            now = self._now()
            if measured > row["reserved_seconds"] or now > self._utc(self.ledger["item_deadlines"][row["item_id"]]):
                self._stop("call_time_limit")
            if (self.ledger["elapsed_seconds"] > self.limits.cumulative_seconds
                    or now > self._utc(self.ledger["deadline_at"])):
                self._stop("cumulative_time_limit")
            totals = [sum(r["usage"][key] for r in self.ledger["requests"]
                          if r["kind"] == "generation" and r.get("usage") is not None)
                      for key in ("input_tokens", "output_tokens")]
            if totals[0] > self.limits.cumulative_input_tokens or totals[1] > self.limits.cumulative_output_tokens:
                self._stop("cumulative_token_limit")
            spent = sum((money(r["actual_cost_usd"]) for r in self.ledger["requests"]
                         if r.get("actual_cost_usd") is not None), Decimal(0))
            if spent > money(self.limits.hard_cap_usd):
                self._stop("money_cap_breached")
            row["released_usd"] = format(money(row["reserved_usd"]) - actual, "f")
            row["status"] = "completed" if status == "completed" else "failed"
            if status != "completed":
                self.ledger["stop_code"] = "request_failed_no_retry"
            self._save()
            if status != "completed":
                raise PilotBlocked("request_failed_no_retry")

    def halt(self, code):
        with self._mutex:
            self._ensure_open()
            self._stop(code)

    def close(self):
        with self._mutex:
            if self._lockfile is not None:
                self._lockfile.close()
                self._lockfile = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
