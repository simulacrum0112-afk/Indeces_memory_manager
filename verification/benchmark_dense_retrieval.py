"""Synthetic dense-query profile and byte-equivalence benchmark.

Never reads service configuration, user knowledge, scratch, or live databases.
Run --save-baseline before a change, then --compare-baseline after the change.
The baseline directory should be outside the repository; artifacts are write-once.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import cProfile
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import pstats
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "indeces" / "memory.py"
SOURCE_BYTES = SOURCE.read_bytes()
RUN_RECORDS_SOURCE_BYTES = (ROOT / "indeces" / "run_records.py").read_bytes()
SCRATCH_SOURCE_BYTES = (ROOT / "indeces" / "scratch.py").read_bytes()
from indeces import memory  # noqa: E402
from indeces.run_records import freeze_retrieval, validate_graph_audit  # noqa: E402
from indeces.scratch import CanonicalSnapshot, canonical  # noqa: E402

if SOURCE.read_bytes() != SOURCE_BYTES:
    raise RuntimeError("memory.py changed during import")
if ((ROOT / "indeces" / "run_records.py").read_bytes() != RUN_RECORDS_SOURCE_BYTES
        or (ROOT / "indeces" / "scratch.py").read_bytes() != SCRATCH_SOURCE_BYTES):
    raise RuntimeError("audit dependency changed during import")

SCOPE = "synthetic:dense-retrieval"
EVENT = "synthetic:dense-event"
QUERY = " ".join(f"m{i:06d}" for i in range(16))
PHASES = ("records", "observe", "edges", "query_edges", "selection", "json_encoding",
          "literal_match", "snapshot", "freeze")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def write_once(path, content):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)


def synthetic_facts(size, scenario):
    """Fixed dense 120-mark component; unrelated dense components grow.

    Each fact has at most eight marks, matching the annotation contract.
    The growing case uses one component containing every mark. Randomness
    is deterministic and only controls synthetic co-occurrence topology.
    """
    if size < 120 or size % 10:
        raise ValueError("sizes must be multiples of ten and at least 120")
    component_size = 120 if scenario == "fixed_neighborhood" else size
    facts = []
    rng = random.Random(1847)
    for start in range(0, size, component_size):
        members = list(range(start, min(start + component_size, size)))
        # Four records per mark create a dense graph while keeping each
        # individual annotation within the existing 1--8 mark schema.
        for anchor in members:
            for repetition in range(4):
                chosen = ([anchor] if scenario == "growing_star_neighborhood" else
                          sorted({anchor, *rng.sample(members, min(7, len(members)))}))
                marks = [f"m{i:06d}" for i in chosen]
                text = (f"Synthetic component {start:06d}, anchor {anchor:06d}, "
                        f"record {repetition}: " + " ".join(marks)
                        + ". Synthetic evidence only. " * 3)
                facts.append({"text": text, "quote": text, "marks": marks})
    if scenario == "growing_star_neighborhood":
        # Force each direct hit to have a size-dependent incident neighborhood.
        # A tenth of marks are local neighbors; the rest remain singleton
        # background. This keeps the control short while tripling 100 -> 300
        # neighbors in the requested 1000/3000-mark comparison.
        neighbor_stop = 4 + size // 10
        for start in range(4, neighbor_stop, 4):
            marks = [f"m{i:06d}" for i in [0, 1, 2, 3, *range(start, min(start + 4, neighbor_stop))]]
            text = f"Synthetic growing star {start:06d}: " + " ".join(marks) + ". Synthetic evidence only. " * 3
            facts.append({"text": text, "quote": text, "marks": marks})
    return facts


def prepared_database(size, scenario):
    db = sqlite3.connect(":memory:")
    graph = memory.MemoryGraph(db)
    graph.add(SCOPE, "synthetic:all", "synthetic-author", synthetic_facts(size, scenario), 1.0)
    return db


def relative_profile(profile, count=22):
    stats = pstats.Stats(profile)
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, callers) in sorted(
            stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:count]:
        try:
            filename = str(Path(filename).resolve().relative_to(ROOT))
        except (ValueError, OSError):
            filename = Path(filename).name if filename.startswith(("C:", "D:")) else filename
        rows.append({"file": filename, "line": line, "function": function,
                     "primitive_calls": primitive, "calls": calls,
                     "own_seconds": own, "cumulative_seconds": cumulative})
    return rows


def related_pairs(selection, hits):
    # Preserve every edge inspected during direct-hit expansion, including
    # rejected >top-k and context-failed candidates, plus direct-hit edges.
    hit_set = set(hits)
    pairs = {(row["a"], row["b"]) for row in selection["edge_statistics"]
             if row["a"] in hit_set or row["b"] in hit_set}
    for field in ("expansion_candidates", "ranked_candidates"):
        for row in selection[field]:
            items = row["evidence"] if field == "ranked_candidates" else [row]
            pairs.update(tuple(sorted((item["from_mark"], item["mark"]))) for item in items)
    return pairs


def comparison_projection(result):
    """Only explicitly declared edge scope metadata may differ.

    Complete returned records, all observation transitions, frequency counts,
    limits, selection decisions, ranking and selected provenance are retained.
    Durable hashes necessarily change if the audit envelope changes.
    """
    result = deepcopy(result)
    audit = result["audit"]
    audit.pop("durable_payload_sha256", None)
    selection = audit["selection"]
    pairs = related_pairs(selection, audit["match"]["direct_hits"])
    selection["edge_statistics"] = [row for row in selection["edge_statistics"]
                                    if (row["a"], row["b"]) in pairs]
    selection.pop("edge_statistics_scope", None)
    return result


def expression_probe(module, source_bytes):
    """Time just the statistics expression in an isolated profile sample.

    The expression is evaluated once by a zero-argument lambda. No graph
    policy, loop bound, data, ordering or returned field changes. This probe
    never contributes to ordinary medians and is byte-compared separately.
    """
    tree = ast.parse(source_bytes)
    graph_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MemoryGraph")
    selection = deepcopy(next(node for node in graph_class.body
                              if isinstance(node, ast.FunctionDef) and node.name == "_selection"))
    measured = {"edge_statistics_expression_seconds": 0.0, "edge_statistics_expression_calls": 0}
    for node in ast.walk(selection):
        if isinstance(node, ast.Dict):
            for index, key in enumerate(node.keys):
                if isinstance(key, ast.Constant) and key.value == "edge_statistics":
                    expression = node.values[index]
                    node.values[index] = ast.copy_location(ast.Call(
                        func=ast.Name(id="_dense_statistics_probe", ctx=ast.Load()),
                        args=[ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
                                                         kw_defaults=[], defaults=[], vararg=None, kwarg=None), body=expression)],
                        keywords=[]), expression)
                    measured["edge_statistics_expression_original_line"] = expression.lineno

    def measure(function):
        started = time.perf_counter()
        try:
            return function()
        finally:
            measured["edge_statistics_expression_seconds"] += time.perf_counter() - started
            measured["edge_statistics_expression_calls"] += 1

    namespace = dict(module.__dict__, _dense_statistics_probe=measure)
    compiled = ast.fix_missing_locations(ast.Module(body=[selection], type_ignores=[]))
    exec(compile(compiled, "<synthetic-edge-statistics-expression-probe:memory.py>", "exec"), namespace)
    return namespace["_selection"], measured


def compare_artifact(expected_bytes, result_bytes):
    expected, current = json.loads(expected_bytes), json.loads(result_bytes)
    if canonical(expected["records"]) != canonical(current["records"]):
        raise ValueError("returned-record bytes differ from baseline")
    expected_projection, actual_projection = comparison_projection(expected), comparison_projection(current)
    if canonical(expected_projection) != canonical(actual_projection):
        raise ValueError("related-edge audit or unchanged audit fields differ from baseline")
    return {"returned_records_bytes_equal": True,
            "related_edges_and_unchanged_audit_bytes_equal": True,
            "returned_records_sha256": sha256(canonical(current["records"])),
            "audit_projection_sha256": sha256(canonical(actual_projection["audit"])),
            "exact_full_artifact_bytes_equal": expected_bytes == result_bytes}


def one_sample(prepared, cache, profile=False, selection_only=False, probe_statistics=False):
    db = sqlite3.connect(":memory:")
    try:
        prepared.backup(db)
        graph = memory.MemoryGraph(db)
        if cache == "steady":
            graph.retrieve(SCOPE, [], QUERY, 2.0, event_id="synthetic:seed-event", ranking_mode="static")
        elif cache == "warm":
            # No observation is used for priming. Cache warmth is the only
            # difference; the measured real event has identical DB history.
            with graph._retrieval_transaction(SCOPE):
                graph._edges(SCOPE, "static")
        counts = {table: db.execute(f"SELECT count(*) FROM {table} WHERE scope=?", (SCOPE,)).fetchone()[0]
                  for table in ("memory_records", "memory_static", "memory_support")}
        elapsed = {phase: 0.0 for phase in PHASES}
        calls = {phase: 0 for phase in PHASES}
        expression_measurement = None
        if probe_statistics:
            function, expression_measurement = expression_probe(memory, SOURCE_BYTES)
            graph._selection = types.MethodType(function, graph)

        def instrument(function, phase):
            def wrapper(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    elapsed[phase] += time.perf_counter() - started
                    calls[phase] += 1
            return wrapper

        for name, phase in (("_records", "records"), ("_observe", "observe"),
                            ("_edges", "edges"), ("_query_edges", "query_edges"),
                            ("_selection", "selection")):
            if hasattr(graph, name):
                setattr(graph, name, instrument(getattr(graph, name), phase))
        gc.collect()
        if cache == "cold":
            re.purge()
        profiler = cProfile.Profile() if profile else None
        audit = {}
        with patch.object(memory, "_hit", instrument(memory._hit, "literal_match")), \
                patch.object(memory, "_json", instrument(memory._json, "json_encoding")):
            if profiler:
                profiler.enable()
            started = time.perf_counter()
            records = graph.retrieve(SCOPE, [], QUERY, 3.0, event_id=EVENT, audit=audit, ranking_mode="static")
            retrieve_seconds = time.perf_counter() - started
            started = time.perf_counter()
            snapshot = CanonicalSnapshot(audit)
            elapsed["snapshot"] = time.perf_counter() - started
            started = time.perf_counter()
            frozen = freeze_retrieval(db, SCOPE, EVENT, QUERY, records, snapshot)
            elapsed["freeze"] = time.perf_counter() - started
            if profiler:
                profiler.disable()
        started = time.perf_counter()
        validate_graph_audit(audit, frozen)
        validation_seconds = time.perf_counter() - started
        output = canonical({"records": records, "audit": audit})
        selection = audit["selection"]
        runtime_elapsed, runtime_calls = dict(elapsed), dict(calls)
        # Independent selection-only phase reuses exactly the materialized
        # baseline index/edges. Observation and global cache building are
        # excluded here; this isolation is not the production retrieve path.
        isolated_seconds = None
        if selection_only:
            with graph._retrieval_transaction(SCOPE) as index:
                hits = audit["match"]["direct_hits"]
                edges = (graph._query_edges(SCOPE, "static", hits) if hasattr(graph, "_query_edges")
                         else graph._edges(SCOPE, "static"))
                started = time.perf_counter()
                graph._selection(index.records, hits, edges, QUERY, EVENT)
                isolated_seconds = time.perf_counter() - started
        sample = {"retrieve_seconds": retrieve_seconds,
                  "runtime_read_freeze_seconds": retrieve_seconds + elapsed["snapshot"] + elapsed["freeze"],
                  "phase_seconds_inclusive": runtime_elapsed, "phase_calls": runtime_calls,
                  "validation_seconds_outside_runtime": validation_seconds,
                  "selection_only_seconds": isolated_seconds,
                  "counts": counts, "known_marks": audit["match"]["known_marks_count"],
                  "literal_matches": len(audit["match"]["literal_matches"]),
                  "direct_hits": audit["match"]["direct_hits"],
                  "live_edges": selection["live_edge_count"],
                  "serialized_edge_statistics": len(selection["edge_statistics"]),
                  "related_edge_count": len(related_pairs(selection, audit["match"]["direct_hits"])),
                  "expansion_candidate_count": len(selection["expansion_candidates"]),
                  "expanded_marks": selection["expanded_marks"],
                  "ranked_candidate_count": len(selection["ranked_candidates"]),
                  "observation_changed_edges": len(audit["observation"]["changed_edges"]),
                  "audit_utf8_bytes": len(canonical(audit)),
                  "records_sha256": sha256(canonical(records)),
                  "audit_projection_sha256": sha256(canonical(comparison_projection(
                      {"records": records, "audit": audit})["audit"]))}
        if profiler:
            sample["cprofile_top_cumulative"] = relative_profile(profiler)
            stats = pstats.Stats(profiler)
            sample["memory_profile_own_seconds"] = [
                {"line": line, "function": name, "calls": data[1],
                 "own_seconds": data[2], "cumulative_seconds": data[3]}
                for (filename, line, name), data in sorted(
                    stats.stats.items(), key=lambda item: item[1][2], reverse=True)
                if Path(filename).name == "memory.py" or "memory.py>" in filename]
        if expression_measurement:
            sample["isolated_expression_probe"] = expression_measurement
        return sample, output
    finally:
        db.close()


def main():
    global memory, SOURCE_BYTES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 3000])
    parser.add_argument("--scenarios", nargs="+", choices=("fixed_neighborhood", "growing_neighborhood", "growing_star_neighborhood"),
                        default=["fixed_neighborhood", "growing_star_neighborhood"])
    parser.add_argument("--caches", nargs="+", choices=("cold", "warm", "steady"), default=["cold", "warm", "steady"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", action="store_true", help="additional profiled sample; excluded from medians")
    parser.add_argument("--probe-statistics", action="store_true", help="profile-only AST wrapper around edge_statistics expression")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline-ref", help="profile an exact tracked source revision without editing workspace source")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save-baseline", type=Path)
    group.add_argument("--compare-baseline", type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.repeats < 1:
        parser.error("output must not exist and repeats must be positive")
    if args.save_baseline and ROOT in [args.save_baseline.resolve(), *args.save_baseline.resolve().parents]:
        parser.error("baseline artifacts must be outside the repository")
    if args.baseline_ref:
        SOURCE_BYTES = subprocess.check_output(["git", "show", f"{args.baseline_ref}:indeces/memory.py"], cwd=ROOT)
        legacy = types.ModuleType("indeces._dense_benchmark_baseline")
        legacy.__package__ = "indeces"
        sys.modules[legacy.__name__] = legacy
        exec(compile(SOURCE_BYTES, f"<synthetic-baseline:{args.baseline_ref}:memory.py>", "exec"), legacy.__dict__)
        memory = legacy
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, encoding="utf-8").strip()
    report = {"format_version": 1, "classification": "synthetic only; no live service data",
              "source": {"head": commit, "memory_py_sha256": sha256(SOURCE_BYTES),
                         "baseline_ref": args.baseline_ref,
                         "run_records_py_sha256": sha256(RUN_RECORDS_SOURCE_BYTES),
                         "scratch_py_sha256": sha256(SCRATCH_SOURCE_BYTES),
                         "benchmark_py_sha256": sha256(Path(__file__).read_bytes())},
              "environment": {"python": sys.version, "platform": platform.platform(),
                              "sqlite": sqlite3.sqlite_version,
                              "python_hash_seed": os.environ.get("PYTHONHASHSEED")},
              "method": {"query": QUERY, "fact_marks_max": 8, "facts_per_mark": 4,
                         "fixed_neighborhood_marks": 120, "random_seed": 1847,
                         "growing_star_neighbors": "size//10 (100 at1000 marks;300 at3000 marks)",
                         "ranking_mode": "static", "ingestion_timed": False,
                         "warm_cache": "prime source/edge caches without observation; measured first event unchanged",
                         "steady_cache": "prime actual retrieval at2.0 with distinct event; measure fresh event3.0 after seeding",
                         "phase_times": "inclusive/nested; do not add together",
                         "runtime_read_freeze": "retrieve + CanonicalSnapshot + freeze_retrieval; no disk scratch write",
                         "tracked_baseline_dependencies": "--baseline-ref replays only memory.py; run_records/scratch are current imported dependencies, preserving the old full-audit validation path",
                         "limitations": "Two synthetic sizes cannot prove asymptotic complexity or service deadline compliance; observation remains full-graph baseline policy."},
              "cases": []}
    for scenario in args.scenarios:
        for size in sorted(args.sizes):
            prepared = prepared_database(size, scenario)
            try:
                for cache in args.caches:
                    key = f"{scenario}-{size}-{cache}"
                    artifact = f"{key}.canonical.json"
                    expected = (args.compare_baseline / artifact).read_bytes() if args.compare_baseline else None
                    samples, comparison = [], None
                    for repeat in range(args.repeats):
                        sample, encoded = one_sample(prepared, cache, selection_only=True)
                        if expected is None:
                            expected = encoded
                            if args.save_baseline:
                                write_once(args.save_baseline / artifact, encoded)
                        comparison = compare_artifact(expected, encoded)
                        samples.append(sample)
                    case = {"scenario": scenario, "marks": size, "cache": cache,
                            "samples": samples, "comparison": comparison,
                            "retrieve_median_seconds": statistics.median(s["retrieve_seconds"] for s in samples),
                            "runtime_read_freeze_median_seconds": statistics.median(s["runtime_read_freeze_seconds"] for s in samples),
                            "selection_only_median_seconds": statistics.median(s["selection_only_seconds"] for s in samples),
                            "phase_median_seconds_inclusive": {phase: statistics.median(
                                s["phase_seconds_inclusive"][phase] for s in samples) for phase in PHASES}}
                    if args.profile:
                        sample, encoded = one_sample(prepared, cache, profile=True,
                                                     probe_statistics=args.probe_statistics)
                        compare_artifact(expected, encoded)
                        case["profile"] = sample
                        write_once(args.output.with_name(f"{args.output.stem}.{key}.profile.json"),
                                   canonical({"source": report["source"], "case": key, "profile": sample}) + b"\n")
                    report["cases"].append(case)
                    print(json.dumps({"case": key, "retrieve_seconds": case["retrieve_median_seconds"],
                                      "runtime_read_freeze_seconds": case["runtime_read_freeze_median_seconds"],
                                      "phases": case["phase_median_seconds_inclusive"],
                                      "edges": samples[0]["live_edges"],
                                      "related_edges": samples[0]["related_edge_count"],
                                      "expansion_candidates": samples[0]["expansion_candidate_count"],
                                      "comparison": comparison}, ensure_ascii=False), flush=True)
            finally:
                prepared.close()
    write_once(args.output, canonical(report) + b"\n")
    print(json.dumps({"report": str(args.output.resolve()),
                      "baseline": str(args.save_baseline.resolve()) if args.save_baseline else None}), flush=True)


if __name__ == "__main__":
    main()
