from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import deque
import unittest

from indeces.adapter import OpenAIAdapter, reservation
from indeces.config import AdapterConfig, Budget
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
    def adapter(self, transport, *, seconds=1.0, threshold=3, input_limit=100, output_limit=20):
        budget = Budget(input_limit, output_limit, seconds)
        config = AdapterConfig("gpt-6-luna", "https://api.openai.com/v1",
                               {stage: budget for stage in ("reply", "summary", "label")},
                               failure_threshold=threshold, cooldown_seconds=60.0)
        scratch = Scratch()
        return OpenAIAdapter(config, scratch, api_key="never-log-this-key", request=transport), scratch

    async def invoke(self, adapter, stage="reply", schema=None):
        return await adapter.call(stage, "instructions", [{"role": "user", "content": "source"}], "trace-1", schema)

    async def assert_code(self, adapter, code, *, stage="reply"):
        with self.assertRaises(GovernedError) as caught:
            await self.invoke(adapter, stage)
        self.assertEqual(caught.exception.code, code)

    async def test_count_precedes_generation_and_both_payloads_are_traced(self):
        transport = SequenceTransport([{"input_tokens": 10}, completed()])
        adapter, scratch = self.adapter(transport)
        schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
        result = await self.invoke(adapter, "summary", schema)
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens", "/responses"])
        count_payload, payload = [data for _, data in transport.requests]
        self.assertEqual(count_payload["text"], payload["text"])
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

    async def test_count_over_limit_prevents_generation_without_tripping_circuit(self):
        transport = SequenceTransport([{"input_tokens": 101}] * 3)
        adapter, scratch = self.adapter(transport, threshold=1)
        for _ in range(3):
            await self.assert_code(adapter, "input_token_limit")
        self.assertEqual([path for path, _ in transport.requests], ["/responses/input_tokens"] * 3)
        self.assertTrue(all(not row["admitted"] for row in scratch.select("input_gate")))
        self.assertEqual(adapter._circuits["reply"]["failures"], 0)

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

    async def test_missing_and_invalid_usage_are_rejected(self):
        for usage, code in ((None, "missing_usage"), ({"input_tokens": True, "output_tokens": 4}, "invalid_usage"),
                            ({"input_tokens": 10, "output_tokens": -1}, "invalid_usage")):
            with self.subTest(usage=usage):
                response = completed()
                response["usage"] = usage
                transport = SequenceTransport([{"input_tokens": 10}, response])
                adapter, _ = self.adapter(transport)
                await self.assert_code(adapter, code)
                self.assertEqual(len(transport.requests), 2)

    async def test_incomplete_text_is_never_returned_as_success(self):
        response = completed("partial answer")
        response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        transport = SequenceTransport([{"input_tokens": 10}, response])
        adapter, scratch = self.adapter(transport)
        await self.assert_code(adapter, "incomplete_response")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(scratch.select("http_response")[-1]["payload"]["output"][0]["content"][0]["text"], "partial answer")

    async def test_timeout_stops_request_and_stage_circuits_are_independent(self):
        requests = []
        cancelled = asyncio.Event()

        async def transport(path, payload):
            requests.append(path)
            if len(requests) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return {"input_tokens": 10} if path.endswith("input_tokens") else completed()

        adapter, scratch = self.adapter(transport, seconds=0.01, threshold=1)
        await self.assert_code(adapter, "stage_timeout")
        self.assertTrue(cancelled.is_set())
        await self.assert_code(adapter, "circuit_open")
        self.assertEqual(requests, ["/responses/input_tokens"])
        self.assertEqual((await self.invoke(adapter, "summary")).text, "a visible reply")
        timeout = next(row for row in scratch.select("call_end") if row.get("code") == "stage_timeout")
        self.assertTrue(timeout["remote_usage_unknown"])

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


if __name__ == "__main__":
    unittest.main()
