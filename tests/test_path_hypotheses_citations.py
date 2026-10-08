"""Reject coordinated global citation tampering at the independent path API."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from indeces.path_hypotheses import analyze_path_hypotheses
from indeces.requery_records import append_retrieval, validate_bundle
from tests import test_path_hypotheses as helpers


class IndependentPathCitationTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing native fixture; do not inherit its test cases.
        self.fixture = helpers.AutomaticPathTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("offline test forbids network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.http = patch("aiohttp.ClientSession._request", side_effect=AssertionError("offline test forbids HTTP"))
        self.http.start()
        self.addCleanup(self.http.stop)

        self.fixture.add()
        first = self.fixture.frozen("alpha")
        self.fixture.add(raw=helpers.quote([helpers.fact()]) + "\nFresh second synthetic source.")
        second = self.fixture.frozen("beta")
        self.bundle = append_retrieval(None, first, round_index=0,
            request_id="synthetic-initial", planning_call_id=None)
        self.bundle = append_retrieval(self.bundle, second, round_index=1,
            request_id=second["event_id"], planning_call_id="synthetic-plan")
        self.assertEqual([item["citation_id"] for item in self.bundle["materials"]], ["M1", "M2"])
        self.assertEqual(self.bundle["materials"][1]["round_index"], 1)

    def diagnostic(self, bundle):
        return analyze_path_hypotheses(bundle,
            [self.fixture.wrapper(index=1)], config=self.fixture.cfg)

    def rewrite_last_global_citation(self, citation):
        changed = deepcopy(self.bundle)
        target = changed["materials"][-1]
        target["citation_id"] = citation
        for round_ in changed["rounds"]:
            for binding in round_["bindings"]:
                if binding["evidence_uid"] == target["evidence_uid"]:
                    binding["citation_id"] = citation
        # This targets the coordinated equality-check bypass: the global ID
        # and every same-UID binding agree, with native receipts untouched.
        self.assertEqual(changed["rounds"][0], self.bundle["rounds"][0])
        self.assertEqual(changed["rounds"][1]["record"], self.bundle["rounds"][1]["record"])
        return changed

    def assert_invalid_citation(self, citation):
        changed = self.rewrite_last_global_citation(citation)
        with self.assertRaises(ValueError):
            validate_bundle(changed)
        before = deepcopy(changed)
        changes = self.fixture.store.db.total_changes
        result = self.diagnostic(changed)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["proof"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(changed, before)
        self.assertEqual(self.fixture.store.db.total_changes, changes)

    def test_normal_native_multiround_bundle_remains_pending_with_unique_ids(self):
        validate_bundle(self.bundle)
        before = deepcopy(self.bundle)
        changes = self.fixture.store.db.total_changes
        result = self.diagnostic(self.bundle)
        self.assertEqual(result["status"], "pending_hypothesis")
        bindings = result["candidates"][0]["edge_bindings"]
        self.assertEqual({item["citation_id"] for item in bindings}, {"M1", "M2"})
        self.assertEqual({item["evidence_uid"] for item in bindings},
            {item["evidence_uid"] for item in self.bundle["materials"]})
        self.assertFalse(result["proof"])
        self.assertEqual(self.bundle, before)
        self.assertEqual(self.fixture.store.db.total_changes, changes)

    def test_coordinated_numbering_gap_is_unknown(self):
        self.assert_invalid_citation("M9")

    def test_coordinated_duplicate_global_citation_is_unknown(self):
        self.assert_invalid_citation("M1")

    def test_coordinated_malformed_numbering_is_unknown(self):
        for citation in ("M02", "M0", "citation-2", 2):
            with self.subTest(citation=citation):
                self.assert_invalid_citation(citation)


if __name__ == "__main__":
    unittest.main()
