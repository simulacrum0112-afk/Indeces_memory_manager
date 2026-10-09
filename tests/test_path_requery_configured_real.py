"""Configured-real path entry with synthetic, network-blocked responses.

The ledger follows its non-mock admission and tariff branch. No real provider,
credential, application configuration, corpus or production process is used.
The HTTP case substitutes aiohttp's session at the existing transport boundary.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
import time
import unittest
from unittest.mock import patch

from indeces.active_requery import TurnBudgetAdapter
from indeces.adapter import OpenAIAdapter
from indeces.console import create_runtime
from indeces.contracts import GovernedError
from indeces.requery_records import verify_active_turn
from indeces.run_records import verify_runs
from tests import test_active_path_requery_c9 as native


class ConfiguredRealPathEntryTests(unittest.IsolatedAsyncioTestCase):
    def prepare_fixture(self, *, enabled=True, limits=None, consume_feedback=True):
        helper = native.ActivePathRequeryC9Tests("runTest")
        fixture = helper.make_fixture(enabled=False, consume_feedback=consume_feedback)
        self.addAsyncCleanup(fixture.asyncTearDown)
        path_config = fixture.runtime.path_requery_loop_config
        fixture.adapter.offline_mock = False
        changes = dict(path_requery_loop_enabled=enabled,
                       active_requery_allow_real_calls=True,
                       active_requery_cost_usd=0.1,
                       active_requery_input_usd_per_million=1,
                       active_requery_output_usd_per_million=2)
        changes.update(limits or {})
        fixture.config.runtime = replace(fixture.config.runtime, **changes)
        fixture.path_config = path_config
        fixture.native_helper = helper
        return fixture

    def activate(self, fixture):
        fixture.runtime = create_runtime(fixture.config, fixture.store,
                                         fixture.adapter, fixture.scratch)
        fixture.runtime.path_requery_loop_config = fixture.path_config

    async def run_fixture(self, fixture):
        # The native helper additionally checks that memory_static is identical
        # before and after the whole turn. Admission failures during initial
        # model selection produce a preserved failed turn without a frozen
        # initial bundle; they cannot be claimed as a complete evidence record.
        calls = await fixture.native_helper.run_captured(fixture, verify=False)
        fixture.verification = verify_runs([fixture.entries()[0]])
        self.assertEqual(fixture.verification["issues"], [])
        if fixture.verification["counts"]["complete"] != 1:
            end = fixture.event("turn_end")[0]
            self.assertEqual(end["status"], "failed")
            self.assertIn(end["code"], {"requery_cost_budget", "input_token_limit"})
        return calls

    def assert_real_ledger(self, fixture, *, complete=True):
        ledger = fixture.saved_ledger()
        self.assertEqual(ledger["mode"], "configured_real")
        self.assertEqual(ledger["cost_basis"], "configured_upper_unit_prices_not_invoice")
        self.assertEqual(ledger["automatic_retries"], 0)
        self.assertEqual(ledger["usage_complete"], complete)
        price_in, price_out = Decimal("0.000001"), Decimal("0.000002")
        expected = sum((price_in * row["usage"]["input_tokens"]
                        + price_out * row["usage"]["output_tokens"]
                        for row in ledger["calls"] if row["usage"] is not None), Decimal("0"))
        self.assertEqual(Decimal(ledger["estimated_cost_usd"]), expected)
        for row in ledger["calls"]:
            self.assertEqual(Decimal(row["reserved_cost_usd"]),
                price_in * row["reserved_input_tokens"]
                + price_out * row["reserved_output_tokens"])
        start = fixture.event("turn_start")[0]
        if fixture.config.runtime.path_requery_loop_enabled:
            self.assertIs(start["path_requery_loop_offline_mock"], False)
            self.assertEqual(start["path_requery_loop_provider_mode"], "configured_real")
            self.assertEqual(start["path_requery_loop_real_call_policy"], {
                "allow_real_calls": True, "cost_usd": fixture.config.runtime.active_requery_cost_usd,
                "input_usd_per_million": 1, "output_usd_per_million": 2})
        return ledger

    def assert_cannot_reopen(self, fixture):
        path = next((fixture.config.state_dir / "active_requery_ledger").glob("*.json"))
        previous = path.read_bytes()
        requests = len(fixture.requests)
        with self.assertRaisesRegex(GovernedError, "active_requery_run_already_recorded"):
            TurnBudgetAdapter(fixture.adapter, fixture.config, fixture.scratch,
                              "synthetic-reopen", fixture.message.message_id, fixture.message.scope)
        self.assertEqual(len(fixture.requests), requests)
        self.assertEqual(path.read_bytes(), previous)

    def assert_accumulation(self, fixture):
        fixture.native_helper.assert_strict_two_round_sources(fixture)
        self.assertEqual(len(fixture.event("path_requery_feedback_result")), 1)

    async def test_authorized_non_mock_entry_runs_native_loop_and_exact_positive_cost_ledger(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        await self.run_fixture(fixture)
        self.assert_accumulation(fixture)
        fixture.native_helper.assert_final_sources_and_ledger(fixture)
        self.assertTrue(fixture.feedback_was_consumed)
        ledger = self.assert_real_ledger(fixture)
        self.assertEqual(ledger["phase"], "completed")
        self.assertEqual(Decimal(ledger["estimated_cost_usd"]), Decimal("0.000512"))
        self.assertEqual(fixture.native_helper.generation_stages(fixture),
                         ["selection", "query", "query", "reply"])
        self.assertIsNone(fixture.adapter._session)
        self.assert_cannot_reopen(fixture)

    async def test_off_non_mock_entry_keeps_existing_payload_and_policy_absence(self):
        fixture = self.prepare_fixture(enabled=False, consume_feedback=False)
        self.activate(fixture)
        await self.run_fixture(fixture)
        self.assertEqual(fixture.event("path_requery_feedback"), [])
        self.assertEqual(fixture.event("path_requery_feedback_result"), [])
        start = fixture.event("turn_start")[0]
        self.assertTrue(all(not key.startswith("path_requery_loop_") for key in start))
        self.assertTrue(all("path_feedback" not in row and "path_feedback_sha256" not in row
                            for row in fixture.planning_inputs))
        self.assertTrue(all("effective_terms" not in row and "actual_query_sha256" not in row
                            for row in fixture.event("requery_retrieval")))
        self.assertEqual(fixture.native_helper.generation_stages(fixture), ["selection", "query", "reply"])
        self.assert_real_ledger(fixture)

    async def test_non_mock_requires_allowance_and_positive_cost_and_prices_before_any_transport(self):
        cases = ({"active_requery_allow_real_calls": False},
                 {"active_requery_cost_usd": 0},
                 {"active_requery_input_usd_per_million": 0},
                 {"active_requery_output_usd_per_million": 0})
        for changes in cases:
            with self.subTest(changes=changes):
                fixture = self.prepare_fixture(limits=changes)
                with self.assertRaisesRegex(GovernedError, "active_requery_real_calls_not_authorized"):
                    self.activate(fixture)
                self.assertEqual(fixture.requests, [])
                with self.assertRaisesRegex(GovernedError, "active_requery_real_calls_not_authorized"):
                    TurnBudgetAdapter(fixture.adapter, fixture.config, fixture.scratch,
                        "synthetic-gate", fixture.message.message_id, fixture.message.scope)
                self.assertEqual(fixture.requests, [])
                self.assertFalse((fixture.config.state_dir / "active_requery_ledger").exists())

    async def test_positive_but_insufficient_cost_stops_before_input_count_or_generation(self):
        fixture = self.prepare_fixture(limits={"active_requery_cost_usd": 0.000001})
        self.activate(fixture)
        await self.run_fixture(fixture)
        self.assertEqual(fixture.requests, [])
        self.assertEqual(fixture.event("turn_end")[0]["status"], "failed")
        self.assertEqual(fixture.event("turn_end")[0]["code"], "requery_cost_budget")
        ledger = self.assert_real_ledger(fixture)
        self.assertEqual(ledger["calls"], [])
        self.assertEqual(ledger["unknown_generation_count"], 0)
        self.assert_cannot_reopen(fixture)

    async def test_accumulated_receipt_cannot_bypass_late_cost_call_or_token_allowance(self):
        cases = (({"active_requery_cost_usd": 0.0053}, "requery_cost_budget"),
                 ({"active_requery_max_calls": 3}, "requery_call_budget"),
                 ({"active_requery_input_tokens": 640}, "requery_token_budget"))
        for limits, reason in cases:
            with self.subTest(reason=reason):
                fixture = self.prepare_fixture(limits=limits)
                self.activate(fixture)
                await self.run_fixture(fixture)
                self.assert_accumulation(fixture)
                self.assertEqual(fixture.native_helper.generation_stages(fixture), ["selection", "query"])
                self.assertEqual(fixture.event("requery_stop")[0]["reason"], reason)
                ledger = self.assert_real_ledger(fixture)
                self.assertEqual(Decimal(ledger["estimated_cost_usd"]), Decimal("0.000256"))
                self.assertEqual(ledger["unknown_generation_count"], 0)
                self.assert_cannot_reopen(fixture)

    async def test_accumulated_receipt_cannot_reopen_an_expired_registered_deadline(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        original_write = fixture.scratch.write
        post_seen = False

        def write(event, **fields):
            nonlocal post_seen
            result = original_write(event, **fields)
            if event == "path_requery_feedback_result":
                post_seen = True
            return result

        original_check = TurnBudgetAdapter.check

        def expire_after_post(adapter):
            nonlocal post_seen
            if post_seen:
                # Simulate elapsed global time at this exact boundary. No
                # configured limit is enlarged and no external clock changes.
                elapsed = min(adapter.limits.active_requery_seconds, adapter.limits.turn_seconds) + 1
                adapter.started = time.monotonic() - elapsed
                adapter.deadline = time.monotonic() - 1
                post_seen = False
            return original_check(adapter)

        fixture.scratch.write = write
        with patch.object(TurnBudgetAdapter, "check", expire_after_post):
            await self.run_fixture(fixture)
        self.assert_accumulation(fixture)
        self.assertEqual(fixture.native_helper.generation_stages(fixture), ["selection", "query"])
        self.assertEqual(fixture.event("requery_stop")[0]["reason"], "requery_time_budget")
        ledger = self.assert_real_ledger(fixture)
        self.assertEqual(ledger["unknown_generation_count"], 0)
        self.assertGreater(ledger["elapsed_seconds"], ledger["limits"]["seconds"])
        self.assert_cannot_reopen(fixture)

    async def test_input_count_over_limit_never_issues_generation_or_marks_unknown_usage(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        previous = fixture.adapter._request_override

        async def count_over_limit(path, payload):
            result = await previous(path, payload)
            return {"input_tokens": 5000} if path == "/responses/input_tokens" else result

        fixture.adapter._request_override = count_over_limit
        await self.run_fixture(fixture)
        self.assertEqual([path for path, _, _ in fixture.requests], ["/responses/input_tokens"])
        self.assertEqual(fixture.native_helper.generation_stages(fixture), [])
        ledger = self.assert_real_ledger(fixture)
        self.assertEqual(ledger["unknown_generation_count"], 0)
        self.assertFalse(ledger["calls"][0]["generation_started"])
        self.assertIsNone(ledger["calls"][0]["usage"])
        self.assertEqual(Decimal(ledger["unresolved_reserved_cost_usd"]), 0)
        self.assert_cannot_reopen(fixture)

    async def test_second_planner_unknown_usage_preserves_positive_reservation_and_stops_persistently(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        previous = fixture.adapter._request_override

        async def fail_second_planner(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            if path == "/responses" and stage == "query" and fixture.query_index >= 1:
                fixture.query_error = GovernedError("synthetic_network_loss_after_accumulation")
            return await previous(path, payload)

        fixture.adapter._request_override = fail_second_planner
        await self.run_fixture(fixture)
        self.assert_accumulation(fixture)
        self.assertTrue(fixture.feedback_was_consumed)
        self.assertEqual(fixture.native_helper.generation_stages(fixture), ["selection", "query", "query"])
        ledger = self.assert_real_ledger(fixture, complete=False)
        self.assertEqual((ledger["phase"], ledger["halted"], ledger["unknown_generation_count"]),
                         ("halted", "requery_usage_unknown", 1))
        self.assertIsNone(ledger["calls"][-1]["usage"])
        self.assertEqual(Decimal(ledger["unresolved_reserved_cost_usd"]), Decimal("0.005120"))
        self.assertEqual(Decimal(ledger["estimated_cost_usd"]), Decimal("0.000256"))
        self.assert_cannot_reopen(fixture)

    async def test_known_usage_over_global_budget_is_saved_then_stops_without_retry_or_reply(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        previous = fixture.adapter._request_override

        async def overrun_second_planner(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            overrun = path == "/responses" and stage == "query" and fixture.query_index >= 1
            result = await previous(path, payload)
            if overrun:
                result["usage"] = {"input_tokens": 40000, "output_tokens": 32}
            return result

        fixture.adapter._request_override = overrun_second_planner
        await self.run_fixture(fixture)
        self.assert_accumulation(fixture)
        self.assertEqual(fixture.native_helper.generation_stages(fixture), ["selection", "query", "query"])
        ledger = self.assert_real_ledger(fixture)
        self.assertEqual(ledger["halted"], "requery_provider_budget_breach")
        self.assertEqual(ledger["phase"], "halted")
        self.assertEqual(ledger["calls"][-1]["usage"], {"input_tokens": 40000, "output_tokens": 32})
        self.assertEqual(ledger["unknown_generation_count"], 0)
        self.assertGreater(ledger["input_tokens"], ledger["limits"]["input_tokens"])
        self.assert_cannot_reopen(fixture)

    async def test_real_mode_tariff_cost_and_reservation_forgery_are_rejected(self):
        fixture = self.prepare_fixture()
        self.activate(fixture)
        await self.run_fixture(fixture)
        trace_id = fixture.event("turn_start")[0]["trace_id"]
        trace = [(row["event"], deepcopy(row["fields"])) for row in fixture.entries()[1]
                 if row["fields"].get("trace_id") == trace_id]
        for kind in ("mock-flag", "provider-mode", "tariff", "cost", "cost-basis", "reservation"):
            with self.subTest(kind=kind):
                damaged = deepcopy(trace)
                start = next(fields for event, fields in damaged if event == "turn_start")
                if kind == "mock-flag":
                    start["path_requery_loop_offline_mock"] = True
                elif kind == "provider-mode":
                    start["path_requery_loop_provider_mode"] = "mock"
                elif kind == "tariff":
                    start["path_requery_loop_real_call_policy"]["input_usd_per_million"] = 2
                else:
                    for event, fields in damaged:
                        if event in {"requery_budget_event", "requery_stop"}:
                            ledger = fields["ledger"]
                            if kind == "cost":
                                ledger["estimated_cost_usd"] = "0"
                            elif kind == "cost-basis":
                                ledger["cost_basis"] = "synthetic_zero"
                            elif ledger["calls"]:
                                ledger["calls"][0]["reserved_cost_usd"] = "0"
                with self.assertRaises(ValueError):
                    verify_active_turn(damaged, start)

    async def test_existing_http_branch_without_request_override_uses_fake_session_and_real_ledger(self):
        fixture = self.prepare_fixture()
        local_response_transport = fixture.adapter._request_override
        fixture.adapter = OpenAIAdapter(fixture.config.adapter, fixture.scratch,
                                         api_key="synthetic-placeholder")
        fixture.adapter.offline_mock = False
        self.activate(fixture)
        http_requests = []

        class Content:
            def __init__(self, response):
                self.response = response

            async def iter_chunked(self, size):
                self.response.assert_headers_seen()
                wire = json.dumps(self.response.data).encode("utf-8")
                middle = len(wire) // 2
                yield wire[:middle]
                yield wire[middle:]

        class Response:
            def __init__(self, path, payload, provider_id):
                self.path, self.payload = path, payload
                self.status = 200
                self.headers = {"x-request-id": provider_id}
                self.content = Content(self)

            async def __aenter__(self):
                self.data = await local_response_transport(self.path, self.payload)
                return self

            async def __aexit__(self, *args):
                return None

            def assert_headers_seen(self):
                events = fixture.event("requery_budget_event")
                assert events[-1]["budget_event"] == "response_headers"

        class Session:
            def __init__(self, **kwargs):
                self.closed = False

            def post(self, url, *, json, headers, allow_redirects):
                assert not allow_redirects
                assert headers["Authorization"] == "Bearer synthetic-placeholder"
                assert url.startswith(fixture.config.adapter.base_url.rstrip("/"))
                path = "/responses/input_tokens" if url.endswith("/responses/input_tokens") else "/responses"
                assert fixture.saved_ledger()["calls"][-1]["transport_intents"][-1] == path
                http_requests.append({"path": path, "payload": deepcopy(json),
                                      "client_request_id": headers["X-Client-Request-Id"]})
                return Response(path, json, f"synthetic-http-{len(http_requests)}")

            async def close(self):
                self.closed = True

        sessions = []

        def factory(**kwargs):
            session = Session(**kwargs)
            sessions.append(session)
            return session

        async def run_http_turn():
            await fixture.runtime.process(fixture.message, fixture.deliver)

        fixture.run_turn = run_http_turn
        # Socket guards installed by the synthetic fixture remain active.
        # Only the session factory is substituted; _post itself is unmodified.
        with patch("aiohttp.ClientSession", side_effect=factory):
            await self.run_fixture(fixture)
        self.assertIsNone(fixture.adapter._request_override)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(http_requests), 8)
        self.assertEqual(len({row["client_request_id"] for row in http_requests}), 8)
        self.assertTrue(all(row["payload"]["model"] == "gpt-6.1-sol" for row in http_requests))
        self.assertTrue(all(row["payload"]["reasoning"] == {"effort": "medium"}
                            for row in http_requests))
        generation = [row["payload"] for row in http_requests if row["path"] == "/responses"]
        self.assertTrue(all(row["text"]["verbosity"] == "high" and row["tools"] == []
                            and row["store"] is False and row["stream"] is False
                            and row["truncation"] == "disabled" for row in generation))
        self.assert_accumulation(fixture)
        fixture.native_helper.assert_final_sources_and_ledger(fixture)
        self.assertTrue(fixture.feedback_was_consumed)
        self.assert_real_ledger(fixture)
        self.assertNotIn("synthetic-placeholder", json.dumps(fixture.entries()[1]))
        self.assertEqual(verify_runs([fixture.entries()[0]])["issues"], [])
        await fixture.adapter.close()
        self.assertTrue(sessions[0].closed)


if __name__ == "__main__":
    unittest.main()
