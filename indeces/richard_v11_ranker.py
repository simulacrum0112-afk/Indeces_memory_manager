"""Frozen solver_v1_1 concept ranking; no text interpretation or runtime wiring.

Source: owner-forwarded Bota 1558657933649121420 and the inspected (never
executed/imported) v3.0.2 conformance reference. Book: Bojie Li, AI Agents in
Depth, v2.0 September 23, 2026, A4 ch4 pp127-128/PDF135-136 (contracts),
S7 ch3 pp111-113/PDF119-121 (provenance), E1/E4 ch7 pp232-233,243-246/
PDF240-241,251-254 (regression and claim limits). Guidance is not certification.
"""
from dataclasses import dataclass, field
import math
from typing import Iterable

from .richard_data import GraphData


SPEC_ID = "solver_v1_1_ranking_on_v3_concept_graph"
OUTPUT_SPEC = "pilot20 solver_v1_1 ranking on v3 concept graph"
ALPHA_RESTART = 0.5
ALPHA_STEPS = 40
BETA_ORDERS = 20
TOP_K = 10
VARIANTS = frozenset(("alpha_nomeet", "alpha_meet", "beta_nomeet", "beta_meet", "frequency"))


@dataclass(frozen=True, slots=True)
class RankedConcept:
    cid: int
    score: float | int


@dataclass(frozen=True, slots=True)
class FrozenRanker:
    """Immutable fixed-graph arithmetic, returning concept IDs, never record IDs.

    Seeds are original member IDs or existing CIDs. Empty diffusion groups and
    unknown inputs are rejected rather than assigned invented meanings. A
    known pool returns min(10, pool size) unique concepts without padding.
    """
    graph: GraphData
    _arcs: tuple[tuple[int, int, float], ...] = field(init=False, repr=False)
    degrees: tuple[float, ...] = field(init=False)
    rho: float = field(init=False)

    def __post_init__(self):
        if not isinstance(self.graph, GraphData) or not self.graph.nodes:
            raise ValueError("a nonempty reviewed GraphData is required")
        arcs = []
        degree = [0.0] * len(self.graph.nodes)
        # Preserve reference float64 scatter accumulation order: canonical
        # edge order, with both directions interleaved for every edge.
        for edge in sorted(self.graph.edges, key=lambda e: (e.a, e.b)):
            a, b = self.graph.cid_to_index[edge.a], self.graph.cid_to_index[edge.b]
            weight = float(edge.npmi)
            arcs.extend(((a, b, weight), (b, a, weight)))
            degree[a] += weight
            degree[b] += weight
        degree = tuple(value if value != 0.0 else 1.0 for value in degree)
        object.__setattr__(self, "_arcs", tuple(arcs))
        object.__setattr__(self, "degrees", degree)
        # The inspected reference applies the isolated-degree substitution
        # BEFORE taking max; preserve it even for graphs with weak edges.
        object.__setattr__(self, "rho", max(degree))

    def resolve_seeds(self, seeds: Iterable[int]) -> tuple[int, ...]:
        if isinstance(seeds, (str, bytes, dict)):
            raise ValueError("seeds must be an iterable of existing integer IDs")
        result = set()
        try:
            for value in seeds:
                if type(value) is not int or value < 1:
                    raise ValueError("seed IDs must be positive integers, not booleans")
                cid = self.graph.member_to_cid.get(value, value)
                if cid not in self.graph.cid_to_index:
                    raise ValueError(f"unknown seed ID: {value}")
                result.add(cid)
        except TypeError as error:
            raise ValueError("seeds must be an iterable of existing integer IDs") from error
        return tuple(sorted(result))

    def _diffusion(self, seeds: tuple[int, ...], algorithm: str) -> tuple[float, ...]:
        if not seeds:
            raise ValueError("diffusion requires a nonempty unique seed group")
        if algorithm not in ("alpha", "beta"):
            raise ValueError("unknown diffusion algorithm")
        count = len(self.graph.nodes)
        initial = [0.0] * count
        probability = 1.0 / len(seeds)
        for cid in seeds:
            initial[self.graph.cid_to_index[cid]] = probability
        current = initial.copy()
        if algorithm == "alpha":
            for _ in range(ALPHA_STEPS):
                accumulated = [0.0] * count
                for source, target, weight in self._arcs:
                    accumulated[target] += current[source] * weight / self.degrees[source]
                current = [ALPHA_RESTART * initial[i] + (1.0 - ALPHA_RESTART) * accumulated[i]
                           for i in range(count)]
            return tuple(current)
        result = current.copy()
        for order in range(1, BETA_ORDERS + 1):
            accumulated = [0.0] * count
            for source, target, weight in self._arcs:
                accumulated[target] += current[source] * weight / self.rho
            current = [value / order for value in accumulated]
            result = [result[i] + current[i] for i in range(count)]
        return tuple(result)

    def diffusion(self, seeds: Iterable[int], *, algorithm: str) -> tuple[float, ...]:
        """Scores in graph.index_to_cid order; isolated mass is not redistributed."""
        return self._diffusion(self.resolve_seeds(seeds), algorithm)

    def _pool(self, excluded: frozenset[int], target: str, level: str) -> tuple[int, ...]:
        if level not in ("fine", "coarse"):
            raise ValueError("level must be exactly fine or coarse")
        if not isinstance(target, str) or not target:
            raise ValueError("target must be an explicit nonempty type label")
        labels = {getattr(node, level) for node in self.graph.nodes}
        if target not in labels:
            raise ValueError(f"unknown {level} target: {target}")
        # The released coarse column encodes the parent of each fine label.
        # No inferred taxonomy, substring expansion or chemistry is applied.
        return tuple(node.cid for node in self.graph.nodes
                     if node.cid not in excluded and getattr(node, level) == target)

    def _ordered(self, pool: tuple[int, ...], scores: tuple[float | int, ...]) -> tuple[RankedConcept, ...]:
        ids = sorted(pool, key=lambda cid: (-scores[self.graph.cid_to_index[cid]], cid))[:TOP_K]
        return tuple(RankedConcept(cid, scores[self.graph.cid_to_index[cid]]) for cid in ids)

    def rank(self, seeds_A: Iterable[int], seeds_B: Iterable[int] = (), *, target: str,
             level: str, variant: str) -> tuple[RankedConcept, ...]:
        if not isinstance(variant, str) or variant not in VARIANTS:
            raise ValueError("unknown ranking variant")
        a, b = self.resolve_seeds(seeds_A), self.resolve_seeds(seeds_B)
        pool = self._pool(frozenset(a) | frozenset(b), target, level)
        if variant == "frequency":
            return self._ordered(pool, tuple(node.frequency for node in self.graph.nodes))
        algorithm, mode = variant.split("_")
        if mode == "meet":
            if not a or not b:
                raise ValueError("confluence requires two nonempty seed groups")
            left, right = self._diffusion(a, algorithm), self._diffusion(b, algorithm)
            scores = tuple(math.sqrt(x * y) for x, y in zip(left, right))
        else:
            scores = self._diffusion(tuple(sorted(set(a) | set(b))), algorithm)
        return self._ordered(pool, scores)

    def rank_case(self, item: dict) -> dict:
        """Pure conformance-shaped output, omitting meet variants when B is empty.

        A must be nonempty. Empty-A reference behavior divides by zero, so it
        is rejected explicitly rather than emulated as a ranking of NaNs.
        """
        if not isinstance(item, dict) or set(item) != {"id", "seeds_A", "seeds_B", "target", "level"}:
            raise ValueError("case requires id, seeds_A, seeds_B, target and level")
        if not isinstance(item["id"], str) or not item["id"]:
            raise ValueError("case id must be a nonempty string")
        a, b = self.resolve_seeds(item["seeds_A"]), self.resolve_seeds(item["seeds_B"])
        if not a:
            raise ValueError("case A must be nonempty; no empty-group diffusion is defined")
        union = tuple(sorted(set(a) | set(b)))
        pool = self._pool(frozenset(union), item["target"], item["level"])
        cache = {}

        def diffuse(seeds, algorithm):
            key = (algorithm, seeds)
            if key not in cache:
                cache[key] = self._diffusion(seeds, algorithm)
            return cache[key]

        rankings = {}
        for algorithm in ("alpha", "beta"):
            rankings[algorithm + "_nomeet"] = [row.cid for row in self._ordered(pool, diffuse(union, algorithm))]
            if b:
                scores = tuple(math.sqrt(x * y) for x, y in zip(diffuse(a, algorithm), diffuse(b, algorithm)))
                rankings[algorithm + "_meet"] = [row.cid for row in self._ordered(pool, scores)]
        rankings["frequency"] = [row.cid for row in self._ordered(pool, tuple(node.frequency for node in self.graph.nodes))]
        return {"id": item["id"], "seeds_A_cid": list(a), "seeds_B_cid": list(b),
                "target": item["target"], "level": item["level"], "pool_size": len(pool), "top10": rankings}


def rank_cases(graph: GraphData, items: Iterable[dict]) -> dict:
    """Return JSON-ready results; caller owns validated file I/O/serialization."""
    ranker = FrozenRanker(graph)
    return {"spec": OUTPUT_SPEC, "results": [ranker.rank_case(item) for item in items]}
