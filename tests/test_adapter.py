from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter, reservation
from indeces.config import AdapterConfig, Budget, load_config
from indeces.contracts import GovernedError


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))

    def select(self, event):
        return [fields for name, fields in self.events if name == event]


def completed(text="a visible reply", *, inp=10, out=4):
    return {"id": "response-1", "status": "completed", "usage": {"input_tokens": inp, "output_tokens": out},
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


class SequenceTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []

    async def __call__(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, transport, *, seconds=1.0, threshold=3, input_limit=100, output_limit=20, verbosity="high"):
        budget = Budget(input_limit, output_limit, seconds)
        config = AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1",
                               {stage: budget for stage in ("reply", "summary", "label")},
                               failure_threshold=threshold, cooldown_seconds=60.0, verbosity=verbosity)
        scratch = Scratch()
        return OpenAIAdapter(config, scratch, api_key="never-log-this-key", request=transport), scratch

    async def invoke(self, adapter, stage="reply", schema=None, *, input_limit=None, audit=None, budget_override=None):
        return await adapter.call(stage, "instructions", [{"role": "user", "content": "source"}], "trace-1", schema,
                                  input_limit=input_limit, audit=audit, budget_override=budget_override)

    async def test_durable_audit_reserves_before_generation_and_excludes_raw_payload(self):
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        async def transport(path, payload):
            self.assertEqual(events[-1][0], "request_intent")
            self.assertEqual(events[-1][1]["path"], path)
            if path == "/responses":
                gate = next(fields for event, fields in events if event == "input_gate")
                self.assertEqual((gate["reserved_input_tokens"], gate["reserved_output_tokens"]), (10, 20))
                self.assertTrue(gate["admitted"])
                return completed("raw private answer", inp=11, out=5)
            return {"input_tokens": 10}

        adapter, scratch = self.adapter(transport)
        await self.invoke(adapter, audit=audit)
        intents = [fields for event, fields in events if event == "request_intent"]
        self.assertEqual(len({fields["client_request_id"] for fields in intents}), 2)
        self.assertTrue(intents[0]["input_count_is_non_generating"])
        self.assertFalse(intents[1]["input_count_is_non_generating"])
        received = [fields for event, fields in events if event == "response_received"]
        self.assertIsNone(received[0]["known_usage"])
        self.assertEqual(received[1]["known_usage"], {"input_tokens": 11, "output_tokens": 5})
        self.assertEqual(received[1]["response_id"], "response-1")
        terminal = events[-1][1]
        self.assertEqual((events[-1][0], terminal["status"]), ("call_end", "completed"))
        self.assertGreaterEqual(terminal["elapsed_seconds"], 0)
        self.assertNotIn("raw private answer", repr(events))
        self.assertNotIn("instructions", repr(events))
        self.assertNotIn("source", repr(events))
        self.assertEqual(scratch.select("call_end")[0]["result"]["text"], "raw private answer")

    async def test_audit_rejection_before_send_never_issues_generation(self):
        for rejected_event, rejected_phase, request_count in (("request_intent", "input_count", 0),
                                                             ("input_gate", None, 1),
                                                             ("request_intent", "generation", 1)):
            with self.subTest(event=rejected_event, phase=rejected_phase):
                transport = SequenceTransport([{"input_tokens": 10}, completed()])
                adapter, _ = self.adapter(transport, threshold=1)

                def audit(event, **fields):
                    if event == rejected_event and (rejected_phase is None or fields["phase"] == rejected_phase):
                        raise GovernedError("audit_reservation_refused")

                with self.assertRaises(GovernedError) as caught:
                    await self.invoke(adapter, audit=audit)
                self.assertEqual(caught.exception.code, "audit_reservation_refused")
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertEqual(len(transport.requests), request_count)
                self.assertEqual(adapter._circuits["reply"], {"failures": 0, "until": 0.0})

    async def test_usage_receipt_hook_failure_keeps_usage_and_stops_without_retry(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed(inp=17, out=6)])
        adapter, scratch = self.adapter(transport)
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))
            if event == "response_received" and fields["phase"] == "generation":
                raise RuntimeError("receipt sink failed")

        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(adapter, audit=audit)
        expected = {"input_tokens": 17, "output_tokens": 6}
        self.assertEqual(caught.exception.known_usage, expected)
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(events[-1][0], "call_end")
        self.assertEqual(events[-1][1]["known_usage"], expected)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(len(scratch.select("http_response")), 1)

    async def test_usage_is_durable_before_scratch_response_failure(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed(inp=17, out=6)])
        adapter, scratch = self.adapter(transport)
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        original_write = scratch.write

        def write(event, **fields):
            if event == "http_response" and fields["path"] == "/responses":
                self.assertEqual(events[-1][0], "response_received")
                self.assertEqual(events[-1][1]["known_usage"], {"input_tokens": 17, "output_tokens": 6})
                raise RuntimeError("scratch failed")
            original_write(event, **fields)

        scratch.write = write
        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(adapter, audit=audit)
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 17, "output_tokens": 6})
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(events[-1][0], "call_end")
        self.assertEqual(events[-1][1]["known_usage"], caught.exception.known_usage)
        self.assertEqual(len(transport.requests), 2)

    async def test_count_scalar_is_durable_before_scratch_count_response_failure(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed()])
        adapter, scratch = self.adapter(transport)
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        original_write = scratch.write

        def write(event, **fields):
            if event == "http_response" and fields["path"] == "/responses/input_tokens":
                self.assertEqual(events[-1][0], "response_received")
                self.assertEqual(events[-1][1]["counted_input_tokens"], 10)
                self.assertIsNone(events[-1][1]["known_usage"])
                raise RuntimeError("scratch count response failed")
            original_write(event, **fields)

        scratch.write = write
        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(adapter, audit=audit)
        self.assertIsNone(caught.exception.known_usage)
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens"])
        self.assertNotIn("input_gate", [event for event, _ in events])
        self.assertFalse(events[-1][1]["generation_request_started"])

    async def test_invalid_output_has_known_usage_in_independent_audit(self):
        response = completed(inp=17, out=6)
        response["output"] = "invalid"
        transport = SequenceTransport([{"input_tokens": 10}, response])
        adapter, _ = self.adapter(transport)
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, audit=audit)
        self.assertEqual(caught.exception.code, "invalid_output")
        self.assertEqual(events[-1][1]["known_usage"], {"input_tokens": 17, "output_tokens": 6})
        self.assertFalse(events[-1][1]["remote_usage_unknown"])

    async def test_terminal_audit_failure_preserves_usage_on_original_exception(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed(inp=17, out=6)])
        adapter, _ = self.adapter(transport)

        def audit(event, **fields):
            if event == "call_end":
                raise RuntimeError("terminal sink failed")

        with self.assertRaises(RuntimeError) as caught:
            await self.invoke(adapter, audit=audit)
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 17, "output_tokens": 6})
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(len(transport.requests), 2)

    async def test_unique_client_headers_and_provider_id_capture_precede_body(self):
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        class Content:
            def __init__(content_self, payload, provider_id):
                content_self.payload, content_self.provider_id = payload, provider_id

            async def iter_chunked(content_self, size):
                self.assertEqual(events[-1][0], "response_headers")
                self.assertEqual(events[-1][1]["provider_request_id"], content_self.provider_id)
                yield json.dumps(content_self.payload).encode("utf-8")

        class Response:
            def __init__(response_self, payload, provider_id):
                response_self.status = 200
                response_self.headers = {"x-request-id": provider_id}
                response_self.content = Content(payload, provider_id)

            async def __aenter__(response_self):
                return response_self

            async def __aexit__(response_self, *args):
                return None

        class Session:
            def __init__(session_self):
                session_self.requests = []

            def post(session_self, url, **options):
                self.assertEqual(events[-1][0], "request_intent")
                self.assertEqual(options["headers"]["X-Client-Request-Id"], events[-1][1]["client_request_id"])
                session_self.requests.append((url, deepcopy(options)))
                counting = url.endswith("input_tokens")
                return Response({"input_tokens": 10} if counting else completed(),
                                "req-count" if counting else "req-generation")

        adapter, scratch = self.adapter(None)
        session = Session()
        adapter._session = session
        await self.invoke(adapter, audit=audit)
        request_ids = [options["headers"]["X-Client-Request-Id"] for _, options in session.requests]
        self.assertEqual(len(set(request_ids)), 2)
        self.assertEqual(scratch.select("http_response")[-1]["provider_request_id"], "req-generation")
        self.assertEqual(events[-1][1]["provider_request_id"], "req-generation")
        self.assertEqual(events[-1][1]["response_id"], "response-1")
        self.assertNotIn("never-log-this-key", repr(events) + repr(scratch.events))

    async def test_label_deadline_override_is_scoped_and_defaults_remain_unchanged(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed()] * 2)
        adapter, scratch = self.adapter(transport, seconds=15.0)
        configured = adapter.config.budgets["label"]
        timeouts = []
        original_timeout = asyncio.timeout

        def tracked_timeout(seconds):
            timeouts.append(seconds)
            return original_timeout(seconds)

        with patch("indeces.adapter.asyncio.timeout", side_effect=tracked_timeout):
            await self.invoke(adapter, "label", budget_override=Budget(100, 20, 45.0))
            await self.invoke(adapter, "label")
        self.assertEqual(timeouts, [45.0, 15.0])
        self.assertIs(adapter.config.budgets["label"], configured)
        self.assertEqual([row["budget"]["seconds"] for row in scratch.select("call_start")], [45.0, 15.0])
        self.assertEqual([payload["max_output_tokens"] for path, payload in transport.requests if path == "/responses"],
                         [20, 20])

    async def test_invalid_budget_override_never_issues_a_request(self):
        overrides = [("label", "not-a-budget"), ("label", Budget(101, 20, 15.0)),
                     ("label", Budget(100, 21, 15.0)), ("label", Budget(100, 20, 46.0)),
                     ("label", Budget(100, 20, 15.0, reasoning="low")),
                     ("reply", Budget(100, 20, 45.0))]
        for stage, override in overrides:
            with self.subTest(stage=stage, override=override):
                transport = SequenceTransport([])
                adapter, scratch = self.adapter(transport, seconds=15.0)
                with self.assertRaises(GovernedError) as caught:
                    await self.invoke(adapter, stage, budget_override=override)
                self.assertEqual(caught.exception.code, "invalid_call_budget_override")
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertEqual(transport.requests, [])
                self.assertEqual(scratch.events, [])

    async def test_coroutine_audit_is_rejected_without_issuing(self):
        transport = SequenceTransport([])
        adapter, _ = self.adapter(transport)

        async def audit(event, **fields):
            return None

        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, audit=audit)
        self.assertEqual(caught.exception.code, "async_call_audit_not_supported")
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual(transport.requests, [])

    async def test_circuit_rejection_has_terminal_audit_without_requests(self):
        transport = SequenceTransport([])
        adapter, _ = self.adapter(transport)
        adapter._circuits["label"]["until"] = time.monotonic() + 60
        events = []

        def audit(event, **fields):
            events.append((event, deepcopy(fields)))

        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, "label", audit=audit)
        self.assertEqual(caught.exception.code, "circuit_open")
        self.assertEqual([event for event, _ in events], ["call_start", "call_end"])
        self.assertFalse(events[-1][1]["remote_usage_unknown"])
        self.assertEqual(events[-1][1]["code"], "circuit_open")
        self.assertEqual(transport.requests, [])

    async def assert_code(self, adapter, code, *, stage="reply"):
        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, stage)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def test_count_precedes_generation_and_both_payloads_are_traced(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed()])
        adapter, scratch = self.adapter(transport)
        schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
        result = await self.invoke(adapter, "summary", schema)
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens", "/responses"])
        count_payload, payload = [data for _, data in transport.requests]
        self.assertEqual(count_payload["text"], payload["text"])
        self.assertEqual(payload["text"]["verbosity"], "high")
        self.assertEqual(payload["text"]["format"]["schema"], schema)
        self.assertEqual(payload["max_output_tokens"], 20)
        self.assertEqual(payload["tools"], [])
        self.assertEqual((payload["store"], payload["stream"], payload["truncation"]), (False, False, "disabled"))
        self.assertEqual((result.text, result.input_tokens, result.output_tokens), ("a visible reply", 10, 4))
        self.assertEqual(len(scratch.select("http_request")), 2)
        self.assertEqual(len(scratch.select("http_response")), 2)
        self.assertEqual(scratch.select("http_request")[1]["payload"], payload)
        self.assertEqual(scratch.select("http_response")[1]["payload"], completed())
        call_ids = {fields["call_id"] for _, fields in scratch.events if "call_id" in fields}
        self.assertEqual(len(call_ids), 1)
        self.assertEqual(scratch.select("call_end")[0]["status"], "completed")
        self.assertNotIn("never-log-this-key", repr(scratch.events) + repr(adapter))

    async def test_all_stages_send_sol_medium_effort_high_verbosity_with_original_caps(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml").adapter
        transport = SequenceTransport([{"input_tokens": 10}, completed()] * 3)
        scratch = Scratch()
        adapter = OpenAIAdapter(config, scratch, request=transport)
        schema = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"], "additionalProperties": False}
        expected = {"label": (4096, 512, 15.0), "summary": (16384, 2048, 60.0),
                    "reply": (16384, 2048, 45.0)}
        for stage in expected:
            await self.invoke(adapter, stage, schema if stage != "reply" else None)
        for index, (stage, caps) in enumerate(expected.items()):
            count_request, request = [payload for _, payload in transport.requests[index * 2:index * 2 + 2]]
            self.assertEqual(count_request["text"], request["text"])
            self.assertEqual(request["text"]["verbosity"], "high")
            self.assertEqual(request["reasoning"], {"effort": "medium"})
            self.assertEqual(request["model"], "gpt-6.1-sol")
            self.assertEqual(count_request["model"], request["model"])
            self.assertEqual(count_request["reasoning"], request["reasoning"])
            self.assertEqual(request["max_output_tokens"], caps[1])
            start = scratch.select("call_start")[index]
            self.assertEqual((start["stage"], start["verbosity"], start["budget"]["reasoning"]), (stage, "high", "medium"))
            self.assertEqual(tuple(start["budget"][key] for key in ("input_tokens", "output_tokens", "seconds")), caps)
            self.assertEqual("format" in request["text"], stage != "reply")

    async def test_explicit_verbosity_is_used_for_plain_and_structured_responses(self):
        schema = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"], "additionalProperties": False}
        for verbosity in ("low", "medium", "high"):
            for response_schema in (None, schema):
                with self.subTest(verbosity=verbosity, structured=response_schema is not None):
                    transport = SequenceTransport([{"input_tokens": 10}, completed()])
                    adapter, scratch = self.adapter(transport, verbosity=verbosity)
                    await self.invoke(adapter, schema=response_schema)
                    text_options = transport.requests[-1][1]["text"]
                    self.assertEqual(text_options["verbosity"], verbosity)
                    self.assertEqual("format" in text_options, response_schema is not None)
                    if response_schema is not None:
                        self.assertEqual(text_options["format"]["schema"], schema)
                    self.assertEqual(scratch.select("call_start")[0]["verbosity"], verbosity)

    async def test_summary_completion_after_old_deadline_uses_approved_cap_without_retry(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.assertEqual(config.runtime.turn_seconds, 130.0)
        baseline = config.adapter
        for seconds, expected_code in ((20.0, "stage_timeout"), (60.0, None)):
            with self.subTest(seconds=seconds):
                offset = 0.0
                requests = []

                async def transport(path, payload):
                    nonlocal offset
                    requests.append((path, deepcopy(payload)))
                    if path.endswith("input_tokens"):
                        return {"input_tokens": 10}
                    # Simulate a 25-second completion without delaying the
                    # suite or changing asyncio's real cancellation clock.
                    offset = 25.0
                    return completed('{"summary":"An attributed continuity summary."}')

                budget = replace(baseline.budgets["summary"], seconds=seconds)
                adapter_config = replace(baseline, budgets={**baseline.budgets, "summary": budget})
                scratch = Scratch()
                adapter = OpenAIAdapter(adapter_config, scratch, request=transport)
                with patch("indeces.adapter.time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)):
                    if expected_code:
                        with self.assertRaises(GovernedError) as caught:
                            await self.invoke(adapter, "summary")
                        self.assertEqual(caught.exception.code, expected_code)
                        self.assertFalse(caught.exception.remote_usage_unknown)
                    else:
                        result = await self.invoke(adapter, "summary")
                        self.assertGreaterEqual(result.elapsed_seconds, 25.0)
                self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
                self.assertEqual(requests[-1][1]["reasoning"], {"effort": "medium"})
                self.assertEqual(requests[-1][1]["max_output_tokens"], 2048)
                self.assertEqual(scratch.select("call_start")[0]["budget"]["seconds"], seconds)

    async def test_count_over_limit_prevents_generation_without_tripping_circuit(self):
        transport = SequenceTransport([{"input_tokens": 101}] * 3)
        adapter, scratch = self.adapter(transport, threshold=1)
        for _ in range(3):
            await self.assert_code(adapter, "input_token_limit")
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens"] * 3)
        self.assertTrue(all(not row["admitted"] for row in scratch.select("input_gate")))
        self.assertEqual(adapter._circuits["reply"]["failures"], 0)

    async def test_remaining_input_allowance_uses_measured_count_and_preserves_stage_caps(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed()])
        adapter, scratch = self.adapter(transport)
        original_budget = adapter.config.budgets["label"]
        result = await self.invoke(adapter, "label", input_limit=12)
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 4))
        self.assertEqual(adapter.config.budgets["label"], original_budget)
        start = scratch.select("call_start")[0]
        self.assertEqual(start["budget"]["input_tokens"], 100)
        self.assertEqual(start["requested_input_limit"], 12)
        self.assertEqual(start["effective_input_limit"], 12)
        gate = scratch.select("input_gate")[0]
        self.assertEqual((gate["limit"], gate["stage_limit"], gate["admitted"]), (12, 100, True))
        request = transport.requests[-1][1]
        self.assertEqual(request["max_output_tokens"], 20)
        self.assertEqual(request["reasoning"], {"effort": "medium"})
        self.assertEqual(request["text"]["verbosity"], "high")
        self.assertEqual(request["model"], "gpt-6.1-sol")

    async def test_count_above_remaining_allowance_never_generates_or_trips_circuit(self):
        transport = SequenceTransport([{"input_tokens": 13}])
        adapter, scratch = self.adapter(transport, threshold=1)
        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, "label", input_limit=12)
        self.assertEqual(caught.exception.code, "input_token_limit")
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens"])
        self.assertEqual(adapter._circuits["label"], {"failures": 0, "until": 0.0})
        self.assertFalse(scratch.select("input_gate")[0]["admitted"])

    async def test_larger_caller_allowance_cannot_expand_stage_input_cap(self):
        for counted, expected in ((100, "completed"), (101, "input_token_limit")):
            with self.subTest(counted=counted):
                transport = SequenceTransport([{"input_tokens": counted}, completed(inp=counted)])
                adapter, scratch = self.adapter(transport)
                if expected == "completed":
                    self.assertEqual((await self.invoke(adapter, input_limit=1000)).input_tokens, counted)
                else:
                    with self.assertRaises(GovernedError) as caught:
                        await self.invoke(adapter, input_limit=1000)
                    self.assertEqual(caught.exception.code, expected)
                    self.assertEqual(len(transport.requests), 1)
                self.assertEqual(scratch.select("call_start")[0]["effective_input_limit"], 100)
                self.assertEqual(scratch.select("input_gate")[0]["limit"], 100)

    async def test_invalid_caller_allowance_rejected_before_any_request(self):
        for value in (True, False, 0, -1, 1.5, "10", [], {}):
            with self.subTest(value=value):
                transport = SequenceTransport([])
                adapter, _ = self.adapter(transport)
                with self.assertRaises(GovernedError) as caught:
                    await self.invoke(adapter, input_limit=value)
                self.assertEqual(caught.exception.code, "invalid_call_input_limit")
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertEqual(transport.requests, [])
                self.assertEqual(adapter._circuits["reply"], {"failures": 0, "until": 0.0})

    async def test_provider_usage_above_remaining_allowance_is_accounted_and_rejected(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed(inp=13, out=4)])
        adapter, scratch = self.adapter(transport)
        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, "label", input_limit=12)
        self.assertEqual(caught.exception.code, "provider_token_limit_breach")
        self.assertEqual(caught.exception.known_usage, {"input_tokens": 13, "output_tokens": 4})
        self.assertFalse(caught.exception.remote_usage_unknown)
        end = scratch.select("call_end")[0]
        self.assertEqual(end["known_usage"], caught.exception.known_usage)
        self.assertTrue(end["generation_usage_received"])
        self.assertEqual(len(transport.requests), 2)

    async def test_invalid_counts_never_generate(self):
        for value in (True, -1, "10", None, 3.5):
            with self.subTest(value=value):
                transport = SequenceTransport([{"input_tokens": value}])
                adapter, scratch = self.adapter(transport)
                await self.assert_code(adapter, "invalid_input_token_count")
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(scratch.select("call_end")[0]["status"], "failed")

    async def test_actual_provider_usage_enforces_both_caps_without_retry(self):
        for inp, out in ((101, 4), (10, 21)):
            with self.subTest(inp=inp, out=out):
                transport = SequenceTransport([{"input_tokens": 10}, completed(inp=inp, out=out)])
                adapter, scratch = self.adapter(transport)
                await self.assert_code(adapter, "provider_token_limit_breach")
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(scratch.select("call_end")[0]["code"], "provider_token_limit_breach")
                self.assertFalse(scratch.select("call_end")[0]["remote_usage_unknown"])
                self.assertTrue(scratch.select("call_end")[0]["generation_usage_received"])
                self.assertEqual(scratch.select("call_end")[0]["known_usage"], {"input_tokens": inp, "output_tokens": out})

    async def test_missing_and_invalid_usage_are_rejected(self):
        for usage, code in ((None, "missing_usage"), ({"input_tokens": True, "output_tokens": 4}, "invalid_usage"),
                            ({"input_tokens": 10, "output_tokens": -1}, "invalid_usage")):
            with self.subTest(usage=usage):
                response = completed()
                response["usage"] = usage
                transport = SequenceTransport([{"input_tokens": 10}, response])
                adapter, scratch = self.adapter(transport)
                await self.assert_code(adapter, code)
                self.assertEqual(len(transport.requests), 2)
                self.assertTrue(scratch.select("call_end")[0]["generation_request_started"])
                self.assertFalse(scratch.select("call_end")[0]["generation_usage_received"])
                self.assertTrue(scratch.select("call_end")[0]["remote_usage_unknown"])
                self.assertIsNone(scratch.select("call_end")[0]["known_usage"])

    async def test_incomplete_text_is_never_returned_as_success(self):
        response = completed("partial answer")
        response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        transport = SequenceTransport([{"input_tokens": 10}, response])
        adapter, scratch = self.adapter(transport)
        error = await self.assert_code(adapter, "incomplete_response")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(scratch.select("http_response")[-1]["payload"]["output"][0]["content"][0]["text"], "partial answer")
        self.assertEqual(error.known_usage, {"input_tokens": 10, "output_tokens": 4})
        self.assertEqual(scratch.select("call_end")[0]["known_usage"], error.known_usage)
        self.assertFalse(error.remote_usage_unknown)

    async def test_timeout_stops_request_and_stage_circuits_are_independent(self):
        requests = []
        cancelled = asyncio.Event()
        timeouts = []
        original_timeout = asyncio.timeout

        def tracked_timeout(seconds):
            timeout = original_timeout(seconds)
            timeouts.append(timeout)
            return timeout

        async def transport(path, payload):
            requests.append(path)
            if len(requests) == 1:
                try:
                    # Expire the real asyncio deadline only after this request
                    # begins. A 10 ms wall-clock deadline could previously
                    # expire during local admission under load, which correctly
                    # issues no request and cannot cancel this transport.
                    timeouts[-1].reschedule(asyncio.get_running_loop().time())
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {"input_tokens": 10} if path.endswith("input_tokens") else completed()

        adapter, scratch = self.adapter(transport, seconds=5.0, threshold=1)
        with patch("indeces.adapter.asyncio.timeout", side_effect=tracked_timeout):
            await self.assert_code(adapter, "stage_timeout")
            self.assertTrue(cancelled.is_set())
            self.assertTrue(timeouts[0].expired())
            await self.assert_code(adapter, "circuit_open")
            self.assertEqual(requests, ["/responses/input_tokens"])
            self.assertEqual((await self.invoke(adapter, "summary")).text, "a visible reply")
        timeout = next(row for row in scratch.select("call_end") if row.get("code") == "stage_timeout")
        self.assertEqual(timeout["phase"], "input_count")
        self.assertTrue(timeout["remote_request_started"])
        self.assertFalse(timeout["generation_request_started"])
        self.assertFalse(timeout["remote_usage_unknown"])

    async def test_label_slot_timeout_issues_no_request_and_preserves_provider_circuit(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def transport(path, payload):
            requests.append(path)
            if path == "/responses":
                entered.set()
                await release.wait()
                return completed()
            return {"input_tokens": 10}

        adapter, scratch = self.adapter(transport, threshold=1)
        adapter.config = replace(adapter.config, budgets={**adapter.config.budgets,
                                                       "label": Budget(100, 20, 0.01)})
        first = asyncio.create_task(self.invoke(adapter, "reply"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await self.assert_code(adapter, "model_slot_timeout", stage="label")
            self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
            self.assertEqual(adapter._circuits["label"], {"failures": 0, "until": 0.0})
            end = next(row for row in scratch.select("call_end") if row["stage"] == "label")
            self.assertEqual(end["phase"], "slot_wait")
            self.assertGreaterEqual(end["slot_wait_seconds"], 0.01)
            self.assertFalse(end["remote_request_started"])
            self.assertFalse(end["generation_request_started"])
            self.assertFalse(end["remote_usage_unknown"])
        finally:
            release.set()
            await first

    async def test_generation_timeout_preserves_unknown_usage_and_provider_circuit(self):
        requests = []
        timeouts = []
        original_timeout = asyncio.timeout

        def tracked_timeout(seconds):
            timeout = original_timeout(seconds)
            timeouts.append(timeout)
            return timeout

        async def transport(path, payload):
            requests.append(path)
            if path.endswith("input_tokens"):
                return {"input_tokens": 10}
            # Exercise a real deadline cancellation only once generation has
            # issued. A 10ms initial deadline could instead expire during local
            # admission on a busy/coarse-clock Windows runner.
            timeouts[-1].reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()

        adapter, scratch = self.adapter(transport, seconds=5.0, threshold=1)
        with patch("indeces.adapter.asyncio.timeout", side_effect=tracked_timeout):
            await self.assert_code(adapter, "stage_timeout")
        self.assertTrue(timeouts[0].expired())
        end = scratch.select("call_end")[0]
        self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
        self.assertEqual(end["phase"], "generation")
        self.assertTrue(end["generation_request_started"])
        self.assertTrue(end["remote_request_in_flight"])
        self.assertFalse(end["generation_usage_received"])
        self.assertTrue(end["remote_usage_unknown"])
        self.assertEqual(adapter._circuits["reply"]["failures"], 1)
        self.assertGreater(adapter._circuits["reply"]["until"], 0)

    async def test_local_deadline_after_count_never_issues_generation_or_penalizes_provider(self):
        transport = SequenceTransport([{"input_tokens": 10}])
        adapter, scratch = self.adapter(transport, threshold=1)
        offset = 0.0
        original_write = scratch.write

        def write(event, **fields):
            nonlocal offset
            original_write(event, **fields)
            if event == "input_gate":
                # Simulate local work crossing the unchanged one-second limit;
                # keep the event loop's own monotonic clock untouched.
                offset = 2.0

        scratch.write = write
        with patch("indeces.adapter.time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)):
            with self.assertRaises(GovernedError) as caught:
                await self.invoke(adapter)
        self.assertEqual(caught.exception.code, "stage_timeout")
        self.assertFalse(caught.exception.remote_usage_unknown)
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens"])
        self.assertEqual(adapter._circuits["reply"], {"failures": 0, "until": 0.0})
        end = scratch.select("call_end")[0]
        self.assertEqual(end["phase"], "input_gate")
        self.assertTrue(end["remote_request_started"])
        self.assertFalse(end["remote_request_in_flight"])
        self.assertFalse(end["generation_request_started"])
        self.assertFalse(end["remote_usage_unknown"])

    async def test_local_start_audit_deadline_exhaustion_never_issues_input_count(self):
        transport = SequenceTransport([])
        adapter, scratch = self.adapter(transport, threshold=1)
        original_write = scratch.write
        offset = 0.0

        def write(event, **fields):
            nonlocal offset
            original_write(event, **fields)
            if event == "call_start":
                offset = 2.0

        scratch.write = write
        with patch("indeces.adapter.time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset)):
            error = await self.assert_code(adapter, "stage_timeout")
        self.assertEqual(transport.requests, [])
        self.assertEqual(adapter._circuits["reply"], {"failures": 0, "until": 0.0})
        self.assertFalse(error.remote_usage_unknown)
        self.assertIsNone(error.known_usage)
        end = scratch.select("call_end")[0]
        self.assertEqual(end["phase"], "slot_acquired")
        self.assertFalse(end["remote_request_started"])
        self.assertFalse(end["remote_usage_unknown"])

    async def test_cancellation_is_traced_and_never_retried(self):
        started = asyncio.Event()
        requests = []

        async def transport(path, payload):
            requests.append(path)
            started.set()
            await asyncio.Event().wait()

        adapter, scratch = self.adapter(transport)
        task = asyncio.create_task(self.invoke(adapter))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(requests, ["/responses/input_tokens"])
        self.assertEqual(scratch.select("call_end")[0]["status"], "cancelled")
        self.assertFalse(scratch.select("call_end")[0]["remote_usage_unknown"])

    async def test_cancellation_during_generation_preserves_unknown_generation_usage(self):
        entered = asyncio.Event()
        requests = []

        async def transport(path, payload):
            requests.append(path)
            if path.endswith("input_tokens"):
                return {"input_tokens": 10}
            entered.set()
            await asyncio.Event().wait()

        adapter, scratch = self.adapter(transport)
        task = asyncio.create_task(self.invoke(adapter))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        end = scratch.select("call_end")[0]
        self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
        self.assertEqual(end["phase"], "generation")
        self.assertTrue(end["generation_request_started"])
        self.assertFalse(end["generation_usage_received"])
        self.assertTrue(end["remote_usage_unknown"])
        self.assertTrue(caught.exception.remote_usage_unknown)

    async def test_cancellation_after_generation_response_preserves_confirmed_usage(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed(inp=17, out=6)])
        adapter, scratch = self.adapter(transport)
        original_write = scratch.write

        def write(event, **fields):
            original_write(event, **fields)
            if event == "http_response" and fields["path"] == "/responses":
                raise asyncio.CancelledError()

        scratch.write = write
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.invoke(adapter)
        known = {"input_tokens": 17, "output_tokens": 6}
        self.assertEqual(caught.exception.known_usage, known)
        self.assertFalse(caught.exception.remote_usage_unknown)
        end = scratch.select("call_end")[0]
        self.assertEqual(end["status"], "cancelled")
        self.assertEqual(end["known_usage"], known)
        self.assertFalse(end["remote_usage_unknown"])

    async def test_generation_http_rejection_without_usage_stays_unknown(self):
        class Content:
            async def iter_chunked(self, size):
                yield b'{"input_tokens": 10}'

        class Response:
            def __init__(self, status):
                self.status, self.content = status, Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Session:
            def __init__(self, status):
                self.status, self.requests = status, []

            def post(self, url, **options):
                self.requests.append(url)
                return Response(200 if url.endswith("input_tokens") else self.status)

        for status in (400, 401, 403, 408, 409, 429):
            with self.subTest(status=status):
                adapter, scratch = self.adapter(None)
                adapter._session = Session(status)
                error = await self.assert_code(adapter, "provider_http_" + str(status))
                self.assertTrue(error.remote_usage_unknown)
                self.assertIsNone(error.known_usage)
                end = scratch.select("call_end")[0]
                self.assertTrue(end["generation_rejected"])
                self.assertTrue(end["remote_usage_unknown"])
                self.assertIsNone(end["known_usage"])
                self.assertEqual(len(adapter._session.requests), 2)

    async def test_cancellation_while_waiting_for_slot_never_issues_label_request(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def transport(path, payload):
            requests.append(path)
            if path == "/responses":
                entered.set()
                await release.wait()
                return completed()
            return {"input_tokens": 10}

        adapter, scratch = self.adapter(transport)
        first = asyncio.create_task(self.invoke(adapter, "reply"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            second = asyncio.create_task(self.invoke(adapter, "label"))
            await asyncio.sleep(0)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await second
            self.assertFalse(caught.exception.remote_usage_unknown)
            self.assertEqual(requests, ["/responses/input_tokens", "/responses"])
            end = next(row for row in scratch.select("call_end") if row["stage"] == "label")
            self.assertEqual(end["phase"], "slot_wait")
            self.assertFalse(end["remote_request_started"])
            self.assertFalse(end["remote_usage_unknown"])
            self.assertEqual(adapter._circuits["label"], {"failures": 0, "until": 0.0})
        finally:
            release.set()
            await first

    async def test_calls_have_no_parallel_transport_or_interleaved_generation(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        requests = []
        active = maximum = 0

        async def transport(path, payload):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            requests.append(path)
            try:
                if len(requests) == 1:
                    entered.set()
                    await release.wait()
                await asyncio.sleep(0)
                return {"input_tokens": 10} if path.endswith("input_tokens") else completed()
            finally:
                active -= 1

        adapter, _ = self.adapter(transport)
        first = asyncio.create_task(self.invoke(adapter))
        await entered.wait()
        second = asyncio.create_task(self.invoke(adapter, "summary"))
        await asyncio.sleep(0)
        self.assertEqual(requests, ["/responses/input_tokens"])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(maximum, 1)
        self.assertEqual(requests, ["/responses/input_tokens", "/responses"] * 2)

    async def test_queued_same_stage_rechecks_newly_opened_circuit(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        requests = []

        async def transport(path, payload):
            requests.append(path)
            if len(requests) == 1:
                entered.set()
                await release.wait()
                return {"input_tokens": 10}
            if len(requests) == 2:
                raise GovernedError("provider_network_error")
            return {"input_tokens": 10} if path.endswith("input_tokens") else completed()

        adapter, _ = self.adapter(transport, threshold=1)
        first = asyncio.create_task(self.invoke(adapter))
        await entered.wait()
        second = asyncio.create_task(self.invoke(adapter))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], GovernedError)
        self.assertIsInstance(results[1], GovernedError)
        self.assertEqual(results[1].code, "circuit_open")
        self.assertEqual(len(requests), 2)

    async def test_malformed_output_is_governed_and_records_call_end(self):
        for output in (None, "text", [{"type": "message", "role": "assistant", "content": ["text"]}],
                       [{"type": "message", "role": "assistant", "content": None}]):
            with self.subTest(output=output):
                response = completed()
                response["output"] = output
                transport = SequenceTransport([{"input_tokens": 10}, response])
                adapter, scratch = self.adapter(transport)
                await self.assert_code(adapter, "invalid_output")
                self.assertEqual(scratch.select("call_end")[-1]["status"], "failed")
                self.assertEqual(len(transport.requests), 2)

    async def test_refusal_and_empty_output_are_rejected(self):
        for content, code in (([{"type": "refusal", "refusal": "no"}], "provider_refusal"),
                              ([{"type": "output_text", "text": " "}], "empty_output")):
            with self.subTest(code=code):
                response = completed()
                response["output"][0]["content"] = content
                adapter, _ = self.adapter(SequenceTransport([{"input_tokens": 10}, response]))
                await self.assert_code(adapter, code)


class ReservationTests(unittest.TestCase):
    def test_reservation_is_positive_and_accounts_for_utf8_and_schema(self):
        ascii_cost = reservation("instructions", [{"role": "user", "content": "abc"}])
        unicode_cost = reservation("instructions", [{"role": "user", "content": "中文文"}])
        self.assertGreater(unicode_cost, ascii_cost)
        self.assertGreater(reservation("instructions", [], {"type": "object"}), reservation("instructions", []))


class AdapterConfigTests(unittest.TestCase):
    def load_template(self, transform=lambda value: value):
        template = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.toml"
            path.write_text(transform(template), encoding="utf-8")
            return load_config(path).adapter

    def test_omitted_fields_default_to_medium_effort_and_high_verbosity(self):
        config = self.load_template(lambda template: "\n".join(
            line for line in template.splitlines() if not line.startswith(("reasoning =", "verbosity ="))))
        self.assertEqual(config.verbosity, "high")
        self.assertEqual({budget.reasoning for budget in config.budgets.values()}, {"medium"})

    def test_verbosity_rejects_invalid_values_when_constructed_or_loaded(self):
        config = self.load_template()
        for value in ("none", "HIGH", "", True, 1, 1.5, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "verbosity"):
                replace(config, verbosity=value)
        for literal in ('"none"', '"HIGH"', '""', "true", "1", "1.5", "[]", "{}"):
            with self.subTest(literal=literal), self.assertRaisesRegex(ValueError, "verbosity"):
                self.load_template(lambda value: value.replace('verbosity = "high"', "verbosity = " + literal))

    def test_effort_is_validated_and_explicit_old_settings_are_preserved(self):
        for value in ("minimal", "LOW", "", True, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "reasoning"):
                Budget(100, 20, 1, reasoning=value)
        config = self.load_template(lambda value: value.replace('reasoning = "medium"', 'reasoning = "low"'))
        self.assertEqual({budget.reasoning for budget in config.budgets.values()}, {"low"})

    def test_sol_rejects_unsupported_effort_and_old_model_without_silent_migration(self):
        with self.assertRaisesRegex(ValueError, "does not support.*none"):
            self.load_template(lambda value: value.replace('reasoning = "medium"', 'reasoning = "none"'))
        with self.assertRaisesRegex(ValueError, "does not support.*none"):
            replace(self.load_template(), budgets={"reply": Budget(100, 20, 1, reasoning="none")})
        with self.assertRaisesRegex(ValueError, "migrate the model explicitly"):
            self.load_template(lambda value: value.replace('model = "gpt-6.1-sol"', 'model = "gpt-6-luna"'))

    def test_valid_explicit_verbosity_loads_without_changing_budgets(self):
        baseline = self.load_template()
        for verbosity in ("low", "medium", "high"):
            with self.subTest(verbosity=verbosity):
                config = self.load_template(lambda value: value.replace('verbosity = "high"', f'verbosity = "{verbosity}"'))
                self.assertEqual(config.verbosity, verbosity)
                self.assertEqual(config.budgets, baseline.budgets)


if __name__ == "__main__":
    unittest.main()
