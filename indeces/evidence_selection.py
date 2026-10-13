"""Offline evidence selection over supplied frozen records; no database or API.

This module is deliberately not wired into Runtime. Scores describe lexical
coverage, never semantic entailment. Gold annotations are not an input.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
import time

from .relevance import contains, normalize, plan

POLICY = "evidence_offline_v1"
DEFAULT_LIMITS = dict(reference_limit=3, preview_total=400, preview_entry=110,
                      quote_total=1200, read_chars=4096, read_seconds=0.25,
                      read_rounds=2)
# Generic question instructions and answer forms are demands, not topic concepts.
QUESTION_WORDS = frozenset("according explain describe discuss give show state stated identify determine values value numerical numeric equation equations formula formulas expression expressions range ranges magnitude correction corrections relationship relationships relation relations premise premises alternative alternatives pathway pathways mechanism mechanisms assumption assumptions steps step respectively based result results article study studies authors author figure figures fig table tables supporting support citation citations quote quotes page pages relevant relevantly text answer question reference references details detailed following follow first second third additionally also whether can could should would must need needs derive derived derivation derivations account accounts accounted consider considered energy".split()) - {"energy"}
FORM_RULES = {
    "numeric": (r"数值|多少|范围|常数|\b(?:value|values|numeric|numerical|range|ranges|constant|constants|magnitude)\b",
                r"(?<![A-Za-z])[-+−]?\d+(?:\.\d+)?"),
    "formula": (r"公式|方程|表达式|\b(?:equation|equations|formula|formulas|expression|expressions)\b",
                r"[=≈≤≥]|\b(?:equals|proportional)\b"),
    "range": (r"范围|区间|\b(?:range|ranges|interval|intervals)\b",
              r"[-+−]?\d+(?:\.\d+)?\s*(?:[A-Za-z]+\s*)?(?:to|through|–|—|-)\s*[-+−]?\d+(?:\.\d+)?"),
    "definition": (r"定义|含义|\b(?:define|definition|means|meaning)\b",
                   r"\b(?:is|means|defined|denotes|represents)\b|定义|表示|指的是"),
    "enumeration": (r"几类|哪些|步骤|\b(?:classes|categories|types|kinds|steps)\b",
                    r"(?:^|\n)\s*(?:\d+[.)]|[-•])|\b(?:include|includes|consist|comprise)\b"),
    "condition": (r"前提|条件|假设|\b(?:premise|premises|condition|conditions|assumption|assumptions)\b",
                  r"\b(?:if|when|provided|assum|require|only|pair|adjacent)\w*\b|条件|假设|前提"),
    "alternative": (r"替代|另一|\b(?:alternative|alternatives|another|instead)\b",
                    r"\b(?:alternative|another|instead|either|or)\b|替代|另一"),
}


def classify(text):
    """Conservative line spans. Ambiguous/mixed text stays inspectable."""
    spans, offset = [], 0
    lines = text.splitlines(keepends=True) or [text]
    author_start = re.compile(r"^\s*(?:\[\d+\]|\(\d+\)|\d+[.)])\s*(?:(?:[A-Z]\.\s*){1,4}[A-Z][a-z]|[A-Z][a-z]+,)")
    for line in lines:
        caption = bool(re.search(r"^\s*(?:Fig(?:ure)?\.?|Scheme|Table)\s*\d+\b|^\s*[图表]\s*\d+", line, re.I))
        numbered_author = bool(author_start.search(line))
        # A single year or a numbered methodological step is insufficient.
        ref_signals = sum((bool(re.search(r"\b(?:19|20)\d{2}\b", line)),
                           bool(re.search(r"\b(?:doi|vol\.|pp\.)|10\.\d{4,9}/", line, re.I)),
                           len(re.findall(r"\b[A-Z]\.\s*[A-Z]", line)) >= 2,
                           bool(re.search(r"\b(?:J\.|Journal|Chem\.|Phys\.|Catal\.|Rev\.)", line))))
        kind = "caption" if caption else "bibliography" if numbered_author and ref_signals >= 2 else "body"
        confidence = "high" if caption or kind == "bibliography" else "low"
        if re.search(r"all rights reserved|copyright|©|www\.|published online|received:|accepted:", line, re.I) and len(line) < 220:
            kind, confidence = "publication_metadata", "medium"
        spans.append(dict(start=offset, end=offset + len(line), kind=kind, confidence=confidence))
        offset += len(line)
    # Wrapped bibliographic entries can put the year/journal on the next line.
    # Only an explicit numbered author start licenses continuation classification.
    for index, line in enumerate(lines):
        if not author_start.search(line):
            continue
        end = index + 1
        while (end < min(len(lines), index + 5) and lines[end].strip()
               and not author_start.search(lines[end]) and spans[end]["kind"] == "body"
               and not re.search(r"\b(?:is|are|was|were|has|have|can|will|measured|shows|requires|increases)\b", lines[end], re.I)):
            end += 1
        entry = " ".join(lines[index:end])
        signals = sum((bool(re.search(r"\b(?:19|20)\d{2}\b", entry)),
                       bool(re.search(r"\b(?:doi|vol\.|pp\.)|10\.\d{4,9}/", entry, re.I)),
                       len(re.findall(r"\b[A-Z]\.\s*[A-Z]", entry)) >= 2,
                       bool(re.search(r"\b(?:J\.|Journal|Chem\.|Phys\.|Catal\.|Rev\.)", entry))))
        if signals >= 2:
            for s in spans[index:end]:
                s.update(kind="bibliography", confidence="high")
    kinds = {s["kind"] for s in spans if text[s["start"]:s["end"]].strip()}
    kind = next(iter(kinds)) if len(kinds) == 1 else "mixed" if kinds else "unknown"
    content = "".join(text[s["start"]:s["end"]] for s in spans if s["kind"] in ("body", "caption"))
    return dict(kind=kind, spans=spans, content=content,
                confidence="low" if kind in ("body", "mixed", "unknown") else "high")


def concept_plan(query, candidates, statistics):
    # Explicit publication identity is optional context, never a topic need.
    question = re.sub(r"(?im)^\s*(?:来源|出处|source|publication)\s*[:：].*$", "", query)
    question = re.sub(r"10\.\d{4,9}/[A-Za-z0-9.()_;/:-]+", "", question)
    focus = plan(question)["focus"]
    marks = sorted({m for c in candidates for m in c.get("marks", []) if contains(m, focus)},
                   key=lambda m: (-len(normalize(m)), normalize(m), m))
    groups = []
    for mark in marks:
        key = normalize(mark)
        equal = next((g for g in groups if key == g["key"]), None)
        if equal:
            equal["aliases"].append(mark)
        elif not any(contains(mark, g["key"]) for g in groups):
            groups.append(dict(key=key, aliases=[mark], basis="existing_mark"))
    # Only explicit full-form (ACRONYM) in the question establishes this alias.
    for full, short in re.findall(r"([A-Za-z][A-Za-z -]{3,80})\s*\(([A-Z]{2,8})\)", focus):
        words = full.split()
        suffix = words[-len(short):]
        if "".join(w[0].upper() for w in suffix) != short:
            continue
        expanded = " ".join(suffix)
        relevant = [g for g in groups if g["key"] in (normalize(expanded), normalize(short))]
        if relevant:
            aliases = list(dict.fromkeys([expanded, short] + [a for g in relevant for a in g["aliases"]]))
            groups = [g for g in groups if g not in relevant]
            groups.append(dict(key=normalize(expanded), aliases=aliases, basis="explicit_question_acronym"))
    for term in plan(question)["terms"]:
        if term in QUESTION_WORDS or any(contains(term, alias) for g in groups for alias in g["aliases"]):
            continue
        groups.append(dict(key=term, aliases=[term], basis="focus_token"))
    groups = groups[:32]
    n = statistics.get("n")
    n_valid = type(n) is int and n >= 1
    for g in groups:
        counts = statistics.get("mark_counts", {}) if g["basis"] != "focus_token" else statistics.get("term_counts", {})
        observed = [counts[a] for a in g["aliases"] if a in counts and type(counts[a]) is int
                    and n_valid and 0 <= counts[a] <= n]
        # Alias-union df is not available in historical receipts. max is the
        # conservative documented approximation; never sum overlapping aliases.
        df = max(observed) if observed else None
        g.update(df=df, df_status="observed_max_alias_df" if df is not None else "missing_neutral",
                 raw_weight=min(2.0, 1.0 + math.log((n + 1) / (df + 1)) / math.log(n + 1)) if df is not None else 1.0)
    total = sum(g["raw_weight"] for g in groups)
    for g in groups:
        g["weight"] = g["raw_weight"] / total if total else 0.0
    forms = [name for name, (request, _) in FORM_RULES.items() if re.search(request, focus, re.I)]
    identity = plan(query)
    return dict(focus=focus, concepts=groups, forms=forms, source_dois=identity["dois"], source_titles=identity["titles"],
                frequency_unit="active_record_presence_in_frozen_scope", n=n,
                alias_df="maximum_observed_alias_df_not_exact_union")


def _coverage(candidate, query_plan, classify_enabled):
    typed = classify(candidate["text"])
    text = typed["content"] if classify_enabled else candidate["text"]
    covered = {g["key"] for g in query_plan["concepts"] if any(contains(a, text) for a in g["aliases"])}
    coverage = sum(g["weight"] for g in query_plan["concepts"] if g["key"] in covered)
    # Demand forms count only in a sentence/line that also contains a topic.
    local = [s for s in re.split(r"(?<=[.!?])\s+|\n", text)
             if any(contains(a, s) for g in query_plan["concepts"] for a in g["aliases"])]
    forms = {f for f in query_plan["forms"] if any(re.search(FORM_RULES[f][1], s, re.I) for s in local)}
    phrase = any(" " in g["key"] and g["key"] in covered for g in query_plan["concepts"])
    relevant = bool(covered) and coverage >= 0.2 and (len(query_plan["concepts"]) == 1 or len(covered) >= 2 or phrase)
    source_passed = not query_plan.get("source_constraint_applied") or _source_match(candidate, query_plan)
    relevant = relevant and source_passed
    return dict(classification=typed, concepts=sorted(covered), forms=sorted(forms),
                relevance=coverage, eligible=relevant,
                gate="pass" if relevant else "source_constraint_mismatch" if not source_passed else "insufficient_focus_coverage")


def _member(c):
    return {k: deepcopy(c[k]) for k in ("key", "source_id", "source_version", "scope", "chunk_index", "start_character", "baseline_rank", "baseline_scores", "historical_record_id") if k in c}


def _overlap(left, right):
    """Exact, provenance-compatible overlap only; never fuzzy page deletion."""
    if not _same_source(left, right):
        return None
    a, b = left["text"], right["text"]
    if a == b:
        return a
    sa, sb = left.get("start_character"), right.get("start_character")
    if type(sa) is not int or type(sb) is not int:
        return None
    if sb < sa:
        return _overlap(right, left)
    position = sb - sa
    if position >= len(a) or len(a) - position < 24:
        return None
    overlap = min(len(a) - position, len(b))
    if a[position:position + overlap] != b[:overlap]:
        return None
    return a + b[overlap:]


def _same_source(left, right):
    keys = ("source_id", "source_version", "scope")
    return all(isinstance(left.get(k), str) and left[k] and left[k] == right.get(k) for k in keys)


def _rank(candidate):
    rank = candidate.get("baseline_rank")
    return rank if type(rank) is int and rank > 0 else math.inf


def _source_match(candidate, query_plan):
    hint = candidate.get("source_hint", "")
    dois = set(plan(hint)["dois"])
    return any(doi in dois for doi in query_plan.get("source_dois", [])) or any(contains(title, hint) for title in query_plan.get("source_titles", []))


def _chunk_sort(candidate):
    chunk = candidate.get("chunk_index")
    return chunk if type(chunk) is int and chunk >= 0 else math.inf


def _deduplicate(candidates, enabled, quote_total):
    result, merges = [], []
    for c in sorted(candidates, key=lambda c: (_rank(c), str(c["key"]))):
        c = deepcopy(c)
        c["members"] = c.get("members", [_member(c)])
        old = next((p for p in result if enabled and normalize(p["text"]) == normalize(c["text"])), None)
        if old:
            old["members"].extend(c["members"])
            merges.append(dict(kept=old["key"], merged=c["key"], reason="normalized_duplicate"))
            continue
        merged = False
        if enabled:
            for p in result:
                joined = _overlap(p, c)
                if joined is not None and len(joined) <= quote_total:
                    p["text"] = joined
                    if type(c.get("start_character")) is int:
                        p["start_character"] = min(p["start_character"], c["start_character"])
                    p["marks"] = sorted(set(p.get("marks", [])) | set(c.get("marks", [])))
                    p["members"].extend(c["members"])
                    merges.append(dict(kept=p["key"], merged=c["key"], reason="exact_source_overlap"))
                    merged = True
                    break
        if not merged:
            result.append(c)
    return result, merges


def select_evidence(query, candidates, *, statistics, neighbors=(), limits=None, features=None):
    """Return separate offline materials/audit/gaps; inputs remain unchanged.

    neighbors must be supplied frozen chunks. The function cannot discover new
    sources, generate queries, invoke NPMI or access gold annotations.
    """
    options = dict(weighted=True, classification=True, diversity=True, reread=True)
    options.update(features or {})
    caps = {**DEFAULT_LIMITS, **(limits or {})}
    for k in caps:
        if isinstance(caps[k], bool) or not isinstance(caps[k], (int, float)) or not math.isfinite(caps[k]) or caps[k] < 0:
            raise ValueError("invalid evidence budget: " + k)
    for k in ("reference_limit", "preview_total", "preview_entry", "quote_total", "read_chars", "read_rounds"):
        if type(caps[k]) is not int:
            raise ValueError("integer evidence budget required: " + k)
    if caps["reference_limit"] > 3 or caps["preview_total"] > 400 or caps["preview_entry"] > 110:
        raise ValueError("offline output exceeds existing reference/preview caps")
    if caps["quote_total"] > 1200:
        raise ValueError("offline output exceeds first-version complete quote cap")
    if caps["read_chars"] > 4096 or caps["read_rounds"] > 2 or caps["read_seconds"] > 0.25:
        raise ValueError("offline reread exceeds declared pilot caps")
    candidates = list(candidates)
    if len({c["key"] for c in candidates}) != len(candidates):
        raise ValueError("candidate keys must be unique")
    qp = concept_plan(query, candidates, statistics)
    qp["source_constraint_applied"] = bool((qp["source_dois"] or qp["source_titles"]) and any(_source_match(c, qp) for c in candidates))
    qp["source_constraint_status"] = "matched_frozen_identity" if qp["source_constraint_applied"] else "unavailable" if qp["source_dois"] or qp["source_titles"] else "not_requested"
    if not options["weighted"] and qp["concepts"]:
        for g in qp["concepts"]:
            g["weight"] = 1.0 / len(qp["concepts"])
    # Constrain before dedup: an earlier identical quote from another source
    # cannot displace the actual requested source's provenance.
    allowed_candidates = [c for c in candidates if not qp["source_constraint_applied"] or _source_match(c, qp)]
    source_excluded = [dict(key=c["key"], baseline_rank=c.get("baseline_rank"), baseline_scores=c.get("baseline_scores", {}), gate="source_constraint_mismatch", eligible=False) for c in candidates if c not in allowed_candidates]
    pool, merges = _deduplicate(allowed_candidates, options["diversity"], caps["quote_total"])
    decisions, covered, forms, selected = [], set(), set(), []
    used = 0

    def choose():
        nonlocal selected, covered, forms, used, decisions
        selected, covered, forms, used, decisions = [], set(), set(), 0, []
        remaining = [(c, _coverage(c, qp, options["classification"])) for c in pool]
        while remaining and len(selected) < caps["reference_limit"]:
            eligible = []
            for c, evidence in remaining:
                if not evidence["eligible"] or used + len(c["text"]) > caps["quote_total"]:
                    continue
                new = set(evidence["concepts"]) - covered
                form_new = set(evidence["forms"]) - forms
                gain = sum(g["weight"] for g in qp["concepts"] if g["key"] in new)
                form_gain = len(form_new) / len(qp["forms"]) if qp["forms"] else 0.0
                utility = (gain + 0.25 * form_gain) if options["diversity"] else evidence["relevance"] + 0.25 * (len(evidence["forms"]) / len(qp["forms"]) if qp["forms"] else 0)
                if options["diversity"] and utility <= 0:
                    continue
                eligible.append((utility, evidence["relevance"], -_rank(c), str(c["key"]), c, evidence, new, form_new))
            if not eligible:
                break
            # Last tie uses original order then stable lexical key, not known IDs.
            best = sorted(eligible, key=lambda x: (-x[0], -x[1], -x[2], x[3]))[0]
            utility, _, _, _, c, evidence, new, form_new = best
            selected.append({**deepcopy(c), "coverage": evidence})
            covered.update(evidence["concepts"])
            forms.update(evidence["forms"])
            used += len(c["text"])
            decisions.append(dict(key=c["key"], marginal_gain=utility, newly_covered=sorted(new), newly_covered_forms=sorted(form_new), covered_after=sorted(covered), forms_after=sorted(forms)))
            remaining = [(p, e) for p, e in remaining if p["key"] != c["key"]]

    choose()
    read_used, read_rounds, read_keys, stop = 0, 0, [], "disabled"
    started = time.perf_counter()
    seen = {c["key"] for c in candidates}
    def coordinate(c):
        return (c.get("source_id"), c.get("source_version"), c.get("scope"), c.get("chunk_index")) if type(c.get("chunk_index")) is int else None
    seen_coordinates = {coordinate(c) for c in candidates if coordinate(c) is not None}
    pending = sorted(list(neighbors), key=lambda c: (str(c.get("source_id", "")), _chunk_sort(c), str(c["key"])))
    if options["reread"]:
        stop = "round_budget_exhausted"
        for _ in range(caps["read_rounds"]):
            missing = {g["key"] for g in qp["concepts"]} - covered
            if not missing and set(qp["forms"]) <= forms:
                break
            if time.perf_counter() - started >= caps["read_seconds"]:
                stop = "time_budget_exhausted"
                break
            anchors = selected or sorted(pool, key=_rank)[:caps["reference_limit"]]
            approved = []
            for neighbor in pending:
                if neighbor["key"] in seen or coordinate(neighbor) in seen_coordinates:
                    continue
                if time.perf_counter() - started >= caps["read_seconds"]:
                    stop = "time_budget_exhausted"
                    break
                if not any(_same_source(a, neighbor)
                           and type(a.get("chunk_index")) is int and type(neighbor.get("chunk_index")) is int
                           and abs(a["chunk_index"] - neighbor["chunk_index"]) == 1 for a in anchors):
                    continue
                if read_used + len(neighbor["text"]) > caps["read_chars"]:
                    stop = "character_budget_exhausted"
                    break
                read_used += len(neighbor["text"])
                read_keys.append(neighbor["key"])
                seen.add(neighbor["key"])
                if coordinate(neighbor) is not None:
                    seen_coordinates.add(coordinate(neighbor))
                evidence = _coverage(neighbor, qp, options["classification"])
                if evidence["eligible"] and (set(evidence["concepts"]) - covered or set(evidence["forms"]) - forms):
                    approved.append(neighbor)
            read_rounds += 1
            if not approved:
                if stop not in ("time_budget_exhausted", "character_budget_exhausted"):
                    stop = "no_new_evidence"
                break
            previous = (set(covered), set(forms))
            exhausted = stop if stop in ("time_budget_exhausted", "character_budget_exhausted") else None
            pool, new_merges = _deduplicate(pool + approved, options["diversity"], caps["quote_total"])
            merges.extend(new_merges)
            choose()
            if previous == (covered, forms):
                stop = exhausted or "no_new_selected_coverage"
                break
            if exhausted:
                stop = exhausted
                break
            stop = "round_budget_exhausted"
        if {g["key"] for g in qp["concepts"]} <= covered and set(qp["forms"]) <= forms:
            stop = "requirements_covered"
    preview_used = 0
    for i, c in enumerate(selected, 1):
        available = min(caps["preview_entry"], caps["preview_total"] - preview_used)
        preview = c["text"][:max(0, available)]
        c.update(citation_marker=f"[M{i}]", preview=preview, preview_truncated=len(preview) < len(c["text"]))
        preview_used += len(preview)
    return dict(policy=POLICY, selected=selected, query_plan=qp,
                gaps=dict(concepts=sorted({g["key"] for g in qp["concepts"]} - covered), forms=sorted(set(qp["forms"]) - forms), semantic_support="unverified"),
                budget=dict(limits=caps, quote_characters=used, preview_characters=preview_used,
                            additional_read_characters=read_used, read_rounds=read_rounds,
                            read_seconds=time.perf_counter() - started, stop_reason=stop),
                audit=dict(features=options, selection_order=decisions, merges=merges, read_keys=read_keys,
                           candidates=source_excluded + [dict(key=c["key"], baseline_rank=c.get("baseline_rank"), baseline_scores=c.get("baseline_scores", {}), **_coverage(c, qp, options["classification"])) for c in pool]))
