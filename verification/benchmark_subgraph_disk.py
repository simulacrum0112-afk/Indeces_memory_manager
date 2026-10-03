"""Representative temporary disk SQLite neighborhood query verification.

Reuses the main harness without editing it.  A module-local connect facade
explicitly replaces that harness's in-memory sample destination with a named
temporary disk SQLite file.  The real memory/store modules keep normal sqlite3.
Journal settings match Store: WAL, synchronous=FULL, foreign_keys=ON.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verification import benchmark_subgraph_retrieval as harness  # noqa: E402


def configure(connection):
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def disk_sample(prepared, old, expected, filename, repeat):
    def connect(database, *args, **kwargs):
        if database != ":memory:":
            raise ValueError("unexpected sample connection requested by main harness")
        return configure(sqlite3.connect(filename, *args, **kwargs))

    # Only the harness's sqlite3 binding is substituted; this does not patch
    # the sqlite3 module or the memory implementation. one_sample's SQLite
    # backup, startup, query, snapshot, real scratch writes and validators are
    # otherwise exactly the primary harness path.
    with patch.object(harness, "sqlite3", types.SimpleNamespace(connect=connect)):
        sample = harness.one_sample(prepared, old, "cold", repeat, expected)
    sample["sample_dbfile_bytes_after_close"] = filename.stat().st_size
    return sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 3000, 10000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline-ref", default="db04135")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.repeats < 1:
        parser.error("output must not exist; repeats must be positive")
    old, old_source = harness.legacy_module(args.baseline_ref)
    sources = [ROOT / "indeces" / name for name in
               ("memory.py", "memory_index.py", "run_records.py", "scratch.py", "store.py")]
    sources += [ROOT / "verification" / name for name in
                ("benchmark_dense_retrieval.py", "benchmark_subgraph_retrieval.py", "benchmark_subgraph_disk.py")]
    hashes = {path.name: harness.sha256(path.read_bytes()) for path in sources}
    report = {"format_version": 1, "classification": "synthetic temporary disk SQLite only; no live/private data",
              "source": {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                         "current_sha256": hashes, "old_oracle_ref": args.baseline_ref,
                         "old_memory_sha256": harness.sha256(old_source)},
              "environment": {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version},
              "method": {"scenario": "fixed_neighborhood", "query": harness.QUERY, "repeats": args.repeats,
                         "cache": "cold graph query caches after constructor; no OS-cold or storage-cache-cold claim",
                         "sqlite": "actual named temporary disk files, prepared source and every measured sample",
                         "connection_facade": "module-local harness.sqlite3.connect(':memory:') redirected to a fresh named disk DB; real sqlite3 and memory modules unmodified",
                         "journal_mode": "wal", "synchronous": "FULL", "foreign_keys": "ON",
                         "timed": "same actual local-cap boundary as primary harness: retrieve+snapshot+observation scratch flush/fsync+freeze",
                         "untimed": "synthetic ingestion, database backup, constructor, scratch startup, frozen old oracle",
                         "additional_timing": "independent graph validator and two post-cap scratch flush/fsync writes separately/combined reported",
                         "limitations": "OS page cache warmed by preparation/backup/startup; finite synthetic local tests cannot prove arbitrary-size or real-service deadline compliance"},
              "cases": []}
    with TemporaryDirectory(prefix="indeces-synthetic-disk-") as temporary:
        directory = Path(temporary)
        for size in sorted(args.sizes):
            filename = directory / f"prepared-{size}.sqlite3"
            db = configure(sqlite3.connect(filename))
            try:
                graph = harness.memory.MemoryGraph(db)
                facts = harness.synthetic_facts(size, "fixed_neighborhood")
                started = time.perf_counter()
                graph.add(harness.SCOPE, "synthetic:all", "synthetic-author", facts, 1.0)
                ingestion_seconds = time.perf_counter() - started
                started = time.perf_counter()
                expected = harness.frozen_selection(db, graph, old)
                oracle_seconds = time.perf_counter() - started
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                samples = [disk_sample(db, old, expected, directory / f"sample-{size}-{repeat}.sqlite3", repeat)
                           for repeat in range(1, args.repeats + 1)]
                case = {"scenario": "fixed_neighborhood", "marks": size, "cache": "cold", "samples": samples,
                        "prepared_dbfile_bytes": filename.stat().st_size,
                        "ingestion_seconds_untimed": ingestion_seconds, "old_oracle_seconds_untimed": oracle_seconds,
                        "initialize_median_seconds_untimed": statistics.median(s["initialize_seconds_untimed"] for s in samples),
                        "retrieve_median_seconds": statistics.median(s["retrieve_seconds"] for s in samples),
                        "runtime_read_freeze_median_seconds": statistics.median(s["runtime_read_freeze_seconds"] for s in samples),
                        "runtime_local_cap_median_seconds": statistics.median(s["runtime_local_cap_seconds"] for s in samples),
                        "runtime_local_cap_max_seconds": max(s["runtime_local_cap_seconds"] for s in samples),
                        "complete_local_records_and_validation_median_seconds": statistics.median(s["complete_local_records_and_validation_seconds"] for s in samples),
                        "complete_local_records_and_validation_max_seconds": max(s["complete_local_records_and_validation_seconds"] for s in samples),
                        "local_edges": samples[0]["local_edges"]}
                report["cases"].append(case)
                print(json.dumps({key: value for key, value in case.items() if key != "samples"}), flush=True)
            finally:
                db.close()
    if hashes != {path.name: harness.sha256(path.read_bytes()) for path in sources}:
        raise RuntimeError("source changed during disk benchmark; rerun evidence on fixed sources")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(harness.canonical(report) + b"\n")
    print(json.dumps({"report": str(args.output.resolve())}), flush=True)


if __name__ == "__main__":
    main()
