"""Explicit administrative reviews use synthetic owner-bound ledgers only."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from indeces import claude_prune_budget as module
from indeces.claude_prune_budget import PruneBudget
from indeces.contracts import GovernedError
from test_portable_claude_prune import CHARGE, PRICE, known_round, legacy_fixture


class KnownFailureReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-known-review-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.binding = legacy_fixture(self.directory)
        self.review_sha = hashlib.sha256(b"synthetic operator approval").hexdigest()

    def create(self, *, terminal="failed_known", halt="incomplete_response"):
        budget = PruneBudget.bind_existing_grant(self.binding, price=PRICE, offline_accepted=True)
        try:
            permit = budget.reserve("synthetic-completed", 100, 20)
            known_round(budget, permit)
            budget.observe(permit, "call_end", {"provider": "anthropic", "protocol": "messages",
                "base_url": module.BASE_URL, "status": "completed"})
            budget.complete(permit)
            permit = budget.reserve("synthetic-reviewed-plan", 100, 20)
            if terminal in {"failed_known", "known", "completed"}:
                known_round(budget, permit)
            if terminal == "completed":
                budget.observe(permit, "call_end", {"provider": "anthropic", "protocol": "messages",
                    "base_url": module.BASE_URL, "status": "completed"})
                budget.complete(permit)
                budget.halt(halt)
            elif terminal in {"failed_known", "unknown"}:
                budget.fail(permit, GovernedError(halt))
            else:
                budget.halt(halt)
        finally:
            budget.close()

    def data(self):
        with closing(sqlite3.connect(self.binding.ledger_path)) as db:
            schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            return schema, {r[1]: db.execute("SELECT * FROM " + r[1] + " ORDER BY rowid").fetchall()
                            for r in schema if r[0] == "table"}

    def review(self, **kwargs):
        return PruneBudget.review_resume_known_failure(kwargs.pop("binding", self.binding),
            kwargs.pop("price", PRICE), expected_halt=kwargs.pop("expected_halt", "incomplete_response"),
            expected_spent_nusd=kwargs.pop("expected_spent_nusd", 3 * CHARGE),
            expected_round_id=kwargs.pop("expected_round_id", "synthetic-reviewed-plan"),
            review_receipt_sha256=kwargs.pop("review_receipt_sha256", self.review_sha),
            operator_approved=kwargs.pop("operator_approved", True), **kwargs)

    def refused_unchanged(self, **kwargs):
        before = self.data()
        with self.assertRaises((ValueError, GovernedError)):
            self.review(**kwargs)
        self.assertEqual(before, self.data())

    def test_approved_review_changes_only_halt_and_keeps_all_charged_history(self):
        self.create()
        before = self.data()
        identity = self.binding.ledger_path.stat()
        receipt = self.review()
        after = self.data()
        self.assertEqual(before[0], after[0])
        for name in ("session", "attempt", "prune_rounds"):
            self.assertEqual(before[1][name], after[1][name])
        row = before[1]["prune_meta"][0]
        self.assertEqual(after[1]["prune_meta"], [(row[0], row[1], None, row[3])])
        current = self.binding.ledger_path.stat()
        self.assertEqual((identity.st_dev, identity.st_ino), (current.st_dev, current.st_ino))
        self.assertEqual((receipt["spent_nusd"], receipt["pending_nusd"], receipt["round_count"]), (3 * CHARGE, 0, 2))
        self.assertEqual(receipt["review_receipt_sha256"], self.review_sha)
        self.assertNotEqual(receipt["before_state_sha256"], receipt["after_state_sha256"])
        json.dumps(receipt, allow_nan=False)
        reopened = PruneBudget.bind_existing_grant(self.binding, price=PRICE, offline_accepted=True)
        try:
            snapshot = reopened.snapshot()
            self.assertEqual(snapshot["spent_nusd"], 3 * CHARGE)
            self.assertIsNone(snapshot["halted"])
            self.assertEqual(snapshot["rounds"][-1]["status"], "failed_known")
            reopened.reserve("new-independent-user-turn", 100, 20)
        finally:
            reopened.close()

    def test_approval_defaults_to_false_and_requires_literal_true(self):
        self.create()
        self.refused_unchanged(operator_approved=False)
        self.refused_unchanged(operator_approved=1)
        before = self.data()
        with self.assertRaises(ValueError):
            PruneBudget.review_resume_known_failure(self.binding, PRICE,
                expected_halt="incomplete_response", expected_spent_nusd=3 * CHARGE,
                expected_round_id="synthetic-reviewed-plan", review_receipt_sha256=self.review_sha)
        self.assertEqual(before, self.data())

    def test_exact_state_and_review_identity_are_required(self):
        self.create()
        for kwargs in ({"expected_spent_nusd": 3 * CHARGE + 1}, {"expected_spent_nusd": True},
                       {"expected_round_id": "synthetic-completed"}, {"expected_halt": "round_failure"},
                       {"review_receipt_sha256": "bad"}, {"review_receipt_sha256": "G" * 64}):
            with self.subTest(kwargs=kwargs):
                self.refused_unchanged(**kwargs)

    def test_unknown_usage_hold_is_never_released(self):
        self.create(terminal="unknown")
        self.refused_unchanged(expected_spent_nusd=2 * CHARGE)
        self.assertEqual(self.data()[1]["prune_rounds"][-1][5], "unknown")

    def test_pending_reservation_is_never_released(self):
        self.create(terminal="reserved")
        self.refused_unchanged(expected_spent_nusd=2 * CHARGE)

    def test_known_unfinished_round_and_completed_latest_round_are_refused(self):
        for terminal in ("known", "completed"):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as folder:
                old_binding = self.binding
                self.binding = legacy_fixture(Path(folder))
                try:
                    self.create(terminal=terminal)
                    self.refused_unchanged()
                finally:
                    self.binding = old_binding

    def test_other_halt_is_sticky(self):
        self.create(halt="delivery_failed")
        self.refused_unchanged()
        self.assertEqual(self.data()[1]["prune_meta"][0][2], "delivery_failed")

    def test_replay_cannot_clear_or_recharge_anything(self):
        self.create()
        self.review()
        self.refused_unchanged()

    def test_expired_or_changed_price_is_refused(self):
        self.create()
        self.refused_unchanged(price=replace(PRICE, valid_until="2000-01-01T00:00:00+00:00"))
        self.refused_unchanged(price=replace(PRICE, input_nusd_per_token=4001))
        self.refused_unchanged(price=replace(PRICE, valid_until="2098-01-01T00:00:00+00:00"))

    def test_changed_receipt_and_clone_are_refused(self):
        self.create()
        clone = self.directory / "clone.sqlite3"
        shutil.copyfile(self.binding.ledger_path, clone)
        self.refused_unchanged(binding=replace(self.binding, ledger_path=clone))
        self.binding.receipt_path.write_bytes(b"{}")
        self.refused_unchanged()

    def test_original_unmigrated_ledger_cannot_be_created_or_reset(self):
        self.refused_unchanged()
        self.assertEqual(set(self.data()[1]), {"session", "attempt"})

    def test_corrupt_balance_or_usage_is_not_reconciled(self):
        self.create()
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            db.execute("UPDATE session SET spent=spent+1")
        self.refused_unchanged()

    def test_trigger_schema_cannot_mutate_history_during_review(self):
        self.create()
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            db.execute("CREATE TRIGGER corrupt_review AFTER UPDATE ON prune_meta BEGIN UPDATE session SET spent=0; END")
        self.refused_unchanged()

    def test_active_owner_lease_blocks_review(self):
        self.create()
        active = PruneBudget.bind_existing_grant(self.binding, price=PRICE, offline_accepted=True)
        try:
            self.refused_unchanged()
        finally:
            active.close()
