"""Decoded typed facts must remain safe for UTF-8 diagnostic persistence."""
import json
import unittest

from tests import test_path_hypotheses as native_fixture
from tests import test_active_path_hypotheses as runtime_fixture
from tests import test_active_requery as active_fixture


class DecodedTypedFactTests(unittest.TestCase):
    def test_unpaired_surrogates_in_all_accepted_string_fields_stay_unknown(self):
        for field in ("subject", "object", "relation", "time", "scope", "conditions"):
            for scalar in (chr(0xD800), chr(0xDFFF)):
                with self.subTest(field=field, scalar=ord(scalar)):
                    fixture = native_fixture.AutomaticPathTests("runTest")
                    fixture.setUp()
                    try:
                        value = native_fixture.fact()
                        value[field] = [scalar] if field == "conditions" else scalar
                        fixture.add([value])
                        result = fixture.run_diagnostic(fixture.frozen())
                        self.assertEqual(result["status"], "unknown")
                        self.assertTrue(result["candidates"])
                        self.assertTrue(all("invalid_typed_facts" in item["reasons"]
                                            for item in result["candidates"]))
                        json.dumps(result, ensure_ascii=False).encode("utf-8")
                    finally:
                        fixture.tearDown()

    def test_valid_json_surrogate_pair_and_normal_unicode_are_preserved(self):
        for relation in ("links", "links" + chr(0x1F600), "连接"):
            with self.subTest(relation=relation):
                fixture = native_fixture.AutomaticPathTests("runTest")
                fixture.setUp()
                try:
                    fixture.add([native_fixture.fact(relation=relation)])
                    result = fixture.run_diagnostic(fixture.frozen(), fixture.action(relation=relation))
                    self.assertEqual(result["status"], "pending_hypothesis")
                    self.assertEqual(result["candidates"][0]["edge_bindings"][0]["fact"]["relation"], relation)
                    json.dumps(result, ensure_ascii=False).encode("utf-8")
                finally:
                    fixture.tearDown()


class DecodedFactRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = runtime_fixture.ActivePathHypothesesConsoleTests("runTest")
        await self.fixture.asyncSetUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_escaped_surrogate_is_a_safe_unknown_and_turn_still_completes(self):
        f = self.fixture
        f.configure()
        # The frozen quote itself is valid ASCII; JSON decoding exposes the
        # lone surrogate that previously escaped into a UTF-8 scratch write.
        value = json.loads(runtime_fixture.fact_quote()[len("typed_facts_v1: "):])
        value["facts"][0]["relation"] = "links" + chr(0xD800)
        quote = "typed_facts_v1: " + json.dumps(value)
        self.assertIn("\\ud800", quote)
        f.add_source(quote)
        f.fixture.outputs = [runtime_fixture.demand_action(), active_fixture.done()]
        before = f.weights()
        await f.fixture.run_turn()
        bundle = f.fixture.event("requery_evidence")[0]["bundle"]
        event = f.diagnostic(bundle)
        self.assertEqual(event["receipt"]["status"], "unknown")
        self.assertIn("invalid_typed_facts", json.dumps(event["receipt"]))
        json.dumps(event["receipt"], ensure_ascii=False).encode("utf-8")
        self.assertEqual(f.stages(), ["query", "query", "reply"])
        self.assertEqual(f.fixture.saved_ledger()["phase"], "completed")
        self.assertEqual(f.weights(), before)
        f.assert_complete()

    async def test_normal_typed_fact_still_persists_and_completes(self):
        f = self.fixture
        f.configure()
        f.add_source(runtime_fixture.fact_quote())
        f.fixture.outputs = [runtime_fixture.demand_action(), active_fixture.done()]
        await f.fixture.run_turn()
        bundle = f.fixture.event("requery_evidence")[0]["bundle"]
        event = f.diagnostic(bundle)
        self.assertEqual(event["receipt"]["status"], "pending_hypothesis")
        self.assertEqual(f.stages(), ["query", "query", "reply"])
        f.assert_complete()
