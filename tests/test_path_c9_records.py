"""Cross-round source/origin tampering must fail after receipt hashes are renewed."""
from copy import deepcopy
import unittest

from indeces.requery_records import verify_active_turn
from indeces.run_records import digest
from tests import test_active_path_requery_c9 as native


class PathC9RecordTests(unittest.IsolatedAsyncioTestCase):
    def make_fixture(self):
        owner = native.ActivePathRequeryC9Tests("runTest")
        owner.addAsyncCleanup = self.addAsyncCleanup
        fixture = owner.make_fixture(consume_feedback=True)
        return owner, fixture

    @staticmethod
    def trace(fixture):
        trace_id = fixture.event("turn_start")[0]["trace_id"]
        return [(row["event"], deepcopy(row["fields"])) for row in fixture.entries()[1]
                if row["fields"].get("trace_id") == trace_id]

    @staticmethod
    def resign(fields):
        receipt = fields["result_receipt"]
        receipt["feedback_sha256"] = digest({k: v for k, v in receipt.items()
                                             if k != "feedback_sha256"})
        fields["result_receipt_sha256"] = digest(receipt)

    async def test_rehashed_origin_uid_version_and_fact_forgeries_fail_frozen_replay(self):
        owner, fixture = self.make_fixture()
        await owner.run_captured(fixture)
        owner.assert_strict_two_round_sources(fixture)
        for change in ("origin_round", "record_hash", "uid", "source_version", "polarity"):
            events = self.trace(fixture)
            post = next(f for e, f in events if e == "path_requery_feedback_result")
            receipt = post["result_receipt"]
            candidate = next(receipt["candidates"][i] for i in receipt["accumulated_candidate_indices"]
                             if receipt["candidates"][i]["path_nodes"] == ["alpha", "beta", "gamma"])
            field = "edge_bindings" if change == "polarity" else "edge_source_bindings"
            binding = next(row for row in candidate[field]
                           if row["source_record_id"] == fixture.source_ids["B"])
            if change == "origin_round":
                binding["round_index"] = 0
            elif change == "record_hash":
                binding["record_sha256"] = "0" * 64
            elif change == "uid":
                binding["evidence_uid"] = "0" * 64
            elif change == "source_version":
                binding["source_version"]["fingerprint"] = "0" * 64
            else:
                binding["fact"]["polarity"] = "negative"
            self.resign(post)
            with self.subTest(change=change), self.assertRaisesRegex(
                    ValueError, "frozen replay mismatch"):
                verify_active_turn(events, events[0][1])

    async def test_rehashed_accumulation_mode_and_scope_cannot_reinterpret_old_policy(self):
        owner, fixture = self.make_fixture()
        await owner.run_captured(fixture)
        for change in ("mode", "scope"):
            events = self.trace(fixture)
            post = next(f for e, f in events if e == "path_requery_feedback_result")
            receipt = post["result_receipt"]
            if change == "mode":
                receipt["limits"]["accumulate_rounds"] = False
            else:
                receipt["analysis_scope"] = "each_frozen_round"
            self.resign(post)
            with self.subTest(change=change), self.assertRaisesRegex(
                    ValueError, "accumulation policy mismatch"):
                verify_active_turn(events, events[0][1])
