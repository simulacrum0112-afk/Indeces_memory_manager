"""Pure synthetic full-audit retrieval benchmark; never opens service data.

Run before and after an implementation change with the same arguments:
    python verification/benchmark_memory_scaling.py --save-baseline BASELINE_DIR --output BEFORE_JSON
    python verification/benchmark_memory_scaling.py --compare-baseline BASELINE_DIR --output AFTER_JSON

Each baseline artifact contains every returned record and the complete graph
audit as canonical JSON, plus per-table database digests. Baseline files and
reports are created exclusively and never overwritten by this program.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import marshal
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
MEMORY_SOURCE_PATH = PROJECT_ROOT / "indeces" / "memory.py"
IMPORTED_MEMORY_SOURCE_BYTES = MEMORY_SOURCE_PATH.read_bytes()

from indeces import memory  # noqa: E402
from indeces.run_records import validate_graph_audit  # noqa: E402
from indeces.scratch import canonical  # noqa: E402

if MEMORY_SOURCE_PATH.read_bytes() != IMPORTED_MEMORY_SOURCE_BYTES:
    raise RuntimeError("memory.py changed while importing; rerun against one stable source version")


SCOPE = "synthetic:memory-scaling"
QUERY = "m000000 m000001 m000002 m000003"
UNIT_MARKS = 10
UNIT_FACTS = ((0, 1, 2, 3), (2, 3, 4, 5), (4, 5, 6, 7),
              (6, 7, 8, 9), (0, 1, 8, 9))
PHASES = ("records", "observe", "edges", "selection", "literal_match", "json_encoding")
SCENARIOS = ("first_seed", "already_seeded")
FORMAT_VERSION = 1


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def write_once(path, content):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
    return path


def synthetic_facts(mark_count):
    if mark_count < UNIT_MARKS or mark_count % UNIT_MARKS:
        raise ValueError("sizes must be positive multiples of ten")
    facts = []
    for unit in range(mark_count // UNIT_MARKS):
        marks = [f"m{unit * UNIT_MARKS + offset:06d}" for offset in range(UNIT_MARKS)]
        for fact_index, offsets in enumerate(UNIT_FACTS):
            chosen = [marks[offset] for offset in offsets]
            text = (f"Synthetic unit {unit:06d}, fact {fact_index}: "
                    + " ".join(chosen) + "; synthetic evidence only. " * 5)
            facts.append({"text": text, "quote": text, "marks": chosen})
    return facts


def make_database(mark_count):
    """Prepare one synthetic graph; ingestion is never inside timed retrieval."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE synthetic_runtime_turns (message_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    graph = memory.MemoryGraph(db)
    facts = synthetic_facts(mark_count)
    graph.add(SCOPE, "synthetic:all-units", "synthetic-author", facts, 1.0)
    return db


def database_digests(db):
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_%' ORDER BY name")]
    return {table: sha256(canonical(list(db.execute(f"SELECT * FROM {table} ORDER BY rowid"))))
            for table in tables}


def measured_retrieval(prepared_db, scenario):
    # A fresh in-memory clone isolates every repeat. No external database,
    # knowledge directory, credentials, model call or scratch log is opened.
    db = sqlite3.connect(":memory:")
    try:
        prepared_db.backup(db)
        graph = memory.MemoryGraph(db)
        if scenario == "already_seeded":
            graph.retrieve(SCOPE, [], QUERY, 2.0, event_id="synthetic-initial-seed")
        # Simulate the unrelated accepted-turn writes that Runtime/Store make
        # before retrieval. A graph cache must not depend on global SQLite
        # total_changes, which these writes advance without changing sources.
        changes_before_runtime = db.total_changes
        with db:
            db.execute("INSERT INTO synthetic_runtime_turns VALUES(?,?)", ("synthetic-measured-event", "accepted"))
            db.execute("UPDATE synthetic_runtime_turns SET status=? WHERE message_id=?",
                       ("processing", "synthetic-measured-event"))
        unrelated_runtime_changes = db.total_changes - changes_before_runtime
        counts = {table: db.execute(f"SELECT count(*) FROM {table} WHERE scope=?", (SCOPE,)).fetchone()[0]
                  for table in ("memory_records", "memory_static", "memory_support", "memory_dynamic")}
        elapsed = {phase: 0.0 for phase in PHASES}
        calls = {phase: 0 for phase in PHASES}

        def instrument(function, phase):
            def measured(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return function(*args, **kwargs)
                finally:
                    elapsed[phase] += time.perf_counter() - started
                    calls[phase] += 1
            return measured

        for name, phase in (("_records", "records"), ("_observe", "observe"),
                            ("_edges", "edges"), ("_selection", "selection")):
            setattr(graph, name, instrument(getattr(graph, name), phase))
        audit = {}
        # Cold regex caches make the documented scenario reproducible; this
        # is not a promise about a running service's warm-cache latency.
        gc.collect()
        re.purge()
        gc_before = [generation["collections"] for generation in gc.get_stats()]
        with patch.object(memory, "_hit", instrument(memory._hit, "literal_match")), \
                patch.object(memory, "_json", instrument(memory._json, "json_encoding")):
            started = time.perf_counter()
            records = graph.retrieve(SCOPE, [], QUERY, 3.0, event_id="synthetic-measured-event",
                                     audit=audit, ranking_mode="static")
            total = time.perf_counter() - started
        gc_after = [generation["collections"] for generation in gc.get_stats()]
        # Full independent audit validation and byte capture are not counted
        # as graph.retrieve time and are recorded separately.
        started = time.perf_counter()
        validate_graph_audit(audit)
        validation_seconds = time.perf_counter() - started
        encoded_audit = canonical(audit)
        durable_audit = db.execute("SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
                                  (SCOPE, "synthetic-measured-event")).fetchone()[0].encode("utf-8")
        immutable_result = canonical({"records": records, "audit": audit,
                                      "database_table_sha256": database_digests(db)})
        statistics_record = {
            "retrieve_seconds": total,
            "unrelated_runtime_changes_before_retrieve": unrelated_runtime_changes,
            "phase_seconds_inclusive": elapsed,
            "phase_calls": calls,
            "validation_seconds_outside_retrieve": validation_seconds,
            "gc_collections_during_retrieve": [after - before for before, after in zip(gc_before, gc_after)],
            "counts_before": counts,
            "known_marks": audit["match"]["known_marks_count"],
            "direct_hits": audit["match"]["direct_hits"],
            "changed_edges": len(audit["observation"]["changed_edges"]),
            "live_edges": audit["selection"]["live_edge_count"],
            "ranked_candidates": len(audit["selection"]["ranked_candidates"]),
            "selected_records": len(records),
            "full_audit_utf8_bytes": len(encoded_audit),
            "durable_audit_utf8_bytes": len(durable_audit),
            "complete_result_utf8_bytes": len(immutable_result),
            "complete_result_sha256": sha256(immutable_result),
        }
        return statistics_record, immutable_result
    finally:
        db.close()


def summarize(samples):
    first = samples[0]
    return {
        "samples": samples,
        "retrieve_median_seconds": statistics.median(sample["retrieve_seconds"] for sample in samples),
        "retrieve_min_seconds": min(sample["retrieve_seconds"] for sample in samples),
        "retrieve_max_seconds": max(sample["retrieve_seconds"] for sample in samples),
        "phase_median_seconds_inclusive": {phase: statistics.median(
            sample["phase_seconds_inclusive"][phase] for sample in samples) for phase in PHASES},
        "validation_median_seconds_outside_retrieve": statistics.median(
            sample["validation_seconds_outside_retrieve"] for sample in samples),
        "known_marks": first["known_marks"],
        "active_records": first["counts_before"]["memory_records"],
        "static_edges": first["counts_before"]["memory_static"],
        "support_edges": first["counts_before"]["memory_support"],
        "changed_edges": first["changed_edges"],
        "live_edges": first["live_edges"],
        "ranked_candidates": first["ranked_candidates"],
        "selected_records": first["selected_records"],
        "full_audit_utf8_bytes": first["full_audit_utf8_bytes"],
        "complete_result_utf8_bytes": first["complete_result_utf8_bytes"],
        "complete_result_sha256": first["complete_result_sha256"],
    }


def source_identity():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                         encoding="utf-8", stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit = None
    return {"head_commit": commit,
            "memory_py_sha256": sha256(IMPORTED_MEMORY_SOURCE_BYTES),
            "memory_source_captured": "immediately before import and checked unchanged immediately after import",
            "retrieve_code_object_sha256": sha256(marshal.dumps(memory.MemoryGraph.retrieve.__code__)),
            "benchmark_py_sha256": sha256(Path(__file__).read_bytes())}


def legacy_memory_module(ref):
    """Compile tracked baseline source without changing any workspace file."""
    source = subprocess.check_output(["git", "show", f"{ref}:indeces/memory.py"], cwd=PROJECT_ROOT)
    module = types.ModuleType("indeces._synthetic_memory_publication_baseline")
    module.__package__ = "indeces"
    sys.modules[module.__name__] = module
    exec(compile(source, f"<synthetic-baseline:{ref}:indeces/memory.py>", "exec"), module.__dict__)
    return module, sha256(source)


def temp_revision(db):
    table = db.execute("SELECT 1 FROM sqlite_temp_master WHERE type='table' "
                       "AND name='indeces_retrieval_revisions'").fetchone()
    if not table:
        return 0
    row = db.execute("SELECT revision FROM temp.indeces_retrieval_revisions WHERE scope=?", (SCOPE,)).fetchone()
    return row[0] if row else 0


def publication_sample(module, mark_count):
    """Measure publication/rebuild costs independently from retrieval timing."""
    db = sqlite3.connect(":memory:")
    try:
        graph = module.MemoryGraph(db)
        facts = synthetic_facts(mark_count)
        added = [{"text": "Synthetic added source: " + fact["text"],
                  "quote": "Synthetic added source: " + fact["quote"], "marks": fact["marks"]}
                 for fact in facts[:len(UNIT_FACTS)]]
        rebuild_time = 0.0
        original_rebuild = graph._rebuild_static

        def timed_rebuild(*args, **kwargs):
            nonlocal rebuild_time
            started = time.perf_counter()
            try:
                return original_rebuild(*args, **kwargs)
            finally:
                rebuild_time += time.perf_counter() - started

        graph._rebuild_static = timed_rebuild
        measurements, outcomes = {}, {}

        def measure_mutation(name, operation):
            nonlocal rebuild_time
            gc.collect()
            rebuild_time = 0.0
            changes_before, revision_before = db.total_changes, temp_revision(db)
            started = time.perf_counter()
            operation()
            duration = time.perf_counter() - started
            measurements[name] = {"seconds": duration, "rebuild_seconds_inclusive": rebuild_time,
                                  "sqlite_total_changes": db.total_changes - changes_before,
                                  "temp_revision_writes": temp_revision(db) - revision_before}

        def retrieve_after(name, event, now):
            gc.collect()
            re.purge()
            audit = {}
            started = time.perf_counter()
            records = graph.retrieve(SCOPE, [], QUERY, now, event_id=event, audit=audit, ranking_mode="static")
            measurements[name] = {"seconds": time.perf_counter() - started}
            validate_graph_audit(audit)
            outcomes[name] = canonical({"records": records, "audit": audit,
                                        "database_table_sha256": database_digests(db)})

        measure_mutation("initial_add", lambda: graph.add(
            SCOPE, "synthetic:all-units", "synthetic-author", facts, 1.0))
        retrieve_after("retrieve_after_initial_add", "synthetic-publication-prime", 2.0)
        measure_mutation("representative_add", lambda: graph.add(
            SCOPE, "synthetic:added-unit", "synthetic-author", added, 3.0))
        retrieve_after("retrieve_after_representative_add", "synthetic-after-add", 4.0)
        with patch.object(module.time, "time", return_value=5.0):
            measure_mutation("representative_retire", lambda: graph.deactivate_source(SCOPE, "synthetic:added-unit"))
        retrieve_after("retrieve_after_representative_retire", "synthetic-after-retire", 6.0)
        return measurements, outcomes
    finally:
        db.close()


def publication_comparison(sizes, repeats, ref):
    before, source_hash = legacy_memory_module(ref)
    report = {"baseline_ref": ref, "baseline_memory_py_sha256": source_hash,
              "method": "initial full add; one five-fact source add; retire that source; cold retrieval after each mutation",
              "timing_limit": "publication timings are separate and excluded from ordinary retrieval benchmark",
              "cases": []}
    for size in sorted(sizes):
        samples = {"before": [], "after": []}
        expected = None
        for repeat in range(repeats):
            for label, module in (("before", before), ("after", memory)):
                measurement, outcomes = publication_sample(module, size)
                if expected is None:
                    expected = outcomes
                if outcomes != expected:
                    raise ValueError(f"publication full result/audit/database bytes differ: marks={size}, repeat={repeat}, implementation={label}")
                samples[label].append(measurement)
        operations = {}
        for operation in samples["before"][0]:
            operations[operation] = {}
            for label in ("before", "after"):
                operation_samples = [sample[operation] for sample in samples[label]]
                operations[operation][label] = {
                    "samples": operation_samples,
                    "median_seconds": statistics.median(sample["seconds"] for sample in operation_samples),
                }
                if "temp_revision_writes" in operation_samples[0]:
                    operations[operation][label].update(
                        sqlite_total_changes=operation_samples[0]["sqlite_total_changes"],
                        temp_revision_writes=operation_samples[0]["temp_revision_writes"],
                        rebuild_median_seconds_inclusive=statistics.median(
                            sample["rebuild_seconds_inclusive"] for sample in operation_samples))
            previous, current = (operations[operation][label]["median_seconds"] for label in ("before", "after"))
            operations[operation]["after_divided_by_before_seconds"] = current / previous
        case = {"marks": size, "operations": operations, "all_full_bytes_equal": True,
                "outcome_sha256": {name: sha256(value) for name, value in expected.items()}}
        report["cases"].append(case)
        print(json.dumps({"source_publication_marks": size, "all_full_bytes_equal": True,
                          "operations": {name: {"before_seconds": data["before"]["median_seconds"],
                                                 "after_seconds": data["after"]["median_seconds"],
                                                 "temp_revision_writes": data["after"].get("temp_revision_writes")}
                                         for name, data in operations.items()}}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 3000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--publication-only", action="store_true",
                        help="compare source add/retirement against tracked baseline, without scaling cases")
    parser.add_argument("--baseline-ref", default="HEAD", help="tracked source revision for publication comparison")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--save-baseline", type=Path)
    baseline.add_argument("--compare-baseline", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or len(set(args.sizes)) != len(args.sizes):
        parser.error("repeats must be positive and sizes must be distinct")
    if args.output.exists():
        parser.error("output already exists; reports are write-once")
    report = {
        "format_version": FORMAT_VERSION,
        "data_classification": "fully synthetic; no existing runtime or knowledge data used",
        "source": source_identity(),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "sqlite": sqlite3.sqlite_version, "python_hash_seed": os.environ.get("PYTHONHASHSEED")},
        "method": {"sizes": args.sizes, "repeats": args.repeats, "unit_marks": UNIT_MARKS,
                   "unit_fact_offsets": UNIT_FACTS, "scope": SCOPE, "fixed_query": QUERY,
                   "ranking_mode": "static", "ingestion_included": False,
                   "database": "fresh SQLite :memory: clone for every sample",
                   "runtime_simulation": "unrelated turn INSERT and UPDATE before each retrieval",
                   "regex_cache": "purged before each timed retrieval",
                   "garbage_collection": "enabled; explicit collection before timing",
                   "phase_times": "inclusive/nested; do not sum them",
                   "validation_and_output_encoding_in_retrieve_time": False},
        "cases": [],
        "comparisons": [],
    }
    for size in [] if args.publication_only else sorted(args.sizes):
        prepared_db = make_database(size)
        try:
            for scenario in SCENARIOS:
                samples = []
                expected_bytes = None
                artifact_name = f"marks-{size}-{scenario}.canonical.json"
                if args.compare_baseline:
                    expected_bytes = (args.compare_baseline / artifact_name).read_bytes()
                for repeat in range(args.repeats):
                    sample, result_bytes = measured_retrieval(prepared_db, scenario)
                    if expected_bytes is None:
                        expected_bytes = result_bytes
                        if args.save_baseline:
                            write_once(args.save_baseline / artifact_name, result_bytes)
                    if result_bytes != expected_bytes:
                        raise ValueError(f"full result/audit/database bytes differ: marks={size}, scenario={scenario}, repeat={repeat}")
                    samples.append(sample)
                case = {"marks": size, "scenario": scenario, **summarize(samples),
                        "all_repeat_bytes_equal": True,
                        "baseline_bytes_equal": True if args.compare_baseline else None,
                        "baseline_artifact": artifact_name if args.save_baseline or args.compare_baseline else None}
                report["cases"].append(case)
                print(json.dumps({key: case[key] for key in ("marks", "scenario", "retrieve_median_seconds",
                    "retrieve_min_seconds", "retrieve_max_seconds", "active_records", "static_edges",
                    "ranked_candidates", "full_audit_utf8_bytes")}, ensure_ascii=False), flush=True)
        finally:
            prepared_db.close()
    for scenario in SCENARIOS:
        relevant = [case for case in report["cases"] if case["scenario"] == scenario]
        for small, large in zip(relevant, relevant[1:]):
            size_ratio = large["marks"] / small["marks"]
            time_ratio = large["retrieve_median_seconds"] / small["retrieve_median_seconds"]
            report["comparisons"].append({
                "scenario": scenario, "small_marks": small["marks"], "large_marks": large["marks"],
                "mark_ratio": size_ratio, "record_ratio": large["active_records"] / small["active_records"],
                "edge_ratio": large["static_edges"] / small["static_edges"],
                "audit_byte_ratio": large["full_audit_utf8_bytes"] / small["full_audit_utf8_bytes"],
                "median_time_ratio": time_ratio, "time_ratio_divided_by_mark_ratio": time_ratio / size_ratio,
                "two_point_effective_exponent": math.log(time_ratio) / math.log(size_ratio),
                "interpretation_limit": "Two synthetic sizes cannot prove asymptotic complexity or service deadline compliance.",
            })
    if args.publication_only:
        report["source_publication"] = publication_comparison(args.sizes, args.repeats, args.baseline_ref)
    write_once(args.output, canonical(report) + b"\n")
    print(json.dumps({"report": str(args.output.resolve()), "comparisons": report["comparisons"]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
