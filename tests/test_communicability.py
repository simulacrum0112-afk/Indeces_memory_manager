"""New synthetic fixtures only; no existing question, answer, DB or model."""

import copy
import json
import math
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from indeces.communicability import (CommunicabilityConfig, PathRequirement, TypedEdge,
    analyze_frozen_graph, sparse_expv, validate_path_meeting)


class KrylovTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CommunicabilityConfig(enabled=True, max_seconds=3.0)

    def test_disabled_consumes_nothing(self):
        class Untouchable:
            def __iter__(self):
                raise AssertionError("disabled diagnostics consumed input")
        self.assertEqual(sparse_expv(10**9, Untouchable(), Untouchable())["matvecs"], 0)
        self.assertEqual(analyze_frozen_graph(Untouchable())["status"], "disabled")

    def test_two_node_analytic_oracle(self):
        result = sparse_expv(2, [(0, 1, 0.7), (1, 0, 0.7)], [1, 0], config=self.cfg)
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["values"][0], math.cosh(0.7), places=11)
        self.assertAlmostEqual(result["values"][1], math.sinh(0.7), places=11)
        self.assertIsNone(result["certified_total_error_bound"])
        self.assertFalse(result["floating_point_error_included"])
        self.assertEqual(result["dense_graph_entries"], 0)

    def test_directed_nilpotent_analytic_oracle(self):
        # A e0 = 2e1; A² e0 = 6e2; A³=0; exp(A)e0=e0+2e1+3e2.
        result = sparse_expv(3, [(0, 1, 2), (1, 2, 3)], [1, 0, 0], config=self.cfg)
        self.assertEqual(result["status"], "converged")
        for actual, expected in zip(result["values"], (1, 2, 3)):
            self.assertAlmostEqual(actual, expected, places=11)

    def test_nonconvergence_retains_unaccepted_diagnostic(self):
        result = sparse_expv(3, [(0, 1, 1), (1, 2, 1)], [1, 0, 0],
                             config=replace(self.cfg, max_krylov=1))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "krylov_or_matvec_limit")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["matvecs"], 1)

    def test_small_nonzero_residual_cannot_hide_large_vector_error(self):
        # A²=0, so exp(A)[1e14,0] is exactly [1e14,1]. Residual is only 1e-14.
        arcs, v = [(0, 1, 1e-14)], [1e14, 0]
        stopped = sparse_expv(2, arcs, v, config=replace(self.cfg, max_krylov=1))
        self.assertFalse(stopped["accepted"])
        self.assertEqual(stopped["status"], "unknown")
        self.assertTrue(stopped["near_breakdown_observed"])
        self.assertEqual(stopped["defect_estimate"], 1.0)
        complete = sparse_expv(2, arcs, v, config=self.cfg)
        self.assertTrue(complete["accepted"])
        self.assertEqual(complete["matvecs"], 2)
        self.assertAlmostEqual(complete["values"][1], 1.0, places=11)

    def test_identity_cannot_succeed_after_input_work_expires_deadline(self):
        now = [0.0]
        def slow_input():
            yield 1.0
            now[0] = 4.0
        result = sparse_expv(1, [], slow_input(), config=self.cfg, clock=lambda: now[0])
        self.assertEqual(result["status"], "budget_stop")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "seconds")

    def test_small_residual_followed_by_large_growth_is_not_accepted(self):
        # Even beta=1 is unsafe: later self-growth amplifies the 1e-14 coupling.
        # Analytic triangular action is [1, 1e-14*(exp(50)-1)/50].
        arcs = [(0, 1, 1e-14), (1, 1, 50)]
        exact = 1e-14 * math.expm1(50) / 50
        self.assertGreater(exact, 1e6)
        first = sparse_expv(2, arcs, [1, 0], config=replace(self.cfg, max_krylov=1))
        self.assertFalse(first["accepted"])
        self.assertEqual(first["status"], "unknown")
        full = sparse_expv(2, arcs, [1, 0], config=self.cfg)
        self.assertFalse(full["accepted"])
        self.assertEqual(full["status"], "budget_stop")
        self.assertEqual(full["reason"], "projection_terms")
        expanded = sparse_expv(2, arcs, [1, 0],
                              config=replace(self.cfg, max_projection_terms=256))
        self.assertTrue(expanded["accepted"])
        self.assertAlmostEqual(expanded["values"][1], exact, places=6)

    def test_large_sparse_storage_and_independent_series_oracle(self):
        # Directed 512-node chain. Nonzero oracle terms are 1/k! at node k.
        n = 512
        arcs = [(i, i + 1, 1.0) for i in range(n - 1)]
        result = sparse_expv(n, arcs, [1.0] + [0.0] * (n - 1), config=self.cfg)
        self.assertTrue(result["accepted"])
        self.assertLessEqual(result["matvecs"], 32)
        self.assertLessEqual(result["reserved_projected_entries"], 4096)
        self.assertEqual(result["dense_graph_entries"], 0)
        self.assertLess(result["reserved_vector_elements"], n * n)
        oracle = 1.0
        for k in range(12):
            if k:
                oracle /= k
            self.assertAlmostEqual(result["values"][k], oracle, places=11)

    def test_each_resource_limit_stops_without_dense_fallback(self):
        n, arcs, v = 4, [(0, 1, 1), (1, 2, 1), (2, 3, 1)], [1, 0, 0, 0]
        for override, expected in [({"max_nodes": 3}, "nodes"),
                ({"max_arcs": 2}, "arcs"),
                ({"max_vector_elements": 20}, "vector_elements"),
                ({"max_projected_entries": 10}, "projected_entries"),
                ({"max_projection_terms": 1}, "projection_terms")]:
            with self.subTest(expected=expected):
                # Symmetric edge produces nonzero projected Taylor terms.
                test_arcs = [(0, 1, 1), (1, 0, 1), (2, 3, 1)]
                result = sparse_expv(n, test_arcs, v, config=replace(self.cfg, **override))
                self.assertEqual(result["status"], "budget_stop")
                self.assertEqual(result["reason"], expected)
                self.assertFalse(result["accepted"])
                self.assertEqual(result["dense_graph_entries"], 0)

    def test_deadline_stops_before_matvec(self):
        result = sparse_expv(2, [(0, 1, 1)], [1, 0], config=self.cfg,
                             deadline=9.0, clock=lambda: 10.0)
        self.assertEqual(result["status"], "budget_stop")
        self.assertEqual(result["reason"], "seconds")
        self.assertEqual(result["matvecs"], 0)

    def test_identity_zero_scale_and_invalid_data(self):
        identity = sparse_expv(2, [(0, 1, 3)], [4, 2], config=replace(self.cfg, scale=0))
        self.assertEqual(identity["values"], [4, 2])
        self.assertEqual(identity["matvecs"], 0)
        invalid = sparse_expv(2, [(0, 1, float("nan"))], [1, 0], config=self.cfg)
        self.assertEqual(invalid["status"], "unknown")
        self.assertFalse(invalid["accepted"])
        json.dumps(invalid, allow_nan=False)

    def test_optional_normalization_is_audited_and_input_immutable(self):
        arcs, v = [(0, 1, 4), (1, 0, 4)], [1, 0]
        before = copy.deepcopy((arcs, v))
        result = sparse_expv(2, arcs, v, config=replace(self.cfg, normalization="max_row_sum"))
        self.assertEqual((arcs, v), before)
        self.assertEqual(result["normalization_divisor"], 4)
        self.assertEqual(result["effective_scale"], 0.25)
        self.assertAlmostEqual(result["values"][0], math.cosh(1), places=10)


class FrozenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CommunicabilityConfig(enabled=True, max_seconds=3.0)
        self.audit = {"request": {"ranking_mode": "static", "scope": "synthetic"},
            "match": {"direct_hits": ["alpha"]},
            "selection": {"edge_statistics_scope": "direct_hit_incident_v1", "edge_statistics": [
                {"a": "alpha", "b": "beta", "static_score": 0.5, "source_record_ids": [101, 102]}]}}

    def active_bundle(self):
        second = copy.deepcopy(self.audit)
        second["match"]["direct_hits"] = ["gamma"]
        second["selection"]["edge_statistics"] = [
            {"a": "beta", "b": "gamma", "static_score": 0.3, "source_record_ids": [103]}]
        return {"schema": "active_requery_evidence_v1", "version": 1, "scope": "synthetic", "rounds": [
            {"round_index": 0, "request_id": "synthetic-round-0", "planning_call_id": None,
             "record": {"scope": "synthetic", "event_id": "synthetic-initial-retrieval",
                        "graph_audit": copy.deepcopy(self.audit)}, "record_sha256": "a" * 64},
            {"round_index": 1, "request_id": "synthetic-round-1", "planning_call_id": "synthetic-plan-1",
             "record": {"scope": "synthetic", "event_id": "synthetic-round-1",
                        "graph_audit": second}, "record_sha256": "b" * 64}]}

    def test_actual_active_bundle_shape_two_rounds_projects_graph_metadata(self):
        bundle = self.active_bundle()
        before = copy.deepcopy(bundle)
        result = analyze_frozen_graph(bundle, config=self.cfg)
        self.assertEqual(bundle, before)
        self.assertEqual(result["input_mode"], "active_requery_bundle")
        self.assertEqual(len(result["rounds_metadata"]), 2)
        self.assertEqual(result["rounds_metadata"][1]["planning_call_id"], "synthetic-plan-1")
        self.assertEqual(result["bundle_binding_validation"], "upstream_only_not_replayed")
        self.assertEqual(result["status"], "converged")
        self.assertTrue(result["numerical"]["accepted"])
        self.assertEqual(result["numerical"]["arc_count"], 4)
        self.assertEqual(result["seeds"], ["alpha", "gamma"])
        # A three-node symmetric graph may need all of its Krylov space.
        self.assertTrue(result["numerical"]["matvecs"] > 0)
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["semantic_status"], "insufficient_relation_semantics")
        self.assertFalse(result["proof"])

    def test_active_bundle_does_not_inspect_question_materials_or_bindings(self):
        class Unreadable:
            def __iter__(self):
                raise AssertionError("read unrelated body")
            def __repr__(self):
                raise AssertionError("decoded unrelated body")
        bundle = self.active_bundle()
        bundle["materials"] = Unreadable()
        for item in bundle["rounds"]:
            item["record"]["query"] = Unreadable()
            item["record"]["materials"] = Unreadable()
            item["bindings"] = Unreadable()
        result = analyze_frozen_graph(bundle, config=self.cfg)
        self.assertEqual(result["input_mode"], "active_requery_bundle")
        self.assertEqual(result["status"], "converged")
        self.assertTrue(result["numerical"]["accepted"])
        self.assertEqual(len(result["edges"]), 2)

    def test_active_bundle_schema_shape_round_bound_and_scope_rejections(self):
        mutations = [lambda b: b.update(schema="unsupported"), lambda b: b.update(version=2),
            lambda b: b.update(version=True), lambda b: b.update(rounds=None),
            lambda b: b.update(rounds=[]), lambda b: b.update(scope=None),
            lambda b: b["rounds"][1].update(record=None),
            lambda b: b["rounds"][1].update(round_index=0),
            lambda b: b["rounds"][1].update(request_id="synthetic-round-0"),
            lambda b: b["rounds"][1]["record"].update(scope="other"),
            lambda b: b["rounds"][1]["record"]["graph_audit"]["request"].update(scope="other"),
            lambda b: b["rounds"][1]["record"].update(graph_audit=None)]
        for index, mutation in enumerate(mutations):
            with self.subTest(shape=index):
                bundle = self.active_bundle()
                mutation(bundle)
                result = analyze_frozen_graph(bundle, config=self.cfg)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["numerical"]["matvecs"], 0)
        result = analyze_frozen_graph(self.active_bundle(), config=replace(self.cfg, max_bundle_rounds=1))
        self.assertEqual(result["status"], "budget_stop")
        self.assertEqual(result["reason"], "bundle_rounds")

    def native_bundle(self):
        from indeces.memory import MemoryGraph
        from indeces.requery_records import append_retrieval
        from indeces.run_records import freeze_retrieval
        from indeces.store import Store
        with TemporaryDirectory() as directory:
            store = Store(Path(directory).resolve() / "state")
            try:
                graph = MemoryGraph(store.db)
                scope = "synthetic:knowledge"
                graph.add(scope, "synth-pair", "synth-author", [
                    {"text": "Fresh alpha beta paired observation.",
                     "quote": "Fresh alpha beta paired observation.", "marks": ["alpha", "beta"]}], 1.0)
                graph.add(scope, "synth-isolated", "synth-author", [
                    {"text": "Fresh gamma isolated observation.",
                     "quote": "Fresh gamma isolated observation.", "marks": ["gamma"]}], 1.0)
                bundle = None
                for index, query in enumerate(("alpha", "gamma")):
                    event_id = f"native-synthetic-retrieval-{index}"
                    audit = {}
                    selected = graph.retrieve(scope, [], query, 2.0 + index, event_id=event_id, audit=audit)
                    frozen = freeze_retrieval(store.db, scope, event_id, query, selected, audit)
                    bundle = append_retrieval(bundle, frozen, round_index=index,
                        request_id="native-initial-request" if index == 0 else event_id,
                        planning_call_id=None if index == 0 else "native-synthetic-plan")
                return bundle
            finally:
                store.db.close()

    def test_native_memory_graph_two_round_bundle_converges(self):
        bundle = self.native_bundle()
        first_audit = bundle["rounds"][0]["record"]["graph_audit"]
        self.assertEqual(first_audit["scope"], "synthetic:knowledge")
        self.assertNotIn("scope", first_audit["request"])
        self.assertNotIn("ranking_mode", first_audit["request"])
        self.assertEqual(first_audit["selection"]["ranking_mode"], "static")
        before = copy.deepcopy(bundle)
        result = analyze_frozen_graph(bundle, config=self.cfg)
        self.assertEqual(bundle, before)
        self.assertEqual(result["status"], "converged")
        self.assertTrue(result["numerical"]["accepted"])
        self.assertEqual(len(result["rounds_metadata"]), 2)
        self.assertFalse(result["proof"])

    def test_native_dynamic_missing_conflicting_mode_and_cross_scope_are_rejected(self):
        native = self.native_bundle()
        for kind in ("dynamic", "missing", "conflicting", "scope"):
            with self.subTest(kind=kind):
                bundle = copy.deepcopy(native)
                audit = bundle["rounds"][1]["record"]["graph_audit"]
                if kind == "dynamic":
                    audit["selection"]["ranking_mode"] = "dynamic"
                elif kind == "missing":
                    del audit["selection"]["ranking_mode"]
                elif kind == "conflicting":
                    audit["request"]["ranking_mode"] = "dynamic"
                else:
                    audit["scope"] = "other"
                result = analyze_frozen_graph(bundle, config=self.cfg)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["numerical"]["matvecs"], 0)
                flat = analyze_frozen_graph(audit, config=self.cfg)
                # A lone graph may be in another scope; its native static mode
                # remains valid. Cross-scope rejection belongs to the bundle.
                if kind != "scope":
                    self.assertEqual(flat["status"], "unknown")
                    self.assertEqual(flat["numerical"]["matvecs"], 0)

    def test_frozen_adapter_provenance_partial_scope_and_immutable_weights(self):
        before = copy.deepcopy(self.audit)
        result = analyze_frozen_graph({"graph_audit": self.audit, "text": "must not read"}, config=self.cfg)
        self.assertEqual(self.audit, before)
        self.assertEqual(result["status"], "converged")
        self.assertEqual(result["numerical"]["arc_count"], 2)
        self.assertEqual(result["edges"][0]["evidence_ids"], [101, 102])
        self.assertEqual(result["edges"][0]["direction"], "undirected")
        self.assertEqual(result["edge_scope"], ["direct_hit_incident_v1"])
        self.assertFalse(result["index_directions_are_semantic_relations"])
        self.assertEqual(result["semantic_status"], "insufficient_relation_semantics")
        self.assertFalse(result["proof"])
        self.assertFalse(result["changes_selection"])
        self.assertNotIn("text", json.dumps(result))

    def test_empty_and_missing_provenance_remain_explicit(self):
        self.assertEqual(analyze_frozen_graph(self.audit, config=self.cfg, seed_nodes=[])["status"], "empty")
        self.audit["selection"]["edge_statistics"][0]["source_record_ids"] = []
        result = analyze_frozen_graph(self.audit, config=self.cfg)
        self.assertFalse(result["provenance_complete"])
        self.assertEqual(result["hypothesis_status"], "pending_unverified")

    def test_bundle_duplicate_conflict_and_source_scope_rejection(self):
        result = analyze_frozen_graph([self.audit, copy.deepcopy(self.audit)], config=self.cfg)
        self.assertEqual(len(result["edges"]), 1)
        changed = copy.deepcopy(self.audit)
        changed["selection"]["edge_statistics"][0]["static_score"] = 0.75
        self.assertEqual(analyze_frozen_graph([self.audit, changed], config=self.cfg)["status"], "unknown")
        changed = copy.deepcopy(self.audit)
        changed["request"]["scope"] = "different_synthetic_scope"
        self.assertEqual(analyze_frozen_graph([self.audit, changed], config=self.cfg)["status"], "unknown")

    def test_dynamic_unknown_and_graph_arc_budget(self):
        dynamic = copy.deepcopy(self.audit)
        dynamic["request"]["ranking_mode"] = "dynamic"
        self.assertEqual(analyze_frozen_graph(dynamic, config=self.cfg)["status"], "unknown")
        result = analyze_frozen_graph(self.audit, config=replace(self.cfg, max_arcs=1))
        self.assertEqual(result["status"], "budget_stop")
        self.assertEqual(result["numerical"]["matvecs"], 0)

    def test_provenance_storage_budget_and_invalid_deadline(self):
        for cfg, reason in [(replace(self.cfg, max_evidence_ids_per_edge=1), "evidence_ids_per_edge"),
                            (replace(self.cfg, max_evidence_ids_total=1), "evidence_ids_total")]:
            with self.subTest(reason=reason):
                result = analyze_frozen_graph(self.audit, config=cfg)
                self.assertEqual(result["status"], "budget_stop")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["numerical"]["matvecs"], 0)
        self.assertEqual(analyze_frozen_graph(self.audit, config=self.cfg,
                                            deadline=float("nan"))["status"], "unknown")
        self.assertEqual(analyze_frozen_graph({"selection": None}, config=self.cfg)["status"], "unknown")

    def test_huge_integer_public_numeric_inputs_fail_safely(self):
        huge = 10**400
        for name in ("scale", "tolerance", "breakdown_tolerance", "max_seconds"):
            with self.subTest(config_field=name), self.assertRaises(ValueError):
                replace(self.cfg, **{name: huge})
        for result in (
            sparse_expv(2, [(0, 1, 1)], [1, 0], config=self.cfg, deadline=huge),
            analyze_frozen_graph(self.audit, config=self.cfg, deadline=huge),
            sparse_expv(2, [(0, 1, huge)], [1, 0], config=self.cfg),
            sparse_expv(2, [(0, 1, 1)], [huge, 0], config=self.cfg),
        ):
            self.assertEqual(result["status"], "unknown")
            json.dumps(result, allow_nan=False)
        changed = copy.deepcopy(self.audit)
        changed["selection"]["edge_statistics"][0]["static_score"] = huge
        result = analyze_frozen_graph(changed, config=self.cfg)
        self.assertEqual(result["status"], "unknown")
        json.dumps(result, allow_nan=False)

    def test_adapter_cannot_accept_after_total_deadline(self):
        now = [0.0]
        def numerical_fixture(*args, **kwargs):
            now[0] = 4.0
            return {"status": "converged", "accepted": True, "values": [1, 0.5], "matvecs": 1}
        with patch("indeces.communicability.time.monotonic", side_effect=lambda: now[0]), \
                patch("indeces.communicability.sparse_expv", side_effect=numerical_fixture):
            result = analyze_frozen_graph(self.audit, config=self.cfg)
        self.assertEqual(result["status"], "budget_stop")
        self.assertFalse(result["numerical"]["accepted"])
        self.assertEqual(result["numerical"]["reason"], "seconds_after_numerical")

    def test_current_memory_graph_frozen_audit_smoke_is_read_only(self):
        from indeces.memory import MemoryGraph
        from indeces.run_records import freeze_retrieval
        with sqlite3.connect(":memory:") as db:
            graph = MemoryGraph(db)
            graph.add("synthetic", "synthetic-source", "synthetic-author", [
                {"text": "Fresh synthetic alpha beta association.",
                 "quote": "Fresh synthetic alpha beta association.", "marks": ["alpha", "beta"]}], 1.0)
            audit = {}
            selected = graph.retrieve("synthetic", [], "alpha", 2.0, event_id="communicability-smoke", audit=audit)
            frozen = freeze_retrieval(db, "synthetic", "communicability-smoke", "alpha", selected, audit)
            before = db.total_changes
            result = analyze_frozen_graph(frozen, config=self.cfg)
            self.assertEqual(result["status"], "converged")
            self.assertEqual(db.total_changes, before)
            self.assertEqual(result["edges"][0]["evidence_ids"], [1])
            self.assertFalse(result["changes_base_weights"])


class TypedPathTests(unittest.TestCase):
    def setUp(self):
        self.demand = [TypedEdge("reactant", "intermediate", "converts_to", ("synth-demand-1",), scope="pilot", time_window=(1, 10))]
        self.data = [TypedEdge("intermediate", "product", "yields", ("synth-data-1",), scope="pilot", time_window=(1, 10))]

    def test_meeting_traceable_only_pending_hypothesis(self):
        need = PathRequirement("intermediate", "product", "yields", scope="pilot", time_window=(2, 3))
        result = validate_path_meeting(self.demand, self.data, requirements=[need])
        self.assertEqual(result["status"], "pending_hypothesis")
        self.assertEqual(result["meeting_node"], "intermediate")
        self.assertEqual(result["edges"][1]["evidence_ids"], ["synth-data-1"])
        self.assertFalse(result["proof"])
        self.assertFalse(result["source_truth_verified"])

    def test_disconnected_and_missing_evidence(self):
        disconnected = replace(self.data[0], source="different")
        result = validate_path_meeting(self.demand, [disconnected])
        self.assertIn("disconnected_or_direction_mismatch", result["issues"])
        missing = replace(self.data[0], evidence_ids=())
        self.assertIn("missing_or_invalid_evidence", validate_path_meeting(self.demand, [missing])["issues"])

    def test_direction_relation_negation_time_and_scope_constraints(self):
        for need, issue in [
            (PathRequirement("product", "intermediate", "yields"), "direction_mismatch"),
            (PathRequirement("intermediate", "product", "inhibits"), "relation_mismatch"),
            (PathRequirement("intermediate", "product", "yields", polarity="negative"), "negation_mismatch"),
            (PathRequirement("intermediate", "product", "yields", time_window=(11, 12)), "time_mismatch_or_unknown"),
            (PathRequirement("intermediate", "product", "yields", scope="other"), "scope_mismatch_or_unknown")]:
            with self.subTest(issue=issue):
                result = validate_path_meeting(self.demand, self.data, requirements=[need])
                self.assertIn(issue, result["issues"])
                self.assertEqual(result["status"], "insufficient")

    def test_opposing_same_relation_is_conflict(self):
        loop = TypedEdge("product", "intermediate", "returns_to", ("synth-loop",), scope="pilot")
        opposed = replace(self.data[0], polarity="negative")
        result = validate_path_meeting(self.demand, self.data + [loop, opposed])
        self.assertIn("contradictory_polarity", result["issues"])
        self.assertFalse(result["proof"])

    def test_unknown_metadata_and_path_budget(self):
        edge = replace(self.data[0], time_window=None, scope=None)
        need = PathRequirement("intermediate", "product", "yields", scope="pilot", time_window=(2, 3))
        result = validate_path_meeting(self.demand, [edge], requirements=[need])
        self.assertIn("scope_mismatch_or_unknown", result["issues"])
        self.assertIn("time_mismatch_or_unknown", result["issues"])
        self.assertEqual(validate_path_meeting(self.demand, self.data, max_edges=1)["status"], "budget_stop")

    def test_invalid_time_metadata_is_safe_with_repeated_edges(self):
        for invalid in ({}, (), [1, 2], (float("nan"), 2), (2, 1), (False, 2)):
            with self.subTest(invalid_type=type(invalid).__name__):
                first = TypedEdge("a", "b", "connects", (1,), time_window=invalid, scope="pilot")
                back = TypedEdge("b", "a", "returns", (2,), time_window=(1, 2), scope="pilot")
                result = validate_path_meeting([first], [back, first])
                self.assertEqual(result["status"], "insufficient")
                self.assertIn("invalid_polarity_or_time", result["issues"])
                json.dumps(result, allow_nan=False)

    def test_chain_scope_time_and_missing_metadata_checked_without_requirements(self):
        for edge, issue in [(replace(self.data[0], scope="other"), "path_scope_conflict"),
                            (replace(self.data[0], time_window=(11, 12)), "path_time_conflict"),
                            (replace(self.data[0], time_window=None), "time_unknown"),
                            (replace(self.data[0], scope=None), "scope_unknown")]:
            with self.subTest(issue=issue):
                result = validate_path_meeting(self.demand, [edge])
                self.assertEqual(result["status"], "insufficient")
                self.assertIn(issue, result["issues"])

    def test_huge_integer_edge_time_is_safe(self):
        huge = 10**400
        edge = replace(self.data[0], time_window=(huge, huge))
        result = validate_path_meeting(self.demand, [edge])
        self.assertEqual(result["status"], "insufficient")
        self.assertIn("invalid_polarity_or_time", result["issues"])
        json.dumps(result, allow_nan=False)

    def test_huge_integer_requirement_time_is_safe(self):
        huge = 10**400
        need = PathRequirement("intermediate", "product", "yields", time_window=(huge, huge))
        result = validate_path_meeting(self.demand, self.data, requirements=[need])
        self.assertEqual(result["status"], "insufficient")
        self.assertIn("invalid_requirement", result["issues"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
