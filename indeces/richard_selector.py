"""Opt-in Richard v3 NPMI association over already eligible local evidence.

This is a repository-owned ranking adaptation of the frozen concept graph,
not the bundled, unfrozen solver protocol. It never performs type filtering,
expands the native eligible pool, changes graph weights, or executes code from
the supplied archive. Preload the graph before Runtime/SQLite retrieval.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from math import fsum
from types import MappingProxyType

from .contracts import GovernedError
from .richard_aliases import ALIAS_POLICY, LexicalAliasPolicy
from .richard_semantics import SEMANTIC_POLICY, UNRESOLVED_LABELS
from .selection_policy import (RankedTopThreeSelector, SelectionDecision,
                               SelectionCandidate, SelectionExclusion, SelectionLimits, SelectionRequest, digest)

SCORING_VERSION = "released_npmi_association_v1"
MAX_QUERY_CHARACTERS = 4096
MAX_CANDIDATES = 128
MAX_MARKS_PER_CANDIDATE = 8
MAX_MARK_CHARACTERS = 40


def _word(char):
    return (char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")
            or char in ("\u200c", "\u200d"))


def _freeze_trie(node):
    return MappingProxyType({key: tuple(value) if key is None else _freeze_trie(value)
                             for key, value in node.items()})


def _matches(text, trie, *, compact=False):
    def continuation(char):
        return _word(char) or (compact and (unicodedata.category(char) in ("Sm", "Pd")
            or char in "+-*./\u00b7()[]\u207a\u207b\u208a\u208b\u2010\u2011\u2012\u2013\u2014\u2212"))

    found = set()
    for start, char in enumerate(text):
        if start and continuation(text[start - 1]):
            continue
        node = trie
        for end in range(start, len(text)):
            node = node.get(text[end])
            if node is None:
                break
            if end + 1 == len(text) or not continuation(text[end + 1]):
                found.update(node.get(None, ()))
    return found


@dataclass(frozen=True)
class RichardGraphSelector:
    graph: object = field(repr=False)
    mode: str = "shadow"
    policy_id: str = field(default="richard_npmi_association", init=False)
    policy_version: str = field(init=False)
    _exact: object = field(init=False, repr=False)
    _folded: object = field(init=False, repr=False)
    _blocked: object = field(init=False, repr=False)
    _aliases: object = field(init=False, repr=False)
    _neighbors: object = field(init=False, repr=False)
    _fold_collisions: object = field(init=False, repr=False)

    def __post_init__(self):
        if self.mode not in ("shadow", "opt_in"):
            raise ValueError("Richard mode must be shadow or opt_in")
        from .richard_data import GraphData
        if not isinstance(self.graph, GraphData):
            raise ValueError("Richard selector requires validated GraphData")
        aliases = LexicalAliasPolicy(self.graph.name_to_cid)
        exact, folded, blocked = {}, {}, {}
        groups = {}
        for name, cid in self.graph.name_to_cid.items():
            groups.setdefault(name.casefold(), []).append((name, cid))
        collisions = {key: tuple(rows) for key, rows in groups.items()
                      if len({cid for name, cid in rows}) > 1}
        for name, cid in self.graph.name_to_cid.items():
            if name.casefold() in UNRESOLVED_LABELS or name.casefold() in collisions:
                target, key = blocked, name.casefold()
            else:
                # Case-insensitive matching is reserved for ordinary phrases
                # and long lowercase words. Compact identifiers preserve case.
                insensitive = " " in name or (len(name) > 4 and name.isalpha() and name.islower())
                target, key = (folded, name.casefold()) if insensitive else (exact, name)
            node = target
            for char in key:
                node = node.setdefault(char, {})
            node.setdefault(None, []).append((name, cid) if target is blocked else cid)
        object.__setattr__(self, "_exact", _freeze_trie(exact))
        object.__setattr__(self, "_folded", _freeze_trie(folded))
        object.__setattr__(self, "_blocked", _freeze_trie(blocked))
        object.__setattr__(self, "_aliases", aliases)
        object.__setattr__(self, "_neighbors", tuple(MappingProxyType(dict(row)) for row in self.graph.adjacency))
        object.__setattr__(self, "_fold_collisions", MappingProxyType(collisions))
        version = digest({"graph": self.graph.identity_sha256, "mode": self.mode,
            "scoring": SCORING_VERSION, "semantics": SEMANTIC_POLICY, "lexical": ALIAS_POLICY,
            "blocked": sorted(UNRESOLVED_LABELS), "max_candidates": MAX_CANDIDATES,
            "max_query_characters": MAX_QUERY_CHARACTERS,
            "max_marks_per_candidate": MAX_MARKS_PER_CANDIDATE,
            "max_mark_characters": MAX_MARK_CHARACTERS})
        object.__setattr__(self, "policy_version", version)

    def _candidate_cids(self, marks):
        result = set()
        unresolved = set()
        for mark in marks:
            if not isinstance(mark, str):
                raise GovernedError("invalid_selection_request")
            try:
                canonical = self._aliases.canonical(mark)
            except ValueError:
                raise GovernedError("invalid_selection_request") from None
            forms = {mark.casefold(), canonical.casefold()}
            blocked = forms & UNRESOLVED_LABELS
            collisions = forms & self._fold_collisions.keys()
            if blocked or collisions:
                unresolved.update(blocked)
                for collision in collisions:
                    unresolved.update(name for name, cid in self._fold_collisions[collision])
                continue
            # No guessed synonym, formula reordering, inferred charge or type.
            cid = self.graph.name_to_cid.get(mark)
            if cid is None:
                cid = self.graph.name_to_cid.get(canonical)
            if cid is not None:
                result.add(cid)
        return result, unresolved

    def evaluate(self, request: SelectionRequest) -> dict:
        """Return separate, reproducible diagnostics; never write an audit/log."""
        if (type(request) is not SelectionRequest or not isinstance(request.query, str)
                or not isinstance(request.scope, str) or not request.scope.strip()
                or not isinstance(request.event_id, str) or not request.event_id.strip()
                or not isinstance(request.context_query, str) or len(request.context_query) > MAX_QUERY_CHARACTERS
                or len(request.query) > MAX_QUERY_CHARACTERS
                or type(request.candidates) is not tuple
                or len(request.candidates) > MAX_CANDIDATES
                or type(request.limits) is not SelectionLimits
                or type(request.limits.references) is not int or not 1 <= request.limits.references <= 3
                or type(request.limits.total_text_characters) is not int or not 1 <= request.limits.total_text_characters <= 400
                or type(request.limits.entry_text_characters) is not int or not 1 <= request.limits.entry_text_characters <= 110
                or any(type(c) is not SelectionCandidate or type(c.record_id) is not int or c.record_id <= 0
                       or type(c.rank) is not int or c.rank <= 0 or type(c.marks) is not tuple
                       or len(c.marks) > MAX_MARKS_PER_CANDIDATE
                       or any(not isinstance(mark, str) or not mark.strip()
                              or len(mark) > MAX_MARK_CHARACTERS for mark in c.marks)
                       for c in request.candidates)
                or any(c.scope != request.scope for c in request.candidates)
                or len({c.record_id for c in request.candidates}) != len(request.candidates)):
            raise GovernedError("invalid_richard_selection_request")
        query_cids = _matches(request.query, self._exact, compact=True) | _matches(request.query.casefold(), self._folded)
        blocked_labels = _matches(request.query.casefold(), self._blocked, compact=True)
        unresolved = {name for name, cid in blocked_labels}
        scores = []
        for candidate in request.candidates:
            cids, blocked_names = self._candidate_cids(candidate.marks)
            unresolved.update(blocked_names)
            direct = len(cids & query_cids)
            # Each unordered concept pair contributes once. Association is
            # lexical co-occurrence, never semantic support for a quote.
            pairs = {tuple(sorted((a, b))) for a in query_cids for b in cids if a != b}
            weights = [self._neighbors[self.graph.cid_to_index[a]].get(b, 0.0) for a, b in sorted(pairs)]
            association = fsum(weights)
            scores.append({"record_id": candidate.record_id, "source_id": candidate.source_id,
                "fingerprint": candidate.fingerprint, "native_rank": candidate.rank,
                "concept_cids": sorted(cids), "direct_hits": direct, "association_score": association})
        baseline = RankedTopThreeSelector().select(request)
        status = "ranked" if query_cids else "no_graph_seed"
        if query_cids:
            ordered = sorted(scores, key=lambda row: (-row["direct_hits"], -row["association_score"],
                                                       row["native_rank"], row["record_id"]))
            # Preserve the existing preview eligibility/limits in new order.
            by_id = {c.record_id: c for c in request.candidates}
            chosen, used = [], 0
            for row in ordered:
                candidate = by_id[row["record_id"]]
                available = min(request.limits.entry_text_characters, request.limits.total_text_characters - used)
                if len(chosen) >= request.limits.references or available <= 0:
                    break
                chosen.append(candidate.record_id)
                used += min(len(candidate.text), available)
            proposed = tuple(chosen)
        else:
            proposed = baseline.selected_record_ids
        selected = baseline.selected_record_ids if self.mode == "shadow" else proposed
        report = {"schema": "richard_selection_diagnostic_v1", "policy": {"id": self.policy_id, "version": self.policy_version},
            "mode": self.mode, "status": status, "graph_identity_sha256": self.graph.identity_sha256,
            "graph_verification_status": self.graph.verification_status, "scope": request.scope, "event_id": request.event_id,
            "query_sha256": hashlib.sha256(request.query.encode()).hexdigest(),
            "context_query_sha256": hashlib.sha256(request.context_query.encode()).hexdigest(),
            "candidate_set_sha256": digest([c.identity() for c in request.candidates]),
            "query_cids": sorted(query_cids), "candidate_scores": scores,
            "baseline_selected_record_ids": list(baseline.selected_record_ids),
            "proposed_selected_record_ids": list(proposed), "selected_record_ids": list(selected),
            "unresolved": sorted(unresolved), "type_filtering": "disabled_unreviewed_semantics",
            "released_alias_equivalence": "not_semantically_verified",
            "pool_scope": "native_eligible_candidates_only", "semantic_support": "not_assessed"}
        report["diagnostic_sha256"] = digest(report)
        return report

    def select(self, request: SelectionRequest) -> SelectionDecision:
        report = self.evaluate(request)
        selected = tuple(report["selected_record_ids"])
        reason = ("richard_shadow_prefix" if self.mode == "shadow" else
                  "richard_no_graph_seed" if report["status"] == "no_graph_seed" else "richard_rank_limit")
        return SelectionDecision(selected, tuple(SelectionExclusion(c.record_id, reason)
            for c in request.candidates if c.record_id not in selected))
