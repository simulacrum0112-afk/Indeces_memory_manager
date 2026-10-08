"""Synthetic model label selection checks; no network or private materials."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter, reservation
from indeces.config import Budget
from indeces.console import create_runtime
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage, ModelResult
from indeces.model_selection import ModelNPMILabelSelector
from indeces.run_records import validate_graph_audit, validate_retrieval, verify_runs
from indeces.scratch import ScratchLog, verify
from indeces.selection_policy import (SelectionCandidate, SelectionLimits,
                                      SelectionRequest, digest)
from tests.test_run_records import Scratch
from tests.test_selection_policy_integration import SelectorFixture


def selection_request(count=6):
    candidates = tuple(SelectionCandidate(
        record_id=index + 1, scope="10:knowledge", source_id=f"synthetic-source-{index}",
        fingerprint=f"synthetic-fingerprint-{index}", rank=index + 1,
        text=f"Synthetic body {index} must remain outside the label request.",
        quote=f"Synthetic complete quote {index} must remain outside the label request.",
        marks=("alpha", "topic"), direct_marks=("alpha",), expanded_marks=("topic",),
        direct_match_count=1, effective_score=0.8, static_score=0.8,
        dynamic_score=0.0, ranking_score=2.0) for index in range(count))
    return SelectionRequest("10:knowledge", "synthetic-event", "alpha topic",
                            "previous synthetic topic", "static", "concept_v1",
                            candidates, SelectionLimits())


class ChoiceAdapter:
    def __init__(self, output=None):
        self.output, self.calls = output, []

    async def call(self, stage, instructions, messages, trace_id, schema):
        self.calls.append((stage, instructions, deepcopy(messages), trace_id, deepcopy(schema)))
        data = json.loads(messages[0]["content"])
        text = (json.dumps({"selected_record_ids": [candidate["record_id"]
                   for candidate in data["candidates"][-data["required_selection_count"]:]]})
                if self.output is None else self.output)
        return ModelResult(text, 43, 11, "synthetic-selection-response", 0.01)


class ModelSelectionContractTests(unittest.IsolatedAsyncioTestCase):
    def selector(self, output=None, *, input_tokens=4096):
        scratch, adapter = Scratch(), ChoiceAdapter(output)
        return ModelNPMILabelSelector(adapter, scratch, Budget(input_tokens, 512, 15, "medium")), adapter, scratch

    @staticmethod
    def event(scratch, name):
        return next(fields for event, fields in scratch.events if event == name)

    async def test_one_choice_contains_only_labels_existing_scores_and_stable_ids(self):
        request = selection_request()
        selector, adapter, scratch = self.selector()
        decision = await selector.choose(request, "synthetic-trace")
        self.assertEqual(decision.selected_record_ids, (4, 5, 6))
        self.assertEqual([(item.record_id, item.reason) for item in decision.exclusions],
                         [(1, "model_not_selected"), (2, "model_not_selected"), (3, "model_not_selected")])
        self.assertEqual(len(adapter.calls), 1)
        stage, instructions, messages, trace_id, schema = adapter.calls[0]
        self.assertEqual((stage, trace_id), ("selection", "synthetic-trace"))
        data = json.loads(messages[0]["content"])
        self.assertEqual((data["query"], data["context_query"]), (request.query, request.context_query))
        self.assertEqual(data["candidates"][0]["record_id"], 1)
        self.assertEqual(data["candidates"][0]["static_score"], 0.8)
        self.assertEqual(data["candidates"][0]["relevance_score"], 2.0)
        self.assertNotIn("text", data["candidates"][0])
        self.assertNotIn("quote", data["candidates"][0])
        self.assertNotIn(request.candidates[0].text, messages[0]["content"])
        self.assertEqual(schema["properties"]["selected_record_ids"]["maxItems"], 3)
        receipt = self.event(scratch, "model_selection_input")
        self.assertEqual(receipt["messages"], messages)
        self.assertEqual(receipt["messages_sha256"], digest(messages))
        self.assertEqual(receipt["candidate_set_sha256"], digest([c.identity() for c in request.candidates]))
        self.assertLessEqual(reservation(instructions, messages, schema), 4096)
        terminal = self.event(scratch, "model_selection_decision")
        self.assertEqual(terminal["input_sha256"], receipt["input_sha256"])
        self.assertEqual(terminal["selected_record_ids"], list(decision.selected_record_ids))
        self.assertEqual(terminal["model_result"]["input_tokens"], 43)

    async def test_strict_json_rejects_invalid_ids_shapes_and_incomplete_choices_without_retry(self):
        outputs = (
            '{"selected_record_ids":[1,2,99999]}',
            '{"selected_record_ids":[1,1,2]}',
            '{"selected_record_ids":[true,2,3]}',
            '{"selected_record_ids":[1,2]}',
            '{"selected_record_ids":[]}',
            '{"selected_record_ids":[1,2,3,4]}',
            '{"selected_record_ids":[1,2,3],"answer":"extra"}',
            '{"selected_record_ids":[1,2,3],"selected_record_ids":[4,5,6]}',
            '{"selected_record_ids":[NaN,2,3]}',
            '[1,2,3]',
            'not json',
        )
        for output in outputs:
            with self.subTest(output=output):
                selector, adapter, scratch = self.selector(output)
                with self.assertRaises(GovernedError) as caught:
                    await selector.choose(selection_request(), "synthetic-invalid")
                self.assertEqual(caught.exception.code, "invalid_model_selection")
                self.assertEqual(caught.exception.known_usage, {"input_tokens": 43, "output_tokens": 11})
                self.assertFalse(caught.exception.remote_usage_unknown)
                self.assertEqual(len(adapter.calls), 1)
                terminal = self.event(scratch, "model_selection_decision")
                self.assertEqual(terminal["status"], "failed")
                self.assertEqual(terminal["model_result"]["text"], output)

    async def test_short_candidate_pools_require_exact_available_count(self):
        for count in (1, 2):
            with self.subTest(count=count):
                selector, adapter, _ = self.selector()
                decision = await selector.choose(selection_request(count), "synthetic-short")
                self.assertEqual(decision.selected_record_ids, tuple(range(1, count + 1)))
                self.assertEqual(len(adapter.calls), 1)
                schema = adapter.calls[0][-1]
                self.assertEqual(schema["properties"]["selected_record_ids"]["minItems"], count)

    async def test_empty_candidate_pool_records_explicit_no_call(self):
        selector, adapter, scratch = self.selector()
        decision = await selector.choose(selection_request(0), "synthetic-empty")
        self.assertEqual(decision.selected_record_ids, ())
        self.assertEqual(decision.exclusions, ())
        self.assertEqual(adapter.calls, [])
        terminal = self.event(scratch, "model_selection_decision")
        self.assertEqual(terminal["status"], "no_candidates")
        self.assertEqual(terminal["reason"], "empty_candidate_pool")
        self.assertFalse(terminal["model_call_performed"])

    async def test_input_budget_uses_audited_ranked_prefix_and_excludes_omitted_ids(self):
        request = selection_request(30)
        selector, adapter, scratch = self.selector()
        decision = await selector.choose(request, "synthetic-prefix")
        receipt = self.event(scratch, "model_selection_input")
        visible = receipt["seen_record_ids"]
        self.assertGreaterEqual(len(visible), 3)
        self.assertLess(len(visible), len(request.candidates))
        self.assertEqual(visible, list(range(1, len(visible) + 1)))
        self.assertEqual(receipt["omitted_record_ids"], list(range(len(visible) + 1, 31)))
        self.assertEqual(receipt["omission_reason"], "input_budget")
        self.assertEqual(receipt["seen_candidates_sha256"],
                         digest([c.identity() for c in request.candidates[:len(visible)]]))
        excluded = {item.record_id: item.reason for item in decision.exclusions}
        self.assertTrue(all(excluded[record_id] == "input_budget" for record_id in receipt["omitted_record_ids"]))
        self.assertTrue(set(decision.selected_record_ids) <= set(visible))
        self.assertEqual(len(adapter.calls), 1)
        self.assertLessEqual(receipt["planning_reservation"], selector.budget.input_tokens)

    async def test_prefix_omitted_candidate_cannot_be_selected(self):
        selector, adapter, scratch = self.selector('{"selected_record_ids":[1,2,30]}')
        with self.assertRaises(GovernedError) as caught:
            await selector.choose(selection_request(30), "synthetic-omitted")
        self.assertEqual(caught.exception.code, "invalid_model_selection")
        self.assertIn(30, self.event(scratch, "model_selection_input")["omitted_record_ids"])
        self.assertEqual(len(adapter.calls), 1)

    async def test_budget_too_small_for_required_records_fails_before_model_call(self):
        selector, adapter, scratch = self.selector(input_tokens=64)
        with self.assertRaises(GovernedError) as caught:
            await selector.choose(selection_request(), "synthetic-budget")
        self.assertEqual(caught.exception.code, "selection_input_budget")
        self.assertEqual(adapter.calls, [])
        terminal = self.event(scratch, "model_selection_decision")
        self.assertFalse(terminal["model_call_performed"])
        self.assertEqual(terminal["status"], "failed")

    def test_sync_policy_call_requires_explicit_async_runtime_path(self):
        selector, adapter, _ = self.selector()
        with self.assertRaisesRegex(GovernedError, "model_selection_requires_async"):
            selector.select(selection_request())
        self.assertEqual(adapter.calls, [])


def completed(text, stage):
    return {"id": "synthetic-" + stage, "status": "completed",
            "usage": {"input_tokens": 43 if stage == "selection" else 47,
                      "output_tokens": 11 if stage == "selection" else 9},
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}]}


async def run_synthetic_console_trial(fixture, *, invalid=False, change_revision=False,
                                      count_limit=False, empty=False, selection_error=False):
    """Use the actual Console factory and adaptor with a synthetic transport."""
    scratch = ScratchLog(fixture.root / "scratch" / "model-selection")
    requests, transaction_states, deliveries, choices = [], [], [], []
    runtime = None

    async def transport(path, payload):
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        requests.append((path, deepcopy(payload), stage))
        if stage == "selection":
            transaction_states.append(fixture.store.db.in_transaction)
            if fixture.store.db.in_transaction:
                raise AssertionError("SQLite transaction crossed a model await")
            await asyncio.sleep(0)
            if fixture.store.db.in_transaction:
                raise AssertionError("SQLite transaction appeared across a model await")
        if path == "/responses/input_tokens":
            return {"input_tokens": 4097 if count_limit and stage == "selection"
                    else 43 if stage == "selection" else 47}
        if path != "/responses":
            raise AssertionError("Unexpected synthetic endpoint")
        if stage == "selection":
            if selection_error:
                raise GovernedError("synthetic_selection_transport_error", remote_usage_unknown=True)
            data = json.loads(payload["input"][0]["content"])
            chosen = [candidate["record_id"] for candidate in data["candidates"][-3:]]
            choices.extend(chosen)
            if change_revision:
                runtime.graph.add(fixture.scope, "synthetic-new-revision", "synthetic-author",
                                  [{"text": "alpha topic new synthetic revision",
                                    "quote": "alpha topic new synthetic revision",
                                    "marks": ["alpha", "topic"]}], 2.0)
            text = json.dumps({"selected_record_ids": [999999] if invalid else chosen})
            return completed(text, stage)
        return completed("No synthetic memory evidence was retrieved." if empty
                         else "Synthetic selected evidence is available [M1].", stage)

    adapter = OpenAIAdapter(fixture.config.adapter, scratch, request=transport)
    try:
        runtime = create_runtime(fixture.config, fixture.store, adapter, scratch)
        message = IncomingMessage("synthetic-model-selection-turn", "20", "10", "30", "Synthetic Human",
                                  "unmatched987654321" if empty else "alpha topic",
                                  "2026-10-08T05:00:00+00:00")

        async def deliver(text):
            deliveries.append(text)
            return DeliveryReceipt(("synthetic-delivery",), text)

        with patch.object(runtime.graph, "prepare_retrieval", wraps=runtime.graph.prepare_retrieval) as prepare, \
                patch.object(runtime.graph, "finish_retrieval", wraps=runtime.graph.finish_retrieval) as finish, \
                patch.object(runtime.graph, "retrieve", wraps=runtime.graph.retrieve) as retrieve, \
                redirect_stdout(io.StringIO()):
            await runtime.process(message, deliver)
            call_counts = {"prepare": prepare.call_count, "finish": finish.call_count,
                           "retrieve": retrieve.call_count}
    finally:
        await adapter.close()
        scratch.close()
    verify(scratch.path)
    entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
    return {"runtime": runtime, "requests": requests, "transaction_states": transaction_states,
            "deliveries": deliveries, "choices": choices, "call_counts": call_counts,
            "path": scratch.path, "entries": entries}


class ModelSelectionRuntimeTests(SelectorFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_selector_fixture()
        self.seed_candidates()

    def tearDown(self):
        self.teardown_selector_fixture()

    @staticmethod
    def events(result, event):
        return [entry["fields"] for entry in result["entries"] if entry["event"] == event]

    async def test_production_console_model_choice_binds_complete_quotes_inputs_and_usage(self):
        result = await run_synthetic_console_trial(self)
        self.assertIsInstance(result["runtime"].model_selector, ModelNPMILabelSelector)
        self.assertEqual(result["runtime"].model_selector.policy_id, "model_npmi_labels")
        self.assertEqual(result["call_counts"]["prepare"], 1)
        self.assertEqual(result["call_counts"]["finish"], 1)
        self.assertLessEqual(result["call_counts"]["retrieve"], 1)
        self.assertEqual(result["transaction_states"], [False, False])
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection"), ("/responses", "selection"),
                          ("/responses/input_tokens", "reply"), ("/responses", "reply")])
        retrieval = self.events(result, "retrieval_record")[0]["record"]
        selected_ids = [material["id"] for material in retrieval["model_materials"]]
        self.assertEqual(selected_ids, result["choices"])
        self.assertEqual(selected_ids, retrieval["graph_audit"]["selection"]["selector_receipt"]["selected_record_ids"])
        candidate_rows = retrieval["graph_audit"]["selection"]["ranked_candidates"]
        self.assertTrue(all(next(row["rank"] for row in candidate_rows if row["record_id"] == record_id) > 3
                            for record_id in selected_ids))
        quotes = [material["quote"] for material in retrieval["model_materials"]]
        self.assertTrue(all(len(quote) == 400 for quote in quotes))
        self.assertEqual(quotes, [material["quote"] for material in retrieval["materials"]])
        validate_retrieval(retrieval)
        validate_graph_audit(retrieval["graph_audit"], retrieval)
        selection_input = self.events(result, "model_selection_input")[0]
        generation = next(payload for path, payload, stage in result["requests"]
                          if path == "/responses" and stage == "selection")
        self.assertEqual(selection_input["messages"], generation["input"])
        self.assertEqual(selection_input["instructions"], generation["instructions"])
        self.assertEqual(selection_input["response_schema"], generation["text"]["format"]["schema"])
        selection_result = self.events(result, "model_selection_decision")[0]
        self.assertEqual(selection_result["selected_record_ids"], selected_ids)
        self.assertEqual(selection_result["input_sha256"], selection_input["input_sha256"])
        call_end = next(fields for fields in self.events(result, "call_end") if fields["stage"] == "selection")
        self.assertEqual(selection_result["model_result"], call_end["result"])
        self.assertEqual((call_end["result"]["input_tokens"], call_end["result"]["output_tokens"]), (43, 11))
        reply = next(payload for path, payload, stage in result["requests"]
                     if path == "/responses" and stage == "reply")
        citations = json.loads(reply["input"][0]["content"].split("\n", 1)[1])["memory_citations"]
        self.assertEqual([material["quote"] for material in citations], quotes)
        self.assertEqual(len(result["deliveries"]), 1)
        verification = verify_runs([result["path"]])
        self.assertEqual(verification["issues"], [])
        self.assertEqual(verification["counts"]["complete"], 1)

    async def test_invalid_model_choice_has_no_reply_or_ranked_fallback(self):
        result = await run_synthetic_console_trial(self, invalid=True)
        self.assertEqual([stage for path, _, stage in result["requests"] if path == "/responses"], ["selection"])
        self.assertEqual(result["call_counts"]["finish"], 0)
        self.assertEqual(self.events(result, "retrieval_record"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        self.assertEqual(self.events(result, "turn_end")[0]["code"], "invalid_model_selection")
        self.assertEqual(len(result["deliveries"]), 1)

    async def test_source_revision_change_during_selection_rejects_without_requery_or_reply(self):
        result = await run_synthetic_console_trial(self, change_revision=True)
        self.assertEqual(result["call_counts"]["prepare"], 1)
        self.assertEqual(result["call_counts"]["finish"], 1)
        self.assertLessEqual(result["call_counts"]["retrieve"], 1)
        self.assertEqual([stage for path, _, stage in result["requests"] if path == "/responses"], ["selection"])
        self.assertEqual(self.events(result, "retrieval_record"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        self.assertEqual(self.events(result, "turn_end")[0]["status"], "failed")
        self.assertEqual(self.events(result, "turn_end")[0]["code"], "selection_sources_changed")

    async def test_selection_transport_failure_retains_unknown_usage_and_has_no_reply_fallback(self):
        result = await run_synthetic_console_trial(self, selection_error=True)
        self.assertEqual([stage for path, _, stage in result["requests"] if path == "/responses"], ["selection"])
        self.assertEqual(self.events(result, "model_selection_decision"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        call_end = next(fields for fields in self.events(result, "call_end") if fields["stage"] == "selection")
        self.assertTrue(call_end["remote_usage_unknown"])
        self.assertIsNone(call_end["known_usage"])
        self.assertEqual(self.events(result, "turn_end")[0]["status"], "failed")

    async def test_exact_selection_count_gate_blocks_generation_and_reply(self):
        result = await run_synthetic_console_trial(self, count_limit=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection")])
        self.assertEqual(result["call_counts"]["finish"], 0)
        self.assertEqual(self.events(result, "answer_generated"), [])
        gate = next(fields for fields in self.events(result, "input_gate") if fields["stage"] == "selection")
        self.assertEqual((gate["input_tokens"], gate["limit"], gate["admitted"]), (4097, 4096, False))

    async def test_empty_production_pool_skips_selection_call_and_preserves_empty_audit(self):
        result = await run_synthetic_console_trial(self, empty=True)
        self.assertEqual([stage for _, _, stage in result["requests"]], ["reply", "reply"])
        self.assertEqual(result["transaction_states"], [])
        terminal = self.events(result, "model_selection_decision")[0]
        self.assertEqual(terminal["status"], "no_candidates")
        self.assertFalse(terminal["model_call_performed"])
        retrieval = self.events(result, "retrieval_record")[0]["record"]
        self.assertEqual(retrieval["model_materials"], [])
        self.assertEqual(retrieval["graph_audit"]["selection"]["selector_receipt"]["selected_record_ids"], [])
        self.assertEqual(verify_runs([result["path"]])["issues"], [])

    async def test_complete_run_requires_bound_model_input_output_and_usage_records(self):
        result = await run_synthetic_console_trial(self)
        mutations = ("remove_model_events", "selection_input", "actual_http_input",
                     "visible_inventory", "decision_output", "decision_usage")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tampered = ScratchLog(self.root / "scratch" / ("tamper-" + mutation))
                try:
                    for entry in result["entries"]:
                        event, fields = entry["event"], deepcopy(entry["fields"])
                        if mutation == "remove_model_events" and event in {
                                "model_selection_input", "model_selection_decision"}:
                            continue
                        if mutation == "selection_input" and event == "model_selection_input":
                            data = json.loads(fields["messages"][0]["content"])
                            data["query"] = "changed synthetic question"
                            fields["messages"][0]["content"] = json.dumps(data)
                        if (mutation == "actual_http_input" and event == "http_request"
                                and fields["path"] == "/responses"
                                and fields["payload"].get("text", {}).get("format", {}).get("name") == "selection"):
                            data = json.loads(fields["payload"]["input"][0]["content"])
                            data["candidates"][0]["static_score"] = 0.0
                            fields["payload"]["input"][0]["content"] = json.dumps(data)
                        if mutation == "visible_inventory" and event == "model_selection_input":
                            fields["seen_record_ids"] = list(reversed(fields["seen_record_ids"]))
                        if mutation == "decision_output" and event == "model_selection_decision":
                            fields["model_result"]["text"] = '{"selected_record_ids":[999999,999998,999997]}'
                        if mutation == "decision_usage" and event == "model_selection_decision":
                            fields["model_result"]["input_tokens"] = 0
                        tampered.write(event, **fields)
                finally:
                    tampered.close()
                # Rebuild a valid scratch hash chain, so failure must come from
                # the independent run contract rather than a broken file hash.
                verify(tampered.path)
                self.assertTrue(verify_runs([tampered.path])["issues"])


if __name__ == "__main__":
    unittest.main()
