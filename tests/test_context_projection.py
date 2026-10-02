"""Offline context admission keeps complete input and frozen retrieval evidence."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces import prompts
from indeces.adapter import OpenAIAdapter, reservation
from indeces.config import Budget, RuntimeConfig
from indeces.context import encode, groups, history_data, raw_capacity, reply_messages
from indeces.contracts import DeliveryReceipt, FailureNotice, GovernedError, IncomingMessage
from indeces.run_records import verify_runs
from indeces.runtime import Runtime
from indeces.scratch import ScratchLog, canonical, verify
from tests.test_run_records import RecordFixture


PROJECTION = "citation_material_v1"
PROJECTED_FIELDS = {
    "citation_id", "citation_marker", "id", "source_id", "scope", "text", "text_truncated",
    "quote", "marks", "direct_marks", "expanded_marks", "ranking_mode", "weight_basis",
    "static_score", "dynamic_score", "ranking_score",
}


def incoming(identity, *, bot=False, text="alpha beta gamma theta"):
    return IncomingMessage(identity, "20", "10", "30", "Synthetic Author", text,
                           "2026-10-02T12:00:00+00:00", raw_text="<@99> " + text,
                           author_is_bot=bot)


def reply_policy(current):
    return ((prompts.bot_reply_instructions("Indeces"), prompts.BOT_REPLY_SCHEMA)
            if current.author_is_bot else (prompts.reply_instructions("Indeces"), None))


class ContextSchemaBoundaryTests(unittest.TestCase):
    def test_actual_bot_schema_utf8_and_json_escaping_admit_capacity_one_but_not_zero(self):
        for text in ("alpha", "\u4e2d\U0001f600", '\\"\n\t\u4e2d\U0001f600'):
            with self.subTest(text=text):
                current = incoming("schema-boundary", bot=True, text=text)
                instructions, schema = reply_policy(current)
                fixed = reservation(instructions, reply_messages("", [], [], current), schema)
                reserve = 4096
                config = SimpleNamespace(runtime=RuntimeConfig(summary_max_bytes=reserve),
                                         adapter=SimpleNamespace(budgets={"reply": Budget(fixed + reserve, 100, 1)}))
                with self.assertRaises(GovernedError) as caught:
                    raw_capacity(config, instructions, current, [], schema)
                self.assertEqual(caught.exception.code, "fixed_context_too_large")
                config.adapter.budgets["reply"] = Budget(fixed + reserve + 1, 100, 1)
                self.assertEqual(raw_capacity(config, instructions, current, [], schema), 1)
                self.assertEqual(json.loads(reply_messages("", [], [], current)[1]["content"])["text"], text)

    def test_bot_extra_instructions_schema_and_metadata_use_the_same_stage_cap(self):
        current = incoming("same-budget", text='\u4e2d "source" \\ path\n')
        config = SimpleNamespace(runtime=RuntimeConfig(),
                                 adapter=SimpleNamespace(budgets={"reply": Budget(16384, 2048, 45)}))
        human_instructions, _ = reply_policy(current)
        bot = replace(current, author_is_bot=True)
        bot_instructions, schema = reply_policy(bot)
        human_cost = reservation(human_instructions, reply_messages("", [], [], current))
        bot_cost = reservation(bot_instructions, reply_messages("", [], [], bot), schema)
        self.assertGreater(bot_cost, human_cost)
        self.assertEqual(raw_capacity(config, human_instructions, current, [])
                         - raw_capacity(config, bot_instructions, bot, [], schema), bot_cost - human_cost)
        self.assertGreater(bot_cost, reservation(bot_instructions, reply_messages("", [], [], bot)))
        self.assertEqual(config.adapter.budgets["reply"], Budget(16384, 2048, 45))


class ContextProjectionRuntimeTests(RecordFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_fixture()
        self.addCleanup(self.teardown_fixture)
        self.config.runtime = RuntimeConfig(summary_max_bytes=4096, turn_seconds=5)
        self.config.adapter = replace(self.config.adapter, budgets={
            "reply": Budget(16384, 2048, 45), "summary": Budget(16384, 2048, 20),
            "label": Budget(4096, 512, 15),
        })
        self.counter = 0

    def seed_repeated_records(self, count=180):
        marks = ["alpha", "beta", "gamma", "theta"]
        texts = [f"alpha beta gamma theta source {index:03d}" for index in range(count)]
        raw_text = "\n".join(texts)
        source = "kb:" + "c" * 32
        name = "synthetic-repeated.md"
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO knowledge_versions(source_id,path,digest,raw_text,status,created_at,scope) "
                "VALUES(?,?,?,?,?,?,?)",
                (source, name, hashlib.sha256(raw_text.encode()).hexdigest(), raw_text, "ready", 1.0, self.scope))
            start = 0
            for index, text in enumerate(texts):
                self.store.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?)",
                                      (source, index, text, start, encode(marks)))
                start += len(text) + 1
            for table in ("knowledge_desired", "knowledge_published"):
                self.store.db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (self.scope, name, source))
        return self.graph.add(self.scope, source, "knowledge:" + name,
                              [{"text": text, "quote": text, "marks": marks} for text in texts], 1.0)

    async def run_turn(self, current):
        self.counter += 1
        scratch = ScratchLog(self.root / "context-projection" / str(self.counter))
        requests, deliveries, selected = [], [], {}
        reply = "Synthetic documented conclusion [M1]."

        async def request(path, payload):
            requests.append((path, deepcopy(payload)))
            if path == "/responses/input_tokens":
                return {"input_tokens": 10}
            stage = payload.get("text", {}).get("format", {}).get("name")
            if stage == "summary":
                text = encode({"summary": "Synthetic attributed continuity remains unresolved."})
            elif current.author_is_bot:
                text = encode({"action": "reply", "text": reply})
            else:
                text = reply
            return {"id": "synthetic-projection-response", "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": text}]}]}

        async def deliver(payload):
            deliveries.append(payload)
            text = payload.text if isinstance(payload, FailureNotice) else payload
            return DeliveryReceipt(("synthetic-delivery-" + str(self.counter),), text)

        adapter = OpenAIAdapter(self.config.adapter, scratch, request=request)
        runtime = Runtime(self.config, self.store, adapter, scratch)
        retrieve = runtime.graph.retrieve

        def capture(*args, **kwargs):
            result = retrieve(*args, **kwargs)
            selected.update(records=deepcopy(result), audit=deepcopy(kwargs["audit"]))
            return result

        try:
            with patch.object(runtime.graph, "retrieve", side_effect=capture), redirect_stdout(io.StringIO()):
                await runtime.process(current, deliver)
        finally:
            await adapter.close()
            scratch.close()
        verify(scratch.path)
        entries = [json.loads(line) for line in scratch.path.read_text(encoding="utf-8").splitlines()]
        return scratch.path, entries, requests, deliveries, selected

    @staticmethod
    def fields(entries, event):
        return [entry["fields"] for entry in entries if entry["event"] == event]

    def assert_verified(self, path, *, complete=1, failed=0):
        report = verify_runs([path])
        self.assertEqual(report["issues"], [], report)
        self.assertEqual(report["counts"]["complete"], complete, report)
        self.assertEqual(report["counts"]["failed"], failed, report)

    async def repeated_support_turn(self, *, bot):
        seeded = self.seed_repeated_records()
        current = incoming("repeated-bot" if bot else "repeated-human", bot=bot)
        before = {stage: asdict(budget) for stage, budget in self.config.adapter.budgets.items()}
        path, entries, requests, deliveries, selected = await self.run_turn(current)
        self.assertEqual([route for route, _ in requests], ["/responses/input_tokens", "/responses"])
        self.assertEqual([call["stage"] for call in self.fields(entries, "call_start")], ["reply"])
        self.assertEqual(deliveries, ["Synthetic documented conclusion [M1]."])
        self.assertEqual(before, {stage: asdict(budget) for stage, budget in self.config.adapter.budgets.items()})
        self.assertEqual(self.fields(entries, "call_start")[0]["budget"], before["reply"])
        self.assert_verified(path)
        record = self.fields(entries, "retrieval_record")[0]["record"]
        self.assertEqual(record["model_projection"], PROJECTION)
        self.assertEqual(canonical(record["graph_audit"]), canonical(selected["audit"]))
        self.assertEqual(record["graph_audit"]["selection"]["active_record_count"], len(seeded))
        self.assertEqual(len(record["graph_audit"]["selection"]["ranked_candidates"]), len(seeded))
        for edge in record["graph_audit"]["selection"]["edge_statistics"]:
            self.assertEqual(len(edge["source_record_ids"]), len(seeded))
        self.assertTrue(all(len(candidate["evidence"]) == 6
                            for candidate in record["graph_audit"]["selection"]["ranked_candidates"]))
        for projected, original, material in zip(record["model_materials"], selected["records"], record["materials"]):
            self.assertEqual(set(projected), PROJECTED_FIELDS)
            for key in PROJECTED_FIELDS - {"citation_id", "citation_marker"}:
                self.assertEqual(projected[key], original[key])
            self.assertEqual(projected["quote"], material["stored_text"])
            self.assertEqual(projected["marks"], material["marks"])
        instructions, schema = reply_policy(current)
        legacy_materials = [{**deepcopy(value), "citation_id": f"M{index}", "citation_marker": f"[M{index}]"}
                            for index, value in enumerate(selected["records"], 1)]
        with self.assertRaises(GovernedError) as caught:
            raw_capacity(self.config, instructions, current, legacy_materials, schema)
        self.assertEqual(caught.exception.code, "fixed_context_too_large")
        self.assertGreater(raw_capacity(self.config, instructions, current, record["model_materials"], schema), 0)
        context = self.fields(entries, "reply_context")[0]
        historical = json.loads(context["messages"][0]["content"].split("\n", 1)[1])
        self.assertEqual(historical["memory_citations"], record["model_materials"])
        self.assertEqual(historical["recent_observations"], [])
        self.assertEqual(historical["continuity_summary"], "")
        self.assertEqual(context["messages"], requests[1][1]["input"])
        self.assertEqual(json.loads(context["messages"][1]["content"])["text"], current.text)
        generated = self.fields(entries, "answer_generated")[0]["record"]
        delivered = self.fields(entries, "answer_delivered")[0]["record"]
        self.assertEqual(generated, delivered)
        self.assertEqual(delivered["citations"][0]["record_id"], record["materials"][0]["record_id"])
        self.assertEqual(delivered["citations"][0]["source_id"], record["materials"][0]["source_id"])

    async def test_human_repeated_support_fits_original_reply_cap_with_complete_audit(self):
        await self.repeated_support_turn(bot=False)

    async def test_bot_repeated_support_fits_original_reply_cap_with_complete_audit(self):
        await self.repeated_support_turn(bot=True)

    async def test_unicode_and_json_escape_current_overflow_never_truncates_or_calls_model(self):
        self.seed_repeated_records(1)
        for index, text in enumerate(("\u4e2d" * 6000 + " alpha beta gamma theta",
                                      '\\"\n' * 4000 + " alpha beta gamma theta")):
            with self.subTest(index=index):
                current = incoming("oversize-current-" + str(index), bot=True, text=text)
                before = {stage: asdict(budget) for stage, budget in self.config.adapter.budgets.items()}
                path, entries, requests, deliveries, _ = await self.run_turn(current)
                self.assertEqual(requests, [])
                self.assertEqual(len(deliveries), 1)
                self.assertIsInstance(deliveries[0], FailureNotice)
                self.assertIn("fixed_context_too_large", deliveries[0].text)
                self.assertEqual(self.fields(entries, "turn_start")[0]["input"]["text"], text)
                self.assertEqual(self.fields(entries, "turn_start")[0]["input"]["raw_text"], current.raw_text)
                self.assertEqual(self.fields(entries, "retrieval_record")[0]["record"]["query"], text)
                self.assertEqual(self.fields(entries, "turn_end")[0]["code"], "fixed_context_too_large")
                self.assertEqual(self.fields(entries, "call_start"), [])
                self.assertEqual(self.fields(entries, "reply_context"), [])
                self.assertEqual(self.fields(entries, "answer_generated"), [])
                self.assertEqual(self.store.db.execute("SELECT content FROM turns WHERE message_id=?",
                                                      (current.message_id,)).fetchone()[0], text)
                self.assertEqual(before, {stage: asdict(budget) for stage, budget in self.config.adapter.budgets.items()})
                self.assert_verified(path, complete=0, failed=1)

    async def test_existing_checkpoint_and_recent_history_preserve_full_current_and_sources(self):
        self.seed_repeated_records(5)
        old = incoming("checkpoint-covered", text="A source-attributed prior observation.")
        self.store.begin(old)
        self.store.finish(old, DeliveryReceipt(("prior-receipt",), "A prior assistant claim with uncertainty."))
        covered = self.store.history(old.scope)
        summary = "Synthetic Author: earlier proposal remains unapproved. \u4e2d\u6587"
        checkpoint = self.store.save_checkpoint(old.scope, covered, summary, "synthetic-old-checkpoint")
        recent = incoming("checkpoint-recent", text='Recent complete text with "quotes" and \\ paths. \u4e2d\u6587')
        self.store.begin(recent)
        self.store.finish(recent, DeliveryReceipt(("recent-receipt",), "Recent unverified assistant observation."))
        expected_recent = self.store.history(old.scope, checkpoint["through_seq"])
        current = incoming("current-with-checkpoint", bot=True, text='alpha beta gamma theta \u4e2d\u6587 "question" \\path\n' * 8)
        path, entries, _, deliveries, _ = await self.run_turn(current)
        self.assertEqual(deliveries, ["Synthetic documented conclusion [M1]."])
        self.assertEqual([call["stage"] for call in self.fields(entries, "call_start")], ["reply"])
        context = self.fields(entries, "reply_context")[0]["messages"]
        data = json.loads(context[0]["content"].split("\n", 1)[1])
        self.assertEqual(data["continuity_summary"], summary)
        self.assertEqual(data["recent_observations"], history_data(expected_recent))
        self.assertEqual(data["memory_citations"], self.fields(entries, "retrieval_record")[0]["record"]["model_materials"])
        self.assertEqual(json.loads(context[1]["content"])["text"], current.text)
        self.assertEqual(self.store.checkpoint(old.scope), checkpoint)
        self.assert_verified(path)

    async def test_real_history_overflow_summarizes_whole_turns_and_keeps_current_complete(self):
        self.seed_repeated_records(5)
        for index in range(4):
            old = incoming("large-history-" + str(index), text="prior source " * 130)
            self.store.begin(old)
            self.store.finish(old, DeliveryReceipt(("history-receipt-" + str(index),), "prior assistant " * 115))
        before = self.store.history("10:20")
        current = incoming("current-after-compaction", bot=True,
                           text='alpha beta gamma theta \u4e2d\u6587 "full question" \\path\n' * 5)
        path, entries, requests, deliveries, _ = await self.run_turn(current)
        self.assertEqual(deliveries, ["Synthetic documented conclusion [M1]."])
        self.assertEqual([call["stage"] for call in self.fields(entries, "call_start")], ["summary", "reply"])
        self.assertEqual([route for route, _ in requests], ["/responses/input_tokens", "/responses"] * 2)
        planned = self.fields(entries, "context_watermark")[0]["planned_source_seqs"]
        checkpoint = self.fields(entries, "checkpoint_saved")[0]["checkpoint"]
        summarized = json.loads(requests[1][1]["input"][0]["content"])["source_prefix"]
        self.assertEqual([row["source_seq"] for row in summarized], planned)
        covered = [row for row in before if row["seq"] in planned]
        self.assertEqual(summarized, history_data(covered))
        self.assertEqual(json.loads(checkpoint["source_ids"]), planned)
        self.assertTrue(planned)
        self.assertEqual(len(covered), 2 * len(groups(covered)))
        self.assertEqual({row["turn_id"] for row in covered}
                         & {row["turn_id"] for row in before if row["seq"] not in planned}, set())
        context = self.fields(entries, "reply_context")[0]["messages"]
        self.assertEqual(json.loads(context[1]["content"])["text"], current.text)
        self.assertEqual(json.loads(context[0]["content"].split("\n", 1)[1])["recent_observations"],
                         history_data([row for row in before if row["seq"] not in planned]))
        self.assertEqual(self.store.history("10:20", exclude_turn=current.message_id), before)
        self.assert_verified(path)


if __name__ == "__main__":
    unittest.main()
