"""Versioned, deterministic lexical coverage; never semantic entailment.

All indexes are derived from active original records. Publication rebuilds them;
retrieval only reads a bounded lexical pool and label postings. No corpus names,
answer values, or scientific synonym tables participate in the policy.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid
import unicodedata

from .path_policy import validate_managed_path

POLICY = "concept_v1"
MAX_MARKS = 64
LEXICAL_LIMIT = 128
MAX_TERMS = 32
STOP = frozenset("a an the of in on at to for from by with and or is are was were be been as it its this that these those what which how where when who why do does did used use using reported report please answer evidence include page record block ids short supporting excerpt distinguish anything general knowledge found say not no perform new ingest modify settings only round paper calculate calculated et al versus then between specifically section experimental concrete".split())


def normalize(text):
    # Chemical stars, plus signs and parenthesized indices remain significant.
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'(?<=[A-Za-z])[-‐]\s*\n\s*(?=[a-z])', '', text)
    return re.sub(r"\s+", " ", re.sub(r"[-‐‑‒–—]+", " ", text.casefold())).strip()


def contains(term, text):
    term, text = normalize(term), normalize(text)
    if not term:
        return False
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", text))


def plan(query, context_query=''):
    # Separate an explicit quoted publication identity from the actual question.
    focus = re.sub(r'[“"].*?[”"]\s*\([^)]*(?:\b\d{4}\b|doi)[^)]*\)', ' ', query, flags=re.I)
    focus = re.split(r"(?:Please\s|Use this round|Base the answer|No ingest|Do not ingest)", focus, flags=re.I)[0]
    question = re.search(r"\b(?:what|how|where|which)\b", focus, re.I)
    if question:
        focus = focus[question.start():]
    terms = list(dict.fromkeys(t for t in re.findall(r"[a-z][a-z0-9_]*", normalize(focus))
                               if t not in STOP and not t.isdigit() and len(t) > 1))[:MAX_TERMS]
    dois = re.findall(r"10\.\d{4,9}/[A-Za-z0-9.()_;/:-]+", query, re.I)
    result = {"focus": focus, "terms": terms, "dois": [d.rstrip('.,);').casefold() for d in dois],
            'titles': [normalize(t) for t in re.findall(r'[“"]([^”"]{20,})[”"]', query)],
            'years': re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', query),
            'enumeration': bool(re.search(r'几类|哪.*类|哪些类型|\b(?:classes|categories|types|kinds)\b', focus, re.I)),
            'definition': bool(re.search(r'具体.*(?:哪|什么)|\b(?:define|definition|what.*(?:descriptor|difference|parameter))\b', focus, re.I))}
    if context_query and re.search(r'这套|这篇|上述|\b(?:this|that)\s+(?:paper|study|analysis)\b', query, re.I):
        prior = plan(context_query)
        for key in ('dois', 'titles', 'years'):
            if not result[key]:
                result[key] = prior[key]
    return result


def idf(n, frequency):
    return 1.0 + math.log((n + 1) / (frequency + 1))


def metadata_factor(text):
    lines = text.splitlines()
    refs = len(re.findall(r"(?:^|\n)\s*(?:\[\d+\]|\d+[.)])\s*(?:[A-Z]\.\s*[A-Z][a-z]|[A-Z][a-z]+,\s*[A-Z]\.)", text))
    years = len(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if refs >= 2 or (years >= 3 and len(re.findall(r"\b[A-Z]\.\s", text)) >= 4):
        return "bibliography", 0.25
    if re.search(r"(?:all rights reserved|www\.|©|copyright|published online|received:|accepted:)", text, re.I):
        return "publication_metadata", 0.55
    if re.search(r"\bdoi\s*:|10\.\d{4,9}/", text, re.I) and years:
        return "publication_identity", 0.7
    return "body_or_title", 1.0


def score(inputs):
    # Overlapping mark phrases count once; body terms provide label-independent
    # coverage. Source identity is a soft prior, never an eligibility gate.
    return round(inputs["metadata_factor"] * (inputs["mark_relevance"]
                 + inputs["body_relevance"] + inputs['answer_form_bonus']) + inputs["source_prior"], 6)


def relevance(text, direct, mark_weights, term_weights, source_hint, query_plan):
    chosen = []
    for mark in sorted(direct, key=lambda m: (-mark_weights[m], -len(normalize(m)), m)):
        if not any(contains(mark, other) for other in chosen):
            chosen.append(mark)
    kind, factor = metadata_factor(text)
    body = sorted(term for term in term_weights if contains(term, text))
    normalized_hint = normalize(source_hint)
    prior = 12.0 if (any(d in source_hint.casefold() for d in query_plan["dois"])
                     or any(t in normalized_hint for t in query_plan['titles'])) else 0.0
    if not prior and query_plan['years'] and any(y in source_hint for y in query_plan['years']):
        prior = 4.0
    enumeration = query_plan['enumeration'] and bool(re.search(r'\b(?:include|includes|comprise|comprises|consist|types|classes|categories)\b[^.]{0,250}(?:,[^.]{0,80}){3}', normalize(text)))
    definition = query_plan['definition'] and any(re.search(r'\b(?:is|means|defined|denotes|represents)\b[^.]{0,100}\b' + re.escape(t) + r'\b', normalize(text))
                                               for t in term_weights if len(t) >= 5)
    inputs = {"metadata_kind": kind, "metadata_factor": factor,
              "mark_terms": chosen, "body_terms": body,
              "literal_mark_terms": [m for m in chosen if contains(m, text)],
              "mark_relevance": round(0.25 * sum(mark_weights[m] * (1.0 if contains(m, text) else 0.15) for m in chosen), 6),
              "body_relevance": round(sum(term_weights[t] for t in body), 6),
              "source_prior": prior}
    inputs['answer_form_bonus'] = 10.0 if enumeration else 6.0 if definition else 0.0
    return inputs, score(inputs)


def source_hint(texts, path=''):
    unique = list(dict.fromkeys(texts))
    header = ''.join(unique)[:4000]
    identity = ''.join(t for t in unique if metadata_factor(t)[0] in
                       ('publication_metadata', 'publication_identity'))[:4000]
    return (path + '\n' if path else '') + header + '\n' + identity


class CoverageIndex:
    def __init__(self, db):
        self.db = db

    def ensure_schema(self):
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'memory_coverage_fts', 'memory_coverage_sources', 'memory_coverage_trie',
                    'memory_coverage_scopes', 'memory_coverage_events'}
        self.recreated = bool(required - tables)
        if self.recreated:
            filename = next(r[2] for r in self.db.execute("PRAGMA database_list") if r[1] == "main")
            if filename:
                directory = validate_managed_path(Path(filename).parent / "migration_backups", "coverage backups")
                directory.mkdir(exist_ok=True)
                destination = directory / ("memory.before-coverage-" + uuid.uuid4().hex + ".sqlite3")
                with destination.open("xb"):
                    pass
                backup = sqlite3.connect(destination)
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
        if 'memory_coverage_fts' in tables and 'memory_coverage_events' not in tables:
            raise ValueError('coverage event identities missing; restore during maintenance')
        self.db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_coverage_fts USING fts5(
                scope UNINDEXED, record_id UNINDEXED, body, scope_key, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS memory_coverage_sources(
                scope TEXT NOT NULL, source_id TEXT NOT NULL, hint TEXT NOT NULL,
                PRIMARY KEY(scope,source_id));
            CREATE TABLE IF NOT EXISTS memory_coverage_trie(
                scope TEXT NOT NULL, prefix TEXT NOT NULL, terminals TEXT NOT NULL,
                PRIMARY KEY(scope,prefix));
            CREATE TABLE IF NOT EXISTS memory_coverage_scopes(
                scope TEXT PRIMARY KEY, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_coverage_events(
                scope TEXT NOT NULL, event_id TEXT NOT NULL, context_query TEXT NOT NULL,
                PRIMARY KEY(scope,event_id));
        """)

    def rebuild(self, scope, records, revision):
        for table in ("memory_coverage_fts", "memory_coverage_sources", "memory_coverage_trie"):
            self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
        key = 's' + hashlib.sha256(scope.encode()).hexdigest()
        self.db.executemany("INSERT INTO memory_coverage_fts VALUES(?,?,?,?)",
                            [(scope, r["id"], normalize(r["text"]), key) for r in records])
        texts, trie = defaultdict(list), defaultdict(list)
        for record in records:
            texts[record['source_id']].append(record['text'])
        hints = {s: source_hint(t) for s, t in texts.items()}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_versions'").fetchone():
            for source in hints:
                row = self.db.execute('SELECT path FROM knowledge_versions WHERE source_id=? AND scope=?', (source, scope)).fetchone()
                if row:
                    hints[source] = source_hint(texts[source], row[0])
        for mark in sorted({m for r in records for m in r["marks"]}):
            value = normalize(mark)
            for i in range(1, len(value) + 1):
                trie[value[:i]]
            trie[value].append(mark)
        self.db.executemany("INSERT INTO memory_coverage_sources VALUES(?,?,?)",
                            [(scope, s, h) for s, h in hints.items()])
        self.db.executemany("INSERT INTO memory_coverage_trie VALUES(?,?,?)",
                            [(scope, p, json.dumps(v, ensure_ascii=False)) for p, v in trie.items()])
        self.db.execute("INSERT OR REPLACE INTO memory_coverage_scopes VALUES(?,?)", (scope, revision))

    def matches(self, scope, query):
        query, matches = normalize(query), set()
        cache = {}
        for start in range(len(query)):
            if start and query[start - 1].isascii() and (query[start - 1].isalnum() or query[start - 1] == '_'):
                continue
            for end in range(start + 1, len(query) + 1):
                prefix = query[start:end]
                if prefix not in cache:
                    row = self.db.execute("SELECT terminals FROM memory_coverage_trie WHERE scope=? AND prefix=?", (scope, prefix)).fetchone()
                    cache[prefix] = json.loads(row[0]) if row else None
                terminals = cache[prefix]
                if terminals is None:
                    break
                if end == len(query) or not (query[end].isascii() and (query[end].isalnum() or query[end] == '_')):
                    matches.update(terminals)
        return matches

    def lexical(self, scope, query_plan, n):
        terms = query_plan["terms"]
        weights, counts = {}, {}
        scope_filter = 'scope_key:s' + hashlib.sha256(scope.encode()).hexdigest()
        for term in terms:
            expression = scope_filter + ' AND body:"' + term.replace('"', '""') + '"'
            count = self.db.execute("SELECT COUNT(*) FROM memory_coverage_fts WHERE memory_coverage_fts MATCH ? AND scope=?", (expression, scope)).fetchone()[0]
            if count:
                weights[term] = round(idf(n, count), 6)
                counts[term] = count
        if not weights:
            return [], weights, counts
        expression = scope_filter + ' AND body:(' + ' OR '.join('"' + t.replace('"', '""') + '"' for t in weights) + ')'
        patterns = [(re.compile(r'(?<![A-Za-z0-9_])' + re.escape(t) + r'(?![A-Za-z0-9_])'), w) for t, w in weights.items()]
        self.db.create_function('coverage_lexical_rank', 1, lambda body: sum(w for pattern, w in patterns if pattern.search(body)))
        try:
            ids = [r[0] for r in self.db.execute("SELECT record_id FROM memory_coverage_fts WHERE memory_coverage_fts MATCH ? AND scope=? ORDER BY coverage_lexical_rank(body) DESC,record_id DESC LIMIT ?", (expression, scope, LEXICAL_LIMIT))]
        finally:
            self.db.create_function('coverage_lexical_rank', 1, None)
        return ids, weights, counts

    def hint(self, scope, source):
        row = self.db.execute("SELECT hint FROM memory_coverage_sources WHERE scope=? AND source_id=?", (scope, source)).fetchone()
        return row[0] if row else ""
