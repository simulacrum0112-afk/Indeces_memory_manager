from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig, RuntimeConfig
from indeces.adapter import OpenAIAdapter
from indeces.context import encode
from indeces.contracts import DeliveryReceipt, GovernedError, IncomingMessage
from indeces.discord_bridge import discord_reply_text
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.run_records import (answer_record, digest, freeze_retrieval, validate_answer,
                                validate_graph_audit, validate_retrieval, verify_runs)
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from indeces.store import Store


SOURCE_A = "kb:" + "a" * 32
SOURCE_B = "kb:" + "b" * 32


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class RecordFixture:
    def setup_fixture(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.store = Store(self.root / "state")
        self.graph = MemoryGraph(self.store.db)
        self.scope = "10:knowledge"
        self.scratch = Scratch()
        self.config = SimpleNamespace(name="Indeces", knowledge_dir=self.root / "knowledge",
            knowledge=KnowledgeConfig(), discord=DiscordConfig("10"),
            runtime=RuntimeConfig(summary_max_bytes=256, turn_seconds=5),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {
                stage: Budget(16384, 2048, 1) for stage in ("reply", "summary", "label")}))
        # Only initialize the schema. Source fixtures never dispatch a model.
        KnowledgeService(self.config, self.store, self.graph, None, self.scratch)

    def seed(self, raw=b"alpha topic documented source", *, source_id=SOURCE_A, name="source.md"):
        text = raw.decode("utf-8-sig")
        digest = hashlib.sha256(raw).hexdigest()
        with self.store.db:
            self.store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                (source_id, name, digest, text, "ready", 1.0, self.scope))
            self.store.db.execute("INSERT INTO knowledge_chunks VALUES(?,0,?,0,?)",
                (source_id, text, encode(["alpha", "topic"])))
            for table in ("knowledge_desired", "knowledge_published"):
                self.store.db.execute(f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(scope,path) DO UPDATE SET source_id=excluded.source_id",
                    (self.scope, name, source_id))
        return self.graph.add(self.scope, source_id, "knowledge:" + name, [
            {"text": text, "quote": text, "marks": ["alpha", "topic"]}], 1.0)[0]

    def retrieval(self, *, event_id="retrieval-event", query="alpha topic"):
        audit = {}
        selected = self.graph.retrieve(self.scope, [], query, 2.0, event_id=event_id, audit=audit)
        return freeze_retrieval(self.store.db, self.scope, event_id, query, selected, audit)

    def teardown_fixture(self):
        self.store.close()
        self.directory.cleanup()


class RetrievalRecordTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def tearDown(self):
        self.teardown_fixture()

    def test_record_freezes_full_materials_and_binds_short_marker_to_ids(self):
        fact = self.seed()
        retrieval = self.retrieval()
        validate_retrieval(retrieval)
        self.assertEqual(retrieval["version"], 1)
        self.assertEqual(retrieval["scope"], self.scope)
        self.assertEqual(retrieval["event_id"], "retrieval-event")
        self.assertEqual(retrieval["query"], "alpha topic")
        self.assertEqual(retrieval["model_materials"][0]["citation_id"], "M1")
        self.assertEqual(retrieval["model_materials"][0]["citation_marker"], "[M1]")
        material = retrieval["materials"][0]
        self.assertEqual(material["record_id"], fact["id"])
        self.assertEqual(material["source_id"], SOURCE_A)
        self.assertEqual(material["stored_text"], fact["text"])
        self.assertEqual(material["quote"], fact["quote"])
        source = retrieval["sources"][SOURCE_A]
        self.assertEqual(source["raw_text"], fact["quote"])
        self.assertEqual(source["scope"], self.scope)
        self.assertEqual(source["published_source_id"], SOURCE_A)
        self.assertEqual(source["desired_source_id"], SOURCE_A)

    def test_bom_original_digest_remains_distinct_from_normalized_text_digest(self):
        raw = b"\xef\xbb\xbf" + "alpha topic 中文 source".encode()
        self.seed(raw)
        retrieval = self.retrieval()
        validate_retrieval(retrieval)
        source = retrieval["sources"][SOURCE_A]
        self.assertEqual(source["original_file_bytes_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(source["normalized_text_sha256"], hashlib.sha256(raw.decode("utf-8-sig").encode()).hexdigest())
        self.assertNotEqual(source["original_file_bytes_sha256"], source["normalized_text_sha256"])
        self.assertFalse(source["raw_text"].startswith("\ufeff"))

    def test_model_material_explicitly_distinguishes_static_basis_from_shadow_weight(self):
        self.seed()
        retrieval = self.retrieval()
        payload = retrieval["model_materials"][0]
        self.assertEqual((payload["ranking_mode"], payload["weight_basis"]), ("static", "static_npmi"))
        self.assertEqual(payload["ranking_score"], 1.0)
        self.assertEqual(payload["dynamic_score"], 1.99)
        payload["weight_basis"] = "dynamic_or_static"
        with self.assertRaisesRegex(ValueError, "model material weight basis"):
            validate_graph_audit(retrieval["graph_audit"], retrieval)

    def test_full_material_survives_model_display_text_truncation(self):
        raw = ("alpha topic " + "long source text " * 20).encode()
        fact = self.seed(raw)
        retrieval = self.retrieval()
        validate_retrieval(retrieval)
        self.assertTrue(retrieval["model_materials"][0]["text_truncated"])
        self.assertLess(len(retrieval["model_materials"][0]["text"]), len(fact["text"]))
        self.assertEqual(retrieval["materials"][0]["stored_text"], fact["text"])
        self.assertEqual(retrieval["materials"][0]["quote"], fact["quote"])

    def test_frozen_snapshot_remains_verifiable_after_publication_switch(self):
        fact = self.seed()
        retrieval = self.retrieval()
        frozen = deepcopy(retrieval)
        self.graph.deactivate_source(self.scope, SOURCE_A)
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET status='superseded' WHERE source_id=?", (SOURCE_A,))
        self.seed(b"alpha topic later completed snapshot", source_id=SOURCE_B)
        validate_retrieval(retrieval)
        self.assertEqual(retrieval, frozen)
        self.assertEqual(retrieval["sources"][SOURCE_A]["status"], "ready")
        self.assertEqual(retrieval["sources"][SOURCE_A]["published_source_id"], SOURCE_A)
        answer = answer_record("Original snapshot says this. [M1]", retrieval)
        validate_answer(answer, retrieval)
        self.assertEqual(retrieval["materials"][0]["record_id"], fact["id"])

    def test_material_and_graph_snapshot_tampering_is_detected(self):
        self.seed()
        original = self.retrieval()
        changed = deepcopy(original)
        changed["sources"][SOURCE_A]["raw_text"] += " forged"
        with self.assertRaises(ValueError):
            validate_retrieval(changed)
        changed = deepcopy(original)
        changed["graph_audit"]["request"]["query"] = "different query"
        with self.assertRaises(ValueError):
            validate_retrieval(changed)

    def test_no_hit_record_has_no_material_or_fabricated_marker(self):
        self.seed()
        retrieval = self.retrieval(query="unrelated words")
        validate_retrieval(retrieval)
        self.assertEqual(retrieval["model_materials"], [])
        self.assertEqual(retrieval["materials"], [])
        self.assertEqual(retrieval["sources"], {})
        answer = answer_record("No matching memory was retrieved.", retrieval)
        validate_answer(answer, retrieval)
        self.assertEqual(answer["citations"], [])

    def test_whitespace_only_chunks_may_leave_sparse_original_indices(self):
        first, last = "alpha topic first segment", "alpha topic last segment"
        raw = (first + " " * 100 + last).encode()
        self.seed(raw)
        with self.store.db:
            self.store.db.execute("DELETE FROM knowledge_chunks WHERE source_id=?", (SOURCE_A,))
            self.store.db.executemany("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)", [
                (SOURCE_A, 0, first, 0, encode(["alpha", "topic"])),
                (SOURCE_A, 2, last, len(first) + 100, encode(["alpha", "topic"]))])
        self.graph.deactivate_source(self.scope, SOURCE_A)
        self.graph.add(self.scope, SOURCE_A, "knowledge:source.md", [
            {"text": first, "quote": first, "marks": ["alpha", "topic"]},
            {"text": last, "quote": last, "marks": ["alpha", "topic"]}], 1.0)
        retrieval = self.retrieval()
        validate_retrieval(retrieval)
        self.assertEqual([c["chunk_index"] for c in retrieval["sources"][SOURCE_A]["chunks"]], [0, 2])
        self.assertEqual({m["chunk_index"] for m in retrieval["materials"]}, {0, 2})

    def test_desired_replacement_pointer_is_logged_without_replacing_published_source(self):
        self.seed()
        with self.store.db:
            self.store.db.execute("INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) VALUES(?,?,?,?,?,?,?)",
                (SOURCE_B, "source.md", hashlib.sha256(b"new pending source").hexdigest(), "new pending source", "pending", 3.0, self.scope))
            self.store.db.execute("UPDATE knowledge_desired SET source_id=? WHERE scope=? AND path='source.md'", (SOURCE_B, self.scope))
        retrieval = self.retrieval()
        source = retrieval["sources"][SOURCE_A]
        self.assertEqual(source["desired_source_id"], SOURCE_B)
        self.assertEqual(source["published_source_id"], SOURCE_A)
        self.assertEqual(source["status"], "ready")
        validate_retrieval(retrieval)

    def test_legacy_material_without_file_metadata_is_explicitly_identified(self):
        self.graph.add(self.scope, "legacy-source", "prior imported source", [
            {"text": "alpha topic legacy text", "quote": "original quote", "marks": ["alpha", "topic"]}], 1.0)
        retrieval = self.retrieval()
        self.assertEqual(retrieval["sources"]["legacy-source"], {"source_id": "legacy-source", "metadata_available": False})
        self.assertIsNone(retrieval["materials"][0]["chunk_index"])
        validate_retrieval(retrieval)

    def test_kb_material_without_knowledge_version_is_rejected(self):
        self.graph.add(self.scope, SOURCE_A, "knowledge:missing.md", [
            {"text": "alpha topic missing source", "quote": "missing source", "marks": ["alpha", "topic"]}], 1.0)
        with self.assertRaisesRegex(ValueError, "metadata missing"):
            self.retrieval()

    def test_source_chunk_cannot_omit_nonwhitespace_tail(self):
        self.seed()
        retrieval = self.retrieval()
        source = retrieval["sources"][SOURCE_A]
        source["raw_text"] += " omitted material"
        source["normalized_text_sha256"] = hashlib.sha256(source["raw_text"].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "coverage incomplete"):
            validate_retrieval(retrieval)


class AnswerRecordTests(RecordFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.seed()
        self.retrieved = self.retrieval()

    def tearDown(self):
        self.teardown_fixture()

    def test_unicode_offsets_bind_repeated_markers_to_their_literal_paragraphs(self):
        text = "中文结论 [M1]。\n\nSecond paragraph [M1] cites again."
        result = answer_record(text, self.retrieved)
        validate_answer(result, self.retrieved)
        self.assertEqual(len(result["citations"]), 2)
        for item in result["citations"]:
            self.assertEqual(text[item["start_character"]:item["end_character"]], "[M1]")
            self.assertEqual(text[item["paragraph_start"]:item["paragraph_end"]], item["paragraph"])
            self.assertEqual(item["source_id"], SOURCE_A)
            self.assertEqual(item["record_id"], self.retrieved["materials"][0]["record_id"])
        self.assertEqual(result["citations"][0]["paragraph"], "中文结论 [M1]。")
        self.assertEqual(result["citations"][1]["paragraph"], "Second paragraph [M1] cites again.")
        self.assertEqual(result["cited_material_ids"], ["M1"])

    def test_unknown_and_noncanonical_markers_do_not_resolve_to_a_source(self):
        result = answer_record("Known [M1]; unknown [M9], [M01], [MX], [M].", self.retrieved)
        validate_answer(result, self.retrieved)
        self.assertEqual(result["unresolved_markers"], ["[M9]", "[M01]", "[MX]", "[M]"])
        self.assertEqual(result["cited_material_ids"], ["M1"])
        for item in result["citations"][1:]:
            self.assertEqual(item["status"], "unresolved")
            self.assertNotIn("source_id", item)
            self.assertNotIn("record_id", item)

    def test_literal_code_marker_establishes_no_semantic_support_or_coverage(self):
        text = 'The example syntax is:\n\n```text\n[M1]\n```\n\nAn uncited assertion.'
        result = answer_record(text, self.retrieved)
        self.assertEqual(len(result["citations"]), 1)
        self.assertEqual(result["citations"][0]["paragraph"], "```text\n[M1]\n```")
        self.assertEqual(result["semantic_support"], "not_evaluated")
        self.assertEqual(result["citation_coverage"], "not_established")
        self.assertEqual(result["text"], text)

    def test_uncited_material_is_reported_without_appending_a_marker(self):
        text = "A plain answer without a citation."
        result = answer_record(text, self.retrieved)
        self.assertEqual(result["text"], text)
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["uncited_material_ids"], ["M1"])

    def test_discord_truncation_recomputes_offsets_and_drops_unseen_citation(self):
        text = "Beginning [M1]. " + "a" * 2100 + " Final [M1]."
        generated = answer_record(text, self.retrieved)
        delivered_text = discord_reply_text(text)
        delivered = answer_record(delivered_text, self.retrieved)
        self.assertEqual(len(generated["citations"]), 2)
        self.assertEqual(len(delivered["citations"]), 1)
        self.assertEqual(delivered["citations"][0]["paragraph_end"], len(delivered_text))
        self.assertNotEqual(generated["text_sha256"], delivered["text_sha256"])
        validate_answer(generated, self.retrieved)
        validate_answer(delivered, self.retrieved)

    def test_marker_removed_by_truncation_does_not_count_as_delivered_citation(self):
        text = "a" * 2100 + " [M1]"
        generated = answer_record(text, self.retrieved)
        delivered = answer_record(discord_reply_text(text), self.retrieved)
        self.assertEqual(generated["cited_material_ids"], ["M1"])
        self.assertEqual(delivered["cited_material_ids"], [])
        self.assertEqual(delivered["uncited_material_ids"], ["M1"])

    def test_tampered_text_offsets_or_source_binding_are_rejected(self):
        original = answer_record("A conclusion [M1].", self.retrieved)
        for mutation in ("text", "offset", "source"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(original)
                if mutation == "text":
                    changed["text"] += " changed"
                elif mutation == "offset":
                    changed["citations"][0]["start_character"] += 1
                else:
                    changed["citations"][0]["source_id"] = SOURCE_B
                with self.assertRaises(ValueError):
                    validate_answer(changed, self.retrieved)


class RuntimeRecordTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.counter = 0

    def tearDown(self):
        self.teardown_fixture()

    async def run_turn(self, text="Documented conclusion [M1].", *, during_generation=None, deliver_transform=discord_reply_text):
        self.counter += 1
        scratch = ScratchLog(self.root / "scratch" / ("run-" + str(self.counter)))
        requests, deliveries = [], []

        async def request(path, payload):
            requests.append((path, deepcopy(payload)))
            if path == "/responses/input_tokens":
                return {"input_tokens": 10}
            if during_generation is not None:
                during_generation()
            return {"id": "synthetic-response", "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}

        async def deliver(generated):
            deliveries.append(generated)
            return DeliveryReceipt(("delivery-" + str(self.counter),), deliver_transform(generated))

        # Exercise the actual adapter/logging path with synthetic HTTP results;
        # neither a key, network session, nor Discord connection is used.
        adapter = OpenAIAdapter(self.config.adapter, scratch, request=request)
        runtime = Runtime(self.config, self.store, adapter, scratch)
        incoming = IncomingMessage("message-" + str(self.counter), "20", "10", "30", "Alice", "alpha topic", "2026-09-30T12:00:00+00:00")
        try:
            await runtime.process(incoming, deliver)
        finally:
            await adapter.close()
            scratch.close()
        self.assertEqual([path for path, _ in requests], ["/responses/input_tokens", "/responses"])
        self.assertEqual(deliveries, [text])
        verify(scratch.path)
        entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
        return scratch.path, entries

    def events(self, entries, event):
        return [item["fields"] for item in entries if item["event"] == event]

    def rewritten(self, entries, name, *, edit=None, omit=()):
        scratch = ScratchLog(self.root / "rewritten" / name)
        try:
            for item in deepcopy(entries):
                if item["event"] not in omit:
                    if edit is not None:
                        edit(item["event"], item["fields"])
                    scratch.write(item["event"], **item["fields"])
        finally:
            scratch.close()
        verify(scratch.path)  # Rewriting is hash-valid; contract verification must still fail.
        return scratch.path

    async def test_actual_runtime_records_exact_model_materials_and_both_reply_forms(self):
        self.seed(b"\xef\xbb\xbfalpha topic original source")
        path, entries = await self.run_turn()
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [])
        retrieval = self.events(entries, "retrieval_record")[0]
        self.assertEqual(retrieval["record_sha256"], digest(retrieval["record"]))
        context = self.events(entries, "reply_context")[0]
        request = next(fields for fields in self.events(entries, "http_request") if fields["path"] == "/responses")
        start = self.events(entries, "call_start")[0]
        self.assertEqual(start["verbosity"], "high")
        self.assertEqual(request["payload"]["text"]["verbosity"], "high")
        self.assertEqual(request["payload"]["reasoning"]["effort"], "low")
        self.assertEqual(context["messages"], request["payload"]["input"])
        self.assertEqual(context["instructions"], request["payload"]["instructions"])
        generated = self.events(entries, "answer_generated")[0]["record"]
        delivered = self.events(entries, "answer_delivered")[0]["record"]
        self.assertEqual(generated, delivered)
        self.assertEqual(delivered["citations"][0]["source_id"], SOURCE_A)
        self.assertNotIn("Authorization", path.read_text(encoding="utf-8"))

    async def test_hash_valid_wrong_or_missing_verbosity_binding_is_invalid(self):
        self.seed()
        _, entries = await self.run_turn()
        for mutation in ("call", "payloads", "missing"):
            with self.subTest(mutation=mutation):
                def change(event, fields):
                    if event == "call_start" and mutation == "call":
                        fields["verbosity"] = "low"
                    elif event == "call_start" and mutation == "missing":
                        fields.pop("verbosity")
                    elif event == "http_request" and mutation == "payloads":
                        fields["payload"]["text"]["verbosity"] = "low"
                path = self.rewritten(entries, "verbosity-" + mutation, edit=change)
                result = verify_runs([path])
                self.assertEqual(result["counts"]["invalid"], 1, result)
                self.assertEqual(result["call_counts"]["invalid"], 1, result)
                self.assertTrue(any("verbosity binding" in issue["reason"] for issue in result["issues"]), result)

    async def test_prior_receipts_without_verbosity_remain_compatible(self):
        self.seed()
        _, entries = await self.run_turn()
        def prior_contract(event, fields):
            if event == "call_start":
                fields.pop("verbosity")
            elif event == "http_request":
                fields["payload"]["text"].pop("verbosity")
                if not fields["payload"]["text"]:
                    fields["payload"].pop("text")
        path = self.rewritten(entries, "prior-no-verbosity", edit=prior_contract)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [])

    async def test_publication_switch_during_generation_keeps_old_material_binding(self):
        self.seed()

        def publish_new():
            self.graph.deactivate_source(self.scope, SOURCE_A)
            with self.store.db:
                self.store.db.execute("UPDATE knowledge_versions SET status='superseded' WHERE source_id=?", (SOURCE_A,))
            self.seed(b"alpha topic newer published source", source_id=SOURCE_B)

        path, entries = await self.run_turn(during_generation=publish_new)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)
        material = self.events(entries, "retrieval_record")[0]["record"]["materials"][0]
        delivered = self.events(entries, "answer_delivered")[0]["record"]
        self.assertEqual(material["source_id"], SOURCE_A)
        self.assertEqual(delivered["citations"][0]["source_id"], SOURCE_A)
        self.assertEqual(self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=? AND path='source.md'", (self.scope,)).fetchone()[0], SOURCE_B)

    async def test_runtime_truncated_delivery_keeps_distinct_verified_generated_spans(self):
        self.seed()
        text = "Documented [M1]. " + "a" * 2100 + " Ending [M1]."
        path, entries = await self.run_turn(text)
        generated = self.events(entries, "answer_generated")[0]["record"]
        delivered = self.events(entries, "answer_delivered")[0]["record"]
        self.assertEqual(len(generated["citations"]), 2)
        self.assertEqual(len(delivered["citations"]), 1)
        self.assertEqual(delivered["text"], discord_reply_text(text))
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)

    async def test_unknown_citation_is_warning_without_false_integrity_or_semantic_claim(self):
        self.seed()
        path, entries = await self.run_turn("Known [M1], unknown [M9].")
        result = verify_runs([path])
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertIn("unresolved_citation", result["turns"][0]["warnings"])
        delivered = self.events(entries, "answer_delivered")[0]["record"]
        self.assertEqual(delivered["unresolved_markers"], ["[M9]"])
        self.assertEqual(delivered["semantic_support"], "not_evaluated")

    async def test_hash_valid_wrong_retrieval_link_is_reported_invalid(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "answer_generated":
                fields["retrieval_sha256"] = "0" * 64

        path = self.rewritten(entries, "wrong-link", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertIn("retrieval link mismatch", result["issues"][0]["reason"])

    async def test_hash_valid_wrong_actual_http_input_is_reported_invalid(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "http_request" and fields["path"] == "/responses":
                fields["payload"]["input"] = [{"role": "user", "content": "substituted private text"}]

        path = self.rewritten(entries, "wrong-http-input", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertNotIn("substituted private text", repr(result))

    async def test_missing_provider_evidence_cannot_be_claimed_complete(self):
        self.seed()
        _, entries = await self.run_turn()
        path = self.rewritten(entries, "missing-provider", omit=("http_request", "http_response"))
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)

    async def test_generated_model_usage_must_match_the_actual_call_receipt(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "answer_generated":
                fields["model_result"]["input_tokens"] += 1

        path = self.rewritten(entries, "wrong-generated-usage", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertTrue(result["issues"])

    async def test_call_provider_response_id_must_match_transport_response(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "call_end" and fields["stage"] == "reply":
                fields["result"]["response_id"] = "substituted-response-id"
            if event == "answer_generated":
                fields["model_result"]["response_id"] = "substituted-response-id"

        path = self.rewritten(entries, "wrong-response-id", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertEqual(result["call_counts"]["invalid"], 1, result)

    async def test_missing_turn_end_is_incomplete_and_prior_schema_is_legacy(self):
        self.seed()
        _, entries = await self.run_turn()
        partial = self.rewritten(entries, "partial", omit=("answer_delivered", "turn_end"))

        def old_start(event, fields):
            if event == "turn_start":
                fields.pop("run_record_version")
                fields["trace_id"] = "legacy-turn"
            else:
                fields["trace_id"] = "legacy-turn"
            if "call_id" in fields:
                fields["call_id"] = "legacy-" + fields["call_id"]

        legacy = self.rewritten(entries, "legacy", edit=old_start)
        result = verify_runs([partial, legacy])
        self.assertEqual(result["counts"]["incomplete"], 1, result)
        self.assertEqual(result["counts"]["legacy"], 1, result)
        self.assertEqual(result["counts"]["complete"], 0)
        self.assertEqual(result["issues"], [])

    async def test_hash_valid_nested_schema_error_is_invalid_and_does_not_crash_verifier(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "retrieval_record":
                fields["record"]["sources"][SOURCE_A]["raw_text"] = 42
                fields["record_sha256"] = digest(fields["record"])

        path = self.rewritten(entries, "bad-nested-schema", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertTrue(result["issues"])

    async def test_hash_valid_delivery_receipt_identity_change_is_invalid(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "turn_end":
                fields["receipt"]["ids"] = ["unconfirmed-receipt"]

        path = self.rewritten(entries, "wrong-delivery-receipt", edit=change)
        result = verify_runs([path])
        self.assertEqual(result["counts"]["invalid"], 1, result)
        self.assertIn("delivery receipt mismatch", result["issues"][0]["reason"])

    async def test_one_turn_split_across_daily_files_still_verifies(self):
        self.seed()
        _, entries = await self.run_turn()
        boundary = next(index for index, item in enumerate(entries) if item["event"] == "answer_generated")
        first = self.rewritten(entries[:boundary], "day-one")
        second = self.rewritten(entries[boundary:], "day-two")
        result = verify_runs([first, second])
        self.assertEqual(result["counts"]["complete"], 1, result)
        self.assertEqual(result["call_counts"]["complete"], 1, result)
        self.assertEqual(result["issues"], [])

    async def test_failed_turn_remains_failed_without_claiming_delivered_evidence(self):
        self.seed()
        _, entries = await self.run_turn()

        def change(event, fields):
            if event == "turn_end":
                fields.clear()
                fields.update(trace_id=self.events(entries, "turn_start")[0]["trace_id"], status="failed", code="synthetic-delivery-failure")

        path = self.rewritten(entries, "failed-delivery", edit=change, omit=("answer_delivered",))
        result = verify_runs([path])
        self.assertEqual(result["counts"]["failed"], 1, result)
        self.assertEqual(result["counts"]["complete"], 0)
        self.assertEqual(result["issues"], [])

    async def test_passive_label_trace_is_a_call_and_not_a_conversation_turn(self):
        scratch = ScratchLog(self.root / "passive")

        async def request(path, payload):
            return {"input_tokens": 10} if path == "/responses/input_tokens" else {
                "id": "synthetic-label", "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"marks":["alpha"]}'}]}]}

        adapter = OpenAIAdapter(self.config.adapter, scratch, request=request)
        try:
            await adapter.call("label", "Label local source text.", [{"role": "user", "content": "alpha source"}], "passive-label-trace")
        finally:
            await adapter.close()
            scratch.close()
        verify(scratch.path)
        result = verify_runs([scratch.path])
        self.assertEqual(result["turns"], [])
        self.assertTrue(all(count == 0 for count in result["counts"].values()))
        self.assertEqual(result["call_counts"]["complete"], 1)
        self.assertEqual(result["issues"], [])

    async def test_failed_generation_confirmed_usage_binds_transport_and_allows_actual_breach_counts(self):
        for case, code, output_tokens in (("incomplete", "incomplete_response", 6),
                                          ("breach", "provider_token_limit_breach", 2049),
                                          ("invalid_output", "invalid_output", 6)):
            with self.subTest(case=case):
                scratch = ScratchLog(self.root / ("failed-usage-" + case))

                async def request(path, payload):
                    if path.endswith("input_tokens"):
                        return {"input_tokens": 17}
                    return {"id": "synthetic-rejected", "status": "incomplete" if case == "incomplete" else "completed",
                            "usage": {"input_tokens": 17, "output_tokens": output_tokens},
                            "output": None if case == "invalid_output" else [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "partial model text"}]}]}

                adapter = OpenAIAdapter(self.config.adapter, scratch, request=request)
                try:
                    with self.assertRaises(GovernedError) as caught:
                        await adapter.call("label", "Label local source.", [{"role": "user", "content": "alpha source"}], "failed-label-" + case)
                finally:
                    await adapter.close()
                    scratch.close()
                self.assertEqual(caught.exception.code, code)
                entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
                end = self.events(entries, "call_end")[0]
                self.assertEqual(end["known_usage"], {"input_tokens": 17, "output_tokens": output_tokens})
                self.assertEqual(end["status"], "failed")
                self.assertNotIn("result", end)
                report = verify_runs([scratch.path])
                self.assertEqual(report["call_counts"]["failed"], 1, report)
                self.assertEqual(report["issues"], [])
                for mutation in ("known", "transport", "missing", "unknown"):
                    def edit(event, fields):
                        if event == "call_end":
                            if mutation == "known":
                                fields["known_usage"]["output_tokens"] += 1
                            elif mutation == "missing":
                                fields.pop("known_usage")
                            elif mutation == "unknown":
                                fields["remote_usage_unknown"] = True
                        elif event == "http_response" and fields["path"] == "/responses" and mutation == "transport":
                            fields["payload"]["usage"]["input_tokens"] += 1

                    rewritten = self.rewritten(entries, "failed-usage-" + case + "-" + mutation, edit=edit)
                    changed = verify_runs([rewritten])
                    self.assertEqual(changed["call_counts"]["invalid"], 1, changed)
                    self.assertTrue(changed["issues"])


if __name__ == "__main__":
    unittest.main()
