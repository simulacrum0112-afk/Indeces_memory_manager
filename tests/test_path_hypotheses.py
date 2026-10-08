"""Automatic paths on newly written synthetic native frozen inputs only."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.active_requery import parse_action
from indeces.memory import MemoryGraph
from indeces.path_hypotheses import PathHypothesesConfig, analyze_path_hypotheses
from indeces.requery_records import append_retrieval
from indeces.run_records import digest
from indeces.run_records import freeze_retrieval
from indeces.store import Store


def fact(a="alpha", b="beta", relation="links", polarity="positive", **qualifiers):
    return dict(subject=a, object=b, relation=relation, polarity=polarity, **qualifiers)


def quote(facts):
    return "typed_facts_v1: " + json.dumps({"facts": facts}, separators=(",", ":"))


class AutomaticPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = Store(Path(self.temp.name).resolve() / "state")
        self.graph = MemoryGraph(self.store.db)
        self.cfg = PathHypothesesConfig(enabled=True, max_seconds=3)
        self.sequence = 0
        self.scope = "synthetic:knowledge"
        self.network = patch("socket.create_connection", side_effect=AssertionError("offline test forbids network"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.store.db.close()
        self.temp.cleanup()

    def add(self, facts=None, marks=None, raw=None):
        self.sequence += 1
        text = raw if raw is not None else quote(facts or [fact()])
        return self.graph.add(self.scope, f"synthetic:{self.sequence}", "synthetic-author", [
            {"text": text, "quote": text, "marks": marks or ["alpha", "beta"]}], 1.0)[0]

    def frozen(self, query="alpha beta", mode="static"):
        self.sequence += 1
        event = f"synthetic-retrieval-{self.sequence}"
        audit = {}
        selected = self.graph.retrieve(self.scope, [], query, 2.0 + self.sequence,
                                      event_id=event, ranking_mode=mode, audit=audit)
        return freeze_retrieval(self.store.db, self.scope, event, query, selected, audit)

    def action(self, a="alpha", b="beta", relation="links", polarity="positive", direction="subject_to_object",
               time=None, scope=None, negation=False):
        original = f"{a} {relation} {b}"
        anchors = {}
        for key, value in (("time", time), ("scope", scope), ("negation", "not" if negation else None)):
            if value is not None:
                start = len(original) + 1
                original += " " + value
                anchors[key] = {"surface": value, "span": [start, len(original)]}
        action = {"action": "query", "query": {
            "entities": [{"id": "e1", "surface": a, "canonical": a, "span": [0, len(a)]},
                         {"id": "e2", "surface": b, "canonical": b,
                          "span": [len(a)+len(relation)+2, len(a)+len(relation)+2+len(b)]}],
            "relations": [{"subject_id": "e1", "object_id": "e2", "predicate": relation,
                "surface": relation, "span": [len(a)+1, len(a)+1+len(relation)],
                "negated": polarity == "negative", "direction": direction}],
            "negations": [anchors["negation"]] if negation else [],
            "time": anchors.get("time"), "scope": anchors.get("scope"),
            "canonical_terms": [a, b], "ambiguities": []}, "missing_evidence": ["Synthetic missing relation"],
            "clarification": "", "stop_reason": ""}
        return parse_action(json.dumps(action), original)

    def wrapper(self, action=None, index=0, call="synthetic-plan"):
        action = action or self.action()
        return {"evidence_round_index": index, "planning_call_id": call, "action": action,
                "action_sha256": digest(action), "anchors_validated": True}

    def run_diagnostic(self, record, action=None, cfg=None):
        return analyze_path_hypotheses(record, [self.wrapper(action)], config=cfg or self.cfg)

    def test_default_off_does_not_iterate_inputs(self):
        class Untouchable:
            def __iter__(self):
                raise AssertionError("disabled inputs accessed")
        result = analyze_path_hypotheses(Untouchable(), Untouchable())
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["stats"]["expansions"], 0)

    def test_automatic_native_positive_binds_source_span_hash_uid_and_citation(self):
        self.add()
        record = self.frozen()
        before, db_changes = deepcopy(record), self.store.db.total_changes
        result = self.run_diagnostic(record)
        self.assertEqual(result["status"], "pending_hypothesis")
        self.assertEqual(record, before)
        self.assertEqual(self.store.db.total_changes, db_changes)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["path_nodes"], ["alpha", "beta"])
        self.assertTrue(result["meetings"][0]["automatic"])
        self.assertEqual(candidate["planning_call_id"], "synthetic-plan")
        self.assertEqual(candidate["query_call_binding"], "declared_upstream_query_call")
        binding = candidate["edge_bindings"][0]
        material = record["materials"][0]
        start, end = binding["fact_line_span"]
        self.assertEqual(binding["fact_line_sha256"], hashlib.sha256(material["quote"][start:end].encode()).hexdigest())
        self.assertEqual(binding["quote_sha256"], hashlib.sha256(material["quote"].encode()).hexdigest())
        self.assertEqual(binding["source_version"]["kind"], "record_content_version")
        self.assertEqual(binding["citation_id"], "M1")
        self.assertFalse(result["proof"])
        self.assertFalse(result["proposals_verified"])

    def test_actual_two_round_bundle_preserves_same_round_ids_and_global_uid(self):
        self.add()
        first, second = self.frozen("alpha"), self.frozen("beta")
        bundle = append_retrieval(None, first, round_index=0, request_id="synthetic-initial", planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        result = analyze_path_hypotheses(bundle, [self.wrapper(index=1)], config=self.cfg)
        self.assertEqual(result["status"], "pending_hypothesis")
        binding = result["candidates"][0]["edge_bindings"][0]
        self.assertEqual(binding["evidence_uid"], bundle["materials"][0]["evidence_uid"])
        self.assertEqual(binding["record_sha256"], bundle["rounds"][1]["record_sha256"])
        self.assertEqual(result["candidates"][0]["round_index"], 1)

    def test_plain_source_has_body_bindings_but_no_typed_semantics(self):
        self.add(raw="Fresh alpha beta cooccurrence without typed claims.")
        result = self.run_diagnostic(self.frozen())
        self.assertEqual(result["status"], "unknown")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["edge_bindings"], [])
        self.assertEqual(candidate["edge_source_bindings"][0]["parse_status"], "no_typed_facts")
        self.assertEqual(candidate["edge_support"][0]["unresolved_support_ids"], [])

    def test_unfrozen_support_is_located_and_later_round_cannot_supply_it(self):
        missing = self.add(raw="Unselected original alpha beta support.", marks=["alpha", "beta", "original"])
        for index in range(4):
            self.add(raw=quote([fact()])+f"\nFresh unique selected line {index}.")
        first = self.frozen()
        self.assertNotIn(missing["id"], [item["record_id"] for item in first["materials"]])
        second = self.frozen("original")
        bundle = append_retrieval(None, first, round_index=0, request_id="initial", planning_call_id=None)
        bundle = append_retrieval(bundle, first, round_index=1, request_id=first["event_id"], planning_call_id="early-plan")
        bundle = append_retrieval(bundle, second, round_index=2, request_id=second["event_id"], planning_call_id="later-plan")
        self.assertIn(missing["id"], [item["material"]["record_id"] for item in bundle["materials"]])
        # Later globally frozen body cannot fill a prior round's source hole.
        result = analyze_path_hypotheses(bundle, [self.wrapper(index=1, call="early-plan")], config=self.cfg)
        self.assertEqual(result["status"], "unknown")
        self.assertIn(missing["id"], result["candidates"][0]["edge_support"][0]["unresolved_support_ids"])
        flat = self.run_diagnostic(first)
        self.assertEqual(flat["status"], "unknown")
        supports = flat["candidates"][0]["edge_support"][0]
        self.assertIn(missing["id"], supports["unresolved_support_ids"])

    def test_automatic_multihop_is_not_relation_composition(self):
        self.add([fact("alpha", "beta")], ["alpha", "beta"])
        self.add([fact("beta", "gamma")], ["beta", "gamma"])
        self.add(raw="Fresh unrelated isolated observation", marks=["isolated"])
        result = self.run_diagnostic(self.frozen("alpha gamma"), self.action(b="gamma"))
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(any(item["path_nodes"] == ["alpha", "beta", "gamma"] for item in result["candidates"]))
        self.assertIn("relation_composition_not_authorized", result["candidates"][0]["reasons"])

    def test_direction_polarity_qualifier_conflicts_and_missing_information(self):
        self.add([fact(time="2023", scope="pilot")])
        record = self.frozen()
        for action, expected in [
            (self.action(time="2023", scope="pilot", direction="object_to_subject"), "rejected"),
            (self.action(time="2023", scope="pilot", polarity="negative"), "rejected"),
            (self.action(time="2024", scope="pilot"), "rejected"),
            (self.action(time="2023", scope="other"), "rejected"),
            (self.action(), "unknown"),
            (self.action(time="2023", scope="pilot", negation=True), "unknown")]:
            with self.subTest(expected=expected):
                self.assertEqual(self.run_diagnostic(record, action)["status"], expected)

    def test_unrelated_predicates_reverse_facts_and_other_time_do_not_negate_match(self):
        self.add([fact(time="2023"), fact(relation="inhibits", time="2023"),
                  fact("beta", "alpha", time="2023"), fact(polarity="negative", time="2024")])
        result = self.run_diagnostic(self.frozen(), self.action(time="2023"))
        self.assertEqual(result["status"], "pending_hypothesis")

    def test_same_relation_direction_qualifier_positive_negative_conflict(self):
        self.add([fact(), fact(polarity="negative")])
        self.assertEqual(self.run_diagnostic(self.frozen())["status"], "rejected")

    def test_unknown_applicability_is_not_discarded_by_matching_fact(self):
        self.add([fact(time="2023"), fact(polarity="negative")])
        result = self.run_diagnostic(self.frozen(), self.action(time="2023"))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("literal_time_unknown", result["candidates"][0]["reasons"])

    def test_preflight_bounds_keys_escaping_utf8_and_cycles_before_encoder(self):
        from indeces.path_hypotheses import _hash, _Stop
        cfg = replace(self.cfg, max_serialized_bytes=64)
        for value in ({"x" * 65: None}, {"key": "\x01" * 16}, {"key": "😀" * 17}):
            with patch("indeces.path_hypotheses.json.JSONEncoder", side_effect=AssertionError("encoded before bound")):
                with self.assertRaises(_Stop):
                    _hash(value, cfg, lambda: None)
        shared = ["safe"]
        self.assertEqual(_hash([shared, shared], self.cfg, lambda: None), digest([shared, shared]))

    def test_conditions_and_malformed_contract_stay_unknown(self):
        self.add([fact(conditions=["heated"])])
        result = self.run_diagnostic(self.frozen())
        self.assertEqual(result["status"], "unknown")
        self.assertIn("conditions_not_declared_in_query", result["candidates"][0]["reasons"])
        self.add(raw='typed_facts_v1: {"facts": not_json}')
        result = self.run_diagnostic(self.frozen())
        self.assertEqual(result["status"], "unknown")

    def test_partial_multiple_requirements_and_empty_action_cannot_hide(self):
        self.add()
        record = self.frozen()
        action = self.action()
        extra = deepcopy(action["query"]["relations"][0])
        extra["predicate"] = "unavailable"
        action["query"]["relations"].append(extra)
        result = self.run_diagnostic(record, action)
        self.assertEqual([item["status"] for item in result["requirements"]], ["pending_hypothesis", "unknown"])
        self.assertEqual(result["status"], "unknown")
        empty = self.action()
        empty["query"]["relations"] = []
        result = analyze_path_hypotheses(record, [self.wrapper(), self.wrapper(empty)], config=self.cfg)
        self.assertEqual([item["status"] for item in result["actions"]], ["pending_hypothesis", "unknown"])
        self.assertEqual(result["status"], "unknown")

    def test_model_material_marker_source_identity_and_body_tampering_reject(self):
        self.add()
        record = self.frozen()
        mutations = [lambda r: r.update(model_materials=[]),
            lambda r: r["materials"][0]["model_payload"].update(citation_marker="[M9]"),
            lambda r: r["materials"][0].update(quote="tampered"),
            lambda r: r.update(scope=None, event_id=None),
            lambda r: r["materials"][0].update(source_id=123)]
        for index, mutation in enumerate(mutations):
            with self.subTest(tamper=index):
                changed = deepcopy(record)
                mutation(changed)
                result = self.run_diagnostic(changed)
                self.assertEqual(result["status"], "unknown")

    def test_dynamic_no_demand_action_identity_and_uid_tampering(self):
        self.add()
        self.assertEqual(self.run_diagnostic(self.frozen(mode="dynamic"))["status"], "unknown")
        record = self.frozen()
        self.assertEqual(analyze_path_hypotheses(record, [], config=self.cfg)["incomplete_reasons"], ["no_logical_demand"])
        action = self.wrapper()
        action["action_sha256"] = "a" * 64
        self.assertEqual(analyze_path_hypotheses(record, [action], config=self.cfg)["status"], "unknown")
        bundle = append_retrieval(None, record, round_index=0, request_id="initial", planning_call_id=None)
        second = self.frozen()
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        bundle["rounds"][1]["bindings"][0]["evidence_uid"] = "b" * 64
        self.assertEqual(analyze_path_hypotheses(bundle, [self.wrapper(index=1)], config=self.cfg)["status"], "unknown")

    def test_resource_limits_large_body_cycles_and_deadline(self):
        self.add()
        record = self.frozen()
        for override in ({"max_nodes": 1}, {"max_arcs": 1}, {"max_expansions": 1},
                         {"max_bindings": 1}, {"max_serialized_bytes": 20}):
            with self.subTest(cap=override):
                result = self.run_diagnostic(record, cfg=replace(self.cfg, **override))
                self.assertEqual(result["status"], "budget_stop")
        huge = deepcopy(record)
        huge["sources"][huge["materials"][0]["source_id"]]["raw_text"] = "x" * 100
        self.assertEqual(self.run_diagnostic(huge, cfg=replace(self.cfg, max_source_chars=50))["status"], "budget_stop")
        cyclic = deepcopy(record)
        cyclic["cycle"] = cyclic
        self.assertEqual(self.run_diagnostic(cyclic)["status"], "unknown")
        self.assertEqual(analyze_path_hypotheses(record, [self.wrapper()], config=self.cfg, deadline=0)["status"], "budget_stop")
        self.assertEqual(analyze_path_hypotheses(record, [self.wrapper()], config=self.cfg, deadline=10**400)["status"], "unknown")
        # A bounded depth refusal describes the frozen search, not no global route.
        self.add([fact("beta", "gamma")], ["beta", "gamma"])
        self.add(raw="First unrelated isolated fixture", marks=["isolated"])
        chain = self.frozen("alpha gamma")
        result = self.run_diagnostic(chain, self.action(b="gamma"), replace(self.cfg, max_depth=1))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no_path_within_frozen_depth", result["incomplete_reasons"])
        # A fresh cooccurring triple plus isolated fact gives a real triangle.
        self.add([fact()], ["alpha", "beta", "gamma"])
        self.add(raw="Second unrelated isolated fixture", marks=["isolated-2"])
        triangle = self.frozen()
        result = self.run_diagnostic(triangle, cfg=replace(self.cfg, max_frontier=1))
        self.assertEqual(result["status"], "budget_stop")
        self.assertIn("frontier", result["incomplete_reasons"])
        result = self.run_diagnostic(triangle, cfg=replace(self.cfg, max_candidates=1))
        self.assertEqual(result["status"], "budget_stop")
        self.assertIn("candidates", result["incomplete_reasons"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["incomplete"])
        keyed = deepcopy(record)
        keyed["x" * 100] = "\x01" * 200
        result = self.run_diagnostic(keyed, cfg=replace(self.cfg, max_serialized_bytes=1000))
        self.assertEqual(result["status"], "budget_stop")


if __name__ == "__main__":
    unittest.main()
