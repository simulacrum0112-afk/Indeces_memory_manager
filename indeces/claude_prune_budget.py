"""Finite native-prune accounting. New ledgers are explicit offline fixtures.

Live binding extends a caller-verified existing grant in place; it never creates
a live balance. Binding provenance remains private caller input. This module
performs no model calls, key access or semantic parsing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import uuid

from .contracts import GovernedError
from .ledger_io import _writer_lease
from .path_policy import validate_managed_path

MODEL = "claude-opus-5-5"
BASE_URL = "https://api.anthropic.com/v1"
CEILING = 5_000_000_000
# Native token counting is an estimate and can differ from billed input.
# Reserve the documented Opus 5.5 input/context ceiling financially, while the
# loop separately enforces the smaller configured execution/token limits.
# This does not change existing rows, ledger schema or the caller's price.
NATIVE_INPUT_RESERVATION = 1_000_000
_SESSION_SQL = "CREATE TABLE session(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, spent INTEGER NOT NULL CHECK(spent>=0))"
_ATTEMPT_SQL = "CREATE TABLE attempt(id INTEGER PRIMARY KEY CHECK(id=1), maximum INTEGER NOT NULL, status TEXT NOT NULL, count_intents INTEGER NOT NULL, generation_intents INTEGER NOT NULL, usage TEXT, charge INTEGER, wire TEXT)"
_META_SQL = "CREATE TABLE prune_meta(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, halted TEXT, legacy_digest TEXT)"
_ROUNDS_SQL = "CREATE TABLE prune_rounds(permit TEXT PRIMARY KEY, round_id TEXT NOT NULL UNIQUE, input_cap INTEGER NOT NULL, output_cap INTEGER NOT NULL, maximum INTEGER NOT NULL, status TEXT NOT NULL, count_intent INTEGER NOT NULL, count_received INTEGER NOT NULL, generation_intent INTEGER NOT NULL, end_success INTEGER NOT NULL, usage TEXT, charge INTEGER)"


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _identity(value):
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise ValueError("invalid budget identity")
    value.encode("utf-8")
    return value


def _error(code):
    return GovernedError("prune_budget_" + code, remote_usage_unknown=False)


@dataclass(frozen=True)
class PrunePriceEnvelope:
    """Caller-verified positive upper bounds, in USD nano-units per token.

    The caller must verify both rates cover the requested native model and
    Standard/global billing until valid_until. Count requests must be free;
    cached input, server tools and other billing features remain unsupported.
    Thinking tokens are already included in native output_tokens.
    """
    input_nusd_per_token: int
    output_nusd_per_token: int
    valid_until: str
    model: str = MODEL
    base_url: str = BASE_URL

    def __post_init__(self):
        if (any(type(v) is not int or not 0 < v <= CEILING for v in
                (self.input_nusd_per_token, self.output_nusd_per_token))
                or self.model != MODEL or self.base_url != BASE_URL):
            raise ValueError("trusted positive native price bounds required")
        try:
            expiry = datetime.fromisoformat(self.valid_until)
            if type(self.valid_until) is not str or expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise ValueError("price expiry must be an explicit UTC timestamp") from None

    def cost(self, inp, out):
        return inp * self.input_nusd_per_token + out * self.output_nusd_per_token


@dataclass(frozen=True, repr=False)
class ExistingGrantBinding:
    """Immutable provenance supplied by the owner of an existing grant.

    ledger_identity is the original file's (st_dev, st_ino), captured during
    read-only reconciliation. The policy and eight-column completed attempt
    are exact canonical SQLite values, not a new opening balance. A successful
    receipt must attest the same grant, charge, usage and generation wire hash.
    No paths, grant IDs or receipts are embedded in distributed application code.
    """
    ledger_path: Path
    ledger_identity: tuple
    grant_id: str
    policy_json: str
    completed_attempt: tuple
    opening_spent_nusd: int
    ceiling_nusd: int
    receipt_path: Path
    receipt_sha256: str

    def __post_init__(self):
        _identity(self.grant_id)
        if (not isinstance(self.ledger_path, Path) or not self.ledger_path.is_absolute()
                or not isinstance(self.receipt_path, Path) or not self.receipt_path.is_absolute()
                or type(self.ledger_identity) is not tuple or len(self.ledger_identity) != 2
                or any(type(v) is not int or v < 0 for v in self.ledger_identity)
                or type(self.opening_spent_nusd) is not int or type(self.ceiling_nusd) is not int
                or not 0 <= self.opening_spent_nusd <= self.ceiling_nusd <= CEILING
                or self.ceiling_nusd <= 0 or type(self.receipt_sha256) is not str
                or len(self.receipt_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.receipt_sha256)):
            raise ValueError("invalid existing grant binding")
        try:
            policy = json.loads(self.policy_json)
            attempt = self.completed_attempt
            if (type(self.policy_json) is not str or _json(policy) != self.policy_json
                    or type(policy) is not dict or policy.get("grant") != self.grant_id
                    or type(policy.get("ceiling_nusd")) is not int
                    or policy.get("ceiling_nusd") != self.ceiling_nusd
                    or type(attempt) is not tuple or len(attempt) != 8
                    or attempt[0] != 1 or type(attempt[0]) is not int
                    or type(attempt[1]) is not int or not self.opening_spent_nusd <= attempt[1] <= self.ceiling_nusd
                    or attempt[2:5] != ("completed", 1, 1)
                    or any(type(v) is not int for v in attempt[3:5])
                    or type(attempt[5]) is not str or _json(json.loads(attempt[5])) != attempt[5]
                    or type(attempt[6]) is not int or attempt[6] != self.opening_spent_nusd
                    or type(attempt[7]) is not str or len(attempt[7]) != 64
                    or any(c not in "0123456789abcdef" for c in attempt[7])):
                raise ValueError()
            self.policy_json.encode("utf-8")
            attempt[5].encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("exact canonical existing policy and completed attempt required") from None
        object.__setattr__(self, "ledger_path", self.ledger_path.resolve())
        object.__setattr__(self, "receipt_path", self.receipt_path.resolve())


def _caps(inp, out):
    if any(type(v) is not int or v <= 0 for v in (inp, out)) or inp + out > 1_048_576:
        raise _error("invalid_caps")


def _usage(native, known, inp_cap, out_cap, price):
    allowed = {"input_tokens", "output_tokens", "service_tier", "inference_geo",
               "cache_read_input_tokens", "cache_creation_input_tokens", "cache_creation",
               "server_tool_use", "output_tokens_details"}
    if (type(native) is not dict or set(native) - allowed or native.get("service_tier") != "standard"
            or native.get("inference_geo") != "global"):
        raise _error("unverified_billing")
    inp, out = native.get("input_tokens"), native.get("output_tokens")
    if (type(inp) is not int or type(out) is not int or not 0 <= inp <= inp_cap or not 0 <= out <= out_cap
            or type(known) is not dict or set(known) != {"input_tokens", "output_tokens"}
            or any(type(known[k]) is not int for k in known) or known != {"input_tokens": inp, "output_tokens": out}):
        raise _error("invalid_usage")
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key in native and (type(native[key]) is not int or native[key] != 0):
            raise _error("unsupported_billing_feature")
    for key, keys in (("cache_creation", {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}),
                      ("server_tool_use", {"web_search_requests", "web_fetch_requests"})):
        if key in native and (type(native[key]) is not dict or set(native[key]) - keys
                              or any(type(v) is not int or v != 0 for v in native[key].values())):
            raise _error("unsupported_billing_feature")
    if "output_tokens_details" in native:
        details = native["output_tokens_details"]
        if (type(details) is not dict or set(details) - {"thinking_tokens"}
                or any(type(v) is not int or not 0 <= v <= out for v in details.values())):
            raise _error("invalid_thinking_usage")
    frozen = _json(native)  # Reject nonfinite data/decoded surrogates before settlement.
    frozen.encode("utf-8")
    return frozen, price.cost(inp, out)


class PruneBudget:
    def __init__(self, path, grant_id, spent_nusd=0, ceiling_nusd=CEILING, offline_only=True, *, price):
        if offline_only is not True or not _identity(grant_id).startswith("offline_fixture:"):
            raise ValueError("new ledgers require the explicit offline_fixture namespace")
        self._initialize(path, grant_id, spent_nusd, ceiling_nusd, original=False, price=price)

    @classmethod
    def bind_existing_grant(cls, binding, *, price, offline_accepted=False):
        """Explicit migration, only called by the reviewed root after offline gates.

        Original path/file identity and successful receipt prevent reset/cloning
        relative to the supplied owner reconciliation.
        The supplied reconciliation must match the existing spent column;
        that column receives every additional charge. Policy/attempt stay unchanged.
        """
        if offline_accepted is not True or type(binding) is not ExistingGrantBinding:
            raise ValueError("explicit existing-grant gate required")
        if type(price) is not PrunePriceEnvelope or datetime.now(timezone.utc) >= datetime.fromisoformat(price.valid_until):
            raise ValueError("a current trusted price envelope is required")
        receipt_path = validate_managed_path(binding.receipt_path, "existing grant receipt")
        receipt_bytes = receipt_path.read_bytes()
        if hashlib.sha256(receipt_bytes).hexdigest() != binding.receipt_sha256:
            raise ValueError("existing grant receipt changed")
        receipt = json.loads(receipt_bytes)
        attempt = binding.completed_attempt
        expected_receipt_attempt = dict(zip(("maximum_nusd", "status", "count_intents", "generation_intents",
            "usage", "charge_nusd", "generation_wire_sha256"), attempt[1:]))
        expected_receipt_attempt["usage"] = json.loads(attempt[5])
        ledger = receipt.get("ledger", {}) if type(receipt) is dict else {}
        if (type(receipt) is not dict or receipt.get("success") is not True or type(ledger) is not dict
                or ledger.get("grant") != binding.grant_id
                or type(ledger.get("spent_or_worst_unknown_nusd")) is not int
                or ledger["spent_or_worst_unknown_nusd"] != binding.opening_spent_nusd
                or type(ledger.get("pending_nusd")) is not int or ledger["pending_nusd"] != 0
                or _json(ledger.get("attempt")) != _json(expected_receipt_attempt)):
            raise ValueError("existing grant receipt rejected")
        result = cls.__new__(cls)
        result._initialize(binding.ledger_path, binding.grant_id, binding.opening_spent_nusd,
                           binding.ceiling_nusd, original=True, price=price, binding=binding)
        return result

    def _initialize(self, path, grant_id, opening, ceiling, *, original, price, binding=None):
        if type(opening) is not int or type(ceiling) is not int or not 0 <= opening <= ceiling <= CEILING or ceiling <= 0:
            raise ValueError("invalid original cumulative balance")
        self._path = validate_managed_path(path, "prune budget ledger")
        if type(price) is not PrunePriceEnvelope:
            raise ValueError("an explicit trusted price envelope is required")
        self._price, self._binding = price, binding
        self._grant, self._opening, self._ceiling, self._original = grant_id, opening, ceiling, original
        self._lock, self._closed, self._failed, self._db = threading.RLock(), False, None, None
        self._policy = _json({"schema": "native_prune_budget_v1", "grant": grant_id, "opening_nusd": opening,
            "ceiling_nusd": ceiling, "offline_only": not original, "model": MODEL, "base_url": BASE_URL,
            "input_nusd_per_token": price.input_nusd_per_token, "output_nusd_per_token": price.output_nusd_per_token,
            "count_fee_nusd": 0, "service_tier": "standard", "inference_geo": "global",
            "price_expiry": price.valid_until, "retries": 0})
        if not original:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        elif not self._path.is_file() or self._path != binding.ledger_path.resolve():
            raise ValueError("the existing original ledger is required")
        if original:
            metadata = self._path.stat()
            if (metadata.st_dev, metadata.st_ino) != binding.ledger_identity:
                raise ValueError("original ledger file identity changed")
        self._lease = _writer_lease(self._path)
        self._lease.__enter__()
        try:
            target = self._path.as_uri() + "?mode=rw" if original else str(self._path)
            self._db = sqlite3.connect(target, timeout=0.0, isolation_level=None, check_same_thread=False, uri=original)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("BEGIN IMMEDIATE")
            objects = self._objects()
            names = {row[1] for row in objects}
            base = {"session", "attempt"} if original else set()
            existing = {"session", "prune_meta", "prune_rounds"} | ({"attempt"} if original else set())
            if names and names not in (base, existing):
                raise _error("schema_changed")
            if original:
                self._legacy = self._check_legacy()
            else:
                self._legacy = None
            if "prune_meta" not in names:
                if not original:
                    self._db.execute(_SESSION_SQL)
                    self._db.execute("INSERT INTO session VALUES(1,?,?)", (self._policy, opening))
                self._db.execute(_META_SQL)
                self._db.execute(_ROUNDS_SQL)
                self._db.execute("INSERT INTO prune_meta VALUES(1,?,NULL,?)", (self._policy, self._legacy))
            if self._db.execute("SELECT 1 FROM prune_rounds WHERE status IN ('reserved','unknown','known') LIMIT 1").fetchone():
                self._db.execute("UPDATE prune_meta SET halted=COALESCE(halted,'recovery_pending') WHERE id=1")
            self._validate()
            self._digest = self._state_digest()
            metadata = self._path.stat()
            self._file_identity = (metadata.st_dev, metadata.st_ino)
            if original and self._file_identity != binding.ledger_identity:
                raise _error("state_changed")
            self._pins = self._invariants()
            self._db.execute("COMMIT")
        except BaseException:
            if self._db is not None:
                self._db.close()
            self._lease.__exit__(*sys.exc_info())
            self._closed = True
            raise

    existing_grant_bound = property(lambda self: self._original)
    bound_path = property(lambda self: self._binding.ledger_path.resolve() if self._binding is not None else None)
    path = property(lambda self: self._path)

    def _invariants(self):
        price = self._price
        binding = self._binding
        original = None if binding is None else (
            binding.ledger_path, binding.ledger_identity, binding.grant_id, binding.policy_json,
            binding.completed_attempt, binding.opening_spent_nusd, binding.ceiling_nusd,
            binding.receipt_path, binding.receipt_sha256)
        return (MODEL, BASE_URL, CEILING, price.input_nusd_per_token, price.output_nusd_per_token,
                price.valid_until, price.model, price.base_url, original)

    def _objects(self):
        return self._db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()

    def _check_legacy(self):
        rows = self._db.execute("SELECT * FROM attempt").fetchall()
        expected = self._binding.completed_attempt
        session = self._db.execute("SELECT id,policy,spent FROM session").fetchall()
        if rows != [expected] or len(session) != 1 or session[0][:2] != (1, self._binding.policy_json):
            raise _error("original_ledger_changed")
        if "prune_meta" not in {r[1] for r in self._objects()} and session[0][2] != self._opening:
            raise _error("original_balance_changed")
        return hashlib.sha256(_json(rows).encode()).hexdigest()

    def _state_digest(self):
        names = ["session", "prune_meta", "prune_rounds"] + (["attempt"] if self._original else [])
        return hashlib.sha256(_json([self._objects()] + [self._db.execute("SELECT * FROM " + n + " ORDER BY rowid").fetchall()
                                                        for n in names]).encode()).hexdigest()

    def _validate(self):
        schema = [("table", "prune_meta", _META_SQL), ("table", "prune_rounds", _ROUNDS_SQL),
                  ("table", "session", _SESSION_SQL)]
        if self._original:
            schema.insert(0, ("table", "attempt", _ATTEMPT_SQL))
        if self._objects() != schema:
            raise _error("schema_changed")
        if self._db.execute("SELECT id,policy,legacy_digest FROM prune_meta").fetchall() != [(1, self._policy, self._legacy)]:
            raise _error("policy_changed")
        session = self._db.execute("SELECT id,policy,spent FROM session").fetchall()
        if len(session) != 1 or session[0][:2] != (1, self._binding.policy_json if self._original else self._policy):
            raise _error("policy_changed")
        if self._original and hashlib.sha256(_json(self._db.execute("SELECT * FROM attempt").fetchall()).encode()).hexdigest() != self._legacy:
            raise _error("original_attempt_changed")
        total, pending = self._opening, 0
        for row in self._db.execute("SELECT * FROM prune_rounds"):
            permit, round_id, inp, out, maximum, status, ci, cr, gi, end, usage, charge = row
            _identity(permit); _identity(round_id); _caps(inp, out)
            if (maximum != self._price.cost(inp, out) or status not in {"reserved", "unknown", "known", "completed", "failed_known"}
                    or any(type(v) is not int or v not in {0, 1} for v in (ci, cr, gi, end)) or not end <= gi <= cr <= ci):
                raise _error("accounting_changed")
            if usage is None:
                if charge is not None or status not in {"reserved", "unknown"} or end:
                    raise _error("accounting_changed")
                pending += maximum
            else:
                native = json.loads(usage)
                frozen, paid = _usage(native, {k: native[k] for k in ("input_tokens", "output_tokens")}, inp, out, self._price)
                if usage != frozen or type(charge) is not int or charge != paid or not gi or status not in {"known", "completed", "failed_known"}:
                    raise _error("accounting_changed")
                total += charge
        if type(session[0][2]) is not int or session[0][2] != total or total + pending > self._ceiling:
            raise _error("balance_changed")

    def _run(self, action, *, admission=False, expiry=True):
        with self._lock:
            if self._closed or self._failed:
                raise _error(self._failed or "closed")
            drift = False
            try:
                metadata = validate_managed_path(self._path, "prune budget ledger").stat()
                if ((metadata.st_dev, metadata.st_ino) != self._file_identity
                        or self._invariants() != self._pins):
                    drift = True
                    raise _error("state_changed")
                self._db.execute("BEGIN IMMEDIATE")
                if self._state_digest() != self._digest:
                    drift = True
                    raise _error("state_changed")
                self._validate()
                if expiry and datetime.now(timezone.utc) >= datetime.fromisoformat(self._price.valid_until):
                    raise _error("price_expired")
                halted = self._db.execute("SELECT halted FROM prune_meta WHERE id=1").fetchone()[0]
                if admission and halted is not None:
                    raise _error("halted")
                result = action()
                self._validate()
                digest = self._state_digest()
                self._db.execute("COMMIT")
                self._digest = digest
                return result
            except BaseException as error:
                self._failed = "state_changed" if drift else None
                try:
                    self._db.execute("ROLLBACK")
                    if not drift:
                        self._db.execute("BEGIN IMMEDIATE")
                        self._db.execute("UPDATE prune_meta SET halted=COALESCE(halted,?) WHERE id=1",
                                         (getattr(error, "code", "ledger_io_failure"),))
                        self._db.execute("COMMIT")
                        self._digest = self._state_digest()
                except BaseException:
                    self._failed = "ledger_io_failure"
                raise

    def reserve(self, round_id, input_cap, output_cap):
        def action():
            _identity(round_id); _caps(input_cap, output_cap)
            if self._db.execute("SELECT 1 FROM prune_rounds WHERE round_id=?", (round_id,)).fetchone():
                raise _error("duplicate_round")
            spent = self._db.execute("SELECT spent FROM session WHERE id=1").fetchone()[0]
            pending = self._db.execute("SELECT COALESCE(SUM(maximum),0) FROM prune_rounds WHERE usage IS NULL").fetchone()[0]
            maximum = self._price.cost(input_cap, output_cap)
            if spent + pending + maximum > self._ceiling:
                raise _error("exhausted")
            permit = uuid.uuid4().hex
            self._db.execute("INSERT INTO prune_rounds VALUES(?,?,?,?,?,'reserved',0,0,0,0,NULL,NULL)",
                             (permit, round_id, input_cap, output_cap, maximum))
            return permit
        return self._run(action, admission=True)

    def _round(self, permit):
        row = self._db.execute("SELECT * FROM prune_rounds WHERE permit=?", (_identity(permit),)).fetchone()
        if row is None:
            raise _error("unknown_permit")
        return row

    def observe(self, permit, event, fields):
        def action():
            # Freeze within the failure boundary so malformed data also halts.
            frozen = json.loads(_json(fields))
            row = self._round(permit)
            _, _, inp, out, _, status, ci, cr, gi, end, usage, _ = row
            if status not in {"reserved", "known"} or type(frozen) is not dict:
                raise _error("terminal_or_invalid_event")
            if event not in {"request_intent", "response_received", "response_headers", "call_start", "call_end"}:
                raise _error("invalid_event")
            if (frozen.get("provider") != "anthropic" or frozen.get("protocol") != "messages"
                    or frozen.get("base_url") != BASE_URL or frozen.get("api_version", "2023-06-01") != "2023-06-01"):
                raise _error("provider_mismatch")
            path = frozen.get("path")
            if event == "call_start":
                if frozen.get("model") != MODEL:
                    raise _error("model_mismatch")
            elif event == "call_end":
                if status != "known" or end or frozen.get("status") != "completed":
                    raise _error("unsuccessful_call_end")
                self._db.execute("UPDATE prune_rounds SET end_success=1 WHERE permit=?", (permit,))
            elif path not in {"/messages/count_tokens", "/messages"}:
                raise _error("invalid_path")
            elif event == "request_intent":
                if path == "/messages/count_tokens" and status == "reserved" and (ci, cr, gi) == (0, 0, 0):
                    self._db.execute("UPDATE prune_rounds SET count_intent=1 WHERE permit=?", (permit,))
                elif path == "/messages" and status == "reserved" and (ci, cr, gi) == (1, 1, 0):
                    self._db.execute("UPDATE prune_rounds SET generation_intent=1 WHERE permit=?", (permit,))
                else:
                    raise _error("duplicate_or_unordered_intent")
            elif event == "response_received":
                if path == "/messages/count_tokens":
                    tokens = frozen.get("counted_input_tokens")
                    if status != "reserved" or (ci, cr, gi) != (1, 0, 0) or type(tokens) is not int or not 0 <= tokens <= inp:
                        raise _error("invalid_count_receipt")
                    self._db.execute("UPDATE prune_rounds SET count_received=1 WHERE permit=?", (permit,))
                else:
                    if status != "reserved" or not gi or usage is not None or frozen.get("response_model") != MODEL:
                        raise _error("unverified_response_model")
                    _identity(frozen.get("response_id"))
                    usage_json, charge = _usage(frozen.get("native_usage"), frozen.get("known_usage"), inp, out, self._price)
                    self._db.execute("UPDATE session SET spent=spent+? WHERE id=1", (charge,))
                    self._db.execute("UPDATE prune_rounds SET status='known',usage=?,charge=? WHERE permit=?", (usage_json, charge, permit))
        new_admission = type(event) is str and event in {"call_start", "request_intent"}
        return self._run(action, admission=new_admission, expiry=new_admission)

    def complete(self, permit):
        def action():
            row = self._round(permit)
            if row[5] != "known" or row[9] != 1:
                raise _error("known_success_required")
            self._db.execute("UPDATE prune_rounds SET status='completed' WHERE permit=?", (permit,))
        return self._run(action, expiry=False)

    def fail(self, permit, error):
        def action():
            row = self._round(permit)
            status = "failed_known" if row[10] is not None else "unknown"
            self._db.execute("UPDATE prune_rounds SET status=? WHERE permit=?", (status, permit))
            self._db.execute("UPDATE prune_meta SET halted=COALESCE(halted,?) WHERE id=1",
                             (str(getattr(error, "code", "round_failure"))[:256],))
        return self._run(action, expiry=False)

    def halt(self, code):
        def action():
            self._db.execute("UPDATE prune_meta SET halted=COALESCE(halted,?) WHERE id=1", (_identity(code),))
        return self._run(action, expiry=False)

    def snapshot(self):
        def action():
            spent = self._db.execute("SELECT spent FROM session WHERE id=1").fetchone()[0]
            pending = self._db.execute("SELECT COALESCE(SUM(maximum),0) FROM prune_rounds WHERE usage IS NULL").fetchone()[0]
            rows = self._db.execute("SELECT round_id,maximum,status,charge,usage FROM prune_rounds ORDER BY rowid").fetchall()
            return {"grant_id": self._grant, "existing_grant_bound": self._original, "ceiling_nusd": self._ceiling,
                    "spent_nusd": spent, "pending_nusd": pending, "remaining_nusd": self._ceiling - spent - pending,
                    "halted": self._db.execute("SELECT halted FROM prune_meta WHERE id=1").fetchone()[0],
                    "rounds": [{"round_id": r[0], "maximum_nusd": r[1], "status": r[2], "charge_nusd": r[3],
                                "native_usage": json.loads(r[4]) if r[4] is not None else None} for r in rows]}
        return self._run(action, expiry=False)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._db.close()
            finally:
                self._lease.__exit__(None, None, None)
