"""Auditable, scope-isolated mark memory adapted from hebbian-mark-graph.

Upstream: Mingyuliu-botaaa/hebbian-mark-graph, MIT,
commit 8769b9ed0af3531b9fbc09fb6e084d8e7adf724d (2026).
NPMI = log(p_ab / (p_a * p_b)) / -log(p_ab), with p_ab == 1 -> 1.
Only positive static edges survive. Dynamic weights are seeded once from
static edges, remain separate, and use w <- 0.99*w + 1 per co-activated
pair. A retrieval event is one explicit decay cycle; retries of the same
event do neither. Historical records and annotation alone never reinforce.

Current retrieval uses only positive static NPMI edges. Dynamic weights
remain a shadow layer for audit and never affect the default one-hop
expansion or ranking. The explicit dynamic mode is a deferred extension;
where selected, a dynamic weight replaces its static weight. Direct
matches have priority in either mode.
The graph is not a probability model: dynamic weights can exceed one.
The upstream source-context gate and self-name exclusion still apply.
Archived source versions retain their records and dynamic weights, but
live edges require a co-occurring pair in an active sourced fact. This
prevents an old dynamic association from bypassing source retirement.

Integration extensions: a sourced, model-labelled fact replaces a diary
file as the static co-occurrence unit. Durable event IDs replace timestamps
as processing cursors. The caller must validate each fact's source quote.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import sqlite3
import time
from collections import Counter
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from typing import Any

from .identity import AGENT_NAME, SELF_NAME_ALIASES


UPSTREAM_COMMIT = "8769b9ed0af3531b9fbc09fb6e084d8e7adf724d"
ETA = 1.0
DECAY = 0.99
MAX_DIRECT = 4
MAX_EXTRA = 2
MAX_REFERENCES = 3
MAX_TEXT_CHARS = 400
MAX_ENTRY_CHARS = 110
_LATIN = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _canonical(mark: str) -> str:
    return mark.strip().casefold()


def _hit(mark: str, text: str) -> bool:
    if _LATIN.fullmatch(mark):
        # Check both ends; digits and underscores are also token characters.
        return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(mark)
                              + r"(?![A-Za-z0-9_])", text, re.IGNORECASE))
    return mark.casefold() in text.casefold()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _IndexedRecords(list):
    """Private, committed source snapshot; never returned to the caller."""

    def __init__(self, records, self_marks):
        super().__init__(records)
        self.self_marks = frozenset(self_marks)
        frequencies = Counter()
        self.postings = {}
        for ordinal, record in enumerate(records):
            marks = set(record["marks"]) - self_marks
            frequencies.update(marks)
            for mark in marks:
                self.postings.setdefault(mark, []).append(ordinal)
        self.known = set(frequencies)
        self.frequencies = dict(sorted(frequencies.items()))
        self.latin_trie, self.substring_trie = {}, {}
        for mark in self.known:
            latin = bool(_LATIN.fullmatch(mark))
            node = self.latin_trie if latin else self.substring_trie
            for character in mark.lower() if latin else mark.casefold():
                node = node.setdefault(character, {})
            node.setdefault(None, []).append(mark)

    def literal_matches(self, query):
        """Find candidate labels by query prefixes, then use the exact gate.

        ASCII regex IGNORECASE additionally matches İ, ı, ſ and K. Its
        lookarounds still use the original query and regex flags. The
        non-Latin matcher instead searches casefolded substrings, including
        matches inside a multi-character casefold expansion.
        """
        special = {"İ": "i", "ı": "i", "ſ": "s", "K": "k"}
        normalized = "".join(special.get(c, c.lower() if "A" <= c <= "Z" else c)
                             for c in query)
        candidates = set(self.substring_trie.get(None, ()))

        def ascii_word(character):
            return ("A" <= character <= "Z" or "a" <= character <= "z"
                    or "0" <= character <= "9" or character == "_")

        for start in range(len(query)):
            if start and ascii_word(query[start - 1]):
                continue
            node = self.latin_trie
            for end in range(start, len(query)):
                node = node.get(normalized[end])
                if node is None:
                    break
                if end + 1 == len(query) or not ascii_word(query[end + 1]):
                    candidates.update(node.get(None, ()))
        folded = query.casefold()
        for start in range(len(folded)):
            node = self.substring_trie
            for end in range(start, len(folded)):
                node = node.get(folded[end])
                if node is None:
                    break
                candidates.update(node.get(None, ()))
        return {mark for mark in candidates if _hit(mark, query)}


class _IndexedEdges(dict):
    """Fresh dynamic values with source-order and incident-edge indexes."""

    def __init__(self, edges, index, ranking_mode):
        super().__init__(edges)
        self.ordered_pairs = index.ordered_pairs
        self.ordinals = index.edge_ordinals
        self.incident = index.incident
        self.static_neighbors = index.static_neighbors if ranking_mode == "static" else None


class _QueryEdges(_IndexedEdges):
    """Complete hit-incident evidence, with the full graph count retained."""

    def __init__(self, edges, index):
        super().__init__(edges, index, "static")
        self.ordered_pairs = tuple(sorted(edges))
        self.live_edge_count = index.static_live_edge_count
        self.statistics_scope = "direct_hit_incident_v1"


class _RetrievalIndex:
    def __init__(self, records, self_marks):
        self.records = _IndexedRecords(records, self_marks)
        self.edges = None
        self.decoded_edges = {}

    def index_edges(self, edges):
        self.edges = edges
        self.indexable_edges = all(isinstance(mark, str) for pair in edges for mark in pair)
        if not self.indexable_edges:
            # SQLite TEXT affinity can still contain legacy BLOB values.
            # The old path sorts only live edges; do not inspect inactive
            # mixed-type keys earlier than that policy does.
            return
        self.ordered_pairs = tuple(sorted(edges))
        self.edge_ordinals = {pair: ordinal for ordinal, pair in enumerate(edges)}
        self.incident = {}
        for pair in edges:
            for mark in pair:
                self.incident.setdefault(mark, []).append(pair)
        self.static_neighbors = None

    def index_static_neighbors(self):
        self.static_live_edge_count = sum(edge["static_score"] > 0 for edge in self.edges.values())
        self.static_neighbors = {}
        for mark, pairs in self.incident.items():
            self.static_neighbors[mark] = tuple(sorted(
                (pair for pair in pairs if self.edges[pair]["static_score"] > 0),
                key=lambda pair: (-self.edges[pair]["static_score"],
                                  pair[1] if pair[0] == mark else pair[0])))


class MemoryGraph:
    """One caller-owned SQLite connection; methods commit atomic updates.

    Pass the Discord message ID as ``event_id`` when retrieving. Otherwise
    identical scope/query/marks/now values identify the same event. A new
    timestamp deliberately identifies a new real retrieval.
    """

    def __init__(self, connection: sqlite3.Connection,
                 self_marks: tuple[str, ...] = (AGENT_NAME,)) -> None:
        self.connection = connection
        self._retrieval_cache = {}
        self._retrieval_index = None
        self._retrieval_token = None
        self._tracking_connection = None
        self._tracking_schema = None
        self._cache_shadows = False
        self.self_marks = {_canonical(mark) for mark in self_marks}
        if self.self_marks & SELF_NAME_ALIASES:
            self.self_marks.update(SELF_NAME_ALIASES)
        had_support = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_support'").fetchone()
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS memory_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, source_id TEXT NOT NULL,
                author_id TEXT NOT NULL, text TEXT NOT NULL, quote TEXT NOT NULL,
                marks_json TEXT NOT NULL, created_at REAL NOT NULL,
                fingerprint TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                archived_at REAL,
                UNIQUE(scope, source_id, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS memory_records_scope
                ON memory_records(scope);
            CREATE TABLE IF NOT EXISTS memory_static (
                scope TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
                weight REAL NOT NULL, context_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL, co_count INTEGER NOT NULL,
                PRIMARY KEY(scope, a, b)
            );
            CREATE TABLE IF NOT EXISTS memory_dynamic (
                scope TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
                weight REAL NOT NULL, context_json TEXT NOT NULL,
                seed_weight REAL NOT NULL, last_event_id TEXT,
                PRIMARY KEY(scope, a, b)
            );
            CREATE TABLE IF NOT EXISTS memory_support (
                scope TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL,
                context_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                co_count INTEGER NOT NULL, PRIMARY KEY(scope, a, b)
            );
            CREATE TABLE IF NOT EXISTS memory_scopes (
                scope TEXT PRIMARY KEY, dynamic_seeded INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memory_events (
                scope TEXT NOT NULL, event_id TEXT NOT NULL,
                observed_at REAL NOT NULL, query TEXT NOT NULL,
                marks_json TEXT NOT NULL, PRIMARY KEY(scope, event_id)
            );
            CREATE TABLE IF NOT EXISTS memory_cycles (
                scope TEXT NOT NULL, cycle_id TEXT NOT NULL,
                event_id TEXT NOT NULL, applied_at REAL NOT NULL,
                decay REAL NOT NULL, PRIMARY KEY(scope, cycle_id),
                UNIQUE(scope, event_id)
            );
            CREATE TABLE IF NOT EXISTS memory_event_audits (
                scope TEXT NOT NULL, event_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(scope,event_id)
            );
            CREATE TRIGGER IF NOT EXISTS memory_event_audits_no_update
                BEFORE UPDATE ON memory_event_audits
                BEGIN SELECT RAISE(ABORT, 'memory event audit is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS memory_event_audits_no_delete
                BEFORE DELETE ON memory_event_audits
                BEGIN SELECT RAISE(ABORT, 'memory event audit is immutable'); END;
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_records)")}
        migrated = "active" not in columns
        with connection:
            if "active" not in columns:
                connection.execute("ALTER TABLE memory_records ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE memory_records ADD COLUMN archived_at REAL")
            if not had_support or migrated:
                scopes = [row[0] for row in connection.execute("SELECT DISTINCT scope FROM memory_records")]
                for scope in scopes:
                    self._rebuild_static(scope)
            # A former self-name may have been labelled as a normal mark.
            # Only derived caches change: records, dynamic weights and frozen
            # event evidence retain their original values and identity.
            affected_scopes = set()
            for table in ("memory_static", "memory_support"):
                for scope, a, b, context_json in connection.execute(
                        f"SELECT scope,a,b,context_json FROM {table}"):
                    cached_marks = {a, b, *json.loads(context_json)}
                    if cached_marks & self.self_marks:
                        affected_scopes.add(scope)
            for scope in sorted(affected_scopes):
                self._rebuild_static(scope)
        self._ensure_cache_tracking()

    def _schema_versions(self):
        return (self.connection.execute("PRAGMA main.schema_version").fetchone()[0],
                self.connection.execute("PRAGMA temp.schema_version").fetchone()[0])

    def _ensure_cache_tracking(self):
        """Connection-local revisions change only with source graph DML.

        TEMP state and triggers do not migrate or change persisted evidence.
        Their revisions roll back together with publication transactions.
        data_version additionally covers committed writes by other connections.
        """
        schema = self._schema_versions()
        if self._tracking_connection is self.connection and self._tracking_schema == schema:
            return
        self._retrieval_cache.clear()
        self._cache_shadows = bool(self.connection.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type='table' "
            "AND name IN ('memory_records','memory_static','memory_support') LIMIT 1").fetchone())
        self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS indeces_retrieval_revisions "
                                "(scope TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
        existing = {name: (table, sql) for name, table, sql in self.connection.execute(
            "SELECT name,tbl_name,sql FROM temp.sqlite_master WHERE type='trigger'")}

        def install(name, table, timing, operation, body):
            sql = (f"CREATE TRIGGER {name} {timing} {operation.upper()} "
                   f"ON main.{table} BEGIN {body} END")
            if existing.get(name) != (table, sql):
                # DDL may rename the source table or remove the trigger.
                # Another Graph on this connection can share valid tracking.
                self.connection.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
                self.connection.execute(sql.replace("CREATE TRIGGER", "CREATE TEMP TRIGGER", 1))

        for table in ("memory_records", "memory_static", "memory_support"):
            for operation in ("insert", "update", "delete"):
                name = f"indeces_retrieval_{table}_{operation}"
                row = "OLD" if operation == "delete" else "NEW"
                body = (f"INSERT INTO indeces_retrieval_revisions VALUES({row}.scope,1) "
                        "ON CONFLICT(scope) DO UPDATE SET revision=revision+1;")
                if operation == "update":
                    body += ("INSERT INTO indeces_retrieval_revisions "
                             "SELECT OLD.scope,1 WHERE OLD.scope != NEW.scope "
                             "ON CONFLICT(scope) DO UPDATE SET revision=revision+1;")
                install(name, table, "AFTER", operation, body)
        # With recursive_triggers OFF, REPLACE does not run DELETE triggers
        # for the conflicting row. The records' global ID can move across
        # scopes, so invalidate that old scope before its implicit deletion.
        for operation in ("insert", "update"):
            name = f"indeces_retrieval_records_replace_{operation}"
            body = ("INSERT INTO indeces_retrieval_revisions "
                "SELECT scope,1 FROM main.memory_records WHERE id=NEW.id AND scope != NEW.scope "
                "ON CONFLICT(scope) DO UPDATE SET revision=revision+1;")
            install(name, "memory_records", "BEFORE", operation, body)
        self._tracking_connection = self.connection
        self._tracking_schema = self._schema_versions()

    def _cache_token(self, scope):
        revision = self.connection.execute(
            "SELECT revision FROM temp.indeces_retrieval_revisions WHERE scope=?", (scope,)).fetchone()
        return (self.connection.execute("PRAGMA main.data_version").fetchone()[0],
                self._schema_versions(), revision[0] if revision else 0,
                frozenset(self.self_marks))

    def _source_edges(self, scope):
        edges = {}
        for a, b, context, evidence, count in self.connection.execute(
                "SELECT a,b,context_json,evidence_json,co_count FROM memory_support WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": 0.0, "context": context,
                             "source_record_ids": evidence, "co_count": count}
        for a, b, weight, context, evidence, count in self.connection.execute(
                "SELECT a,b,weight,context_json,evidence_json,co_count FROM memory_static WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": weight, "context": context,
                             "source_record_ids": evidence, "co_count": count}
        return edges

    def _indexed_sources(self, scope, token):
        cached = self._retrieval_cache.get(scope)
        if cached is None or cached[0] != token:
            index = _RetrievalIndex(self._records(scope), self.self_marks)
            self._retrieval_cache[scope] = (token, index)
            return index
        return cached[1]

    @contextmanager
    def _retrieval_transaction(self, scope):
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self._ensure_cache_tracking()
                token = self._cache_token(scope)
                index = self._indexed_sources(scope, token)
                self._retrieval_index = index
                self._retrieval_token = token
                yield index
                # An application trigger or subclass may modify sources
                # during observation. Never certify the old index as new.
                if self._cache_shadows or self._cache_token(scope) != token:
                    self._retrieval_cache.pop(scope, None)
        except BaseException:
            # This includes commit failure, interruption and cancellation.
            # Uncommitted source snapshots must never survive rollback.
            self._retrieval_cache.clear()
            raise
        finally:
            self._retrieval_index = None
            self._retrieval_token = None

    def _marks(self, marks: list[str]) -> list[str]:
        if not isinstance(marks, list) or not all(isinstance(m, str) for m in marks):
            raise ValueError("marks must be a list of strings")
        return sorted({_canonical(m) for m in marks if m.strip()}
                      - self.self_marks)

    @staticmethod
    def _validate_scope_time(scope: str, now: float) -> None:
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope must be a nonempty string")
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now must be a finite timestamp")

    @staticmethod
    def _record(row: Any) -> dict[str, Any]:
        return {"id": row[0], "scope": row[1], "source_id": row[2],
                "author_id": row[3], "text": row[4], "quote": row[5],
                "marks": json.loads(row[6]), "created_at": row[7],
                "active": bool(row[8]), "archived_at": row[9]}

    def _records(self, scope: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at "
            "FROM memory_records WHERE scope=? AND active=1 ORDER BY id", (scope,))
        return [self._record(row) for row in rows]

    def add(self, scope: str, source_id: str, author_id: str,
            facts: list[dict[str, Any]], now: float, *, commit: bool = True) -> list[dict[str, Any]]:
        """Store sourced facts once and rebuild only this scope's static graph.

        Re-adding identical source/text/quote retains its original marks.
        The caller, rather than an LLM claim, establishes quote validity.
        """
        self._validate_scope_time(scope, now)
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a nonempty string")
        if not isinstance(author_id, str):
            raise ValueError("author_id must be a string")
        if not isinstance(facts, list):
            raise ValueError("facts must be a list")
        prepared = []
        for fact in facts:
            if not isinstance(fact, dict):
                raise ValueError("each fact must be an object")
            text, quote = fact.get("text"), fact.get("quote")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("fact text must be nonempty")
            if not isinstance(quote, str) or not quote:
                raise ValueError("fact quote must be nonempty")
            marks = self._marks(fact.get("marks", []))
            fingerprint = hashlib.sha256(_json([text, quote]).encode()).hexdigest()
            prepared.append((text, quote, marks, fingerprint))
        result = []
        if not commit and not self.connection.in_transaction:
            raise ValueError("commit=False requires a caller-owned transaction")
        with self.connection if commit else nullcontext():
            changed = False
            for text, quote, marks, fingerprint in prepared:
                cur = self.connection.execute(
                    "INSERT OR IGNORE INTO memory_records "
                    "(scope,source_id,author_id,text,quote,marks_json,created_at,fingerprint) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (scope, source_id, author_id, text, quote, _json(marks), now, fingerprint))
                changed = changed or bool(cur.rowcount)
                row = self.connection.execute(
                    "SELECT id,scope,source_id,author_id,text,quote,marks_json,created_at,active,archived_at "
                    "FROM memory_records WHERE scope=? AND source_id=? AND fingerprint=?",
                    (scope, source_id, fingerprint)).fetchone()
                result.append(self._record(row))
            if changed:
                self._rebuild_static(scope)
        return result

    def deactivate_source(self, scope: str, source_id: str, *, commit: bool = True) -> int:
        """Archive a superseded knowledge version without deleting evidence.

        Only matching active records are retired. Repeating the request is
        harmless. Dynamic history is preserved, while retrieval and static
        support now consider only active sourced facts. Re-adding the old
        exact source/text/quote does not silently reactivate it.
        """
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope must be a nonempty string")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a nonempty string")
        if not commit and not self.connection.in_transaction:
            raise ValueError("commit=False requires a caller-owned transaction")
        with self.connection if commit else nullcontext():
            count = self.connection.execute(
                "UPDATE memory_records SET active=0,archived_at=? "
                "WHERE scope=? AND source_id=? AND active=1",
                (time.time(), scope, source_id)).rowcount
            if count:
                self._rebuild_static(scope)
        return count

    def _rebuild_static(self, scope: str) -> None:
        records = self._records(scope)
        n = len(records)
        frequencies: Counter[str] = Counter()
        co: Counter[tuple[str, str]] = Counter()
        contexts: dict[tuple[str, str], set[str]] = {}
        evidence: dict[tuple[str, str], list[int]] = {}
        for record in records:
            marks = set(record["marks"]) - self.self_marks
            frequencies.update(marks)
            for pair in itertools.combinations(sorted(marks), 2):
                co[pair] += 1
                contexts.setdefault(pair, set()).update(marks - set(pair))
                evidence.setdefault(pair, []).append(record["id"])
        self.connection.execute("DELETE FROM memory_static WHERE scope=?", (scope,))
        self.connection.execute("DELETE FROM memory_support WHERE scope=?", (scope,))
        for (a, b), count in sorted(co.items()):
            self.connection.execute(
                "INSERT INTO memory_support VALUES(?,?,?,?,?,?)",
                (scope, a, b, _json(sorted(contexts[(a, b)])),
                 _json(evidence[(a, b)]), count))
            p_ab = count / n
            npmi = (1.0 if p_ab == 1.0 else
                    math.log(p_ab / ((frequencies[a] / n) * (frequencies[b] / n)))
                    / -math.log(p_ab))
            if npmi > 0:
                self.connection.execute(
                    "INSERT INTO memory_static VALUES(?,?,?,?,?,?,?)",
                    (scope, a, b, round(npmi, 4), _json(sorted(contexts[(a, b)])),
                     _json(evidence[(a, b)]), count))

    def _observe(self, scope: str, hits: list[str], query: str,
                 now: float, event_id: str, *, audit: dict | None = None,
                 commit: bool = True) -> bool:
        """Seed, decay and reinforce once, capturing actual row transitions.

        ``commit=False`` allows retrieval selection and its immutable audit to
        commit in the same transaction as these transitions.
        """
        if not commit and not self.connection.in_transaction:
            raise ValueError("commit=False requires a caller-owned transaction")
        details = {"status": "applied", "applied": True, "original_changes_known": True,
                   "parameters": {"eta": ETA, "decay": DECAY, "dynamic_round_digits": 6,
                                  "static_round_digits": 4,
                                  "rounding": "Python round after decay and each reinforcement"},
                   "changed_edges": []}
        with self.connection if commit else nullcontext():
            state = self.connection.execute(
                "SELECT dynamic_seeded FROM memory_scopes WHERE scope=?", (scope,)).fetchone()
            details["seeded_before"] = bool(state[0]) if state else False
            old = self.connection.execute(
                "SELECT query,marks_json FROM memory_events WHERE scope=? AND event_id=?",
                (scope, event_id)).fetchone()
            if old is not None:
                if old[0] != query or old[1] != _json(hits):
                    raise ValueError("retrieval event ID reused with different input")
                details.update(status="replay", applied=False, original_changes_known=False,
                               seeded_after=details["seeded_before"], seeded_edges=0)
                if audit is not None:
                    audit.update(details)
                return False
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_scopes(scope) VALUES(?)", (scope,))
            seeded = self.connection.execute(
                "SELECT dynamic_seeded FROM memory_scopes WHERE scope=?", (scope,)).fetchone()[0]
            seeded_pairs = set()
            if not seeded:
                seeded_pairs = {tuple(row) for row in self.connection.execute(
                    "SELECT s.a,s.b FROM memory_static s LEFT JOIN memory_dynamic d "
                    "ON d.scope=s.scope AND d.a=s.a AND d.b=s.b "
                    "WHERE s.scope=? AND d.a IS NULL", (scope,))}
                self.connection.execute(
                    "INSERT OR IGNORE INTO memory_dynamic "
                    "(scope,a,b,weight,context_json,seed_weight) "
                    "SELECT scope,a,b,weight,context_json,weight FROM memory_static WHERE scope=?",
                    (scope,))
                self.connection.execute(
                    "UPDATE memory_scopes SET dynamic_seeded=1 WHERE scope=?", (scope,))
            details.update(seeded_after=True, seeded_edges=len(seeded_pairs))
            self.connection.execute("INSERT INTO memory_events VALUES(?,?,?,?,?)",
                                    (scope, event_id, now, query, _json(hits)))
            self.connection.execute("INSERT INTO memory_cycles VALUES(?,?,?,?,?)",
                                    (scope, event_id, event_id, now, DECAY))
            # Upstream rounds after decay and after each reinforcement.
            transitions = {}
            decay_updates = []
            for a, b, weight, seed_weight, last_event, context, evidence, co_count, static in list(self.connection.execute(
                    "SELECT d.a,d.b,d.weight,d.seed_weight,d.last_event_id,s.context_json,s.evidence_json,s.co_count,t.weight "
                    "FROM memory_dynamic d LEFT JOIN memory_support s ON s.scope=d.scope AND s.a=d.a AND s.b=d.b "
                    "LEFT JOIN memory_static t ON t.scope=d.scope AND t.a=d.a AND t.b=d.b "
                    "WHERE d.scope=? ORDER BY d.a,d.b", (scope,))):
                decayed = round(weight * DECAY, 6)
                decay_updates.append((decayed, event_id, scope, a, b))
                created = (a, b) in seeded_pairs
                transitions[(a, b)] = {"a": a, "b": b, "created_by": "static_seed" if created else None,
                    "before_weight": None if created else weight, "after_seed": weight if created else None,
                    "decay_applied": True, "after_decay": decayed, "reinforcement_added": 0.0,
                    "after_weight": decayed, "seed_weight": seed_weight,
                    "before_last_event_id": last_event, "after_last_event_id": event_id,
                    "active_source_support": evidence is not None,
                    "source_record_ids": json.loads(evidence) if evidence is not None else [],
                    "source_context": json.loads(context) if context is not None else [],
                    "co_count": co_count if co_count is not None else 0,
                    "static_score": static if static is not None else 0.0}
            # Keep Python rounding and the same ordered row updates, with
            # one call across the Python/SQLite boundary. The caller's
            # transaction still owns decay, reinforcement and audit together.
            self.connection.executemany(
                "UPDATE memory_dynamic SET weight=?,last_event_id=? "
                "WHERE scope=? AND a=? AND b=?", decay_updates)
            for a, b in itertools.combinations(sorted(hits), 2):
                inserted = self.connection.execute(
                    "INSERT OR IGNORE INTO memory_dynamic "
                    "(scope,a,b,weight,context_json,seed_weight) VALUES(?,?,?,0,'[]',0)",
                    (scope, a, b))
                weight = self.connection.execute(
                    "SELECT weight FROM memory_dynamic WHERE scope=? AND a=? AND b=?",
                    (scope, a, b)).fetchone()[0]
                reinforced = round(weight + ETA, 6)
                self.connection.execute(
                    "UPDATE memory_dynamic SET weight=?,last_event_id=? "
                    "WHERE scope=? AND a=? AND b=?",
                    (reinforced, event_id, scope, a, b))
                if inserted.rowcount:
                    support = self.connection.execute(
                        "SELECT context_json,evidence_json,co_count FROM memory_support WHERE scope=? AND a=? AND b=?",
                        (scope, a, b)).fetchone()
                    static = self.connection.execute(
                        "SELECT weight FROM memory_static WHERE scope=? AND a=? AND b=?", (scope, a, b)).fetchone()
                    transitions[(a, b)] = {"a": a, "b": b, "created_by": "direct_reinforcement",
                        "before_weight": None, "after_seed": None, "decay_applied": False,
                        "after_decay": None, "seed_weight": 0.0, "before_last_event_id": None,
                        "after_last_event_id": event_id, "active_source_support": support is not None,
                        "source_record_ids": json.loads(support[1]) if support else [],
                        "source_context": json.loads(support[0]) if support else [],
                        "co_count": support[2] if support else 0,
                        "static_score": static[0] if static else 0.0}
                transitions[(a, b)].update(reinforcement_before=weight, reinforcement_added=ETA,
                                             after_weight=reinforced)
            details["changed_edges"] = [dict(transition,
                weight_changed=transition["before_weight"] != transition["after_weight"])
                for _, transition in sorted(transitions.items())]
            if audit is not None:
                audit.update(details)
        return True

    def _edge_index(self, scope):
        index = self._retrieval_index
        if index is not None:
            if self._cache_shadows or self._cache_token(scope) != self._retrieval_token:
                # Records were read before observation in the old policy;
                # edges were read afterwards. Preserve that boundary if an
                # application trigger changes source edges during decay.
                index.index_edges(self._source_edges(scope))
                index.decoded_edges.clear()
            if index.edges is None:
                index.index_edges(self._source_edges(scope))
        return index

    def _query_edges(self, scope, ranking_mode, hits):
        # Dynamic mode and unindexed legacy rows retain their full historical
        # path. The product's static path owns only edges that can enter its
        # direct evidence or one-hop decisions (including rejected neighbors).
        if ranking_mode != "static" or self._retrieval_index is None:
            return self._edges(scope, ranking_mode)
        index = self._edge_index(scope)
        if not index.indexable_edges:
            return self._uncached_edges(scope, ranking_mode)
        if index.static_neighbors is None:
            index.index_static_neighbors()
        pairs = {pair for hit in hits for pair in index.static_neighbors.get(hit, ())}
        live_edges = {}
        # Preserve source insertion order for direct evidence and float sums.
        for pair in sorted(pairs, key=index.edge_ordinals.__getitem__):
            edge = dict(index.edges[pair], dynamic_score=0.0,
                        effective_score=index.edges[pair]["static_score"], dynamic_last_event_id=None)
            row = self.connection.execute(
                "SELECT weight,last_event_id FROM memory_dynamic WHERE scope=? AND a=? AND b=?",
                (scope, *pair)).fetchone()
            if row is not None:
                edge.update(dynamic_score=row[0], dynamic_last_event_id=row[1])
            if pair not in index.decoded_edges:
                index.decoded_edges[pair] = (json.loads(edge["context"]),
                                             json.loads(edge["source_record_ids"]))
            edge["context"], edge["source_record_ids"] = deepcopy(index.decoded_edges[pair])
            live_edges[pair] = edge
        return _QueryEdges(live_edges, index)

    def _edges(self, scope: str, ranking_mode: str) -> dict[tuple[str, str], dict[str, Any]]:
        index = self._edge_index(scope)
        if index is not None:
            if not index.indexable_edges:
                return self._uncached_edges(scope, ranking_mode)
            edges = {pair: dict(edge, dynamic_score=0.0,
                                effective_score=edge["static_score"], dynamic_last_event_id=None)
                     for pair, edge in index.edges.items()}
            for a, b, weight, event_id in self.connection.execute(
                    "SELECT a,b,weight,last_event_id FROM memory_dynamic WHERE scope=?", (scope,)):
                edge = edges.get((a, b))
                if edge is not None:
                    edge.update(dynamic_score=weight, dynamic_last_event_id=event_id)
                    if ranking_mode == "dynamic":
                        edge["effective_score"] = weight
            live_edges = {}
            for pair, edge in edges.items():
                if not edge["effective_score"] > 0:
                    continue
                if pair not in index.decoded_edges:
                    index.decoded_edges[pair] = (json.loads(edge["context"]),
                                                 json.loads(edge["source_record_ids"]))
                # Every query owns its output tree. Mutating a returned
                # audit or reference cannot poison the next cached query.
                edge["context"], edge["source_record_ids"] = deepcopy(index.decoded_edges[pair])
                live_edges[pair] = edge
            if ranking_mode == "static" and index.static_neighbors is None:
                index.index_static_neighbors()
            return _IndexedEdges(live_edges, index, ranking_mode)
        return self._uncached_edges(scope, ranking_mode)

    def _uncached_edges(self, scope: str, ranking_mode: str) -> dict[tuple[str, str], dict[str, Any]]:
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        for a, b, context, evidence, count in self.connection.execute(
                "SELECT a,b,context_json,evidence_json,co_count "
                "FROM memory_support WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": 0.0, "dynamic_score": 0.0,
                             "effective_score": 0.0, "context": context,
                             "source_record_ids": evidence, "co_count": count,
                             "dynamic_last_event_id": None}
        for a, b, weight, context, evidence, count in self.connection.execute(
                "SELECT a,b,weight,context_json,evidence_json,co_count "
                "FROM memory_static WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": weight, "dynamic_score": 0.0,
                             "effective_score": weight, "context": context,
                             "source_record_ids": evidence, "co_count": count,
                             "dynamic_last_event_id": None}
        for a, b, weight, event_id in self.connection.execute(
                "SELECT a,b,weight,last_event_id "
                "FROM memory_dynamic WHERE scope=?", (scope,)):
            if (a, b) not in edges:
                continue  # Preserved history is not valid live source support.
            edge = edges[(a, b)]
            edge.update(dynamic_score=weight, dynamic_last_event_id=event_id)
            if ranking_mode == "dynamic":
                edge["effective_score"] = weight
            # Prefer current static evidence when it exists. The seeded
            # dynamic context remains stored unchanged for audit.
        # Static rows supersede support metadata; decode the final live
        # evidence once, instead of decoding and replacing it twice. This
        # preserves support insertion order and every eligible audit edge.
        live_edges = {}
        for pair, edge in edges.items():
            if edge["effective_score"] > 0:
                edge["context"] = json.loads(edge["context"])
                edge["source_record_ids"] = json.loads(edge["source_record_ids"])
                live_edges[pair] = edge
        return live_edges

    def retrieve(self, scope: str, marks: list[str], query: str,
                 now: float, *, event_id: str | None = None,
                 audit: dict | None = None,
                 ranking_mode: str = "static") -> list[dict[str, Any]]:
        """Return at most three sourced references, with <=400 text chars.

        Direct hits require literal current-message evidence and known
        labels in this scope. Expanded hits never feed Hebbian updates.
        Self-only and weak/no-hit requests return no historical reference.
        ``audit`` receives committed observable transitions and selection. The
        first event audit is durable and immutable; replay selection remains
        based on current active sources without repeating graph learning.
        Static mode is the product default. Dynamic mode is retained as an
        explicit low-level option, without a Console/config activation path.
        """
        self._validate_scope_time(scope, now)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if audit is not None and not isinstance(audit, dict):
            raise ValueError("audit must be a dictionary")
        if ranking_mode not in ("static", "dynamic"):
            raise ValueError("ranking_mode must be static or dynamic")
        requested = self._marks(marks)
        if event_id is None:
            event_id = hashlib.sha256(_json([scope, query, requested, now]).encode()).hexdigest()
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a nonempty string")
        payload = {"schema_version": 1, "scope": scope, "event_id": event_id,
                   "request": {"query": query, "requested_marks": requested, "observed_at": now}}
        with self._retrieval_transaction(scope) as index:
            records = index.records
            known = records.known
            literal = sorted(records.literal_matches(query),
                             key=lambda m: (-len(m), m))
            hits = literal[:MAX_DIRECT]
            payload["match"] = {"known_marks_count": len(known), "literal_matches": literal,
                                "direct_hits": hits, "direct_limit": MAX_DIRECT,
                                "discarded_direct_matches": literal[MAX_DIRECT:],
                                "excluded_self_marks": sorted(self.self_marks)}
            stored = self.connection.execute(
                "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?", (scope, event_id)).fetchone()
            original = json.loads(stored[0]) if stored else None
            legacy = self.connection.execute(
                "SELECT query FROM memory_events WHERE scope=? AND event_id=?", (scope, event_id)).fetchone()
            if (original and original["request"]["query"] != query) or (legacy and legacy[0] != query):
                raise ValueError("retrieval event ID reused with different input")
            # A replay may select current active source versions, but cannot
            # relabel an old dynamic experiment as a static retrieval event.
            original_mode = original["selection"].get("ranking_mode", "dynamic") if original else "dynamic"
            if (original or legacy) and original_mode != ranking_mode:
                raise ValueError("retrieval event ID reused with different ranking mode")
            observation = {}
            if hits:
                if original and not original["match"]["direct_hits"]:
                    raise ValueError("retrieval event ID reused with different input")
                self._observe(scope, hits, query, now, event_id, audit=observation, commit=False)
                result, selection = self._selection(records, hits, self._query_edges(scope, ranking_mode, hits), query, event_id)
            else:
                state = self.connection.execute(
                    "SELECT dynamic_seeded FROM memory_scopes WHERE scope=?", (scope,)).fetchone()
                seeded = bool(state[0]) if state else False
                observation = {"status": "no_hit", "applied": False, "original_changes_known": True,
                               "seeded_before": seeded, "seeded_after": seeded, "seeded_edges": 0,
                               "changed_edges": [], "reason": "no_literal_known_mark"}
                result, selection = self._selection(records, [], {}, query, event_id)
            selection.update(ranking_mode=ranking_mode,
                             weight_basis="static_npmi" if ranking_mode == "static" else "dynamic_or_static")
            for record in result:
                record.update(ranking_mode=selection["ranking_mode"], weight_basis=selection["weight_basis"])
            if original:
                observation["original_changes_known"] = original["observation"]["original_changes_known"]
                payload["original_event"] = {"payload_sha256": hashlib.sha256(stored[0].encode()).hexdigest(),
                    "observed_at": original["request"]["observed_at"],
                    "ranking_mode": original_mode,
                    "observation_status": original["observation"]["status"],
                    "changed_edges_count": len(original["observation"]["changed_edges"])}
            elif legacy:
                observation.update(status="legacy_replay", original_changes_known=False)
            payload.update(observation=observation, selection=selection, replay=bool(original or legacy))
            if not stored:
                serialized = _json(payload)
                self.connection.execute("INSERT INTO memory_event_audits VALUES(?,?,?)", (scope, event_id, serialized))
            else:
                serialized = stored[0]
        payload["durable_payload_sha256"] = hashlib.sha256(serialized.encode()).hexdigest()
        if audit is not None:
            audit.clear()
            audit.update(payload)
        return result

    def _selection(self, records, hits, edges, query, event_id):
        """The existing one-hop/ranking policy, with observable decision data."""
        context_matches = {}

        def context_hit(mark):
            # One query owns this cache. Reusing the exact literal matcher
            # preserves Unicode/token boundaries while avoiding repeated
            # regex compilation for shared source-context labels.
            if mark not in context_matches:
                context_matches[mark] = _hit(mark, query)
            return context_matches[mark]

        indexed_records = isinstance(records, _IndexedRecords)
        if indexed_records and records.self_marks != frozenset(self.self_marks):
            records = _IndexedRecords(records, self.self_marks)
        indexed_edges = isinstance(edges, _IndexedEdges)
        if indexed_records:
            frequencies = records.frequencies
            known = records.known
            mark_frequencies = dict(frequencies)
        else:
            frequencies = Counter()
            for record in records:
                frequencies.update(set(record["marks"]) - self.self_marks)
            known = set(frequencies)
            mark_frequencies = dict(sorted(frequencies.items()))
        statistic_pairs = (edges.ordered_pairs if indexed_edges else sorted(edges))
        selection = {"active_record_count": len(records),
            "live_edge_count": edges.live_edge_count if isinstance(edges, _QueryEdges) else len(edges),
            "mark_frequencies": mark_frequencies, "static_formula_reproducible": True,
            "static_policy": {"formula": "log(p_ab/(p_a*p_b))/-log(p_ab)",
                              "p_ab_one_value": 1.0, "retain_only_positive": True, "round_digits": 4},
            "edge_statistics": [{"a": a, "b": b, "co_count": edge["co_count"],
                                 "active_source_support": True, "source_record_ids": edge["source_record_ids"],
                                 "context": edge["context"], "dynamic_last_event_id": edge["dynamic_last_event_id"],
                                 "static_score": edge["static_score"], "dynamic_score": edge["dynamic_score"],
                                 "effective_score": edge["effective_score"]}
                                for a, b in statistic_pairs if (a, b) in edges
                                for edge in (edges[(a, b)],)],
            "limits": {"direct_marks": MAX_DIRECT, "neighbors_per_hit": 5,
                       "expanded_marks": MAX_EXTRA, "references": MAX_REFERENCES,
                       "total_text_characters": MAX_TEXT_CHARS, "entry_text_characters": MAX_ENTRY_CHARS},
            "ranking_order": ["direct_match_count descending", "effective_score descending",
                              "static_score descending", "record_id descending"],
            "expansion_candidates": [], "ranked_candidates": [], "selected": []}
        if isinstance(edges, _QueryEdges):
            selection["edge_statistics_scope"] = edges.statistics_scope

        # Rank first, THEN deduplicate, so the strongest provenance wins.
        expansion: dict[str, dict[str, Any]] = {}
        for hit in hits:
            neighbors = []
            if indexed_edges:
                pairs = (edges.static_neighbors.get(hit, ()) if edges.static_neighbors is not None
                         else edges.incident.get(hit, ()))
                incident = ((pair, edges[pair]) for pair in pairs if pair in edges)
            else:
                incident = edges.items()
            for (a, b), edge in incident:
                if hit not in (a, b):
                    continue
                neighbor = b if a == hit else a
                if neighbor in hits or neighbor in self.self_marks or neighbor not in known:
                    continue
                neighbors.append((neighbor, edge))
            if not indexed_edges or edges.static_neighbors is None:
                neighbors.sort(key=lambda item: (-item[1]["effective_score"], item[0]))
            for neighbor_rank, (neighbor, edge) in enumerate(neighbors, 1):
                context_passed = not edge["context"] or any(context_hit(m) for m in edge["context"])
                decision = dict(edge, from_mark=hit, mark=neighbor, neighbor_rank=neighbor_rank,
                    within_neighbor_limit=neighbor_rank <= 5, context_passed=context_passed,
                    considered=neighbor_rank <= 5 and context_passed)
                selection["expansion_candidates"].append(decision)
                if neighbor_rank > 5 or not context_passed:
                    continue
                entry = dict(edge, from_mark=hit, mark=neighbor)
                old = expansion.get(neighbor)
                if old is None or (entry["effective_score"], entry["from_mark"]) > (
                        old["effective_score"], old["from_mark"]):
                    expansion[neighbor] = entry
        expanded = sorted(expansion.values(),
                          key=lambda item: (-item["effective_score"], item["mark"]))[:MAX_EXTRA]
        allowed = set(hits) | {entry["mark"] for entry in expanded}
        selection["expanded_marks"] = [entry["mark"] for entry in expanded]
        for candidate in selection["expansion_candidates"]:
            winner = expansion.get(candidate["mark"])
            candidate["deduplication_winner"] = bool(winner and winner["from_mark"] == candidate["from_mark"])
            candidate["selected_for_expansion"] = candidate["deduplication_winner"] and candidate["mark"] in selection["expanded_marks"]

        # Only pairs of direct hits can supply direct ranking evidence. Keep
        # their original edge order so floating-point sums and the complete
        # evidence receipt remain identical, without scanning every live edge
        # again for each candidate record (at most six direct pairs).
        hit_set = set(hits)
        if indexed_edges:
            # Normal sourced edges have six possible direct pairs. Include
            # reversed/self pairs too, preserving any legacy SQLite rows.
            pairs = {(a, b) for a in hit_set for b in hit_set if (a, b) in edges}
            direct_items = ((pair, edges[pair]) for pair in sorted(pairs, key=edges.ordinals.__getitem__))
        else:
            direct_items = edges.items()
        direct_edges = [(a, b, edge) for (a, b), edge in direct_items
                        if a in hit_set and b in hit_set
                        and (not edge["context"] or any(context_hit(m) for m in edge["context"]))]
        ranked = []
        if indexed_records:
            ordinals = {ordinal for mark in allowed for ordinal in records.postings.get(mark, ())}
            candidates = (records[ordinal] for ordinal in sorted(ordinals))
        else:
            candidates = records
        for record in candidates:
            present = set(record["marks"]) & allowed
            if not present:
                continue
            direct = sorted(present & set(hits))
            evidence = []
            for a, b, edge in direct_edges:
                if a in direct and b in direct:
                    evidence.append(dict(edge, from_mark=a, mark=b))
            # Exactly the strongest permitted edge explains each extra
            # label. Other incident edges must not double-count it.
            for entry in expanded:
                if entry["mark"] in present:
                    evidence.append(entry)
            static = sum(item["static_score"] for item in evidence)
            dynamic = sum(item["dynamic_score"] for item in evidence)
            effective = sum(item["effective_score"] for item in evidence)
            result = dict(record, marks=deepcopy(record["marks"]), direct_marks=direct,
                          expanded_marks=sorted(present - set(hits)),
                          static_score=round(static, 6), dynamic_score=round(dynamic, 6),
                          ranking_score=round(effective, 6), evidence=evidence,
                          retrieval_event_id=event_id)
            ranked.append((len(direct), effective, static, record["id"], result))
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3]))
        for rank, (direct_count, effective, static, record_id, record) in enumerate(ranked, 1):
            selection["ranked_candidates"].append({"rank": rank, "record_id": record_id,
                "source_id": record["source_id"], "marks": record["marks"],
                "direct_marks": record["direct_marks"], "expanded_marks": record["expanded_marks"],
                "direct_match_count": direct_count, "effective_score": effective, "static_score": static,
                "dynamic_score": record["dynamic_score"], "evidence": record["evidence"],
                "text_characters": len(record["text"]),
                "text_sha256": hashlib.sha256(record["text"].encode()).hexdigest(),
                "quote_sha256": hashlib.sha256(record["quote"].encode()).hexdigest()})
        result, used = [], 0
        for _, _, _, _, record in ranked:
            if len(result) == MAX_REFERENCES:
                break
            available = min(MAX_ENTRY_CHARS, MAX_TEXT_CHARS - used)
            if available <= 0:
                break
            text = record["text"]
            record["text"] = text if len(text) <= available else text[:available - 1] + "…"
            record["text_truncated"] = len(record["text"]) < len(text)
            used += len(record["text"])
            result.append(record)
            selection["selected"].append({"rank": len(result), "record_id": record["id"],
                "source_id": record["source_id"], "text_characters": len(record["text"]),
                "text_truncated": record["text_truncated"],
                "preview_sha256": hashlib.sha256(record["text"].encode()).hexdigest()})
        selection["selected_record_ids"] = [r["id"] for r in result]
        selection["excluded_record_count"] = len(records) - len(ranked)
        selection["used_text_characters"] = used
        return result, selection
