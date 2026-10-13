"""tools.py — the three agent tools (view / prune / answer) with governance. No LLM calls in here.
v0.1.2: a budget rejection locks the session to answer-only; the log record is persisted BEFORE any state change, so a
failing log sink leaves the session unchanged.
v0.1.1 (Kate's boundary review): every call is atomic (state changes only after the whole call succeeded), every rejected
call is logged, the budget is a hard cap on view/prune output (an output that would exceed it is withheld), answer output
is metered, and malformed citations are flagged instead of raising."""
import json, time
from . import __version__

MAX_PRUNE_ROUNDS = 3
DEFAULT_BUDGET_TOKENS = 12000
SNIPPET_CHARS = 300
MAX_DETAILS = 3   # candidates per call that get records + snippets; all others get compact paths only
VALID_KINDS = ("evidence", "logic")

class ToolError(ValueError): pass
class BudgetExceeded(ToolError): pass

def est_tokens(obj):
    """Estimate of TOOL OUTPUT size only (chars / 3.5). End-to-end model usage must be metered by the runtime."""
    return int(len(json.dumps(obj, ensure_ascii=False)) / 3.5) + 1

def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)

class Session:
    """One question = one session = one draft. The draft is only the set of removed concept ids; the Graph is never touched.
    text_provider(record_id:int) -> str | None is optional; errors inside it are caught and reported per snippet."""
    def __init__(self, graph, question_id, seeds_A, seeds_B, target, level="coarse", budget_tokens=DEFAULT_BUDGET_TOKENS,
                 text_provider=None, log_path=None):
        self.g = graph; self.qid = question_id; self.target, self.level = target, level
        self.log_path = log_path; self.log = []
        for n in list(seeds_A) + list(seeds_B):
            if not _is_int(n) or n not in graph.m2c:
                raise ToolError(f"seed {n!r} is not an original node_id with a member_to_cid mapping")
        self.A = sorted({graph.m2c[n] for n in seeds_A}); self.B = sorted({graph.m2c[n] for n in seeds_B})
        if not self.A: raise ToolError("seeds_A is empty")
        self.removed = set(); self.rounds = 0; self.tokens = 0; self.budget = budget_tokens
        self.text_provider = text_provider; self.closed = False; self.current = []; self.budget_locked = False
        self._log("start", {"version": __version__, "seeds_A_cid": self.A, "seeds_B_cid": self.B, "target": target,
                            "level": level, "budget_tokens": budget_tokens, "graph_hashes": graph.hashes})

    # ---------------------------------------------------------------- logging
    def _record(self, action, payload):
        return {"t": round(time.time(), 3), "qid": self.qid, "action": action, **payload}

    def _persist(self, rec):
        """Write one record to the log sink. Raises on failure; callers persist BEFORE changing any state."""
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _log(self, action, payload):
        rec = self._record(action, payload); self._persist(rec); self.log.append(rec)

    def _commit(self, action, payload, apply):
        """Persist the record first; only if that succeeds apply the state change and keep the record in memory."""
        rec = self._record(action, payload)
        try:
            self._persist(rec)
        except Exception as e:
            self._reject_nolock(action, ToolError(f"log sink failed, call not applied: {type(e).__name__}: {e}"), {"note": "persist failed"})
            raise ToolError(f"{action} not applied: log sink failed ({type(e).__name__}: {e})") from e
        apply(); self.log.append(rec)

    def _reject_nolock(self, tool, err, request):
        rec = self._record("rejected", {"tool": tool, "error": f"{type(err).__name__}: {err}", "request": request,
                                        "round": self.rounds, "cumulative": self.tokens, "budget_locked": self.budget_locked})
        try: self._persist(rec)
        except Exception as e: rec["persist_error"] = f"{type(e).__name__}: {e}"   # keep the audit in memory at least
        self.log.append(rec)

    def _reject(self, tool, err, request):
        if isinstance(err, BudgetExceeded): self.budget_locked = True   # governance: after a budget rejection only answer() is allowed
        self._reject_nolock(tool, err, request)

    # ---------------------------------------------------------------- pure helpers (no state change)
    def _snippet(self, r):
        if not self.text_provider: return None, None
        try:
            t = self.text_provider(r)
            return ((t or "")[:SNIPPET_CHARS] if isinstance(t, str) or t is None else str(t)[:SNIPPET_CHARS]), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _evidence(self, cid, detailed, removed):
        ev = {}
        for side, seeds in (("from_A", self.A), ("from_B", self.B)):
            if not seeds: continue
            path = self.g.best_path(seeds, cid, removed=removed)
            if not path: ev[side] = None; continue
            hops = []
            for a, b in zip(path, path[1:]):
                hop = {"from_cid": a, "to_cid": b, "npmi": round(self.g.nbr[a][b], 4)}
                if not detailed: hops.append(hop); continue
                recs = []
                for r in self.g.edge_records(a, b):
                    snip, err = self._snippet(r)
                    rec = {"record_id": r, "source_id": self.g.rloc.get(r, (None, []))[0], "pages": self.g.rloc.get(r, (None, []))[1], "snippet": snip}
                    if err: rec["snippet_error"] = err
                    recs.append(rec)
                hops.append({**hop, "from": self.g.nodes[a]["name"], "to": self.g.nodes[b]["name"], "records": recs})
            ev[side] = {"path": [self.g.nodes[n]["name"] for n in path], "hops": hops}
        return ev

    def _view_payload(self, removed, details, round_no):
        if details is None: details = []
        if not isinstance(details, (list, tuple)): raise ToolError("details must be a list of concept ids")
        details = list(dict.fromkeys(details))
        if len(details) > MAX_DETAILS: raise ToolError(f"at most {MAX_DETAILS} candidates per call can be expanded with records")
        top, scores, pool = self.g.rank(self.A, self.B, self.target, self.level, removed=frozenset(removed))
        bad = [d for d in details if not _is_int(d) or d not in top]
        if bad: raise ToolError(f"details must be concept ids in the resulting top-10: {bad!r}")
        out = {"question_id": self.qid, "round": round_no, "pool_size": pool,
               "candidates": [{"rank": i + 1, "cid": c, "name": self.g.nodes[c]["name"], "type": [self.g.nodes[c]["coarse"], self.g.nodes[c]["fine"]],
                               "score": round(scores[c], 8), "evidence": self._evidence(c, c in details, frozenset(removed))} for i, c in enumerate(top)]}
        return out, top

    def _check_open(self, answering=False):
        if self.closed: raise ToolError("session already answered/closed")
        if self.budget_locked and not answering: raise BudgetExceeded("budget exhausted earlier in this session: only answer() is allowed")

    def _check_budget(self, out):
        t = est_tokens(out) + 12  # + room for the budget field itself
        if self.tokens + t > self.budget:
            raise BudgetExceeded(f"output of ~{t} tokens would exceed the budget ({self.tokens}/{self.budget} used): only answer() is allowed")
        return t

    # ---------------------------------------------------------------- tools
    def view(self, details=None):
        """details: up to MAX_DETAILS candidate cids (in the top-10) to expand with records/pages/snippets."""
        req = {"details": details}
        try:
            self._check_open()
            out, top = self._view_payload(self.removed, details, self.rounds)
            t = self._check_budget(out)
        except ToolError as e:
            self._reject("view", e, req); raise
        except Exception as e:
            self._reject("view", e, req); raise ToolError(f"view failed: {type(e).__name__}: {e}") from e
        def apply():
            self.current = top; self.tokens += t
        self._commit("view", {"round": self.rounds, "top10": top, "tokens": t, "cumulative": self.tokens + t}, apply)
        out["budget"] = {"used": self.tokens, "limit": self.budget}
        return out

    def prune(self, removals, details=None):
        req = {"removals": removals, "details": details}
        try:
            self._check_open()
            if self.rounds >= MAX_PRUNE_ROUNDS: raise ToolError(f"max {MAX_PRUNE_ROUNDS} prune rounds reached: only answer() is allowed")
            if not isinstance(removals, (list, tuple)) or not removals: raise ToolError("removals must be a non-empty list")
            seeds = set(self.A) | set(self.B); accepted = []
            for r in removals:
                if not isinstance(r, dict): raise ToolError(f"each removal must be an object: {r!r}")
                cid, reason, kind = r.get("node_id"), r.get("reason"), r.get("kind")
                if not _is_int(cid) or cid not in self.g.nodes: raise ToolError(f"unknown concept id {cid!r}")
                if cid in seeds: raise ToolError(f"{cid} is a seed and cannot be removed")
                if cid in self.removed: raise ToolError(f"{cid} is already removed")
                if not isinstance(reason, str) or not reason.strip(): raise ToolError(f"removal of {cid} has no reason")
                if kind not in VALID_KINDS: raise ToolError(f"removal of {cid}: kind must be one of {VALID_KINDS}")
                cited = r.get("cited_record_ids") or []
                if not isinstance(cited, (list, tuple)): raise ToolError(f"removal of {cid}: cited_record_ids must be a list")
                if kind == "evidence":
                    if not cited: raise ToolError(f"removal of {cid}: kind=evidence requires cited_record_ids")
                    bad = [x for x in cited if not _is_int(x) or x not in self.g.rloc]
                    if bad: raise ToolError(f"removal of {cid}: cited records do not exist: {bad!r}")
                accepted.append({"node_id": cid, "name": self.g.nodes[cid]["name"], "kind": kind, "reason": reason.strip(), "cited_record_ids": list(cited)})
            tentative = set(self.removed) | {a["node_id"] for a in accepted}
            out, top = self._view_payload(tentative, details, self.rounds + 1)
            t = self._check_budget(out)
        except ToolError as e:
            self._reject("prune", e, req); raise
        except Exception as e:
            self._reject("prune", e, req); raise ToolError(f"prune failed: {type(e).__name__}: {e}") from e
        before = list(self.current)                      # commit only after the whole call succeeded AND was logged
        def apply():
            self.removed = tentative; self.rounds += 1; self.current = top; self.tokens += t
        self._commit("prune", {"round": self.rounds + 1, "removed": accepted, "top10_before": before, "top10_after": top,
                               "tokens": t, "cumulative": self.tokens + t}, apply)
        out["budget"] = {"used": self.tokens, "limit": self.budget}
        return out

    def answer(self, choice, citations=None):
        """Always allowed while the session is open (also after the budget is spent); its output is metered."""
        req = {"choice": choice, "citations": citations}
        try:
            self._check_open(answering=True)
            current = self.current or self.g.rank(self.A, self.B, self.target, self.level, removed=frozenset(self.removed))[0]
            if choice != "none" and (not _is_int(choice) or choice not in current):
                raise ToolError("choice must be one of the current top-10 candidates or 'none'")
            if citations is not None and not isinstance(citations, (list, tuple)): raise ToolError("citations must be a list")
        except ToolError as e:
            self._reject("answer", e, req); raise
        checked = [self.verify(c) for c in (citations or [])]
        result = {"question_id": self.qid, "choice": choice,
                  "choice_name": self.g.nodes[choice]["name"] if choice != "none" else "none",
                  "citations": checked, "unverified_citations": sum(1 for c in checked if not c["verified"]),
                  "rounds": self.rounds, "removed": sorted(self.removed)}
        t = est_tokens(result); total = self.tokens + t
        result["tokens"] = total; result["over_budget"] = total > self.budget
        def apply():
            self.tokens = total; self.closed = True; self.removed = set()   # draft discarded
        self._commit("answer", {**result, "answer_tokens": t}, apply)
        return result

    # ---------------------------------------------------------------- verifier (never raises)
    def verify(self, c):
        """c = {"edge": [cid_a, cid_b], "record_id": r}. Verified only if the edge exists in the BASE graph and the record
        contains both endpoint concepts. Any malformed input is returned with verified=False and a problem string."""
        if not isinstance(c, dict): return {"citation": repr(c), "verified": False, "problem": "malformed citation (not an object)"}
        edge, r = c.get("edge"), c.get("record_id")
        out = {"edge": edge if isinstance(edge, (list, tuple)) else repr(edge), "record_id": r if _is_int(r) else repr(r)}
        if not isinstance(edge, (list, tuple)) or len(edge) != 2 or not all(_is_int(x) for x in edge):
            return {**out, "verified": False, "problem": "malformed edge (need two integer concept ids)"}
        a, b = edge
        if (a, b) not in self.g.edge: return {**out, "verified": False, "problem": "edge not in graph"}
        if not _is_int(r): return {**out, "verified": False, "problem": "missing or non-integer record id"}
        if r not in self.g.crec[a] or r not in self.g.crec[b]:
            return {**out, "verified": False, "problem": "record does not contain both concepts"}
        return {**out, "verified": True}
