"""One bounded chat selection, literal grounding, frozen ranking, native evidence.

No factory, credentials, new model stage, retrieval, delivery or numeric changes.
Book: Bojie Li, AI Agents in Depth v2.0 September23 2026: A4/A5 pp127-129
/PDF135-137, S7 pp111-113/PDF119-121, E1/E4 pp232-233,243-246/PDF240-241,
251-254. Contracts/provenance/evidence scope guidance, not certification.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import re
import time
import unicodedata

from .contracts import GovernedError
from .model_selection import ModelNPMILabelSelector, INSTRUCTIONS as OLD_INSTRUCTIONS
from .richard_v11_ranker import FrozenRanker
from .richard_chat_binding import NativeEvidenceBinder
from .selection_policy import digest


PLAN_SCHEMA = "richard_chat_plan_v1"
REDUCER = "ranked_cid_then_native_rank_v1"
INSTRUCTIONS = OLD_INSTRUCTIONS + """
Also return richard_plan. Only consider the supplied literal richard_catalog and
allowed_targets; do not invent IDs, aliases, chemistry, spans, or unseen sources.
Seed anchors must be exact character spans of the CURRENT query, preserving case.
Confidence high is a conservative intent judgment, not calibrated probability.
Use single with nonempty A and empty B for one grounded topic. Use dual only
when the current query explicitly asks for BOTH distinct grounded groups; never
manufacture B from context, candidate labels, synonyms, or the same concept.
The target must use a supplied literal target anchor; do not infer catalyst,
product, molecule, species or material from unstated intent. For general chat,
ambiguity or insufficient grounding, use mode fallback, confidence low, empty
groups and target/level, target_start=target_end=0. Still provide the ordinary
selected_record_ids required above. This is the only selection call.
"""


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    value = json.loads(text, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    def valid_strings(item):
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("invalid decoded Unicode") from None
        elif type(item) is dict:
            for key, child in item.items():
                valid_strings(key)
                valid_strings(child)
        elif type(item) is list:
            for child in item:
                valid_strings(child)
    # Reject decoded lone surrogates while the base selector still owns the
    # known-usage typed failure boundary, before any successful decision.
    valid_strings(value)
    return value


def _spans(text, phrase):
    if not phrase:
        return ()
    def continuation(char):
        category = unicodedata.category(char)
        return (category[0] in "LNM" or category in ("Cf", "Pc", "Sm", "Pd")
                or char in "+-\u2212\u207a\u207b\u208a\u208b^\u2022\u00b7/*")
    spans = []
    for match in re.finditer(re.escape(phrase), text):
        start, end = match.span()
        if ((start and continuation(text[start - 1]))
                or (end < len(text) and continuation(text[end]))):
            continue
        # Standalone enclosing punctuation is fine; embedding inside a
        # compact bracketed token does not establish this separate alias.
        left, right = start - 1, end
        while left >= 0 and text[left] in "([{":
            left -= 1
        while right < len(text) and text[right] in ")]}":
            right += 1
        if ((start and text[start - 1] in ")]}")
                or (end < len(text) and text[end] in "([{")
                or (left != start - 1 and left >= 0 and continuation(text[left]))
                or (right != end and right < len(text)
                    and (continuation(text[right]) or text[right] in "([{"))):
            continue
        # A dot inside a contiguous token (for example a hydrate) does not
        # authorize its prefix. Sentence punctuation remains a boundary.
        if ((end + 1 < len(text) and text[end] == "." and continuation(text[end + 1]))
                or (start > 1 and text[start - 1] == "." and continuation(text[start - 2]))):
            continue
        spans.append((start, end))
    return tuple(spans)


def _target_qualifiers(query):
    return tuple(re.finditer(r"(?i)(?<!\w)(fine|coarse)(?!\w)(?:\s*:\s*|\s+)", query))


def target_catalog(graph, query):
    qualifiers = _target_qualifiers(query)
    if qualifiers:
        rows = []
        for qualifier in qualifiers:
            level = qualifier.group(1).lower()
            matched = []
            for label in sorted({getattr(node, level) for node in graph.nodes}):
                phrases = {label, label.replace("_", " ")}
                if level == "coarse":
                    plural = label[:-1] + "ies" if label.endswith("y") else label if label.endswith("s") else label + "s"
                    phrases.add(plural)
                for phrase in sorted(phrases):
                    for start, end in _spans(query, phrase):
                        if start == qualifier.end():
                            full = query[qualifier.start():end]
                            if (qualifier.start(), end) in _spans(query, full):
                                matched.append({"target": label, "level": level, "phrase": full,
                                                "start": qualifier.start(), "end": end})
            # An unknown explicit qualifier cannot be replaced by a shorter
            # bare label anywhere else in this single-target query.
            if not matched:
                return []
            longest = max(row["end"] for row in matched)
            rows.extend(row for row in matched if row["end"] == longest)
        if len({(row["level"], row["target"]) for row in rows}) != 1:
            return []
        unique = {(row["target"], row["level"], row["start"], row["end"]): row for row in rows}
        return list(unique.values())[:32]
    rows = []
    for level in ("coarse", "fine"):
        for label in sorted({getattr(node, level) for node in graph.nodes}):
            phrases = {label, label.replace("_", " "), f"{level}: {label}", f"{level} {label}"}
            if level == "coarse":
                plural = label[:-1] + "ies" if label.endswith("y") else label if label.endswith("s") else label + "s"
                phrases.add(plural)
            for phrase in sorted(phrases):
                for start, end in _spans(query, phrase):
                    rows.append({"target": label, "level": level, "phrase": phrase,
                                 "start": start, "end": end})
    # A literal which names both a coarse and a fine label does not pick one.
    ambiguous = { (row["start"], row["end"]) for row in rows
                  if len({(other["level"], other["target"]) for other in rows
                          if (other["start"], other["end"]) == (row["start"], row["end"])}) > 1 }
    return [row for row in rows if (row["start"], row["end"]) not in ambiguous][:32]


def richard_schema(required, catalog, targets):
    seed = {"type": "object", "additionalProperties": False,
            "properties": {"term": {"type": "string", "enum": sorted({r["term"] for r in catalog}) or [""]},
                           "start": {"type": "integer", "minimum": 0},
                           "end": {"type": "integer", "minimum": 0}},
            "required": ["term", "start", "end"]}
    plan = {"type": "object", "additionalProperties": False,
            "properties": {"mode": {"type": "string", "enum": ["single", "dual", "fallback"]},
                "confidence": {"type": "string", "enum": ["high", "low"]},
                "seeds_A": {"type": "array", "maxItems": 4, "items": seed},
                "seeds_B": {"type": "array", "maxItems": 4, "items": seed},
                "target": {"type": "string", "enum": [""] + sorted({r["target"] for r in targets})},
                "level": {"type": "string", "enum": ["", "fine", "coarse"]},
                "target_start": {"type": "integer", "minimum": 0},
                "target_end": {"type": "integer", "minimum": 0}},
            "required": ["mode", "confidence", "seeds_A", "seeds_B", "target", "level", "target_start", "target_end"]}
    schema = ModelNPMILabelSelector._schema(required)
    schema["properties"]["richard_plan"] = plan
    schema["required"].append("richard_plan")
    return schema


def validate_plan_shape(plan, query, catalog, targets):
    keys = {"mode", "confidence", "seeds_A", "seeds_B", "target", "level", "target_start", "target_end"}
    if type(plan) is not dict or set(plan) != keys:
        return "invalid_plan"
    if plan["mode"] not in ("single", "dual", "fallback") or plan["confidence"] not in ("high", "low"):
        return "invalid_plan"
    if any(type(plan[k]) is not list or len(plan[k]) > 4 for k in ("seeds_A", "seeds_B")):
        return "invalid_plan"
    if any(type(plan[k]) is not int or plan[k] < 0 for k in ("target_start", "target_end")):
        return "invalid_plan"
    if type(plan["target"]) is not str or type(plan["level"]) is not str:
        return "invalid_plan"
    allowed = {(row["term"], row["start"], row["end"]) for row in catalog}
    for seed in plan["seeds_A"] + plan["seeds_B"]:
        if (type(seed) is not dict or set(seed) != {"term", "start", "end"}
                or type(seed["term"]) is not str or type(seed["start"]) is not int
                or type(seed["end"]) is not int
                or (seed["term"], seed["start"], seed["end"]) not in allowed
                or query[seed["start"]:seed["end"]] != seed["term"]):
            return "invalid_seed_anchor"
    if plan["mode"] == "fallback":
        return "model_declined" if not plan["seeds_A"] and not plan["seeds_B"] and not plan["target"] and not plan["level"] and plan["target_start"] == plan["target_end"] == 0 else "invalid_plan"
    if plan["confidence"] != "high":
        return "low_confidence"
    # Control-shaped text is not a grounded relation request. These literal
    # denials do not replace the bounded model's intent judgment.
    if re.search(r"(?i)seeds_[AB]|richard_plan|(?:target|level)\s*=|ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", query):
        return "untrusted_control_text"
    if re.search(r"(?i)(?<!\w)(?:not|never|avoid|exclude|without|neither)(?!\w)|don['’]t|不要|不是|而非|不需要", query):
        return "ambiguous_negated_intent"
    if not plan["seeds_A"] or (plan["mode"] == "single" and plan["seeds_B"]) or (plan["mode"] == "dual" and not plan["seeds_B"]):
        return "invalid_seed_groups"
    if plan["mode"] == "dual" and not re.search(r"(?i)(?<!\w)both(?!\w)|同时|两者", query):
        return "dual_intent_not_explicit"
    if plan["mode"] == "dual" and re.search(r"['\"‘’“”]", query):
        return "ambiguous_dual_intent"
    key = (plan["target"], plan["level"], plan["target_start"], plan["target_end"])
    qualifiers = _target_qualifiers(query)
    if qualifiers and not any(plan["target_start"] == row.start()
            and plan["target_end"] > row.end() and plan["level"] == row.group(1).lower()
            for row in qualifiers):
        return "invalid_target_anchor"
    if key not in {(r["target"], r["level"], r["start"], r["end"]) for r in targets}:
        return "invalid_target_anchor"
    return None


class _Capture:
    def __init__(self, adapter, before_call):
        self.adapter, self.result, self.wait_seconds = adapter, None, 0.0
        self.before_call = before_call

    async def call(self, *args, **kwargs):
        self.before_call()
        started = time.monotonic()
        try:
            self.result = await self.adapter.call(*args, **kwargs)
            return self.result
        finally:
            self.wait_seconds += time.monotonic() - started


class RichardChatSelector(ModelNPMILabelSelector):
    policy_id = "richard_chat_v11"
    policy_version = "v2"

    def __init__(self, adapter, scratch, budget, *, ranker, binder, graph_identity, algorithm):
        super().__init__(adapter, scratch, budget)
        if (type(ranker) is not FrozenRanker or type(binder) is not NativeEvidenceBinder
                or binder.graph is not ranker.graph or graph_identity != ranker.graph.identity_sha256
                or algorithm not in ("alpha", "beta")):
            raise GovernedError("invalid_richard_chat_configuration", remote_usage_unknown=False)
        self.ranker, self.binder, self.graph_identity, self.algorithm = ranker, binder, graph_identity, algorithm
        self._busy, self._pending = False, None
        self.last_local_seconds = 0.0
        self._local_deadline = self._turn_deadline = None
        self._capture = None

    def set_deadlines(self, *, local_deadline, turn_deadline):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or value <= 0 for value in (local_deadline, turn_deadline)):
            raise GovernedError("invalid_richard_deadline", remote_usage_unknown=False)
        self._local_deadline, self._turn_deadline = local_deadline, turn_deadline

    def _check_deadline(self):
        now = time.monotonic()
        wait = self._capture.wait_seconds if self._capture else 0.0
        code = ("turn_timeout" if self._turn_deadline is not None and now >= self._turn_deadline
                else "local_memory_timeout" if self._local_deadline is not None
                and now >= self._local_deadline + wait else None)
        if code:
            result = self._capture.result if self._capture else None
            raise GovernedError(code, remote_usage_unknown=False,
                known_usage={"input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
                if result else None)

    def _catalog(self, request, visible):
        ids = {c.record_id for c in request.candidates[:visible]}
        cids = {cid for b in self._snapshot["bindings"] if b["native_record_id"] in ids for cid in b["cids"]}
        rows = []
        for term, cid in sorted(self.ranker.graph.name_to_cid.items()):
            if cid in cids:
                rows.extend({"term": term, "start": start, "end": end}
                            for start, end in _spans(request.query, term))
        return rows[:32]

    def _schema(self, required):
        return super()._schema(required) if self._off else richard_schema(required, self._catalog(self._request, len(self._request.candidates)), self._targets)

    def _messages(self, request, rows, required, visible):
        messages = super()._messages(request, rows, required, visible)
        self._visible_catalog = self._catalog(request, visible) if not self._off else []
        if not self._off:
            payload = json.loads(messages[0]["content"])
            payload.update(richard_catalog=self._visible_catalog, allowed_targets=self._targets,
                           richard_algorithm=self.algorithm, graph_identity=self.graph_identity,
                           richard_binding_sha256=digest(self._snapshot),
                           richard_plan_schema=PLAN_SCHEMA)
            messages[0]["content"] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return messages

    def _audit_fields(self, request, messages):
        # Read the final planned payload, not a trial prefix from binary search.
        # CIDs stay in the local receipt and cannot affect input reservations,
        # exact provider counting, schema, or model-visible candidate admission.
        catalog = json.loads(messages[0]["content"]).get("richard_catalog", [])
        self._local_input_audit = {
            "schema": "richard_chat_local_cids_v1",
            "graph_identity": self.graph_identity,
            "binding_snapshot_sha256": digest(self._snapshot),
            "catalog_cids": [{**row, "cid": self.ranker.graph.name_to_cid[row["term"]]}
                             for row in catalog],
        }
        return self._local_input_audit

    def _selected(self, text, visible_ids, required):
        data = strict_json(text)
        if self._off:
            self._fallback = super()._selected(text, visible_ids, required)
            return self._fallback
        if type(data) is not dict or set(data) != {"selected_record_ids", "richard_plan"}:
            raise ValueError("invalid selection object")
        self._fallback = super()._selected(json.dumps({"selected_record_ids": data["selected_record_ids"]}), visible_ids, required)
        self._plan = data["richard_plan"]
        reason = validate_plan_shape(self._plan, self._request.query, self._visible_catalog, self._targets)
        if reason:
            self._reason = reason
            return self._fallback
        a = self.ranker.resolve_seeds(self.ranker.graph.name_to_cid[s["term"]] for s in self._plan["seeds_A"])
        b = self.ranker.resolve_seeds(self.ranker.graph.name_to_cid[s["term"]] for s in self._plan["seeds_B"])
        self._seed_a, self._seed_b = list(a), list(b)
        if self._plan["mode"] == "dual" and set(a) & set(b):
            self._reason = "ambiguous_seed_overlap"
            return self._fallback
        self._variant = self.algorithm + ("_meet" if self._plan["mode"] == "dual" else "_nomeet")
        self._check_deadline()
        self._called = True
        ranked = self.ranker.rank(a, b, target=self._plan["target"], level=self._plan["level"], variant=self._variant)
        self._check_deadline()
        self._ranked = [{"cid": row.cid, "score": row.score} for row in ranked]
        if len(ranked) < 10:
            self._reason = "short_target_pool"
            return self._fallback
        candidates = {c.record_id: c for c in self._request.candidates if c.record_id in visible_ids}
        result = []
        for row in ranked:
            native = [binding["native_record_id"] for binding in self._snapshot["bindings"]
                      if binding["native_record_id"] in candidates and row.cid in binding["cids"]]
            for rid in sorted(native, key=lambda value: (candidates[value].rank, value)):
                if rid not in result:
                    result.append(rid)
        self._native_ranked = result
        if len(result) < required:
            self._reason = "short_bound_evidence"
            return self._fallback
        self._applied, self._reason = True, "ranked_native_evidence"
        return tuple(result[:required])

    async def choose(self, request, trace_id, *, adapter_override=None):
        if self._busy:
            raise GovernedError("richard_chat_busy", remote_usage_unknown=False)
        self._busy, self._pending = True, None
        started, capture = time.monotonic(), None
        try:
            self._capture = None
            self._check_deadline()
            self._request = request
            self._snapshot = self.binder.snapshot(request)
            self._check_deadline()
            self._targets = target_catalog(self.ranker.graph, request.query)
            self._off = "no_grounded_vocabulary" if not self._catalog(request, len(request.candidates)) else "no_explicit_target" if not self._targets else None
            self.instructions = OLD_INSTRUCTIONS if self._off else INSTRUCTIONS
            self._called, self._applied, self._variant = False, False, None
            self._ranked, self._native_ranked, self._fallback, self._plan = [], [], (), None
            self._seed_a, self._seed_b = [], []
            self._reason = self._off or "not_entered"
            self._check_deadline()
            capture = _Capture(adapter_override if adapter_override is not None else self.adapter,
                               self._check_deadline)
            self._capture = capture
            decision = await super().choose(request, trace_id, adapter_override=capture)
            # The planner commitment includes query/context, policy and every
            # limit in addition to the binder's narrower source identity.
            commitment = (digest(asdict(request)), digest(asdict(decision)),
                          digest(self._snapshot), self._applied)
            # Bind to the exact input/response already retained by base choose.
            rows = [self._row(c, request.retrieval_policy) for c in request.candidates]
            visible_ids = {c.record_id for c in request.candidates if not any(e.record_id == c.record_id and e.reason == "input_budget" for e in decision.exclusions)}
            visible = len(visible_ids)
            messages = self._messages(request, rows, min(request.limits.references, len(rows)), visible)
            from .adapter import reservation
            inventory = [c.identity() for c in request.candidates]
            ids = [c.record_id for c in request.candidates]
            binding = {"policy": {"id": self.policy_id, "version": self.policy_version}, "scope": request.scope, "event_id": request.event_id,
                "query_sha256": hashlib.sha256(request.query.encode()).hexdigest(), "context_query_sha256": hashlib.sha256(request.context_query.encode()).hexdigest(),
                "candidate_count": len(rows), "candidate_set_sha256": digest(inventory), "seen_record_ids": ids[:visible],
                "seen_candidates_sha256": digest(inventory[:visible]), "omitted_record_ids": ids[visible:], "omitted_candidates_sha256": digest(inventory[visible:]),
                "messages_sha256": digest(messages), "instructions_sha256": hashlib.sha256(self.instructions.encode()).hexdigest(),
                "schema_sha256": digest(self._schema(min(request.limits.references, len(rows)))),
                "local_input_audit_sha256": digest(self._local_input_audit)}
            receipt = {"schema": "richard_chat_selection_v2", "policy": binding["policy"], "input_sha256": digest(binding),
                "local_input_audit_sha256": binding["local_input_audit_sha256"],
                "response_id": capture.result.response_id if capture.result else None,
                "raw_model_result_sha256": digest(asdict(capture.result)) if capture.result else None,
                "fallback_record_ids": list(self._fallback), "final_record_ids": list(decision.selected_record_ids),
                "exclusions": [asdict(e) for e in decision.exclusions], "ranker_enabled": True, "ranker_called": self._called,
                "ranker_applied": self._applied, "fallback_used": not self._applied, "reason": self._reason,
                "graph_identity": self.graph_identity, "algorithm": self.algorithm, "variant": self._variant,
                "resolved_seeds": {"namespace": "concept_cid", "A": self._seed_a, "B": self._seed_b,
                    "normalization": "exact_release_name_to_cid_then_sorted_dedup"},
                "parsed_plan_sha256": digest(self._plan), "catalog_sha256": digest(self._visible_catalog),
                "binding_snapshot": self._snapshot, "binding_snapshot_sha256": digest(self._snapshot),
                "ranked_concepts": self._ranked, "ranked_concepts_sha256": digest(self._ranked),
                "native_ranked_record_ids": self._native_ranked, "reducer": REDUCER}
            self.scratch.write("richard_chat_selection", trace_id=trace_id, **receipt)
            self._check_deadline()
            self._pending = commitment
            return decision
        finally:
            self.last_local_seconds = max(0.0, time.monotonic() - started - (capture.wait_seconds if capture else 0.0))
            self._busy = False

    def validate_commit(self, request, decision):
        self._check_deadline()
        pending, self._pending = self._pending, None
        if pending is None or pending[:2] != (digest(asdict(request)), digest(asdict(decision))):
            raise GovernedError("richard_selection_not_bound", remote_usage_unknown=False)
        if pending[3]:
            current = digest(self.binder.snapshot(request))
            self._check_deadline()
            if current != pending[2]:
                raise GovernedError("richard_binding_changed", remote_usage_unknown=False)
