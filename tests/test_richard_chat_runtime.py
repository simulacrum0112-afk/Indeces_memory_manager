"""Synthetic group-chat integration; no provider, Discord, or accuracy claim.

Bojie Li, AI Agents in Depth: Design Principles and Engineering Practice,
v2.0 September 23 2026: E1 ch7 pp232-233/PDF240-241 (independent harness),
A4 ch4 pp127-128/PDF135-136 (strict contracts), S7 ch3 pp111-113/PDF119-121
(source isolation). This engineering guidance is not certification.

Only repository-owned modules and synthetic temporary SQLite/scratch state
are used. Real OpenAIAdapter input gates and usage records run against an
injected transport; all socket resolution/connect methods are denied.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
import socket
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.adapter import OpenAIAdapter
from indeces.config import AdapterConfig, Budget
from indeces.contracts import DeliveryReceipt, IncomingMessage
from indeces.context import encode
from indeces.discord_bridge import DiscordBridge
from indeces.memory import MemoryGraph
from indeces.richard_chat_selection import RichardChatSelector
from indeces.richard_data import from_rows
from indeces.richard_chat_binding import NativeEvidenceBinder
from indeces.richard_v11_ranker import FrozenRanker
from indeces.run_records import validate_graph_audit, validate_retrieval, verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, verify
from tests.test_run_records import RecordFixture


SINGLE_QUERY = "What species relate to alpha?"
DUAL_QUERY = "What species relate to both alpha and topic?"
ORDINARY_QUERY = "alpha topic catch-up"
SPECIES_NAMES = ("bridge", "catalyst", "product", "reactant", "solvent", "ligand",
                 "radical", "complex", "adduct", "intermediate", "cluster", "precursor")


class SyntheticAllowedMentions(SimpleNamespace):
    @classmethod
    def none(cls):
        return cls(users=False, roles=False, everyone=False, replied_user=False)


def grounded_plan(query, *, mode="dual", confidence="high"):
    def seed(term):
        start = query.index(term)
        return {"term": term, "start": start, "end": start + len(term)}

    start = query.index("species")
    return {"mode": mode, "confidence": confidence,
            "seeds_A": [seed("alpha")],
            "seeds_B": [seed("topic")] if mode == "dual" else [],
            "target": "species", "level": "coarse",
            "target_start": start, "target_end": start + len("species")}


def completion(text, stage, *, unknown_usage=False):
    value = {"id": "richard-chat-synthetic-" + stage, "status": "completed",
             "output": [{"type": "message", "role": "assistant", "content": [
                 {"type": "output_text", "text": text}]}]}
    if not unknown_usage:
        value["usage"] = {"input_tokens": 43 if stage == "selection" else 47,
                          "output_tokens": 11 if stage == "selection" else 9}
    return value


class RichardChatRuntimeTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.network_attempts = []

        def deny_network(*args, **kwargs):
            self.network_attempts.append("denied")
            raise AssertionError("network denied in synthetic Richard chat regression")

        self.guards = [patch("socket.create_connection", deny_network),
                       patch("socket.getaddrinfo", deny_network),
                       patch.object(socket.socket, "connect", deny_network),
                       patch.object(socket.socket, "connect_ex", deny_network)]
        for guard in self.guards:
            guard.start()
        self.config.runtime = replace(self.config.runtime, turn_seconds=30)
        self.config.adapter = AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {
            stage: Budget(65536, 4096, 5, reasoning="medium")
            for stage in ("reply", "summary", "label", "selection")})
        self.graph = MemoryGraph(self.store.db,
                                 retrieval_policy=self.config.runtime.retrieval_policy)
        self.native_records = []
        self.graph_records = []
        self.cids = [101 + index * 37 for index in range(len(SPECIES_NAMES))]
        for index, (cid, name) in enumerate(zip(self.cids, SPECIES_NAMES)):
            marks = ["alpha", "topic", name]
            # Distinct documented chunks; no template quotation is fabricated
            # by either the selector or its model-facing plan.
            text = (f"alpha topic {name}; synthetic source {index}. "
                    + f"Documented fixture observation {index}. " * 20)[:400]
            record, graph_row = self.seed_bound_record(text, marks, index)
            self.native_records.append(record)
            self.graph_records.append(graph_row)
        graph_ids = [row["record_id"] for row in self.graph_records]
        nodes = [
            {"cid": 17, "name": "alpha", "members": [17], "record_frequency": 12,
             "coarse": "other", "fine": "other"},
            {"cid": 29, "name": "topic", "members": [29], "record_frequency": 12,
             "coarse": "other", "fine": "other"},
        ] + [{"cid": cid, "name": name, "members": [cid], "record_frequency": 1,
              "coarse": "species", "fine": "s_fixture"}
             for cid, name in zip(self.cids, SPECIES_NAMES)]
        # Reviewed synthetic arithmetic, not rebuilt corpus NPMI or evidence
        # of relevance. Source intersections still match every co_count.
        edges = [{"a": 17, "b": 29, "npmi": 0.4, "co_count": 12}]
        for seed_cid in (17, 29):
            edges.extend({"a": seed_cid, "b": cid,
                          "npmi": round(0.95 - index * 0.04, 2), "co_count": 1}
                         for index, cid in enumerate(self.cids))
        edges.sort(key=lambda row: (row["a"], row["b"]))
        concept_records = {17: graph_ids, 29: graph_ids}
        concept_records.update({cid: [rid] for cid, rid in zip(self.cids, graph_ids)})
        self.richard_graph = from_rows(nodes, edges, record_rows=self.graph_records,
                                       concept_records=concept_records,
                                       source_manifest={"fixture": "synthetic_chat_e2e"})
        self.ranker = FrozenRanker(self.richard_graph)
        self.binder = NativeEvidenceBinder(self.richard_graph, self.store.db)
        self.turn_counter = 0

    def tearDown(self):
        for guard in reversed(self.guards):
            guard.stop()
        self.teardown_fixture()

    def seed_bound_record(self, text, marks, index):
        source_id = "kb:" + f"{index + 1:032x}"
        name = f"richard-chat-synthetic-{index}.md"
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) "
                "VALUES(?,?,?,?,?,?,?)", (source_id, name, text_sha, text, "ready", 1.0, self.scope))
            self.store.db.execute("INSERT INTO knowledge_chunks VALUES(?,0,?,0,?)",
                                  (source_id, text, encode(marks)))
            for table in ("knowledge_desired", "knowledge_published"):
                self.store.db.execute(f"INSERT INTO {table} VALUES(?,?,?)",
                                      (self.scope, name, source_id))
        native = self.graph.add(self.scope, source_id, "knowledge:" + name,
                                [{"text": text, "quote": text, "marks": marks}], 1.0)[0]
        graph_rid = 10001 + index
        graph_row = {"record_id": graph_rid, "source_id": source_id,
                     "version": {"source_id": source_id, "material_sha256": text_sha,
                                 "converted_markdown_sha256": text_sha},
                     "chunk_locations": [{"record_id": graph_rid, "source_id": source_id,
                                          "chunk_index": 0, "start_character": 0,
                                          "end_character": len(text), "pdf_page_numbers": []}]}
        return native, graph_row

    @staticmethod
    def events(result, name):
        return [entry["fields"] for entry in result["entries"] if entry["event"] == name]

    async def run_turn(self, query, *, plan=None, duplicate=False, algorithm="alpha",
                       unknown_selection_usage=False, unknown_reply_usage=False,
                       ordinary_selector=False, invalid_output=False, change_binding=False,
                       rank_delay=0.0, via_bridge=False, author_is_bot=False,
                       admission_probe=False):
        self.turn_counter += 1
        scratch = ScratchLog(self.root / "scratch" / f"richard-chat-{self.turn_counter}")
        requests, deliveries, model_ids, transaction_states = [], [], [], []
        plan_sent = []
        bridge, admissions, delivery_options = None, [], []

        async def transport(path, payload):
            stage = payload.get("text", {}).get("format", {}).get("name", "reply")
            requests.append((path, deepcopy(payload), stage))
            if stage == "selection":
                transaction_states.append(self.store.db.in_transaction)
                if self.store.db.in_transaction:
                    raise AssertionError("SQLite transaction crossed selection await")
                await asyncio.sleep(0)
                if self.store.db.in_transaction:
                    raise AssertionError("SQLite transaction appeared during selection await")
            if path == "/responses/input_tokens":
                return {"input_tokens": 43 if stage == "selection" else 47}
            if path != "/responses":
                raise AssertionError("unexpected mock adapter endpoint")
            if stage == "selection":
                data = json.loads(payload["input"][0]["content"])
                rows, required = data["candidates"], data["required_selection_count"]
                baseline = [row["record_id"] for row in rows[:required]]
                model_ids.extend(baseline)
                response = {"selected_record_ids": [999999] if invalid_output else baseline}
                schema = payload["text"]["format"]["schema"]
                if "richard_plan" in schema["properties"]:
                    response["richard_plan"] = deepcopy(plan)
                    plan_sent.append(deepcopy(plan))
                if change_binding:
                    with self.store.db:
                        self.store.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id=?",
                                              ("0" * 64, self.native_records[0]["source_id"]))
                return completion(json.dumps(response), stage,
                                  unknown_usage=unknown_selection_usage)
            reply_text = "Synthetic bound evidence says this [M1]."
            if author_is_bot:
                reply_text = json.dumps({"action": "reply", "text": reply_text})
            return completion(reply_text, stage,
                              unknown_usage=unknown_reply_usage)

        adapter = OpenAIAdapter(self.config.adapter, scratch, request=transport)
        if ordinary_selector:
            from indeces.model_selection import ModelNPMILabelSelector
            selector = ModelNPMILabelSelector(adapter, scratch, self.config.adapter.budgets["selection"])
        else:
            selector = RichardChatSelector(adapter, scratch, self.config.adapter.budgets["selection"],
                                           ranker=self.ranker, binder=self.binder,
                                           graph_identity=self.richard_graph.identity_sha256,
                                           algorithm=algorithm)
        runtime = Runtime(self.config, self.store, adapter, scratch, selector=selector)
        message = IncomingMessage(f"richard-chat-message-{self.turn_counter}", "20", "10", "30",
                                  "Synthetic Group Human", query, "2026-10-11T05:00:00+00:00",
                                  raw_text=f"<@99> {query}" if via_bridge else "",
                                  author_is_bot=author_is_bot)

        async def deliver(text):
            deliveries.append(text)
            return DeliveryReceipt((f"richard-chat-delivery-{self.turn_counter}",), text)

        original_rank = FrozenRanker.rank

        def rank_with_observed_local_delay(ranker, *args, **kwargs):
            if rank_delay:
                time.sleep(rank_delay)
            return original_rank(ranker, *args, **kwargs)

        async def bridge_reply(text, **options):
            deliveries.append(text)
            delivery_options.append(options)
            return SimpleNamespace(id=f"richard-chat-delivery-{self.turn_counter}")

        try:
            with patch.object(runtime.graph, "prepare_retrieval", wraps=runtime.graph.prepare_retrieval) as prepare, \
                    patch.object(runtime.graph, "finish_retrieval", wraps=runtime.graph.finish_retrieval) as finish, \
                    patch.object(selector, "choose", wraps=selector.choose) as choose, \
                    patch.object(FrozenRanker, "rank", autospec=True, side_effect=rank_with_observed_local_delay) as rank_calls, \
                    redirect_stdout(io.StringIO()):
                if via_bridge:
                    self.config.discord = replace(self.config.discord, channel_ids=("20",))
                    bridge = DiscordBridge(self.config, runtime, scratch)
                    bridge._discord = SimpleNamespace(HTTPException=RuntimeError,
                        AllowedMentions=SyntheticAllowedMentions, Object=lambda **values: SimpleNamespace(**values))
                    bridge._allowed_mentions = SyntheticAllowedMentions.none()
                    source = SimpleNamespace(id=message.message_id, content=message.raw_text,
                        guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=20),
                        author=SimpleNamespace(id=30, display_name="Synthetic Group Author", bot=author_is_bot),
                        webhook_id=None, created_at=message.created_at, reply=bridge_reply)
                    bridge._start_worker()
                    if admission_probe:
                        rejected = (
                            dict(content=query), dict(guild=SimpleNamespace(id=11)),
                            dict(channel=SimpleNamespace(id=21)), dict(webhook_id=444),
                            dict(author=SimpleNamespace(id=99, display_name="Self", bot=True)))
                        for changed in rejected:
                            probe = SimpleNamespace(**{**vars(source), **changed})
                            admissions.append(bridge.enqueue(probe, "99"))
                        self.assertEqual(requests, [])
                    admissions.append(bridge.enqueue(source, "99"))
                    await asyncio.wait_for(bridge._queue.join(), timeout=5)
                    if duplicate:
                        admissions.append(bridge.enqueue(source, "99"))
                else:
                    await runtime.process(message, deliver)
                    if duplicate:
                        await runtime.process(message, deliver)
                counts = {"prepare": prepare.call_count, "choose": choose.call_count,
                          "finish": finish.call_count}
                rank_call_count = rank_calls.call_count
        finally:
            if bridge is not None:
                await bridge.close()
            await adapter.close()
            scratch.close()
        verify(scratch.path)
        entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(self.network_attempts, [])
        return {"runtime": runtime, "selector": selector, "requests": requests,
                "deliveries": deliveries, "model_ids": model_ids, "plan_sent": plan_sent,
                "transaction_states": transaction_states, "counts": counts,
                "rank_call_count": rank_call_count,
                "admissions": admissions, "delivery_options": delivery_options,
                "path": scratch.path, "entries": entries}

    def assert_successful_bound_turn(self, result):
        self.assertEqual(result["counts"], {"prepare": 1, "choose": 1, "finish": 1})
        self.assertEqual(result["transaction_states"], [False, False])
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection"), ("/responses", "selection"),
                          ("/responses/input_tokens", "reply"), ("/responses", "reply")])
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertEqual([field["status"] for field in self.events(result, "turn_end")], ["delivered"])
        frozen = self.events(result, "retrieval_record")[0]["record"]
        validate_retrieval(frozen)
        validate_graph_audit(frozen["graph_audit"], frozen)
        selected = [row["record_id"] for row in frozen["materials"]]
        self.assertEqual(selected, frozen["graph_audit"]["selection"]["selector_receipt"]["selected_record_ids"])
        self.assertEqual(selected, [row["id"] for row in frozen["model_materials"]])
        native = {row["id"]: row for row in self.native_records}
        for material in frozen["materials"]:
            original = native[material["record_id"]]
            self.assertEqual(material["source_id"], original["source_id"])
            self.assertEqual(material["stored_text"], original["text"])
            self.assertEqual(material["quote"], original["quote"])
            self.assertEqual(len(material["quote"]), 400)
            self.assertEqual(material["chunk_index"], 0)
            source = frozen["sources"][material["source_id"]]
            self.assertEqual(source["raw_text"], original["text"])
            self.assertEqual(source["published_source_id"], original["source_id"])
        context = self.events(result, "reply_context")[0]
        citations = json.loads(context["messages"][0]["content"].split("\n", 1)[1])["memory_citations"]
        self.assertEqual([row["id"] for row in citations], selected)
        self.assertEqual([row["quote"] for row in citations], [native[rid]["quote"] for rid in selected])
        self.assertEqual([row["source_id"] for row in citations], [native[rid]["source_id"] for rid in selected])
        reply_payload = [payload for path, payload, stage in result["requests"]
                         if path == "/responses" and stage == "reply"][0]
        self.assertEqual(reply_payload["input"], context["messages"])
        completed_calls = [row for row in self.events(result, "call_end") if row["status"] == "completed"]
        self.assertEqual([row["stage"] for row in completed_calls], ["selection", "reply"])
        self.assertEqual(completed_calls[0]["known_usage"], {"input_tokens": 43, "output_tokens": 11})
        self.assertEqual(completed_calls[1]["known_usage"], {"input_tokens": 47, "output_tokens": 9})
        selection_output = json.loads(completed_calls[0]["result"]["text"])
        self.assertEqual(selection_output["selected_record_ids"], result["model_ids"])
        if result["plan_sent"]:
            self.assertEqual(selection_output["richard_plan"], result["plan_sent"][0])
        decision = self.events(result, "model_selection_decision")[0]
        self.assertEqual(decision["model_result"], completed_calls[0]["result"])
        self.assertEqual(decision["selected_record_ids"], selected)
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertEqual(result["rank_call_count"], int(diagnostic["ranker_called"]))
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["complete"], 1, report)
        return frozen, selected

    def assert_verified_failed_turn(self, result):
        report = verify_runs([result["path"]])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["failed"], 1, report)

    async def test_dual_group_plan_reranks_bound_quotes_and_duplicate_delivers_once(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), duplicate=True)
        _, selected = self.assert_successful_bound_turn(result)
        expected = self.ranker.rank((17,), (29,), target="species", level="coarse", variant="alpha_meet")
        self.assertEqual(len(expected), 10)
        self.assertEqual(len({row.cid for row in expected}), 10)
        by_cid = {cid: row["id"] for cid, row in zip(self.cids, self.native_records)}
        self.assertEqual(selected, [by_cid[row.cid] for row in expected[:3]])
        self.assertNotEqual(selected, result["model_ids"])
        self.assertEqual(result["plan_sent"], [grounded_plan(DUAL_QUERY)])
        self.assertEqual(len(self.events(result, "duplicate_ignored")), 1)
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertTrue(diagnostic["ranker_called"])
        self.assertTrue(diagnostic["ranker_applied"])
        self.assertFalse(diagnostic["fallback_used"])
        self.assertEqual(diagnostic["reason"], "ranked_native_evidence")
        self.assertEqual(diagnostic["variant"], "alpha_meet")
        self.assertEqual(diagnostic["graph_identity"], self.richard_graph.identity_sha256)
        self.assertEqual([row["cid"] for row in diagnostic["ranked_concepts"]], [row.cid for row in expected])
        self.assertEqual(diagnostic["final_record_ids"], selected)
        bindings = diagnostic["binding_snapshot"]["bindings"]
        self.assertEqual(len(bindings), 12)
        self.assertTrue(all(row["native_record_id"] != row["graph_record_id"] for row in bindings))

    async def test_single_group_beta_plan_uses_existing_frozen_ranker(self):
        result = await self.run_turn(SINGLE_QUERY, plan=grounded_plan(SINGLE_QUERY, mode="single"), algorithm="beta")
        _, selected = self.assert_successful_bound_turn(result)
        expected = self.ranker.rank((17,), target="species", level="coarse", variant="beta_nomeet")
        by_cid = {cid: row["id"] for cid, row in zip(self.cids, self.native_records)}
        self.assertEqual(selected, [by_cid[row.cid] for row in expected[:3]])
        self.assertNotEqual(selected, result["model_ids"])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertEqual(diagnostic["variant"], "beta_nomeet")
        self.assertTrue(diagnostic["ranker_applied"])

    async def test_ordinary_chat_uses_existing_model_selection_without_invented_plan(self):
        result = await self.run_turn(ORDINARY_QUERY)
        _, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, result["model_ids"])
        self.assertEqual(result["plan_sent"], [])
        schema = next(payload["text"]["format"]["schema"] for path, payload, stage in result["requests"]
                      if path == "/responses" and stage == "selection")
        self.assertEqual(set(schema["properties"]), {"selected_record_ids"})
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertFalse(diagnostic["ranker_called"])
        self.assertFalse(diagnostic["ranker_applied"])
        self.assertTrue(diagnostic["fallback_used"])
        self.assertEqual(diagnostic["reason"], "no_explicit_target")
        self.assertEqual(diagnostic["final_record_ids"], selected)
        from indeces.model_selection import INSTRUCTIONS
        payload = next(payload for path, payload, stage in result["requests"]
                       if path == "/responses" and stage == "selection")
        self.assertEqual(payload["instructions"], INSTRUCTIONS)
        data = json.loads(payload["input"][0]["content"])
        self.assertNotIn("richard_catalog", data)
        self.assertNotIn("allowed_targets", data)

    async def test_low_confidence_plan_retains_model_selected_ids(self):
        plan = grounded_plan(DUAL_QUERY, confidence="low")
        result = await self.run_turn(DUAL_QUERY, plan=plan)
        _, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, result["model_ids"])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertFalse(diagnostic["ranker_called"])
        self.assertFalse(diagnostic["ranker_applied"])
        self.assertTrue(diagnostic["fallback_used"])
        self.assertEqual(diagnostic["reason"], "low_confidence")

    async def test_plan_with_unanchored_seed_falls_back_without_diffusion(self):
        plan = grounded_plan(DUAL_QUERY)
        plan["seeds_A"][0]["start"] += 1
        result = await self.run_turn(DUAL_QUERY, plan=plan)
        _, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, result["model_ids"])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertFalse(diagnostic["ranker_called"])
        self.assertEqual(diagnostic["reason"], "invalid_seed_anchor")

    async def test_two_group_plan_without_explicit_both_intent_falls_back(self):
        query = "What species relate to alpha and topic?"
        result = await self.run_turn(query, plan=grounded_plan(query))
        _, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, result["model_ids"])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertFalse(diagnostic["ranker_called"])
        self.assertEqual(diagnostic["reason"], "dual_intent_not_explicit")

    async def test_target_pool_under_ten_falls_back_without_padding_concepts(self):
        query = "What species relate to alpha bridge catalyst product?"
        plan = grounded_plan(query, mode="single")
        for term in ("bridge", "catalyst", "product"):
            start = query.index(term)
            plan["seeds_A"].append({"term": term, "start": start, "end": start + len(term)})
        result = await self.run_turn(query, plan=plan)
        _, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, result["model_ids"])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertTrue(diagnostic["ranker_called"])
        self.assertFalse(diagnostic["ranker_applied"])
        self.assertEqual(diagnostic["reason"], "short_target_pool")
        self.assertEqual(len(diagnostic["ranked_concepts"]), 9)
        self.assertEqual(len({row["cid"] for row in diagnostic["ranked_concepts"]}), 9)

    async def test_unbound_source_is_excluded_from_graph_mapping_and_foreign_scope_is_invisible(self):
        with self.store.db:
            self.store.db.execute("UPDATE knowledge_versions SET digest=? WHERE source_id=?",
                                  ("0" * 64, self.native_records[0]["source_id"]))
        foreign = self.graph.add("11:knowledge", "foreign-synthetic-source", "Synthetic foreign author",
                                 [{"text": "alpha topic foreign scope evidence",
                                   "quote": "alpha topic foreign scope evidence",
                                   "marks": ["alpha", "topic", "bridge"]}], 1.0)[0]
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY))
        frozen, selected = self.assert_successful_bound_turn(result)
        self.assertEqual(selected, [row["id"] for row in self.native_records[1:4]])
        diagnostic = self.events(result, "richard_chat_selection")[0]
        self.assertTrue(diagnostic["ranker_applied"])
        unbound = diagnostic["binding_snapshot"]["unbound"]
        self.assertEqual(unbound, [{"record_id": self.native_records[0]["id"],
                                   "reason": "graph_provenance_mismatch"}])
        all_candidate_ids = [row["record_id"] for row in frozen["graph_audit"]["selection"]["ranked_candidates"]]
        self.assertNotIn(foreign["id"], all_candidate_ids)
        self.assertNotIn(self.native_records[0]["id"], selected)
        self.assertNotIn(foreign["source_id"], frozen["sources"])

    async def test_unknown_selection_usage_halts_before_reply_generation(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), unknown_selection_usage=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection"), ("/responses", "selection")])
        self.assertEqual(result["counts"], {"prepare": 1, "choose": 1, "finish": 0})
        self.assertEqual(result["rank_call_count"], 0)
        self.assertEqual(self.events(result, "reply_context"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        self.assertEqual(self.events(result, "answer_delivered"), [])
        endings = self.events(result, "turn_end")
        self.assertEqual([row["status"] for row in endings], ["failed"])
        self.assertEqual(endings[0]["code"], "missing_usage")
        selection_end = [row for row in self.events(result, "call_end") if row["stage"] == "selection"]
        self.assertEqual(len(selection_end), 1)
        self.assertTrue(selection_end[0]["remote_usage_unknown"])
        self.assertFalse(selection_end[0]["generation_usage_received"])
        self.assert_verified_failed_turn(result)

    async def test_invalid_record_id_fails_once_without_graph_or_reply_fallback(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), invalid_output=True)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection"), ("/responses", "selection")])
        self.assertEqual(result["counts"], {"prepare": 1, "choose": 1, "finish": 0})
        self.assertEqual(result["rank_call_count"], 0)
        self.assertEqual(self.events(result, "answer_generated"), [])
        self.assertEqual([(row["status"], row["code"]) for row in self.events(result, "turn_end")],
                         [("failed", "invalid_model_selection")])
        self.assert_verified_failed_turn(result)

    async def test_changed_source_digest_during_model_await_blocks_final_commit(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), change_binding=True)
        self.assertEqual(sum(path == "/responses" for path, _, _ in result["requests"]), 1)
        self.assertEqual(self.events(result, "retrieval_record"), [])
        self.assertEqual(self.events(result, "reply_context"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        endings = self.events(result, "turn_end")
        self.assertEqual([(row["status"], row["code"]) for row in endings],
                         [("failed", "richard_binding_changed")])
        self.assert_verified_failed_turn(result)

    async def test_actual_rank_work_exceeding_local_budget_stops_after_known_selection_usage(self):
        self.config.runtime = replace(self.config.runtime, local_seconds=0.4)
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), rank_delay=0.5)
        self.assertEqual([(path, stage) for path, _, stage in result["requests"]],
                         [("/responses/input_tokens", "selection"), ("/responses", "selection")])
        self.assertEqual(result["rank_call_count"], 1)
        self.assertEqual(result["counts"], {"prepare": 1, "choose": 1, "finish": 0})
        self.assertEqual(self.events(result, "reply_context"), [])
        self.assertEqual(self.events(result, "answer_generated"), [])
        self.assertEqual(self.events(result, "answer_delivered"), [])
        endings = self.events(result, "turn_end")
        self.assertEqual([(row["status"], row["code"]) for row in endings],
                         [("failed", "local_memory_timeout")])
        selection_end = [row for row in self.events(result, "call_end") if row["stage"] == "selection"]
        self.assertEqual(len(selection_end), 1)
        self.assertEqual(selection_end[0]["status"], "completed")
        self.assertEqual(selection_end[0]["known_usage"], {"input_tokens": 43, "output_tokens": 11})
        self.assertFalse(selection_end[0]["remote_usage_unknown"])
        self.assert_verified_failed_turn(result)

    async def test_discord_bridge_human_admission_filters_and_duplicate_preserve_one_answer(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), via_bridge=True,
                                     admission_probe=True, duplicate=True)
        self.assert_successful_bound_turn(result)
        self.assertEqual(result["admissions"], [False] * 5 + [True, False])
        self.assertEqual(len(self.events(result, "discord_message_queued")), 1)
        self.assertEqual(len(self.events(result, "discord_runtime_started")), 1)
        self.assertEqual(len(self.events(result, "discord_delivery_finished")), 1)
        rejected = self.events(result, "discord_message_rejected")
        self.assertEqual([row["reason"] for row in rejected], ["duplicate"])
        options = result["delivery_options"][0]
        self.assertFalse(options["mention_author"])
        self.assertFalse(options["allowed_mentions"].users)
        self.assertFalse(options["allowed_mentions"].roles)
        self.assertFalse(options["allowed_mentions"].everyone)
        self.assertFalse(result["deliveries"][0].startswith("<@"))

    async def test_discord_bridge_bot_reply_retains_gate_round_and_transport_only_peer_mention(self):
        result = await self.run_turn(DUAL_QUERY, plan=grounded_plan(DUAL_QUERY), via_bridge=True,
                                     author_is_bot=True, duplicate=True)
        self.assert_successful_bound_turn(result)
        self.assertEqual(result["admissions"], [True, False])
        queued = self.events(result, "discord_message_queued")[0]
        self.assertTrue(queued["author_is_bot"])
        self.assertEqual(queued["bot_round"], 1)
        bot_decision = self.events(result, "bot_reply_decision")[0]
        self.assertEqual(bot_decision["action"], "reply")
        self.assertTrue(result["deliveries"][0].startswith("<@30> "))
        options = result["delivery_options"][0]
        self.assertFalse(options["mention_author"])
        self.assertEqual([peer.id for peer in options["allowed_mentions"].users], [30])
        self.assertFalse(options["allowed_mentions"].roles)
        self.assertFalse(options["allowed_mentions"].everyone)
        self.assertEqual(len(self.events(result, "discord_delivery_finished")), 1)


if __name__ == "__main__":
    unittest.main()
