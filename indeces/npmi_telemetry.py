"""Query-local execution counters, separate from immutable graph evidence."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import math
import time


_CURRENT = ContextVar("indeces_npmi_retrieval_telemetry", default=None)
_STAGES = ("precomputed_lookup", "neighbor_ordering", "candidate_scoring", "static_rebuild")
_COUNTERS = ("precomputed_edges_loaded", "precomputed_weight_reads",
             "neighbor_ordering_weight_uses", "candidate_edge_weight_uses",
             "scored_candidates", "formula_evaluation_attempts", "formula_evaluations")


class NPMIRetrievalTelemetry:
    def __init__(self):
        self.path = "not_entered"
        self.ranking_mode = None
        self.retrieval_calls = 0
        self.counters = dict.fromkeys(_COUNTERS, 0)
        self.stages = {name: {"calls": 0, "completed_calls": 0, "seconds": 0.0}
                       for name in _STAGES}
        self.status = "not_started"
        self.error_type = None
        self.elapsed_seconds = 0.0

    def __enter__(self):
        self._token = _CURRENT.set(self)
        self._started = time.perf_counter()
        self._suspended_seconds = 0.0
        self.status = "running"
        return self

    def __exit__(self, error_type, error, traceback):
        self.elapsed_seconds = max(0.0, time.perf_counter() - self._started - self._suspended_seconds)
        self.status = "failed" if error_type else "completed"
        self.error_type = error_type.__name__ if error_type else None
        _CURRENT.reset(self._token)

    @contextmanager
    def suspended(self):
        """Exclude a separately awaited selector from local retrieval work."""
        token = _CURRENT.set(None)
        started = time.perf_counter()
        try:
            yield
        finally:
            self._suspended_seconds += time.perf_counter() - started
            _CURRENT.reset(token)

    def receipt(self):
        supported = self.ranking_mode in (None, "static")
        used = any(self.counters[k] for k in (
            "precomputed_weight_reads", "neighbor_ordering_weight_uses", "candidate_edge_weight_uses"))
        return {"version": 1, "status": self.status, "error_type": self.error_type,
                "coverage": "static_retrieval_v1", "path": self.path,
                "ranking_mode": self.ranking_mode, "retrieval_calls": self.retrieval_calls,
                "precomputed_npmi_used": used if supported else None,
                "formula_recomputed": self.counters["formula_evaluations"] > 0,
                "counters": deepcopy(self.counters), "stages": deepcopy(self.stages),
                "elapsed_seconds": self.elapsed_seconds}


def retrieval_path(ranking_mode):
    trace = _CURRENT.get()
    if trace is not None:
        trace.retrieval_calls += 1
        trace.ranking_mode = ranking_mode
        trace.path = ("keyed_static_neighborhood_v1" if ranking_mode == "static"
                      else "legacy_outside_coverage")


def count(name, amount=1):
    trace = _CURRENT.get()
    if trace is not None:
        trace.counters[name] += amount


@contextmanager
def stage(name):
    trace = _CURRENT.get()
    if trace is None:
        yield
        return
    item = trace.stages[name]
    item["calls"] += 1
    started = time.perf_counter()
    try:
        yield
    except BaseException:
        raise
    else:
        item["completed_calls"] += 1
    finally:
        item["seconds"] += time.perf_counter() - started


def validate_receipt(receipt, *, graph=None):
    """Check recorded counters and, when available, the bound graph receipt."""
    def require(condition, message):
        if not condition:
            raise ValueError("NPMI telemetry " + message)

    require(isinstance(receipt, dict) and type(receipt.get("version")) is int
            and receipt["version"] == 1, "version invalid")
    require(receipt.get("coverage") == "static_retrieval_v1", "coverage invalid")
    require(receipt.get("status") in ("completed", "failed"), "status invalid")
    counters, stages = receipt.get("counters"), receipt.get("stages")
    require(isinstance(counters, dict) and set(counters) == set(_COUNTERS), "counters invalid")
    require(all(type(v) is int and v >= 0 for v in counters.values()), "counter value invalid")
    require(counters["formula_evaluations"] <= counters["formula_evaluation_attempts"], "formula counts invalid")
    require(isinstance(stages, dict) and set(stages) == set(_STAGES), "stages invalid")
    for item in stages.values():
        require(isinstance(item, dict) and set(item) == {"calls", "completed_calls", "seconds"}, "stage invalid")
        require(type(item["calls"]) is int and type(item["completed_calls"]) is int
                and 0 <= item["completed_calls"] <= item["calls"], "stage calls invalid")
        require(type(item["seconds"]) in (int, float) and math.isfinite(item["seconds"])
                and item["seconds"] >= 0, "stage duration invalid")
    elapsed = receipt.get("elapsed_seconds")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "duration invalid")
    require(type(receipt.get("retrieval_calls")) is int and receipt["retrieval_calls"] >= 0, "retrieval calls invalid")
    require(type(receipt.get("formula_recomputed")) is bool
            and receipt["formula_recomputed"] == (counters["formula_evaluations"] > 0), "formula flag mismatch")
    path, mode = receipt.get("path"), receipt.get("ranking_mode")
    require((path, mode) in (("not_entered", None), ("keyed_static_neighborhood_v1", "static"))
            or (path == "legacy_outside_coverage" and mode != "static"), "path invalid")
    used = any(counters[k] for k in (
        "precomputed_weight_reads", "neighbor_ordering_weight_uses", "candidate_edge_weight_uses"))
    if path == "legacy_outside_coverage":
        require(receipt.get("precomputed_npmi_used") is None, "unsupported usage must be unknown")
    else:
        require(type(receipt.get("precomputed_npmi_used")) is bool
                and receipt["precomputed_npmi_used"] == used, "usage flag mismatch")
    if graph is not None:
        require(receipt["status"] == "completed" and mode == graph["selection"]["ranking_mode"] == "static",
                "graph execution mismatch")
        require(path == "keyed_static_neighborhood_v1" and receipt["retrieval_calls"] == 1, "graph path mismatch")
        selection = graph["selection"]
        require(counters["precomputed_edges_loaded"] == counters["precomputed_weight_reads"]
                == len(selection["edge_statistics"]), "edge count mismatch")
        require(counters["scored_candidates"] == len(selection["ranked_candidates"]), "candidate count mismatch")
        require(counters["candidate_edge_weight_uses"]
                == sum(len(c["evidence"]) for c in selection["ranked_candidates"]), "candidate evidence count mismatch")
        require(counters["neighbor_ordering_weight_uses"]
                == 2 * sum(e["static_score"] > 0 for e in selection["edge_statistics"]), "neighbor edge count mismatch")
        for name in ("precomputed_lookup", "neighbor_ordering", "candidate_scoring"):
            require(stages[name]["calls"] == stages[name]["completed_calls"] == 1, "graph stage mismatch")
