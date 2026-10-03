"""Exact legacy-result and bounded-edge-work checks using synthetic data only."""

from collections import Counter
from copy import deepcopy
from contextlib import nullcontext
import hashlib
import itertools
import json
import random
import sqlite3
import unittest
from unittest.mock import patch

from indeces.memory import (DECAY, ETA, MAX_DIRECT, MAX_ENTRY_CHARS, MAX_EXTRA, MAX_REFERENCES,
                           MAX_TEXT_CHARS, MemoryGraph, _hit, _json)
from indeces.run_records import validate_graph_audit
from indeces.scratch import canonical


_QUERY_EDGE_SCOPE = "direct_hit_incident_v1"


def _scoped_legacy_payload(payload):
    """Independently project the frozen oracle's full edge statistics."""
    scoped = deepcopy(payload)
    hits = set(scoped["match"]["direct_hits"])
    scoped["selection"]["edge_statistics"] = [
        edge for edge in scoped["selection"]["edge_statistics"]
        if edge["a"] in hits or edge["b"] in hits]
    scoped["selection"]["edge_statistics_scope"] = _QUERY_EDGE_SCOPE
    return scoped


def assert_retrieval_audits_compatible(test, original, current, original_db, current_db,
                                     *, validate=True):
    """Allow only the declared statistics projection and its durable links.

    Selection, observation, and all relevant edge bytes come from the frozen
    independent oracle. Both real durable documents are validated separately;
    the expected digest is rebuilt from the projected original, never copied
    from the implementation being tested.
    """
    scope, event_id = original["scope"], original["event_id"]
    original_serialized = original_db.execute(
        "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
        (scope, event_id)).fetchone()[0]
    current_serialized = current_db.execute(
        "SELECT payload_json FROM memory_event_audits WHERE scope=? AND event_id=?",
        (scope, event_id)).fetchone()[0]
    original_stored, current_stored = json.loads(original_serialized), json.loads(current_serialized)
    expected_stored = original_stored
    if current_stored["selection"].get("edge_statistics_scope") == _QUERY_EDGE_SCOPE:
        expected_stored = _scoped_legacy_payload(original_stored)
    test.assertEqual(current_serialized.encode(), canonical(expected_stored))
    original_digest = hashlib.sha256(original_serialized.encode()).hexdigest()
    current_digest = hashlib.sha256(current_serialized.encode()).hexdigest()
    expected_digest = hashlib.sha256(canonical(expected_stored)).hexdigest()
    test.assertEqual(original["durable_payload_sha256"], original_digest)
    test.assertEqual(current["durable_payload_sha256"], current_digest)

    expected = original
    if current["selection"].get("edge_statistics_scope") == _QUERY_EDGE_SCOPE:
        expected = _scoped_legacy_payload(original)
    else:
        expected = deepcopy(original)
    expected["durable_payload_sha256"] = expected_digest
    if "original_event" in expected:
        expected["original_event"]["payload_sha256"] = expected_digest
    test.assertEqual(canonical(current), canonical(expected))
    if validate:
        for payload in (original, current,
                        dict(original_stored, durable_payload_sha256=original_digest),
                        dict(current_stored, durable_payload_sha256=current_digest)):
            validate_graph_audit(payload)


def assert_database_compatible(test, original_db, current_db):
    """All persisted rows stay exact except declared event-audit scoping."""
    original = MemorySelectionPerformanceTests.database_state(original_db)
    current = MemorySelectionPerformanceTests.database_state(current_db)
    test.assertEqual(set(original), set(current))
    for table, original_rows in original.items():
        if table != "memory_event_audits":
            test.assertEqual(current[table], original_rows, table)
            continue
        test.assertEqual(len(current[table]), len(original_rows))
        for old, new in zip(original_rows, current[table]):
            test.assertEqual(old[:2], new[:2])
            expected = json.loads(old[2])
            if json.loads(new[2])["selection"].get("edge_statistics_scope") == _QUERY_EDGE_SCOPE:
                expected = _scoped_legacy_payload(expected)
            test.assertEqual(new[2].encode(), canonical(expected))


# Frozen selection policy from 0.12.0 source commit 7405790d71b114b887c91ac96c29a40686e3a708.
# Its candidate-by-all-edges scan is intentionally retained as the compatibility
# oracle; the optimized implementation must preserve its entire observable result.
def _legacy_selection(self, records, hits, edges, query, event_id):
    """The existing one-hop/ranking policy, with observable decision data."""
    frequencies = Counter()
    for record in records:
        frequencies.update(set(record["marks"]) - self.self_marks)
    known = set(frequencies)
    selection = {"active_record_count": len(records), "live_edge_count": len(edges),
        "mark_frequencies": dict(sorted(frequencies.items())), "static_formula_reproducible": True,
        "static_policy": {"formula": "log(p_ab/(p_a*p_b))/-log(p_ab)",
                          "p_ab_one_value": 1.0, "retain_only_positive": True, "round_digits": 4},
        "edge_statistics": [{"a": a, "b": b, "co_count": edge["co_count"],
                             "active_source_support": True, "source_record_ids": edge["source_record_ids"],
                             "context": edge["context"], "dynamic_last_event_id": edge["dynamic_last_event_id"],
                             "static_score": edge["static_score"], "dynamic_score": edge["dynamic_score"],
                             "effective_score": edge["effective_score"]}
                            for (a, b), edge in sorted(edges.items())],
        "limits": {"direct_marks": MAX_DIRECT, "neighbors_per_hit": 5,
                   "expanded_marks": MAX_EXTRA, "references": MAX_REFERENCES,
                   "total_text_characters": MAX_TEXT_CHARS, "entry_text_characters": MAX_ENTRY_CHARS},
        "ranking_order": ["direct_match_count descending", "effective_score descending",
                          "static_score descending", "record_id descending"],
        "expansion_candidates": [], "ranked_candidates": [], "selected": []}

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
        for neighbor_rank, (neighbor, edge) in enumerate(neighbors, 1):
            context_passed = not edge["context"] or any(_hit(m, query) for m in edge["context"])
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


def _legacy_observe(self, scope: str, hits: list[str], query: str,
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
        for a, b, weight, seed_weight, last_event, context, evidence, co_count, static in list(self.connection.execute(
                "SELECT d.a,d.b,d.weight,d.seed_weight,d.last_event_id,s.context_json,s.evidence_json,s.co_count,t.weight "
                "FROM memory_dynamic d LEFT JOIN memory_support s ON s.scope=d.scope AND s.a=d.a AND s.b=d.b "
                "LEFT JOIN memory_static t ON t.scope=d.scope AND t.a=d.a AND t.b=d.b "
                "WHERE d.scope=? ORDER BY d.a,d.b", (scope,))):
            decayed = round(weight * DECAY, 6)
            self.connection.execute(
                "UPDATE memory_dynamic SET weight=?,last_event_id=? "
                "WHERE scope=? AND a=? AND b=?",
                (decayed, event_id, scope, a, b))
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


def _legacy_edges(self, scope, ranking_mode):
    # The original decoder is also frozen so full retrieval comparisons cover
    # support/static overlap, current evidence precedence, and source gating.
    edges = {}
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
            continue
        edge = edges[(a, b)]
        edge.update(dynamic_score=weight, dynamic_last_event_id=event_id)
        if ranking_mode == "dynamic":
            edge["effective_score"] = weight
    return {pair: edge for pair, edge in edges.items() if edge["effective_score"] > 0}


class _LegacyMemoryGraph(MemoryGraph):
    _observe = _legacy_observe
    _selection = _legacy_selection
    _edges = _legacy_edges

    def _query_edges(self, scope, ranking_mode, hits):
        # Freeze the full-edge routing too: the oracle must not use the new
        # query-scoped materialization under comparison.
        return self._edges(scope, ranking_mode)


class _CountedEdges(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.visited_pairs = 0

    def items(self):
        for item in super().items():
            self.visited_pairs += 1
            yield item


class _CountedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.decay_execute_calls = 0
        self.decay_executemany_calls = 0

    def execute(self, sql, parameters=()):
        if sql.startswith("UPDATE memory_dynamic SET weight="):
            self.decay_execute_calls += 1
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        if sql.startswith("UPDATE memory_dynamic SET weight="):
            self.decay_executemany_calls += 1
        return self.connection.executemany(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        return self.connection.__enter__()

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


class MemorySelectionPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.graph = MemoryGraph(self.db)
        self.addCleanup(self.db.close)

    @staticmethod
    def record(index, marks):
        return {"id": index, "scope": "synthetic", "source_id": f"source-{index}",
                "author_id": "synthetic-author", "text": "synthetic " * (index + 15),
                "quote": "synthetic quotation", "marks": marks, "created_at": 1.0,
                "active": True, "archived_at": None}

    @staticmethod
    def edge(static=.4, dynamic=.396, *, context=(), effective=None):
        return {"static_score": static, "dynamic_score": dynamic,
                "effective_score": static if effective is None else effective,
                "context": list(context), "source_record_ids": [1], "co_count": 1,
                "dynamic_last_event_id": "synthetic-first"}

    def assert_selection_equal(self, records, hits, edges, query):
        original = _legacy_selection(self.graph, deepcopy(records), hits, edges, query, "synthetic-event")
        current = self.graph._selection(deepcopy(records), hits, edges, query, "synthetic-event")
        self.assertEqual(current, original)
        return current

    def test_full_result_equivalence_for_seeded_synthetic_graphs(self):
        rng = random.Random(91207)
        labels = ["alpha", "beta", "gamma", "d_4", "RuO2", "IrO2", "中文", "气氛", "m-4", "3.1415"]
        for case in range(60):
            with self.subTest(case=case):
                hits = rng.sample(labels, rng.randrange(MAX_DIRECT + 1))
                query = " ".join(hits + rng.sample(labels, 3))
                records = [self.record(i + 1, rng.sample(labels, rng.randrange(1, 9)))
                           for i in range(rng.randrange(1, 41))]
                pairs = list(itertools.combinations(sorted(labels), 2))
                rng.shuffle(pairs)
                edges = {}
                for a, b in pairs[:rng.randrange(len(pairs) + 1)]:
                    static = rng.choice([.1, .3333, .9987])
                    dynamic = rng.choice([.99, 3.9876, 4.9])
                    edges[(a, b)] = self.edge(static, dynamic,
                        context=rng.sample(labels, rng.randrange(4)),
                        effective=rng.choice([static, dynamic]))
                self.assert_selection_equal(records, hits, edges, query)

    def test_original_edge_order_and_float_sums_are_preserved(self):
        # Evidence must retain insertion order and the exact baseline sums,
        # including the interpreter's floating-point summation behavior.
        edges = {
            ("gamma", "theta"): self.edge(1.0, 1e16, effective=1e16),
            ("alpha", "beta"): self.edge(.5, 1.0, effective=1.0),
            ("alpha", "gamma"): self.edge(.2, 1.0, effective=1.0),
        }
        records = [self.record(1, ["alpha", "beta", "gamma", "theta"])]
        result, selection = self.assert_selection_equal(records,
            ["alpha", "beta", "gamma", "theta"], edges, "alpha beta gamma theta")
        self.assertEqual([(e["from_mark"], e["mark"]) for e in result[0]["evidence"]], list(edges))
        self.assertEqual(selection["ranked_candidates"][0]["effective_score"],
                         sum(e["effective_score"] for e in edges.values()))

    def test_context_gate_expansion_and_reference_caps_stay_identical(self):
        records = [self.record(i, ["alpha", "beta", "extra"]) for i in range(1, 8)]
        edges = {
            ("alpha", "beta"): self.edge(context=["gate"]),
            ("alpha", "extra"): self.edge(.8, .792),
            ("beta", "extra"): self.edge(.7, .693),
        }
        for query in ("alpha beta", "alpha beta gate"):
            with self.subTest(query=query):
                result, selection = self.assert_selection_equal(records, ["alpha", "beta"], edges, query)
                self.assertEqual(len(result), MAX_REFERENCES)
                self.assertEqual(len(selection["ranked_candidates"]), len(records))
                self.assertEqual(len(selection["edge_statistics"]), len(edges))
                self.assertEqual(selection["expanded_marks"], ["extra"])
                self.assertTrue(any(r["text_truncated"] for r in result))
                for record in result:
                    direct_evidence = [(e["from_mark"], e["mark"]) for e in record["evidence"]
                                       if e["mark"] != "extra"]
                    self.assertEqual(direct_evidence, [("alpha", "beta")] if "gate" in query else [])

    def test_edge_work_does_not_scale_with_candidate_count(self):
        base_edges = {(f"unrelated-{i}", f"unrelated-{i+1}"): self.edge()
                      for i in range(250)}
        base_edges[("alpha", "beta")] = self.edge()
        visits = []
        for count in (1, 200):
            edges = _CountedEdges(base_edges)
            result, selection = self.graph._selection(
                [self.record(i + 1, ["alpha", "beta"]) for i in range(count)],
                ["alpha", "beta"], edges, "alpha beta", "synthetic-event")
            visits.append(edges.visited_pairs)
            self.assertEqual(len(selection["edge_statistics"]), len(edges))
            self.assertEqual(len(selection["ranked_candidates"]), count)
            self.assertEqual(len(result), min(count, MAX_REFERENCES))
        self.assertEqual(visits[0], visits[1])
        # Full statistics, one scan per direct hit, then one evidence scan.
        self.assertLessEqual(visits[1], (MAX_DIRECT + 2) * len(base_edges))

    def test_final_live_edge_metadata_is_decoded_once(self):
        for source, marks in (("pair", ["alpha", "beta"]), ("alpha", ["alpha"]),
                              ("beta", ["beta"]), ("positive", ["gamma", "delta"])):
            self.graph.add("synthetic", source, "author", [
                {"text": source, "quote": source, "marks": marks}], 1.0)
        with patch("indeces.memory.json.loads", wraps=json.loads) as decode:
            original = _legacy_edges(self.graph, "synthetic", "static")
            original_decode_count = decode.call_count
        with patch("indeces.memory.json.loads", wraps=json.loads) as decode:
            current = self.graph._edges("synthetic", "static")
            current_decode_count = decode.call_count
        self.assertEqual(current, original)
        self.assertEqual(list(current), list(original))
        self.assertEqual(set(current), {("delta", "gamma")})
        self.assertEqual(current_decode_count, 2 * len(current))
        self.assertLess(current_decode_count, original_decode_count)

    def test_decay_updates_cross_sql_boundary_once(self):
        marks = ["alpha", "beta", "gamma", "theta"]
        self.graph.add("synthetic", "cluster", "author", [
            {"text": "cluster", "quote": "cluster", "marks": marks}], 1.0)
        self.graph.add("synthetic", "other", "author", [
            {"text": "other", "quote": "other", "marks": ["unrelated"]}], 1.0)
        counted = _CountedConnection(self.db)
        self.graph.connection = counted
        audit = {}
        self.graph.retrieve("synthetic", [], "alpha", 2.0, event_id="single-hit", audit=audit)
        self.assertEqual(len(audit["observation"]["changed_edges"]), 6)
        self.assertEqual(counted.decay_execute_calls, 0)
        self.assertEqual(counted.decay_executemany_calls, 1)
        validate_graph_audit(audit)

    @staticmethod
    def database_state(db):
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_%' ORDER BY name")]
        return {table: list(db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}

    def assert_database_equal(self, original_db, current_db):
        assert_database_compatible(self, original_db, current_db)

    def test_partial_decay_and_audit_insert_failure_roll_back_exactly(self):
        for failure in ("decay", "audit"):
            with self.subTest(failure=failure):
                original_db, current_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
                self.addCleanup(original_db.close)
                self.addCleanup(current_db.close)
                original, current = _LegacyMemoryGraph(original_db), MemoryGraph(current_db)
                for graph in (original, current):
                    graph.add("synthetic", "cluster", "author", [{"text": "cluster", "quote": "cluster",
                              "marks": ["alpha", "beta", "gamma", "theta"]}], 1.0)
                    graph.add("synthetic", "other", "author", [{"text": "other", "quote": "other",
                              "marks": ["unrelated"]}], 1.0)
                    graph.retrieve("synthetic", [], "alpha beta gamma theta", 2.0, event_id="seed")
                self.assert_database_equal(original_db, current_db)
                for graph, db in ((original, original_db), (current, current_db)):
                    before = self.database_state(db)
                    if failure == "decay":
                        # The last sorted pair fails after prior updates have
                        # executed, exercising rollback of a partial batch.
                        db.executescript("""
                            CREATE TRIGGER reject_decay BEFORE UPDATE ON memory_dynamic
                            WHEN NEW.a='gamma' AND NEW.b='theta'
                            BEGIN SELECT RAISE(ABORT, 'synthetic decay failure'); END;
                        """)
                    else:
                        db.executescript("""
                            CREATE TRIGGER reject_audit BEFORE INSERT ON memory_event_audits
                            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END;
                        """)
                    audit = {"untouched": True}
                    with self.assertRaisesRegex(sqlite3.IntegrityError, f"synthetic {failure} failure"):
                        graph.retrieve("synthetic", [], "alpha", 3.0, event_id="failed", audit=audit)
                    self.assertEqual(audit, {"untouched": True})
                    self.assertFalse(db.in_transaction)
                    self.assertEqual(self.database_state(db), before)
                self.assert_database_equal(original_db, current_db)

    def test_full_retrieval_audit_and_database_equivalence_across_lifecycle(self):
        for mode in ("static", "dynamic"):
            with self.subTest(mode=mode):
                original_db, current_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
                self.addCleanup(original_db.close)
                self.addCleanup(current_db.close)
                original, current = _LegacyMemoryGraph(original_db), MemoryGraph(current_db)
                facts = [
                    ("cluster-old", ["alpha", "beta", "gamma", "delta"]),
                    ("cluster-retained", ["alpha", "beta", "gamma", "delta"]),
                    ("pair-a", ["alpha", "beta"]),
                    ("pair-b", ["beta", "gamma"]),
                    ("unrelated-a", ["epsilon", "zeta"]),
                    ("unrelated-b", ["theta", "iota"]),
                    ("unsupported-a", ["orphan-one"]),
                    ("unsupported-b", ["orphan-two"]),
                ]
                for graph in (original, current):
                    for source, marks in facts:
                        graph.add("synthetic", source, "author", [{"text": source, "quote": source,
                                  "marks": marks}], 1.0)

                def compare(event, query, now):
                    old_audit, new_audit = {}, {}
                    old_records = original.retrieve("synthetic", [], query, now, event_id=event,
                                                    audit=old_audit, ranking_mode=mode)
                    new_records = current.retrieve("synthetic", [], query, now, event_id=event,
                                                   audit=new_audit, ranking_mode=mode)
                    self.assertEqual(new_records, old_records)
                    assert_retrieval_audits_compatible(self, old_audit, new_audit,
                                                      original_db, current_db)
                    self.assert_database_equal(original_db, current_db)
                    return new_audit

                compare("first", "alpha beta gamma delta", 2.0)
                compare("single-hit", "alpha", 3.0)
                compare("context", "alpha gamma", 4.0)
                replay = compare("first", "alpha beta gamma delta", 5.0)
                self.assertFalse(replay["observation"]["applied"])
                compare("unsupported", "orphan-one orphan-two", 6.0)
                compare("no-hit", "unseen mark", 7.0)
                with patch("indeces.memory.time.time", return_value=8.0):
                    for graph in (original, current):
                        graph.deactivate_source("synthetic", "cluster-old")
                retired = compare("retired", "alpha beta gamma delta", 9.0)
                self.assertTrue(all(c["source_id"] != "cluster-old"
                                    for c in retired["selection"]["ranked_candidates"]))
                for graph in (original, current):
                    graph.add("synthetic", "cluster-new", "author", [{"text": "new cluster", "quote": "new cluster",
                              "marks": ["alpha", "beta", "gamma", "delta"]}], 10.0)
                compare("first", "alpha beta gamma delta", 11.0)
                compare("replacement", "alpha beta gamma delta", 12.0)


if __name__ == "__main__":
    unittest.main()
