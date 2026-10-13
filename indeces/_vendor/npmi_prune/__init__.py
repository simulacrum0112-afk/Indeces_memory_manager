"""npmi_prune — subtraction-only agent tools over a frozen NPMI concept graph (prototype for the Indeces runtime).

Behaviour approved by Richard (2026-10-12); v0.1.2 = v0.1.1 + budget lock + log-first commits (v0.1.1 = v0.1.0 + atomic calls, audited rejections, hard cap):
  1. Exactly three tools: view (ranking + evidence), prune (delete nodes, re-rank), answer. The model cannot add seeds.
  2. At most 3 prune rounds per question; per-question budget of tool OUTPUT (default 12000 estimated tokens); after the
     budget would be exceeded the output is withheld and only answer() is accepted (answer output is metered).
  3. Every removal needs a reason and a kind: "evidence" (must cite record ids that exist) or "logic". Seeds cannot be
     removed.
  4. answer() must choose a current candidate or "none"; every citation is checked by a deterministic verifier and
     failures are flagged, never silently accepted.
  5. The base graph is never modified (hash-pinned); each question has its own draft (a set of removed nodes) that is
     discarded at answer(); every call is logged.
Ranking = frozen v1.1 spec (alpha personalised PageRank, restart 0.5, 40 iterations, row-normalised positive NPMI,
meet = sqrt(rA * rB), hard type filter, seeds excluded, ties by concept id), computed exactly as reference_rank_v11.py.
"""
__version__ = "0.1.2"
from .core import Graph
from .tools import Session, BudgetExceeded, ToolError
