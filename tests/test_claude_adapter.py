"""Native Claude transport regressions; authored offline, no live API required."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.claude_adapter import (ClaudeAdapter, CLAUDE_BASE_URL, CLAUDE_MODELS,
                                   _native_usage, _schema_wire)
from indeces.config import Budget
from indeces.contracts import GovernedError
from indeces.prompts import LABEL_SCHEMA, BOT_REPLY_SCHEMA, summary_schema


class Scratch:
    def __init__(self, fail_event=None, error=None):
        self.events = []
        self.fail_event = fail_event
        self.error = error or RuntimeError("synthetic sink failure")

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))
        if event == self.fail_event:
            raise self.error

    def select(self, event):
        return [fields for name, fields in self.events if name == event]


def response(text="visible answer", *, model="claude-opus-5-5", inp=10, out=4):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


class SequenceTransport:
    def __init__(self, values):
        self.values = deque(values)
        self.requests = []

    async def __call__(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        value = self.values.popleft()
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)


class ClaudeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, transport, *, model="claude-opus-5-5", seconds=1.0, threshold=3, scratch=None):
        config = SimpleNamespace(provider="anthropic", model=model, base_url=CLAUDE_BASE_URL,
                                 budgets={stage: Budget(100, 20, seconds, "high")
                                          for stage in ("reply", "summary", "label")},
                                 failure_threshold=threshold, cooldown_seconds=30.0, verbosity="high")
        scratch = scratch or Scratch()
        return ClaudeAdapter(config, scratch, "synthetic-key", request=transport), scratch

    async def invoke(self, adapter, stage="reply", **kwargs):
        return await adapter.call(stage, "instructions", [{"role": "user", "content": "synthetic input"}],
                                  "trace", **kwargs)

    async def assert_code(self, adapter, code, **kwargs):
        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def test_both_models_explicit_high_native_wire_and_count_correspondence(self):
        for model in sorted(CLAUDE_MODELS):
            with self.subTest(model=model):
                transport = SequenceTransport([{"input_tokens": 10}, response(model=model)])
                adapter, _ = self.adapter(transport, model=model)
                result = await self.invoke(adapter)
                self.assertEqual(result.text, "visible answer")
                self.assertEqual([path for path, _ in transport.requests], ["/messages/count_tokens", "/messages"])
                counted, generated = [payload for _, payload in transport.requests]
                self.assertEqual(counted, {k: v for k, v in generated.items() if k not in {"max_tokens", "stream"}})
                self.assertEqual(counted["model"], model)
                self.assertEqual(counted["output_config"], {"effort": "high"})
                self.assertEqual(counted["tool_choice"], {"type": "none"})
                self.assertEqual(counted["tools"], [])
                self.assertEqual(generated["max_tokens"], 20)
                self.assertIs(generated["stream"], False)
                self.assertTrue({"input", "instructions", "reasoning", "text", "max_output_tokens", "store", "truncation"}
                                .isdisjoint(generated))

    async def test_count_fake_mutation_cannot_change_generation(self):
        seen = []

        async def transport(path, payload):
            seen.append((path, deepcopy(payload)))
            if path.endswith("count_tokens"):
                payload["system"] = "changed"
                payload["messages"][0]["content"] = "changed"
                payload["output_config"]["effort"] = "low"
                return {"input_tokens": 10}
            return response()

        adapter, _ = self.adapter(transport)
        await self.invoke(adapter)
        self.assertEqual(seen[1][1]["system"], "instructions")
        self.assertEqual(seen[1][1]["messages"][0]["content"], "synthetic input")
        self.assertEqual(seen[1][1]["output_config"]["effort"], "high")

    async def test_count_rejection_never_generates_and_does_not_trip_circuit(self):
        transport = SequenceTransport([{"input_tokens": 101}])
        adapter, scratch = self.adapter(transport, threshold=1)
        error = await self.assert_code(adapter, "input_token_limit")
        self.assertFalse(error.remote_usage_unknown)
        self.assertIsNone(error.known_usage)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(adapter._circuits["reply"]["failures"], 0)
        self.assertFalse(scratch.select("input_gate")[-1]["admitted"])

    async def test_input_limit_narrows_without_expanding_and_bool_count_declines(self):
        for count, limit, code in ((11, 10, "input_token_limit"), (101, 1000, "input_token_limit"),
                                   (True, None, "invalid_input_token_count"), (-1, None, "invalid_input_token_count")):
            with self.subTest(count=count, limit=limit):
                transport = SequenceTransport([{"input_tokens": count}])
                adapter, _ = self.adapter(transport)
                await self.assert_code(adapter, code, input_limit=limit)
                self.assertEqual(len(transport.requests), 1)

    async def test_invalid_input_limit_and_expanding_override_issue_no_request(self):
        for invalid in (True, 0, -1, 1.5):
            transport = SequenceTransport([])
            adapter, _ = self.adapter(transport)
            await self.assert_code(adapter, "invalid_call_input_limit", input_limit=invalid)
            self.assertEqual(transport.requests, [])
        for budget in (Budget(101, 20, 1, "high"), Budget(100, 21, 1, "high"),
                       Budget(100, 20, 2, "high"), Budget(100, 20, 1, "medium")):
            transport = SequenceTransport([])
            adapter, _ = self.adapter(transport)
            await self.assert_code(adapter, "invalid_call_budget_override", budget_override=budget)
            self.assertEqual(transport.requests, [])

    async def test_narrow_output_override_preserves_high_and_stage_config(self):
        transport = SequenceTransport([{"input_tokens": 10}, response()])
        adapter, _ = self.adapter(transport)
        override = Budget(50, 5, 0.5, "high")
        await self.invoke(adapter, budget_override=override)
        self.assertEqual(transport.requests[-1][1]["max_tokens"], 5)
        self.assertEqual(adapter.config.budgets["reply"].output_tokens, 20)

    async def test_cache_usage_sums_disjoint_inputs_preserves_native_details(self):
        data = response(inp=4)
        data["usage"].update(cache_read_input_tokens=5, cache_creation_input_tokens=6,
                             cache_creation={"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 4},
                             output_tokens_details={"thinking_tokens": 2}, service_tier="standard")
        events = []
        transport = SequenceTransport([{"input_tokens": 15}, data])
        adapter, scratch = self.adapter(transport)
        result = await self.invoke(adapter, audit=lambda event, **fields: events.append((event, deepcopy(fields))))
        self.assertEqual((result.input_tokens, result.output_tokens), (15, 4))
        receipt = [f for e, f in events if e == "response_received"][-1]
        self.assertEqual(receipt["known_usage"], {"input_tokens": 15, "output_tokens": 4})
        self.assertEqual(receipt["native_usage"], data["usage"])
        self.assertEqual(scratch.select("call_end")[-1]["native_usage"], data["usage"])

    async def test_missing_or_malformed_native_usage_stays_unknown(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 4},
                      {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": -1},
                      {"input_tokens": 10, "output_tokens": 4, "cache_creation_input_tokens": 3,
                       "cache_creation": {"ephemeral_5m_input_tokens": 4}},
                      {"input_tokens": 10, "output_tokens": 4, "output_tokens_details": {"thinking_tokens": 5}}):
            with self.subTest(usage=usage):
                data = response()
                data["usage"] = usage
                transport = SequenceTransport([{"input_tokens": 10}, data])
                adapter, scratch = self.adapter(transport)
                error = await self.assert_code(adapter, "missing_usage" if usage is None else "invalid_usage")
                self.assertTrue(error.remote_usage_unknown)
                self.assertIsNone(error.known_usage)
                self.assertTrue(scratch.select("call_end")[-1]["remote_usage_unknown"])
                self.assertEqual(len(transport.requests), 2)

    async def test_usage_metadata_hook_cannot_mutate_confirmed_budget_counters(self):
        def audit(event, **fields):
            if event == "response_received" and fields.get("known_usage"):
                fields["known_usage"]["input_tokens"] = 0
                fields["native_usage"]["input_tokens"] = 0

        adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, response(inp=101)]))
        error = await self.assert_code(adapter, "provider_token_limit_breach", audit=audit)
        self.assertEqual(error.known_usage["input_tokens"], 101)

    async def test_actual_usage_breach_keeps_known_counters_and_never_retries(self):
        for data in (response(inp=101), response(out=21)):
            transport = SequenceTransport([{"input_tokens": 10}, data])
            adapter, _ = self.adapter(transport)
            error = await self.assert_code(adapter, "provider_token_limit_breach")
            self.assertFalse(error.remote_usage_unknown)
            self.assertEqual(error.known_usage["output_tokens"], data["usage"]["output_tokens"])
            self.assertEqual(len(transport.requests), 2)

    async def test_only_text_emitted_thinking_and_signatures_remain_scratch(self):
        data = response()
        data["content"] = [{"type": "thinking", "thinking": "private reasoning", "signature": "opaque-signature"},
                           {"type": "redacted_thinking", "data": "opaque-ciphertext"},
                           {"type": "text", "text": "visible "}, {"type": "text", "text": "answer"}]
        adapter, scratch = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
        result = await self.invoke(adapter)
        self.assertEqual(result.text, "visible answer")
        self.assertEqual(scratch.select("http_response")[-1]["payload"]["content"], data["content"])
        self.assertNotIn("private reasoning", str(scratch.select("call_end")[-1]["result"]))

    async def test_bad_thinking_signature_or_unexpected_tool_blocks_decline_with_known_usage(self):
        for block in ({"type": "thinking", "thinking": "private"},
                      {"type": "thinking", "thinking": 4, "signature": "sig"},
                      {"type": "redacted_thinking", "data": ""},
                      {"type": "tool_use", "id": "toolu_test", "name": "npmi", "input": {}},
                      {"type": "server_tool_use", "id": "srvtoolu_test", "name": "web_search", "input": {}},
                      {"type": "text", "text": 4}):
            with self.subTest(block=block):
                data = response()
                data["content"].append(block)
                adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
                error = await self.assert_code(adapter, "unexpected_provider_item" if "tool" in block["type"] else "invalid_output")
                self.assertFalse(error.remote_usage_unknown)
                self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})

    async def test_incomplete_max_tokens_tool_use_and_refusal_never_emit_partial_text(self):
        for reason, code in ((None, "incomplete_response"), ("max_tokens", "incomplete_response"),
                             ("tool_use", "incomplete_response"), ("pause_turn", "incomplete_response"),
                             ("stop_sequence", "incomplete_response"), ("refusal", "provider_refusal")):
            with self.subTest(reason=reason):
                data = response("partial text")
                data["stop_reason"] = reason
                adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
                error = await self.assert_code(adapter, code)
                self.assertFalse(error.remote_usage_unknown)
        data = response("apparently normal text")
        data["stop_details"] = {"type": "refusal", "category": "synthetic"}
        adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
        await self.assert_code(adapter, "provider_refusal")

    async def test_malformed_identity_content_or_surrogate_retains_known_usage(self):
        for field, value in (("type", "response"), ("role", "user"), ("model", "other-model"),
                             ("id", ""), ("content", None), ("content", ["text"]),
                             ("content", [{"type": "text", "text": "\ud800"}])):
            with self.subTest(field=field, value=value):
                data = response()
                data[field] = value
                adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
                with self.assertRaises(GovernedError) as caught:
                    await self.invoke(adapter)
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertEqual(caught.exception.known_usage, {"input_tokens": 10, "output_tokens": 4})

    async def test_empty_or_thinking_only_output_cannot_become_visible_reply(self):
        for content in ([], [{"type": "text", "text": "  "}],
                        [{"type": "thinking", "thinking": "private", "signature": "sig"}]):
            data = response()
            data["content"] = content
            adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, data]))
            await self.assert_code(adapter, "empty_output")

    async def test_native_schema_for_existing_three_shapes_counts_same_format(self):
        for schema, text in ((summary_schema(128), '{"summary":"short"}'),
                             (LABEL_SCHEMA, '{"marks":["specific"]}'),
                             (BOT_REPLY_SCHEMA, '{"action":"skip","text":""}')):
            with self.subTest(schema=schema):
                transport = SequenceTransport([{"input_tokens": 10}, response(text)])
                adapter, _ = self.adapter(transport)
                result = await self.invoke(adapter, schema=schema)
                self.assertEqual(result.text, text)
                count_format = transport.requests[0][1]["output_config"]["format"]
                self.assertEqual(count_format["type"], "json_schema")
                self.assertEqual(count_format, transport.requests[1][1]["output_config"]["format"])
                wire = json.dumps(count_format)
                self.assertNotIn('"maxLength"', wire)
                self.assertNotIn('"maxItems"', wire)
                self.assertNotIn('"minLength"', wire)
                self.assertEqual(transport.requests[-1][1]["tools"], [])

    async def test_local_original_schema_constraints_remain_authoritative(self):
        cases = ((summary_schema(128), '{"summary":""}'),
                 (summary_schema(128), json.dumps({"summary": "s" * 33})),
                 (LABEL_SCHEMA, json.dumps({"marks": ["x"] * 9})),
                 (LABEL_SCHEMA, json.dumps({"marks": ["x" * 41]})),
                 (LABEL_SCHEMA, '{"marks":[" "]}'),
                 (BOT_REPLY_SCHEMA, '{"action":"unknown","text":""}'),
                 (BOT_REPLY_SCHEMA, '{"action":"skip","text":"","extra":0}'),
                 (BOT_REPLY_SCHEMA, '{"action":"skip","action":"reply","text":"a"}'),
                 (BOT_REPLY_SCHEMA, '{"action":"skip","text":"\\ud800"}'))
        for schema, text in cases:
            with self.subTest(text=text):
                adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, response(text)]))
                error = await self.assert_code(adapter, "invalid_structured_output", schema=schema)
                self.assertFalse(error.remote_usage_unknown)

    async def test_unsupported_schema_and_tool_input_fail_before_count(self):
        for schema in ({"type": "string", "pattern": "(?=x)"}, {"type": "integer", "exclusiveMinimum": 0},
                       {"type": "object", "properties": {}, "additionalProperties": True}):
            transport = SequenceTransport([])
            adapter, _ = self.adapter(transport)
            await self.assert_code(adapter, "unsupported_output_schema", schema=schema)
            self.assertEqual(transport.requests, [])
        for messages in ([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "x"}]}],
                         [{"role": "system", "content": "privileged"}],
                         [{"role": "assistant", "content": "prefill"}],
                         [{"role": "user", "content": "\ud800"}]):
            transport = SequenceTransport([])
            adapter, _ = self.adapter(transport)
            with self.assertRaises(GovernedError) as caught:
                await adapter.call("reply", "instructions", messages, "trace")
            self.assertFalse(caught.exception.remote_usage_unknown)
            self.assertEqual(transport.requests, [])

    async def test_numeric_bounds_use_deterministic_native_description_and_same_count_schema(self):
        schema = {"type": "integer", "description": "Span index.", "maximum": 4, "minimum": 0}
        original = deepcopy(schema)
        for value in (0, 4):
            with self.subTest(value=value):
                transport = SequenceTransport([{"input_tokens": 10}, response(str(value))])
                adapter, scratch = self.adapter(transport)
                result = await self.invoke(adapter, schema=schema)
                self.assertEqual(result.text, str(value))
                wire = transport.requests[0][1]["output_config"]["format"]["schema"]
                self.assertEqual(wire, {"type": "integer",
                                       "description": "Span index. Local acceptance constraints: minimum: 0; maximum: 4."})
                self.assertEqual(wire, transport.requests[1][1]["output_config"]["format"]["schema"])
                self.assertEqual(scratch.select("call_start")[-1]["response_schema"], original)
        self.assertEqual(schema, original)

    async def test_number_bounds_are_inclusive_for_finite_integer_and_fractional_values(self):
        schema = {"type": "number", "minimum": -1.5, "maximum": 2}
        for value in (-1.5, 0, 0.25, 2):
            transport = SequenceTransport([{"input_tokens": 10}, response(json.dumps(value))])
            adapter, _ = self.adapter(transport)
            result = await self.invoke(adapter, schema=schema)
            self.assertEqual(json.loads(result.text), value)
            self.assertEqual(result.input_tokens, 10)
            self.assertEqual(len(transport.requests), 2)

    async def test_numeric_violations_boolean_and_fractional_integer_keep_confirmed_usage(self):
        for schema, values in (({"type": "integer", "minimum": 0, "maximum": 4}, (-1, 5, True, 1.5)),
                               ({"type": "number", "minimum": -1.5, "maximum": 2}, (-1.51, 2.01, False))):
            for value in values:
                with self.subTest(schema=schema, value=value):
                    transport = SequenceTransport([{"input_tokens": 10}, response(json.dumps(value))])
                    adapter, scratch = self.adapter(transport)
                    error = await self.assert_code(adapter, "invalid_structured_output", schema=schema)
                    self.assertFalse(error.remote_usage_unknown)
                    self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})
                    self.assertEqual(scratch.select("call_end")[-1]["status"], "failed")
                    self.assertEqual(len(transport.requests), 2)

    async def test_malformed_numeric_constraints_decline_before_any_http(self):
        schemas = [{"type": kind, key: value}
                   for kind, key, value in (("string", "minimum", 0), ("array", "maximum", 1),
                                            ("integer", "minimum", True), ("integer", "maximum", False),
                                            ("number", "minimum", "0"), ("number", "maximum", None),
                                            ("number", "minimum", float("nan")),
                                            ("number", "maximum", float("inf")),
                                            ("integer", "minimum", float("-inf")))]
        schemas.append({"type": "number", "minimum": 2.0, "maximum": 1})
        for schema in schemas:
            with self.subTest(schema=schema):
                with self.assertRaises(GovernedError) as direct:
                    _schema_wire(schema)
                self.assertEqual(direct.exception.code, "unsupported_output_schema")
                transport = SequenceTransport([])
                adapter, _ = self.adapter(transport)
                with self.assertRaises(GovernedError) as caught:
                    await self.invoke(adapter, schema=schema)
                self.assertIn(caught.exception.code, {"unsupported_output_schema", "invalid_claude_input"})
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertIsNone(caught.exception.known_usage)
                self.assertEqual(transport.requests, [])

    async def test_nonfinite_numeric_output_fails_with_known_usage_no_retry(self):
        for text in ("NaN", "Infinity", "-Infinity", "1e999"):
            transport = SequenceTransport([{"input_tokens": 10}, response(text)])
            adapter, _ = self.adapter(transport)
            error = await self.assert_code(adapter, "invalid_structured_output", schema={"type": "number", "minimum": 0})
            self.assertFalse(error.remote_usage_unknown)
            self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})
            self.assertEqual(len(transport.requests), 2)

    async def test_real_richard_span_schema_admits_native_and_rejects_negative_indices(self):
        from indeces.richard_chat_selection import richard_schema

        schema = richard_schema(1, [{"term": "alpha"}], [])
        payload = {"selected_record_ids": [7], "richard_plan": {
            "mode": "single", "confidence": "high", "seeds_A": [{"term": "alpha", "start": 0, "end": 5}],
            "seeds_B": [], "target": "", "level": "", "target_start": 0, "target_end": 0}}
        for negative in (False, True):
            with self.subTest(negative=negative):
                value = deepcopy(payload)
                if negative:
                    value["richard_plan"]["seeds_A"][0]["start"] = -1
                transport = SequenceTransport([{"input_tokens": 10}, response(json.dumps(value))])
                adapter, _ = self.adapter(transport)
                if negative:
                    error = await self.assert_code(adapter, "invalid_structured_output", schema=schema)
                    self.assertFalse(error.remote_usage_unknown)
                    self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})
                else:
                    result = await self.invoke(adapter, schema=schema)
                    self.assertEqual(json.loads(result.text), payload)
                numeric = transport.requests[0][1]["output_config"]["format"]["schema"]["properties"][
                    "richard_plan"]["properties"]["seeds_A"]["items"]["properties"]["start"]
                self.assertEqual(numeric, {"type": "integer", "description": "Local acceptance constraints: minimum: 0."})
                self.assertEqual(len(transport.requests), 2)

    async def test_numeric_schema_primary_failure_survives_terminal_audit_failure(self):
        def audit(event, **fields):
            if event == "call_end":
                raise RuntimeError("secondary terminal metadata failure")

        transport = SequenceTransport([{"input_tokens": 10}, response("-1")])
        adapter, _ = self.adapter(transport)
        error = await self.assert_code(adapter, "invalid_structured_output",
                                       schema={"type": "integer", "minimum": 0}, audit=audit)
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})
        self.assertEqual(len(transport.requests), 2)

    async def test_unknown_generation_failure_one_attempt_then_circuit(self):
        original = GovernedError("provider_network_error")
        transport = SequenceTransport([{"input_tokens": 10}, original])
        adapter, _ = self.adapter(transport, threshold=1)
        error = await self.assert_code(adapter, "provider_network_error")
        self.assertIs(error, original)
        self.assertTrue(error.remote_usage_unknown)
        await self.assert_code(adapter, "circuit_open")
        self.assertEqual(len(transport.requests), 2)

    async def test_count_timeout_never_generates_generation_timeout_remains_unknown(self):
        for wait_path, unknown in (("/messages/count_tokens", False), ("/messages", True)):
            seen = []

            async def transport(path, payload):
                seen.append(path)
                if path == wait_path:
                    await asyncio.Event().wait()
                return {"input_tokens": 10}

            adapter, _ = self.adapter(transport, seconds=0.01)
            error = await self.assert_code(adapter, "stage_timeout")
            self.assertIs(error.remote_usage_unknown, unknown)
            self.assertEqual(seen, ["/messages/count_tokens"] + (["/messages"] if unknown else []))

    async def test_waiting_slot_uses_original_deadline_without_remote_attempt(self):
        transport = SequenceTransport([])
        adapter, _ = self.adapter(transport, seconds=0.01)
        await adapter._lock.acquire()
        try:
            error = await self.assert_code(adapter, "model_slot_timeout")
        finally:
            adapter._lock.release()
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(transport.requests, [])

    async def test_two_stages_share_exclusive_transport_lock(self):
        entered, release = asyncio.Event(), asyncio.Event()
        paths = []
        active = maximum = 0

        async def transport(path, payload):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            paths.append(path)
            try:
                if len(paths) == 1:
                    entered.set()
                    await release.wait()
                await asyncio.sleep(0)
                return {"input_tokens": 10} if path.endswith("count_tokens") else response()
            finally:
                active -= 1

        adapter, _ = self.adapter(transport)
        first = asyncio.create_task(self.invoke(adapter))
        await entered.wait()
        second = asyncio.create_task(self.invoke(adapter, "summary"))
        await asyncio.sleep(0)
        self.assertEqual(paths, ["/messages/count_tokens"])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(maximum, 1)
        self.assertEqual(paths, ["/messages/count_tokens", "/messages"] * 2)

    async def test_cancelled_generation_primary_survives_terminal_audit_and_scratch_faults(self):
        for sink in ("audit", "scratch"):
            entered = asyncio.Event()
            paths = []
            primary = []

            async def transport(path, payload):
                paths.append(path)
                if path.endswith("count_tokens"):
                    return {"input_tokens": 10}
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    primary.append(error)
                    raise

            def audit(event, **fields):
                if sink == "audit" and event == "call_end":
                    raise RuntimeError("secondary terminal audit")

            scratch = Scratch("call_end" if sink == "scratch" else None)
            adapter, _ = self.adapter(transport, scratch=scratch)
            task = asyncio.create_task(self.invoke(adapter, audit=audit))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertIs(caught.exception, primary[0])
            self.assertTrue(task.cancelled())
            self.assertTrue(caught.exception.remote_usage_unknown)
            self.assertEqual(paths, ["/messages/count_tokens", "/messages"])

    async def test_cancel_after_confirmed_receipt_keeps_known_usage_with_terminal_fault(self):
        adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, response()]), scratch=Scratch("call_end"))
        post = adapter._post
        entered = asyncio.Event()
        primary = []

        async def post_then_barrier(path, *args, **kwargs):
            data = await post(path, *args, **kwargs)
            if path == "/messages":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    primary.append(error)
                    raise
            return data

        adapter._post = post_then_barrier
        task = asyncio.create_task(self.invoke(adapter))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertIs(caught.exception, primary[0])
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 10, "output_tokens": 4})

    async def test_primary_governed_and_arbitrary_errors_survive_secondary_terminal_sinks(self):
        for primary in (GovernedError("provider_network_error"), RuntimeError("synthetic primary transport")):
            for sink in ("audit", "scratch"):
                transport = SequenceTransport([{"input_tokens": 10}, primary])
                adapter, _ = self.adapter(transport, scratch=Scratch("call_end" if sink == "scratch" else None))

                def audit(event, **fields):
                    if event == "call_end" and sink == "audit":
                        raise RuntimeError("secondary terminal")

                with self.assertRaises(type(primary)) as caught:
                    await self.invoke(adapter, audit=audit)
                self.assertIs(caught.exception, primary)
                self.assertTrue(caught.exception.remote_usage_unknown)
                self.assertEqual(len(transport.requests), 2)

    async def test_receipt_sink_failure_and_success_terminal_failure_keep_known_usage(self):
        for event in ("http_response", "call_end"):
            primary = RuntimeError("synthetic sink primary")
            # Fail only the generation's http_response, after its receipt.
            class GenerationScratch(Scratch):
                def write(self, name, **fields):
                    super().write(name, **fields)
                    if name == event and (event == "call_end" or fields["path"] == "/messages"):
                        raise primary

            transport = SequenceTransport([{"input_tokens": 10}, response()])
            adapter, _ = self.adapter(transport, scratch=GenerationScratch())
            with self.assertRaises(RuntimeError) as caught:
                await self.invoke(adapter)
            self.assertIs(caught.exception, primary)
            self.assertFalse(primary.remote_usage_unknown)
            self.assertEqual(primary.known_usage, {"input_tokens": 10, "output_tokens": 4})
            self.assertEqual(len(transport.requests), 2)

    async def test_audit_intent_must_succeed_before_each_remote_attempt(self):
        for phase, paths in (("input_count", []), ("generation", ["/messages/count_tokens"])):
            primary = RuntimeError("synthetic durable intent")

            def audit(event, **fields):
                if event == "request_intent" and fields["phase"] == phase:
                    raise primary

            transport = SequenceTransport([{"input_tokens": 10}, response()])
            adapter, _ = self.adapter(transport)
            with self.assertRaises(RuntimeError) as caught:
                await self.invoke(adapter, audit=audit)
            self.assertIs(caught.exception, primary)
            self.assertFalse(primary.remote_usage_unknown)
            self.assertEqual([path for path, _ in transport.requests], paths)

    async def test_async_audit_hook_declines_before_network(self):
        async def audit(event, **fields):
            pass

        transport = SequenceTransport([])
        adapter, _ = self.adapter(transport)
        error = await self.assert_code(adapter, "async_call_audit_not_supported", audit=audit)
        self.assertFalse(error.remote_usage_unknown)
        self.assertEqual(transport.requests, [])


class NativeUsageTests(unittest.TestCase):
    def test_cache_breakdown_not_double_counted_and_zero_is_valid(self):
        normalized, native = _native_usage({"input_tokens": 0, "output_tokens": 0,
                                           "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
                                           "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 0}})
        self.assertEqual(normalized, {"input_tokens": 5, "output_tokens": 0})
        self.assertEqual(native["cache_creation_input_tokens"], 2)

    def test_constructor_refuses_wrong_endpoint_model_provider_or_effort(self):
        baseline = SimpleNamespace(provider="anthropic", model="claude-opus-5-5", base_url=CLAUDE_BASE_URL,
                                   budgets={"reply": Budget(100, 20, 1, "high")},
                                   failure_threshold=3, cooldown_seconds=30)
        for field, value in (("base_url", "https://api.openai.com/v1"), ("base_url", "https://unconfirmed.example/v1"),
                             ("model", "claude-opus-5"), ("provider", "openai"),
                             ("budgets", {"reply": Budget(100, 20, 1, "medium")})):
            config = deepcopy(baseline)
            setattr(config, field, value)
            with self.assertRaises(ValueError):
                ClaudeAdapter(config, Scratch(), "synthetic-key")

    def test_repr_redacts_credentials(self):
        config = SimpleNamespace(provider="anthropic", model="claude-opus-5-5", base_url=CLAUDE_BASE_URL,
                                 budgets={"reply": Budget(100, 20, 1, "high")})
        adapter = ClaudeAdapter(config, Scratch(), "never-display-this")
        self.assertNotIn("never-display-this", repr(adapter))

    def test_one_million_context_is_independent_of_legacy_budget_ceiling(self):
        config = SimpleNamespace(provider="anthropic", model="claude-opus-5-5", base_url=CLAUDE_BASE_URL,
                                 budgets={"reply": Budget(1_000_000, 1, 1, "high")})
        with self.assertRaises(ValueError):
            ClaudeAdapter(config, Scratch())


class MockHttpResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self.headers = {"request-id": "request_test"}
        self.payload = payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        value = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        for offset in range(0, len(value), size):
            yield value[offset:offset + size]


class MockSession:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []
        self.closed = 0

    def post(self, url, **kwargs):
        self.requests.append((url, deepcopy(kwargs)))
        return self.responses.popleft()

    async def close(self):
        self.closed += 1


class ClaudeHttpTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, session, key="synthetic-only"):
        config = SimpleNamespace(provider="anthropic", model="claude-opus-5-5", base_url=CLAUDE_BASE_URL,
                                 budgets={"reply": Budget(100, 20, 1, "high")}, failure_threshold=3,
                                 cooldown_seconds=30, verbosity="high")
        adapter = ClaudeAdapter(config, Scratch(), key)
        adapter._session = session
        return adapter

    def fake_aiohttp(self):
        return SimpleNamespace(ClientError=type("SyntheticClientError", (Exception,), {}))

    async def invoke(self, adapter):
        return await adapter.call("reply", "instructions", [{"role": "user", "content": "synthetic"}], "trace")

    async def test_native_headers_frozen_json_no_redirects_close_and_receipt_id(self):
        session = MockSession([MockHttpResponse({"input_tokens": 10}), MockHttpResponse(response())])
        adapter = self.adapter(session)
        audit = []
        with patch.dict(sys.modules, {"aiohttp": self.fake_aiohttp()}):
            result = await adapter.call("reply", "instructions", [{"role": "user", "content": "synthetic"}], "trace",
                                        audit=lambda event, **fields: audit.append((event, deepcopy(fields))))
        for url, options in session.requests:
            self.assertTrue(url.startswith(CLAUDE_BASE_URL + "/messages"))
            self.assertIs(options["allow_redirects"], False)
            self.assertIsInstance(options["data"], bytes)
            self.assertEqual(options["headers"]["x-api-key"], "synthetic-only")
            self.assertEqual(options["headers"]["anthropic-version"], "2023-06-01")
            self.assertEqual(options["headers"]["Content-Type"], "application/json")
            self.assertNotIn("Authorization", options["headers"])
        counted = json.loads(session.requests[0][1]["data"])
        generated = json.loads(session.requests[1][1]["data"])
        self.assertEqual(counted, {k: v for k, v in generated.items() if k not in {"max_tokens", "stream"}})
        self.assertEqual(result.response_id, "msg_test")
        self.assertEqual([f for e, f in audit if e == "response_received"][-1]["provider_request_id"], "request_test")
        self.assertNotIn("synthetic-only", repr(audit))
        await adapter.close()
        await adapter.close()
        self.assertEqual(session.closed, 1)

    async def test_redirect_and_generation_http_error_have_no_followup_retry(self):
        for generation, status in ((False, 302), (True, 401), (True, 429), (True, 503)):
            responses = ([MockHttpResponse({"input_tokens": 10})] if generation else [])
            responses.append(MockHttpResponse({"error": "never surface provider text"}, status))
            session = MockSession(responses)
            adapter = self.adapter(session)
            with patch.dict(sys.modules, {"aiohttp": self.fake_aiohttp()}), self.assertRaises(GovernedError) as caught:
                await self.invoke(adapter)
            self.assertEqual(caught.exception.code, f"provider_http_{status}")
            self.assertIs(caught.exception.remote_usage_unknown, generation)
            self.assertEqual(len(session.requests), 2 if generation else 1)
            self.assertTrue(all(options["allow_redirects"] is False for _, options in session.requests))

    async def test_missing_key_closes_before_http_even_with_instantiated_session(self):
        session = MockSession([])
        adapter = self.adapter(session, key="")
        with patch.dict(sys.modules, {"aiohttp": self.fake_aiohttp()}), self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter)
        self.assertEqual(caught.exception.code, "missing_anthropic_key")
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(session.requests, [])

    async def test_invalid_or_oversized_generation_json_remains_unknown(self):
        for raw, code in ((b'{"usage": {"input_tokens":10,"output_tokens":4}, "usage":{}}', "invalid_provider_json"),
                          (b'{"usage":NaN}', "invalid_provider_json"),
                          (b"x" * (2 * 1024 * 1024 + 1), "provider_response_too_large")):
            session = MockSession([MockHttpResponse({"input_tokens": 10}), MockHttpResponse(raw)])
            adapter = self.adapter(session)
            with patch.dict(sys.modules, {"aiohttp": self.fake_aiohttp()}), self.assertRaises(GovernedError) as caught:
                await self.invoke(adapter)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.remote_usage_unknown)
            self.assertIsNone(caught.exception.known_usage)
            self.assertEqual(len(session.requests), 2)


if __name__ == "__main__":
    unittest.main()
