"""Persistent, numeric-keyed source indexes for neighborhood-only queries.

Publication may inspect the source scope to update NPMI-derived indexes. Query
methods never load the scope's complete records, frequencies, labels or edges.
The legacy source tables remain authoritative; this module changes no formula,
corroboration gate, evidence order, or dynamic ranking policy.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .path_policy import validate_managed_path


SCHEMA_VERSION = 1
REQUIRED_QUERY_TABLES = frozenset({
    "memory_query_marks", "memory_query_scopes", "memory_query_frequencies",
    "memory_query_postings", "memory_query_trie", "memory_query_edges",
    "memory_query_adjacency", "memory_query_event_headers",
})
_LATIN = re.compile(r"^[A-Za-z0-9_.\-]+$")
_SELECTOR_HEADER_PREFIX = "selector_header_v1:"
_SELECTOR_HEADER_MAX_CHARS = 1024
_SELECTOR_POLICY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _selector_header_status(value):
    """Decode only the explicit, bounded extension of a replay header.

    Default statuses and the table/schema remain unchanged.  An injected
    event needs a durable selector marker so default replay can reject it
    without reading any original graph-audit payload.
    """
    if not isinstance(value, str):
        return value, None
    if not value.startswith(_SELECTOR_HEADER_PREFIX):
        if value.startswith("selector_header_"):
            raise ValueError("invalid selector event header")
        return value, None
    try:
        if len(value) > _SELECTOR_HEADER_MAX_CHARS:
            raise ValueError
        envelope = json.loads(value[len(_SELECTOR_HEADER_PREFIX):])
        if (not isinstance(envelope, dict)
                or set(envelope) != {"kind", "version", "status", "selector_binding"}
                or envelope["kind"] != "selector_event_header"
                or type(envelope["version"]) is not int or envelope["version"] != 1
                or not isinstance(envelope["status"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", envelope["status"])):
            raise ValueError
        binding = envelope["selector_binding"]
        if (not isinstance(binding, dict)
                or set(binding) != {"contract", "policy", "receipt_sha256"}
                or binding["contract"] != "selector_receipt_v1"
                or not isinstance(binding["receipt_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", binding["receipt_sha256"])):
            raise ValueError
        policy = binding["policy"]
        if (not isinstance(policy, dict) or set(policy) != {"id", "version"}
                or any(not isinstance(policy[key], str)
                       or not _SELECTOR_POLICY_ID.fullmatch(policy[key]) for key in policy)):
            raise ValueError
        return envelope["status"], binding
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid selector event header") from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hit(mark, text):
    if _LATIN.fullmatch(mark):
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(mark)
                              + r"(?![A-Za-z0-9_])", text, re.IGNORECASE))
    return mark.casefold() in text.casefold()


def lazy_decay(weight, last_updated, now, *, period_seconds, decay=0.99,
               round_digits=6):
    """Settle completed time periods without mutating any stored edge.

    The caller must explicitly choose a period: existing dynamic memory uses
    retrieval cycles, not elapsed seconds. This unused structural helper does
    not activate or redefine that policy. Iterative Python rounding matches
    repeated cycles and must not be replaced by a rounded exponentiation.
    """
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (weight, last_updated, now, period_seconds, decay)):
        raise ValueError("lazy decay values must be finite numbers")
    if period_seconds <= 0 or not 0 <= decay <= 1 or now < last_updated:
        raise ValueError("invalid lazy decay period, coefficient, or timestamp")
    if not isinstance(round_digits, int) or round_digits < 0:
        raise ValueError("round_digits must be a nonnegative integer")
    cycles = int((now - last_updated) // period_seconds)
    settled = weight
    for _ in range(cycles):
        updated = round(settled * decay, round_digits)
        if updated == settled:
            break
        settled = updated
    return {"weight": settled, "last_updated": last_updated + cycles * period_seconds,
            "cycles": cycles}


class KeyedMemoryIndex:
    """An additive derived index on the caller-owned SQLite connection.

    Schema installation and publication are explicit operations. In particular,
    constructing this object or querying it never rebuilds a source scope.
    IDs are global and never removed; existing directed coordinates are updated
    with UPSERT. Retired edge rows keep their IDs and are marked inactive.
    """

    def __init__(self, connection, self_marks=()):
        self.connection = connection
        self.self_marks = frozenset(self_marks)
        self.schema_recreated = False
        self.missing_tables = frozenset()

    def _backup_migration(self, tables):
        if (REQUIRED_QUERY_TABLES <= tables
                or not ("memory_records" in tables or REQUIRED_QUERY_TABLES & tables)):
            return None
        filename = next(row[2] for row in self.connection.execute("PRAGMA database_list")
                        if row[1] == "main")
        if not filename:
            return None
        if self.connection.in_transaction:
            raise ValueError("memory_query_migration_requires_committed_store")
        directory = Path(filename).parent / "migration_backups"
        validate_managed_path(directory, "memory query migration backups")
        directory.mkdir(parents=True, exist_ok=True)
        validate_managed_path(directory, "memory query migration backups")
        destination = directory / ("memory.before-query-index-" + uuid.uuid4().hex + ".sqlite3")
        with destination.open("xb"):
            pass
        backup = sqlite3.connect(destination)
        try:
            self.connection.backup(backup)
        finally:
            backup.close()
        return destination

    def ensure_schema(self):
        """Back up an existing disk database before the additive migration."""
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'")}
        self.missing_tables = REQUIRED_QUERY_TABLES - tables
        self.schema_recreated = bool(self.missing_tables)
        backup = self._backup_migration(tables)
        if "memory_query_scopes" in tables and "memory_query_marks" not in tables:
            # The ID map is authoritative for stable coordinates. Other
            # derived tables can be republished; recreating this map from
            # active source labels could silently reassign existing IDs.
            raise ValueError("memory_query_mark_ids_missing; restore authoritative mapping during maintenance")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS memory_query_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS memory_query_scopes (
                scope TEXT PRIMARY KEY, active_record_count INTEGER NOT NULL,
                known_marks_count INTEGER NOT NULL, live_edge_count INTEGER NOT NULL,
                revision INTEGER NOT NULL, self_marks_json TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_query_frequencies (
                scope TEXT NOT NULL, mark_id INTEGER NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(scope,mark_id)
            );
            CREATE TABLE IF NOT EXISTS memory_query_postings (
                scope TEXT NOT NULL, mark_id INTEGER NOT NULL, record_id INTEGER NOT NULL,
                PRIMARY KEY(scope,mark_id,record_id)
            );
            CREATE TABLE IF NOT EXISTS memory_query_trie (
                scope TEXT NOT NULL, kind TEXT NOT NULL, prefix TEXT NOT NULL,
                terminals_json TEXT NOT NULL, PRIMARY KEY(scope,kind,prefix)
            );
            CREATE TABLE IF NOT EXISTS memory_query_edges (
                scope TEXT NOT NULL, i INTEGER NOT NULL, j INTEGER NOT NULL,
                a TEXT NOT NULL, b TEXT NOT NULL, static_score REAL NOT NULL,
                co_count INTEGER NOT NULL, context_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL, ordinal INTEGER NOT NULL,
                last_updated REAL NOT NULL, primary_edge INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(scope,i,j)
            );
            CREATE TABLE IF NOT EXISTS memory_query_adjacency (
                scope TEXT NOT NULL, mark_id INTEGER NOT NULL,
                i INTEGER NOT NULL, j INTEGER NOT NULL, neighbor_id INTEGER NOT NULL,
                neighbor TEXT NOT NULL, static_score REAL NOT NULL,
                PRIMARY KEY(scope,mark_id,i,j)
            );
            CREATE INDEX IF NOT EXISTS memory_query_neighbors
                ON memory_query_adjacency(scope,mark_id,static_score DESC,neighbor);
            CREATE TABLE IF NOT EXISTS memory_query_event_headers (
                scope TEXT NOT NULL, event_id TEXT NOT NULL, query TEXT NOT NULL,
                observed_at REAL NOT NULL, ranking_mode TEXT NOT NULL,
                direct_hit_count INTEGER NOT NULL, original_changes_known INTEGER NOT NULL,
                observation_status TEXT NOT NULL, changed_edges_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL, PRIMARY KEY(scope,event_id)
            );
            CREATE TRIGGER IF NOT EXISTS memory_query_event_headers_no_update
                BEFORE UPDATE ON memory_query_event_headers
                BEGIN SELECT RAISE(ABORT, 'memory query event header is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS memory_query_event_headers_no_delete
                BEFORE DELETE ON memory_query_event_headers
                BEGIN SELECT RAISE(ABORT, 'memory query event header is immutable'); END;
        """)
        return backup

    def _source_edges(self, scope):
        edges = {}
        for a, b, context, evidence, count in self.connection.execute(
                "SELECT a,b,context_json,evidence_json,co_count FROM memory_support WHERE scope=?",
                (scope,)):
            edges[(a, b)] = {"static_score": 0.0, "context": context,
                             "source_record_ids": evidence, "co_count": count}
        for a, b, score, context, evidence, count in self.connection.execute(
                "SELECT a,b,weight,context_json,evidence_json,co_count FROM memory_static WHERE scope=?",
                (scope,)):
            edges[(a, b)] = {"static_score": score, "context": context,
                             "source_record_ids": evidence, "co_count": count}
        return edges

    def rebuild_scope(self, scope, records, edges=None, *, revision=0, updated_at=None):
        """Publish a derived scope after ingestion/migration, within its transaction.

        Raw JSON is retained when already supplied by source rows. The original
        support insertion order followed by static overrides supplies ordinals,
        so direct-evidence floating sums retain the legacy sequence.
        ``updated_at`` is a publication timestamp, defaulting to the current
        wall clock, rather than a source creation time or active decay checkpoint.
        Changed rows advance it monotonically; unchanged rows preserve it.
        """
        if not self.connection.in_transaction:
            raise ValueError("memory query publication requires a caller-owned transaction")
        timestamp = time.time() if updated_at is None else updated_at
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError("memory query publication timestamp must be finite")
        if edges is None:
            edges = self._source_edges(scope)
        labels = set()
        counts = Counter()
        postings = []
        for record in records:
            marks = set(record["marks"]) - self.self_marks
            counts.update(marks)
            labels.update(marks)
            postings.extend((mark, record["id"]) for mark in marks)
        for pair in edges:
            if not all(isinstance(mark, str) for mark in pair):
                raise ValueError("memory query index requires text edge labels")
            labels.update(pair)
        self.connection.executemany("INSERT OR IGNORE INTO memory_query_marks(label) VALUES(?)",
                                    ((label,) for label in sorted(labels)))
        ids = {label: self.connection.execute(
            "SELECT id FROM memory_query_marks WHERE label=?", (label,)).fetchone()[0]
               for label in labels}
        for table in ("memory_query_frequencies", "memory_query_postings",
                      "memory_query_trie", "memory_query_adjacency"):
            self.connection.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
        self.connection.executemany("INSERT INTO memory_query_frequencies VALUES(?,?,?)",
                                    ((scope, ids[label], count) for label, count in counts.items()))
        self.connection.executemany("INSERT INTO memory_query_postings VALUES(?,?,?)",
                                    ((scope, ids[label], record_id) for label, record_id in postings))
        nodes = {}
        for mark in counts:
            kind = "latin" if _LATIN.fullmatch(mark) else "substring"
            normalized = mark.lower() if kind == "latin" else mark.casefold()
            for end in range(len(normalized) + 1):
                nodes.setdefault((kind, normalized[:end]), [])
            nodes[(kind, normalized)].append(mark)
        self.connection.executemany("INSERT INTO memory_query_trie VALUES(?,?,?,?)",
                                    ((scope, kind, prefix, _json(sorted(terminals)))
                                     for (kind, prefix), terminals in nodes.items()))
        self.connection.execute("UPDATE memory_query_edges SET active=0 WHERE scope=?", (scope,))
        edge_rows = {}
        adjacency = []
        for ordinal, ((a, b), edge) in enumerate(edges.items()):
            i, j = ids[a], ids[b]
            context = edge["context"]
            evidence = edge["source_record_ids"]
            context = context if isinstance(context, str) else _json(context)
            evidence = evidence if isinstance(evidence, str) else _json(evidence)
            row = (scope, i, j, a, b, edge["static_score"], edge["co_count"],
                   context, evidence, ordinal, timestamp, 1, 1)
            edge_rows[(i, j)] = row
            # Symmetric NPMI has two addressable directed coordinates. Actual
            # legacy reverse rows, when present, keep their independent data.
            if (b, a) not in edges and i != j:
                edge_rows[(j, i)] = (scope, j, i, b, a, edge["static_score"],
                    edge["co_count"], context, evidence, ordinal, timestamp, 0, 1)
            for mark, neighbor in ((a, b), (b, a)) if i != j else ((a, b),):
                adjacency.append((scope, ids[mark], i, j, ids[neighbor], neighbor,
                                  edge["static_score"]))
        self.connection.executemany("""
            INSERT INTO memory_query_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(scope,i,j) DO UPDATE SET
                a=excluded.a,b=excluded.b,static_score=excluded.static_score,
                co_count=excluded.co_count,context_json=excluded.context_json,
                evidence_json=excluded.evidence_json,ordinal=excluded.ordinal,
                last_updated=CASE WHEN memory_query_edges.static_score!=excluded.static_score
                    OR memory_query_edges.co_count!=excluded.co_count
                    OR memory_query_edges.context_json!=excluded.context_json
                    OR memory_query_edges.evidence_json!=excluded.evidence_json
                    THEN max(memory_query_edges.last_updated,excluded.last_updated)
                    ELSE memory_query_edges.last_updated END,
                primary_edge=excluded.primary_edge,active=1
        """, edge_rows.values())
        self.connection.executemany("INSERT INTO memory_query_adjacency VALUES(?,?,?,?,?,?,?)", adjacency)
        self.connection.execute("""
            INSERT INTO memory_query_scopes VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(scope) DO UPDATE SET active_record_count=excluded.active_record_count,
                known_marks_count=excluded.known_marks_count,live_edge_count=excluded.live_edge_count,
                revision=excluded.revision,self_marks_json=excluded.self_marks_json,
                schema_version=excluded.schema_version
        """, (scope, len(records), len(counts),
              sum(edge["static_score"] > 0 for edge in edges.values()), revision,
              _json(sorted(self.self_marks)), SCHEMA_VERSION))

    def meta(self, scope):
        row = self.connection.execute(
            "SELECT active_record_count,known_marks_count,live_edge_count,revision,"
            "self_marks_json,schema_version FROM memory_query_scopes WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return None
        return {"active_record_count": row[0], "known_marks_count": row[1],
                "live_edge_count": row[2], "revision": row[3],
                "self_marks": frozenset(json.loads(row[4])), "schema_version": row[5]}

    def mark_ids(self, scope, labels):
        result = {}
        for label in set(labels):
            row = self.connection.execute(
                "SELECT m.id FROM memory_query_marks m JOIN memory_query_frequencies f "
                "ON f.mark_id=m.id AND f.scope=? WHERE m.label=?", (scope, label)).fetchone()
            if row:
                result[label] = row[0]
        return result

    def frequencies(self, scope, labels):
        result = {}
        for label in sorted(set(labels)):
            row = self.connection.execute(
                "SELECT f.count FROM memory_query_marks m JOIN memory_query_frequencies f "
                "ON f.mark_id=m.id AND f.scope=? WHERE m.label=?", (scope, label)).fetchone()
            if row:
                result[label] = row[0]
        return result

    def literal_matches(self, scope, query):
        """Walk persisted prefix nodes; validate candidates with the exact gate."""
        special = {"İ": "i", "ı": "i", "ſ": "s", "K": "k"}
        normalized = "".join(special.get(c, c.lower() if "A" <= c <= "Z" else c)
                             for c in query)
        candidates, cached = set(), {}

        def terminal(kind, prefix):
            key = kind, prefix
            if key not in cached:
                row = self.connection.execute(
                    "SELECT terminals_json FROM memory_query_trie "
                    "WHERE scope=? AND kind=? AND prefix=?", (scope, kind, prefix)).fetchone()
                cached[key] = None if row is None else json.loads(row[0])
            return cached[key]

        def ascii_word(character):
            return ("A" <= character <= "Z" or "a" <= character <= "z"
                    or "0" <= character <= "9" or character == "_")

        for start in range(len(query)):
            if start and ascii_word(query[start - 1]):
                continue
            for end in range(start, len(query)):
                terminals = terminal("latin", normalized[start:end + 1])
                if terminals is None:
                    break
                if end + 1 == len(query) or not ascii_word(query[end + 1]):
                    candidates.update(terminals)
        candidates.update(terminal("substring", "") or ())
        folded = query.casefold()
        for start in range(len(folded)):
            for end in range(start, len(folded)):
                terminals = terminal("substring", folded[start:end + 1])
                if terminals is None:
                    break
                candidates.update(terminals)
        return {mark for mark in candidates if _hit(mark, query)}

    def incident_edges(self, scope, hits):
        """Load positive direct-hit incident evidence, retaining source order."""
        found = {}
        for mark_id in self.mark_ids(scope, hits).values():
            for row in self.connection.execute(
                    "SELECT e.a,e.b,e.static_score,e.co_count,e.context_json,e.evidence_json,e.ordinal "
                    "FROM memory_query_adjacency x JOIN memory_query_edges e "
                    "ON e.scope=x.scope AND e.i=x.i AND e.j=x.j "
                    "WHERE x.scope=? AND x.mark_id=? AND x.static_score>0 "
                    "AND e.active=1 AND e.primary_edge=1", (scope, mark_id)):
                found[(row[0], row[1])] = row
        edges, ordinals = {}, {}
        for pair, row in sorted(found.items(), key=lambda item: item[1][6]):
            edges[pair] = {"static_score": row[2], "co_count": row[3],
                           "context": json.loads(row[4]), "source_record_ids": json.loads(row[5])}
            ordinals[pair] = row[6]
        return edges, ordinals

    def records_for_marks(self, scope, labels):
        """Union keyed postings, then load only those active source record IDs."""
        record_ids = set()
        for mark_id in self.mark_ids(scope, labels).values():
            record_ids.update(row[0] for row in self.connection.execute(
                "SELECT record_id FROM memory_query_postings WHERE scope=? AND mark_id=?",
                (scope, mark_id)))
        records = []
        for record_id in sorted(record_ids):
            row = self.connection.execute(
                "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at "
                "FROM memory_records WHERE id=? AND scope=? AND active=1", (record_id, scope)).fetchone()
            if row:
                records.append({"id": row[0], "scope": row[1], "source_id": row[2],
                    "author_id": row[3], "text": row[4], "quote": row[5], "marks": json.loads(row[6]),
                    "created_at": row[7], "active": bool(row[8]), "archived_at": row[9]})
        return records

    def event_header(self, scope, event_id):
        """Read the fixed replay metadata without parsing a historical graph."""
        row = self.connection.execute(
            "SELECT query,observed_at,ranking_mode,direct_hit_count,original_changes_known,"
            "observation_status,changed_edges_count,payload_sha256 "
            "FROM memory_query_event_headers WHERE scope=? AND event_id=?",
            (scope, event_id)).fetchone()
        if row is None:
            return None
        status, selector_binding = _selector_header_status(row[5])
        header = {"query": row[0], "observed_at": row[1], "ranking_mode": row[2],
                "direct_hit_count": row[3], "original_changes_known": bool(row[4]),
                "observation_status": status, "changed_edges_count": row[6],
                "payload_sha256": row[7]}
        if selector_binding is not None:
            header["selector_binding"] = selector_binding
        return header

    def publish_event_header(self, scope, event_id, serialized):
        """Add an immutable header derived from the exact immutable payload.

        Called when writing a fresh audit, or by startup migration. Retrieval
        replays use ``event_header`` and never parse the original whole graph.
        """
        payload = json.loads(serialized)
        if payload["scope"] != scope or payload["event_id"] != event_id:
            raise ValueError("memory query event header identity mismatch")
        observation = payload["observation"]
        header = {"query": payload["request"]["query"],
            "observed_at": payload["request"]["observed_at"],
            "ranking_mode": payload["selection"].get("ranking_mode", "dynamic"),
            "direct_hit_count": len(payload["match"]["direct_hits"]),
            "original_changes_known": bool(observation["original_changes_known"]),
            "observation_status": observation["status"],
            "changed_edges_count": len(observation["changed_edges"]),
            "payload_sha256": hashlib.sha256(serialized.encode()).hexdigest()}
        stored_status = header["observation_status"]
        selection = payload["selection"]
        if "selector_contract" in selection or "selector_receipt" in selection:
            receipt = selection.get("selector_receipt")
            if (selection.get("selector_contract") != "selector_receipt_v1"
                    or not isinstance(receipt, dict) or receipt.get("schema") != "selector_receipt_v1"):
                raise ValueError("invalid selector event header")
            binding = {"contract": selection["selector_contract"], "policy": receipt.get("policy"),
                       "receipt_sha256": hashlib.sha256(_json(receipt).encode()).hexdigest()}
            stored_status = _SELECTOR_HEADER_PREFIX + _json({"kind": "selector_event_header", "version": 1,
                "status": header["observation_status"], "selector_binding": binding})
            _, header["selector_binding"] = _selector_header_status(stored_status)
        self.connection.execute(
            "INSERT OR IGNORE INTO memory_query_event_headers VALUES(?,?,?,?,?,?,?,?,?,?)",
            (scope, event_id, header["query"], header["observed_at"], header["ranking_mode"],
             header["direct_hit_count"], int(header["original_changes_known"]),
             stored_status, header["changed_edges_count"], header["payload_sha256"]))
        if self.event_header(scope, event_id) != header:
            raise ValueError("memory query immutable event header mismatch")
        return header

    def migrate_missing_event_headers(self):
        """Startup-only additive migration; preserve all original audit bytes."""
        if not self.connection.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='memory_event_audits'").fetchone():
            return 0
        count = 0
        for scope, event_id, serialized in self.connection.execute(
                "SELECT a.scope,a.event_id,a.payload_json FROM memory_event_audits a "
                "LEFT JOIN memory_query_event_headers h ON h.scope=a.scope AND h.event_id=a.event_id "
                "WHERE h.event_id IS NULL"):
            self.publish_event_header(scope, event_id, serialized)
            count += 1
        return count
