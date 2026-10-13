"""Separate calibration grant tests: synthetic receipts and local SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from indeces import claude_prune_budget as module
from indeces.claude_prune_budget import CampaignGrantBinding, PruneBudget, PrunePriceEnvelope
from indeces.contracts import GovernedError
from test_portable_claude_prune import CHARGE, known_round, legacy_fixture


PRICE = PrunePriceEnvelope(4000, 20000, "2099-01-01T00:00:00+00:00")


class CampaignBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-campaign-budget-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.binding = self.make_binding()

    def make_binding(self, *, ledger=None, **overrides):
        ledger = ledger or self.directory / "campaign.sqlite3"
        receipt_path = self.directory / "campaign-authorization.json"
        receipt = {"schema": "native_prune_campaign_grant_v1", "authorized": True,
                   "authorization_id": "synthetic-authorization-source-sha256",
                   "grant_id": "calibration_campaign:synthetic", "ledger_path": str(ledger.resolve()),
                   "ceiling_nusd": module.CAMPAIGN_CEILING, "model": module.MODEL,
                   "base_url": module.BASE_URL, "purpose": "calibration"}
        receipt.update(overrides)
        raw = module._json(receipt).encode("utf-8")
        receipt_path.write_bytes(raw)
        info = receipt_path.stat()
        return CampaignGrantBinding(ledger.resolve(), "calibration_campaign:synthetic",
                                    receipt_path, hashlib.sha256(raw).hexdigest(),
                                    (info.st_dev, info.st_ino), "synthetic-authorization-source-sha256")

    def bind(self, binding=None, price=PRICE):
        budget = PruneBudget.bind_campaign(binding or self.binding, price=price, offline_accepted=True)
        self.addCleanup(budget.close)
        return budget

    def complete(self, budget, ident="synthetic-known"):
        permit = budget.reserve(ident, 100, 20)
        known_round(budget, permit)
        budget.observe(permit, "call_end", {"provider": "anthropic", "protocol": "messages",
                       "base_url": module.BASE_URL, "status": "completed"})
        budget.complete(permit)
        return permit

    def test_explicit_separate_fifty_dollar_ceiling_and_exact_type(self):
        budget = self.bind()
        snapshot = budget.snapshot()
        self.assertIs(type(budget), PruneBudget)
        self.assertTrue(budget.campaign_grant_bound)
        self.assertFalse(budget.existing_grant_bound)
        self.assertEqual(budget.bound_path, budget.path)
        self.assertEqual(snapshot["ceiling_nusd"], 50_000_000_000)
        self.assertTrue(snapshot["campaign_grant_bound"])
        self.assertEqual(snapshot["spent_nusd"], 0)
        self.assertEqual(snapshot["pending_nusd"], 0)
        with closing(sqlite3.connect(budget.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"session", "prune_meta", "prune_rounds"})

    def test_existing_five_dollar_fixture_never_expands(self):
        legacy = legacy_fixture(self.directory)
        with self.assertRaises(ValueError):
            replace(legacy, ceiling_nusd=50_000_000_000)
        with self.assertRaises(ValueError):
            PruneBudget(self.directory / "offline.sqlite3", "offline_fixture:test",
                        ceiling_nusd=50_000_000_000, price=PRICE)
        budget = PruneBudget.bind_existing_grant(legacy, price=PRICE, offline_accepted=True)
        self.addCleanup(budget.close)
        self.assertTrue(budget.existing_grant_bound)
        self.assertFalse(budget.campaign_grant_bound)
        self.assertEqual(budget.snapshot()["ceiling_nusd"], module.CEILING)

    def test_foreign_original_ledger_rejected_without_byte_mutation(self):
        legacy = legacy_fixture(self.directory)
        before = legacy.ledger_path.read_bytes()
        binding = self.make_binding(ledger=legacy.ledger_path)
        with self.assertRaises(GovernedError):
            self.bind(binding)
        self.assertEqual(legacy.ledger_path.read_bytes(), before)

    def test_foreign_offline_ledger_and_preexisting_empty_file_rejected(self):
        path = self.binding.ledger_path
        path.write_bytes(b"")
        with self.assertRaises(GovernedError):
            self.bind()
        self.assertEqual(path.read_bytes(), b"")
        path.unlink()
        offline = PruneBudget(path, "offline_fixture:foreign", price=PRICE)
        offline.close()
        before = path.read_bytes()
        with self.assertRaises(GovernedError):
            self.bind()
        self.assertEqual(path.read_bytes(), before)

    def test_authentication_gate_and_subclass_are_rejected(self):
        with self.assertRaises(ValueError):
            PruneBudget.bind_campaign(self.binding, price=PRICE)
        class Derived(PruneBudget):
            pass
        with self.assertRaises(ValueError):
            Derived.bind_campaign(self.binding, price=PRICE, offline_accepted=True)
        self.assertFalse(self.binding.ledger_path.exists())

    def test_binding_is_immutable_and_exact_cap_required(self):
        with self.assertRaises(FrozenInstanceError):
            self.binding.ceiling_nusd = 100_000_000_000
        for ceiling in (module.CEILING, 0, 51_000_000_000, True):
            with self.assertRaises(ValueError):
                replace(self.binding, ceiling_nusd=ceiling)

    def test_rejected_receipt_schema_model_provider_purpose_and_cap(self):
        variants = [{"schema": "wrong"}, {"model": "gpt-6.1-sol"},
                    {"base_url": "https://example.invalid"}, {"authorized": False},
                    {"purpose": "production"}, {"ceiling_nusd": module.CEILING},
                    {"extra": "not accepted"}]
        for variant in variants:
            with self.subTest(variant=variant):
                binding = self.make_binding(**variant)
                with self.assertRaises(ValueError):
                    self.bind(binding)
                self.assertFalse(binding.ledger_path.exists())

    def test_expired_price_rejected_before_creation(self):
        with self.assertRaises(ValueError):
            self.bind(price=replace(PRICE, valid_until="2000-01-01T00:00:00+00:00"))
        self.assertFalse(self.binding.ledger_path.exists())

    def test_reopen_retains_paid_history(self):
        budget = self.bind()
        self.complete(budget)
        before = budget.snapshot()
        budget.close()
        reopened = self.bind()
        self.assertEqual(reopened.snapshot(), before)
        self.assertEqual(before["spent_nusd"], CHARGE)

    def test_deleted_campaign_cannot_be_recreated_by_same_grant(self):
        budget = self.bind()
        self.complete(budget)
        budget.close()
        budget.path.unlink()
        with self.assertRaises(GovernedError):
            self.bind()
        self.assertFalse(budget.path.exists())

    def test_cloned_campaign_file_cannot_replace_original_identity(self):
        budget = self.bind()
        self.complete(budget)
        budget.close()
        clone = budget.path.with_name("clone.sqlite3")
        clone.write_bytes(budget.path.read_bytes())
        os.replace(clone, budget.path)
        with self.assertRaises(GovernedError):
            self.bind()

    def test_campaign_identity_marker_drift_closes_admission(self):
        budget = self.bind()
        marker = module._campaign_marker_path(self.binding)
        marker.write_bytes(b"{}")
        with self.assertRaises(GovernedError):
            budget.reserve("identity-drift", 100, 20)
        with self.assertRaises(GovernedError):
            budget.snapshot()

    def test_unknown_hold_halt_and_reopen_remain_sticky(self):
        budget = self.bind()
        permit = budget.reserve("unknown-generation", 1_000_000, 8192)
        maximum = PRICE.cost(1_000_000, 8192)
        budget.fail(permit, GovernedError("synthetic_unknown", remote_usage_unknown=True))
        before = budget.snapshot()
        self.assertEqual(before["pending_nusd"], maximum)
        self.assertEqual(before["halted"], "synthetic_unknown")
        budget.close()
        reopened = self.bind()
        self.assertEqual(reopened.snapshot(), before)
        with self.assertRaises(GovernedError):
            reopened.reserve("later-question", 100, 20)
        self.assertEqual(reopened.snapshot()["pending_nusd"], maximum)

    def test_unfinished_reserved_reopen_halts_without_release(self):
        budget = self.bind()
        budget.reserve("crash-pending", 100, 20)
        budget.close()
        reopened = self.bind()
        self.assertEqual(reopened.snapshot()["halted"], "recovery_pending")
        self.assertEqual(reopened.snapshot()["pending_nusd"], PRICE.cost(100, 20))

    def test_concurrent_worst_case_reservations_never_exceed_ceiling(self):
        budget = self.bind()
        def reserve(index):
            try:
                return budget.reserve("parallel-" + str(index), 1_000_000, 32_000)
            except GovernedError:
                return None
        with ThreadPoolExecutor(max_workers=11) as executor:
            permits = list(executor.map(reserve, range(11)))
        snapshot = budget.snapshot()
        self.assertEqual(sum(permit is not None for permit in permits), 10)
        self.assertEqual(snapshot["pending_nusd"], 46_400_000_000)
        self.assertLessEqual(snapshot["spent_nusd"] + snapshot["pending_nusd"], module.CAMPAIGN_CEILING)

    def test_settled_turn_fuses_stop_only_the_turn(self):
        budget = self.bind()
        self.complete(budget)
        for code in ("prune_turn_token_limit", "prune_turn_time_limit", "prune_turn_call_limit"):
            budget.halt(code)
            self.assertIsNone(budget.snapshot()["halted"])
        self.complete(budget, "next-question")
        self.assertEqual(budget.snapshot()["spent_nusd"], CHARGE * 2)

    def test_known_failed_turn_fuse_keeps_charge_without_global_halt(self):
        budget = self.bind()
        permit = budget.reserve("known-fuse", 100, 20)
        known_round(budget, permit)
        budget.fail(permit, GovernedError("prune_turn_token_limit"))
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["spent_nusd"], CHARGE)
        self.assertEqual(snapshot["rounds"][0]["status"], "failed_known")
        self.assertIsNone(snapshot["halted"])

    def test_fuse_does_not_release_unknown_or_clear_previous_halt(self):
        budget = self.bind()
        budget.halt("provider_failure")
        budget.halt("prune_turn_token_limit")
        self.assertEqual(budget.snapshot()["halted"], "provider_failure")
        budget.close()
        other = self.directory / "other"
        other.mkdir()
        self.directory = other
        binding = self.make_binding()
        unknown = self.bind(binding)
        permit = unknown.reserve("unknown-fuse", 100, 20)
        unknown.fail(permit, GovernedError("prune_turn_time_limit", remote_usage_unknown=True))
        self.assertEqual(unknown.snapshot()["halted"], "prune_turn_time_limit")
        self.assertEqual(unknown.snapshot()["pending_nusd"], PRICE.cost(100, 20))

    def test_count_only_failure_remains_unknown_and_fully_held(self):
        budget = self.bind()
        permit = budget.reserve("count-only", 100, 20)
        budget.fail(permit, GovernedError("prune_turn_time_limit"))
        self.assertEqual(budget.snapshot()["rounds"][0]["status"], "unknown")
        self.assertEqual(budget.snapshot()["pending_nusd"], PRICE.cost(100, 20))
        self.assertIsNotNone(budget.snapshot()["halted"])

    def test_receipt_drift_is_checked_on_every_transaction(self):
        budget = self.bind()
        self.binding.receipt_path.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            budget.reserve("receipt-drift", 100, 20)
        with self.assertRaises(ValueError):
            budget.snapshot()
        with closing(sqlite3.connect(budget.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prune_rounds").fetchone()[0], 0)
            self.assertIsNotNone(db.execute("SELECT halted FROM prune_meta").fetchone()[0])

    def test_policy_tampering_and_schema_trigger_do_not_admit(self):
        budget = self.bind()
        budget.close()
        with closing(sqlite3.connect(budget.path)) as db, db:
            db.execute("CREATE TRIGGER forged AFTER INSERT ON prune_rounds BEGIN UPDATE session SET spent=0; END")
        with self.assertRaises(GovernedError):
            self.bind()


if __name__ == "__main__":
    unittest.main()
