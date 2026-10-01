"""Bounded, read-only views of graph evidence and retained scratch records.

Inspection never opens a model slot or writes/migrates the runtime database.
Graph event history belongs to the durable database, not scratch retention.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from . import scratch
from .identity import SELF_NAME_ALIASES
from .run_records import (MAX_PDF_METADATA_BYTES, digest, pdf_conversion_metadata,
                          validate_answer, validate_graph_audit, validate_retrieval)


MAX_NODES = 500
MAX_EDGES = 1000
MAX_SCRATCH_FILES = 8
MAX_SCRATCH_BYTES = 32 * 1024 * 1024
MAX_TOTAL_SCRATCH_BYTES = 64 * 1024 * 1024
MAX_SCRATCH_RECORDS = 10000
MAX_TRACES = 100
MAX_TRACE_RECORDS = 1000
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_HISTORY_EVENTS = 1000
MAX_HISTORY_CHANGES = 100
MAX_AUDIT_BYTES = 1024 * 1024
MAX_SOURCE_CHARACTERS = 256 * 1024
MAX_SOURCE_TEXT_BYTES = 512 * 1024
MAX_SOURCE_CHUNKS = 512
MAX_SOURCE_BYTES = 1024 * 1024
MAX_PDF_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_VERSIONS = 256
MAX_EDGE_CONTEXT = 100
MAX_EDGE_SOURCES = 128
MAX_GRAPH_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
_MANAGED = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl\Z")


def _now():
    return datetime.now(timezone.utc)


def _scope(config):
    return f"{config.discord.guild_id}:knowledge"


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _json(value, fallback=None):
    if value is None:
        return fallback
    decoded = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    return decoded


@contextmanager
def _database(config):
    """A short SQLite snapshot, with a VM deadline and no schema constructors."""
    directory = Path(config.state_dir)
    path = directory / "memory.sqlite3"
    if directory.is_symlink() or directory.is_junction() or path.is_symlink() or path.is_junction():
        raise ValueError("database snapshot path must not be a link")
    if directory.exists() and not directory.is_dir():
        raise ValueError("state directory must be a directory")
    if path.resolve().parent != directory.resolve():
        raise ValueError("database snapshot path escaped state directory")
    if path.exists() and not path.is_file():
        raise ValueError("database snapshot path must be a regular file")
    if not path.is_file():
        yield None
        return
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 2.0
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _empty_graph():
    return {"nodes": [], "edges": [], "active_record_count": 0,
            "total_nodes": 0, "total_edges": 0, "truncated": False,
            "diagnostics": None, "diagnostics_status": "uninitialized", "diagnostics_warning": None}


_PAIRS = """SELECT a,b FROM memory_support WHERE scope=:scope
    UNION SELECT a,b FROM memory_static WHERE scope=:scope
    UNION SELECT a,b FROM memory_dynamic WHERE scope=:scope"""
_NODES = """WITH frequencies AS (
    SELECT CAST(j.value AS TEXT) AS id,COUNT(DISTINCT r.id) AS frequency
    FROM memory_records r,json_each(r.marks_json) j
    WHERE r.scope=:scope AND r.active=1
      AND lower(trim(CAST(j.value AS TEXT))) NOT IN (SELECT value FROM json_each(:self_marks)) GROUP BY j.value),
    identities AS (SELECT id FROM frequencies
    UNION SELECT a FROM memory_dynamic WHERE scope=:scope
    UNION SELECT b FROM memory_dynamic WHERE scope=:scope
    UNION SELECT a FROM memory_support WHERE scope=:scope
    UNION SELECT b FROM memory_support WHERE scope=:scope
    UNION SELECT a FROM memory_static WHERE scope=:scope
    UNION SELECT b FROM memory_static WHERE scope=:scope)
    SELECT i.id AS id,COALESCE(f.frequency,0) AS frequency
    FROM identities i LEFT JOIN frequencies f ON f.id=i.id"""


def _self_marks(config):
    marks = {str(config.name).strip().casefold()}
    if marks & SELF_NAME_ALIASES:
        marks.update(SELF_NAME_ALIASES)
    return json.dumps(sorted(marks))


def _npmi_diagnostics(db, scope, self_marks):
    """Whole-scope counts, independent of the bounded graph display.

    One active labelled fact is the co-occurrence unit, as in MemoryGraph.
    Counts describe the saved corpus; they are not confidence estimates.
    """
    required = {"memory_records", "memory_support", "memory_static", "memory_dynamic"}
    if not required <= _tables(db):
        return None
    row = db.execute("""WITH frequencies AS (
        SELECT CAST(j.value AS TEXT) AS id,COUNT(DISTINCT r.id) AS frequency
        FROM memory_records r,json_each(r.marks_json) j
        WHERE r.scope=:scope AND r.active=1
          AND lower(trim(CAST(j.value AS TEXT))) NOT IN (SELECT value FROM json_each(:self_marks)) GROUP BY j.value),
        support_counts AS MATERIALIZED (
          SELECT s.a,s.b,s.co_count,t.weight,
            (SELECT COUNT(DISTINCT r.source_id) FROM json_each(s.evidence_json) j
             CROSS JOIN memory_records r NOT INDEXED
             WHERE r.id=j.value AND r.scope=s.scope AND r.active=1) AS source_count
          FROM memory_support s LEFT JOIN memory_static t
            ON t.scope=s.scope AND t.a=s.a AND t.b=s.b
          WHERE s.scope=:scope AND s.co_count>0
            AND s.a NOT IN (SELECT value FROM json_each(:self_marks))
            AND s.b NOT IN (SELECT value FROM json_each(:self_marks))),
        supported AS MATERIALIZED (SELECT * FROM support_counts WHERE source_count>0),
        positive AS MATERIALIZED (SELECT * FROM supported WHERE weight>0),
        support_marks AS (SELECT a AS id FROM supported UNION SELECT b FROM supported),
        positive_marks AS (SELECT a AS id FROM positive UNION SELECT b FROM positive)
        SELECT
          (SELECT COUNT(*) FROM memory_records WHERE scope=:scope AND active=1) AS active_records,
          (SELECT COUNT(DISTINCT source_id) FROM memory_records WHERE scope=:scope AND active=1) AS active_source_versions,
          (SELECT COUNT(*) FROM frequencies) AS active_marks,
          (SELECT COUNT(*) FROM supported) AS supported_pairs,
          (SELECT COUNT(*) FROM positive) AS positive_edges,
          (SELECT COUNT(*) FROM supported WHERE weight IS NULL OR weight<=0) AS supported_without_positive,
          (SELECT COUNT(*) FROM frequencies WHERE id NOT IN (SELECT id FROM support_marks)) AS isolated_marks,
          (SELECT COUNT(*) FROM frequencies WHERE id NOT IN (SELECT id FROM positive_marks)) AS marks_without_positive_edges,
          (SELECT COUNT(*) FROM positive WHERE co_count=1) AS single_support_positive_edges,
          (SELECT COUNT(*) FROM positive WHERE source_count=1) AS single_source_positive_edges,
          (SELECT COUNT(*) FROM positive WHERE weight=1) AS unit_weight_edges,
          (SELECT MIN(weight) FROM positive) AS min_positive_weight,
          (SELECT MAX(weight) FROM positive) AS max_positive_weight,
          (SELECT AVG(weight) FROM (SELECT weight FROM positive ORDER BY weight
            LIMIT 2-(SELECT COUNT(*) FROM positive)%2 OFFSET ((SELECT COUNT(*) FROM positive)-1)/2)) AS median_positive_weight
        """, {"scope": scope, "self_marks": self_marks}).fetchone()
    return dict(row)


def npmi_diagnostics(config):
    """A read-only aggregate suitable for Console; never reads scratch text."""
    result = {"scope": _scope(config), "generated_at": _now().isoformat(),
              "ranking_mode": "static", "weight_basis": "static_npmi",
              "statistics_status": "uninitialized", "statistics": None, "warnings": []}
    try:
        with _database(config) as db:
            if db is not None:
                result["statistics"] = _npmi_diagnostics(db, _scope(config), _self_marks(config))
                if result["statistics"] is not None:
                    result["statistics_status"] = "available"
    except sqlite3.Error as error:
        result["statistics_status"] = "unavailable"
        result["warnings"].append("database_snapshot_timeout" if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT else "database_snapshot_unavailable")
    except (ValueError, TypeError, KeyError, OverflowError):
        result["statistics_status"] = "unavailable"
        result["warnings"].append("database_snapshot_unavailable")
    if result["statistics"] is None and not result["warnings"]:
        result["warnings"].append("graph_not_initialized")
    return result


def _edge(db, scope, a, b, *, include_statistics=False):
    a, b = sorted((a, b))
    support = db.execute("SELECT * FROM memory_support WHERE scope=? AND a=? AND b=?", (scope, a, b)).fetchone()
    static = db.execute("SELECT * FROM memory_static WHERE scope=? AND a=? AND b=?", (scope, a, b)).fetchone()
    dynamic = db.execute("SELECT * FROM memory_dynamic WHERE scope=? AND a=? AND b=?", (scope, a, b)).fetchone()
    if not any((support, static, dynamic)):
        return None
    evidence = static or support
    ids = _json(evidence["evidence_json"], []) if evidence else []
    if not isinstance(ids, list) or not all(type(item) is int for item in ids):
        raise ValueError("invalid edge evidence")
    # The viewer derives source ids from current active records; an archived
    # dynamic row alone must never appear eligible for retrieval.
    sources = []
    if ids:
        sources = [row[0] for row in db.execute(
            "SELECT DISTINCT r.source_id FROM json_each(?) j CROSS JOIN memory_records r NOT INDEXED WHERE r.id=j.value AND r.scope=? AND r.active=1 ORDER BY r.source_id",
            (evidence["evidence_json"], scope))]
    supported = bool(sources) and bool(evidence) and evidence["co_count"] > 0
    static_weight = float(static["weight"]) if static else None
    dynamic_weight = float(dynamic["weight"]) if dynamic else None
    # Dynamic rows are retained observations; current retrieval uses positive
    # static NPMI only. Mere co-occurrence support is not a positive edge.
    effective = static_weight if supported and static_weight is not None and static_weight > 0 else None
    npmi_statistics = None
    if include_statistics:
        counts = db.execute("""SELECT
        COUNT(DISTINCT CASE WHEN j.value=:a THEN r.id END) AS frequency_a,
        COUNT(DISTINCT CASE WHEN j.value=:b THEN r.id END) AS frequency_b
        FROM memory_records r LEFT JOIN json_each(r.marks_json) j
        WHERE r.scope=:scope AND r.active=1""", {"scope": scope, "a": a, "b": b}).fetchone()
        n = db.execute("SELECT COUNT(*) FROM memory_records WHERE scope=? AND active=1", (scope,)).fetchone()[0]
        co_count = evidence["co_count"] if evidence else 0
        raw_npmi = None
        if supported and n and counts["frequency_a"] and counts["frequency_b"] and 0 < co_count <= n:
            p_ab = co_count / n
            raw_npmi = (1.0 if p_ab == 1.0 else
                        math.log(p_ab / ((counts["frequency_a"] / n) * (counts["frequency_b"] / n))) / -math.log(p_ab))
        npmi_statistics = {"active_records": n, "frequency_a": counts["frequency_a"],
                           "frequency_b": counts["frequency_b"], "co_count": co_count,
                           "recomputed_npmi": raw_npmi}
    context = _json(evidence["context_json"] if evidence else dynamic["context_json"], [])
    if not isinstance(context, list) or not all(isinstance(item, str) for item in context):
        raise ValueError("invalid edge context")
    return {"a": a, "b": b, "static_weight": static_weight,
            "dynamic_weight": dynamic_weight, "effective_weight": effective,
            "ranking_mode": "static", "weight_basis": "static_npmi",
            "npmi_statistics": npmi_statistics,
            "active_source_support": supported, "source_ids": sources[:MAX_EDGE_SOURCES], "source_count": len(sources),
            "context": context[:MAX_EDGE_CONTEXT], "context_count": len(context), "co_count": evidence["co_count"] if evidence else 0,
            "last_event_id": dynamic["last_event_id"] if dynamic else None,
            "truncated": len(sources) > MAX_EDGE_SOURCES or len(context) > MAX_EDGE_CONTEXT}


def _graph(db, scope, self_marks):
    graph = _empty_graph()
    required = {"memory_records", "memory_support", "memory_static", "memory_dynamic"}
    if not required <= _tables(db):
        return graph
    graph["active_record_count"] = db.execute(
        "SELECT COUNT(*) FROM memory_records WHERE scope=? AND active=1", (scope,)).fetchone()[0]
    graph["total_nodes"] = db.execute("SELECT COUNT(*) FROM (" + _NODES + ")", {"scope": scope, "self_marks": self_marks}).fetchone()[0]
    graph["total_edges"] = db.execute("SELECT COUNT(*) FROM (" + _PAIRS + ")", {"scope": scope}).fetchone()[0]
    graph["nodes"] = [dict(row) for row in db.execute(
        _NODES + " ORDER BY frequency DESC,id LIMIT :limit", {"scope": scope, "self_marks": self_marks, "limit": MAX_NODES})]
    known = {node["id"] for node in graph["nodes"]}
    # Select display edges only after selecting their visible endpoints.
    # Otherwise early alphabetic pairs can consume the cap without drawing
    # anything. This is a display ordering, never a retrieval-policy change.
    pairs = db.execute("SELECT p.a,p.b FROM (" + _PAIRS + """ ) p
        LEFT JOIN memory_static s ON s.scope=:scope AND s.a=p.a AND s.b=p.b
        LEFT JOIN memory_dynamic d ON d.scope=:scope AND d.a=p.a AND d.b=p.b
        WHERE p.a IN (SELECT value FROM json_each(:known))
          AND p.b IN (SELECT value FROM json_each(:known))
        ORDER BY (s.weight>0) DESC,s.weight DESC,d.weight DESC,p.a,p.b LIMIT :limit""",
        {"scope": scope, "known": json.dumps(sorted(known)), "limit": MAX_EDGES + 1}).fetchall()
    graph_bytes = len(scratch.canonical(graph["nodes"]))
    for pair in pairs[:MAX_EDGES]:
        if pair["a"] in known and pair["b"] in known:
            edge = _edge(db, scope, pair["a"], pair["b"])
            amount = len(scratch.canonical(edge))
            if graph_bytes + amount > MAX_GRAPH_BYTES:
                break
            graph["edges"].append(edge)
            graph_bytes += amount
    graph["truncated"] = graph["total_nodes"] > len(graph["nodes"]) or graph["total_edges"] > len(graph["edges"])
    # Diagnostics are an optional read-only view. Preserve a completed graph
    # if their query is interrupted; no more DB work follows in this view.
    try:
        graph["diagnostics"] = _npmi_diagnostics(db, scope, self_marks)
        graph["diagnostics_status"] = "available" if graph["diagnostics"] is not None else "uninitialized"
    except sqlite3.Error as error:
        graph["diagnostics_status"] = "unavailable"
        graph["diagnostics_warning"] = "npmi_diagnostics_timeout" if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT else "npmi_diagnostics_unavailable"
    except (ValueError, TypeError, KeyError, OverflowError):
        graph["diagnostics_status"] = "unavailable"
        graph["diagnostics_warning"] = "npmi_diagnostics_unavailable"
    return graph


def _knowledge(db, scope):
    result = {"published": [], "pending": [], "counts": {"published": 0, "pending": 0, "versions": 0}, "truncated": False}
    if not {"knowledge_versions", "knowledge_desired", "knowledge_published"} <= _tables(db):
        return result
    fields = "v.path,v.source_id,v.digest,v.status,v.created_at,v.input_tokens,v.output_tokens,v.elapsed_seconds,v.error"
    predicates = {
        "published": "knowledge_published h JOIN knowledge_versions v ON v.source_id=h.source_id WHERE h.scope=? AND v.scope=?",
        "pending": "knowledge_desired h JOIN knowledge_versions v ON v.source_id=h.source_id LEFT JOIN knowledge_published p ON p.scope=h.scope AND p.path=h.path WHERE h.scope=? AND v.scope=? AND (p.source_id IS NULL OR p.source_id<>h.source_id)",
    }
    for name, expression in predicates.items():
        result["counts"][name] = db.execute("SELECT COUNT(*) FROM " + expression, (scope, scope)).fetchone()[0]
        result[name] = [dict(row) for row in db.execute("SELECT " + fields + " FROM " + expression + " ORDER BY v.path LIMIT ?", (scope, scope, MAX_VERSIONS))]
        result["truncated"] |= result["counts"][name] > len(result[name])
    result["counts"]["versions"] = db.execute("SELECT COUNT(*) FROM knowledge_versions WHERE scope=?", (scope,)).fetchone()[0]
    return result


def _scratch_view(config, now):
    directory = Path(config.scratch_dir)
    cutoff = now - timedelta(seconds=scratch.RETENTION_SECONDS)
    result = {"directory": str(directory), "cutoff": cutoff.isoformat(), "files": [], "traces": [], "truncated": False}
    rows, checkpoints, warnings, expired_traces = [], [], [], set()
    if not directory.exists():
        return result, rows, checkpoints, warnings
    if directory.is_symlink() or directory.is_junction() or not directory.is_dir():
        warnings.append("scratch_directory_unavailable")
        return result, rows, checkpoints, warnings
    def managed(path):
        if not _MANAGED.fullmatch(path.name):
            return False
        try:
            datetime.strptime(path.stem, "%Y-%m-%d")
        except ValueError:
            return False
        return True

    files = sorted((path for path in directory.iterdir() if managed(path)), key=lambda path: path.name, reverse=True)
    result["truncated"] = len(files) > MAX_SCRATCH_FILES
    remaining = MAX_TOTAL_SCRATCH_BYTES
    for path in files[:MAX_SCRATCH_FILES]:
        if remaining <= 1:
            result["truncated"] = True
            warnings.append("scratch_aggregate_read_limit")
            break
        info = {"name": path.name, "records": 0, "retained_records": 0, "head": None, "checkpoint": None, "error": None}
        result["files"].append(info)
        loaded = None
        for attempt in range(2):
            if remaining <= 1:
                info["error"] = "temporarily_unverifiable_or_exceeds_limit"
                result["truncated"] = True
                warnings.append("scratch_aggregate_read_limit")
                break
            # Reserve the reader's one-byte overflow probe, including retries.
            # Failed validation exposes no byte count, so charge its full
            # possible read allocation rather than guessing from a new stat.
            allowance = min(MAX_SCRATCH_BYTES, remaining - 1)
            try:
                loaded = scratch.read_snapshot(path, max_bytes=allowance)
                break
            except (OSError, ValueError, UnicodeError):
                remaining -= allowance + 1
                if attempt == 1:
                    info["error"] = "temporarily_unverifiable_or_exceeds_limit"
                    warnings.append(f"scratch_file_unavailable:{path.name}")
        if loaded is None:
            continue
        # The reader rejects incomplete append boundaries and validates all
        # hashes before exposing a single record or checkpoint.
        remaining -= loaded["bytes_read"]
        info.update(records=len(loaded["records"]), head=loaded["head"], checkpoint=loaded["checkpoint"])
        if loaded["checkpoint"]:
            checkpoints.append({"file": path.name, **loaded["checkpoint"]})
            if loaded["checkpoint"]["cleanup_pending"]:
                warnings.append(f"scratch_cleanup_pending:{path.name}")
        retained = []
        for row in loaded["records"]:
            stamp = datetime.fromisoformat(row["timestamp"])
            if cutoff < stamp <= now:
                retained.append(row)
            elif stamp <= cutoff:
                trace = row["fields"].get("trace_id")
                if isinstance(trace, str) and trace:
                    expired_traces.add(trace)
        info["retained_records"] = len(retained)
        rows.extend(retained)
        if remaining <= 1:
            result["truncated"] = True
            warnings.append("scratch_aggregate_read_limit")
            break
    rows.sort(key=lambda row: (row["timestamp"], row["sequence"]))
    if len(rows) > MAX_SCRATCH_RECORDS:
        rows = rows[-MAX_SCRATCH_RECORDS:]
        result["truncated"] = True
    groups = {}
    for row in rows:
        trace = row["fields"].get("trace_id")
        if isinstance(trace, str) and trace:
            groups.setdefault(trace, []).append(row)
    partial = {identifier for checkpoint in checkpoints for identifier in checkpoint["partial_trace_ids"]} | expired_traces
    result["view_partial_trace_ids"] = sorted(partial & groups.keys())
    for trace, events in groups.items():
        names = list(dict.fromkeys(row["event"] for row in events))
        ends = [row["fields"] for row in events if row["event"] == "turn_end"]
        status = "incomplete" if "turn_start" in names else "passive"
        if trace in partial:
            status = "retention_partial"
        elif ends:
            status = "delivered" if ends[-1].get("status") == "delivered" else "failed"
        result["traces"].append({"trace_id": trace, "timestamp": events[-1]["timestamp"],
                                 "event_count": len(events), "events": names, "status": status})
    result["traces"].sort(key=lambda trace: trace["timestamp"], reverse=True)
    if len(result["traces"]) > MAX_TRACES:
        result["traces"] = result["traces"][:MAX_TRACES]
        result["truncated"] = True
    return result, rows, checkpoints, warnings


def snapshot(config):
    now = _now()
    result = {"schema_version": 1, "generated_at": now.isoformat(), "knowledge_scope": _scope(config),
              "mode": "static_npmi", "ranking_mode": "static", "weight_basis": "static_npmi",
              "service_status": "gateway_not_verified", "graph": _empty_graph(),
              "knowledge": {"published": [], "pending": [], "counts": {"published": 0, "pending": 0, "versions": 0}, "truncated": False}, "warnings": []}
    try:
        with _database(config) as db:
            if db is not None:
                result["knowledge"] = _knowledge(db, _scope(config))
                result["graph"] = _graph(db, _scope(config), _self_marks(config))
                if result["graph"]["diagnostics_warning"]:
                    result["warnings"].append(result["graph"]["diagnostics_warning"])
    except sqlite3.Error as error:
        result["warnings"].append("database_snapshot_timeout" if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT else "database_snapshot_unavailable")
    except (ValueError, TypeError, KeyError, OverflowError):
        result["warnings"].append("database_snapshot_unavailable")
    result["scratch"], _, _, warnings = _scratch_view(config, now)
    result["warnings"].extend(warnings)
    return result


def edge_details(config, a, b):
    a, b = sorted((a, b))
    result = {"a": a, "b": b, "edge": None, "history": [], "truncated": False, "warnings": [],
              "history_retention": "durable_database_not_scratch_24h"}
    try:
        with _database(config) as db:
            if db is None or not {"memory_records", "memory_support", "memory_static", "memory_dynamic"} <= _tables(db):
                return result
            result["edge"] = _edge(db, _scope(config), a, b, include_statistics=True)
            if not {"memory_event_audits", "memory_events"} <= _tables(db):
                return result
            audits = db.execute("""SELECT e.observed_at,e.event_id,e.query,
                CASE WHEN length(CAST(a.payload_json AS BLOB))<=? THEN a.payload_json ELSE NULL END AS payload_json
                FROM memory_events e JOIN memory_event_audits a ON a.scope=e.scope AND a.event_id=e.event_id
                WHERE e.scope=? ORDER BY e.observed_at DESC,e.event_id DESC LIMIT ?""", (MAX_AUDIT_BYTES, _scope(config), MAX_HISTORY_EVENTS + 1))
            scanned, history_bytes = 0, 0
            for row in audits:
                scanned += 1
                if scanned > MAX_HISTORY_EVENTS:
                    result["truncated"] = True
                    break
                if row["payload_json"] is None:
                    result["truncated"] = True
                    continue
                audit = _json(row["payload_json"])
                for change in audit["observation"]["changed_edges"]:
                    if (change["a"], change["b"]) != (a, b):
                        continue
                    if len(result["history"]) == MAX_HISTORY_CHANGES:
                        result["truncated"] = True
                        break
                    item = {"timestamp": _timestamp(row["observed_at"]), "event_id": row["event_id"],
                        "query": row["query"], "change": change, "parameters": audit["observation"]["parameters"]}
                    amount = len(scratch.canonical(item))
                    if history_bytes + amount > MAX_HISTORY_BYTES:
                        result["truncated"] = True
                        return result
                    result["history"].append(item)
                    history_bytes += amount
                if len(result["history"]) == MAX_HISTORY_CHANGES:
                    result["truncated"] = True
                    break
    except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        result["warnings"].append("edge_history_unavailable")
    return result


def _source_pdf_details(db, source_id, markdown, markdown_truncated, source_digest, source_status):
    """Check bounded archived bytes locally; the HTTP result never contains them."""
    row = db.execute("""SELECT length(pdf_bytes) AS byte_count,typeof(pdf_bytes) AS archive_type,
        CASE WHEN typeof(pdf_bytes)='blob' AND length(pdf_bytes)<=? THEN pdf_bytes ELSE NULL END AS archive,
        CASE WHEN length(CAST(metadata_json AS BLOB))<=? THEN metadata_json ELSE NULL END AS metadata,
        length(CAST(metadata_json AS BLOB)) AS metadata_bytes
        FROM knowledge_pdf_versions WHERE source_id=?""",
        (MAX_PDF_ARCHIVE_BYTES, MAX_PDF_METADATA_BYTES, source_id)).fetchone()
    if row is None:
        return None
    result = {"pdf_conversion": None, "pdf_conversion_validation": "not_verified",
              "pdf_archive": {"available": True, "byte_count": row["byte_count"], "sha256": None,
                              "digest_matches_metadata": None, "digest_matches_source_version": None,
                              "verification_status": "not_verified"},
              "warnings": []}
    try:
        if row["metadata_bytes"] is None:
            if source_status == "ready":
                result["pdf_conversion_validation"] = "invalid_or_unavailable"
                result["warnings"].append("published_pdf_conversion_metadata_missing")
            else:
                result["pdf_conversion_validation"] = "not_available_yet"
        elif row["metadata_bytes"] > MAX_PDF_METADATA_BYTES:
            result["pdf_conversion_validation"] = "not_verified_metadata_limit"
            result["warnings"].append("pdf_conversion_metadata_size_limit")
        else:
            metadata = pdf_conversion_metadata(row["metadata"])
            result["pdf_conversion"] = metadata
            if metadata.get("original_pdf_sha256") != source_digest:
                result["warnings"].append("pdf_source_version_digest_mismatch")
            if markdown_truncated:
                result["pdf_conversion_validation"] = "not_verified_markdown_truncated"
                result["warnings"].append("pdf_conversion_validation_skipped_truncated_markdown")
            else:
                from .pdf_import import validate_conversion
                validate_conversion(markdown, metadata, source_digest)
                result["pdf_conversion_validation"] = "validated"
    except (ValueError, TypeError, KeyError, OverflowError):
        result["pdf_conversion_validation"] = "invalid_or_unavailable"
        result["warnings"].append("pdf_conversion_metadata_invalid_or_unavailable")
    archive = result["pdf_archive"]
    if row["archive_type"] != "blob":
        archive["verification_status"] = "invalid_archive_type"
        result["warnings"].append("pdf_archive_invalid_type")
    elif row["byte_count"] > MAX_PDF_ARCHIVE_BYTES:
        archive["verification_status"] = "not_verified_size_limit"
        result["warnings"].append("pdf_archive_digest_skipped_size_limit")
    else:
        archive["sha256"] = hashlib.sha256(row["archive"]).hexdigest()
        archive["digest_matches_source_version"] = archive["sha256"] == source_digest
        metadata = result["pdf_conversion"]
        if metadata is None or not isinstance(metadata.get("original_pdf_sha256"), str):
            archive["verification_status"] = "metadata_unavailable"
        else:
            archive["digest_matches_metadata"] = archive["sha256"] == metadata["original_pdf_sha256"]
            archive["verification_status"] = "digest_matches_metadata" if archive["digest_matches_metadata"] else "digest_mismatch"
            if not archive["digest_matches_metadata"]:
                result["warnings"].append("pdf_archive_digest_mismatch")
        if not archive["digest_matches_source_version"]:
            archive["verification_status"] = "digest_mismatch"
            result["warnings"].append("pdf_archive_source_version_digest_mismatch")
    return result


def source_details(config, source_id):
    result = {"source_id": source_id, "found": False, "version": None, "chunks": [],
              "active": False, "published": False, "desired": False, "truncated": False, "warnings": []}
    try:
        with _database(config) as db:
            if db is None or "knowledge_versions" not in _tables(db):
                return result
            version = db.execute("""SELECT source_id,path,digest,status,created_at,input_tokens,output_tokens,
                elapsed_seconds,error,scope,substr(raw_text,1,?) AS raw_text,length(raw_text) AS raw_text_characters
                FROM knowledge_versions WHERE source_id=? AND scope=?""", (MAX_SOURCE_CHARACTERS, source_id, _scope(config))).fetchone()
            if version is None:
                return result
            result["found"], result["version"] = True, dict(version)
            result["version"]["raw_text_truncated"] = version["raw_text_characters"] > MAX_SOURCE_CHARACTERS
            encoded = result["version"]["raw_text"].encode("utf-8")
            if len(encoded) > MAX_SOURCE_TEXT_BYTES:
                result["version"]["raw_text"] = encoded[:MAX_SOURCE_TEXT_BYTES].decode("utf-8", errors="ignore")
                result["version"]["raw_text_truncated"] = True
            result["truncated"] = result["version"]["raw_text_truncated"]
            tables = _tables(db)
            pdf = None
            if "knowledge_pdf_versions" in tables:
                pdf = _source_pdf_details(db, source_id, result["version"]["raw_text"],
                                          result["version"]["raw_text_truncated"], result["version"]["digest"], result["version"]["status"])
                if pdf is not None:
                    result.update({key: value for key, value in pdf.items() if key != "warnings"})
                    result["warnings"].extend(pdf["warnings"])
            if pdf is None and result["version"]["path"].casefold().endswith(".pdf"):
                result.update(pdf_conversion=None, pdf_conversion_validation="invalid_or_unavailable",
                              pdf_archive={"available": False, "byte_count": None, "sha256": None,
                                           "digest_matches_metadata": None, "digest_matches_source_version": None,
                                           "verification_status": "missing_archive"})
                result["warnings"].append("pdf_archive_or_conversion_provenance_missing")
            if "knowledge_chunks" in tables:
                chunk_count = db.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id=?", (source_id,)).fetchone()[0]
                chunks = db.execute("SELECT chunk_index,substr(text,1,?) AS text,length(text) AS text_characters,start_character,marks_json FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index LIMIT ?", (MAX_SOURCE_CHARACTERS, source_id, MAX_SOURCE_CHUNKS))
                result["truncated"] |= chunk_count > MAX_SOURCE_CHUNKS
                result["chunk_count"] = chunk_count
                source_bytes = len(scratch.canonical({"version": result["version"],
                    "pdf_conversion": result.get("pdf_conversion"), "pdf_archive": result.get("pdf_archive")}))
                for chunk in chunks:
                    item = dict(chunk)
                    item["marks"] = _json(item.pop("marks_json"), None)
                    if item["text_characters"] > len(item["text"]):
                        item["text_truncated"] = True
                        result["truncated"] = True
                    amount = len(scratch.canonical(item))
                    if source_bytes + amount > MAX_SOURCE_BYTES:
                        result["truncated"] = True
                        break
                    result["chunks"].append(item)
                    source_bytes += amount
            for label, table in (("published", "knowledge_published"), ("desired", "knowledge_desired")):
                if table in tables:
                    result[label] = db.execute(f"SELECT 1 FROM {table} WHERE scope=? AND source_id=?", (_scope(config), source_id)).fetchone() is not None
            if "memory_records" in tables:
                result["active"] = db.execute("SELECT 1 FROM memory_records WHERE scope=? AND source_id=? AND active=1 LIMIT 1", (_scope(config), source_id)).fetchone() is not None
    except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        result["warnings"].append("source_snapshot_unavailable")
    return result


def _verify_retained(rows, truncated, checkpoints, trace_id, view_partial):
    report = {"status": "retained_checks_passed" if rows else "no_retained_records", "lifecycle": "not_evaluated",
              "hash_chains": "validated_for_returned_files", "checked_records": 0, "issues": [],
              "warnings": ["semantic_support_not_evaluated", "full_lifecycle_use_console_scratch"]}
    if truncated:
        report["warnings"].append("view_truncated")
    if view_partial or any(trace_id in checkpoint["partial_trace_ids"] for checkpoint in checkpoints):
        report["warnings"].append("retention_partial")
    if view_partial and not checkpoints:
        report["warnings"].append("viewer_cutoff_removed_trace_records")
    retrievals = {}
    for row in rows:
        fields = row["fields"]
        try:
            if row["event"] == "memory_observation":
                validate_graph_audit(fields["audit"])
                if digest(fields["audit"]) != fields["audit_sha256"]:
                    raise ValueError("graph observation digest mismatch")
                report["checked_records"] += 1
            elif row["event"] == "retrieval_record":
                validate_retrieval(fields["record"])
                validate_graph_audit(fields["record"]["graph_audit"], fields["record"])
                if digest(fields["record"]) != fields["record_sha256"]:
                    raise ValueError("retrieval digest mismatch")
                retrievals[fields["record_sha256"]] = fields["record"]
                report["checked_records"] += 1
            elif row["event"] in {"answer_generated", "answer_delivered"}:
                retrieval = retrievals.get(fields.get("retrieval_sha256"))
                if retrieval is None:
                    report["warnings"].append("answer_retrieval_dependency_unavailable")
                else:
                    validate_answer(fields["record"], retrieval)
                    report["checked_records"] += 1
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, ZeroDivisionError):
            report["status"] = "invalid"
            report["issues"].append({"sequence": row["sequence"], "event": row["event"], "reason": "retained_record_check_failed"})
    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    return report


def trace_details(config, trace_id):
    view, rows, checkpoints, warnings = _scratch_view(config, _now())
    selected = [row for row in rows if row["fields"].get("trace_id") == trace_id]
    truncated = view["truncated"] or len(selected) > MAX_TRACE_RECORDS
    selected = selected[-MAX_TRACE_RECORDS:]
    retained, size = [], 0
    for row in reversed(selected):
        amount = len(scratch.canonical(row))
        if size + amount > MAX_TRACE_BYTES:
            truncated = True
            break
        retained.append(row)
        size += amount
    retained.reverse()
    relevant = [checkpoint for checkpoint in checkpoints if trace_id in checkpoint["partial_trace_ids"]]
    view_partial = trace_id in view.get("view_partial_trace_ids", [])
    verification = _verify_retained(retained, truncated, relevant, trace_id, view_partial)
    verification["warnings"].extend(warnings)
    return {"trace_id": trace_id, "cutoff": view["cutoff"], "records": retained, "checkpoints": relevant,
            "truncated": truncated, "verification": verification}
