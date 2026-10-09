"""Synthetic source-bound path feedback; no claim of scientific entailment.

The native Store exists only in a temporary directory. Alias and composition
rules are empty unless a test explicitly supplies a synthetic declaration.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from indeces.active_requery import parse_action
from indeces.memory import MemoryGraph
from indeces.requery_records import append_retrieval, validate_bundle
from indeces.run_records import digest, freeze_retrieval
from indeces.store import Store


def synthetic_fact(subject="alpha", target="beta", relation="links", polarity="positive", **qualifiers):
    return dict(subject=subject, object=target, relation=relation, polarity=polarity, **qualifiers)


def synthetic_quote(facts):
    return "typed_facts_v1: " + json.dumps({"facts": facts}, separators=(",", ":"))


def synthetic_action(subject="alpha", target="gamma", relation="links", *,
                     polarity="positive", direction="subject_to_object", time=None,
                     scope=None, negation=False):
    original = f"{subject} {relation} {target}"
    anchors = {}
    for field, value in (("time", time), ("scope", scope), ("negation", "not" if negation else None)):
        if value is not None:
            first = len(original) + 1
            original += " " + value
            anchors[field] = {"surface": value, "span": [first, len(original)]}
    action = {"action": "query", "query": {
        "entities": [{"id": "e1", "surface": subject, "canonical": subject, "span": [0, len(subject)]},
                     {"id": "e2", "surface": target, "canonical": target,
                      "span": [len(subject) + len(relation) + 2, len(subject) + len(relation) + 2 + len(target)]}],
        "relations": [{"subject_id": "e1", "object_id": "e2", "predicate": relation,
                       "surface": relation, "span": [len(subject) + 1, len(subject) + len(relation) + 1],
                       "negated": polarity == "negative", "direction": direction}],
        "negations": [anchors["negation"]] if negation else [], "time": anchors.get("time"),
        "scope": anchors.get("scope"), "canonical_terms": [subject, target], "ambiguities": []},
        "missing_evidence": ["Synthetic relation fixture remains unresolved."],
        "clarification": "", "stop_reason": ""}
    return parse_action(json.dumps(action), original)


class SyntheticFrozenPool:
    def __init__(self):
        self.temporary = TemporaryDirectory()
        self.store = Store(Path(self.temporary.name).resolve() / "state")
        self.graph = MemoryGraph(self.store.db)
        self.scope = "synthetic:knowledge"
        self.sequence = 0

    def close(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, facts=None, *, marks=("alpha", "beta"), quote=None, source=None):
        self.sequence += 1
        text = quote if quote is not None else synthetic_quote(facts or [synthetic_fact()])
        return self.graph.add(self.scope, source or f"synthetic:source-{self.sequence}",
            "synthetic-author", [{"text": text, "quote": text, "marks": list(marks)}], 1.0)[0]

    def frozen(self, query="alpha gamma", *, full_audit=False):
        self.sequence += 1
        event_id = f"synthetic-loop-retrieval-{self.sequence}"
        audit = {}
        if full_audit:
            # The existing legacy diagnostic API produces schema-1 full
            # graph arithmetic; production stays on its neighborhood path.
            selected = self.graph._retrieve_legacy(self.scope, [], query, float(self.sequence + 2),
                event_id=event_id, ranking_mode="static", audit=audit)
        else:
            selected = self.graph.retrieve(self.scope, [], query, float(self.sequence + 2),
                event_id=event_id, ranking_mode="static", context_query="", audit=audit)
        return freeze_retrieval(self.store.db, self.scope, event_id, query, selected, audit)

    def chain(self, *, qualifiers=None):
        qualifiers = qualifiers or {}
        self.add([synthetic_fact(**qualifiers)], marks=("alpha", "beta"))
        self.add([synthetic_fact("beta", "gamma", **qualifiers)], marks=("beta", "gamma"))
        # Without a third unrelated observation beta occurs in every record,
        # making the native NPMI of the two chain edges zero.
        self.add(quote="Synthetic isolated observation.", marks=("isolated",))
        return self.frozen()

    def disconnected(self):
        self.add([synthetic_fact()], marks=("alpha", "beta"))
        self.add([synthetic_fact("gamma", "delta")], marks=("gamma", "delta"))
        return self.frozen()

    @staticmethod
    def declared_full_graph(frozen):
        # This is a declaration for a known complete, temporary synthetic
        # graph, not a claim that a production corpus was exhaustively read.
        declared = deepcopy(frozen)
        selection = declared["graph_audit"]["selection"]
        positive = [row for row in selection["edge_statistics"] if row["static_score"] > 0]
        assert len(positive) == selection["live_edge_count"]
        nodes = set(selection["mark_frequencies"])
        nodes.update(node for row in positive for node in (row["a"], row["b"]))
        nodes.update(mark for material in declared["materials"] for mark in material["marks"])
        selection.pop("edge_statistics_scope", None)
        selection["path_graph_scope"] = {
            "kind": "full_positive_static_graph_v1", "scope": declared["scope"],
            "complete": True, "node_labels": sorted(nodes), "edge_count": len(positive)}
        audit = declared["graph_audit"]
        audit["durable_payload_sha256"] = digest({key: value for key, value in audit.items()
                                                if key != "durable_payload_sha256"})
        declared["graph_audit_sha256"] = digest(declared["graph_audit"])
        return declared

    def weights(self):
        return {table: [tuple(row) for row in self.store.db.execute(
            f"SELECT * FROM {table} ORDER BY scope,a,b")]
            for table in ("memory_static", "memory_dynamic")}


class PathRequeryLoopTests(unittest.TestCase):
    def setUp(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig, build_path_feedback
        self.Config, self.build = PathRequeryLoopConfig, build_path_feedback
        self.pool = SyntheticFrozenPool()
        self.addCleanup(self.pool.close)
        self.guards = [patch("socket.create_connection", side_effect=AssertionError("offline network forbidden")),
                       patch("socket.socket.connect", side_effect=AssertionError("offline network forbidden")),
                       patch("aiohttp.ClientSession", side_effect=AssertionError("offline HTTP forbidden"))]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)

    def feedback(self, frozen, action=None, *, config=None, **kwargs):
        return self.build(frozen, action if action is not None else synthetic_action(),
            planning_call_id="synthetic-prospective-plan", config=config or self.Config(enabled=True, max_seconds=3), **kwargs)

    def assert_safe_receipt(self, receipt):
        self.assertEqual(receipt["schema"], "path_requery_feedback_v1")
        self.assertFalse(receipt["proof"])
        self.assertFalse(receipt["semantic_support_verified"])
        self.assertFalse(receipt["changes_base_weights"])
        self.assertFalse(receipt["additive_score"])

    def candidate(self, receipt, nodes):
        matching = [candidate for candidate in receipt["candidates"] if candidate["path_nodes"] == nodes]
        self.assertTrue(matching, receipt)
        return matching[0]

    def direct(self, facts=None, *, quote=None):
        self.pool.add(facts, quote=quote)
        self.pool.add(quote="Synthetic unrelated observation.", marks=("isolated",))
        return self.pool.frozen("alpha beta")

    def test_c1_exact_path_and_native_edge_ids_bind_source_receipts(self):
        frozen = self.pool.chain()
        before, weights = deepcopy(frozen), self.pool.weights()
        action = synthetic_action()
        receipt = self.feedback(frozen, action)
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta", "gamma"])
        expected = []
        edge_rows = frozen["graph_audit"]["selection"]["edge_statistics"]
        for pair in (("alpha", "beta"), ("beta", "gamma")):
            row = next(edge for edge in edge_rows if (edge["a"], edge["b"]) == pair)
            expected.append(digest([frozen["scope"], pair, row["static_score"], row["source_record_ids"]]))
        self.assertEqual(candidate["edge_ids"], expected)
        self.assertEqual(receipt["baseline_terms"], ["alpha", "gamma"])
        self.assertEqual(receipt["added_terms"], ["beta"])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma", "beta"])
        demand = receipt["demand_binding"]
        self.assertEqual(demand["action_sha256"], digest(action))
        self.assertEqual(demand["parent_evidence_sha256"], digest(frozen))
        self.assertEqual(demand["planning_call_id"], "synthetic-prospective-plan")
        clue = next(clue for clue in receipt["clues"] if clue["term"] == "beta")
        self.assertEqual({binding["edge_id"] for binding in clue["provenance"]}, set(expected))
        for binding in clue["provenance"]:
            self.assertEqual(len(binding["evidence_uid"]), 64)
            self.assertEqual(len(binding["quote_sha256"]), 64)
            self.assertTrue(binding["citation_id"].startswith("M"))
        self.assertEqual(frozen, before)
        self.assertEqual(self.pool.weights(), weights)

    def test_c1b_exact_mapped_direct_path_is_a_noop(self):
        frozen = self.direct()
        action = synthetic_action(target="beta")
        receipt = self.feedback(frozen, action)
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["effective_terms"], action["query"]["canonical_terms"])
        self.assertEqual(receipt["added_terms"], [])
        self.assertEqual([mapping["graph_endpoint"] for mapping in receipt["mapping"]], ["alpha", "beta"])
        self.candidate(receipt, ["alpha", "beta"])

    def test_c2_missing_endpoint_preserves_original_terms_without_guessed_mapping(self):
        frozen = self.pool.chain()
        action = synthetic_action(target="absent")
        # Keep the delivered DEV11 experiment reproducible in explicit legacy
        # mode. The new mode separately admits a qualified half-edge clue.
        receipt = self.feedback(frozen, action,
            config=self.Config(enabled=True, accumulate_rounds=False, max_seconds=3))
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["baseline_terms"], ["alpha", "absent"])
        self.assertEqual(receipt["effective_terms"], ["alpha", "absent"])
        self.assertEqual(receipt["added_terms"], [])
        missing = next(mapping for mapping in receipt["mapping"] if mapping["entity_id"] == "e2")
        self.assertIsNone(missing["graph_endpoint"])
        self.assertNotEqual(missing["status"], "mapped")

    def test_c3_aliases_require_explicit_synthetic_declarations(self):
        frozen = self.pool.chain()
        action = synthetic_action("Aster", "Brine")
        before = deepcopy(action)
        ordinary = self.feedback(frozen, action)
        self.assertEqual(ordinary["effective_terms"], ["Aster", "Brine"])
        self.assertEqual(ordinary["added_terms"], [])
        cfg = self.Config(enabled=True, max_seconds=3, aliases={"Aster": "alpha", "Brine": "gamma"})
        declared = self.feedback(frozen, action, config=cfg)
        self.assert_safe_receipt(declared)
        self.assertEqual(declared["baseline_terms"], ["Aster", "Brine"])
        self.assertEqual(declared["effective_terms"][:2], ["Aster", "Brine"])
        self.assertEqual(set(declared["added_terms"]), {"alpha", "gamma", "beta"})
        self.assertEqual([mapping["graph_endpoint"] for mapping in declared["mapping"]], ["alpha", "gamma"])
        self.assertEqual(action, before)
        self.assertEqual(self.Config().aliases, {})

    def test_c4_reversed_direction_is_retained_as_a_conflict(self):
        frozen = self.direct()
        action = synthetic_action(target="beta", direction="object_to_subject")
        before = deepcopy(action)
        receipt = self.feedback(frozen, action)
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["beta", "alpha"])
        self.assertIn("direction_conflict", candidate["reasons"])
        self.assertEqual(candidate["status"], "rejected")
        self.assertEqual((candidate["relation_proposal"]["from"], candidate["relation_proposal"]["to"]),
                         ("beta", "alpha"))
        self.assertEqual(action, before)

    def test_c5_negation_and_negative_demand_are_not_rewritten_as_positive(self):
        frozen = self.direct()
        action = synthetic_action(target="beta", polarity="negative", negation=True)
        before = deepcopy(action)
        receipt = self.feedback(frozen, action)
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta"])
        self.assertEqual(candidate["relation_proposal"]["polarity"], "negative")
        self.assertEqual(candidate["relation_proposal"]["independent_negation_anchors"], action["query"]["negations"])
        self.assertIn("polarity_conflict", candidate["reasons"])
        self.assertIn("query_negation_scope_unknown", candidate["reasons"])
        self.assertEqual(action, before)

    def test_c6_literal_conditions_remain_unknown_with_full_fact_binding(self):
        frozen = self.direct([synthetic_fact(conditions=["synthetic-dry-condition"])])
        receipt = self.feedback(frozen, synthetic_action(target="beta"))
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta"])
        self.assertIn("conditions_not_declared_in_query", candidate["reasons"])
        self.assertEqual(candidate["status"], "unknown")
        self.assertEqual(candidate["edge_bindings"][0]["fact"]["conditions"], ["synthetic-dry-condition"])

    def test_c7_bibliography_cannot_authorize_exploration_even_with_typed_markers(self):
        facts = [synthetic_fact(), synthetic_fact("beta", "gamma")]
        self.pool.add(quote="References\n" + synthetic_quote(facts), marks=("alpha", "beta", "gamma"))
        self.pool.add(quote="Synthetic unrelated observation.", marks=("isolated",))
        receipt = self.feedback(self.pool.frozen())
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["added_terms"], [])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
        self.assertTrue(receipt["candidates"])
        self.assertTrue(all(not candidate["structural_support_qualified"] for candidate in receipt["candidates"]))
        self.assertIn("bibliograph", json.dumps(receipt).lower())

    def test_c8_conflicting_polarities_are_preserved_without_verified_relation(self):
        frozen = self.direct([synthetic_fact(), synthetic_fact(polarity="negative")])
        receipt = self.feedback(frozen, synthetic_action(target="beta"))
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta"])
        self.assertEqual(candidate["status"], "rejected")
        self.assertIn("polarity_conflict", candidate["reasons"])
        self.assertEqual({binding["fact"]["polarity"] for binding in candidate["edge_bindings"]},
                         {"positive", "negative"})

    def test_c9_declared_synthetic_composition_binds_two_distinct_record_uids(self):
        left = synthetic_quote([synthetic_fact(relation="synthetic-left")])
        right = synthetic_quote([synthetic_fact("beta", "gamma", relation="synthetic-right")])
        self.pool.graph.add(self.pool.scope, "synthetic:two-record-source", "synthetic-author",
            [{"text": left, "quote": left, "marks": ["alpha", "beta"]},
             {"text": right, "quote": right, "marks": ["beta", "gamma"]}], 1.0)
        self.pool.add(quote="Synthetic unrelated observation.", marks=("isolated",))
        frozen = self.pool.frozen()
        self.assertEqual(len(frozen["materials"]), 2)
        rule = {"rule_id": "synthetic-only-rule", "relation1": "synthetic-left",
                "relation2": "synthetic-right", "result": "synthetic-composed"}
        cfg = self.Config(enabled=True, max_seconds=3, composition_rules=(rule,))
        receipt = self.feedback(frozen, synthetic_action(relation="synthetic-composed"), config=cfg)
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta", "gamma"])
        derivation = candidate["formal_derivation"]
        self.assertEqual(derivation["status"], "formal_under_declared_rule")
        self.assertEqual(derivation["rule_ids"], ["synthetic-only-rule"])
        bindings = derivation["fact_bindings"]
        self.assertEqual(len({binding["evidence_uid"] for binding in bindings}), 2)
        self.assertEqual({binding["fact"]["relation"] for binding in bindings},
                         {"synthetic-left", "synthetic-right"})
        self.assertEqual(len({binding["record_id"] for binding in bindings}), 2)

    def test_c9b_empty_rules_do_not_compose_a_multihop_relation(self):
        frozen = self.pool.chain()
        cfg = self.Config(enabled=True, max_seconds=3)
        self.assertEqual(cfg.composition_rules, ())
        receipt = self.feedback(frozen, config=cfg)
        self.assert_safe_receipt(receipt)
        candidate = self.candidate(receipt, ["alpha", "beta", "gamma"])
        self.assertEqual(candidate["status"], "unknown")
        self.assertIn("relation_composition_not_authorized", candidate["reasons"])
        self.assertNotEqual((candidate.get("formal_derivation") or {}).get("status"), "formal_under_declared_rule")

    def test_c10_exhaustive_declared_scope_is_only_structural_absence(self):
        native = self.pool.disconnected()
        neighborhood = self.feedback(native)
        self.assert_safe_receipt(neighborhood)
        self.assertIn("no_path_in_frozen_neighborhood", neighborhood["reasons"])
        self.assertEqual(neighborhood["requirements"][0]["status"], "unknown")
        self.assertFalse(neighborhood["graph_scopes"][0]["declared_complete"])
        frozen = self.pool.declared_full_graph(self.pool.frozen(full_audit=True))
        receipt = self.feedback(frozen)
        self.assert_safe_receipt(receipt)
        self.assertIn("declared_full_graph_no_path", receipt["reasons"])
        self.assertEqual(receipt["requirements"][0]["status"], "structurally_disconnected_under_declaration")
        self.assertEqual(receipt["candidates"], [])
        scope = receipt["graph_scopes"][0]
        self.assertTrue(scope["declared_complete"])
        self.assertEqual(scope["completeness"], "declared_not_corpus_verified")
        self.assertFalse(scope["corpus_completeness_verified"])
        self.assertEqual(scope["record_sha256"], digest(frozen))

    def test_c10_expansion_cutoff_does_not_report_full_graph_absence(self):
        self.pool.disconnected()
        frozen = self.pool.declared_full_graph(self.pool.frozen(full_audit=True))
        before = deepcopy(frozen)
        receipt = self.feedback(frozen, config=self.Config(enabled=True, max_seconds=3, max_expansions=1))
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertEqual(receipt["reason"], "expansions")
        self.assertEqual(receipt["reasons"], ["expansions"])
        self.assertNotIn("declared_full_graph_no_path", receipt["reasons"])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
        for key in ("added_terms", "candidates", "requirements", "mapping", "clues", "frontier_clues", "graph_scopes"):
            self.assertEqual(receipt[key], [])
        self.assertTrue(receipt["incomplete"])
        self.assertEqual(frozen, before)

    def test_c11_branch_frontier_cutoff_preserves_baseline_without_partial_clues(self):
        for target in ("branchone", "branchtwo", "branchthree"):
            self.pool.add([synthetic_fact("alpha", target)], marks=("alpha", target))
        self.pool.add([synthetic_fact("gamma", "tail")], marks=("gamma", "tail"))
        frozen = self.pool.frozen()
        weights = self.pool.weights()
        receipt = self.feedback(frozen, config=self.Config(enabled=True, max_seconds=3, max_frontier=1))
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertEqual(receipt["reason"], "frontier")
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
        self.assertEqual(receipt["added_terms"], [])
        self.assertEqual(receipt["candidates"], [])
        self.assertEqual(receipt["clues"], [])
        self.assertEqual(self.pool.weights(), weights)

    def test_c12_bounded_enabled_feedback_does_not_double_count_or_mutate_base(self):
        frozen = self.pool.chain()
        bundle = append_retrieval(None, frozen, round_index=0, request_id="synthetic-initial", planning_call_id=None)
        bundle = append_retrieval(bundle, frozen, round_index=1, request_id=frozen["event_id"],
                                  planning_call_id="synthetic-previous-plan")
        validate_bundle(bundle)
        before, weights, writes = deepcopy(bundle), self.pool.weights(), self.pool.store.db.total_changes
        cfg = self.Config(enabled=True, max_seconds=3, max_added_terms=1)
        receipt = self.feedback(bundle, config=cfg)
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["added_terms"], ["beta"])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma", "beta"])
        self.assertEqual(len(receipt["effective_terms"]), len(set(receipt["effective_terms"])))
        self.assertEqual(bundle, before)
        self.assertEqual(self.pool.weights(), weights)
        self.assertEqual(self.pool.store.db.total_changes, writes)

    def test_c13_disabled_feedback_never_reads_or_changes_inputs(self):
        class Untouchable:
            def __iter__(self):
                raise AssertionError("disabled feedback touched input")
            def __getattr__(self, name):
                raise AssertionError("disabled feedback touched input")
        cfg = self.Config()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.aliases, {})
        self.assertEqual(cfg.composition_rules, ())
        receipt = self.feedback(Untouchable(), Untouchable(), config=cfg)
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["status"], "disabled")
        self.assertEqual(receipt["effective_terms"], [])
        self.assertEqual(receipt["added_terms"], [])
        action = synthetic_action("ALPHA", "GAMMA")
        original = deepcopy(action)
        self.feedback(Untouchable(), action, config=cfg)
        self.assertEqual(action, original)

    def test_c14_empty_initial_round_does_not_hide_later_complete_same_round_path(self):
        initial = self.pool.frozen()
        later = self.pool.chain()
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, later, round_index=1, request_id=later["event_id"],
                                  planning_call_id="synthetic-previous-plan")
        validate_bundle(bundle)
        receipt = self.feedback(bundle)
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["added_terms"], ["beta"])
        candidate = self.candidate(receipt, ["alpha", "beta", "gamma"])
        self.assertEqual(candidate["round_index"], 1)
        self.assertEqual(candidate["planning_call_id"], "synthetic-previous-plan")
        self.assertEqual(candidate["demand_planning_call_id"], "synthetic-prospective-plan")
        self.assertTrue(all(row["record_sha256"] == digest(later) for row in candidate["edge_source_bindings"]))
        self.assertIn("proposed_endpoint_not_in_this_frozen_round", receipt["reasons"])

    def test_c15_refusal_receipt_fits_explicit_serialized_budget(self):
        with self.assertRaises(ValueError):
            self.Config(enabled=True, max_serialized_bytes=8192)
        cfg = self.Config(enabled=True, max_seconds=3, max_serialized_bytes=16384)
        frozen = self.pool.chain()
        padded = deepcopy(frozen)
        padded["synthetic_oversized_metadata"] = "x" * 20000
        weights, writes = self.pool.weights(), self.pool.store.db.total_changes
        receipt = self.feedback(padded, config=cfg)
        self.assert_safe_receipt(receipt)
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertEqual(receipt["reason"], "serialized_bytes")
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
        self.assertEqual(receipt["added_terms"], [])
        self.assertEqual(receipt["candidates"], [])
        encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), cfg.max_serialized_bytes)
        self.assertEqual(receipt["feedback_sha256"], digest({key: value for key, value in receipt.items()
                                                          if key != "feedback_sha256"}))
        self.assertEqual(self.pool.weights(), weights)
        self.assertEqual(self.pool.store.db.total_changes, writes)


class CrossRoundPathRequeryTests(unittest.TestCase):
    """Every synthetic round may contain just one edge and one selected body."""

    def setUp(self):
        from indeces.path_requery_loop import PathRequeryLoopConfig, build_path_feedback
        self.Config, self.build = PathRequeryLoopConfig, build_path_feedback
        self.rule = {"rule_id": "synthetic-split-rule", "relation1": "synthetic-left",
                     "relation2": "synthetic-right", "result": "synthetic-composed"}
        self.action = synthetic_action(relation="synthetic-composed")
        for target in ("socket.create_connection", "socket.socket.connect", "aiohttp.ClientSession"):
            guard = patch(target, side_effect=AssertionError("offline network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def record(self, facts, *, marks, source, query, sequence, full_audit=False):
        pool = SyntheticFrozenPool()
        self.addCleanup(pool.close)
        pool.sequence = sequence
        pool.add(facts, marks=marks, source=source)
        pool.add(quote="Synthetic disconnected background.", marks=("isolated",), source=f"background-{sequence}")
        result = pool.frozen(query, full_audit=full_audit)
        self.assertEqual(len(result["materials"]), 1)
        return result

    def split(self, second_facts=None, *, same_source=False):
        initial = self.record([synthetic_fact(relation="synthetic-left")], marks=("alpha", "beta"),
            source="synthetic:first-source", query="alpha", sequence=10)
        second = self.record(second_facts or [synthetic_fact("beta", "gamma", relation="synthetic-right")],
            marks=("beta", "gamma"), source="synthetic:first-source" if same_source else "synthetic:second-source",
            query="gamma", sequence=20)
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"],
                                  planning_call_id="synthetic-clue-guided-plan")
        validate_bundle(bundle)
        return initial, second, bundle

    def feedback(self, frozen, *, config=None, action=None, **kwargs):
        return self.build(frozen, action or self.action, planning_call_id="synthetic-current-demand",
            config=config or self.Config(enabled=True, max_seconds=3, composition_rules=(self.rule,)), **kwargs)

    def accumulated(self, receipt):
        rows = [receipt["candidates"][index] for index in receipt["accumulated_candidate_indices"]]
        return next(row for row in rows if row["path_nodes"] == ["alpha", "beta", "gamma"])

    def test_missing_endpoint_half_edge_supplies_only_sourced_beta_clue(self):
        initial, _, _ = self.split()
        before = deepcopy(initial)
        receipt = self.feedback(initial)
        self.assertEqual(receipt["added_terms"], ["beta"])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma", "beta"])
        self.assertFalse(any(row["graph_endpoint"] == "gamma" for row in receipt["mapping"]))
        self.assertEqual(receipt["candidates"], [])
        frontier = receipt["frontier_clues"][0]
        self.assertEqual(frontier["missing_endpoints"], ["gamma"])
        self.assertFalse(frontier["mapping_complete"])
        self.assertEqual(frontier["endpoint_role"], "prefix_from_known_subject")
        self.assertEqual({row["round_index"] for row in frontier["provenance"]}, {0})
        self.assertEqual({row["record_sha256"] for row in frontier["provenance"]}, {digest(initial)})
        self.assertTrue(all(row["source_id"] == "synthetic:first-source" for row in frontier["provenance"]))
        self.assertFalse(receipt["proof"])
        self.assertEqual(initial, before)

    def test_two_round_two_source_path_uses_each_original_frozen_body(self):
        initial, second, bundle = self.split()
        before = deepcopy(bundle)
        receipt = self.feedback(bundle)
        candidate = self.accumulated(receipt)
        self.assertEqual(candidate["round_indices"], [0, 1])
        self.assertEqual(candidate["status"], "formal_under_declared_rule")
        facts = candidate["formal_derivation"]["fact_bindings"]
        self.assertEqual([(row["round_index"], row["source_id"]) for row in facts],
                         [(0, "synthetic:first-source"), (1, "synthetic:second-source")])
        self.assertEqual([row["record_sha256"] for row in facts], [digest(initial), digest(second)])
        self.assertEqual(len({row["evidence_uid"] for row in facts}), 2)
        self.assertEqual({row["citation_id"] for row in facts}, {"M1", "M2"})
        self.assertEqual(candidate["edge_ids"], [row["edge_id"] for row in candidate["edge_support"]])
        self.assertTrue(all(row["static_weight"] > 0 for row in candidate["edge_source_bindings"]))
        self.assertFalse(candidate["proof"])
        self.assertFalse(candidate["semantic_support_verified"])
        self.assertFalse(receipt["accumulation_scope"]["complete"])
        self.assertEqual(bundle, before)

    def test_reverse_negative_condition_and_literal_conflicts_never_formally_compose(self):
        cases = [
            ([synthetic_fact("gamma", "beta", relation="synthetic-right")], "direction_conflict"),
            ([synthetic_fact("beta", "gamma", relation="synthetic-right", polarity="negative")], "negative_edge_claim_present"),
            ([synthetic_fact("beta", "gamma", relation="synthetic-right", conditions=["synthetic-K"])], "conditions_not_declared_in_query"),
            ([synthetic_fact("beta", "gamma", relation="synthetic-right", time="2024")], "literal_time_unknown"),
            ([synthetic_fact("beta", "gamma", relation="synthetic-right", scope="synthetic-other")], "literal_scope_unknown"),
            ([synthetic_fact("beta", "gamma", relation="synthetic-right"),
              synthetic_fact("beta", "gamma", relation="synthetic-right", polarity="negative")], "polarity_conflict")]
        for facts, reason in cases:
            with self.subTest(reason=reason):
                _, _, bundle = self.split(facts)
                receipt = self.feedback(bundle)
                candidate = self.accumulated(receipt)
                self.assertIsNone(candidate["formal_derivation"])
                self.assertIn(reason, candidate["reasons"])
                self.assertFalse(candidate["semantic_support_verified"])
                self.assertTrue(any(row["fact"] == facts[0] for row in candidate["edge_bindings"]))

    def test_missing_endpoint_negative_or_reverse_half_edge_cannot_supply_clue(self):
        for fact in (synthetic_fact("beta", "alpha", relation="synthetic-left"),
                     synthetic_fact(relation="synthetic-left", polarity="negative"),
                     synthetic_fact(relation="synthetic-left", conditions=["synthetic-K"])):
            with self.subTest(fact=fact):
                initial = self.record([fact], marks=("alpha", "beta"), source="synthetic:unsafe-half",
                                      query="alpha", sequence=30)
                receipt = self.feedback(initial)
                self.assertEqual(receipt["added_terms"], [])
                self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
                self.assertEqual(receipt["frontier_clues"], [])

    def test_cross_round_source_record_version_conflict_excludes_both_versions(self):
        _, _, bundle = self.split(same_source=True)
        receipt = self.feedback(bundle)
        self.assertEqual(receipt["accumulated_candidate_indices"], [])
        self.assertIn("conflicting_frozen_source_versions", receipt["reasons"])
        self.assertEqual(receipt["added_terms"], [])
        self.assertTrue(receipt["accumulation_scope"]["source_version_conflicts"])
        self.assertFalse(any(row["formal_derivation"] for row in receipt["candidates"]))

    def test_cross_round_evidence_keeps_empty_rule_semantics_unknown(self):
        _, _, bundle = self.split()
        receipt = self.feedback(bundle, config=self.Config(enabled=True, max_seconds=3))
        candidate = self.accumulated(receipt)
        self.assertIsNone(candidate["formal_derivation"])
        self.assertEqual(candidate["status"], "unknown")
        self.assertIn("relation_composition_not_authorized", candidate["reasons"])

    def test_cross_round_budget_cutoff_omits_every_partial_accumulation(self):
        _, _, bundle = self.split()
        receipt = self.feedback(bundle, config=self.Config(enabled=True, max_seconds=3, max_expansions=1))
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertIsNone(receipt["accumulation_scope"])
        self.assertEqual(receipt["accumulated_candidate_indices"], [])
        self.assertEqual(receipt["candidates"], [])
        self.assertEqual(receipt["added_terms"], [])
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])

    def test_explicit_legacy_mode_retains_the_original_cross_round_unknown(self):
        _, _, bundle = self.split()
        cfg = self.Config(enabled=True, accumulate_rounds=False, max_seconds=3, composition_rules=(self.rule,))
        receipt = self.feedback(bundle, config=cfg)
        self.assertEqual(receipt["analysis_scope"], "each_frozen_round")
        self.assertNotIn("accumulate_rounds", receipt["limits"])
        self.assertNotIn("accumulation_scope", receipt)
        self.assertIn("cross_round_path_not_implemented", receipt["reasons"])
        self.assertEqual(receipt["added_terms"], [])

    def test_accumulation_config_bool_is_not_coerced(self):
        with self.assertRaises(ValueError):
            self.Config(accumulate_rounds=1)

    def test_legacy_minimum_envelope_cap_is_accepted_and_actual_refusal_fits(self):
        # This exact cap was legal in the delivered pre-accumulation config.
        # Legacy declarations and receipts both omit the new mode flag.
        cfg = self.Config(enabled=True, accumulate_rounds=False, max_serialized_bytes=8616)
        receipt = self.feedback(None, config=cfg, deadline=0.0)
        self.assertEqual(receipt["status"], "budget_stop")
        self.assertEqual(receipt["effective_terms"], ["alpha", "gamma"])
        self.assertNotIn("accumulate_rounds", receipt["limits"])
        actual_bytes = len(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(actual_bytes, cfg.max_serialized_bytes)
        self.assertEqual(receipt["feedback_sha256"], digest({key: value for key, value in receipt.items()
                                                           if key != "feedback_sha256"}))
        with self.assertRaises(ValueError):
            self.Config(enabled=True, accumulate_rounds=True, max_serialized_bytes=8616)

    @staticmethod
    def knowledge_metadata(record, *, path, original_sha, chunks):
        """Explicit synthetic file-version contract, never a real file read."""
        frozen = deepcopy(record)
        material = frozen["materials"][0]
        raw = "\n".join(text for text, _ in chunks)
        rows, offset = [], 0
        for index, (text, marks) in enumerate(chunks):
            rows.append({"chunk_index": index, "start_character": offset, "text": text, "marks": marks})
            if material["quote"] == text:
                material["chunk_index"] = index
            offset += len(text) + 1
        frozen["sources"][material["source_id"]] = {
            "source_id": material["source_id"], "metadata_available": True, "scope": frozen["scope"],
            "path": path, "status": "ready", "published_source_id": material["source_id"],
            "raw_text": raw, "normalized_text_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "original_file_bytes_sha256": original_sha, "original_file_bytes_verifiable": False,
            "chunks": rows}
        from indeces.run_records import validate_graph_audit, validate_retrieval
        validate_retrieval(frozen)
        validate_graph_audit(frozen["graph_audit"], frozen)
        return frozen

    def test_same_frozen_file_version_allows_different_quote_spans(self):
        initial, second, _ = self.split()
        chunks = [(initial["materials"][0]["quote"], ["alpha", "beta"]),
                  (second["materials"][0]["quote"], ["beta", "gamma"])]
        initial = self.knowledge_metadata(initial, path="synthetic/shared.txt", original_sha="a" * 64, chunks=chunks)
        second = self.knowledge_metadata(second, path="synthetic/shared.txt", original_sha="a" * 64, chunks=chunks)
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        candidate = self.accumulated(receipt)
        self.assertEqual(receipt["accumulation_scope"]["source_version_conflicts"], [])
        self.assertIsNotNone(candidate["formal_derivation"])
        versions = [row["source_version"] for row in candidate["formal_derivation"]["fact_bindings"]]
        self.assertNotEqual(versions[0]["quote_source_span"], versions[1]["quote_source_span"])
        self.assertEqual(versions[0]["normalized_text_sha256"], versions[1]["normalized_text_sha256"])

    def test_different_source_ids_cannot_mix_two_versions_of_one_frozen_file_path(self):
        initial, second, _ = self.split()
        initial = self.knowledge_metadata(initial, path="synthetic/replaced.txt", original_sha="a" * 64,
            chunks=[(initial["materials"][0]["quote"], ["alpha", "beta"])])
        second = self.knowledge_metadata(second, path="synthetic/replaced.txt", original_sha="b" * 64,
            chunks=[(second["materials"][0]["quote"], ["beta", "gamma"])])
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        self.assertEqual(receipt["accumulated_candidate_indices"], [])
        self.assertEqual(receipt["added_terms"], [])
        conflict = receipt["accumulation_scope"]["source_version_conflicts"][0]
        self.assertEqual(conflict["identity_kind"], "source_path")
        self.assertEqual(conflict["round_indices"], [0, 1])

    def test_later_quote_does_not_backfill_old_unresolved_support(self):
        pool = SyntheticFrozenPool()
        self.addCleanup(pool.close)
        ids = []
        for index in range(4):
            term = f"uniquebody{index}"
            text = f"Synthetic distinct body {index} {term}"
            text += "\n" + synthetic_quote([synthetic_fact()])
            ids.append(pool.add(quote=text, marks=("alpha", "beta", term), source=f"synthetic:body-{index}")["id"])
        pool.add(quote="Synthetic isolated observation.", marks=("isolated",))
        later_id = ids[0]
        initial = pool.frozen("alpha")
        self.assertNotIn(later_id, [row["record_id"] for row in initial["materials"]])
        later = pool.frozen("alpha uniquebody0")
        self.assertIn(later_id, [row["record_id"] for row in later["materials"]])
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, later, round_index=1, request_id=later["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle, action=synthetic_action(target="beta"))
        candidate = receipt["candidates"][receipt["accumulated_candidate_indices"][0]]
        old = [row for row in candidate["qualified_edge_support"] if row["round_index"] == 0]
        self.assertTrue(old)
        self.assertIn(later_id, old[0]["unresolved_support_ids"])
        self.assertNotIn(later_id, [row["source_record_id"] for row in old[0]["provenance"]])
        later_bindings = [row for row in candidate["edge_source_bindings"] if row["source_record_id"] == later_id]
        self.assertTrue(later_bindings)
        self.assertTrue(all(row["round_index"] == 1 and row["record_sha256"] == digest(later) for row in later_bindings))

    def test_later_conflict_suppresses_an_earlier_round_local_formal_result(self):
        pool = SyntheticFrozenPool()
        self.addCleanup(pool.close)
        pool.add([synthetic_fact(relation="synthetic-left")], marks=("alpha", "beta"), source="synthetic:left")
        pool.add([synthetic_fact("beta", "gamma", relation="synthetic-right")],
                 marks=("beta", "gamma"), source="synthetic:right")
        pool.add(quote="Synthetic disconnected background.", marks=("isolated",))
        initial = pool.frozen()
        self.assertTrue(any(row["formal_derivation"] for row in self.feedback(initial)["candidates"]))
        later = self.record([synthetic_fact("beta", "gamma", relation="synthetic-right", polarity="negative")],
                           marks=("beta", "gamma"), source="synthetic:opposing-source", query="gamma", sequence=60)
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, later, round_index=1, request_id=later["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        local = [row for row in receipt["candidates"] if row.get("evaluation_role") == "round_local_observation"]
        self.assertTrue(any(row["round_local_formal_derivation"] for row in local))
        self.assertFalse(any(row["formal_derivation"] for row in receipt["candidates"]))
        self.assertIn("polarity_conflict", self.accumulated(receipt)["reasons"])

    def test_missing_frozen_knowledge_path_blocks_uncertain_version_accumulation(self):
        initial, second, _ = self.split()
        initial = self.knowledge_metadata(initial, path="", original_sha="a" * 64,
            chunks=[(initial["materials"][0]["quote"], ["alpha", "beta"])])
        second = self.knowledge_metadata(second, path="synthetic/known.txt", original_sha="b" * 64,
            chunks=[(second["materials"][0]["quote"], ["beta", "gamma"])])
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        self.assertIn("knowledge_source_path_unavailable_for_accumulation", receipt["reasons"])
        self.assertTrue(receipt["accumulation_scope"]["unavailable_source_path_evidence_uids"])
        self.assertEqual(receipt["accumulated_candidate_indices"], [])
        self.assertFalse(any(row["formal_derivation"] for row in receipt["candidates"]))

    def test_accumulated_absence_never_inherits_round_complete_declarations(self):
        initial = self.record([synthetic_fact()], marks=("alpha", "beta"),
                              source="synthetic:first", query="alpha", sequence=70, full_audit=True)
        later = self.record([synthetic_fact("gamma", "delta")], marks=("gamma", "delta"),
                            source="synthetic:second", query="gamma", sequence=80, full_audit=True)
        # Each temporary fixture declares its own full graph only. Their
        # snapshots cannot establish a jointly complete corpus or time point.
        initial = SyntheticFrozenPool.declared_full_graph(initial)
        later = SyntheticFrozenPool.declared_full_graph(later)
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, later, round_index=1, request_id=later["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        self.assertEqual(receipt["accumulated_candidate_indices"], [])
        self.assertFalse(receipt["accumulation_scope"]["complete"])
        accumulated = [row for row in receipt["requirements"] if row.get("analysis_scope") == "current_task_frozen_rounds"]
        self.assertEqual(accumulated[0]["status"], "unknown")
        self.assertIn("no_path_in_accumulated_frozen_neighborhood", accumulated[0]["reasons"])

    def test_ambiguous_case_or_separator_paths_cannot_authorize_version_mix(self):
        for alternate in ("synthetic/shared.txt", "synthetic\\Shared.txt"):
            with self.subTest(alternate=alternate):
                initial, second, _ = self.split()
                initial = self.knowledge_metadata(initial, path="synthetic/Shared.txt", original_sha="a" * 64,
                    chunks=[(initial["materials"][0]["quote"], ["alpha", "beta"])])
                second = self.knowledge_metadata(second, path=alternate, original_sha="b" * 64,
                    chunks=[(second["materials"][0]["quote"], ["beta", "gamma"])])
                bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
                bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
                receipt = self.feedback(bundle)
                self.assertIn("source_path_identity_ambiguous", receipt["reasons"])
                self.assertEqual(receipt["accumulated_candidate_indices"], [])
                self.assertFalse(any(row["formal_derivation"] for row in receipt["candidates"]))
                conflict = receipt["accumulation_scope"]["source_version_conflicts"][0]
                self.assertEqual(conflict["identity_kind"], "ambiguous_source_path_hint")

    def test_distinct_frozen_paths_allow_distinct_source_versions(self):
        initial, second, _ = self.split()
        initial = self.knowledge_metadata(initial, path="synthetic/first.txt", original_sha="a" * 64,
            chunks=[(initial["materials"][0]["quote"], ["alpha", "beta"])])
        second = self.knowledge_metadata(second, path="synthetic/second.txt", original_sha="b" * 64,
            chunks=[(second["materials"][0]["quote"], ["beta", "gamma"])])
        bundle = append_retrieval(None, initial, round_index=0, request_id=initial["event_id"], planning_call_id=None)
        bundle = append_retrieval(bundle, second, round_index=1, request_id=second["event_id"], planning_call_id="synthetic-plan")
        receipt = self.feedback(bundle)
        self.assertEqual(receipt["accumulation_scope"]["source_version_conflicts"], [])
        self.assertIsNotNone(self.accumulated(receipt)["formal_derivation"])


if __name__ == "__main__":
    unittest.main()
