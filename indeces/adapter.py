"""Stateless Luna transport: exact count, one generation, no retries or tools."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import time
import uuid

from .contracts import GovernedError, ModelResult


def reservation(instructions: str, messages: list[dict], schema=None) -> int:
    """Offline planning allowance, NOT billed tokens or an exact tokenizer."""
    text = json.dumps({"instructions": instructions, "input": messages, "schema": schema}, ensure_ascii=False)
    return len(text.encode("utf-8")) + 256 + 64 * len(messages)


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

    async def _post(self, path, payload, trace_id, call_id):
        self.scratch.write("http_request", trace_id=trace_id, call_id=call_id, path=path, payload=payload)
        if self._request_override is not None:
            data = await self._request_override(path, payload)
        else:
            import aiohttp
            if not self._key:
                raise GovernedError("missing_openai_key")
            if self._session is None:
                # The external asyncio deadline covers connect + read + parsing.
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
            try:
                async with self._session.post(self.config.base_url.rstrip("/") + path, json=payload,
                        headers={"Authorization": "Bearer " + self._key}, allow_redirects=False) as response:
                    if response.status != 200:
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
        self.scratch.write("http_response", trace_id=trace_id, call_id=call_id, path=path, payload=data)
        if not isinstance(data, dict):
            raise GovernedError("invalid_provider_response")
        return data

    async def call(self, stage: str, instructions: str, messages: list[dict], trace_id: str, schema=None) -> ModelResult:
        budget = self.config.budgets[stage]
        circuit = self._circuits[stage]
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        self.scratch.write("call_start", trace_id=trace_id, call_id=call_id, stage=stage,
                           budget=asdict(budget), model=self.config.model,
                           planning_reservation=reservation(instructions, messages, schema))
        if circuit["until"] > started:
            self.scratch.write("call_rejected", trace_id=trace_id, call_id=call_id, stage=stage, code="circuit_open")
            raise GovernedError("circuit_open")
        common = {"model": self.config.model, "instructions": instructions, "input": messages,
                  "reasoning": {"effort": budget.reasoning}}
        if schema is not None:
            common["text"] = {"format": {"type": "json_schema", "name": stage, "strict": True, "schema": schema}}
        try:
            async with asyncio.timeout(budget.seconds):
                async with self._lock:
                    if circuit["until"] > time.monotonic():
                        raise GovernedError("circuit_open")
                    counted = await self._post("/responses/input_tokens", common, trace_id, call_id)
                    tokens = counted.get("input_tokens")
                    if type(tokens) is not int or tokens < 0:
                        raise GovernedError("invalid_input_token_count")
                    self.scratch.write("input_gate", trace_id=trace_id, call_id=call_id, stage=stage,
                                       input_tokens=tokens, limit=budget.input_tokens, admitted=tokens <= budget.input_tokens)
                    if tokens > budget.input_tokens:
                        raise GovernedError("input_token_limit")
                    payload = {**common, "max_output_tokens": budget.output_tokens, "store": False,
                               "stream": False, "truncation": "disabled", "tools": []}
                    data = await self._post("/responses", payload, trace_id, call_id)
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        raise GovernedError("missing_usage")
                    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                    if type(inp) is not int or type(out) is not int or min(inp, out) < 0:
                        raise GovernedError("invalid_usage")
                    if inp > budget.input_tokens or out > budget.output_tokens:
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
        except asyncio.CancelledError:
            self.scratch.write("call_end", trace_id=trace_id, call_id=call_id, stage=stage,
                               status="cancelled", elapsed_seconds=time.monotonic() - started, remote_usage_unknown=True)
            raise
        except (TimeoutError, GovernedError) as error:
            code = error.code if isinstance(error, GovernedError) else "stage_timeout"
            # User input admission does not mark a healthy provider as unhealthy.
            if code not in {"input_token_limit", "missing_openai_key", "circuit_open"}:
                circuit["failures"] += 1
                if circuit["failures"] >= self.config.failure_threshold:
                    circuit["until"] = time.monotonic() + self.config.cooldown_seconds
            self.scratch.write("call_end", trace_id=trace_id, call_id=call_id, stage=stage, status="failed", code=code,
                               elapsed_seconds=time.monotonic() - started,
                               remote_usage_unknown=code in {"stage_timeout", "provider_network_error"}, circuit=dict(circuit))
            raise GovernedError(code) from None
        circuit.update(failures=0, until=0.0)
        self.scratch.write("call_end", trace_id=trace_id, call_id=call_id, stage=stage, status="completed", result=asdict(result))
        return result
