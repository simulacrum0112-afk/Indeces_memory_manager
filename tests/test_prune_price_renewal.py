"""Same-tariff renewal tests use only synthetic owner-bound local ledgers."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from indeces import claude_prune_budget as module
from indeces.claude_prune_budget import PruneBudget, PrunePriceEnvelope
from indeces.contracts import GovernedError
from test_portable_claude_prune import CHARGE, known_round, legacy_fixture


class BeforeExpiry(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(1999, 1, 1, tzinfo=timezone.utc)


class PrunePriceRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-price-renewal-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.binding = legacy_fixture(self.directory)
        self.old = PrunePriceEnvelope(4000, 20000, "2000-01-01T00:00:00+00:00")
        self.new = replace(self.old, valid_until="2099-01-01T00:00:00+00:00")

    def create(self, *, terminal="completed"):
        with patch.object(module, "datetime", BeforeExpiry):
            budget = PruneBudget.bind_existing_grant(self.binding, price=self.old, offline_accepted=True)
            try:
                permit = budget.reserve("synthetic-renewal-turn", 100, 20)
                if terminal != "reserved" and terminal != "unknown":
                    known_round(budget, permit)
                    if terminal == "completed":
                        budget.observe(permit, "call_end", {"provider": "anthropic", "protocol": "messages",
                            "base_url": module.BASE_URL, "status": "completed"})
                        budget.complete(permit)
                if terminal == "unknown":
                    budget.fail(permit, GovernedError("synthetic_unknown"))
                if terminal == "halted":
                    budget.halt("synthetic_sticky_halt")
            finally:
                budget.close()

    def data(self):
        with closing(sqlite3.connect(self.binding.ledger_path)) as db:
            names = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            tables = {r[1]: db.execute("SELECT * FROM " + r[1] + " ORDER BY rowid").fetchall()
                      for r in names if r[0] == "table"}
        return names, tables

    def renew(self, **kwargs):
        return PruneBudget.renew_existing_price(kwargs.pop("binding", self.binding),
            kwargs.pop("previous_price", self.old), kwargs.pop("new_price", self.new),
            offline_accepted=kwargs.pop("offline_accepted", True), **kwargs)

    def test_expired_snapshot_renewal_changes_only_expiry_and_reopens_same_balance(self):
        self.create()
        before = self.data()
        metadata = self.binding.ledger_path.stat()
        receipt = self.renew()
        after = self.data()
        self.assertEqual(before[0], after[0])
        for name in ("session", "attempt", "prune_rounds"):
            self.assertEqual(before[1][name], after[1][name])
        old_meta, new_meta = before[1]["prune_meta"][0], after[1]["prune_meta"][0]
        self.assertEqual(old_meta[:1] + old_meta[2:], new_meta[:1] + new_meta[2:])
        expected = json.loads(old_meta[1])
        expected["price_expiry"] = self.new.valid_until
        self.assertEqual(json.loads(new_meta[1]), expected)
        self.assertEqual(receipt["old_policy_sha256"], hashlib.sha256(old_meta[1].encode()).hexdigest())
        self.assertEqual(receipt["new_policy_sha256"], hashlib.sha256(new_meta[1].encode()).hexdigest())
        self.assertEqual((receipt["spent_nusd"], receipt["pending_nusd"], receipt["round_count"]), (2 * CHARGE, 0, 1))
        current = self.binding.ledger_path.stat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), (current.st_dev, current.st_ino))
        reopened = PruneBudget.bind_existing_grant(self.binding, price=self.new, offline_accepted=True)
        try:
            snapshot = reopened.snapshot()
            self.assertEqual(snapshot["spent_nusd"], 2 * CHARGE)
            self.assertEqual(snapshot["remaining_nusd"], self.binding.ceiling_nusd - 2 * CHARGE)
        finally:
            reopened.close()

    def test_rate_changes_do_not_reprice_or_mutate_history(self):
        self.create()
        before = self.data()
        for price in (replace(self.new, input_nusd_per_token=4001), replace(self.new, output_nusd_per_token=19999)):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.renew(new_price=price)
        self.assertEqual(before, self.data())

    def test_nonfuture_or_nonadvancing_expiry_rejected(self):
        self.create()
        before = self.data()
        for expiry in (self.old.valid_until, "1998-01-01T00:00:00+00:00", "2001-01-01T00:00:00+00:00"):
            with self.subTest(expiry=expiry), self.assertRaises(ValueError):
                self.renew(new_price=replace(self.old, valid_until=expiry))
        self.assertEqual(before, self.data())

    def test_unreviewed_gate_and_non_envelope_are_refused(self):
        self.create()
        before = self.data()
        for kwargs in ({"offline_accepted": False}, {"new_price": object()}, {"binding": object()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.renew(**kwargs)
        self.assertEqual(before, self.data())

    def test_changed_model_or_endpoint_refused_before_mutation(self):
        self.create()
        before = self.data()
        for field, value in (("model", "other-model"), ("base_url", "https://example.invalid/v1")):
            price = replace(self.new)
            object.__setattr__(price, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.renew(new_price=price)
        self.assertEqual(before, self.data())

    def test_wrong_previous_snapshot_policy_is_refused(self):
        self.create()
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew(previous_price=replace(self.old, valid_until="2001-01-01T00:00:00+00:00"))
        self.assertEqual(before, self.data())

    def test_pending_reservation_is_not_released_or_halted_by_renewal(self):
        self.create(terminal="reserved")
        before = self.data()
        with self.assertRaises(GovernedError) as caught:
            self.renew()
        self.assertEqual(caught.exception.code, "prune_budget_renewal_requires_settled_unhalted")
        self.assertEqual(before, self.data())
        self.assertIsNone(before[1]["prune_meta"][0][2])
        self.assertIsNone(before[1]["prune_rounds"][0][10])

    def test_sticky_halt_and_unknown_hold_survive_failed_renewal(self):
        self.create(terminal="unknown")
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew()
        self.assertEqual(before, self.data())
        self.assertEqual(before[1]["prune_meta"][0][2], "synthetic_unknown")

    def test_known_but_unfinished_round_cannot_resume_through_renewal(self):
        self.create(terminal="known")
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew()
        self.assertEqual(before, self.data())

    def test_known_sticky_halt_is_not_cleared(self):
        self.create(terminal="halted")
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew()
        self.assertEqual(before, self.data())
        self.assertEqual(before[1]["prune_meta"][0][2], "synthetic_sticky_halt")

    def test_changed_receipt_and_cloned_file_cannot_renew(self):
        self.create()
        before = self.data()
        cloned = self.directory / "clone.sqlite3"
        shutil.copyfile(self.binding.ledger_path, cloned)
        with self.assertRaises(ValueError):
            self.renew(binding=replace(self.binding, ledger_path=cloned))
        self.binding.receipt_path.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            self.renew()
        self.assertEqual(before, self.data())

    def test_unmigrated_original_is_not_created_or_reset(self):
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew()
        self.assertEqual(before, self.data())
        self.assertEqual(set(before[1]), {"session", "attempt"})

    def test_corrupted_balance_is_not_reconciled_by_renewal(self):
        self.create()
        with closing(sqlite3.connect(self.binding.ledger_path)) as db, db:
            db.execute("UPDATE session SET spent=spent+1")
        before = self.data()
        with self.assertRaises(GovernedError):
            self.renew()
        self.assertEqual(before, self.data())

    def test_active_owner_lease_prevents_renewal(self):
        self.create()
        before = self.data()
        with patch.object(module, "datetime", BeforeExpiry):
            active = PruneBudget.bind_existing_grant(self.binding, price=self.old, offline_accepted=True)
        try:
            with self.assertRaises(GovernedError) as caught:
                self.renew()
            self.assertEqual(caught.exception.code, "requery_ledger_writer_busy")
            self.assertEqual(before, self.data())
        finally:
            active.close()
