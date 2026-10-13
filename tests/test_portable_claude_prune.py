"""Data-free public seam tests; all ledger, graph and wire evidence is synthetic.

The five test graph hashes are patched only inside the fixture. The unchanged
v0.1.2 Graph/Session and original native Claude transport/audits actually run.
No production graph, private grant, successful real receipt or credential is read.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp

from indeces import claude_prune_budget as budget_module
from indeces import claude_prune_loop as loop_module
from indeces.claude_adapter import CLAUDE_BASE_URL, ClaudeAdapter
from indeces.claude_prune_budget import ExistingGrantBinding, PruneBudget, PrunePriceEnvelope
from indeces.claude_prune_loop import PruneToolLoop
from indeces.config import Budget
from indeces.contracts import GovernedError
from indeces.model_provider import create_model_adapter
from indeces._vendor.npmi_prune import Graph, Session
from indeces._vendor.npmi_prune import core as graph_module


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


PRICE = PrunePriceEnvelope(4000, 20000, "2099-01-01T00:00:00+00:00")
NATIVE_USAGE = {"input_tokens": 10, "output_tokens": 8,
                "service_tier": "standard", "inference_geo": "global"}
CHARGE = 200_000


class Scratch:
    def __init__(self):
        self.events = []

    def write(self, event, **fields):
        self.events.append((event, deepcopy(fields)))


class ScriptedTransport:
    def __init__(self, messages):
        self.responses = deque(v for msg in messages for v in ({"input_tokens": 10}, msg))
        self.requests = []

    async def __call__(self, path, payload):
        self.requests.append((path, deepcopy(payload)))
        if not self.responses:
            raise AssertionError("unexpected retry/request")
        return deepcopy(self.responses.popleft())


def native_message(content, *, reason="tool_use", ident="msg_synthetic"):
    return {"id": ident, "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": reason, "stop_sequence": None, "content": deepcopy(content),
            "usage": deepcopy(NATIVE_USAGE)}


def tool(action, ident, **fields):
    return {"type": "tool_use", "id": ident, "name": "npmi_prune", "input": {"action": action, **fields}}


def config():
    return SimpleNamespace(provider="anthropic", model="claude-opus-5-5", base_url=CLAUDE_BASE_URL,
        budgets={s: Budget(4096, 64, 2.0, "high") for s in ("reply", "summary", "label")},
        failure_threshold=3, cooldown_seconds=30.0, verbosity="high")


def legacy_fixture(directory):
    """Independently construct only the original two-table schema."""
    path = directory / "existing.sqlite3"
    policy = canonical({"grant": "synthetic-reviewed-existing", "ceiling_nusd": 5_000_000_000,
                        "model": "claude-opus-5-5", "retries": 0})
    attempt = (1, 1_000_000, "completed", 1, 1, canonical(NATIVE_USAGE), CHARGE, "a" * 64)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE session(id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, spent INTEGER NOT NULL CHECK(spent>=0))")
        db.execute("CREATE TABLE attempt(id INTEGER PRIMARY KEY CHECK(id=1), maximum INTEGER NOT NULL, status TEXT NOT NULL, count_intents INTEGER NOT NULL, generation_intents INTEGER NOT NULL, usage TEXT, charge INTEGER, wire TEXT)")
        db.execute("INSERT INTO session VALUES(1,?,?)", (policy, CHARGE))
        db.execute("INSERT INTO attempt VALUES(?,?,?,?,?,?,?,?)", attempt)
    receipt_attempt = dict(zip(("maximum_nusd", "status", "count_intents", "generation_intents",
        "usage", "charge_nusd", "generation_wire_sha256"), attempt[1:]))
    receipt_attempt["usage"] = deepcopy(NATIVE_USAGE)
    receipt = {"success": True, "ledger": {"grant": "synthetic-reviewed-existing",
        "spent_or_worst_unknown_nusd": CHARGE, "pending_nusd": 0, "attempt": receipt_attempt}}
    receipt_path = directory / "receipt.json"
    raw = canonical(receipt).encode()
    receipt_path.write_bytes(raw)
    metadata = path.stat()
    return ExistingGrantBinding(path.resolve(), (metadata.st_dev, metadata.st_ino),
        "synthetic-reviewed-existing", policy, attempt, CHARGE, 5_000_000_000,
        receipt_path.resolve(), hashlib.sha256(raw).hexdigest())


def known_round(budget, permit):
    common = {"provider": "anthropic", "protocol": "messages", "base_url": CLAUDE_BASE_URL}
    budget.observe(permit, "call_start", {**common, "model": "claude-opus-5-5"})
    budget.observe(permit, "request_intent", {**common, "path": "/messages/count_tokens"})
    budget.observe(permit, "response_received", {**common, "path": "/messages/count_tokens", "counted_input_tokens": 10})
    budget.observe(permit, "request_intent", {**common, "path": "/messages"})
    budget.observe(permit, "response_received", {**common, "path": "/messages", "response_id": "msg_fixture",
        "response_model": "claude-opus-5-5", "native_usage": deepcopy(NATIVE_USAGE),
        "known_usage": {"input_tokens": 10, "output_tokens": 8}})


class PortableBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable-prune-budget-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.binding = legacy_fixture(self.directory)

    def bind(self, binding=None, **kwargs):
        result = PruneBudget.bind_existing_grant(binding or self.binding, price=kwargs.pop("price", PRICE),
                                                 offline_accepted=kwargs.pop("offline_accepted", True), **kwargs)
        self.addCleanup(result.close)
        return result

    def objects(self):
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}

    def test_binding_keeps_policy_attempt_and_existing_balance_then_reopens(self):
        budget = self.bind()
        self.assertTrue(budget.existing_grant_bound)
        self.assertEqual(budget.path, budget.bound_path)
        self.assertEqual(budget.snapshot()["spent_nusd"], CHARGE)
        permit = budget.reserve("one", 100, 20)
        known_round(budget, permit)
        common = {"provider": "anthropic", "protocol": "messages", "base_url": CLAUDE_BASE_URL, "status": "completed"}
        budget.observe(permit, "call_end", common)
        budget.complete(permit)
        self.assertEqual(budget.snapshot()["spent_nusd"], CHARGE * 2)
        budget.close()
        reopened = self.bind()
        self.assertEqual(reopened.snapshot()["spent_nusd"], CHARGE * 2)
        self.assertEqual(reopened.snapshot()["pending_nusd"], 0)
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            self.assertEqual(db.execute("SELECT policy FROM session").fetchone(), (self.binding.policy_json,))
            self.assertEqual(db.execute("SELECT * FROM attempt").fetchall(), [self.binding.completed_attempt])

    def test_missing_existing_path_is_never_created(self):
        missing = self.directory / "absent.sqlite3"
        with self.assertRaises(ValueError):
            self.bind(replace(self.binding, ledger_path=missing))
        self.assertFalse(missing.exists())

    def test_cloned_file_cannot_use_original_identity(self):
        copied = self.directory / "cloned.sqlite3"
        shutil.copyfile(self.binding.ledger_path, copied)
        with self.assertRaises(ValueError):
            self.bind(replace(self.binding, ledger_path=copied))
        self.assertEqual(self.objects(), {"session", "attempt"})

    def test_receipt_hash_or_receipt_grant_mismatch_rejects_before_migration(self):
        self.binding.receipt_path.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            self.bind()
        self.assertEqual(self.objects(), {"session", "attempt"})
        raw = canonical({"success": True, "ledger": {"grant": "other"}}).encode()
        self.binding.receipt_path.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.bind(replace(self.binding, receipt_sha256=hashlib.sha256(raw).hexdigest()))
        self.assertEqual(self.objects(), {"session", "attempt"})

    def test_exact_policy_attempt_and_balance_are_required(self):
        for sql in ("UPDATE session SET spent=spent+1", "UPDATE session SET policy='{}'",
                    "UPDATE attempt SET wire='wrong'"):
            with self.subTest(sql=sql):
                directory = self.directory / str(len(list(self.directory.iterdir())))
                directory.mkdir()
                binding = legacy_fixture(directory)
                with closing(sqlite3.connect(binding.ledger_path)) as db, db:
                    db.execute(sql)
                with self.assertRaises(GovernedError):
                    self.bind(binding)
                with closing(sqlite3.connect(binding.ledger_path)) as db, db:
                    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
                self.assertEqual(names, {"session", "attempt"})

    def test_offline_mode_and_explicit_acceptance_cannot_become_live(self):
        with self.assertRaises(ValueError):
            self.bind(offline_accepted=False)
        for grant, flag in (("a-live-grant", True), ("offline_fixture:test", False)):
            with self.assertRaises(ValueError):
                PruneBudget(self.directory / "new.sqlite3", grant, offline_only=flag, price=PRICE)
        self.assertFalse((self.directory / "new.sqlite3").exists())

    def test_positive_utc_model_price_and_immutable_binding(self):
        for inp, out, expiry, model in ((0, 1, PRICE.valid_until, PRICE.model),
                (1, False, PRICE.valid_until, PRICE.model), (1, 1, "2099-01-01", PRICE.model),
                (1, 1, PRICE.valid_until, "other")):
            with self.assertRaises(ValueError):
                PrunePriceEnvelope(inp, out, expiry, model=model)
        with self.assertRaises(FrozenInstanceError):
            self.binding.grant_id = "changed"
        with self.assertRaises(ValueError):
            self.bind(price=PrunePriceEnvelope(4000, 20000, "2000-01-01T00:00:00+00:00"))
        self.assertEqual(self.objects(), {"session", "attempt"})

    def test_unknown_hold_and_halt_survive_reopen_without_new_balance(self):
        budget = self.bind()
        permit = budget.reserve("uncertain", 100, 20)
        maximum = 100 * 4000 + 20 * 20000
        error = GovernedError("synthetic_unknown")
        budget.fail(permit, error)
        self.assertEqual(budget.snapshot()["pending_nusd"], maximum)
        budget.close()
        reopened = self.bind()
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot["spent_nusd"], CHARGE)
        self.assertEqual(snapshot["pending_nusd"], maximum)
        self.assertIsNotNone(snapshot["halted"])
        with self.assertRaises(GovernedError):
            reopened.reserve("later", 1, 1)

    def test_all_pending_reservations_share_ceiling(self):
        budget = PruneBudget(self.directory / "offline.sqlite3", "offline_fixture:ceiling",
                            ceiling_nusd=800_000, price=PRICE)
        self.addCleanup(budget.close)
        budget.reserve("one", 100, 20)
        with self.assertRaises(GovernedError) as caught:
            budget.reserve("two", 1, 1)
        self.assertEqual(caught.exception.code, "prune_budget_exhausted")
        self.assertEqual(budget.snapshot()["pending_nusd"], 800_000)

    def test_known_receipt_settles_after_expiry_and_shared_halt(self):
        budget = self.bind()
        permit = budget.reserve("in-flight", 100, 20)
        common = {"provider": "anthropic", "protocol": "messages", "base_url": CLAUDE_BASE_URL}
        budget.observe(permit, "call_start", {**common, "model": "claude-opus-5-5"})
        budget.observe(permit, "request_intent", {**common, "path": "/messages/count_tokens"})
        budget.observe(permit, "response_received", {**common, "path": "/messages/count_tokens", "counted_input_tokens": 10})
        budget.observe(permit, "request_intent", {**common, "path": "/messages"})
        budget.halt("other_turn_failed")
        class Expired(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(budget_module, "datetime", Expired):
            budget.observe(permit, "response_received", {**common, "path": "/messages", "response_id": "msg_late",
                "response_model": "claude-opus-5-5", "native_usage": deepcopy(NATIVE_USAGE),
                "known_usage": {"input_tokens": 10, "output_tokens": 8}})
            budget.fail(permit, GovernedError("primary_failure"))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["spent_nusd"], CHARGE * 2)
        self.assertEqual(snapshot["pending_nusd"], 0)
        self.assertEqual(snapshot["halted"], "other_turn_failed")


class SyntheticGraphFixture:
    def make_graph(self):
        graph_dir = self.directory / "graph"
        source_dir = self.directory / "sources"
        graph_dir.mkdir(); source_dir.mkdir()
        def lines(path, records):
            path.write_text("".join(canonical(r) + "\n" for r in records), encoding="utf-8")
        lines(graph_dir / "nodes_v3.jsonl", [{"cid": i, "name": f"synthetic-{i}", "coarse": "target", "fine": "target"}
                                           for i in (1, 2, 3)])
        lines(graph_dir / "edges_v3.jsonl", [{"a": 1, "b": 3, "npmi": 0.6, "co_count": 1},
                                           {"a": 2, "b": 3, "npmi": 0.5, "co_count": 1}])
        (graph_dir / "member_to_cid.json").write_text(canonical({"1": 1, "2": 2, "3": 3}), encoding="utf-8")
        lines(source_dir / "word_records.jsonl", [{"node_id": i, "record_id": 10} for i in (1, 2, 3)])
        lines(source_dir / "chunk_locations.jsonl", [{"record_id": 10, "source_id": "synthetic", "pdf_page_numbers": [1]}])
        graph_pins = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in graph_dir.iterdir()}
        source_pins = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_dir.iterdir()}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (graph_module, loop_module):
            stack.enter_context(patch.object(module, "PINNED_V302", graph_pins))
            stack.enter_context(patch.object(module, "PINNED_INDECES", source_pins))
        self.graph = Graph(str(graph_dir), str(source_dir))
        self.session = Session(self.graph, "synthetic-question", [1], [2], "target")


class PortableNativeLoopTests(SyntheticGraphFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable-prune-loop-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.make_graph()
        self.budget = PruneBudget(self.directory / "offline.sqlite3", "offline_fixture:loop", price=PRICE)
        self.addCleanup(self.budget.close)
        self.scratch = Scratch()
        self.adapter = create_model_adapter(config(), self.scratch, "synthetic-not-a-credential")
        self.assertIs(type(self.adapter), ClaudeAdapter)

    async def client_fixture(self, *, trust_env=False, retry=False):
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None), trust_env=trust_env)
        client._retry_connection = retry
        self.addAsyncCleanup(client.close)
        sentinel = patch.object(client, "post", side_effect=AssertionError("constructor attempted HTTP"))
        post = sentinel.start()
        self.addCleanup(sentinel.stop)
        self.addCleanup(post.assert_not_called)
        return client

    def bind(self):
        binding = legacy_fixture(self.directory)
        budget = PruneBudget.bind_existing_grant(binding, price=PRICE, offline_accepted=True)
        self.addCleanup(budget.close)
        return budget

    async def test_native_view_prune_answer_continuation_preserves_signed_blocks_and_charges_each_prefix(self):
        signed = [{"type": "thinking", "thinking": "synthetic thought", "signature": "synthetic-signature"},
                  {"type": "redacted_thinking", "data": "synthetic-redaction"}]
        blocks = [signed + [tool("view", "toolu_view")],
                  [tool("prune", "toolu_prune", removals=[{"node_id": 3, "reason": "synthetic", "kind": "logic"}])],
                  [tool("answer", "toolu_answer", choice="none")], [{"type": "text", "text": "DONE"}]]
        responses = [native_message(b, reason="end_turn" if i == 3 else "tool_use", ident=f"msg_{i}")
                     for i, b in enumerate(blocks)]
        transport = ScriptedTransport(responses)
        self.adapter._request_override = transport
        loop = PruneToolLoop(self.adapter, self.session, self.budget, max_rounds=4)
        result = await loop.run("Synthetic system", "Synthetic question", "synthetic-trace",
                                input_cap=4096, output_cap=64, seconds=2.0)
        self.assertEqual(result.text, "DONE")
        self.assertEqual(result.answer_receipt["removed"], [3])
        self.assertEqual(result.answer_receipt["unverified_citations"], 0)
        self.assertEqual((result.rounds, result.input_tokens, result.output_tokens), (4, 40, 32))
        self.assertEqual(result.budget_snapshot["spent_nusd"], 4 * CHARGE)
        self.assertEqual(result.budget_snapshot["pending_nusd"], 0)
        self.assertEqual([p for p, _ in transport.requests], [p for _ in range(4) for p in ("/messages/count_tokens", "/messages")])
        generated = [p for path, p in transport.requests if path == "/messages"]
        for i in range(1, 4):
            self.assertEqual(generated[i]["messages"][-2], {"role": "assistant", "content": blocks[i-1]})
            self.assertEqual(generated[i]["messages"][:-2], generated[i-1]["messages"])
            self.assertEqual(generated[i]["messages"][-1]["content"][0]["tool_use_id"], blocks[i-1][-1]["id"])
        self.assertEqual([r["action"] for r in self.session.log], ["start", "view", "prune", "answer"])
        self.assertEqual(set(self.graph.nodes), {1, 2, 3})

    async def test_live_constructor_accepts_bound_instance_without_mutation_or_http(self):
        budget = self.bind()
        self.adapter._session = await self.client_fixture()
        before = budget.snapshot()
        loop = PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertTrue(loop._live)
        self.assertIs(loop._post_view._session, self.adapter._session)
        self.assertEqual(budget.snapshot(), before)
        self.assertEqual(self.scratch.events, [])

    async def test_offline_and_fake_budgets_cannot_activate_live(self):
        self.adapter._session = await self.client_fixture()
        for budget in (self.budget, SimpleNamespace(existing_grant_bound=True, bound_path=self.budget.path, path=self.budget.path)):
            with self.assertRaises(GovernedError) as caught:
                PruneToolLoop(self.adapter, self.session, budget, live=True)
            self.assertEqual(caught.exception.code, "prune_live_existing_grant_required")

    async def test_halted_or_pending_bound_budget_cannot_activate(self):
        budget = self.bind()
        self.adapter._session = await self.client_fixture()
        budget.reserve("pending", 10, 8)
        with self.assertRaises(GovernedError) as caught:
            PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertEqual(caught.exception.code, "prune_live_budget_not_ready")
        budget.halt("synthetic_halt")
        with self.assertRaises(GovernedError) as caught:
            PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertEqual(caught.exception.code, "prune_live_budget_not_ready")

    async def test_live_gate_rejects_injected_transport_unsafe_client_and_missing_key(self):
        budget = self.bind()
        self.adapter._session = await self.client_fixture()
        self.adapter._request_override = ScriptedTransport([])
        with self.assertRaises(GovernedError) as caught:
            PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertEqual(caught.exception.code, "prune_live_native_transport_required")
        self.adapter._request_override = None
        self.adapter._session = await self.client_fixture(retry=True)
        with self.assertRaises(GovernedError) as caught:
            PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertEqual(caught.exception.code, "prune_live_session_unsafe")
        self.adapter._session = await self.client_fixture()
        self.adapter._key = ""
        with self.assertRaises(GovernedError) as caught:
            PruneToolLoop(self.adapter, self.session, budget, live=True)
        self.assertEqual(caught.exception.code, "prune_live_key_required")


if __name__ == "__main__":
    unittest.main()
