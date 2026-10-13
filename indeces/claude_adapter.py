"""Native Claude Messages: counted admission, one generation, no tools or retries.

The inherited code supplies only lock/circuit ownership, close, and synchronous
audit bookkeeping. No OpenAI request or response format crosses this transport.
Native content is retained in scratch; only validated text reaches ModelResult.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import math
import re
import time
import uuid

from .adapter import OpenAIAdapter, _CallProgress, reservation
from .config import Budget
from .contracts import GovernedError, ModelResult


CLAUDE_MODELS = frozenset({"claude-opus-5-5", "claude-fable-5-1"})
CLAUDE_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
_COUNT_PATH = "/messages/count_tokens"
_GENERATE_PATH = "/messages"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_json(value):
    """Reject decoded surrogates/nonfinite values before accepting native data."""
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("non-string key")
            _validate_json(key)
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or type(value) in (bool, int):
        pass
    elif type(value) is float and math.isfinite(value):
        pass
    else:
        raise ValueError("invalid JSON value")


def _native_usage(usage):
    """Return disjoint input counters once, plus all validated native metadata."""
    if not isinstance(usage, dict):
        return None, None
    for key in ("input_tokens", "output_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            return None, None
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            return None, None
    for key in ("cache_creation", "server_tool_use", "output_tokens_details"):
        counters = usage.get(key)
        if counters is not None and (not isinstance(counters, dict) or any(
                type(value) is not int or value < 0 for value in counters.values())):
            return None, None
    breakdown = usage.get("cache_creation")
    if breakdown is not None and sum(breakdown.values()) != usage.get("cache_creation_input_tokens", 0):
        return None, None
    details = usage.get("output_tokens_details")
    if details is not None and details.get("thinking_tokens", 0) > usage["output_tokens"]:
        return None, None
    try:
        _validate_json(usage)
        native = json.loads(_json_bytes(usage))
    except (ValueError, UnicodeError, RecursionError):
        return None, None
    # The cache_creation breakdown is already included in the aggregate counter.
    normalized = {"input_tokens": usage["input_tokens"] + usage.get("cache_read_input_tokens", 0)
                  + usage.get("cache_creation_input_tokens", 0),
                  "output_tokens": usage["output_tokens"]}
    return normalized, native


@dataclass
class _ClaudeProgress(_CallProgress):
    native_usage: dict | None = None

    def issued(self, path):
        self.remote_request_started = True
        self.remote_request_in_flight = True
        if path == _GENERATE_PATH:
            self.generation_request_started = True

    def received(self, path, data):
        self.remote_request_in_flight = False
        if path == _GENERATE_PATH and isinstance(data, dict):
            self.response_id = data.get("id") if isinstance(data.get("id"), str) else None
            self.known_usage, self.native_usage = _native_usage(data.get("usage"))
            self.generation_usage_received = self.known_usage is not None

    def receipt(self):
        return {**super().receipt(), "provider": "anthropic",
                "protocol": "messages", "base_url": CLAUDE_BASE_URL, "api_version": ANTHROPIC_VERSION,
                "native_usage": json.loads(_json_bytes(self.native_usage)) if self.native_usage is not None else None}


def _schema_wire(schema):
    """Lower only the application's supported schema vocabulary, fail closed.

    Anthropic does not accept the application's length/array/numeric bounds in
    raw output schemas. Those bounds remain authoritative in _schema_matches;
    descriptions make them visible without a tool or relaxed local acceptance.
    Other schema vocabulary is rejected.
    """
    allowed = {"type", "properties", "required", "additionalProperties", "items",
               "enum", "pattern", "description", "minLength", "maxLength", "minItems", "maxItems",
               "minimum", "maximum"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise GovernedError("unsupported_output_schema")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise GovernedError("unsupported_output_schema")
    wire = dict(schema)
    if "description" in schema and not isinstance(schema["description"], str):
        raise GovernedError("unsupported_output_schema")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]
                             or any(type(v) not in (str, int, float, bool, type(None)) for v in schema["enum"])):
        raise GovernedError("unsupported_output_schema")
    constraints = []
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            if type(schema[key]) is not int or schema[key] < 0:
                raise GovernedError("unsupported_output_schema")
            if (key.endswith("Length") and kind != "string") or (key.endswith("Items") and kind != "array"):
                raise GovernedError("unsupported_output_schema")
            if key != "minItems" or schema[key] > 1:
                constraints.append(f"{key}: {schema[key]}")
                wire.pop(key)
    for key in ("minimum", "maximum"):
        if key in schema:
            value = schema[key]
            if (kind not in {"integer", "number"} or type(value) not in (int, float)
                    or (type(value) is float and not math.isfinite(value))):
                raise GovernedError("unsupported_output_schema")
            constraints.append(f"{key}: {value}")
            wire.pop(key)
    for minimum, maximum in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
        if minimum in schema and maximum in schema and schema[minimum] > schema[maximum]:
            raise GovernedError("unsupported_output_schema")
    if "pattern" in schema:
        # Current public label schema uses this pattern. Do not guess support for
        # arbitrary Python regexes in Claude's separate grammar implementation.
        if kind != "string" or schema["pattern"] != r"\S":
            raise GovernedError("unsupported_output_schema")
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or schema.get("additionalProperties") is not False
                or not isinstance(required, list) or any(not isinstance(v, str) for v in required)
                or len(set(required)) != len(required) or set(required) - set(properties)):
            raise GovernedError("unsupported_output_schema")
        wire["properties"] = {key: _schema_wire(value) for key, value in properties.items()}
    elif "properties" in schema or "required" in schema or "additionalProperties" in schema:
        raise GovernedError("unsupported_output_schema")
    if kind == "array":
        wire["items"] = _schema_wire(schema.get("items"))
    elif "items" in schema:
        raise GovernedError("unsupported_output_schema")
    if constraints:
        wire["description"] = (schema.get("description", "") + " Local acceptance constraints: "
                               + "; ".join(constraints) + ".").strip()
    return wire


def _schema_matches(value, schema):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(schema.get("required", [])) - set(value):
            return False
        properties = schema["properties"]
        return not set(value) - set(properties) and all(_schema_matches(v, properties[k]) for k, v in value.items())
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                <= schema.get("maxItems", math.inf)
                and all(_schema_matches(v, schema["items"]) for v in value))
    valid = {"string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in (int, float), "boolean": type(value) is bool, "null": value is None}[kind]
    if not valid or ("enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"])):
        return False
    if kind in {"integer", "number"}:
        return ((type(value) is not float or math.isfinite(value))
                and ("minimum" not in schema or value >= schema["minimum"])
                and ("maximum" not in schema or value <= schema["maximum"]))
    return (kind != "string" or (schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf)
            and ("pattern" not in schema or re.search(schema["pattern"], value) is not None)))


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON key")
        data[key] = value
    return data


class ClaudeAdapter(OpenAIAdapter):
    def __init__(self, config, scratch, api_key: str = "", request=None):
        if (config.model not in CLAUDE_MODELS or not isinstance(config.base_url, str)
                or config.base_url.rstrip("/") != CLAUDE_BASE_URL
                or getattr(config, "provider", "anthropic") != "anthropic"
                or any(b.reasoning != "high" or b.input_tokens + b.output_tokens > 1_000_000
                       for b in config.budgets.values())):
            raise ValueError("native Claude requires an explicit official endpoint, supported model, high effort, and 1M context cap")
        super().__init__(config, scratch, api_key, request)

    def __repr__(self):
        return f"ClaudeAdapter({self.config.model}, credentials=<redacted>)"

    async def _post(self, path, payload, trace_id, call_id, progress=None, audit=None):
        if path not in {_COUNT_PATH, _GENERATE_PATH}:
            raise GovernedError("invalid_claude_request_path")
        # Freeze independently of caller, audit, scratch, or fake-transport mutation.
        body = _json_bytes(payload)
        phase = "input_count" if path == _COUNT_PATH else "generation"
        client_request_id = uuid.uuid4().hex
        if progress is not None:
            progress.phase = phase
            progress.client_request_id = client_request_id
            progress.provider_request_id = None
        fields = {"trace_id": trace_id, "call_id": call_id, "path": path,
                  "phase": phase, "client_request_id": client_request_id,
                  "provider": "anthropic", "protocol": "messages", "base_url": CLAUDE_BASE_URL,
                  "api_version": ANTHROPIC_VERSION}
        self._audit(audit, "request_intent", progress, **fields,
                    input_count_is_non_generating=path == _COUNT_PATH)
        self.scratch.write("http_request", **fields, payload=json.loads(body))
        if progress is not None and progress.deadline is not None and time.monotonic() >= progress.deadline:
            raise GovernedError("stage_timeout")
        if self._request_override is not None:
            if progress is not None:
                progress.issued(path)
            data = await self._request_override(path, json.loads(body))
        else:
            import aiohttp
            if not self._key:
                raise GovernedError("missing_anthropic_key")
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            try:
                if progress is not None:
                    progress.issued(path)
                async with self._session.post(CLAUDE_BASE_URL + path, data=body,
                        headers={"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION,
                                 "Content-Type": "application/json", "X-Client-Request-Id": client_request_id},
                        allow_redirects=False) as response:
                    provider_id = getattr(response, "headers", {}).get("request-id")
                    if not isinstance(provider_id, str):
                        provider_id = None
                    if progress is not None:
                        progress.provider_request_id = provider_id
                    self._audit(audit, "response_headers", progress, **fields,
                                provider_request_id=provider_id, status=response.status)
                    if response.status != 200:
                        if progress is not None:
                            progress.remote_request_in_flight = False
                            progress.generation_rejected = path == _GENERATE_PATH and 400 <= response.status < 500
                        self.scratch.write("http_error", **fields, status=response.status,
                                           provider_request_id=provider_id)
                        raise GovernedError(f"provider_http_{response.status}")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(16384):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise GovernedError("provider_response_too_large")
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks), object_pairs_hook=_pairs,
                                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                    except (ValueError, UnicodeError):
                        raise GovernedError("invalid_provider_json") from None
            except aiohttp.ClientError:
                raise GovernedError("provider_network_error") from None
        if progress is not None:
            # Capture confirmed usage before any decoded-content or sink failure.
            progress.received(path, data)
        self._audit(audit, "response_received", progress, **fields,
                    provider_request_id=progress.provider_request_id if progress is not None else None,
                    response_id=progress.response_id if progress is not None else None,
                    counted_input_tokens=(data.get("input_tokens") if path == _COUNT_PATH and isinstance(data, dict)
                                          and type(data.get("input_tokens")) is int and data["input_tokens"] >= 0 else None),
                    known_usage=dict(progress.known_usage) if progress is not None and progress.known_usage is not None else None,
                    native_usage=(json.loads(_json_bytes(progress.native_usage))
                                  if progress is not None and progress.native_usage is not None else None))
        self.scratch.write("http_response", **fields,
                           provider_request_id=progress.provider_request_id if progress is not None else None,
                           payload=data)
        if not isinstance(data, dict):
            raise GovernedError("invalid_provider_response")
        try:
            _validate_json(data)
        except (ValueError, UnicodeError, RecursionError):
            raise GovernedError("invalid_provider_response") from None
        return data

    def _messages(self, instructions, messages, schema):
        # The ordinary main Runtime supplies text only. Accepting arbitrary
        # content blocks here would create a tool/image/remote-source bypass.
        if not isinstance(instructions, str) or not isinstance(messages, list) or not messages:
            raise GovernedError("invalid_claude_input")
        for message in messages:
            if (not isinstance(message, dict) or set(message) != {"role", "content"}
                    or not isinstance(message["role"], str) or message["role"] not in {"user", "assistant"}
                    or not isinstance(message["content"], str)):
                raise GovernedError("invalid_claude_input")
        if messages[-1]["role"] != "user":
            raise GovernedError("claude_prefill_not_supported")
        common = {"model": self.config.model, "system": instructions, "messages": messages,
                  "output_config": {"effort": "high"}, "tools": [], "tool_choice": {"type": "none"}}
        try:
            _validate_json(schema)
            _validate_json(common)
            if schema is not None:
                common["output_config"]["format"] = {"type": "json_schema", "schema": _schema_wire(schema)}
            return json.loads(_json_bytes(common))
        except (ValueError, UnicodeError, RecursionError):
            raise GovernedError("invalid_claude_input") from None

    def _text(self, data, schema):
        if (data.get("type") != "message" or data.get("role") != "assistant"
                or data.get("model") != self.config.model or not isinstance(data.get("id"), str) or not data["id"]):
            raise GovernedError("invalid_provider_response")
        details = data.get("stop_details")
        if details is not None and not isinstance(details, dict):
            raise GovernedError("invalid_provider_response")
        if data.get("stop_reason") == "refusal" or (isinstance(details, dict) and details.get("type") == "refusal"):
            raise GovernedError("provider_refusal")
        if data.get("stop_reason") != "end_turn" or data.get("stop_sequence") is not None:
            raise GovernedError("incomplete_response")
        content = data.get("content")
        if not isinstance(content, list):
            raise GovernedError("invalid_output")
        texts = []
        for block in content:
            if not isinstance(block, dict):
                raise GovernedError("invalid_output")
            kind = block.get("type")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise GovernedError("invalid_output")
                texts.append(block["text"])
            elif kind == "thinking":
                if not isinstance(block.get("thinking"), str) or not isinstance(block.get("signature"), str) or not block["signature"]:
                    raise GovernedError("invalid_output")
            elif kind == "redacted_thinking":
                if not isinstance(block.get("data"), str) or not block["data"]:
                    raise GovernedError("invalid_output")
            elif kind == "refusal":
                raise GovernedError("provider_refusal")
            else:
                raise GovernedError("unexpected_provider_item")
        text = "".join(texts)
        if not text.strip():
            raise GovernedError("empty_output")
        if schema is not None:
            try:
                value = json.loads(text, object_pairs_hook=_pairs,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
                _validate_json(value)
                if not _schema_matches(value, schema):
                    raise ValueError("schema mismatch")
            except (ValueError, UnicodeError, RecursionError):
                raise GovernedError("invalid_structured_output") from None
        return text

    async def call(self, stage: str, instructions: str, messages: list[dict], trace_id: str, schema=None,
                   *, input_limit: int | None = None, audit=None, budget_override: Budget | None = None) -> ModelResult:
        configured = self.config.budgets[stage]
        budget = configured if budget_override is None else budget_override
        if budget_override is not None:
            ceiling = max(configured.seconds, 45.0) if stage == "label" else configured.seconds
            if (not isinstance(budget, Budget) or budget.input_tokens > configured.input_tokens
                    or budget.output_tokens > configured.output_tokens or budget.seconds > ceiling
                    or budget.reasoning != "high"):
                raise GovernedError("invalid_call_budget_override", remote_usage_unknown=False)
        if audit is not None and not callable(audit):
            raise GovernedError("invalid_call_audit", remote_usage_unknown=False)
        if input_limit is not None and (type(input_limit) is not int or input_limit <= 0):
            raise GovernedError("invalid_call_input_limit", remote_usage_unknown=False)
        effective_limit = min(budget.input_tokens, input_limit) if input_limit is not None else budget.input_tokens
        circuit = self._circuits[stage]
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        progress = _ClaudeProgress(started, deadline=started + budget.seconds)
        try:
            common = self._messages(instructions, messages, schema)
            # Local acceptance and counted input share one immutable snapshot.
            accepted_schema = json.loads(_json_bytes(schema)) if schema is not None else None
            self._audit(audit, "call_start", progress, trace_id=trace_id, call_id=call_id, stage=stage,
                        budget=asdict(budget), configured_budget=asdict(configured), model=self.config.model,
                        provider="anthropic", base_url=CLAUDE_BASE_URL, protocol="messages",
                        api_version=ANTHROPIC_VERSION, effort="high", requested_input_limit=input_limit,
                        effective_input_limit=effective_limit)
            self.scratch.write("call_start", trace_id=trace_id, call_id=call_id, stage=stage,
                               budget=asdict(budget), model=self.config.model, provider="anthropic", effort="high",
                               base_url=CLAUDE_BASE_URL, protocol="messages", api_version=ANTHROPIC_VERSION,
                               response_schema=accepted_schema,
                               requested_input_limit=input_limit, effective_input_limit=effective_limit,
                               usage_receipt_version=1, planning_reservation=reservation(instructions, messages, schema))
            if circuit["until"] > started:
                raise GovernedError("circuit_open")
            # Synchronous preparation/audit time consumes this same deadline.
            async with asyncio.timeout(max(0.0, progress.deadline - time.monotonic())):
                async with self._lock:
                    progress.slot_wait_seconds = time.monotonic() - started
                    progress.phase = "slot_acquired"
                    if circuit["until"] > time.monotonic():
                        raise GovernedError("circuit_open")
                    if time.monotonic() >= progress.deadline:
                        raise GovernedError("stage_timeout")
                    counted = await self._post(_COUNT_PATH, common, trace_id, call_id, progress, audit)
                    progress.phase = "input_gate"
                    tokens = counted.get("input_tokens")
                    if type(tokens) is not int or tokens < 0:
                        raise GovernedError("invalid_input_token_count")
                    self._audit(audit, "input_gate", progress, trace_id=trace_id, call_id=call_id, stage=stage,
                                input_tokens=tokens, limit=effective_limit, admitted=tokens <= effective_limit,
                                reserved_input_tokens=tokens, reserved_output_tokens=budget.output_tokens)
                    self.scratch.write("input_gate", trace_id=trace_id, call_id=call_id, stage=stage,
                                       input_tokens=tokens, limit=effective_limit, stage_limit=budget.input_tokens,
                                       admitted=tokens <= effective_limit)
                    if tokens > effective_limit:
                        raise GovernedError("input_token_limit")
                    if time.monotonic() >= progress.deadline:
                        raise GovernedError("stage_timeout")
                    payload = {**common, "max_tokens": budget.output_tokens, "stream": False}
                    data = await self._post(_GENERATE_PATH, payload, trace_id, call_id, progress, audit)
                    progress.phase = "output_validation"
                    if progress.known_usage is None:
                        raise GovernedError("missing_usage" if not isinstance(data.get("usage"), dict) else "invalid_usage")
                    inp, out = progress.known_usage["input_tokens"], progress.known_usage["output_tokens"]
                    if inp > effective_limit or out > budget.output_tokens:
                        raise GovernedError("provider_token_limit_breach")
                    if any((progress.native_usage.get("server_tool_use") or {}).values()):
                        raise GovernedError("unexpected_provider_item")
                    text = self._text(data, accepted_schema)
                    elapsed = time.monotonic() - started
                    if elapsed >= budget.seconds:
                        raise GovernedError("stage_timeout")
                    result = ModelResult(text, inp, out, data["id"], elapsed)
        except asyncio.CancelledError as error:
            progress.annotate_error(error)
            try:
                self._record_end(progress, audit, trace_id=trace_id, call_id=call_id, stage=stage,
                                 status="cancelled", elapsed_seconds=time.monotonic() - started)
            except BaseException:
                pass
            raise
        except (TimeoutError, GovernedError) as error:
            code = error.code if isinstance(error, GovernedError) else "stage_timeout"
            if code == "stage_timeout" and progress.phase == "slot_wait":
                code = "model_slot_timeout"
            local_timeout = code == "stage_timeout" and not progress.remote_request_in_flight
            if (code not in {"input_token_limit", "missing_anthropic_key", "circuit_open", "model_slot_timeout",
                             "invalid_claude_input", "claude_prefill_not_supported", "unsupported_output_schema"}
                    and not local_timeout and not getattr(error, "adapter_audit_failure", False)):
                circuit["failures"] += 1
                if circuit["failures"] >= self.config.failure_threshold:
                    circuit["until"] = time.monotonic() + self.config.cooldown_seconds
            primary = (error if isinstance(error, GovernedError) and error.code == code else GovernedError(code))
            progress.annotate_error(primary)
            try:
                self._record_end(progress, audit, trace_id=trace_id, call_id=call_id, stage=stage,
                                 status="failed", code=code, elapsed_seconds=time.monotonic() - started,
                                 circuit=dict(circuit))
            except BaseException:
                pass
            raise primary from None
        except BaseException as error:
            progress.annotate_error(error)
            try:
                self._record_end(progress, audit, trace_id=trace_id, call_id=call_id, stage=stage,
                                 status="failed", code="adapter_audit_error", elapsed_seconds=time.monotonic() - started)
            except BaseException:
                pass
            raise
        circuit.update(failures=0, until=0.0)
        self._record_end(progress, audit, trace_id=trace_id, call_id=call_id, stage=stage,
                         status="completed", elapsed_seconds=time.monotonic() - started, result=asdict(result))
        return result
