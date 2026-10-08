"""Optional, bounded offline exponential-action and typed-path diagnostics.

No graph writes, model calls, ranking changes, or dense graph matrix occur here.
Arnoldi projects exp(t A)v onto a small Krylov space (Sidje, TOMS 1998).
Floating-point convergence observations are not certified error guarantees.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Iterable


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # Integers beyond floating-point range are not in this numeric domain.
        return False


@dataclass(frozen=True)
class CommunicabilityConfig:
    enabled: bool = False
    scale: float = 1.0
    normalization: str = "none"
    tolerance: float = 1e-10
    breakdown_tolerance: float = 1e-13
    max_nodes: int = 4096
    max_arcs: int = 16384
    max_bundle_rounds: int = 16
    max_evidence_ids_per_edge: int = 1024
    max_evidence_ids_total: int = 32768
    max_krylov: int = 32
    max_matvecs: int = 32
    max_vector_elements: int = 262144
    max_projected_entries: int = 4096
    max_projection_terms: int = 96
    max_seconds: float = 0.5

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("communicability enabled must be a bool")
        if self.normalization not in ("none", "max_row_sum"):
            raise ValueError("unknown experimental normalization")
        for name in ("scale", "tolerance", "breakdown_tolerance", "max_seconds"):
            value = getattr(self, name)
            if not _finite_number(value):
                raise ValueError(f"{name} must be finite")
            if value < 0 or (name != "scale" and value == 0):
                raise ValueError(f"{name} is out of range")
        for name in ("max_nodes", "max_arcs", "max_bundle_rounds", "max_krylov", "max_matvecs",
                     "max_evidence_ids_per_edge", "max_evidence_ids_total",
                     "max_vector_elements", "max_projected_entries", "max_projection_terms"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


class _BudgetStop(Exception):
    pass


def _digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _norm(values):
    return math.hypot(*values)


def _dot(a, b):
    return math.fsum(x * y for x, y in zip(a, b))


def _tail_bound(x, first):
    """sum(k>=first) x**k/k! bound in exact arithmetic, if ratio < 1."""
    if x == 0:
        return 0.0
    if x / (first + 1) >= 1:
        return None
    try:
        value = math.exp(first * math.log(x) - math.lgamma(first + 1))
        bound = value / (1 - x / (first + 1))
        return bound if math.isfinite(bound) else None
    except OverflowError:
        return None


def _projected_action(h, scale, tolerance, max_terms, check):
    """Taylor action on bounded m by m H; does not construct exp(H)."""
    m = len(h)
    x = abs(scale) * max(math.fsum(abs(x) for x in row) for row in h)
    result = [1.0] + [0.0] * (m - 1)
    term = result[:]
    for k in range(1, max_terms + 1):
        check()
        term = [scale * _dot(row, term) / k for row in h]
        result = [a + b for a, b in zip(result, term)]
        if not all(math.isfinite(v) for v in result):
            raise ArithmeticError("nonfinite projected action")
        tail = _tail_bound(x, k + 1)
        if tail is not None and tail <= tolerance:
            return result, k, tail
    raise _BudgetStop("projection_terms")


def sparse_expv(node_count: int, arcs: Iterable[tuple[int, int, float]],
                vector: Iterable[float], *, config: CommunicabilityConfig | None = None,
                deadline: float | None = None,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """Compute exp(scale A) v; arc (src,dst,w) means A[dst,src]=w.

    Only sparse arcs, O(n m) basis storage and O(m²) projection storage exist.
    Unknown/budget results expose their approximation as unaccepted diagnostics.
    """
    cfg = config or CommunicabilityConfig()
    if not cfg.enabled:
        return {"status": "disabled", "accepted": False, "matvecs": 0,
                "values": [], "dense_graph_entries": 0}
    if deadline is not None and not _finite_number(deadline):
        return {"status": "unknown", "accepted": False, "reason": "invalid_deadline",
                "matvecs": 0, "values": [], "dense_graph_entries": 0}
    started = clock()
    stop_at = min(started + cfg.max_seconds, deadline) if deadline is not None else started + cfg.max_seconds
    report = {"status": "unknown", "accepted": False, "values": [], "matvecs": 0,
              "krylov_dimension": 0, "projected_entries": 0,
              "dense_graph_entries": 0, "certified_total_error_bound": None,
              "normalization": cfg.normalization, "requested_scale": cfg.scale,
              "floating_point_error_included": False}

    def check():
        if clock() >= stop_at:
            raise _BudgetStop("seconds")

    try:
        check()
        if type(node_count) is not int or node_count < 0:
            raise ValueError("invalid node count")
        if node_count > cfg.max_nodes:
            raise _BudgetStop("nodes")
        m_limit = min(node_count, cfg.max_krylov, cfg.max_matvecs)
        # Reserve basis, input, norm work, residual, current/previous results,
        # and temporary vector allocations during orthogonalization/checking.
        if node_count * (m_limit + 10) > cfg.max_vector_elements:
            raise _BudgetStop("vector_elements")
        projected_reserve = (m_limit + 1) * m_limit + m_limit * m_limit
        if projected_reserve > cfg.max_projected_entries:
            raise _BudgetStop("projected_entries")
        v = []
        for value in vector:
            check()
            if len(v) >= node_count:
                raise ValueError("vector size mismatch")
            if not _finite_number(value):
                raise ValueError("invalid vector value")
            v.append(float(value))
        if len(v) != node_count:
            raise ValueError("vector size mismatch")
        sparse = []
        row_sums, column_sums = [0.0] * node_count, [0.0] * node_count
        for src, dst, weight in arcs:
            check()
            if len(sparse) >= cfg.max_arcs:
                raise _BudgetStop("arcs")
            if type(src) is not int or type(dst) is not int or not (0 <= src < node_count and 0 <= dst < node_count):
                raise ValueError("invalid arc endpoint")
            if not _finite_number(weight):
                raise ValueError("invalid arc weight")
            sparse.append((src, dst, float(weight)))
            row_sums[dst] += abs(weight)
            column_sums[src] += abs(weight)
        row_norm = max(row_sums, default=0.0)
        column_norm = max(column_sums, default=0.0)
        if not math.isfinite(row_norm) or not math.isfinite(column_norm):
            raise ArithmeticError("nonfinite operator norm")
        divisor = max(1.0, row_norm) if cfg.normalization == "max_row_sum" else 1.0
        scale = cfg.scale / divisor
        report.update({"node_count": node_count, "arc_count": len(sparse),
                       "operator_row_norm": row_norm, "operator_column_norm": column_norm,
                       "normalization_divisor": divisor, "effective_scale": scale,
                       "reserved_projected_entries": projected_reserve,
                       "reserved_vector_elements": node_count * (m_limit + 10)})
        beta = _norm(v)
        check()
        if not math.isfinite(beta):
            raise ArithmeticError("nonfinite vector norm")
        if beta == 0 or scale == 0 or not sparse:
            check()
            report.update(status="converged", accepted=True, values=v,
                          convergence_reason="identity_or_zero", exact_arithmetic_truncation_bound=0.0)
            return report
        basis = [[x / beta for x in v]]
        h = [[0.0] * m_limit for _ in range(m_limit + 1)]
        previous = None
        for j in range(m_limit):
            check()
            w = [0.0] * node_count
            for k, (src, dst, weight) in enumerate(sparse):
                if k % 256 == 0:
                    check()
                w[dst] += weight * basis[j][src]
            report["matvecs"] += 1
            # Modified Gram-Schmidt with a second pass for loss of orthogonality.
            for _ in range(2):
                for i in range(j + 1):
                    check()
                    coefficient = _dot(basis[i], w)
                    h[i][j] += coefficient
                    w = [a - coefficient * b for a, b in zip(w, basis[i])]
            residual = _norm(w)
            if not math.isfinite(residual):
                raise ArithmeticError("nonfinite Arnoldi residual")
            h[j + 1][j] = residual
            m = j + 1
            projected = [row[:m] for row in h[:m]]
            coefficients, terms, projected_bound = _projected_action(
                projected, scale, cfg.tolerance / max(1.0, beta) / math.sqrt(m) / 8,
                cfg.max_projection_terms, check)
            values = []
            for i in range(node_count):
                if i % 64 == 0:
                    check()
                values.append(beta * math.fsum(basis[k][i] * coefficients[k] for k in range(m)))
            if not all(math.isfinite(x) for x in values):
                raise ArithmeticError("nonfinite Krylov action")
            delta = _norm([x - y for x, y in zip(values, previous)]) if previous is not None else None
            h_row = max(math.fsum(abs(x) for x in row) for row in projected)
            h_col = max(math.fsum(abs(projected[i][k]) for i in range(m)) for k in range(m))
            tail_a = _tail_bound(abs(scale) * math.sqrt(row_norm) * math.sqrt(column_norm), m)
            tail_h = _tail_bound(abs(scale) * math.sqrt(h_row) * math.sqrt(h_col), m)
            # Polynomial exactness through degree m-1 gives this conservative
            # truncation bound in exact arithmetic. sqrt(m) bounds ||V||₂.
            truncation = beta * (tail_a + math.sqrt(m) * tail_h) if tail_a is not None and tail_h is not None else None
            if truncation is not None and not math.isfinite(truncation):
                truncation = None
            defect = abs(scale * beta * residual * coefficients[-1])
            if not math.isfinite(defect):
                defect = None
            orthogonality = 0.0
            for i in range(m):
                for k in range(m):
                    check()
                    orthogonality = max(orthogonality,
                        abs(_dot(basis[i], basis[k]) - (1.0 if i == k else 0.0)))
            projection_error = beta * math.sqrt(m) * projected_bound
            if not math.isfinite(projection_error):
                projection_error = None
            total_exact_arithmetic_bound = (truncation + projection_error
                if truncation is not None and projection_error is not None else None)
            if total_exact_arithmetic_bound is not None and not math.isfinite(total_exact_arithmetic_bound):
                total_exact_arithmetic_bound = None
            near_breakdown = abs(scale) * residual <= cfg.breakdown_tolerance
            exact_breakdown = residual == 0.0
            report.update(values=values, krylov_dimension=m, projected_entries=m * m,
                          dense_projection_entries_in_use=(m_limit + 1) * m_limit + m * m,
                          arnoldi_residual=residual, defect_estimate=defect,
                          successive_projection_delta=delta, projected_taylor_terms=terms,
                          projected_taylor_tail_bound=projected_bound,
                          projected_action_error_bound=projection_error,
                          exact_arithmetic_truncation_bound=truncation,
                          total_exact_arithmetic_error_bound=total_exact_arithmetic_bound,
                          near_breakdown_observed=near_breakdown,
                          basis_orthogonality_max_error=orthogonality)
            # A small residual alone does not bound absolute error: beta can
            # amplify it. Nonzero residuals continue unless the beta-aware
            # polynomial/projection bound satisfies the absolute tolerance.
            accept_zero = exact_breakdown and projection_error is not None and projection_error <= cfg.tolerance
            accept_bound = total_exact_arithmetic_bound is not None and total_exact_arithmetic_bound <= cfg.tolerance
            if accept_zero or accept_bound:
                check()
                if orthogonality > max(cfg.tolerance, 1e-12):
                    report["reason"] = "orthogonality_unknown"
                    return report
                report.update(status="converged", accepted=True,
                              convergence_reason="computed_zero_arnoldi_residual" if accept_zero else "exact_arithmetic_tail_criterion")
                return report
            # Drop this projection before allocating the next one, so the
            # simultaneous H + projected-H reserve is the actual matrix cap.
            del projected
            previous = values
            if j + 1 < m_limit:
                basis.append([x / residual for x in w])
        report["reason"] = "krylov_or_matvec_limit"
    except _BudgetStop as exc:
        report.update(status="budget_stop", reason=str(exc))
    except (ValueError, TypeError, ArithmeticError, OverflowError) as exc:
        report.update(status="unknown", reason=type(exc).__name__)
    finally:
        report["elapsed_seconds"] = max(0.0, clock() - started)
    return report


def _evidence_ids(value):
    if not isinstance(value, (list, tuple)) or not value:
        return ()
    if not all((type(x) is int and x > 0) or (isinstance(x, str) and 0 < len(x) <= 200) for x in value):
        return ()
    return tuple(value)


def _graph_metadata(audit):
    """Read native top-level scope and selection mode; explicit legacy only."""
    if not isinstance(audit, Mapping) or not isinstance(audit.get("request"), Mapping) or not isinstance(audit.get("selection"), Mapping):
        raise ValueError("missing graph metadata")
    request, selection = audit["request"], audit["selection"]
    scopes = [value for mapping, key in ((audit, "scope"), (request, "scope"))
              if key in mapping for value in (mapping[key],)]
    if not scopes or any(not isinstance(value, str) or not 0 < len(value) <= 200 for value in scopes):
        raise ValueError("missing or invalid graph scope")
    if len(set(scopes)) != 1:
        raise ValueError("contradictory graph scope metadata")
    modes = [value for mapping, key in ((selection, "ranking_mode"), (request, "ranking_mode"))
             if key in mapping for value in (mapping[key],)]
    if not modes or any(value not in ("static", "dynamic") for value in modes):
        raise ValueError("missing or invalid explicit graph ranking mode")
    if len(set(modes)) != 1:
        raise ValueError("contradictory graph ranking mode metadata")
    return scopes[0], modes[0]


def _active_bundle_metadata(value, cfg, check):
    """Project bounded round headers/graph audits; do not replay body bindings."""
    if value.get("schema") != "active_requery_evidence_v1" or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("unsupported evidence bundle schema or version")
    scope = value.get("scope")
    if not isinstance(scope, str) or not 0 < len(scope) <= 200:
        raise ValueError("invalid evidence bundle scope")
    rounds = value.get("rounds")
    if type(rounds) is not list or not rounds:
        raise ValueError("evidence bundle needs frozen rounds")
    if len(rounds) > cfg.max_bundle_rounds:
        raise _BudgetStop("bundle_rounds")
    audits, headers, requests = [], [], set()
    for index, item in enumerate(rounds):
        check()
        if not isinstance(item, Mapping) or type(item.get("round_index")) is not int or item["round_index"] != index:
            raise ValueError("invalid evidence round order")
        request_id, planning_call_id = item.get("request_id"), item.get("planning_call_id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 512 or request_id in requests:
            raise ValueError("invalid or repeated evidence request identity")
        if ((index == 0 and planning_call_id is not None) or (index > 0 and
                (not isinstance(planning_call_id, str) or not 0 < len(planning_call_id) <= 512))):
            raise ValueError("invalid evidence planning identity")
        record = item.get("record")
        if not isinstance(record, Mapping) or record.get("scope") != scope:
            raise ValueError("missing or cross-scope evidence record")
        if index > 0 and record.get("event_id") != request_id:
            raise ValueError("evidence event/request identity mismatch")
        audit = record.get("graph_audit")
        if (not isinstance(audit, Mapping)
                or not isinstance(audit.get("selection"), Mapping)
                or not isinstance(audit["selection"].get("edge_statistics"), (list, tuple))
                or not isinstance(audit.get("match"), Mapping)
                or not isinstance(audit["match"].get("direct_hits", ()), (list, tuple))):
            raise ValueError("missing or cross-scope graph metadata")
        graph_scope, graph_mode = _graph_metadata(audit)
        if graph_scope != scope or graph_mode != "static":
            raise ValueError("cross-scope or non-static evidence graph")
        record_sha = item.get("record_sha256")
        if record_sha is not None and (not isinstance(record_sha, str) or len(record_sha) != 64
                or any(x not in "0123456789abcdef" for x in record_sha)):
            raise ValueError("invalid declared record hash")
        requests.add(request_id)
        audits.append(record)
        headers.append({"round_index": index, "request_id": request_id,
                        "planning_call_id": planning_call_id, "record_sha256": record_sha})
    return audits, headers


def analyze_frozen_graph(retrieval_or_bundle, *, config: CommunicabilityConfig | None = None,
                         seed_nodes=None, deadline: float | None = None) -> dict:
    """Read frozen edge metadata only; never fetch records or infer relations.

    Accepts a graph audit, frozen retrieval, list/tuple, or an active evidence
    bundle's frozen rounds. This is a metadata projection, not bundle replay.
    A scoped neighborhood stays scoped: omitted communicability is not certified.
    """
    cfg = config or CommunicabilityConfig()
    receipt = {"schema": "communicability_offline_v1", "enabled": cfg.enabled,
               "graph_semantics": "undirected_npmi_cooccurrence",
               "index_directions_are_semantic_relations": False,
               "changes_base_weights": False, "changes_selection": False,
               "semantic_status": "insufficient_relation_semantics",
               "hypothesis_status": "pending_unverified", "proof": False}
    if not cfg.enabled:
        receipt.update(status="disabled", numerical={"matvecs": 0}, edges=[])
        return receipt
    if deadline is not None and not _finite_number(deadline):
        receipt.update(status="unknown", reason="invalid_deadline", numerical={"matvecs": 0})
        return receipt
    started = time.monotonic()
    stop_at = min(started + cfg.max_seconds, deadline) if deadline is not None else started + cfg.max_seconds
    try:
        if isinstance(retrieval_or_bundle, Mapping) and ("schema" in retrieval_or_bundle or "rounds" in retrieval_or_bundle):
            def check_bundle():
                if time.monotonic() >= stop_at:
                    raise _BudgetStop("seconds")
            audits, headers = _active_bundle_metadata(retrieval_or_bundle, cfg, check_bundle)
            receipt.update(input_mode="active_requery_bundle", rounds_metadata=headers,
                           bundle_binding_validation="upstream_only_not_replayed",
                           bundle_metadata_only=True)
        else:
            audits = retrieval_or_bundle if isinstance(retrieval_or_bundle, (list, tuple)) else (retrieval_or_bundle,)
            receipt["input_mode"] = "frozen_graph_or_retrieval"
        if len(audits) > cfg.max_arcs:
            raise _BudgetStop("bundle_items")
        edges, scopes, source_scopes, inferred_seeds = {}, set(), set(), set()
        evidence_count = 0
        for retrieval in audits:
            if time.monotonic() >= stop_at:
                raise _BudgetStop("seconds")
            if not isinstance(retrieval, Mapping):
                raise ValueError("frozen audit must be a mapping")
            audit = retrieval.get("graph_audit", retrieval)
            if not isinstance(audit, Mapping):
                raise ValueError("graph audit must be a mapping")
            selection = audit["selection"]
            if not isinstance(selection, Mapping):
                raise ValueError("graph selection must be a mapping")
            scopes.add(selection.get("edge_statistics_scope", "full_frozen_graph"))
            graph_scope, graph_mode = _graph_metadata(audit)
            if "graph_audit" in retrieval and "scope" in retrieval and retrieval["scope"] != graph_scope:
                raise ValueError("retrieval/graph scope mismatch")
            source_scopes.add(graph_scope)
            if len(source_scopes) > 1:
                raise ValueError("cannot mix source scopes")
            if graph_mode != "static":
                raise ValueError("static NPMI audit required")
            if seed_nodes is None:
                for node in audit.get("match", {}).get("direct_hits", ()):
                    if time.monotonic() >= stop_at:
                        raise _BudgetStop("seconds")
                    if len(inferred_seeds) >= cfg.max_nodes:
                        raise _BudgetStop("seed_nodes")
                    if not isinstance(node, str) or not (0 < len(node) <= 200):
                        raise ValueError("invalid inferred seed")
                    inferred_seeds.add(node)
            for edge in selection["edge_statistics"]:
                if time.monotonic() >= stop_at:
                    raise _BudgetStop("seconds")
                if not isinstance(edge, Mapping):
                    raise ValueError("edge must be a mapping")
                a, b, weight = edge["a"], edge["b"], edge["static_score"]
                raw_ids = edge.get("source_record_ids")
                if isinstance(raw_ids, (list, tuple)) and len(raw_ids) > cfg.max_evidence_ids_per_edge:
                    raise _BudgetStop("evidence_ids_per_edge")
                ids = _evidence_ids(raw_ids)
                if not isinstance(a, str) or not isinstance(b, str) or not (0 < len(a) <= 200 and 0 < len(b) <= 200) or a >= b:
                    raise ValueError("noncanonical cooccurrence edge")
                if not _finite_number(weight) or weight < 0 or weight > 1:
                    raise ValueError("invalid static NPMI weight")
                if weight == 0:
                    continue
                row = {"from": a, "to": b, "relation": "cooccurs",
                       "direction": "undirected", "weight": float(weight),
                       "evidence_ids": list(ids), "semantic_relation": None}
                row["edge_id"] = _digest(row)
                pair = (a, b)
                if pair in edges and edges[pair] != row:
                    raise ValueError("conflicting frozen edge versions")
                if pair not in edges and 2 * (len(edges) + 1) > cfg.max_arcs:
                    raise _BudgetStop("arcs")
                if pair not in edges:
                    evidence_count += len(ids)
                    if evidence_count > cfg.max_evidence_ids_total:
                        raise _BudgetStop("evidence_ids_total")
                edges[pair] = row
        nodes = sorted({node for pair in edges for node in pair})
        if len(nodes) > cfg.max_nodes:
            raise _BudgetStop("nodes")
        index = {node: i for i, node in enumerate(nodes)}
        seed_values = inferred_seeds if seed_nodes is None else seed_nodes
        seeds = set()
        for node in seed_values:
            if time.monotonic() >= stop_at:
                raise _BudgetStop("seconds")
            if len(seeds) >= cfg.max_nodes:
                raise _BudgetStop("seed_nodes")
            if not isinstance(node, str) or not (0 < len(node) <= 200):
                raise ValueError("invalid seed")
            seeds.add(node)
        valid = sorted(seeds & index.keys())
        receipt.update(edges=list(edges.values()), edge_scope=sorted(scopes),
                       frozen_edge_identity=_digest(list(edges.values())),
                       copied_evidence_ids=evidence_count,
                       provenance_complete=all(row["evidence_ids"] for row in edges.values()),
                       seeds=valid, seed_nodes_absent=sorted(seeds - index.keys()))
        if not valid:
            if time.monotonic() >= stop_at:
                raise _BudgetStop("seconds")
            receipt.update(status="empty", numerical={"matvecs": 0, "accepted": False}, scores=[])
            return receipt
        arcs = []
        for (a, b), edge in edges.items():
            arcs.extend(((index[a], index[b], edge["weight"]),
                         (index[b], index[a], edge["weight"])))
        v = [1.0 / len(valid) if node in seeds else 0.0 for node in nodes]
        result = sparse_expv(len(nodes), arcs, v, config=cfg, deadline=stop_at)
        receipt.update(status=result["status"], numerical=result,
                       scores=[{"node": node, "value": value} for node, value in zip(nodes, result["values"])])
        if time.monotonic() >= stop_at:
            result.update(status="budget_stop", accepted=False, reason="seconds_after_numerical")
            receipt["status"] = "budget_stop"
        # Association scores cannot supply typed relation semantics, even when
        # numerical action converged and all edge provenance is present.
        return receipt
    except _BudgetStop as exc:
        receipt.update(status="budget_stop", reason=str(exc), numerical={"matvecs": 0})
    except (KeyError, TypeError, ValueError) as exc:
        receipt.update(status="unknown", reason=type(exc).__name__, numerical={"matvecs": 0})
    return receipt


@dataclass(frozen=True)
class TypedEdge:
    source: str
    target: str
    relation: str
    evidence_ids: tuple[str | int, ...]
    polarity: str = "positive"
    time_window: tuple[float, float] | None = None
    scope: str | None = None


@dataclass(frozen=True)
class PathRequirement:
    source: str
    target: str
    relation: str
    polarity: str = "positive"
    time_window: tuple[float, float] | None = None
    scope: str | None = None


def _time_valid(value):
    if value is None:
        return True
    if not isinstance(value, tuple) or len(value) != 2:
        return False
    return all(_finite_number(x) for x in value) and value[0] <= value[1]


def _overlap(a, b):
    return a is None or b is None or max(a[0], b[0]) <= min(a[1], b[1])


def validate_path_meeting(demand_path: Iterable[TypedEdge], data_path: Iterable[TypedEdge], *,
                          requirements: Iterable[PathRequirement] = (), max_edges: int = 16) -> dict:
    """Validate explicitly typed synthetic paths; never assert entailment.

    Paths meet at demand[-1].target == data[0].source and form one directed
    chain. Requirements constrain individual edges, not inferred relations.
    Evidence IDs establish declared traceability, not source truth/semantics.
    """
    result = {"schema": "typed_path_meeting_v1", "status": "insufficient",
              "proof": False, "source_truth_verified": False, "issues": [], "edges": []}
    if type(max_edges) is not int or max_edges <= 0:
        raise ValueError("invalid path limit")
    def bounded(values):
        out = []
        for item in values:
            if len(out) >= max_edges:
                raise _BudgetStop("path_edges")
            out.append(item)
        return out
    try:
        demand, data, needs = bounded(demand_path), bounded(data_path), bounded(requirements)
        edges = demand + data
        if len(edges) > max_edges:
            raise _BudgetStop("path_edges")
        if not demand or not data:
            result["issues"].append("empty_path")
        invalid_metadata = False
        for edge in edges:
            if not isinstance(edge, TypedEdge):
                result["issues"].append("untyped_edge")
                invalid_metadata = True
                continue
            valid = True
            if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (edge.source, edge.target, edge.relation)):
                result["issues"].append("invalid_identity_or_relation")
                valid = False
            if edge.polarity not in ("positive", "negative") or not _time_valid(edge.time_window):
                result["issues"].append("invalid_polarity_or_time")
                valid = False
            if edge.scope is not None and (not isinstance(edge.scope, str) or not 0 < len(edge.scope) <= 200):
                result["issues"].append("invalid_scope")
                valid = False
            if not isinstance(edge.evidence_ids, (list, tuple)) or len(edge.evidence_ids) > max_edges or not _evidence_ids(edge.evidence_ids):
                result["issues"].append("missing_or_invalid_evidence")
                valid = False
            if not valid:
                invalid_metadata = True
                continue
            if edge.time_window is None:
                result["issues"].append("time_unknown")
            if edge.scope is None:
                result["issues"].append("scope_unknown")
            result["edges"].append({"from": edge.source, "to": edge.target, "relation": edge.relation,
                                    "polarity": edge.polarity, "time_window": edge.time_window,
                                    "scope": edge.scope, "evidence_ids": list(edge.evidence_ids)})
        # Reject invalid typed metadata before any overlap or indexing. It
        # must also stay out of the JSON-safe provenance receipt.
        if invalid_metadata:
            result["issues"] = sorted(set(result["issues"]))
            return result
        known_scopes = {edge.scope for edge in edges if edge.scope is not None}
        if len(known_scopes) > 1:
            result["issues"].append("path_scope_conflict")
        windows = [edge.time_window for edge in edges if edge.time_window is not None]
        if windows and max(window[0] for window in windows) > min(window[1] for window in windows):
            result["issues"].append("path_time_conflict")
        if any(a.target != b.source for a, b in zip(edges, edges[1:])):
            result["issues"].append("disconnected_or_direction_mismatch")
        for i, edge in enumerate(edges):
            for other in edges[i + 1:]:
                same_pair = edge.source == other.source and edge.target == other.target
                same_scope = edge.scope is None or other.scope is None or edge.scope == other.scope
                if same_pair and same_scope and _overlap(edge.time_window, other.time_window):
                    if edge.relation == other.relation and edge.polarity != other.polarity:
                        result["issues"].append("contradictory_polarity")
        for need in needs:
            if (not isinstance(need, PathRequirement) or not _time_valid(need.time_window)
                    or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in (need.source, need.target, need.relation))
                    or need.polarity not in ("positive", "negative")
                    or (need.scope is not None and (not isinstance(need.scope, str) or not 0 < len(need.scope) <= 200))):
                result["issues"].append("invalid_requirement")
                continue
            same_pair = [edge for edge in edges if edge.source == need.source and edge.target == need.target]
            if not same_pair:
                reverse = any(edge.source == need.target and edge.target == need.source for edge in edges)
                result["issues"].append("direction_mismatch" if reverse else "required_edge_missing")
                continue
            matched = [edge for edge in same_pair if edge.relation == need.relation]
            if not matched:
                result["issues"].append("relation_mismatch")
                continue
            matched_polarity = [edge for edge in matched if edge.polarity == need.polarity]
            if not matched_polarity:
                result["issues"].append("negation_mismatch")
            matched_scope = [edge for edge in matched_polarity if need.scope is None or edge.scope == need.scope]
            if need.scope is not None and not matched_scope:
                result["issues"].append("scope_mismatch_or_unknown")
            if need.time_window is not None and not any(edge.time_window is not None
                    and edge.time_window[0] <= need.time_window[0] and edge.time_window[1] >= need.time_window[1]
                    for edge in matched_scope):
                result["issues"].append("time_mismatch_or_unknown")
        result["issues"] = sorted(set(result["issues"]))
        if not result["issues"]:
            result.update(status="pending_hypothesis", meeting_node=demand[-1].target,
                          traceability="declared_evidence_ids_only")
    except _BudgetStop as exc:
        result.update(status="budget_stop", issues=[str(exc)])
    except (TypeError, ValueError, IndexError, KeyError):
        result["issues"].append("invalid_typed_metadata")
    return result
