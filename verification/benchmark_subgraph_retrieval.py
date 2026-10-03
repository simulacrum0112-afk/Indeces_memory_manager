"""Synthetic neighborhood retrieval, frozen-state byte oracle, and timing.

No configuration, local knowledge, scratch, credentials, or service database
is accessed.  The old selection oracle reads the exact same frozen source and
dynamic state as the new query; it deliberately does not apply old shadow
updates.  The local-cap timing includes retrieve, snapshot, the observation
scratch write (flush/fsync), and freeze.  Independent validation and the two
post-cap scratch writes are also timed separately.  Oracle work, ingestion,
database backup/initialization, and scratch startup remain outside query timing.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indeces import memory  # noqa: E402
from indeces.run_records import digest, freeze_retrieval, validate_graph_audit  # noqa: E402
from indeces.scratch import CanonicalSnapshot, ScratchLog, canonical  # noqa: E402
from verification.benchmark_dense_retrieval import synthetic_facts  # noqa: E402

SCOPE = "synthetic:subgraph-retrieval"
QUERY = " ".join(f"m{i:06d}" for i in range(16))
EVENT = "synthetic:measured-event"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def legacy_module(ref):
    source = subprocess.check_output(["git", "show", f"{ref}:indeces/memory.py"], cwd=ROOT)
    module = types.ModuleType("indeces._subgraph_frozen_oracle")
    module.__package__ = "indeces"
    sys.modules[module.__name__] = module
    exec(compile(source, f"<frozen-oracle:{ref}:memory.py>", "exec"), module.__dict__)
    return module, source


def frozen_selection(db, graph, old, query=QUERY, event=EVENT):
    # Bypass constructor migrations and every new source/cache implementation.
    # The exact old full edge decoder and selection run on read-only DB state.
    oracle = object.__new__(old.MemoryGraph)
    oracle.connection = db
    oracle.self_marks = set(graph.self_marks)
    oracle._retrieval_index = None
    records = oracle._records(SCOPE)
    known = {mark for record in records for mark in record["marks"]} - oracle.self_marks
    literal = sorted((mark for mark in known if old._hit(mark, query)), key=lambda mark: (-len(mark), mark))
    hits = literal[:old.MAX_DIRECT]
    returned, selection = oracle._selection(records, hits, oracle._edges(SCOPE, "static"), query, event)
    selection.update(ranking_mode="static", weight_basis="static_npmi")
    for record in returned:
        record.update(ranking_mode="static", weight_basis="static_npmi")
    match = {"known_marks_count": len(known), "literal_matches": literal,
             "direct_hits": hits, "direct_limit": old.MAX_DIRECT,
             "discarded_direct_matches": literal[old.MAX_DIRECT:],
             "excluded_self_marks": sorted(oracle.self_marks)}
    # Keep only comparison evidence after the exact old full selection has
    # completed. Retaining hundreds of thousands of unrelated rows during
    # the new timed samples would create avoidable memory-pressure bias.
    return returned, strict_projection(selection, hits), match


def strict_projection(selection, hits):
    """Remove only declared audit-scope fields; preserve every local byte."""
    hit_set = set(hits)
    original = selection
    selection = {key: deepcopy(value) for key, value in original.items()
                 if key not in ("edge_statistics", "mark_frequencies")}
    statistics = [deepcopy(edge) for edge in original["edge_statistics"]
                  if edge["a"] in hit_set or edge["b"] in hit_set]
    selection["edge_statistics"] = statistics
    endpoints = {edge[key] for edge in statistics for key in ("a", "b")}
    selection["mark_frequencies"] = {mark: count for mark, count in original["mark_frequencies"].items()
                                     if mark in endpoints}
    for key in ("edge_statistics_scope", "mark_frequencies_scope", "live_edge_count_scope"):
        selection.pop(key, None)
    return selection


def compare(expected, records, audit):
    old_records, old_selection, old_match = expected
    if canonical(records) != canonical(old_records):
        raise ValueError("complete returned record bytes differ from frozen-state old selection")
    if canonical(audit["match"]) != canonical(old_match):
        raise ValueError("literal/direct hit match bytes differ from old selection")
    old_projection = strict_projection(old_selection, old_match["direct_hits"])
    new_projection = strict_projection(audit["selection"], audit["match"]["direct_hits"])
    if canonical(new_projection) != canonical(old_projection):
        differences = [key for key in set(new_projection) | set(old_projection)
                       if canonical(new_projection.get(key)) != canonical(old_projection.get(key))]
        raise ValueError(f"related edge/decision/ranking/evidence bytes differ: {sorted(differences)}")
    local = audit["selection"]
    if local.get("edge_statistics_scope") != "direct_hit_incident_v1":
        raise ValueError("new query did not declare direct-hit edge statistics scope")
    if audit.get("audit_scope") != "direct_hit_neighborhood_v1":
        raise ValueError("new query did not declare neighborhood audit scope")
    if local.get("mark_frequencies_scope") != "edge_endpoints_v1":
        raise ValueError("new query did not declare endpoint-frequency scope")
    if local["live_edge_count"] != old_selection["live_edge_count"]:
        raise ValueError("persisted full live-edge scalar differs from frozen-state old graph")
    if audit["observation"]["changed_edges"]:
        raise ValueError("static query unexpectedly changed dynamic edges")
    return {"frozen_state_complete_records_bytes_equal": True,
            "literal_matches_and_direct_hits_bytes_equal": True,
            "related_edges_decisions_ranking_evidence_bytes_equal": True,
            "records_sha256": sha256(canonical(records)),
            "selection_projection_sha256": sha256(canonical(new_projection)),
            "full_audit_envelope_equal": False,
            "oracle_policy": "exact old selection on same frozen DB state, no old _observe"}


def one_sample(prepared, old, cache, repeat, expected=None):
    db = sqlite3.connect(":memory:")
    temporary = TemporaryDirectory(prefix="indeces-synthetic-subgraph-")
    scratch = None
    try:
        prepared.backup(db)
        started = time.perf_counter()
        graph = memory.MemoryGraph(db)
        initialize_seconds = time.perf_counter() - started
        if cache == "warm":
            # Build only literal-query startup caches, without fetching the
            # measured neighborhood or applying a dynamic update.
            graph.retrieve(SCOPE, [], "synthetic unmatched primer", 2.0,
                           event_id="synthetic:warm-primer", ranking_mode="static")
        elif cache == "steady":
            graph.retrieve(SCOPE, [], QUERY, 2.0, event_id="synthetic:steady-primer", ranking_mode="static")
        if expected is None:
            expected = frozen_selection(db, graph, old)
        counts = {table: db.execute(f"SELECT count(*) FROM {table} WHERE scope=?", (SCOPE,)).fetchone()[0]
                  for table in ("memory_records", "memory_static", "memory_support")}
        dynamic_before = list(db.execute("SELECT * FROM memory_dynamic WHERE scope=? ORDER BY a,b", (SCOPE,)))
        scratch = ScratchLog(Path(temporary.name) / "synthetic-scratch",
                             clock=lambda: datetime(2026, 10, 3, 16, tzinfo=timezone.utc))
        statements = []
        vm_callbacks = 0

        def count_vm_work():
            nonlocal vm_callbacks
            vm_callbacks += 1
            return 0

        db.set_trace_callback(statements.append)
        db.set_progress_handler(count_vm_work, 100)
        gc.collect()
        if cache == "cold":
            re.purge()
        audit = {}
        started = time.perf_counter()
        records = graph.retrieve(SCOPE, [], QUERY, 3.0, event_id=EVENT, audit=audit, ranking_mode="static")
        retrieve_seconds = time.perf_counter() - started
        started = time.perf_counter()
        snapshot = CanonicalSnapshot(audit)
        snapshot_seconds = time.perf_counter() - started
        started = time.perf_counter()
        scratch.write_snapshot("memory_observation", trace_id="synthetic-measured-trace",
                               snapshots={"audit": snapshot}, audit_sha256=snapshot.sha256)
        observation_scratch_seconds = time.perf_counter() - started
        started = time.perf_counter()
        frozen = freeze_retrieval(db, SCOPE, EVENT, QUERY, records, snapshot)
        freeze_seconds = time.perf_counter() - started
        db.set_progress_handler(None, 0)
        db.set_trace_callback(None)
        started = time.perf_counter()
        validate_graph_audit(audit, frozen)
        independent_validation_seconds = time.perf_counter() - started
        # These two writes follow the local-cap check in Runtime.process.
        # Report their cost separately and additionally as a complete local
        # record-persistence boundary, without conflating it with the cap.
        started = time.perf_counter()
        frozen_hash = digest(frozen)
        scratch.write("retrieval_record", trace_id="synthetic-measured-trace", record=frozen, record_sha256=frozen_hash)
        scratch.write("knowledge_retrieved", trace_id="synthetic-measured-trace", input_marks=[], query=QUERY,
                      records=frozen["model_materials"], retrieval_sha256=frozen_hash,
                      elapsed_seconds=retrieve_seconds + snapshot_seconds + observation_scratch_seconds + freeze_seconds)
        post_cap_scratch_seconds = time.perf_counter() - started
        equality = compare(expected, records, audit)
        dynamic_after = list(db.execute("SELECT * FROM memory_dynamic WHERE scope=? ORDER BY a,b", (SCOPE,)))
        if dynamic_after != dynamic_before:
            raise ValueError("static retrieval changed stored dynamic state")
        selection = audit["selection"]
        return {"repeat": repeat, "initialize_seconds_untimed": initialize_seconds,
                "retrieve_seconds": retrieve_seconds, "snapshot_seconds": snapshot_seconds,
                "freeze_seconds": freeze_seconds,
                "runtime_read_freeze_seconds": retrieve_seconds + snapshot_seconds + freeze_seconds,
                "observation_scratch_seconds": observation_scratch_seconds,
                "runtime_local_cap_seconds": retrieve_seconds + snapshot_seconds + observation_scratch_seconds + freeze_seconds,
                "independent_validation_seconds_outside_cap": independent_validation_seconds,
                "post_cap_scratch_seconds": post_cap_scratch_seconds,
                "complete_local_records_and_validation_seconds": retrieve_seconds + snapshot_seconds + observation_scratch_seconds + freeze_seconds + independent_validation_seconds + post_cap_scratch_seconds,
                "counts": counts, "known_marks": audit["match"]["known_marks_count"],
                "local_edges": len(selection["edge_statistics"]),
                "audited_frequencies": len(selection["mark_frequencies"]),
                "expansion_candidates": len(selection["expansion_candidates"]),
                "ranked_candidates": len(selection["ranked_candidates"]),
                "audit_utf8_bytes": len(canonical(audit)),
                "runtime_sql_statement_count": len(statements),
                "runtime_sql_vm_callbacks_every_100": vm_callbacks,
                "static_query_dynamic_state_bytes_unchanged": True, "comparison": equality}
    finally:
        if scratch is not None:
            scratch.close()
        temporary.cleanup()
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="db04135")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 3000, 10000])
    parser.add_argument("--scenarios", nargs="+", choices=("fixed_neighborhood", "growing_star_neighborhood"),
                        default=["fixed_neighborhood", "growing_star_neighborhood"])
    parser.add_argument("--caches", nargs="+", choices=("cold", "warm", "steady"),
                        default=["cold", "warm", "steady"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.repeats < 1:
        parser.error("output must not exist; repeats must be positive")
    old, old_source = legacy_module(args.baseline_ref)
    source_paths = [ROOT / "indeces" / name for name in ("memory.py", "memory_index.py", "run_records.py", "scratch.py")]
    source_paths.append(ROOT / "verification" / "benchmark_dense_retrieval.py")
    source_hashes = {path.name: sha256(path.read_bytes()) for path in source_paths}
    report = {"format_version": 1, "classification": "synthetic only; no live service data",
              "source": {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                         "current_sha256": source_hashes, "old_oracle_ref": args.baseline_ref,
                         "old_memory_sha256": sha256(old_source),
                         "benchmark_sha256": sha256(Path(__file__).read_bytes())},
              "environment": {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version},
              "method": {"query": QUERY, "repeats": args.repeats, "random_seed": 1847,
                         "fixed_component_marks": 120, "facts_per_mark": 4, "fact_marks_max": 8,
                         "growing_star_neighbors": "size//10",
                         "frozen_state_oracle": "exact old full selection/edge decoder; no _observe; same DB state",
                         "timed": "runtime_local_cap = retrieve + CanonicalSnapshot + ScratchLog.write_snapshot memory_observation (flush/fsync) + freeze_retrieval; read_freeze excludes that scratch write for comparison with old harness",
                         "additional_timing": "independent graph validator and retrieval_record/knowledge_retrieved scratch writes (flush/fsync) reported separately and in complete_local_records_and_validation",
                         "untimed": "ingestion, database backup/startup, scratch startup, old oracle",
                         "sql_work_measure": "SQLite progress callback every 100 VM instructions during actual local-cap path; includes instrumentation overhead; callback count is a coarse work measure",
                         "cache_modes": {"cold": "fresh graph, no query primer",
                                         "warm": "unmatched query primer",
                                         "steady": "matching query primer with distinct event id"},
                         "audit_change": "full graph -> direct-hit neighborhood; endpoint-only frequency audit; static shadow disabled",
                         "limitations": "Synthetic in-memory SQLite and new temporary disk scratch (not real service history), no service/model/Discord; finite curves do not prove arbitrary-size <5s."},
              "cases": []}
    for scenario in args.scenarios:
        for size in sorted(args.sizes):
            db = sqlite3.connect(":memory:")
            try:
                graph = memory.MemoryGraph(db)
                facts = synthetic_facts(size, scenario)
                started = time.perf_counter()
                graph.add(SCOPE, "synthetic:all", "synthetic-author", facts, 1.0)
                ingestion_seconds = time.perf_counter() - started
                # The database is copied afresh for every sample.  Static
                # primers do not mutate source/dynamic state, so this frozen
                # old selection is shared across those identical snapshots.
                started = time.perf_counter()
                expected = frozen_selection(db, graph, old)
                old_oracle_seconds = time.perf_counter() - started
                for cache in args.caches:
                    samples = [one_sample(db, old, cache, repeat + 1, expected) for repeat in range(args.repeats)]
                    case = {"scenario": scenario, "marks": size, "cache": cache, "samples": samples,
                            "ingestion_seconds_untimed": ingestion_seconds,
                            "old_oracle_seconds_untimed": old_oracle_seconds,
                            "initialize_median_seconds_untimed": statistics.median(sample["initialize_seconds_untimed"] for sample in samples),
                            "initialize_max_seconds_untimed": max(sample["initialize_seconds_untimed"] for sample in samples),
                            "retrieve_median_seconds": statistics.median(sample["retrieve_seconds"] for sample in samples),
                            "runtime_read_freeze_median_seconds": statistics.median(sample["runtime_read_freeze_seconds"] for sample in samples),
                            "runtime_read_freeze_max_seconds": max(sample["runtime_read_freeze_seconds"] for sample in samples),
                            "runtime_local_cap_median_seconds": statistics.median(sample["runtime_local_cap_seconds"] for sample in samples),
                            "runtime_local_cap_max_seconds": max(sample["runtime_local_cap_seconds"] for sample in samples),
                            "complete_local_records_and_validation_median_seconds": statistics.median(sample["complete_local_records_and_validation_seconds"] for sample in samples),
                            "complete_local_records_and_validation_max_seconds": max(sample["complete_local_records_and_validation_seconds"] for sample in samples),
                            "local_edges": samples[0]["local_edges"],
                            "expansion_candidates": samples[0]["expansion_candidates"]}
                    report["cases"].append(case)
                    print(json.dumps({key: value for key, value in case.items() if key != "samples"}), flush=True)
            finally:
                db.close()
    if source_hashes != {path.name: sha256(path.read_bytes()) for path in source_paths}:
        raise RuntimeError("source changed during benchmark; rerun final evidence on fixed sources")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(canonical(report) + b"\n")
    print(json.dumps({"report": str(args.output.resolve())}), flush=True)


if __name__ == "__main__":
    main()
