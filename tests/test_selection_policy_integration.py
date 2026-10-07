"""Synthetic selector integration acceptance; no network, keys, or user data.

Every Store, knowledge version and scratch record lives in a TemporaryDirectory.
The HTTP-shaped mock exercises OpenAIAdapter's normal post/receipt path.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage
from indeces.memory import MemoryGraph
from indeces.run_records import digest, freeze_retrieval, validate_graph_audit, validate_retrieval, verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store
from indeces.selection_policy import (RankedTopThreeSelector, SelectionDecision,
                                      SelectionExclusion)
from tests.test_local_retrieval_deadline import LocalClock
from tests.test_run_records import RecordFixture


ACCEPTANCE_EVIDENCE = {
    "real_http_requests": 0, "mock_count_requests": 0, "mock_generation_requests": 0,
    "network_attempts_blocked": 0, "end_to_end": [], "invalid_decisions_checked": [],
}


def deny_network(*args, **kwargs):
    ACCEPTANCE_EVIDENCE["network_attempts_blocked"] += 1
    raise AssertionError("Network is disabled for selector acceptance")


class RecordingSelector:
    policy_id = "synthetic_late_rank"
    policy_version = "v1"

    def __init__(self, *, first=False, invalid=None):
        self.requests = []
        self.first, self.invalid = first, invalid

    def select(self, request):
        self.requests.append(request)
        candidates = request.candidates
        chosen = tuple(c.record_id for c in (candidates[:3] if self.first else candidates[-3:]))
        excluded = tuple(SelectionExclusion(c.record_id, "synthetic_not_chosen")
                         for c in candidates if c.record_id not in chosen)
        if self.invalid == "throw":
            raise RuntimeError("synthetic_secret_exception_must_not_be_logged")
        if self.invalid == "missing_exclusion":
            excluded = excluded[:-1]
        elif self.invalid == "duplicate_exclusion":
            excluded += excluded[:1]
        elif self.invalid == "selected_excluded":
            excluded += (SelectionExclusion(chosen[0], "synthetic_not_chosen"),)
        elif self.invalid == "unknown_exclusion":
            excluded += (SelectionExclusion(999999, "synthetic_not_chosen"),)
        elif self.invalid == "invalid_reason":
            excluded = tuple(SelectionExclusion(e.record_id, "not a stable reason") for e in excluded)
        elif self.invalid == "duplicate_selected":
            chosen = (chosen[0], chosen[0])
        elif self.invalid == "unknown_selected":
            chosen = (999999,)
        elif self.invalid == "over_reference_budget":
            chosen = tuple(c.record_id for c in candidates[:4])
            excluded = tuple(SelectionExclusion(c.record_id, "synthetic_not_chosen")
                             for c in candidates if c.record_id not in chosen)
        elif self.invalid == "boolean_record_id":
            chosen = (True,)
        elif self.invalid == "wrong_return_type":
            return {"selected_record_ids": chosen, "exclusions": excluded}
        return SelectionDecision(chosen, excluded)


class SelectorFixture(RecordFixture):
    def setup_selector_fixture(self):
        self.setup_fixture()
        self.config.runtime = replace(self.config.runtime, turn_seconds=30)
        self.network_guards = [patch("socket.create_connection", deny_network),
                               patch.object(socket.socket, "connect", deny_network),
                               patch.object(socket.socket, "connect_ex", deny_network)]
        for guard in self.network_guards:
            guard.start()
        self.config.adapter = AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {
            stage: Budget(16384, 2048, 1, reasoning="medium") for stage in ("reply", "summary", "label")})

    def teardown_selector_fixture(self):
        for guard in reversed(self.network_guards):
            guard.stop()
        self.teardown_fixture()

    def seed_candidates(self, count=6):
        records = []
        for index in range(count):
            # Exactly 400 Unicode characters, complete bodies differ by source.
            text = (f"alpha topic source {index}: " + f"synthetic fact {index}. " * 30)[:400]
            records.append(self.seed(text.encode(), source_id="kb:" + f"{index + 1:032x}",
                                     name=f"synthetic-source-{index}.md"))
        return records

    def retrieve_with(self, selector, event="synthetic-selector-event", *, policy="legacy_v1"):
        graph = MemoryGraph(self.store.db, retrieval_policy=policy, selector=selector)
        audit = {}
        result = graph.retrieve(self.scope, [], "alpha topic", 2.0, event_id=event, audit=audit)
        frozen = freeze_retrieval(self.store.db, self.scope, event, "alpha topic", result, audit)
        return graph, result, audit, frozen

    def durable_counts(self):
        names = [row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_%'")]
        return {name: self.store.db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                for name in names}


class SelectionPolicyContractTests(SelectorFixture, unittest.TestCase):
    def setUp(self):
        self.setup_selector_fixture()
        self.seed_candidates()

    def tearDown(self):
        self.teardown_selector_fixture()

    def test_selector_receives_all_eligible_candidates_before_cutoff_and_full_quotes(self):
        selector = RecordingSelector()
        _, selected, audit, frozen = self.retrieve_with(selector)
        request = selector.requests[0]
        self.assertGreater(len(request.candidates), 3)
        self.assertEqual(len(request.candidates), len(audit["selection"]["ranked_candidates"]))
        self.assertEqual([c.rank for c in request.candidates], list(range(1, 7)))
        self.assertEqual([r["id"] for r in selected], [c.record_id for c in request.candidates[-3:]])
        self.assertTrue(all(len(c.text) == len(c.quote) == 400 for c in request.candidates))
        self.assertEqual([r["quote"] for r in selected], [c.quote for c in request.candidates[-3:]])
        self.assertEqual(frozen["model_projection"], "citation_material_selector_v1")
        self.assertEqual(audit["selection"]["selector_receipt"]["schema"], "selector_receipt_v1")
        self.assertLessEqual(sum(len(r["text"]) for r in selected), request.limits.total_text_characters)
        self.assertTrue(all(len(r["text"]) <= request.limits.entry_text_characters for r in selected))
        self.assertTrue(all(r["text_truncated"] for r in selected))
        validate_retrieval(frozen)
        validate_graph_audit(audit, frozen)

    def test_default_and_explicit_ranked_top_three_have_identical_model_materials(self):
        for policy in ("legacy_v1", "concept_v1"):
            with self.subTest(policy=policy):
                _, _, default_audit, default = self.retrieve_with(None, "default-" + policy, policy=policy)
                _, _, explicit_audit, explicit = self.retrieve_with(RankedTopThreeSelector(),
                                                                    "explicit-" + policy, policy=policy)
                self.assertEqual(default["model_materials"], explicit["model_materials"])
                self.assertEqual(default["materials"], explicit["materials"])
                self.assertNotIn("selector_receipt", default_audit["selection"])
                self.assertIn("selector_receipt", explicit_audit["selection"])
                self.assertEqual(default["model_projection"], "citation_material_v1")
                validate_graph_audit(default_audit, default)
                validate_graph_audit(explicit_audit, explicit)

    def test_candidate_request_and_nested_content_are_immutable(self):
        selector = RecordingSelector()
        self.retrieve_with(selector)
        request = selector.requests[0]
        self.assertIsInstance(request.candidates, tuple)
        self.assertIsInstance(request.candidates[0].marks, tuple)
        with self.assertRaises(FrozenInstanceError):
            request.query = "changed"
        with self.assertRaises(FrozenInstanceError):
            request.candidates[0].quote = "changed"
        with self.assertRaises(TypeError):
            request.candidates[0] = request.candidates[1]

    def test_invalid_decision_variants_do_not_publish_a_durable_memory_event(self):
        variants = ("missing_exclusion", "duplicate_exclusion", "selected_excluded", "unknown_exclusion",
                    "invalid_reason", "duplicate_selected", "unknown_selected", "over_reference_budget",
                    "boolean_record_id", "wrong_return_type")
        for variant in variants:
            with self.subTest(variant=variant):
                before = self.durable_counts()
                with self.assertRaises(GovernedError) as caught:
                    self.retrieve_with(RecordingSelector(invalid=variant), "invalid-" + variant)
                self.assertEqual(caught.exception.code, "invalid_selection_decision")
                self.assertEqual(self.durable_counts(), before)
                ACCEPTANCE_EVIDENCE["invalid_decisions_checked"].append(variant)

    def test_exception_is_safe_and_has_no_partial_publication(self):
        before = self.durable_counts()
        with self.assertRaises(GovernedError) as caught:
            self.retrieve_with(RecordingSelector(invalid="throw"))
        self.assertEqual(caught.exception.code, "selection_policy_failed")
        self.assertNotIn("synthetic_secret", str(caught.exception))
        self.assertEqual(self.durable_counts(), before)

    def test_same_event_replays_identical_policy_and_selection(self):
        selector = RecordingSelector()
        _, _, audit, frozen = self.retrieve_with(selector)
        before = self.durable_counts()
        _, _, replay_audit, replay_frozen = self.retrieve_with(RecordingSelector())
        self.assertEqual(self.durable_counts(), before)
        self.assertEqual(frozen["model_materials"], replay_frozen["model_materials"])
        self.assertEqual(audit["durable_payload_sha256"], replay_audit["durable_payload_sha256"])

    def test_same_event_rejects_policy_version_choice_default_and_candidate_changes(self):
        self.retrieve_with(RecordingSelector())
        selector_v2 = RecordingSelector()
        selector_v2.policy_version = "v2"
        for selector in (selector_v2, RecordingSelector(first=True), None):
            with self.subTest(selector=type(selector).__name__):
                before = self.durable_counts()
                with self.assertRaises(GovernedError) as caught:
                    self.retrieve_with(selector)
                self.assertEqual(caught.exception.code, "selection_event_mismatch")
                self.assertEqual(self.durable_counts(), before)
        self.seed(b"alpha topic newly eligible synthetic source", source_id="kb:" + "f" * 32,
                  name="additional-synthetic.md")
        with self.assertRaises(GovernedError) as caught:
            self.retrieve_with(RecordingSelector())
        self.assertEqual(caught.exception.code, "selection_event_mismatch")

    def test_default_event_cannot_be_reinterpreted_as_a_selector_event(self):
        self.retrieve_with(None)
        with self.assertRaises(GovernedError) as caught:
            self.retrieve_with(RankedTopThreeSelector())
        self.assertEqual(caught.exception.code, "selection_event_mismatch")

    def test_pre_audit_legacy_event_cannot_be_relabelled_by_an_injected_policy(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO memory_events VALUES(?,?,?,?,?)",
                (self.scope, "pre-audit-event", 1.0, "alpha topic", '["alpha","topic"]'))
        before = self.durable_counts()
        with self.assertRaises(GovernedError) as caught:
            self.retrieve_with(RecordingSelector(), event="pre-audit-event")
        self.assertEqual(caught.exception.code, "selection_event_mismatch")
        self.assertEqual(self.durable_counts(), before)

    def test_empty_eligible_set_is_explicit_and_does_not_fabricate_materials(self):
        selector = RecordingSelector()
        graph = MemoryGraph(self.store.db, selector=selector)
        audit = {}
        query = "entirely unrelated absent terms"
        selected = graph.retrieve(self.scope, [], query, 2.0, event_id="empty-candidates", audit=audit)
        frozen = freeze_retrieval(self.store.db, self.scope, "empty-candidates", query, selected, audit)
        self.assertEqual(selector.requests[0].candidates, ())
        self.assertEqual(selected, [])
        self.assertEqual(frozen["model_materials"], [])
        self.assertEqual(audit["selection"]["selector_receipt"]["candidate_set"]["count"], 0)
        validate_retrieval(frozen)
        validate_graph_audit(audit, frozen)

    def test_async_policy_is_rejected_before_any_sql_access(self):
        class AsyncSelector(RecordingSelector):
            async def select(self, request):
                return super().select(request)

        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            with self.assertRaises(GovernedError) as caught:
                MemoryGraph(self.store.db, selector=AsyncSelector())
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(caught.exception.code, "invalid_selection_decision")
        self.assertEqual(statements, [])

    def test_selector_cannot_choose_an_existing_record_from_another_scope(self):
        foreign = self.graph.add("other-guild:knowledge", "synthetic-foreign-source", "synthetic",
            [{"text": "alpha topic foreign synthetic record", "quote": "foreign complete quote",
              "marks": ["alpha", "topic"]}], 1.0)[0]
        selector = RecordingSelector()

        def select_foreign(request):
            return SelectionDecision((foreign["id"],), tuple(
                SelectionExclusion(candidate.record_id, "synthetic_not_chosen") for candidate in request.candidates))

        selector.select = select_foreign
        before = self.durable_counts()
        with self.assertRaises(GovernedError) as caught:
            self.retrieve_with(selector)
        self.assertEqual(caught.exception.code, "invalid_selection_decision")
        self.assertEqual(self.durable_counts(), before)

    def test_receipt_tampering_and_projection_downgrade_are_rejected(self):
        _, _, _, frozen = self.retrieve_with(RecordingSelector())
        mutations = {
            "missing_receipt": lambda record: record["graph_audit"]["selection"].pop("selector_receipt"),
            "old_projection": lambda record: record.update(model_projection="citation_material_v1"),
            "unknown_projection": lambda record: record.update(model_projection="citation_material_selector_unknown"),
            "unknown_receipt_schema": lambda record: record["graph_audit"]["selection"]["selector_receipt"].update(schema="unknown"),
            "ranked_text_hash": lambda record: record["graph_audit"]["selection"]["ranked_candidates"][0].update(text_sha256="0" * 64),
            "selected_quote": lambda record: record["materials"][0].update(quote="forged synthetic quote"),
            "selected_ids": lambda record: record["graph_audit"]["selection"].update(selected_record_ids=[999999]),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                changed = deepcopy(frozen)
                mutate(changed)
                changed["graph_audit_sha256"] = digest(changed["graph_audit"])
                with self.assertRaises(ValueError):
                    validate_retrieval(changed)
                    validate_graph_audit(changed["graph_audit"], changed)

    def test_rehashed_receipt_cannot_omit_candidates_exclusions_or_quote_binding(self):
        _, _, _, frozen = self.retrieve_with(RecordingSelector())
        mutations = {
            "inventory_omission": lambda receipt: receipt["candidate_set"]["inventory"].pop(0),
            "exclusion_omission": lambda receipt: receipt["exclusions"].pop(0),
            "wrong_quote_binding": lambda receipt: receipt["result_bindings"][0].update(quote_sha256="0" * 64),
            "boolean_record_identity": lambda receipt: receipt["result_bindings"][-1].update(record_id=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                changed = deepcopy(frozen)
                selection = changed["graph_audit"]["selection"]
                receipt = selection["selector_receipt"]
                mutate(receipt)
                candidate_set = receipt["candidate_set"]
                candidate_set["count"] = len(candidate_set["inventory"])
                candidate_set["sha256"] = digest(candidate_set["inventory"])
                receipt["input_sha256"] = digest({"input": receipt["input"],
                    "candidate_set_sha256": candidate_set["sha256"], "limits": selection["limits"]})
                changed["selector_receipt_sha256"] = digest(receipt)
                changed["graph_audit_sha256"] = digest(changed["graph_audit"])
                with self.assertRaises(ValueError):
                    validate_retrieval(changed)
                    validate_graph_audit(changed["graph_audit"], changed)

    def test_graph_only_verifier_rejects_missing_or_unknown_selector_contract_on_replay(self):
        self.retrieve_with(RecordingSelector())
        _, _, replay, _ = self.retrieve_with(RecordingSelector())
        self.assertTrue(replay["replay"])
        _, _, original, _ = self.retrieve_with(RecordingSelector(), event="new-graph-only-event")
        for audit in (original, replay):
            for mutation in ("receipt_missing", "marker_missing", "unknown_marker"):
                with self.subTest(replay=audit["replay"], mutation=mutation):
                    changed = deepcopy(audit)
                    selection = changed["selection"]
                    if mutation == "receipt_missing":
                        selection.pop("selector_receipt")
                    elif mutation == "marker_missing":
                        selection.pop("selector_contract")
                    else:
                        selection["selector_contract"] = "unknown"
                    with self.assertRaises(ValueError):
                        validate_graph_audit(changed)

    def test_unselected_ranking_metadata_cannot_be_forged_even_when_receipt_is_rehashed(self):
        for policy in ("legacy_v1", "concept_v1"):
            _, _, _, frozen = self.retrieve_with(RecordingSelector(), event="rank-metadata-" + policy,
                                                 policy=policy)
            for score in (123456.0, True):
                with self.subTest(policy=policy, forged_score=score):
                    changed = deepcopy(frozen)
                    selection = changed["graph_audit"]["selection"]
                    receipt = selection["selector_receipt"]
                    row = selection["ranked_candidates"][0]
                    self.assertNotIn(row["record_id"], selection["selected_record_ids"])
                    row["ranking_score"] = score
                    receipt["candidate_set"]["inventory"][0]["ranking_score"] = score
                    receipt["candidate_set"]["sha256"] = digest(receipt["candidate_set"]["inventory"])
                    receipt["input_sha256"] = digest({"input": receipt["input"],
                        "candidate_set_sha256": receipt["candidate_set"]["sha256"],
                        "limits": selection["limits"]})
                    changed["selector_receipt_sha256"] = digest(receipt)
                    original_payload = deepcopy(changed["graph_audit"])
                    original_payload.pop("durable_payload_sha256", None)
                    changed["graph_audit"]["durable_payload_sha256"] = digest(original_payload)
                    changed["graph_audit_sha256"] = digest(changed["graph_audit"])
                    with self.assertRaisesRegex(ValueError, "selector candidate ranking score mismatch"):
                        validate_graph_audit(changed["graph_audit"])
                    with self.assertRaisesRegex(ValueError, "selector candidate ranking score mismatch"):
                        validate_retrieval(changed)

    def test_selector_binding_survives_sqlite_reopen_and_default_rejects_without_payload_read(self):
        for policy in ("legacy_v1", "concept_v1"):
            event = "reopened-selector-" + policy
            _, _, audit, frozen = self.retrieve_with(RecordingSelector(), event=event, policy=policy)
            self.store.close()
            self.store = Store(self.root / "state")
            default_graph = MemoryGraph(self.store.db, retrieval_policy=policy)
            blocked_reads = []

            def forbid_payload_read(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ and table == "memory_event_audits" and column == "payload_json":
                    blocked_reads.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            self.store.db.set_authorizer(forbid_payload_read)
            try:
                with self.assertRaises(GovernedError) as caught:
                    default_graph.retrieve(self.scope, [], "alpha topic", 2.0, event_id=event, audit={})
            finally:
                self.store.db.set_authorizer(None)
            self.assertEqual(caught.exception.code, "selection_event_mismatch")
            self.assertEqual(blocked_reads, [])
            graph, _, replay, replay_frozen = self.retrieve_with(RecordingSelector(), event=event, policy=policy)
            self.assertTrue(replay["replay"])
            self.assertEqual(replay_frozen["model_materials"], frozen["model_materials"])
            self.assertEqual(replay["durable_payload_sha256"], audit["durable_payload_sha256"])
            stored_status = self.store.db.execute(
                "SELECT observation_status FROM memory_query_event_headers WHERE scope=? AND event_id=?",
                (self.scope, event)).fetchone()[0]
            prefix, body = stored_status.split(":", 1)
            self.assertEqual(prefix, "selector_header_v1")
            header = json.loads(body)
            self.assertEqual(header["selector_binding"]["receipt_sha256"],
                             digest(audit["selection"]["selector_receipt"]))
            header["selector_binding"]["receipt_sha256"] = "0" * 64
            before = self.durable_counts()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "header is immutable"), self.store.db:
                self.store.db.execute(
                    "UPDATE memory_query_event_headers SET observation_status=? WHERE scope=? AND event_id=?",
                    (prefix + ":" + json.dumps(header, sort_keys=True, separators=(",", ":")), self.scope, event))
            altered_payload = deepcopy(audit)
            altered_payload.pop("durable_payload_sha256", None)
            altered_payload["request"]["query"] = "changed synthetic input"
            with self.assertRaisesRegex(ValueError, "immutable event header mismatch"), self.store.db:
                graph.query_index.publish_event_header(self.scope, event,
                    json.dumps(altered_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            self.assertEqual(self.durable_counts(), before)
            self.assertEqual(self.store.db.execute(
                "SELECT observation_status FROM memory_query_event_headers WHERE scope=? AND event_id=?",
                (self.scope, event)).fetchone()[0], stored_status)
            graph.retrieve(self.scope, [], "alpha topic", 2.0, event_id=event, audit={})


class MockResponse:
    status = 200

    def __init__(self, payload, provider_id):
        self.headers = {"x-request-id": provider_id}
        self.content = self
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        yield json.dumps(self.payload).encode()


class MockSession:
    def __init__(self, count=40):
        self.requests, self.count, self.closed = [], count, False

    def post(self, url, **options):
        payload = deepcopy(options["json"])
        self.requests.append((url, payload))
        if url.endswith("/responses/input_tokens"):
            ACCEPTANCE_EVIDENCE["mock_count_requests"] += 1
            return MockResponse({"input_tokens": self.count}, "synthetic-count-id")
        if not url.endswith("/responses"):
            raise AssertionError("Unexpected mock endpoint")
        ACCEPTANCE_EVIDENCE["mock_generation_requests"] += 1
        return MockResponse({"id": "synthetic-response", "status": "completed",
            "usage": {"input_tokens": self.count, "output_tokens": 9},
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text",
                "text": "Synthetic selected evidence is available [M1]."}]}]}, "synthetic-generation-id")

    async def close(self):
        self.closed = True


class SelectionPolicyRuntimeTests(SelectorFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_selector_fixture()
        self.seed_candidates()
        # Runtime's configured coverage index is initialized before measuring
        # event-publication rollback. This remains synthetic fixture setup.
        self.graph = MemoryGraph(self.store.db, retrieval_policy=self.config.runtime.retrieval_policy)
        self.counter = 0

    def tearDown(self):
        self.teardown_selector_fixture()

    async def run_turn(self, selector, *, count=40, duplicate=False, clock=None):
        self.counter += 1
        scratch = ScratchLog(self.root / "scratch" / f"synthetic-{self.counter}")
        session = MockSession(count)
        adapter = OpenAIAdapter(self.config.adapter, scratch, api_key="synthetic-placeholder")
        adapter._session = session
        runtime = Runtime(self.config, self.store, adapter, scratch, selector=selector)
        message = IncomingMessage(f"synthetic-message-{self.counter}", "20", "10", "30", "Synthetic Human",
                                  "alpha topic", "2026-10-07T20:00:00+00:00")
        deliveries = []

        async def deliver(text):
            deliveries.append(text)
            return DeliveryReceipt((f"synthetic-delivery-{self.counter}",), text)

        try:
            with redirect_stdout(io.StringIO()):
                if clock is None:
                    await runtime.process(message, deliver)
                    if duplicate:
                        await runtime.process(message, deliver)
                else:
                    with patch("indeces.runtime.time", clock):
                        await runtime.process(message, deliver)
        finally:
            await adapter.close()
            scratch.close()
        self.assertTrue(session.closed)
        verify(scratch.path)
        entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
        return runtime, session, deliveries, scratch.path, entries

    @staticmethod
    def events(entries, event):
        return [entry["fields"] for entry in entries if entry["event"] == event]

    async def test_runtime_later_rank_selection_quotes_context_http_and_audit_are_identical(self):
        selector = RecordingSelector()
        _, session, deliveries, path, entries = await self.run_turn(selector)
        self.assertEqual(len(selector.requests), 1)
        request = selector.requests[0]
        self.assertGreater(len(request.candidates), 3)
        retrieval = self.events(entries, "retrieval_record")[0]["record"]
        material_ids = [material["record_id"] for material in retrieval["materials"]]
        self.assertEqual(material_ids, [c.record_id for c in request.candidates[-3:]])
        self.assertTrue(all(c.rank > 3 for c in request.candidates[-3:]))
        quotes = [material["quote"] for material in retrieval["materials"]]
        self.assertEqual(quotes, [c.quote for c in request.candidates[-3:]])
        self.assertEqual(quotes, [material["quote"] for material in retrieval["model_materials"]])
        context = self.events(entries, "reply_context")[0]
        generation = next(payload for url, payload in session.requests if url.endswith("/responses"))
        self.assertEqual(context["messages"], generation["input"])
        self.assertEqual(context["instructions"], generation["instructions"])
        context_quotes = json.loads(context["messages"][0]["content"].split("\n", 1)[1])["memory_citations"]
        self.assertEqual(quotes, [material["quote"] for material in context_quotes])
        self.assertEqual(sum(map(len, quotes)), 1200)
        self.assertEqual(generation["model"], "gpt-6.1-sol")
        self.assertEqual(generation["reasoning"], {"effort": "medium"})
        self.assertEqual(generation["max_output_tokens"], 2048)
        self.assertEqual(generation["text"]["verbosity"], "high")
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(deliveries), 1)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [])
        ACCEPTANCE_EVIDENCE["end_to_end"].append({"eligible_candidates": len(request.candidates),
            "selected_ranks": [c.rank for c in request.candidates[-3:]],
            "selected_record_ids": material_ids, "complete_quote_characters": sum(map(len, quotes)),
            "quote_sha256": [hashlib.sha256(quote.encode()).hexdigest() for quote in quotes],
            "mock_http_requests": len(session.requests), "verify_runs_complete": result["counts"]["complete"]})

    async def test_default_runtime_retains_old_contract_and_material_behavior(self):
        _, session, _, path, entries = await self.run_turn(None)
        retrieval = self.events(entries, "retrieval_record")[0]["record"]
        self.assertEqual(retrieval["model_projection"], "citation_material_v1")
        self.assertNotIn("selector_receipt", retrieval["graph_audit"]["selection"])
        expected = [c["record_id"] for c in retrieval["graph_audit"]["selection"]["ranked_candidates"][:3]]
        self.assertEqual([m["record_id"] for m in retrieval["materials"]], expected)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(verify_runs([path])["counts"]["complete"], 1)

    async def test_duplicate_message_calls_selector_and_mock_http_only_once(self):
        selector = RecordingSelector()
        _, session, deliveries, _, entries = await self.run_turn(selector, duplicate=True)
        self.assertEqual(len(selector.requests), 1)
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(len(self.events(entries, "duplicate_ignored")), 1)

    async def test_selector_failure_and_invalid_choice_have_no_api_or_default_fallback(self):
        for variant, code in (("throw", "selection_policy_failed"),
                              ("unknown_selected", "invalid_selection_decision")):
            with self.subTest(variant=variant):
                before = self.durable_counts()
                _, session, deliveries, _, entries = await self.run_turn(RecordingSelector(invalid=variant))
                self.assertEqual(session.requests, [])
                self.assertEqual(self.durable_counts(), before)
                self.assertEqual(self.events(entries, "retrieval_record"), [])
                self.assertEqual(self.events(entries, "answer_generated"), [])
                self.assertEqual(self.events(entries, "turn_end")[0]["code"], code)
                self.assertEqual(len(deliveries), 1)
                self.assertNotIn("synthetic_secret", repr(entries))

    async def test_actual_input_gate_preserves_budget_and_blocks_generation(self):
        selector = RecordingSelector()
        _, session, deliveries, _, entries = await self.run_turn(selector, count=16385)
        self.assertEqual(len(selector.requests), 1)
        self.assertEqual(len(session.requests), 1)
        self.assertTrue(session.requests[0][0].endswith("/responses/input_tokens"))
        self.assertEqual(self.events(entries, "answer_generated"), [])
        self.assertEqual(self.config.adapter.budgets["reply"].input_tokens, 16384)
        self.assertEqual(self.config.adapter.budgets["reply"].output_tokens, 2048)
        self.assertEqual(len(deliveries), 1)

    async def test_selector_time_is_included_in_existing_local_deadline(self):
        clock = LocalClock()
        selector = RecordingSelector()
        select = selector.select

        def delayed(request):
            decision = select(request)
            clock.elapsed = self.config.runtime.local_seconds + 1
            return decision

        selector.select = delayed
        _, session, deliveries, _, entries = await self.run_turn(selector, clock=clock)
        self.assertEqual(session.requests, [])
        self.assertEqual(len(selector.requests), 1)
        self.assertEqual(self.events(entries, "turn_end")[0]["code"], "local_memory_timeout")
        self.assertEqual(self.events(entries, "answer_generated"), [])
        self.assertEqual(len(deliveries), 1)


if __name__ == "__main__":
    unittest.main()
