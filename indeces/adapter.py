"""Stateless Luna transport: exact count, one generation, no retries or tools."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import time
import uuid

from .contracts import GovernedError, ModelResult, validated_token_usage


def reservation(instructions: str, messages: list[dict], schema=None) -> int:
    """Offline planning allowance, NOT billed tokens or an exact tokenizer."""
    text = json.dumps({"instructions": instructions, "input": messages, "schema": schema}, ensure_ascii=False)
    return len(text.encode("utf-8")) + 256 + 64 * len(messages)


@dataclass
class _CallProgress:
    slot_wait_started: float
    phase: str = "slot_wait"
    slot_wait_seconds: float | None = None
    remote_request_started: bool = False
    remote_request_in_flight: bool = False
    generation_request_started: bool = False
    generation_usage_received: bool = False
    generation_rejected: bool = False
    known_usage: dict | None = None

    @property
    def remote_usage_unknown(self):
        # Input counting does not issue a generation or its token usage.
        return self.generation_request_started and not self.generation_usage_received

    def issued(self, path):
        self.remote_request_started = True
        self.remote_request_in_flight = True
        if path == "/responses":
            self.generation_request_started = True

    def received(self, path, data):
        self.remote_request_in_flight = False
        if path == "/responses" and isinstance(data, dict):
            self.known_usage = validated_token_usage(data.get("usage"))
            self.generation_usage_received = self.known_usage is not None

    def receipt(self):
        return {"phase": self.phase,
                "slot_wait_seconds": (time.monotonic() - self.slot_wait_started
                                      if self.slot_wait_seconds is None else self.slot_wait_seconds),
                "remote_request_started": self.remote_request_started,
                "remote_request_in_flight": self.remote_request_in_flight,
                "generation_request_started": self.generation_request_started,
                "generation_usage_received": self.generation_usage_received,
                "generation_rejected": self.generation_rejected,
                "known_usage": dict(self.known_usage) if self.known_usage is not None else None,
                "remote_usage_unknown": self.remote_usage_unknown}

    def annotate_error(self, error):
        error.remote_usage_unknown = self.remote_usage_unknown
        error.known_usage = dict(self.known_usage) if self.known_usage is not None else None


class OpenAIAdapter:
    def __init__(self, config, scratch, api_key: str = "", request=None):
        self.config = config
        self.scratch = scratch
        self._key = api_key
        self._request_override = request
        self._session = None
        self._lock = asyncio.Lock()
        self._circuits = {stage: {"failures": 0, "until": 0.0} for stage in config.budgets}

    def __repr__(self):
        return "OpenAIAdapter(gpt-6-luna, credentials=<redacted>)"

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _record_end(self, progress, **fields):
        try:
            self.scratch.write("call_end", **fields, **progress.receipt())
        except BaseException as error:
            # Preserve the original audit exception and any usage already
            # returned by the provider; do not retry the failed log sink.
            progress.annotate_error(error)
            raise

    async def _post(self, path, payload, trace_id, call_id, progress=None):
        if progress is not None:
            progress.phase = "input_count" if path == "/responses/input_tokens" else "generation"
        self.scratch.write("http_request", trace_id=trace_id, call_id=call_id, path=path, payload=payload)
        if self._request_override is not None:
            if progress is not None:
                progress.issued(path)
            data = await self._request_override(path, payload)
        else:
            import aiohttp
            if not self._key:
                raise GovernedError("missing_openai_key")
            if self._session is None:
                # The external asyncio deadline covers connect + read + parsing.
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            try:
                if progress is not None:
                    progress.issued(path)
                async with self._session.post(self.config.base_url.rstrip("/") + path, json=payload,
                        headers={"Authorization": "Bearer " + self._key}, allow_redirects=False) as response:
                    if response.status != 200:
                        if progress is not None:
                            progress.remote_request_in_flight = False
                            progress.generation_rejected = path == "/responses" and 400 <= response.status < 500
                        self.scratch.write("http_error", trace_id=trace_id, call_id=call_id, path=path, status=response.status)
                        raise GovernedError(f"provider_http_{response.status}")
                    parts = []
                    size = 0
                    async for part in response.content.iter_chunked(16384):
                        size += len(part)
                        if size > 2 * 1024 * 1024:
                            raise GovernedError("provider_response_too_large")
                        parts.append(part)
                    try:
                        data = json.loads(b"".join(parts))
                    except (ValueError, UnicodeError):
                        raise GovernedError("invalid_provider_json") from None
            except aiohttp.ClientError:
                raise GovernedError("provider_network_error") from None
        if progress is not None:
            progress.received(path, data)
        self.scratch.write("http_response", trace_id=trace_id, call_id=call_id, path=path, payload=data)
        if not isinstance(data, dict):
            raise GovernedError("invalid_provider_response")
        return data

    async def call(self, stage: str, instructions: str, messages: list[dict], trace_id: str, schema=None,
                   *, input_limit: int | None = None) -> ModelResult:
        """Count the complete request before generating within unchanged stage caps.

        A caller can narrow input admission to its remaining cumulative budget.
        A larger caller allowance never expands the configured stage budget.
        Output, reasoning, model, slot serialization and deadline are unchanged.
        """
        budget = self.config.budgets[stage]
        if input_limit is not None and (type(input_limit) is not int or input_limit <= 0):
            raise GovernedError("invalid_call_input_limit", remote_usage_unknown=False)
        effective_input_limit = min(budget.input_tokens, input_limit) if input_limit is not None else budget.input_tokens
        circuit = self._circuits[stage]
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        self.scratch.write("call_start", trace_id=trace_id, call_id=call_id, stage=stage,
                           budget=asdict(budget), model=self.config.model,
                           verbosity=self.config.verbosity,
                           requested_input_limit=input_limit, effective_input_limit=effective_input_limit,
                           usage_receipt_version=1,
                           planning_reservation=reservation(instructions, messages, schema))
        if circuit["until"] > started:
            self.scratch.write("call_rejected", trace_id=trace_id, call_id=call_id, stage=stage, code="circuit_open",
                               phase="admission", slot_wait_seconds=0.0, remote_request_started=False,
                               remote_request_in_flight=False, generation_request_started=False,
                               generation_usage_received=False, generation_rejected=False,
                               known_usage=None, remote_usage_unknown=False)
            raise GovernedError("circuit_open", remote_usage_unknown=False)
        common = {"model": self.config.model, "instructions": instructions, "input": messages,
                  "reasoning": {"effort": budget.reasoning}, "text": {"verbosity": self.config.verbosity}}
        if schema is not None:
            common["text"]["format"] = {"type": "json_schema", "name": stage, "strict": True, "schema": schema}
        progress = _CallProgress(time.monotonic())
        try:
            async with asyncio.timeout(budget.seconds):
                async with self._lock:
                    progress.slot_wait_seconds = time.monotonic() - progress.slot_wait_started
                    progress.phase = "slot_acquired"
                    if circuit["until"] > time.monotonic():
                        raise GovernedError("circuit_open")
                    if time.monotonic() - started >= budget.seconds:
                        raise GovernedError("stage_timeout")
                    counted = await self._post("/responses/input_tokens", common, trace_id, call_id, progress)
                    progress.phase = "input_gate"
                    tokens = counted.get("input_tokens")
                    if type(tokens) is not int or tokens < 0:
                        raise GovernedError("invalid_input_token_count")
                    self.scratch.write("input_gate", trace_id=trace_id, call_id=call_id, stage=stage,
                                       input_tokens=tokens, limit=effective_input_limit,
                                       stage_limit=budget.input_tokens, admitted=tokens <= effective_input_limit)
                    if tokens > effective_input_limit:
                        raise GovernedError("input_token_limit")
                    if time.monotonic() - started >= budget.seconds:
                        raise GovernedError("stage_timeout")
                    payload = {**common, "max_output_tokens": budget.output_tokens, "store": False,
                               "stream": False, "truncation": "disabled", "tools": []}
                    data = await self._post("/responses", payload, trace_id, call_id, progress)
                    progress.phase = "output_validation"
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        raise GovernedError("missing_usage")
                    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                    if type(inp) is not int or type(out) is not int or min(inp, out) < 0:
                        raise GovernedError("invalid_usage")
                    if inp > effective_input_limit or out > budget.output_tokens:
                        raise GovernedError("provider_token_limit_breach")
                    if data.get("status") != "completed":
                        raise GovernedError("incomplete_response")
                    texts = []
                    output = data.get("output")
                    if not isinstance(output, list):
                        raise GovernedError("invalid_output")
                    for item in output:
                        if not isinstance(item, dict):
                            raise GovernedError("invalid_output")
                        if item.get("type") == "message" and item.get("role") == "assistant":
                            content = item.get("content")
                            if not isinstance(content, list):
                                raise GovernedError("invalid_output")
                            for part in content:
                                if not isinstance(part, dict):
                                    raise GovernedError("invalid_output")
                                if part.get("type") == "output_text":
                                    value = part.get("text")
                                    if not isinstance(value, str):
                                        raise GovernedError("invalid_output")
                                    texts.append(value)
                                elif part.get("type") == "refusal":
                                    raise GovernedError("provider_refusal")
                        elif item.get("type") != "reasoning":
                            raise GovernedError("unexpected_provider_item")
                    text = "".join(texts)
                    if not text.strip():
                        raise GovernedError("empty_output")
                    elapsed = time.monotonic() - started
                    if elapsed >= budget.seconds:
                        raise GovernedError("stage_timeout")
                    result = ModelResult(text, inp, out, str(data.get("id", "")), elapsed)
        except asyncio.CancelledError as error:
            progress.annotate_error(error)
            self._record_end(progress, trace_id=trace_id, call_id=call_id, stage=stage,
                             status="cancelled", elapsed_seconds=time.monotonic() - started)
            raise
        except (TimeoutError, GovernedError) as error:
            code = error.code if isinstance(error, GovernedError) else "stage_timeout"
            if code == "stage_timeout" and progress.phase == "slot_wait":
                code = "model_slot_timeout"
            # User input admission does not mark a healthy provider as unhealthy.
            local_timeout = code == "stage_timeout" and not progress.remote_request_in_flight
            if code not in {"input_token_limit", "missing_openai_key", "circuit_open", "model_slot_timeout"} and not local_timeout:
                circuit["failures"] += 1
                if circuit["failures"] >= self.config.failure_threshold:
                    circuit["until"] = time.monotonic() + self.config.cooldown_seconds
            self._record_end(progress, trace_id=trace_id, call_id=call_id, stage=stage, status="failed", code=code,
                             elapsed_seconds=time.monotonic() - started, circuit=dict(circuit))
            raise GovernedError(code, remote_usage_unknown=progress.remote_usage_unknown,
                                known_usage=progress.known_usage) from None
        except BaseException as error:
            progress.annotate_error(error)
            raise
        circuit.update(failures=0, until=0.0)
        self._record_end(progress, trace_id=trace_id, call_id=call_id, stage=stage, status="completed", result=asdict(result))
        return result
