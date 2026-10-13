"""Production verifier port: accepted main receipt fixtures plus selector seams.

The ordinary fixture explicitly disables production model selection. Additional
production tests use existing synthetic Richard data and the actual configured
Runtime/selector/provider audit paths. No keys, network or private data.
"""
from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.claude_adapter import ClaudeAdapter
from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, RuntimeConfig
from indeces.contracts import DeliveryReceipt, FailureNotice, IncomingMessage
from indeces.prompts import BOT_REPLY_SCHEMA, LABEL_SCHEMA, summary_schema
from indeces.run_records import (_native_usage, _native_wire_schema, _validate_bot_decision,
                                 _validate_call, _validate_partial_call, _validate_reply_request,
                                 _validate_model_selection, validate_retrieval, validate_graph_audit, verify_runs)
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store
from indeces.console import create_runtime
from tests import test_richard_chat_runtime as richard_fixture


META = {"provider": "anthropic", "protocol": "messages", "base_url": "https://api.anthropic.com/v1",
        "api_version": "2023-06-01"}
COUNT, GENERATE = "/messages/count_tokens", "/messages"
MODEL = "claude-opus-5-5"


def native_events(*, schema=None, text="Visible reply.", model=MODEL):
    usage = {"input_tokens": 7, "cache_read_input_tokens": 11, "cache_creation_input_tokens": 5,
             "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 3},
             "output_tokens": 6, "output_tokens_details": {"thinking_tokens": 2}}
    normalized = {"input_tokens": 23, "output_tokens": 6}
    options = {"effort": "high"}
    if schema is not None:
        options["format"] = {"type": "json_schema", "schema": _native_wire_schema(schema)}
    common = {"model": model, "system": "Fixture instruction.", "messages": [{"role": "user", "content": "Fixture input."}],
              "output_config": options, "tools": [], "tool_choice": {"type": "none"}}
    result = {"text": text, **normalized, "response_id": "msg_fixture", "elapsed_seconds": 0.01}
    response = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": model,
                "stop_reason": "end_turn", "stop_sequence": None, "usage": usage,
                "content": [{"type": "thinking", "thinking": "Synthetic private reasoning.", "signature": "fixture-signature"},
                            {"type": "text", "text": text}]}
    identity = {"trace_id": "trace_fixture", "call_id": "call_fixture"}
    start = {**identity, **META, "stage": "reply", "model": model, "effort": "high", "response_schema": deepcopy(schema),
             "budget": {"input_tokens": 64, "output_tokens": 16, "seconds": 1, "reasoning": "high"},
             "requested_input_limit": None, "effective_input_limit": 64, "usage_receipt_version": 1}
    end = {**identity, **META, "stage": "reply", "status": "completed", "result": result,
           "known_usage": normalized, "native_usage": usage, "response_id": "msg_fixture", "generation_request_started": True,
           "generation_usage_received": True, "remote_usage_unknown": False}
    def transport(event, path, payload):
        return event, {**identity, **META, "path": path, "phase": "input_count" if path == COUNT else "generation",
                       "client_request_id": "count_request" if path == COUNT else "generation_request", "payload": payload}
    return [(event, deepcopy(value)) for event, value in [
        ("call_start", start), transport("http_request", COUNT, common),
        transport("http_response", COUNT, {"input_tokens": 23}),
        ("input_gate", {**identity, "stage": "reply", "input_tokens": 23, "limit": 64, "stage_limit": 64, "admitted": True}),
        transport("http_request", GENERATE, {**common, "max_tokens": 16, "stream": False}),
        transport("http_response", GENERATE, response), ("call_end", end),
    ]]


def fields(events, event, path=None):
    return next(value for kind, value in events if kind == event and (path is None or value.get("path") == path))


class ClaudeReceiptTests(unittest.TestCase):
    def assert_invalid(self, events):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            _validate_call(events)

    def test_native_complete_cache_normalization_and_signed_thinking(self):
        self.assertEqual(_validate_call(native_events()), "complete")

    def test_native_fable_uses_same_strict_protocol(self):
        self.assertEqual(_validate_call(native_events(model="claude-fable-5-1")), "complete")

    def test_optional_native_cache_counters_omitted_mean_zero(self):
        self.assertEqual(_native_usage({"input_tokens": 3, "output_tokens": 2}), {"input_tokens": 3, "output_tokens": 2})

    def test_optional_nullable_native_metadata_preserves_known_base_usage(self):
        events = native_events()
        for event, value in events:
            if event == "http_response" and value["path"] == GENERATE:
                value["payload"]["usage"].update(server_tool_use=None, output_tokens_details=None)
        fields(events, "call_end")["native_usage"].update(server_tool_use=None, output_tokens_details=None)
        self.assertEqual(_validate_call(events), "complete")

    def test_native_cache_counter_bool_is_unknown(self):
        self.assertIsNone(_native_usage({"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": True}))

    def test_cache_breakdown_must_match_aggregate_without_double_count(self):
        self.assertEqual(_native_usage(fields(native_events(), "call_end")["native_usage"])["input_tokens"], 23)
        usage = deepcopy(fields(native_events(), "call_end")["native_usage"])
        usage["cache_creation"]["ephemeral_5m_input_tokens"] += 1
        self.assertIsNone(_native_usage(usage))

    def test_thinking_usage_cannot_exceed_total_output(self):
        self.assertIsNone(_native_usage({"input_tokens": 3, "output_tokens": 2, "output_tokens_details": {"thinking_tokens": 3}}))

    def test_unknown_native_usage_remains_unknown_failed_receipt(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["usage"] = {"input_tokens": 7}
        end = fields(events, "call_end")
        end.update(status="failed", known_usage=None, native_usage=None, generation_usage_received=False, remote_usage_unknown=True)
        end.pop("result")
        self.assertEqual(_validate_call(events), "failed")
        self.assertIsNone(end["known_usage"])

    def test_unknown_usage_cannot_be_fabricated_as_zero(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"].pop("usage")
        fields(events, "call_end").update(known_usage={"input_tokens": 0, "output_tokens": 0}, native_usage=None)
        self.assert_invalid(events)

    def test_unknown_response_cannot_be_relabelled_unissued_known_zero(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"].pop("usage")
        end = fields(events, "call_end")
        end.update(status="failed", known_usage=None, native_usage=None, generation_usage_received=False,
                   generation_request_started=False, remote_usage_unknown=False)
        end.pop("result")
        self.assert_invalid(events)

    def test_cancelled_generation_without_response_preserves_unknown(self):
        events = [pair for pair in native_events() if not (pair[0] == "http_response" and pair[1].get("path") == GENERATE)]
        end = fields(events, "call_end")
        end.update(status="cancelled", known_usage=None, native_usage=None, response_id=None, generation_usage_received=False, remote_usage_unknown=True)
        end.pop("result")
        self.assertEqual(_validate_call(events), "failed")

    def test_failed_known_usage_requires_original_cache_metadata(self):
        events = native_events()
        fields(events, "call_end")["status"] = "failed"
        self.assertEqual(_validate_call(events), "failed")
        fields(events, "call_end")["native_usage"]["cache_read_input_tokens"] += 1
        self.assert_invalid(events)

    def test_raw_usage_cache_tampering_is_detected(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["usage"]["cache_read_input_tokens"] += 1
        self.assert_invalid(events)

    def test_count_and_generation_must_share_exact_context(self):
        events = native_events()
        fields(events, "http_request", GENERATE)["payload"]["messages"][0]["content"] += " changed"
        self.assert_invalid(events)

    def test_count_receipt_must_bind_input_gate(self):
        events = native_events()
        fields(events, "http_response", COUNT)["payload"]["input_tokens"] += 1
        self.assert_invalid(events)

    def test_effective_cap_cannot_be_renewed(self):
        events = native_events()
        fields(events, "call_start").update(requested_input_limit=20, effective_input_limit=64)
        self.assert_invalid(events)

    def test_normalized_input_counts_all_cache_tokens_against_cap(self):
        events = native_events()
        start = fields(events, "call_start")
        start.update(requested_input_limit=22, effective_input_limit=22)
        fields(events, "input_gate").update(input_tokens=22, limit=22)
        fields(events, "http_response", COUNT)["payload"]["input_tokens"] = 22
        self.assert_invalid(events)

    def test_output_token_budget_is_bound_to_native_max_tokens(self):
        events = native_events()
        fields(events, "http_request", GENERATE)["payload"]["max_tokens"] += 1
        self.assert_invalid(events)

    def test_explicit_high_cannot_be_downgraded_or_replaced_by_reasoning_effort(self):
        for change in ("low", "compatibility"):
            with self.subTest(change=change):
                events = native_events()
                for event, value in events:
                    if event == "http_request":
                        if change == "low":
                            value["payload"]["output_config"]["effort"] = "low"
                        else:
                            value["payload"]["reasoning_effort"] = "high"
                self.assert_invalid(events)

    def test_provider_and_paths_cannot_be_mixed(self):
        events = native_events()
        fields(events, "http_request", GENERATE)["path"] = "/responses"
        self.assert_invalid(events)
        events = native_events()
        fields(events, "call_end")["provider"] = "openai"
        self.assert_invalid(events)

    def test_provider_not_inferred_from_native_path(self):
        events = native_events()
        for _, value in events:
            for key in META:
                value.pop(key, None)
        self.assert_invalid(events)

    def test_native_endpoint_api_version_and_model_are_pinned(self):
        for key, value in (("base_url", "https://example.invalid/v1"), ("api_version", "future"), ("model", "unknown-model")):
            with self.subTest(key=key):
                events = native_events()
                fields(events, "call_start")[key] = value
                self.assert_invalid(events)

    def test_http_client_identifier_is_bound_between_request_and_response(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["client_request_id"] = "different-request"
        self.assert_invalid(events)

    def test_native_call_trace_identity_cannot_change_at_transport(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["trace_id"] = "other_trace"
        self.assert_invalid(events)

    def test_tool_and_prefill_bypasses_are_rejected(self):
        for key in ("tools", "tool_choice", "prefill"):
            with self.subTest(key=key):
                events = native_events()
                for event, value in events:
                    if event == "http_request":
                        if key == "prefill":
                            value["payload"]["messages"][-1]["role"] = "assistant"
                        else:
                            value["payload"][key] = [{"name": "bypass"}] if key == "tools" else {"type": "auto"}
                self.assert_invalid(events)

    def test_nonzero_server_tool_usage_cannot_complete(self):
        events = native_events()
        usage = fields(events, "http_response", GENERATE)["payload"]["usage"]
        usage["server_tool_use"] = {"web_search_requests": 1}
        fields(events, "call_end")["native_usage"] = deepcopy(usage)
        self.assert_invalid(events)

    def test_incomplete_and_refused_output_cannot_complete(self):
        for reason in ("max_tokens", "tool_use", "refusal", "pause_turn"):
            with self.subTest(reason=reason):
                events = native_events()
                fields(events, "http_response", GENERATE)["payload"]["stop_reason"] = reason
                self.assert_invalid(events)

    def test_tool_use_content_is_rejected_even_with_end_turn(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["content"].append({"type": "tool_use", "id": "tool", "name": "view", "input": {}})
        self.assert_invalid(events)

    def test_missing_thinking_signature_cannot_be_hidden_by_text_output(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["content"][0].pop("signature")
        self.assert_invalid(events)

    def test_redacted_thinking_preserved_but_only_text_enters_result(self):
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["content"][0] = {"type": "redacted_thinking", "data": "opaque-data"}
        self.assertEqual(_validate_call(events), "complete")
        fields(events, "call_end")["result"]["text"] = "opaque-dataVisible reply."
        self.assert_invalid(events)

    def test_response_model_and_identifier_bind_model_result(self):
        for key, value in (("model", "claude-fable-5-1"), ("id", "different-response")):
            with self.subTest(key=key):
                events = native_events()
                fields(events, "http_response", GENERATE)["payload"][key] = value
                self.assert_invalid(events)

    def test_local_original_label_constraints_survive_wire_lowering(self):
        self.assertEqual(_validate_call(native_events(schema=LABEL_SCHEMA, text='{"marks":["alpha"]}')), "complete")
        self.assert_invalid(native_events(schema=LABEL_SCHEMA, text=json.dumps({"marks": ["x" * 41]})))

    def test_local_original_summary_limit_and_duplicate_keys_are_checked(self):
        schema = summary_schema(16)
        self.assertEqual(_validate_call(native_events(schema=schema, text='{"summary":"abc"}')), "complete")
        self.assert_invalid(native_events(schema=schema, text='{"summary":"too long"}'))
        self.assert_invalid(native_events(schema=schema, text='{"summary":"a","summary":"b"}'))

    def test_native_schema_cannot_drop_frozen_original_constraint(self):
        events = native_events(schema=LABEL_SCHEMA, text='{"marks":["alpha"]}')
        for event, value in events:
            if event == "http_request":
                value["payload"]["output_config"]["format"]["schema"]["properties"]["marks"].pop("description")
        self.assert_invalid(events)

    def test_native_bot_context_and_schema_are_bound(self):
        events = native_events(schema=BOT_REPLY_SCHEMA, text='{"action":"reply","text":"Visible reply."}')
        request = fields(events, "http_request", GENERATE)["payload"]
        context = {"instructions": request["system"], "messages": request["messages"], "response_schema": BOT_REPLY_SCHEMA}
        decision = {"action": "reply", "text": "Visible reply.", "model_result": fields(events, "call_end")["result"]}
        self.assertEqual(_validate_bot_decision(decision, events, context=context)["action"], "reply")
        context["instructions"] += " changed"
        with self.assertRaises(ValueError):
            _validate_bot_decision(decision, events, context=context)

    def test_retained_native_call_checks_surviving_usage_and_output(self):
        events = [pair for pair in native_events() if pair[0] != "call_start"]
        self.assertEqual(_validate_partial_call(events), "retention_partial")
        fields(events, "http_response", GENERATE)["payload"]["usage"]["cache_read_input_tokens"] += 1
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_retained_native_missing_transport_does_not_invent_receipt(self):
        events = [pair for pair in native_events() if pair[0] in {"input_gate", "call_end"}]
        self.assertEqual(_validate_partial_call(events), "retention_partial")
        fields(events, "call_end")["native_usage"]["input_tokens"] += 1
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_retained_native_provenance_cannot_be_omitted(self):
        events = [pair for pair in native_events() if pair[0] != "call_start"]
        fields(events, "http_response", GENERATE).pop("protocol")
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_retained_native_surviving_request_response_ids_must_match(self):
        events = [pair for pair in native_events() if pair[0] != "call_start"]
        fields(events, "http_response", GENERATE)["client_request_id"] = "other-request"
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_retained_terminal_cannot_contradict_result_usage(self):
        events = [pair for pair in native_events() if pair[0] == "call_end"]
        fields(events, "call_end")["result"]["input_tokens"] = 0
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_retained_completed_terminal_cannot_claim_unknown_usage(self):
        events = [pair for pair in native_events() if pair[0] == "call_end"]
        fields(events, "call_end").update(known_usage=None, native_usage=None, generation_usage_received=False, remote_usage_unknown=True)
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_terminal_response_identifier_cannot_contradict_model_result(self):
        events = native_events()
        fields(events, "call_end")["response_id"] = "other_response"
        self.assert_invalid(events)
        events = [pair for pair in native_events() if pair[0] == "call_end"]
        fields(events, "call_end")["response_id"] = "other_response"
        with self.assertRaises(ValueError):
            _validate_partial_call(events)

    def test_native_summary_diagnostic_receipt_uses_native_protocol(self):
        events = native_events(schema=summary_schema(16), text='{"summary":"abc"}')
        for _, value in events:
            if "stage" in value:
                value["stage"] = "summary"
        with TemporaryDirectory() as directory:
            scratch = ScratchLog(Path(directory))
            try:
                scratch.write("diagnostic_summary_trial_start", trace_id="trace_fixture", operator_approved=True,
                              automatic_retries=0, checkpoint_update=False)
                for event, value in events:
                    scratch.write(event, **value)
                scratch.write("diagnostic_summary_trial_end", trace_id="trace_fixture", status="completed", checkpoint_updated=False,
                              input_tokens=23, output_tokens=6)
            finally:
                scratch.close()
            report = verify_runs([scratch.path])
            self.assertEqual(report["diagnostic_counts"]["complete"], 1, report)
            self.assertEqual(report["issues"], [], report)

    def test_native_surrogates_and_nonfinite_extra_usage_are_invalid(self):
        self.assertIsNone(_native_usage({"input_tokens": 3, "output_tokens": 2, "metadata": "\ud800"}))
        self.assertIsNone(_native_usage({"input_tokens": 3, "output_tokens": 2, "metadata": float("nan")}))
        events = native_events()
        fields(events, "http_response", GENERATE)["payload"]["content"][0]["thinking"] = "\ud800"
        self.assert_invalid(events)

    def test_actual_native_reply_context_is_not_openai_input_projection(self):
        events = native_events()
        request = fields(events, "http_request", GENERATE)["payload"]
        context = {"instructions": request["system"], "messages": request["messages"]}
        _validate_reply_request(context, request, provider="anthropic")
        context["messages"] = [{"role": "user", "content": "different input"}]
        with self.assertRaises(ValueError):
            _validate_reply_request(context, request, provider="anthropic")

    def test_plain_native_reply_context_cannot_gain_structured_output(self):
        events = native_events(schema=BOT_REPLY_SCHEMA, text='{"action":"reply","text":"Visible reply."}')
        request = fields(events, "http_request", GENERATE)["payload"]
        context = {"instructions": request["system"], "messages": request["messages"]}
        with self.assertRaises(ValueError):
            _validate_reply_request(context, request, provider="anthropic")

    def test_old_openai_receipt_is_still_verified_strictly(self):
        common = {"model": "gpt-6.1-sol", "instructions": "fixture", "input": [{"role": "user", "content": "input"}],
                  "reasoning": {"effort": "medium"}, "text": {"verbosity": "high"}}
        identity = {"trace_id": "legacy_trace", "call_id": "legacy_call"}
        result = {"text": "legacy output", "input_tokens": 3, "output_tokens": 2, "response_id": "legacy_response", "elapsed_seconds": 0.01}
        def transport(event, path, payload):
            return event, {**identity, "path": path, "payload": payload}
        events = [("call_start", {**identity, "stage": "reply", "model": "gpt-6.1-sol", "verbosity": "high",
                    "budget": {"input_tokens": 64, "output_tokens": 16, "seconds": 1, "reasoning": "medium"}}),
                  transport("http_request", "/responses/input_tokens", common),
                  transport("http_response", "/responses/input_tokens", {"input_tokens": 3}),
                  ("input_gate", {**identity, "stage": "reply", "input_tokens": 3, "limit": 64, "admitted": True}),
                  transport("http_request", "/responses", {**common, "max_output_tokens": 16, "store": False, "stream": False,
                                                              "truncation": "disabled", "tools": []}),
                  transport("http_response", "/responses", {"id": "legacy_response", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2},
                      "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "legacy output"}]}]}),
                  ("call_end", {**identity, "stage": "reply", "status": "completed", "result": result})]
        self.assertEqual(_validate_call(events), "complete")
        fields(events, "http_request", "/responses")["payload"]["reasoning"]["effort"] = "low"
        self.assert_invalid(events)


class ClaudeRuntimeReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, *, bot=False, skip=False, model=MODEL):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            scratch = ScratchLog(root / "scratch")
            config = SimpleNamespace(name="Indeces", discord=DiscordConfig("10"), knowledge=KnowledgeConfig(),
                runtime=RuntimeConfig(summary_max_bytes=256, turn_seconds=5, model_selection_enabled=False),
                adapter=AdapterConfig(model, META["base_url"], {stage: Budget(16384, 2048, 1, "high")
                    for stage in ("reply", "summary", "label")}, provider="anthropic"))
            paths, deliveries = [], []
            visible = "A synthetic grounded observation. [M1]"
            text = json.dumps({"action": "skip" if skip else "reply", "text": "" if skip else visible}) if bot else visible
            async def request(path, payload):
                paths.append(path)
                if path == COUNT:
                    return {"input_tokens": 23}
                response = deepcopy(fields(native_events(text=text, model=model), "http_response", GENERATE)["payload"])
                return response
            async def deliver(text):
                deliveries.append(text)
                return DeliveryReceipt(("synthetic_delivery",), text)
            adapter = ClaudeAdapter(config.adapter, scratch, request=request)
            runtime = Runtime(config, store, adapter, scratch)
            runtime.graph.add("10:knowledge", "synthetic-source", "fixture", [{"text": "alpha topic synthetic observation",
                "quote": "alpha topic synthetic observation", "marks": ["alpha", "topic"]}], 1.0)
            message = IncomingMessage("synthetic_message", "20", "10", "30", "Alice", "alpha topic", "2026-10-13T04:00:00+00:00", author_is_bot=bot)
            try:
                await runtime.process(message, deliver)
            finally:
                await adapter.close()
                scratch.close()
                store.close()
            verify(scratch.path)
            report = verify_runs([scratch.path])
            entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(paths, [COUNT, GENERATE])
            self.assertEqual(deliveries, [] if skip else [visible])
            self.assertEqual(report["issues"], [], report)
            self.assertEqual(report["calls"][0]["status"], "complete", report)
            return report, entries

    async def test_real_runtime_native_user_delivery_is_independently_verified(self):
        report, entries = await self.run_turn()
        self.assertEqual(report["turns"][0]["status"], "complete")
        generated = next(row["fields"] for row in entries if row["event"] == "answer_generated")
        self.assertNotIn("Synthetic private reasoning", generated["record"]["text"])

    async def test_real_runtime_native_bot_reply_preserves_original_decision_binding(self):
        report, _ = await self.run_turn(bot=True)
        self.assertEqual(report["turns"][0]["status"], "complete")

    async def test_real_runtime_native_bot_skip_has_no_delivery(self):
        report, _ = await self.run_turn(bot=True, skip=True)
        self.assertEqual(report["turns"][0]["status"], "skipped")

    async def test_real_runtime_fable_receipt_uses_identical_governance(self):
        report, _ = await self.run_turn(model="claude-fable-5-1")
        self.assertEqual(report["turns"][0]["status"], "complete")

    async def test_actual_runtime_native_retrieval_diagnostic_context_verifies(self):
        _, entries = await self.run_turn()
        trace = next(row["fields"]["trace_id"] for row in entries if row["event"] == "turn_start")
        with TemporaryDirectory() as directory:
            scratch = ScratchLog(Path(directory))
            try:
                scratch.write("diagnostic_trial_start", trace_id=trace, kind="retrieval_reply", operator_approved=True,
                              automatic_retries=0, checkpoint_update=False)
                for row in entries:
                    if row["event"] not in {"turn_start", "turn_end", "delivery_start", "answer_delivered"}:
                        scratch.write(row["event"], **row["fields"])
                scratch.write("diagnostic_trial_end", trace_id=trace, status="completed", checkpoint_updated=False,
                              input_tokens=23, output_tokens=6)
            finally:
                scratch.close()
            report = verify_runs([scratch.path])
            self.assertEqual(report["diagnostic_counts"]["complete"], 1, report)
            self.assertEqual(report["issues"], [], report)


class ClaudeProductionSelectionTests(richard_fixture.RecordFixture, unittest.IsolatedAsyncioTestCase):
    """Actual production factory with synthetic frozen graph data only."""
    seed_bound_record = richard_fixture.RichardChatRuntimeTests.seed_bound_record
    events = staticmethod(richard_fixture.RichardChatRuntimeTests.events)

    def setUp(self):
        richard_fixture.RichardChatRuntimeTests.setUp(self)
        self.config.adapter = AdapterConfig(MODEL, META["base_url"], {
            stage: Budget(65536, 4096, 5, "high") for stage in ("reply", "summary", "label", "selection")},
            provider="anthropic")

    def tearDown(self):
        richard_fixture.RichardChatRuntimeTests.tearDown(self)

    async def native_turn(self, *, richard=True, algorithm="alpha", model=MODEL, query=None,
                          plan=None, unknown_usage=False, negative_span=False, bot=False, duplicate=False):
        self.turn_counter += 1
        query = richard_fixture.DUAL_QUERY if query is None else query
        plan = richard_fixture.grounded_plan(query) if plan is None else deepcopy(plan)
        if negative_span:
            plan["seeds_A"][0]["start"] = -1
        graph_path, source_path = self.root / "native-fixture-graph.zip", self.root / "native-fixture-source.zip"
        graph_path.write_bytes(b"synthetic loader seam, never parsed")
        source_path.write_bytes(b"synthetic loader seam, never parsed")
        self.config.adapter = replace(self.config.adapter, model=model)
        self.config.runtime = replace(self.config.runtime, model_selection_enabled=True, richard_chat_enabled=richard,
            richard_selection_enabled=False, richard_graph_zip=str(graph_path), richard_source_zip=str(source_path),
            richard_chat_algorithm=algorithm, active_requery_enabled=False, path_requery_loop_enabled=False)
        scratch = ScratchLog(self.root / "scratch" / f"native-production-{self.turn_counter}")
        requests, deliveries, notices, selected_ids = [], [], [], []
        visible = "Synthetic bound evidence says this [M1]."

        async def transport(path, payload):
            wire_schema = payload.get("output_config", {}).get("format", {}).get("schema", {})
            selection = "selected_record_ids" in wire_schema.get("properties", {})
            stage = "selection" if selection else "reply"
            requests.append((path, deepcopy(payload), stage))
            self.assertFalse(self.store.db.in_transaction)
            if path == COUNT:
                return {"input_tokens": 43 if selection else 47}
            self.assertEqual(path, GENERATE)
            if selection:
                data = json.loads(payload["messages"][0]["content"])
                ids = [row["record_id"] for row in data["candidates"][:data["required_selection_count"]]]
                selected_ids.extend(ids)
                output = {"selected_record_ids": ids}
                if "richard_plan" in wire_schema["properties"]:
                    output["richard_plan"] = deepcopy(plan)
                text = json.dumps(output)
            else:
                text = json.dumps({"action": "reply", "text": visible}) if bot else visible
            response = {"id": "native-production-" + stage, "type": "message", "role": "assistant", "model": model,
                "stop_reason": "end_turn", "stop_sequence": None,
                "content": [{"type": "thinking", "thinking": "Synthetic hidden content.", "signature": "signed-fixture"},
                            {"type": "text", "text": text}]}
            if not (unknown_usage and selection):
                response["usage"] = {"input_tokens": 7, "cache_read_input_tokens": 29 if selection else 33,
                    "cache_creation_input_tokens": 7, "output_tokens": 11 if selection else 9}
            return response

        async def deliver(text):
            if isinstance(text, FailureNotice):
                notices.append(text.text)
                return DeliveryReceipt(("native-fixed-notice",), text.text)
            deliveries.append(text)
            return DeliveryReceipt(("native-production-delivery",), text)

        adapter = ClaudeAdapter(self.config.adapter, scratch, request=transport)
        try:
            with patch("indeces.richard_v11_data.load_release", return_value=self.richard_graph) as loader:
                runtime = create_runtime(self.config, self.store, adapter, scratch)
            self.assertEqual(loader.call_count, int(richard))
            message = IncomingMessage(f"native-production-message-{self.turn_counter}", "20", "10", "30", "Fixture Human",
                query, "2026-10-13T04:00:00+00:00", author_is_bot=bot)
            with redirect_stdout(io.StringIO()):
                await runtime.process(message, deliver)
                if duplicate:
                    await runtime.process(message, deliver)
        finally:
            await adapter.close()
            scratch.close()
        verify(scratch.path)
        entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(self.network_attempts, [])
        return {"path": scratch.path, "entries": entries, "requests": requests, "deliveries": deliveries,
                "notices": notices, "model_ids": selected_ids, "runtime": runtime}

    def complete_selection(self, result):
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["complete"], 1, report)
        self.assertEqual(report["call_counts"]["complete"], 2, report)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [(COUNT, "selection"), (GENERATE, "selection"), (COUNT, "reply"), (GENERATE, "reply")])
        self.assertEqual(result["deliveries"], ["Synthetic bound evidence says this [M1]."])
        retrieval = self.events(result, "retrieval_record")[0]["record"]
        validate_retrieval(retrieval)
        validate_graph_audit(retrieval["graph_audit"], retrieval)
        native = {row["id"]: row for row in self.native_records}
        for material in retrieval["materials"]:
            original = native[material["record_id"]]
            self.assertEqual((material["source_id"], material["quote"], material["stored_text"]),
                             (original["source_id"], original["quote"], original["text"]))
        return retrieval

    def rewritten(self, result, edit):
        scratch = ScratchLog(self.root / "rewritten" / f"native-production-{self.turn_counter}")
        try:
            for item in deepcopy(result["entries"]):
                edit(item["event"], item["fields"])
                scratch.write(item["event"], **item["fields"])
        finally:
            scratch.close()
        verify(scratch.path)
        return scratch.path

    async def test_actual_standard_selector_native_request_and_original_schema_bind(self):
        result = await self.native_turn(richard=False)
        retrieval = self.complete_selection(result)
        self.assertEqual(retrieval["graph_audit"]["selection"]["selector_receipt"]["policy"]["id"], "model_npmi_labels")

    async def test_actual_richard_alpha_native_rank_and_bound_quotes_preserved(self):
        result = await self.native_turn()
        retrieval = self.complete_selection(result)
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertTrue(diagnostic["ranker_applied"])
        expected = self.ranker.rank((17,), (29,), target="species", level="coarse", variant="alpha_meet")
        by_cid = {cid: row["id"] for cid, row in zip(self.cids, self.native_records)}
        self.assertEqual([row["record_id"] for row in retrieval["materials"]], [by_cid[row.cid] for row in expected[:3]])

    async def test_actual_richard_beta_native_plan_uses_original_solver_boundary(self):
        query = richard_fixture.SINGLE_QUERY
        result = await self.native_turn(algorithm="beta", query=query, plan=richard_fixture.grounded_plan(query, mode="single"))
        self.complete_selection(result)
        self.assertEqual(self.events(result, "richard_chat_selection")[0]["variant"], "beta_nomeet")

    async def test_native_richard_catalog_never_exposes_local_cids(self):
        result = await self.native_turn()
        self.complete_selection(result)
        payload = next(body for path, body, stage in result["requests"] if path == GENERATE and stage == "selection")
        data = json.loads(payload["messages"][0]["content"])
        self.assertTrue(data["richard_catalog"])
        self.assertTrue(all(set(row) == {"term", "start", "end"} for row in data["richard_catalog"]))

    async def test_unknown_native_selection_usage_stops_before_reply(self):
        result = await self.native_turn(unknown_usage=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]], [(COUNT, "selection"), (GENERATE, "selection")])
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["failed"], 1, report)
        end = self.events(result, "call_end")[0]
        self.assertIsNone(end["known_usage"])
        self.assertTrue(end["remote_usage_unknown"])
        self.assertEqual(self.events(result, "answer_generated"), [])

    async def test_negative_original_richard_span_fails_known_usage_without_reply(self):
        result = await self.native_turn(negative_span=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]], [(COUNT, "selection"), (GENERATE, "selection")])
        end = self.events(result, "call_end")[0]
        self.assertEqual(end["known_usage"], {"input_tokens": 43, "output_tokens": 11})
        self.assertFalse(end["remote_usage_unknown"])
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["failed"], 1, report)

    async def test_richard_native_bot_reply_preserves_decision_binding(self):
        result = await self.native_turn(bot=True)
        self.complete_selection(result)
        self.assertEqual(self.events(result, "bot_reply_decision")[0]["action"], "reply")

    async def test_native_richard_duplicate_turn_does_not_send_twice(self):
        result = await self.native_turn(duplicate=True)
        self.complete_selection(result)
        self.assertEqual(len(self.events(result, "duplicate_ignored")), 1)

    async def test_native_fable_richard_uses_same_frozen_grounding(self):
        result = await self.native_turn(model="claude-fable-5-1")
        self.complete_selection(result)

    async def test_native_selection_system_tampering_is_detected(self):
        result = await self.native_turn()
        def edit(event, value):
            if event == "http_request" and value["phase"] == "generation" and value["payload"]["output_config"].get("format", {}).get("schema", {}).get("properties", {}).get("selected_record_ids"):
                value["payload"]["system"] += " changed"
        report = verify_runs([self.rewritten(result, edit)])
        self.assertTrue(report["issues"], report)

    async def test_native_richard_retained_transport_checks_surviving_client_ids(self):
        result = await self.native_turn()
        retrieval = self.complete_selection(result)
        events = [(entry["event"], deepcopy(entry["fields"])) for entry in result["entries"]]
        events = [(event, value) for event, value in events if not (event == "call_start" and value.get("stage") == "selection")]
        report = {"warnings": []}
        _validate_model_selection(events, report, graph=retrieval["graph_audit"], partial=True)
        self.assertIn("expired_richard_chat_transport", report["warnings"])
        selection_id = self.events(result, "model_selection_decision")[0]["model_result"]["response_id"]
        for event, value in events:
            if event == "http_response" and value.get("path") == GENERATE and value["payload"]["id"] == selection_id:
                value["client_request_id"] = "mismatched-retained-client"
        with self.assertRaises(ValueError):
            _validate_model_selection(events, {"warnings": []}, graph=retrieval["graph_audit"], partial=True)


if __name__ == "__main__":
    unittest.main()
