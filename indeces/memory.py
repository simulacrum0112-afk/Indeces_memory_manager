"""Auditable, scope-isolated mark memory adapted from hebbian-mark-graph.

Upstream: Mingyuliu-botaaa/hebbian-mark-graph, MIT,
commit 8769b9ed0af3531b9fbc09fb6e084d8e7adf724d (2026).
NPMI = log(p_ab / (p_a * p_b)) / -log(p_ab), with p_ab == 1 -> 1.
Only positive static edges survive. Dynamic weights are seeded once from
static edges, remain separate, and use w <- 0.99*w + 1 per co-activated
pair. A retrieval event is one explicit decay cycle; retries of the same
event do neither. Historical records and annotation alone never reinforce.

Approved live extension: a dynamic weight, where present, replaces the
static weight for one-hop expansion and ranking. Direct matches have
priority; equally direct records are ranked by their incident live weights.
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
from contextlib import nullcontext
from typing import Any


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


class MemoryGraph:
    """One caller-owned SQLite connection; methods commit atomic updates.

    Pass the Discord message ID as ``event_id`` when retrieving. Otherwise
    identical scope/query/marks/now values identify the same event. A new
    timestamp deliberately identifies a new real retrieval.
    """

    def __init__(self, connection: sqlite3.Connection,
                 self_marks: tuple[str, ...] = ("Indices",)) -> None:
        self.connection = connection
        self.self_marks = {_canonical(mark) for mark in self_marks}
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
                 now: float, event_id: str) -> bool:
        """Atomically seed once, decay once, and reinforce direct pairs once."""
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_scopes(scope) VALUES(?)", (scope,))
            seeded = self.connection.execute(
                "SELECT dynamic_seeded FROM memory_scopes WHERE scope=?", (scope,)).fetchone()[0]
            if not seeded:
                self.connection.execute(
                    "INSERT OR IGNORE INTO memory_dynamic "
                    "(scope,a,b,weight,context_json,seed_weight) "
                    "SELECT scope,a,b,weight,context_json,weight FROM memory_static WHERE scope=?",
                    (scope,))
                self.connection.execute(
                    "UPDATE memory_scopes SET dynamic_seeded=1 WHERE scope=?", (scope,))
            old = self.connection.execute(
                "SELECT query,marks_json FROM memory_events WHERE scope=? AND event_id=?",
                (scope, event_id)).fetchone()
            if old is not None:
                if old[0] != query or old[1] != _json(hits):
                    raise ValueError("retrieval event ID reused with different input")
                return False
            self.connection.execute("INSERT INTO memory_events VALUES(?,?,?,?,?)",
                                    (scope, event_id, now, query, _json(hits)))
            self.connection.execute("INSERT INTO memory_cycles VALUES(?,?,?,?,?)",
                                    (scope, event_id, event_id, now, DECAY))
            # Upstream rounds after decay and after each reinforcement.
            for a, b, weight in list(self.connection.execute(
                    "SELECT a,b,weight FROM memory_dynamic WHERE scope=?", (scope,))):
                self.connection.execute(
                    "UPDATE memory_dynamic SET weight=?,last_event_id=? "
                    "WHERE scope=? AND a=? AND b=?",
                    (round(weight * DECAY, 6), event_id, scope, a, b))
            for a, b in itertools.combinations(sorted(hits), 2):
                self.connection.execute(
                    "INSERT OR IGNORE INTO memory_dynamic "
                    "(scope,a,b,weight,context_json,seed_weight) VALUES(?,?,?,0,'[]',0)",
                    (scope, a, b))
                weight = self.connection.execute(
                    "SELECT weight FROM memory_dynamic WHERE scope=? AND a=? AND b=?",
                    (scope, a, b)).fetchone()[0]
                self.connection.execute(
                    "UPDATE memory_dynamic SET weight=?,last_event_id=? "
                    "WHERE scope=? AND a=? AND b=?",
                    (round(weight + ETA, 6), event_id, scope, a, b))
        return True

    def _edges(self, scope: str) -> dict[tuple[str, str], dict[str, Any]]:
        edges: dict[tuple[str, str], dict[str, Any]] = {}
        for a, b, context, evidence, count in self.connection.execute(
                "SELECT a,b,context_json,evidence_json,co_count "
                "FROM memory_support WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": 0.0, "dynamic_score": 0.0,
                             "effective_score": 0.0, "context": json.loads(context),
                             "source_record_ids": json.loads(evidence), "co_count": count,
                             "dynamic_last_event_id": None}
        for a, b, weight, context, evidence, count in self.connection.execute(
                "SELECT a,b,weight,context_json,evidence_json,co_count "
                "FROM memory_static WHERE scope=?", (scope,)):
            edges[(a, b)] = {"static_score": weight, "dynamic_score": 0.0,
                             "effective_score": weight, "context": json.loads(context),
                             "source_record_ids": json.loads(evidence), "co_count": count,
                             "dynamic_last_event_id": None}
        for a, b, weight, context, event_id in self.connection.execute(
                "SELECT a,b,weight,context_json,last_event_id "
                "FROM memory_dynamic WHERE scope=?", (scope,)):
            if (a, b) not in edges:
                continue  # Preserved history is not valid live source support.
            edge = edges[(a, b)]
            edge.update(dynamic_score=weight, effective_score=weight,
                        dynamic_last_event_id=event_id)
            # Prefer current static evidence when it exists. The seeded
            # dynamic context remains stored unchanged for audit.
        return {pair: edge for pair, edge in edges.items() if edge["effective_score"] > 0}

    def retrieve(self, scope: str, marks: list[str], query: str,
                 now: float, *, event_id: str | None = None) -> list[dict[str, Any]]:
        """Return at most three sourced references, with <=400 text chars.

        Direct hits require literal current-message evidence and known
        labels in this scope. Expanded hits never feed Hebbian updates.
        Self-only and weak/no-hit requests return no historical reference.
        """
        self._validate_scope_time(scope, now)
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        requested = self._marks(marks)
        records = self._records(scope)
        known = {m for record in records for m in record["marks"]} - self.self_marks
        candidates = known | set(requested)
        hits = sorted((m for m in candidates if m in known and _hit(m, query)),
                      key=lambda m: (-len(m), m))[:MAX_DIRECT]
        if not hits:
            return []
        if event_id is None:
            event_id = hashlib.sha256(_json([scope, query, requested, now]).encode()).hexdigest()
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a nonempty string")
        self._observe(scope, hits, query, now, event_id)
        edges = self._edges(scope)

        # Rank first, THEN deduplicate, so the strongest provenance wins.
        expansion: dict[str, dict[str, Any]] = {}
        for hit in hits:
            neighbors = []
            for (a, b), edge in edges.items():
                if hit not in (a, b):
                    continue
                neighbor = b if a == hit else a
                if neighbor in hits or neighbor in self.self_marks or neighbor not in known:
                    continue
                neighbors.append((neighbor, edge))
            neighbors.sort(key=lambda item: (-item[1]["effective_score"], item[0]))
            for neighbor, edge in neighbors[:5]:
                if edge["context"] and not any(_hit(m, query) for m in edge["context"]):
                    continue
                entry = dict(edge, from_mark=hit, mark=neighbor)
                old = expansion.get(neighbor)
                if old is None or (entry["effective_score"], entry["from_mark"]) > (
                        old["effective_score"], old["from_mark"]):
                    expansion[neighbor] = entry
        expanded = sorted(expansion.values(),
                          key=lambda item: (-item["effective_score"], item["mark"]))[:MAX_EXTRA]
        allowed = set(hits) | {entry["mark"] for entry in expanded}

        ranked = []
        for record in records:
            present = set(record["marks"]) & allowed
            if not present:
                continue
            direct = sorted(present & set(hits))
            evidence = []
            for (a, b), edge in edges.items():
                if not ({a, b} <= set(direct)):
                    continue
                if edge["context"] and not any(_hit(m, query) for m in edge["context"]):
                    continue
                evidence.append(dict(edge, from_mark=a, mark=b))
            # Exactly the strongest permitted edge explains each extra
            # label. Other incident edges must not double-count it.
            for entry in expanded:
                if entry["mark"] in present:
                    evidence.append(entry)
            static = sum(item["static_score"] for item in evidence)
            dynamic = sum(item["dynamic_score"] for item in evidence)
            effective = sum(item["effective_score"] for item in evidence)
            result = dict(record, direct_marks=direct,
                          expanded_marks=sorted(present - set(hits)),
                          static_score=round(static, 6), dynamic_score=round(dynamic, 6),
                          ranking_score=round(effective, 6), evidence=evidence,
                          retrieval_event_id=event_id)
            ranked.append((len(direct), effective, static, record["id"], result))
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3]))
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
        return result
