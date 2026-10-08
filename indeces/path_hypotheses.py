"""Automatic bounded graph paths with same-round frozen, explicit claim binding.

This is an optional offline diagnostic, not a theorem prover or relation composer.
Only literal typed_facts_v1 JSON lines in frozen selected quotes supply relations.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import time


@dataclass(frozen=True)
class PathHypothesesConfig:
    enabled: bool = False
    max_nodes: int = 256
    max_arcs: int = 1024
    max_depth: int = 4
    max_frontier: int = 256
    max_candidates: int = 64
    max_expansions: int = 4096
    max_seconds: float = 0.25
    max_rounds: int = 8
    max_actions: int = 8
    max_facts_per_material: int = 16
    max_quote_chars: int = 16384
    max_source_chars: int = 262144
    max_serialized_bytes: int = 1048576
    max_support_ids: int = 128
    max_bindings: int = 1024

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("path hypotheses enabled must be a bool")
        try:
            valid_seconds = type(self.max_seconds) in (int, float) and math.isfinite(self.max_seconds) and self.max_seconds > 0
        except OverflowError:
            valid_seconds = False
        if not valid_seconds:
            raise ValueError("invalid path time budget")
        for name in self.__dataclass_fields__:
            if name not in ("enabled", "max_seconds") and (type(getattr(self, name)) is not int or getattr(self, name) <= 0):
                raise ValueError(f"invalid path limit {name}")


class _Stop(Exception):
    pass


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _text(value, limit=120):
    return type(value) is str and 0 < len(value) <= limit


def _sha(value):
    return type(value) is str and len(value) == 64 and all(x in "0123456789abcdef" for x in value)


def _hash(value, cfg, check):
    # Cheap preflight before JSON can allocate an encoded giant string. Stream
    # child iterators rather than materializing a whole traversal frontier.
    stack, active, count, lower_bytes = [(iter(((value, 0, None),)), None)], set(), 0, 0
    def children(node, depth, field):
        if type(node) is dict:
            for key, item in node.items():
                _require(type(key) is str, "non_string_json_key")
                yield key, depth + 1, None
                yield item, depth + 1, key
        else:
            for item in node:
                yield item, depth + 1, field
    structural_limit = max(1, cfg.max_serialized_bytes // 8)
    while stack:
        check()
        try:
            node, depth, field = next(stack[-1][0])
        except StopIteration:
            _, container_id = stack.pop()
            if container_id is not None:
                active.remove(container_id)
            continue
        count += 1
        if count > structural_limit or depth > 32:
            raise _Stop("json_structure")
        if type(node) is str:
            bound = cfg.max_source_chars if field == "raw_text" else cfg.max_quote_chars if field in ("quote", "stored_text") else cfg.max_serialized_bytes
            if len(node) > bound:
                raise _Stop("body_characters" if field in ("raw_text", "quote", "stored_text") else "serialized_bytes")
            # Account for JSON escaping and UTF-8 before encoder allocation.
            lower_bytes += 2
            for offset, character in enumerate(node):
                if offset % 256 == 0:
                    check()
                code = ord(character)
                _require(not 0xD800 <= code <= 0xDFFF, "invalid_unicode_scalar")
                lower_bytes += (6 if code < 32 else 2 if character in ('"', '\\') else
                                1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4)
                if lower_bytes > cfg.max_serialized_bytes:
                    raise _Stop("serialized_bytes")
        elif type(node) is dict or type(node) in (list, tuple):
            if len(node) > structural_limit:
                raise _Stop("json_structure")
            if id(node) in active:
                raise ValueError("invalid_json_structure")
            active.add(id(node))
            # Frames also own the container id for cycle detection.
            stack.append((iter(children(node, depth, field)), id(node)))
        elif type(node) is int:
            if node.bit_length() > cfg.max_serialized_bytes * 4:
                raise _Stop("serialized_bytes")
            lower_bytes += 1
        elif node is None or type(node) in (bool, float):
            lower_bytes += 1
        else:
            raise ValueError("non_json_input")
        if lower_bytes > cfg.max_serialized_bytes:
            raise _Stop("serialized_bytes")
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result, total = hashlib.sha256(), 0
    for chunk in encoder.iterencode(value):
        check()
        if len(chunk) > cfg.max_serialized_bytes:
            raise _Stop("serialized_bytes")
        data = chunk.encode("utf-8")
        total += len(data)
        if total > cfg.max_serialized_bytes:
            raise _Stop("serialized_bytes")
        result.update(data)
    return result.hexdigest()


def _body_hash(value, cfg, check, limit):
    _require(type(value) is str, "invalid_body")
    if len(value) > limit:
        raise _Stop("body_characters")
    check()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uid(material, source, cfg, check):
    return _hash({"scope": material["scope"], "record_id": material["record_id"],
        "source_id": material["source_id"], "source_version": {
            key: source.get(key) for key in ("original_file_bytes_sha256", "normalized_text_sha256")},
        "fingerprint": material["fingerprint"],
        "stored_text_sha256": _body_hash(material["stored_text"], cfg, check, cfg.max_quote_chars),
        "quote_sha256": _body_hash(material["quote"], cfg, check, cfg.max_quote_chars)}, cfg, check)


def _parse_typed_facts(quote, cfg, check):
    """No NLP inference: parse exact one-line JSON markers, preserving spans."""
    _body_hash(quote, cfg, check, cfg.max_quote_chars)
    found, offset = [], 0
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_typed_json_key")
            result[key] = value
        return result
    for line in quote.splitlines(keepends=True):
        check()
        content = line.rstrip("\r\n")
        prefix = "typed_facts_v1: "
        if content.startswith(prefix):
            try:
                document = json.loads(content[len(prefix):], object_pairs_hook=pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_typed_json")))
            except (ValueError, TypeError, RecursionError):
                raise ValueError("invalid_typed_json") from None
            _require(type(document) is dict and set(document) == {"facts"} and type(document["facts"]) is list,
                     "invalid_typed_contract")
            for fact in document["facts"]:
                check()
                if len(found) >= cfg.max_facts_per_material:
                    raise _Stop("facts_per_material")
                _require(type(fact) is dict and {"subject", "object", "relation", "polarity"} <= set(fact)
                         and set(fact) <= {"subject", "object", "relation", "polarity", "time", "scope", "conditions"},
                         "invalid_typed_fact")
                _require(all(_text(fact[key]) for key in ("subject", "object", "relation"))
                         and fact["polarity"] in ("positive", "negative"), "invalid_typed_fact")
                _require(all(key not in fact or _text(fact[key]) for key in ("time", "scope")), "invalid_literal_qualifier")
                conditions = fact.get("conditions", [])
                _require(type(conditions) is list and len(conditions) <= 8 and all(_text(x) for x in conditions),
                         "invalid_conditions")
                found.append({"fact": fact, "line_span": [offset, offset + len(content)],
                              "line_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
        offset += len(line)
    return found


def _rounds(frozen, cfg, check):
    _require(type(frozen) is dict, "invalid_frozen_input")
    if "schema" not in frozen:
        return [{"round_index": 0, "request_id": frozen.get("event_id"), "planning_call_id": None,
                 "record": frozen, "bindings": None, "record_sha256": _hash(frozen, cfg, check)}], None
    _require(frozen.get("schema") == "active_requery_evidence_v1" and type(frozen.get("version")) is int
             and frozen["version"] == 1 and _text(frozen.get("scope"), 200), "unsupported_frozen_bundle")
    rounds = frozen.get("rounds")
    _require(type(rounds) is list and bool(rounds), "invalid_bundle_rounds")
    if len(rounds) > cfg.max_rounds:
        raise _Stop("rounds")
    requests = set()
    for index, item in enumerate(rounds):
        check()
        _require(type(item) is dict and type(item.get("round_index")) is int and item["round_index"] == index
                 and _text(item.get("request_id"), 512) and item["request_id"] not in requests,
                 "invalid_round_identity")
        _require((index == 0 and item.get("planning_call_id") is None) or
                 (index > 0 and _text(item.get("planning_call_id"), 512)), "invalid_planning_identity")
        record = item.get("record")
        _require(type(record) is dict and record.get("scope") == frozen["scope"]
                 and (index == 0 or record.get("event_id") == item["request_id"]), "round_scope_or_event_mismatch")
        _require(_sha(item.get("record_sha256")) and _hash(record, cfg, check) == item["record_sha256"],
                 "round_record_hash_mismatch")
        _require(type(item.get("bindings")) is list and len(item["bindings"]) <= 3, "invalid_round_bindings")
        requests.add(item["request_id"])
    material_catalog = frozen.get("materials")
    _require(type(material_catalog) is list and len(material_catalog) <= cfg.max_rounds * 3, "invalid_bundle_materials")
    catalog = {}
    for item in material_catalog:
        _require(type(item) is dict and _sha(item.get("evidence_uid")) and item["evidence_uid"] not in catalog,
                 "invalid_bundle_uid")
        catalog[item["evidence_uid"]] = item
    return rounds, catalog


def _graph(record, cfg, check):
    _require(type(record) is dict and type(record.get("version")) is int and record["version"] in (1, 2),
             "unsupported_retrieval")
    _require(_text(record.get("scope"), 200) and _text(record.get("event_id"), 512), "invalid_native_identity")
    audit = record.get("graph_audit")
    _require(type(audit) is dict and audit.get("scope") == record.get("scope")
             and audit.get("event_id") == record.get("event_id"), "native_graph_identity_mismatch")
    _require(_sha(record.get("graph_audit_sha256")) and _hash(audit, cfg, check) == record["graph_audit_sha256"],
             "graph_hash_mismatch")
    selection = audit.get("selection")
    _require(type(selection) is dict and selection.get("ranking_mode") == "static", "non_static_or_unmarked_graph")
    rows = selection.get("edge_statistics")
    _require(type(rows) is list, "missing_frozen_edges")
    if len(rows) * 2 > cfg.max_arcs:
        raise _Stop("arcs")
    edges, adjacency = {}, {}
    for row in rows:
        check()
        _require(type(row) is dict and _text(row.get("a"), 200) and _text(row.get("b"), 200)
                 and row["a"] < row["b"], "invalid_edge_identity")
        weight = row.get("static_score")
        try:
            finite = type(weight) in (int, float) and math.isfinite(weight)
        except OverflowError:
            finite = False
        _require(finite and 0 <= weight <= 1, "invalid_static_weight")
        if weight == 0:
            continue
        support = row.get("source_record_ids")
        _require(type(support) is list and bool(support) and len(support) <= cfg.max_support_ids
                 and all(type(x) is int and x > 0 for x in support) and len(set(support)) == len(support),
                 "invalid_edge_support")
        pair = (row["a"], row["b"])
        _require(pair not in edges, "duplicate_frozen_edge")
        edges[pair] = {"a": pair[0], "b": pair[1], "weight": weight, "support": support,
                       "edge_id": _hash([record["scope"], pair, weight, support], cfg, check)}
        for a, b in (pair, pair[::-1]):
            adjacency.setdefault(a, set()).add(b)
        if len(adjacency) > cfg.max_nodes:
            raise _Stop("nodes")
    return edges, {node: tuple(sorted(neighbors)) for node, neighbors in adjacency.items()}, selection.get("edge_statistics_scope")


def _source_binding(material, source, record, cfg, check):
    _require(type(material) is dict and type(source) is dict and material.get("scope") == record["scope"]
             and _text(material.get("source_id"), 512) and _text(material.get("scope"), 200)
             and material.get("active_at_retrieval") is True and material.get("source_id") == source.get("source_id"),
             "source_identity_mismatch")
    quote_sha = _body_hash(material.get("quote"), cfg, check, cfg.max_quote_chars)
    stored_sha = _body_hash(material.get("stored_text"), cfg, check, cfg.max_quote_chars)
    _require(_hash([material["stored_text"], material["quote"]], cfg, check) == material.get("fingerprint"),
             "material_fingerprint_mismatch")
    payload = material.get("model_payload")
    _require(type(payload) is dict and payload.get("id") == material.get("record_id")
             and payload.get("source_id") == material["source_id"] and payload.get("scope") == material["scope"]
             and payload.get("quote") == material["quote"] and payload.get("marks") == material.get("marks")
             and payload.get("citation_id") == material.get("citation_id")
             and payload.get("citation_marker") == f'[{material.get("citation_id")}]', "model_material_binding_mismatch")
    marks = material.get("marks")
    _require(type(marks) is list and len(marks) <= 8 and all(_text(mark, 40) for mark in marks), "invalid_frozen_marks")
    if source.get("metadata_available") is True:
        raw_hash = _body_hash(source.get("raw_text"), cfg, check, cfg.max_source_chars)
        _require(raw_hash == source.get("normalized_text_sha256") and _sha(source.get("original_file_bytes_sha256"))
                 and source.get("scope") == record["scope"] and source.get("status") == "ready"
                 and source.get("published_source_id") == material["source_id"], "source_version_mismatch")
        start = source["raw_text"].find(material["quote"])
        _require(start >= 0, "quote_absent_from_frozen_source")
        version = {"kind": "knowledge_normalized_text", "normalized_text_sha256": raw_hash,
                   "original_file_bytes_sha256": source["original_file_bytes_sha256"],
                   "quote_source_span": [start, start + len(material["quote"])], "original_bytes_verified": False}
    else:
        _require(source.get("metadata_available") is False and not material["source_id"].startswith("kb:"),
                 "source_version_unavailable")
        version = {"kind": "record_content_version", "fingerprint": material["fingerprint"],
                   "source_file_version_available": False}
    return {"source_id": material["source_id"], "source_version": version,
            "quote_sha256": quote_sha, "stored_text_sha256": stored_sha,
            "fingerprint": material["fingerprint"], "evidence_uid": _uid(material, source, cfg, check)}


def _material_catalog(round_, global_catalog, cfg, check):
    record = round_["record"]
    materials, sources = record.get("materials"), record.get("sources")
    _require(type(materials) is list and len(materials) <= 3 and type(sources) is dict, "invalid_selected_catalog")
    _require(type(record.get("model_materials")) is list and len(record["model_materials"]) <= 3
             and record["model_materials"] == [item.get("model_payload") for item in materials if type(item) is dict],
             "native_model_material_catalog_mismatch")
    result = {}
    for index, material in enumerate(materials, 1):
        check()
        _require(type(material) is dict and type(material.get("record_id")) is int and material["record_id"] > 0
                 and material["record_id"] not in result and material.get("citation_id") == f"M{index}",
                 "invalid_selected_identity")
        source = sources.get(material.get("source_id"))
        binding = _source_binding(material, source, record, cfg, check)
        binding.update(local_citation_id=material["citation_id"], citation_id=material["citation_id"],
                       source_record_id=material["record_id"], record_sha256=round_["record_sha256"])
        if global_catalog is not None:
            rows = [item for item in round_["bindings"] if type(item) is dict
                    and item.get("local_citation_id") == material["citation_id"]]
            _require(len(rows) == 1 and rows[0].get("evidence_uid") == binding["evidence_uid"], "round_uid_binding_mismatch")
            item = global_catalog.get(binding["evidence_uid"])
            _require(type(item) is dict and item.get("citation_id") == rows[0].get("citation_id")
                     and _uid(item["material"], item["source"], cfg, check) == binding["evidence_uid"],
                     "global_uid_binding_mismatch")
            _source_binding(item["material"], item["source"], record, cfg, check)
            binding["citation_id"] = item["citation_id"]
        try:
            facts = _parse_typed_facts(material["quote"], cfg, check)
            parse_error = None
        except ValueError:
            facts, parse_error = [], "invalid_typed_facts"
        result[material["record_id"]] = {"material": material, "binding": binding,
                                         "facts": facts, "parse_error": parse_error}
    return result


def _query(action, cfg, check):
    _require(type(action) is dict and action.get("action") == "query" and type(action.get("query")) is dict,
             "no_logical_demand")
    query = action["query"]
    _require(type(query.get("entities")) is list and 1 <= len(query["entities"]) <= 8
             and type(query.get("relations")) is list and len(query["relations"]) <= 8
             and query.get("ambiguities") == [], "invalid_logical_proposal")
    entities = {}
    for entity in query["entities"]:
        _require(type(entity) is dict and _text(entity.get("id"), 40) and entity["id"] not in entities
                 and _text(entity.get("canonical"), 40), "invalid_entity_proposal")
        entities[entity["id"]] = entity["canonical"]
    for key in ("time", "scope"):
        anchor = query.get(key)
        _require(anchor is None or (type(anchor) is dict and _text(anchor.get("surface"))
                 and type(anchor.get("span")) is list and len(anchor["span"]) == 2
                 and all(type(x) is int for x in anchor["span"]) and 0 <= anchor["span"][0] < anchor["span"][1]),
                 "invalid_literal_anchor")
    needs = []
    negations = query.get("negations")
    _require(type(negations) is list and len(negations) <= 8, "invalid_negation_anchors")
    for anchor in negations:
        _require(type(anchor) is dict and _text(anchor.get("surface")) and type(anchor.get("span")) is list
                 and len(anchor["span"]) == 2 and all(type(x) is int for x in anchor["span"])
                 and 0 <= anchor["span"][0] < anchor["span"][1], "invalid_negation_anchor")
    for relation in query["relations"]:
        check()
        _require(type(relation) is dict and relation.get("subject_id") in entities and relation.get("object_id") in entities
                 and _text(relation.get("predicate"), 80) and type(relation.get("negated")) is bool
                 and relation.get("direction") in ("subject_to_object", "object_to_subject", "undirected"),
                 "invalid_relation_proposal")
        a, b = entities[relation["subject_id"]], entities[relation["object_id"]]
        if relation["direction"] == "object_to_subject":
            a, b = b, a
        needs.append({"from": a, "to": b, "relation": relation["predicate"],
                      "polarity": "negative" if relation["negated"] else "positive", "direction": relation["direction"],
                      "literal_time": query.get("time"), "literal_scope": query.get("scope"),
                      "independent_negation_anchors": negations,
                      "proposal_verified": False})
    return needs


def _paths(start, adjacency, depth, cfg, check, stats):
    queue, paths = deque([(start,)]), {start: [(start,)]}
    while queue:
        check()
        path = queue.popleft()
        if len(path) - 1 >= depth:
            continue
        for neighbor in adjacency.get(path[-1], ()):
            check()
            stats["expansions"] += 1
            if stats["expansions"] > cfg.max_expansions:
                raise _Stop("expansions")
            if neighbor in path:
                continue
            next_path = path + (neighbor,)
            if len(queue) >= cfg.max_frontier:
                raise _Stop("frontier")
            queue.append(next_path)
            paths.setdefault(neighbor, []).append(next_path)
    return paths


def _candidate(path, meeting, need, round_, edges, selected, cfg, check, stats):
    reasons, bindings, direct_facts, source_bindings, edge_support = [], [], [], [], []
    if need["independent_negation_anchors"]:
        reasons.append("query_negation_scope_unknown")
    for a, b in zip(path, path[1:]):
        edge = edges[tuple(sorted((a, b)))]
        support_receipt = {"edge_id": edge["edge_id"], "from": a, "to": b,
            "declared_support_ids": list(edge["support"]), "resolved_support_ids": [], "unresolved_support_ids": []}
        edge_support.append(support_receipt)
        for record_id in edge["support"]:
            check()
            source = selected.get(record_id)
            if source is None:
                reasons.append("support_not_frozen_in_same_round")
                support_receipt["unresolved_support_ids"].append(record_id)
                continue
            support_receipt["resolved_support_ids"].append(record_id)
            check()
            if stats["bindings"] >= cfg.max_bindings:
                raise _Stop("bindings")
            stats["bindings"] += 1
            body_binding = dict(source["binding"])
            body_binding.update(edge_id=edge["edge_id"], graph_from=a, graph_to=b,
                parse_status="invalid" if source["parse_error"] else "typed" if source["facts"] else "no_typed_facts")
            source_bindings.append(body_binding)
            if source["parse_error"]:
                reasons.append(source["parse_error"])
            if a not in source["material"]["marks"] or b not in source["material"]["marks"]:
                reasons.append("support_endpoint_marks_conflict")
                continue
            facts = [item for item in source["facts"] if {item["fact"]["subject"], item["fact"]["object"]} == {a, b}]
            if not facts:
                reasons.append("typed_fact_unavailable")
            for item in facts:
                check()
                if stats["bindings"] >= cfg.max_bindings:
                    raise _Stop("bindings")
                stats["bindings"] += 1
                fact = item["fact"]
                binding = dict(source["binding"])
                binding.update(edge_id=edge["edge_id"], graph_from=a, graph_to=b, static_weight=edge["weight"],
                    fact=fact, fact_line_span=item["line_span"], fact_line_sha256=item["line_sha256"],
                    fact_subject=fact["subject"], fact_object=fact["object"], graph_direction="undirected_cooccurrence")
                bindings.append(binding)
                if len(path) == 2:
                    direct_facts.append(fact)
    rejected = "support_endpoint_marks_conflict" in reasons
    if len(path) != 2:
        reasons.append("relation_composition_not_authorized")
    elif direct_facts:
        related = [fact for fact in direct_facts if fact["relation"] == need["relation"]]
        applicable, conflicts, unknown_qualifiers, uncertain_relevant = [], [], [], []
        for fact in related:
            qualifiers_ok, fact_conflicts, fact_unknown = True, [], []
            for key in ("time", "scope"):
                anchor = need[f"literal_{key}"]
                literal = anchor["surface"] if anchor is not None else None
                fact_literal = fact.get(key)
                if literal != fact_literal:
                    qualifiers_ok = False
                    if literal is not None and fact_literal is not None:
                        fact_conflicts.append(f"literal_{key}_conflict")
                    else:
                        fact_unknown.append(f"literal_{key}_unknown")
            if fact.get("conditions", []):
                qualifiers_ok = False
                fact_unknown.append("conditions_not_declared_in_query")
            conflicts.extend(fact_conflicts)
            unknown_qualifiers.extend(fact_unknown)
            if fact_unknown and not fact_conflicts and (fact["subject"], fact["object"]) == (need["from"], need["to"]):
                uncertain_relevant.extend(fact_unknown)
            if qualifiers_ok:
                applicable.append(fact)
        if not related:
            reasons.append("requested_relation_unavailable")
        elif not applicable:
            reasons.extend(unknown_qualifiers or conflicts or ["required_fact_unknown"])
            rejected |= bool(conflicts) and not unknown_qualifiers
        elif need["direction"] == "undirected":
            reasons.append("query_direction_undetermined")
        else:
            reasons.extend(uncertain_relevant)
            directed = [fact for fact in applicable if (fact["subject"], fact["object"]) == (need["from"], need["to"])]
            if not directed:
                reasons.append("direction_conflict")
                rejected = True
            else:
                polarities = {fact["polarity"] for fact in directed}
                if polarities != {need["polarity"]}:
                    reasons.append("polarity_conflict")
                    rejected = True
    status = "rejected" if rejected else ("unknown" if reasons or not direct_facts else "pending_hypothesis")
    return {"path_nodes": list(path), "meeting_node": meeting, "round_index": round_["round_index"],
            "request_id": round_["request_id"], "planning_call_id": round_["planning_call_id"],
            "query_call_binding": round_.get("query_call_binding", "matched_same_round_bundle"),
            "relation_proposal": need, "status": status, "reasons": sorted(set(reasons)),
            "edge_bindings": bindings, "edge_source_bindings": source_bindings, "edge_support": edge_support,
            "proof": False, "truth_verified": False, "semantic_support_verified": False}


def analyze_path_hypotheses(frozen, query_actions, *, config: PathHypothesesConfig | None = None, deadline=None):
    cfg = config or PathHypothesesConfig()
    receipt = {"schema": "automatic_path_hypotheses_v1", "enabled": cfg.enabled, "status": "disabled",
        "proof": False, "proposals_verified": False, "truth_verified": False, "semantic_support_verified": False,
        "changes_base_weights": False, "changes_selection": False, "candidates": [], "meetings": [],
        "requirements": [],
        "actions": [],
        "incomplete": False, "incomplete_reasons": [], "query_anchor_validation": "upstream_only",
        "analysis_scope": "each_frozen_round", "stats": {"nodes": 0, "arcs": 0, "expansions": 0, "candidates": 0, "bindings": 0}}
    if not cfg.enabled:
        return receipt
    started = time.monotonic()
    stop_at = started + cfg.max_seconds
    try:
        if deadline is not None:
            try:
                valid = type(deadline) in (int, float) and math.isfinite(deadline)
            except OverflowError:
                valid = False
            _require(valid, "invalid_deadline")
            stop_at = min(stop_at, deadline)
        def check():
            if time.monotonic() >= stop_at:
                raise _Stop("seconds")
        check()
        _require(type(query_actions) is list, "invalid_query_actions")
        if len(query_actions) > cfg.max_actions:
            raise _Stop("actions")
        if not query_actions:
            receipt.update(status="unknown", incomplete=True, incomplete_reasons=["no_logical_demand"])
            return receipt
        rounds, global_catalog = _rounds(frozen, cfg, check)
        cache = {}
        for action_index, wrapper in enumerate(query_actions):
            check()
            _require(type(wrapper) is dict and wrapper.get("anchors_validated") is True
                     and type(wrapper.get("evidence_round_index")) is int
                     and 0 <= wrapper["evidence_round_index"] < len(rounds)
                     and _text(wrapper.get("planning_call_id"), 512), "unvalidated_or_unbound_query")
            action = wrapper.get("action")
            _require(_sha(wrapper.get("action_sha256")) and _hash(action, cfg, check) == wrapper["action_sha256"],
                     "query_action_hash_mismatch")
            round_ = rounds[wrapper["evidence_round_index"]]
            _require(global_catalog is None or round_["planning_call_id"] == wrapper["planning_call_id"],
                     "query_round_call_mismatch")
            needs = _query(action, cfg, check)
            action_outcome = {"action_index": action_index, "round_index": round_["round_index"],
                "planning_call_id": wrapper["planning_call_id"], "action_sha256": wrapper["action_sha256"],
                "requirement_indices": [], "status": "unknown", "reasons": []}
            receipt["actions"].append(action_outcome)
            if not needs:
                receipt["incomplete_reasons"].append("no_relation_proposal")
                action_outcome["reasons"].append("no_relation_proposal")
                continue
            effective_round = dict(round_)
            if global_catalog is None:
                effective_round.update(planning_call_id=wrapper["planning_call_id"],
                                       query_call_binding="declared_upstream_query_call")
            index = round_["round_index"]
            if index not in cache:
                edges, adjacency, graph_scope = _graph(round_["record"], cfg, check)
                selected = _material_catalog(round_, global_catalog, cfg, check)
                if receipt["stats"]["nodes"] + len(adjacency) > cfg.max_nodes:
                    raise _Stop("nodes")
                if receipt["stats"]["arcs"] + 2 * len(edges) > cfg.max_arcs:
                    raise _Stop("arcs")
                cache[index] = (edges, adjacency, selected)
                receipt["stats"]["nodes"] += len(adjacency)
                receipt["stats"]["arcs"] += 2 * len(edges)
                if graph_scope is not None:
                    receipt["incomplete_reasons"].append("frozen_neighborhood_only")
            edges, adjacency, selected = cache[index]
            for relation_index, need in enumerate(needs):
                check()
                requirement = {"action_index": action_index, "relation_index": relation_index,
                    "round_index": index, "proposal": need, "status": "unknown", "candidate_indices": [], "reasons": []}
                receipt["requirements"].append(requirement)
                action_outcome["requirement_indices"].append(len(receipt["requirements"]) - 1)
                if need["from"] not in adjacency or need["to"] not in adjacency:
                    receipt["incomplete_reasons"].append("proposed_endpoint_not_in_frozen_graph")
                    requirement["reasons"].append("proposed_endpoint_not_in_frozen_graph")
                    continue
                left = _paths(need["from"], adjacency, (cfg.max_depth + 1) // 2, cfg, check, receipt["stats"])
                right = _paths(need["to"], adjacency, cfg.max_depth // 2, cfg, check, receipt["stats"])
                seen, found = set(), False
                for meeting in sorted(left.keys() & right.keys()):
                    for a in left[meeting]:
                        for b in right[meeting]:
                            check()
                            path = a + tuple(reversed(b[:-1]))
                            if path in seen or len(path) - 1 > cfg.max_depth or len(set(path)) != len(path):
                                continue
                            seen.add(path)
                            if len(receipt["candidates"]) >= cfg.max_candidates:
                                raise _Stop("candidates")
                            candidate = _candidate(path, meeting, need, effective_round, edges, selected, cfg, check, receipt["stats"])
                            requirement["candidate_indices"].append(len(receipt["candidates"]))
                            receipt["candidates"].append(candidate)
                            receipt["meetings"].append({"round_index": index, "node": meeting,
                                "path_nodes": list(path), "automatic": True})
                            receipt["stats"]["candidates"] += 1
                            found = True
                if not found:
                    receipt["incomplete_reasons"].append("no_path_within_frozen_depth")
                    requirement["reasons"].append("no_path_within_frozen_depth")
                statuses = {receipt["candidates"][i]["status"] for i in requirement["candidate_indices"]}
                requirement["status"] = ("pending_hypothesis" if "pending_hypothesis" in statuses else
                    "unknown" if "unknown" in statuses or not statuses else "rejected")
            statuses = {receipt["requirements"][i]["status"] for i in action_outcome["requirement_indices"]}
            action_outcome["status"] = ("unknown" if "unknown" in statuses or not statuses else
                "rejected" if "rejected" in statuses else "pending_hypothesis")
        statuses = {item["status"] for item in receipt["actions"]}
        receipt["status"] = ("unknown" if "unknown" in statuses or not statuses else
            "rejected" if "rejected" in statuses else "pending_hypothesis")
        receipt["incomplete_reasons"] = sorted(set(receipt["incomplete_reasons"]))
        receipt["incomplete"] = bool(receipt["incomplete_reasons"] or "unknown" in statuses)
        check()
    except _Stop as exc:
        receipt.update(status="budget_stop", incomplete=True)
        receipt["incomplete_reasons"].append(str(exc))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        receipt.update(status="unknown", incomplete=True)
        receipt["incomplete_reasons"].append("invalid_or_unbound_frozen_evidence")
    finally:
        receipt["stats"]["elapsed_seconds"] = max(0.0, time.monotonic() - started)
    return receipt
