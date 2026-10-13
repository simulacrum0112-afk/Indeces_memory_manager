"""Opt-in whole-session admission for a finite selection/reply trial.

Metadata only; no factory, credentials, live calls or automatic retries.
Owner book: Bojie Li, AI Agents in Depth v2.0 September23 2026, A4/A5
pp127-129/PDF135-137, E1 pp232-233/PDF240-241, rollout pp258-259/PDF266-267.
Configured upper tariffs are assumptions, not verified provider invoices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import threading
from urllib.parse import urlsplit
import uuid

from .adapter import OpenAIAdapter
from .config import Budget
from .contracts import GovernedError, validated_token_usage
from .ledger_io import _writer_lease

SCALE = 1_000_000_000


def _decimal(text, *, positive=False):
    if type(text) is not str or len(text) > 64:
        raise ValueError("money must be a decimal string")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("invalid monetary upper bound") from None
    if (not value.is_finite() or value < 0 or positive and value <= 0 or value > 10000
            or value != 0 and value < Decimal("1e-18")):
        raise ValueError("invalid monetary upper bound")
    return value


def _money(value):
    return str(Decimal(value) / SCALE)


def _identity(value):
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise ValueError("invalid session identity")
    value.encode("utf-8")
    return value


@dataclass(frozen=True)
class SessionPriceEnvelope:
    model: str
    base_url: str
    input_usd_per_million: str
    output_usd_per_million: str
    per_call_usd: str
    reference: str
    valid_until_utc: str

    def __post_init__(self):
        _identity(self.model)
        _identity(self.reference)
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid public provider URL")
        _decimal(self.input_usd_per_million, positive=True)
        _decimal(self.output_usd_per_million, positive=True)
        _decimal(self.per_call_usd)
        expiry = datetime.fromisoformat(self.valid_until_utc)
        if expiry.tzinfo is None or expiry.utcoffset().total_seconds() != 0:
            raise ValueError("price expiry must be UTC")

    def expired(self):
        return datetime.now(timezone.utc) >= datetime.fromisoformat(self.valid_until_utc)

    def cost(self, input_tokens, output_tokens):
        with localcontext() as context:
            context.prec = 128
            value = (_decimal(self.input_usd_per_million) * input_tokens / 1_000_000
                     + _decimal(self.output_usd_per_million) * output_tokens / 1_000_000
                     + _decimal(self.per_call_usd)) * SCALE
            return int(value.to_integral_value(rounding=ROUND_CEILING))


class SessionBudgetLedger:
    """One lifetime writer owner; all participant guards share this object.

    Existing kernel/process ownership prevents two independent budget copies.
    SQLite FULL commits plus a short thread lock protect every reservation.
    Recovery never frees an unfinished call, even before transport intent.
    """
    def __init__(self, path, *, session_id, envelope, ceiling_usd="5.00"):
        if type(envelope) is not SessionPriceEnvelope:
            raise ValueError("an explicit price envelope is required")
        ceiling = _decimal(ceiling_usd, positive=True)
        with localcontext() as context:
            context.prec = 128
            scaled_ceiling = ceiling * SCALE
            if ceiling > 5 or scaled_ceiling != scaled_ceiling.to_integral_value():
                raise ValueError("session ceiling must be exact and at most USD5")
            ceiling_nusd = int(scaled_ceiling)
        self._path, self._session_id, self._envelope = Path(path), _identity(session_id), envelope
        self._ceiling = ceiling_nusd
        self._lock, self._closed, self._io_failed = threading.RLock(), False, False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not stat.S_ISREG(self.path.stat(follow_symlinks=False).st_mode):
            raise ValueError("session ledger must be a regular file")
        self._lease = _writer_lease(self.path)
        self._lease.__enter__()
        policy = json.dumps({"schema":"controlled_session_budget_v1", "session_id":self.session_id,
                             "ceiling_nusd":self.ceiling,"prices":asdict(envelope)},sort_keys=True)
        self._policy = policy
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, spent INTEGER NOT NULL CHECK(spent>=0), halted TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY)")
                db.execute("CREATE TABLE IF NOT EXISTS calls (reservation_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, stage TEXT NOT NULL, max_cost INTEGER NOT NULL CHECK(max_cost>=0), max_input INTEGER NOT NULL, max_output INTEGER NOT NULL, usage TEXT, status TEXT NOT NULL, input_intents INTEGER NOT NULL DEFAULT 0, generation_intents INTEGER NOT NULL DEFAULT 0, provider_call_id TEXT, UNIQUE(turn_id,stage))")
                row = db.execute("SELECT policy FROM session WHERE id=1").fetchone()
                if row is None:
                    db.execute("INSERT INTO session VALUES (1,?,0,NULL)",(policy,))
                elif row[0] != policy:
                    raise GovernedError("session_policy_mismatch",remote_usage_unknown=False)
                if db.execute("SELECT 1 FROM calls WHERE status NOT IN ('completed','failed') LIMIT 1").fetchone():
                    self._halt(db,"session_recovery_pending")
                self._validate_accounting(db)
                expected_digest = self._state_digest(db)
            self._expected_digest = expected_digest
        except BaseException:
            self._lease.__exit__(None,None,None)
            self._closed = True
            raise

    path = property(lambda self: self._path)
    session_id = property(lambda self: self._session_id)
    envelope = property(lambda self: self._envelope)
    ceiling = property(lambda self: self._ceiling)

    @staticmethod
    def _state_digest(db):
        rows = [db.execute("SELECT * FROM "+table+" ORDER BY rowid").fetchall()
                for table in ("session","turns","calls")]
        rows.append(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name").fetchall())
        return hashlib.sha256(json.dumps(rows,sort_keys=True,allow_nan=False).encode()).hexdigest()

    def _validate_accounting(self, db):
        schema = db.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        if schema != [('table','calls'),('table','session'),('table','turns')]:
            raise ValueError("unexpected session ledger schema")
        spent = db.execute("SELECT spent FROM session WHERE id=1").fetchone()[0]
        total = 0
        for maximum,max_in,max_out,usage_json,status,input_intents,generation_intents in db.execute(
                "SELECT max_cost,max_input,max_output,usage,status,input_intents,generation_intents FROM calls"):
            if (any(type(value) is not int for value in (maximum,max_in,max_out,input_intents,generation_intents))
                    or min(max_in,max_out) <= 0 or max_in+max_out > 1_050_000
                    or maximum != self.envelope.cost(max_in,max_out)
                    or status not in {'reserved','known','completed','failed','unknown'}
                    or input_intents not in {0,1} or generation_intents not in {0,1}
                    or generation_intents > input_intents):
                raise ValueError("invalid session accounting")
            if usage_json is not None:
                usage = json.loads(usage_json)
                if validated_token_usage(usage) != usage or not generation_intents:
                    raise ValueError("invalid session usage")
                total += self.envelope.cost(usage['input_tokens'],usage['output_tokens'])
        if type(spent) is not int or spent != total:
            raise ValueError("session cost reconciliation failed")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path,timeout=0.0)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _halt(db, code):
        db.execute("UPDATE session SET halted=COALESCE(halted,?) WHERE id=1",(code,))

    def _run(self, operation):
        with self._lock:
            if self._closed or self._io_failed:
                raise GovernedError("session_budget_io_failed",remote_usage_unknown=False)
            try:
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT policy FROM session WHERE id=1").fetchone()
                    if row is None or row[0] != self._policy:
                        self._io_failed = True
                        raise GovernedError("session_policy_mismatch",remote_usage_unknown=False)
                    if self._state_digest(db) != self._expected_digest:
                        raise ValueError("session ledger changed outside its owner")
                    self._validate_accounting(db)
                    result = operation(db)
                    self._validate_accounting(db)
                    expected_digest = self._state_digest(db)
                self._expected_digest = expected_digest
            except (sqlite3.Error,OSError,ValueError,TypeError,OverflowError):
                self._io_failed = True
                raise GovernedError("session_budget_io_failed",remote_usage_unknown=False) from None
            if isinstance(result, BaseException):
                raise result
            return result

    def halt(self, code):
        self._run(lambda db: self._halt(db,_identity(code)))

    def _check(self, db):
        halted = db.execute("SELECT halted FROM session WHERE id=1").fetchone()[0]
        if halted:
            # First halt cause is provenance, not the aggregate usage state.
            unknown = db.execute("SELECT 1 FROM calls WHERE generation_intents>0 AND usage IS NULL LIMIT 1").fetchone() is not None
            return GovernedError("session_halted",remote_usage_unknown=unknown or halted in {"session_usage_unknown","session_recovery_pending"})
        if self.envelope.expired():
            self._halt(db,"session_price_expired")
            return GovernedError("session_price_expired",remote_usage_unknown=False)

    def begin_turn(self, turn_id):
        turn_id = _identity(turn_id)
        def operation(db):
            error = self._check(db)
            if error:
                return error
            if db.execute("SELECT 1 FROM turns WHERE turn_id=?",(turn_id,)).fetchone():
                return GovernedError("session_turn_already_recorded",remote_usage_unknown=False)
            db.execute("INSERT INTO turns VALUES (?)",(turn_id,))
        self._run(operation)

    def reserve(self, turn_id, stage, budget):
        if type(budget) is not Budget:
            raise ValueError("expected bounded Budget")
        turn_id = _identity(turn_id)
        def operation(db):
            error = self._check(db)
            if error:
                return error
            if stage not in {"selection","reply"}:
                self._halt(db,"session_stage_not_allowed")
                return GovernedError("session_stage_not_allowed",remote_usage_unknown=False)
            if not db.execute("SELECT 1 FROM turns WHERE turn_id=?",(turn_id,)).fetchone():
                return GovernedError("session_turn_not_admitted",remote_usage_unknown=False)
            prior = db.execute("SELECT stage,status FROM calls WHERE turn_id=?",(turn_id,)).fetchall()
            if any(row[0] == stage for row in prior):
                self._halt(db,"session_stage_already_recorded")
                return GovernedError("session_stage_already_recorded",remote_usage_unknown=False)
            if any(row[1] != "completed" for row in prior) or stage == "selection" and prior:
                self._halt(db,"session_stage_order")
                return GovernedError("session_stage_order",remote_usage_unknown=False)
            maximum = self.envelope.cost(budget.input_tokens,budget.output_tokens)
            spent = db.execute("SELECT spent FROM session WHERE id=1").fetchone()[0]
            pending = db.execute("SELECT COALESCE(SUM(max_cost),0) FROM calls WHERE usage IS NULL").fetchone()[0]
            if spent + pending + maximum > self.ceiling:
                self._halt(db,"session_cost_budget")
                return GovernedError("session_cost_budget",remote_usage_unknown=False)
            reservation_id = uuid.uuid4().hex
            db.execute("INSERT INTO calls(reservation_id,turn_id,stage,max_cost,max_input,max_output,status) VALUES (?,?,?,?,?,?,'reserved')",
                       (reservation_id,turn_id,stage,maximum,budget.input_tokens,budget.output_tokens))
            return reservation_id
        return self._run(operation)

    def audit(self, reservation_id, event, fields):
        if type(fields) is not dict:
            self.halt("session_call_contract")
            raise GovernedError("session_call_contract",remote_usage_unknown=False)
        def operation(db):
            row = db.execute("SELECT stage,max_cost,max_input,max_output,usage,status,input_intents,generation_intents,provider_call_id FROM calls WHERE reservation_id=?",(reservation_id,)).fetchone()
            if row is None:
                raise GovernedError("session_reservation_missing",remote_usage_unknown=False)
            stage,maximum,max_in,max_out,usage_json,status,input_intents,generation_intents,provider_id = row
            if event not in {"call_start","request_intent","response_headers","response_received","input_gate","call_end"}:
                self._halt(db,"session_call_contract")
                return GovernedError("session_call_contract",remote_usage_unknown=bool(generation_intents and usage_json is None))
            if event in {"call_start","request_intent"}:
                error = self._check(db)
                if error:
                    return error
            if event == "call_start":
                budget = fields.get("budget",{})
                call_id = fields.get("call_id")
                if (type(budget) is not dict or type(call_id) is not str or len(call_id) != 32 or any(c not in "0123456789abcdef" for c in call_id)
                        or provider_id is not None or fields.get("stage") != stage or fields.get("model") != self.envelope.model
                        or type(budget.get("input_tokens")) is not int or type(budget.get("output_tokens")) is not int
                        or not 0 < budget["input_tokens"] <= max_in or not 0 < budget["output_tokens"] <= max_out):
                    self._halt(db,"session_call_contract")
                    return GovernedError("session_call_contract",remote_usage_unknown=False)
                db.execute("UPDATE calls SET provider_call_id=? WHERE reservation_id=?",(fields["call_id"],reservation_id))
            elif provider_id is None or fields.get("call_id") != provider_id:
                self._halt(db,"session_call_contract")
                return GovernedError("session_call_contract",remote_usage_unknown=False)
            if event == "request_intent":
                path = fields.get("path")
                if (path == "/responses/input_tokens" and input_intents == 0 and generation_intents == 0):
                    db.execute("UPDATE calls SET input_intents=1 WHERE reservation_id=?",(reservation_id,))
                elif path == "/responses" and input_intents == 1 and generation_intents == 0:
                    db.execute("UPDATE calls SET generation_intents=1 WHERE reservation_id=?",(reservation_id,))
                    generation_intents = 1
                else:
                    self._halt(db,"session_transport_retry_forbidden")
                    return GovernedError("session_transport_retry_forbidden",remote_usage_unknown=bool(generation_intents))
            usage = validated_token_usage(fields.get("known_usage"))
            if usage is not None:
                if not generation_intents or not (event == "call_end" or event == "response_received" and fields.get("path") == "/responses"):
                    self._halt(db,"session_call_contract")
                    return GovernedError("session_call_contract",remote_usage_unknown=bool(generation_intents and usage_json is None))
                encoded = json.dumps(usage,sort_keys=True)
                if usage_json is not None and usage_json != encoded:
                    self._halt(db,"session_usage_conflict")
                    return GovernedError("session_usage_conflict",remote_usage_unknown=False,known_usage=usage)
                if usage_json is None:
                    amount = self.envelope.cost(usage["input_tokens"],usage["output_tokens"])
                    db.execute("UPDATE session SET spent=spent+? WHERE id=1",(amount,))
                    db.execute("UPDATE calls SET usage=?,status='known' WHERE reservation_id=?",(encoded,reservation_id))
                    if usage["input_tokens"] > max_in or usage["output_tokens"] > max_out or amount > maximum:
                        self._halt(db,"session_provider_budget_breach")
            elif event == "response_received" and fields.get("path") == "/responses":
                self._halt(db,"session_usage_unknown")
            if event == "call_end":
                known = usage_json is not None or usage is not None
                if fields.get("remote_usage_unknown") or generation_intents and not known:
                    self._halt(db,"session_usage_unknown")
                    db.execute("UPDATE calls SET status='unknown' WHERE reservation_id=?",(reservation_id,))
                elif fields.get("status") == "completed" and known:
                    db.execute("UPDATE calls SET status='completed' WHERE reservation_id=?",(reservation_id,))
                else:
                    self._halt(db,"session_call_failed")
                    db.execute("UPDATE calls SET status='failed' WHERE reservation_id=?",(reservation_id,))
        self._run(operation)

    def fail(self, reservation_id, error):
        def operation(db):
            row = db.execute("SELECT usage,generation_intents FROM calls WHERE reservation_id=?",(reservation_id,)).fetchone()
            if row is None:
                raise GovernedError("session_reservation_missing",remote_usage_unknown=False)
            unknown = bool(row[0] is None and (getattr(error,"remote_usage_unknown",False) or row[1]))
            self._halt(db,"session_usage_unknown" if unknown else "session_call_failed")
            db.execute("UPDATE calls SET status=? WHERE reservation_id=?",("unknown" if unknown else "failed",reservation_id))
        self._run(operation)

    def snapshot(self):
        def operation(db):
            spent,halted = db.execute("SELECT spent,halted FROM session WHERE id=1").fetchone()
            columns = ["reservation_id","turn_id","stage","max_cost","max_input","max_output","usage","status","input_intents","generation_intents","provider_call_id"]
            calls = [dict(zip(columns,row)) for row in db.execute("SELECT * FROM calls ORDER BY rowid")]
            pending = sum(row["max_cost"] for row in calls if row["usage"] is None)
            for row in calls:
                row["reserved_cost_usd"] = _money(row.pop("max_cost"))
                row["usage"] = json.loads(row["usage"]) if row["usage"] is not None else None
            return {"schema":"controlled_session_budget_v1","session_id":self.session_id,
                    "ceiling_usd":_money(self.ceiling),"spent_usd":_money(spent),
                    "reserved_usd":_money(pending),"liability_usd":_money(spent+pending),
                    "halted":halted,"calls":calls,
                    "turns":[row[0] for row in db.execute("SELECT turn_id FROM turns ORDER BY rowid")],
                    "cost_basis":"configured_upper_tariffs_not_invoice","automatic_retries":0}
        return self._run(operation)

    def close(self):
        if self._closed:
            return
        with self._lock:
            try:
                if not self._io_failed:
                    self._run(lambda db: self._halt(db,"session_recovery_pending") if db.execute("SELECT 1 FROM calls WHERE status NOT IN ('completed','failed') LIMIT 1").fetchone() else None)
            finally:
                self._closed = True
                self._lease.__exit__(None,None,None)


class SessionBudgetAdapter:
    """Factory opt-in only; do not expose the base transport to trial callers."""
    def __init__(self, adapter, ledger):
        if type(adapter) is not OpenAIAdapter or type(ledger) is not SessionBudgetLedger:
            raise ValueError("requires the audited native adapter and shared session ledger")
        self.base,self.ledger,self.config = adapter,ledger,adapter.config
        if self.config.model != ledger.envelope.model or self.config.base_url != ledger.envelope.base_url:
            raise GovernedError("session_policy_mismatch",remote_usage_unknown=False)
        self._config_digest = self._digest()

    def _digest(self):
        return hashlib.sha256(json.dumps(asdict(self.base.config),sort_keys=True,allow_nan=False).encode()).hexdigest()

    def check_config(self):
        try:
            valid = self.base.config is self.config and self._digest() == self._config_digest
        except Exception:
            valid = False
        if not valid:
            self.ledger.halt("session_policy_mismatch")
            raise GovernedError("session_policy_mismatch",remote_usage_unknown=False)

    def begin_turn(self, turn_id):
        self.check_config()
        self.ledger.begin_turn(turn_id)
        return _SessionTurnAdapter(self,turn_id)

    async def call(self, *args, **kwargs):
        self.ledger.halt("session_turn_required")
        raise GovernedError("session_turn_required",remote_usage_unknown=False)


class _SessionTurnAdapter:
    def __init__(self, owner, turn_id):
        self.owner,self.turn_id,self.config = owner,turn_id,owner.config

    def halt(self, code):
        self.owner.ledger.halt(code)

    def finish(self):
        state = self.owner.ledger.snapshot()
        calls = [row for row in state["calls"] if row["turn_id"] == self.turn_id]
        if any(row["status"] != "completed" for row in calls):
            self.owner.ledger.halt("session_turn_incomplete")

    async def call(self, stage, instructions, messages, trace_id, schema=None, **kwargs):
        owner = self.owner
        owner.check_config()
        if stage not in self.config.budgets:
            owner.ledger.halt("session_stage_not_allowed")
            raise GovernedError("session_stage_not_allowed",remote_usage_unknown=False)
        if set(kwargs) - {"input_limit","budget_override","audit"}:
            owner.ledger.halt("session_call_contract")
            raise GovernedError("session_call_contract",remote_usage_unknown=False)
        external = kwargs.pop("audit",None)
        if external is not None and not callable(external):
            owner.ledger.halt("invalid_call_audit")
            raise GovernedError("invalid_call_audit",remote_usage_unknown=False)
        permit = owner.ledger.reserve(self.turn_id,stage,self.config.budgets[stage])
        def audit(event, **fields):
            if event in {"call_start","request_intent"}:
                owner.check_config()
            owner.ledger.audit(permit,event,fields)
            if external is not None:
                OpenAIAdapter._audit(external,event,**fields)
            if event in {"call_start","request_intent"}:
                owner.check_config()
        try:
            # One private snapshot feeds both count and generation. Caller
            # mutation during the count await cannot invalidate admission.
            frozen_messages, frozen_schema = deepcopy((messages,schema))
            result = await owner.base.call(stage,instructions,frozen_messages,trace_id,frozen_schema,audit=audit,**kwargs)
            state = owner.ledger.snapshot()
            if state["halted"]:
                unknown = any(row["generation_intents"] and row["usage"] is None for row in state["calls"])
                raise GovernedError("session_halted",remote_usage_unknown=unknown or state["halted"] == "session_usage_unknown",
                                    known_usage={"input_tokens":result.input_tokens,"output_tokens":result.output_tokens})
            return result
        except BaseException as error:
            try:
                owner.ledger.fail(permit,error)
            except BaseException:
                # The ledger's sticky local I/O latch forbids further calls.
                pass
            raise
