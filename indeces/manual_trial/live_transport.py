"""Opt-in gate around the existing OpenAIAdapter normal HTTP path.

No credential lookup, Console/Gateway startup, retry, or request-protocol
replacement lives here. Tests inject a normal-session double; the default
aiohttp session is created lazily only after explicit validated activation.
Cost receipts are approved-tariff calculations, not provider invoices.
Tariff/account provenance remains an operator assertion; synchronous trusted
callbacks and asyncio deadlines provide cooperative cancellation, not an OS
hard interruption or an independent guarantee about provider billing.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
import json
import math
import time

from .pilot_gate import (COUNT_ENDPOINT, GENERATION_ENDPOINT, MODEL, PilotBlocked,
                        account_scope, canonical_hash)


_ACTIVE_REPLY = ContextVar("manual_live_active_reply", default=None)
_BODY_LIMIT = 2 * 1024 * 1024
_COMMON = {"model", "instructions", "input", "reasoning", "text"}
_GENERATION_EXTRAS = {"max_output_tokens", "store", "stream", "truncation", "tools"}


def _halt(gate, code):
    gate.halt(code)
    raise PilotBlocked(code)  # Defensive for injected gate doubles.


def preflight_live_transport(adapter_config, gate, *, explicit_live=False, validated_descriptor=None):
    """Key-free validation for launcher/finite Gateway entry ordering."""
    descriptor = validated_descriptor
    if explicit_live is not True:
        raise PilotBlocked("live_transport_disabled")
    expected = {"model", "base_url", "allowed_response_models", "descriptor_sha256", "gate_identity_sha256"}
    if (not isinstance(descriptor, dict) or set(descriptor) != expected
            or getattr(gate, "mode", None) != "live"
            or descriptor["model"] != MODEL
            or descriptor["base_url"] != "https://api.openai.com/v1"
            or descriptor["allowed_response_models"] != [MODEL]
            or descriptor["descriptor_sha256"] != canonical_hash(getattr(gate, "approval", None))
            or descriptor["descriptor_sha256"] != getattr(gate, "approval_sha256", None)
            or descriptor["descriptor_sha256"] != gate.ledger.get("approval_sha256")
            or descriptor["gate_identity_sha256"] != gate.ledger.get("identity_sha256")):
        raise PilotBlocked("unverified_activation")
    if (adapter_config.model != descriptor["model"]
            or adapter_config.base_url.rstrip("/") != descriptor["base_url"]):
        raise PilotBlocked("normal_adapter_required")
    budget = adapter_config.budgets.get("reply")
    if budget is None or budget.reasoning != "medium" or adapter_config.verbosity != "high":
        raise PilotBlocked("model_parameters_mismatch")
    readiness = getattr(gate, "require_ready_for_network", None)
    if not callable(readiness):
        raise PilotBlocked("unverified_activation")
    # Hidden credential prompts may take time after gate construction. Recheck
    # live expiry and persisted clock/deadline state before credentials/Gateway.
    readiness()
    if getattr(gate, "_lockfile", True) is None:
        raise PilotBlocked("gate_closed")
    if gate.ledger.get("stop_code"):
        raise PilotBlocked(gate.ledger["stop_code"])
    rows = gate.ledger.get("requests", [])
    if (sum(row["kind"] == "count" for row in rows) >= gate.limits.max_count_requests
            or sum(row["kind"] == "generation" for row in rows) >= gate.limits.max_generations):
        raise PilotBlocked("request_limit")
    return {"ready": True, "descriptor_sha256": descriptor["descriptor_sha256"],
            "gate_identity_sha256": descriptor["gate_identity_sha256"],
            "cost_evidence": "approved_tariff_calculation_not_provider_invoice"}


def _activation(adapter, gate, explicit_live, descriptor):
    preflight_live_transport(adapter.config, gate, explicit_live=explicit_live,
                             validated_descriptor=descriptor)
    from indeces.adapter import OpenAIAdapter
    if (type(adapter) is not OpenAIAdapter
            or getattr(adapter, "_request_override", None) is not None
            or getattr(adapter, "_session", None) is not None):
        raise PilotBlocked("normal_adapter_required")
    # Existing private-input/opaque-key validator only; no environment or
    # credential file is consulted, and no key is copied into audit metadata.
    from indeces.credentials import validate_api_key
    try:
        validate_api_key(getattr(adapter, "_key", ""))
    except Exception:
        raise PilotBlocked("manual_key_missing_or_invalid") from None


@dataclass(frozen=True)
class _ReplyContext:
    item_id: str
    deadline: float
    output_tokens: int


class LiveReplyAdapter:
    def __init__(self, adapter, gate, descriptor):
        self.adapter, self.gate, self.item_id = adapter, gate, None
        self._descriptor = json.loads(json.dumps(descriptor))

    def __repr__(self):
        return "LiveReplyAdapter(credentials=<redacted>)"

    def require_ready_for_network(self):
        return preflight_live_transport(self.adapter.config, self.gate, explicit_live=True,
                                       validated_descriptor=self._descriptor)

    async def call(self, stage, *args, **kwargs):
        if stage != "reply" or not isinstance(self.item_id, str) or not self.item_id:
            _halt(self.gate, "stage_forbidden")
        from indeces.config import Budget
        configured = self.adapter.config.budgets["reply"]
        provided = kwargs.pop("budget_override", None)
        if provided is not None and (not isinstance(provided, Budget)
                or provided.input_tokens > configured.input_tokens
                or provided.output_tokens > configured.output_tokens
                or provided.seconds > configured.seconds or provided.reasoning != configured.reasoning):
            _halt(self.gate, "invalid_call_budget_override")
        budget = provided or configured
        if budget.reasoning != "medium" or self.adapter.config.verbosity != "high":
            _halt(self.gate, "model_parameters_mismatch")
        narrowed = Budget(min(budget.input_tokens, self.gate.limits.input_tokens),
                          min(budget.output_tokens, self.gate.limits.output_tokens),
                          min(budget.seconds, getattr(self.gate.limits, "call_seconds", 45)),
                          reasoning=budget.reasoning)
        token = _ACTIVE_REPLY.set(_ReplyContext(self.item_id, time.monotonic() + narrowed.seconds,
                                               narrowed.output_tokens))
        original_audit = kwargs.pop("audit", None)

        def audit(event, **fields):
            # The native adapter has saved the counted provider response before
            # input_gate. Stop here, before its generation request intent, if
            # the tee could not safely settle that response.
            if event == "input_gate":
                self._require_settlement()
            if original_audit is not None:
                return original_audit(event, **fields)

        try:
            try:
                # Serialization/counting/usage/output validation remain the
                # existing adapter's call implementation, including its audit.
                result = await self.adapter.call(stage, *args, budget_override=narrowed,
                                                 audit=audit, **kwargs)
                # A valid provider JSON response is retained by the native
                # scratch path even when its model/price cannot be verified.
                # No such output may pass this wrapper into Runtime delivery.
                self._require_settlement()
                return result
            except asyncio.CancelledError:
                if self.gate.ledger.get("stop_code") is None:
                    try:
                        _halt(self.gate, "reply_cancelled_no_retry")
                    except PilotBlocked:
                        pass
                raise
            except Exception:
                if self.gate.ledger.get("stop_code") is None:
                    _halt(self.gate, "reply_failed_no_retry")
                raise
        finally:
            _ACTIVE_REPLY.reset(token)

    def _require_settlement(self):
        failure = getattr(self.adapter._session, "_settlement_failure", None)
        stop = self.gate.ledger.get("stop_code")
        if failure or stop:
            if stop is None:
                _halt(self.gate, failure)
            raise PilotBlocked(stop)

    async def close(self):
        await self.adapter.close()


class GatedHTTPSession:
    def __init__(self, gate, descriptor, *, session_factory=None, billing_resolver=None):
        self.gate = gate
        self.descriptor = json.loads(json.dumps(descriptor))
        self._factory, self._session = session_factory, None
        self._resolver = billing_resolver or self._approved_tariff_billing
        self._counted = {}
        self._settlement_failure = None
        self.closed = False

    def __repr__(self):
        return "GatedHTTPSession(credentials=<redacted>)"

    def _underlying(self):
        if self._session is None:
            if self._factory is None:
                import aiohttp
                # Native OpenAIAdapter.call owns the full stage deadline.
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            else:
                self._session = self._factory()
            if asyncio.iscoroutine(self._session) or not callable(getattr(self._session, "post", None)):
                _halt(self.gate, "invalid_session_factory")
        return self._session

    def post(self, endpoint, **options):
        if self.closed:
            raise PilotBlocked("transport_closed")
        context = _ACTIVE_REPLY.get()
        if context is None:
            _halt(self.gate, "stage_forbidden")
        if (canonical_hash(self.gate.approval) != self.descriptor["descriptor_sha256"]
                or self.gate.approval_sha256 != self.descriptor["descriptor_sha256"]
                or self.gate.ledger.get("approval_sha256") != self.descriptor["descriptor_sha256"]
                or self.gate.ledger.get("identity_sha256") != self.descriptor["gate_identity_sha256"]):
            _halt(self.gate, "unverified_activation")
        if endpoint not in {COUNT_ENDPOINT, GENERATION_ENDPOINT}:
            _halt(self.gate, "endpoint_forbidden")
        if options.get("allow_redirects") is not False:
            _halt(self.gate, "redirects_forbidden")
        if set(options) != {"json", "headers", "allow_redirects"}:
            _halt(self.gate, "request_option_forbidden")
        if not isinstance(options["headers"], dict):
            _halt(self.gate, "request_option_forbidden")
        # Bind the admitted bytes to a snapshot, rather than to a mutable
        # caller-owned payload that could change before the lazy HTTP enter.
        try:
            payload = json.loads(json.dumps(options.get("json"), allow_nan=False))
        except (ValueError, TypeError):
            _halt(self.gate, "invalid_request_payload")
        headers = dict(options["headers"])
        if account_scope(self.gate.approval) == "existing_credential_default":
            # Preserve the current native Adapter's credential-default routing.
            # Reject injected routing, rather than guessing IDs or silently
            # replacing an owner-approved default scope with another project.
            if any(isinstance(key, str) and key.lower() in {"openai-organization", "openai-project"}
                   for key in headers):
                _halt(self.gate, "account_routing_conflict")
        else:
            # Explicit IDs are routing assertions, not local authentication of
            # the opaque key's billing owner. Legacy behavior is unchanged.
            for name, value in (("OpenAI-Organization", self.gate.approval["account_id"]),
                                ("OpenAI-Project", self.gate.approval["project_id"])):
                matches = [key for key in headers if isinstance(key, str) and key.lower() == name.lower()]
                if any(headers[key] != value for key in matches):
                    _halt(self.gate, "account_routing_conflict")
                for key in matches:
                    del headers[key]
                headers[name] = value
        options = dict(options, json=payload, headers=headers)
        expected = _COMMON if endpoint == COUNT_ENDPOINT else _COMMON | _GENERATION_EXTRAS
        if (not isinstance(payload, dict) or set(payload) != expected
                or payload.get("model") != self.descriptor["model"]
                or not isinstance(payload.get("instructions"), str)
                or not isinstance(payload.get("input"), list)
                or payload.get("reasoning") != {"effort": "medium"}
                or not isinstance(payload.get("text"), dict)
                or payload["text"].get("verbosity") != "high"
                or set(payload["text"]) not in ({"verbosity"}, {"verbosity", "format"})):
            _halt(self.gate, "invalid_request_payload")
        prompt_hash = canonical_hash({key: payload[key] for key in _COMMON})
        maximum = None
        if endpoint == GENERATION_ENDPOINT:
            maximum = payload["max_output_tokens"]
            if (type(maximum) is not int or not 1 <= maximum <= context.output_tokens
                    or payload["tools"] != [] or payload["store"] is not False
                    or payload["stream"] is not False or payload["truncation"] != "disabled"):
                _halt(self.gate, "request_option_forbidden")
            if self._counted.get(context.item_id) != prompt_hash:
                _halt(self.gate, "count_prompt_mismatch")
        remaining = context.deadline - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            _halt(self.gate, "request_time_limit")
        token = self.gate.admit(context.item_id, "reply", endpoint, canonical_hash(payload),
                                prompt_sha256=prompt_hash, max_output_tokens=maximum,
                                timeout_seconds=remaining)
        return _GatedResponseContext(self, endpoint, token, options, context.item_id, prompt_hash)

    def _approved_tariff_billing(self, endpoint, data, status, metadata):
        if endpoint == COUNT_ENDPOINT:
            usage = {"input_tokens": data["input_tokens"], "output_tokens": 0}
            kind = "count"
        else:
            usage, kind = data["usage"], "generation"
        amount = self.gate.tariff.cost(kind, usage["input_tokens"], usage["output_tokens"])
        return {"status": "known", "verified": True,
                "source": "approved_tariff_usage:" + self.gate.ledger["tariff_sha256"],
                "amount_usd": format(amount, "f"), "evidence_kind": "calculated_not_provider_invoice"}

    async def close(self):
        self.closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None


class _GatedResponseContext:
    def __init__(self, session, endpoint, token, options, item_id, prompt_hash):
        self.session, self.endpoint, self.token = session, endpoint, token
        self.options, self.item_id, self.prompt_hash = options, item_id, prompt_hash
        self._context, self._response = None, None
        self._budget_scope = None
        self._settled = False
        self.started = time.monotonic()

    def _settle(self, *, status, usage=None, billing=None):
        if self._settled:
            return
        self._settled = True
        self.session.gate.settle(self.token, status=status, usage=usage, billing=billing,
                                 elapsed_seconds=max(0, time.monotonic() - self.started))

    def _unknown(self):
        try:
            self._settle(status="failed")
        except PilotBlocked:
            pass  # Keep the native HTTP/parse/timeout exception and sticky ledger.

    async def __aenter__(self):
        try:
            remaining = self.session.gate.transport_timeout(self.token)
            if not math.isfinite(remaining) or remaining <= 0:
                raise PilotBlocked("request_time_limit")
            # This scope survives __aenter__ through body consumption and
            # __aexit__, enforcing the gate's cumulative deadline even when
            # it is shorter than the native adapter's call deadline.
            self._budget_scope = asyncio.timeout(remaining)
            await self._budget_scope.__aenter__()
            # Durable gate admission already happened in post(), before both
            # constructing a normal request and entering its network context.
            self._context = self.session._underlying().post(self.endpoint, **self.options)
            self._response = await self._context.__aenter__()
            return _ResponseView(self, self._response)
        except BaseException as error:
            try:
                self._unknown()
            finally:
                if self._budget_scope is not None:
                    await self._budget_scope.__aexit__(type(error), error, error.__traceback__)
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            try:
                if not self._settled:
                    self._unknown()
            finally:
                if self._context is not None:
                    await self._context.__aexit__(exc_type, exc, traceback)
        finally:
            if self._budget_scope is not None:
                await self._budget_scope.__aexit__(exc_type, exc, traceback)
        return False

    def body_received(self, body):
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            self._unknown()
            raise PilotBlocked("invalid_provider_json") from None
        response = self._response
        if not isinstance(data, dict) or response.status != 200:
            self._unknown()
            raise PilotBlocked("provider_response_unknown")
        try:
            self._settle_received(data, response)
        except Exception as error:
            # Keep the normal adapter's raw provider JSON/usage audit intact.
            # A failed settlement is already sticky in the gate (or pending
            # on disk if persistence itself failed). The wrapper stops at the
            # next input gate / before delivering any generation result.
            self.session._settlement_failure = getattr(error, "code", "settlement_failed_no_retry")

    def _settle_received(self, data, response):
        if self.endpoint == COUNT_ENDPOINT:
            value = data.get("input_tokens")
            if type(value) is not int or value < 0:
                self._unknown()
                raise PilotBlocked("usage_unknown")
            usage = {"input_tokens": value, "output_tokens": 0}
        else:
            from indeces.contracts import validated_token_usage
            usage = validated_token_usage(data.get("usage"))
            if usage is None:
                self._unknown()
                raise PilotBlocked("usage_unknown")
            if data.get("model") not in self.session.descriptor["allowed_response_models"]:
                self._settle(status="failed", usage=usage)
                raise PilotBlocked("response_model_unverified")
        headers = getattr(response, "headers", {})
        organizations = [value for key, value in headers.items()
                         if isinstance(key, str) and key.lower() == "openai-organization"]
        if (account_scope(self.session.gate.approval) == "explicit_organization_project"
                and any(value != self.session.gate.approval["account_id"] for value in organizations)):
            # Preserve valid usage; a response routed to another account has
            # no verified fee under this approval's tariff contract.
            self._settle(status="failed", usage=usage)
            raise PilotBlocked("response_account_unverified")
        request_id = headers.get("x-request-id") if hasattr(headers, "get") else None
        metadata = {"provider_request_id": request_id if isinstance(request_id, str) else None,
                    "response_model": data.get("model"), "evidence_kind": "calculated_not_provider_invoice"}
        try:
            billing = self.session._resolver(self.endpoint, data, response.status, metadata)
        except Exception:
            self._settle(status="failed", usage=usage)
            raise PilotBlocked("billing_unknown") from None
        completed = self.endpoint == COUNT_ENDPOINT or data.get("status") == "completed"
        self._settle(status="completed" if completed else "failed", usage=usage, billing=billing)
        if self.endpoint == COUNT_ENDPOINT:
            self.session._counted[self.item_id] = self.prompt_hash


class _ResponseView:
    def __init__(self, context, response):
        self.context, self.response = context, response
        self.status, self.headers = response.status, response.headers
        self.content = self

    async def iter_chunked(self, size):
        parts, total = [], 0
        try:
            async for part in self.response.content.iter_chunked(size):
                total += len(part)
                if total > _BODY_LIMIT:
                    self.context._unknown()
                    raise PilotBlocked("provider_response_too_large")
                parts.append(part)
                yield part
            self.context.body_received(b"".join(parts))
        except BaseException:
            self.context._unknown()
            raise


def attach_live_transport(adapter, gate, *, explicit_live=False, validated_descriptor=None,
                          billing_resolver=None, session_factory=None):
    """Attach an opted-in normal HTTP session without constructing it yet."""
    _activation(adapter, gate, explicit_live, validated_descriptor)
    if billing_resolver is not None and not callable(billing_resolver):
        raise PilotBlocked("invalid_billing_resolver")
    if session_factory is not None and not callable(session_factory):
        raise PilotBlocked("invalid_session_factory")
    adapter._session = GatedHTTPSession(gate, validated_descriptor,
                                       session_factory=session_factory, billing_resolver=billing_resolver)
    return LiveReplyAdapter(adapter, gate, validated_descriptor)
