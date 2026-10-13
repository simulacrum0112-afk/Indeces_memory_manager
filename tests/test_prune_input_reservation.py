"""Three synthetic counterexamples separating local caps from financial holds.

The native count endpoint is a local admission check, not a billing guarantee.
All receipts, graph files, credentials and pre-existing balances here are test
fixtures. Native transport is scripted; no HTTP or private ledger is accessed.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from indeces import console_prune
from indeces.claude_prune_budget import PruneBudget
from indeces.claude_prune_loop import PruneToolLoop
from indeces.contracts import GovernedError
from indeces.model_provider import create_model_adapter

try:
    from test_console_prune import ConsolePruneFixture
    from test_portable_claude_prune import CHARGE, PRICE, Scratch, ScriptedTransport, canonical, native_message, tool
except ModuleNotFoundError:
    from tests.test_console_prune import ConsolePruneFixture
    from tests.test_portable_claude_prune import CHARGE, PRICE, Scratch, ScriptedTransport, canonical, native_message, tool


class PruneInputReservationTests(ConsolePruneFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture()

    def bind(self, binding=None):
        budget = PruneBudget.bind_existing_grant(binding or self.binding, price=PRICE, offline_accepted=True)
        self.addCleanup(budget.close)
        return budget

    def loop(self, budget, transport):
        adapter = create_model_adapter(self.config.adapter, Scratch(), "synthetic-not-a-credential")
        adapter._request_override = transport
        self.addAsyncCleanup(adapter.close)
        return PruneToolLoop(adapter, self.session, budget, max_rounds=2)

    async def test_count_at_local_cap_billed_one_above_settles_once_then_halts_before_tool(self):
        budget = self.bind()
        response = native_message([tool("view", "toolu_breach")])
        response["usage"]["input_tokens"] = 11
        transport = ScriptedTransport([response])  # Count result is exactly 10.
        loop = self.loop(budget, transport)
        with self.assertRaises(GovernedError) as caught:
            await loop.run("Synthetic system", "Synthetic question", "synthetic-local-cap-breach",
                           input_cap=10, output_cap=8, total_input_cap=10, total_output_cap=8, seconds=2)
        self.assertEqual(caught.exception.code, "provider_token_limit_breach")
        snapshot = budget.snapshot()
        actual_charge = PRICE.cost(11, 8)
        self.assertEqual(snapshot["spent_nusd"], CHARGE + actual_charge)
        self.assertEqual(snapshot["pending_nusd"], 0)
        self.assertEqual(snapshot["halted"], "provider_token_limit_breach")
        self.assertEqual(len(snapshot["rounds"]), 1)
        self.assertEqual(snapshot["rounds"][0]["charge_nusd"], actual_charge)
        self.assertEqual(snapshot["rounds"][0]["status"], "failed_known")
        self.assertEqual([row["action"] for row in self.session.log], ["start"])
        self.assertEqual([path for path, _ in transport.requests], ["/messages/count_tokens", "/messages"])
        with self.assertRaises(GovernedError):
            budget.reserve("synthetic-later", 1_000_000, 8)
        self.assertEqual(budget.snapshot()["spent_nusd"], CHARGE + actual_charge)

    async def test_console_remaining_above_local_hold_below_native_hold_declines_before_credentials_or_http(self):
        # Build a distinct synthetic pre-existing grant with a USD 1 ceiling.
        # Its original nonzero charge/attempt remain unchanged.
        policy = json.loads(self.binding.policy_json)
        policy["ceiling_nusd"] = 1_000_000_000
        policy_json = canonical(policy)
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            db.execute("UPDATE session SET policy=? WHERE id=1", (policy_json,))
        binding = replace(self.binding, policy_json=policy_json, ceiling_nusd=1_000_000_000)
        prepared = replace(self.prepare(), binding=binding)
        remaining = binding.ceiling_nusd - CHARGE
        reply = self.config.adapter.budgets["reply"]
        self.assertGreater(remaining, PRICE.cost(reply.input_tokens, reply.output_tokens))
        self.assertLess(remaining, PRICE.cost(1_000_000, reply.output_tokens))
        with patch.object(console_prune, "resolve_model_key") as key, \
                patch.object(console_prune, "create_model_adapter") as model, \
                patch("aiohttp.ClientSession") as client, self.assertRaises(GovernedError) as caught:
            await console_prune.execute_prune(prepared, self.config)
        self.assertEqual(caught.exception.code, "console_prune_budget_not_ready")
        key.assert_not_called()
        model.assert_not_called()
        client.assert_not_called()
        self.assertEqual(caught.exception.budget_snapshot["spent_nusd"], CHARGE)
        self.assertEqual(caught.exception.budget_snapshot["pending_nusd"], 0)
        with closing(sqlite3.connect(self.binding.ledger_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM attempt").fetchall(), [binding.completed_attempt])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prune_rounds").fetchone()[0], 0)

    async def test_unknown_generation_keeps_full_native_input_hold_and_reopening_stays_halted(self):
        budget = self.bind()
        response = native_message([tool("view", "toolu_unknown")])
        response.pop("usage")
        transport = ScriptedTransport([response])
        loop = self.loop(budget, transport)
        with self.assertRaises(GovernedError):
            await loop.run("Synthetic system", "Synthetic question", "synthetic-unknown-input-hold",
                           input_cap=10, output_cap=8, total_input_cap=10, total_output_cap=8, seconds=2)
        maximum = PRICE.cost(1_000_000, 8)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["spent_nusd"], CHARGE)
        self.assertEqual(snapshot["pending_nusd"], maximum)
        self.assertEqual(snapshot["rounds"][0]["maximum_nusd"], maximum)
        self.assertEqual(snapshot["rounds"][0]["status"], "unknown")
        self.assertTrue(snapshot["halted"])
        budget.close()
        reopened = self.bind()
        later = reopened.snapshot()
        self.assertEqual((later["spent_nusd"], later["pending_nusd"]), (CHARGE, maximum))
        self.assertEqual(later["halted"], snapshot["halted"])
        with self.assertRaises(GovernedError):
            reopened.reserve("synthetic-no-retry", 1_000_000, 8)
        self.assertEqual([path for path, _ in transport.requests], ["/messages/count_tokens", "/messages"])


if __name__ == "__main__":
    unittest.main()
