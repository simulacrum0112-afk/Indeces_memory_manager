"""Trusted, synchronous selection of existing ranked evidence records.

This interface does not execute model calls or merge/rewrite evidence.  The
caller supplies immutable candidates before the reference cutoff and verifies
the returned partition.  It is an injection boundary, not an I/O sandbox or an
operating-system deadline: Runtime retains its cooperative local time checks.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from typing import Protocol

from .contracts import GovernedError


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SelectionLimits:
    references: int = 3
    total_text_characters: int = 400
    entry_text_characters: int = 110


@dataclass(frozen=True)
class SelectionCandidate:
    record_id: int
    scope: str
    source_id: str
    fingerprint: str
    rank: int
    text: str
    quote: str
    marks: tuple[str, ...]
    direct_marks: tuple[str, ...]
    expanded_marks: tuple[str, ...]
    direct_match_count: int
    effective_score: float
    static_score: float
    dynamic_score: float
    ranking_score: float

    def identity(self) -> dict:
        return {
            "record_id": self.record_id, "scope": self.scope,
            "source_id": self.source_id, "fingerprint": self.fingerprint,
            "rank": self.rank, "marks": list(self.marks),
            "direct_marks": list(self.direct_marks),
            "expanded_marks": list(self.expanded_marks),
            "direct_match_count": self.direct_match_count,
            "effective_score": self.effective_score, "static_score": self.static_score,
            "dynamic_score": self.dynamic_score, "ranking_score": self.ranking_score,
            "text_characters": len(self.text), "quote_characters": len(self.quote),
            "text_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
            "quote_sha256": hashlib.sha256(self.quote.encode()).hexdigest(),
        }


@dataclass(frozen=True)
class SelectionRequest:
    scope: str
    event_id: str
    query: str
    context_query: str
    ranking_mode: str
    retrieval_policy: str
    candidates: tuple[SelectionCandidate, ...]
    limits: SelectionLimits


@dataclass(frozen=True)
class SelectionExclusion:
    record_id: int
    reason: str


@dataclass(frozen=True)
class SelectionDecision:
    selected_record_ids: tuple[int, ...]
    exclusions: tuple[SelectionExclusion, ...]


class SelectionPolicy(Protocol):
    policy_id: str
    policy_version: str

    def select(self, request: SelectionRequest) -> SelectionDecision: ...


@dataclass(frozen=True)
class RankedTopThreeSelector:
    """An explicit contract for the existing ranked-reference choice."""

    policy_id: str = "ranked_top_three"
    policy_version: str = "v1"

    def select(self, request: SelectionRequest) -> SelectionDecision:
        selected, exclusions, used = [], [], 0
        for candidate in request.candidates:
            available = min(request.limits.entry_text_characters,
                            request.limits.total_text_characters - used)
            if len(selected) >= request.limits.references:
                exclusions.append(SelectionExclusion(candidate.record_id, "reference_limit"))
            elif available <= 0:
                exclusions.append(SelectionExclusion(candidate.record_id, "preview_budget"))
            else:
                selected.append(candidate.record_id)
                used += min(len(candidate.text), available)
        return SelectionDecision(tuple(selected), tuple(exclusions))


def policy_identity(selector: SelectionPolicy) -> dict:
    """Reject arbitrary policy metadata before it can enter durable audits."""
    try:
        policy_id, version = selector.policy_id, selector.policy_version
        select = selector.select
    except Exception:
        raise GovernedError("invalid_selection_decision") from None
    if (not isinstance(policy_id, str) or not _IDENTITY.fullmatch(policy_id)
            or not isinstance(version, str) or not _IDENTITY.fullmatch(version)
            or not callable(select) or inspect.iscoroutinefunction(select)):
        raise GovernedError("invalid_selection_decision")
    return {"id": policy_id, "version": version}


def checked_decision(selector: SelectionPolicy, request: SelectionRequest,
                     policy: dict) -> SelectionDecision:
    try:
        decision = selector.select(request)
    except Exception:
        # No strategy exception text is propagated to logs or failure notices.
        raise GovernedError("selection_policy_failed") from None
    if inspect.isawaitable(decision):
        if inspect.iscoroutine(decision):
            decision.close()
        raise GovernedError("invalid_selection_decision")
    if policy_identity(selector) != policy or type(decision) is not SelectionDecision:
        raise GovernedError("invalid_selection_decision")
    selected, exclusions = decision.selected_record_ids, decision.exclusions
    if (type(selected) is not tuple or type(exclusions) is not tuple
            or len(selected) > request.limits.references
            or any(type(record_id) is not int for record_id in selected)
            or len(set(selected)) != len(selected)
            or any(type(item) is not SelectionExclusion for item in exclusions)):
        raise GovernedError("invalid_selection_decision")
    excluded_ids = []
    for item in exclusions:
        if (type(item.record_id) is not int or not isinstance(item.reason, str)
                or not _REASON.fullmatch(item.reason)):
            raise GovernedError("invalid_selection_decision")
        excluded_ids.append(item.record_id)
    known = {candidate.record_id for candidate in request.candidates}
    if (len(set(excluded_ids)) != len(excluded_ids)
            or set(selected) & set(excluded_ids)
            or set(selected) | set(excluded_ids) != known):
        raise GovernedError("invalid_selection_decision")
    return decision
