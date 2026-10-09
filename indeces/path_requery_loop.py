"""Pure, opt-in graph clues for the *next* bounded static retrieval.

A new anchored action is a demand on already frozen evidence, not the producer
of that evidence. Real undirected co-occurrence arcs authorize exploration;
they do not establish directed relations, entailment, or scientific truth.
Only explicit typed claims and declared formal rules can yield a formal result.
No model, database, network, or mutable graph is consulted by this module.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import math
import re
import time

from .memory import _canonical, _hit
from .path_hypotheses import (
    PathHypothesesConfig, _Stop, _candidate, _graph, _hash, _material_catalog,
    _paths, _query, _require, _rounds, _text,
)
from .relevance import metadata_factor
from .run_records import digest, validate_graph_audit, validate_retrieval


POLICY = "sourced_path_requery_loop_v1"
SCHEMA = "path_requery_feedback_v1"
_REFERENCE_HEADING = re.compile(
    r"^\s*(?:references|bibliography|literature cited|参考文献)\s*:?\s*$", re.I | re.M)


@dataclass(frozen=True)
class PathRequeryLoopConfig(PathHypothesesConfig):
    accumulate_rounds: bool = True
    max_terms: int = 12
    max_added_terms: int = 4
    max_aliases: int = 16
    max_rules: int = 16
    aliases: dict = field(default_factory=dict)
    composition_rules: tuple = ()

    def __post_init__(self):
        # Validate inherited scalar limits explicitly: the parent validator
        # also walks subclass fields and would treat structured declarations
        # as integers.
        for name in PathHypothesesConfig.__dataclass_fields__:
            value = getattr(self, name)
            if name == "enabled":
                if type(value) is not bool:
                    raise ValueError("path loop enabled must be a bool")
            elif name == "max_seconds":
                try:
                    valid = type(value) in (int, float) and math.isfinite(value) and value > 0
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError("invalid path time budget")
            elif type(value) is not int or value <= 0:
                raise ValueError(f"invalid path limit {name}")
        for name in ("max_terms", "max_added_terms", "max_aliases", "max_rules"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"invalid path loop limit {name}")
        if type(self.accumulate_rounds) is not bool:
            raise ValueError("path round accumulation must be a bool")
        if self.max_terms > 64 or self.max_added_terms > self.max_terms:
            raise ValueError("invalid path term limits")
        if (type(self.aliases) is not dict or len(self.aliases) > self.max_aliases
                or not all(_safe_term(a) and _safe_term(b) for a, b in self.aliases.items())):
            raise ValueError("invalid explicit aliases")
        if type(self.composition_rules) not in (tuple, list) or len(self.composition_rules) > self.max_rules:
            raise ValueError("invalid declared composition rules")
        seen = set()
        for rule in self.composition_rules:
            if (type(rule) is not dict or set(rule) != {"rule_id", "relation1", "relation2", "result"}
                    or not all(_text(rule[key], 80) for key in rule)
                    or rule["rule_id"] in seen):
                raise ValueError("invalid declared composition rule")
            seen.add(rule["rule_id"])
        # The complete receipt cap includes its refusal envelope. Reject a
        # configuration that cannot fit declarations plus bounded baseline,
        # call identity and cutoff metadata; never exceed a tiny cap silently.
        declaration = asdict(self)
        if not self.accumulate_rounds:
            declaration.pop("accumulate_rounds")
        declaration_bytes = len(json.dumps(declaration, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        if self.max_serialized_bytes < declaration_bytes + 8192:
            raise ValueError("path serialized limit cannot fit bounded refusal metadata")


def _safe_term(value):
    return (_text(value, 40) and bool(value.strip())
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def _binding_key(binding):
    key = (binding["edge_id"], binding["evidence_uid"], binding["quote_sha256"])
    # Legacy receipts deliberately deduplicated equal body/edge identities
    # across rounds. New bindings explicitly retain the native observation.
    if "round_index" in binding:
        key += (binding["round_index"], binding["request_id"], binding["record_sha256"])
    return key


def _qualified_edge(edge, selected, cfg, check, stats):
    """At least one actual selected body supports this structural arc.

    Other declared IDs remain unresolved. A positive NPMI score or a selected
    citation alone cannot substitute for the frozen body/endpoint association.
    """
    bindings, reasons, unresolved = [], [], []
    for record_id in edge["support"]:
        check()
        source = selected.get(record_id)
        if source is None:
            unresolved.append(record_id)
            continue
        material = source["material"]
        if not all(node in material["marks"] for node in (edge["a"], edge["b"])):
            reasons.append("support_endpoint_marks_conflict")
            continue
        quote = material["quote"]
        kind, _ = metadata_factor(quote)
        if _REFERENCE_HEADING.search(quote) or kind in (
                "bibliography", "publication_metadata", "publication_identity"):
            reasons.append("bibliography_or_metadata_support")
            continue
        if not all(_hit(node, quote) for node in (edge["a"], edge["b"])):
            reasons.append("support_endpoint_absent_from_quote")
            continue
        if stats["bindings"] >= cfg.max_bindings:
            raise _Stop("bindings")
        stats["bindings"] += 1
        binding = dict(source["binding"])
        binding.update(edge_id=edge["edge_id"], graph_from=edge["a"], graph_to=edge["b"],
                       support_kind="literal_marked_body_cooccurrence", semantic_support_verified=False)
        bindings.append(binding)
    if unresolved:
        reasons.append("additional_support_not_frozen_in_same_round")
    if not bindings:
        reasons.append("no_qualified_same_round_body_support")
    return {"edge_id": edge["edge_id"], "from": edge["a"], "to": edge["b"],
            "qualified": bool(bindings), "provenance": bindings,
            "unresolved_support_ids": unresolved, "reasons": sorted(set(reasons))}


def _map(label, nodes, aliases):
    retrieval = _canonical(label)
    target, method = retrieval, "memory_strip_casefold_v1"
    if label in aliases:
        target, method = aliases[label], "explicit_alias_v1"
    return {"canonical": label, "retrieval_term": retrieval,
            "graph_endpoint": target if target in nodes else None,
            "status": "mapped" if target in nodes else "unknown", "method": method,
            "canonical_equivalence_verified": False}


def _declared_scope(record, nodes, edges):
    selection = record["graph_audit"]["selection"]
    declaration = selection.get("path_graph_scope")
    valid = (type(declaration) is dict and set(declaration) == {
        "kind", "scope", "complete", "node_labels", "edge_count"}
        and declaration.get("kind") == "full_positive_static_graph_v1"
        and declaration.get("scope") == record["scope"] and declaration.get("complete") is True
        and type(declaration.get("edge_count")) is int
        and declaration["edge_count"] == len(edges) == selection.get("live_edge_count")
        and type(declaration.get("node_labels")) is list
        and all(_safe_term(x) and _canonical(x) == x for x in declaration["node_labels"])
        and len(set(declaration["node_labels"])) == len(declaration["node_labels"])
        and set(declaration["node_labels"]) == nodes
        and "edge_statistics_scope" not in selection)
    return {"kind": "declared_full_graph" if valid else "frozen_neighborhood",
            "declaration_present": declaration is not None,
            "completeness": "declared_not_corpus_verified" if valid else "unknown",
            "corpus_completeness_verified": False, "declared_complete": valid}


def _component_exhausted(start, adjacency, depth, cfg, check, stats):
    # Separate bounded traversal determines whether a depth cutoff remains.
    reached, frontier = {start}, [start]
    for _ in range(depth):
        next_frontier = []
        for node in frontier:
            for neighbor in adjacency.get(node, ()):
                check()
                stats["expansions"] += 1
                if stats["expansions"] > cfg.max_expansions:
                    raise _Stop("expansions")
                if neighbor not in reached:
                    reached.add(neighbor)
                    if len(next_frontier) >= cfg.max_frontier:
                        raise _Stop("frontier")
                    next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            return True
    for node in frontier:
        for neighbor in adjacency.get(node, ()):
            check()
            stats["expansions"] += 1
            if stats["expansions"] > cfg.max_expansions:
                raise _Stop("expansions")
            if neighbor not in reached:
                return False
    return True


def _formal_derivation(candidate, cfg, check, stats):
    """Literal directed positive composition under an explicitly named rule.

    Applicable claims must agree with query time/scope and be unconditional.
    Negative, reverse, conditional, or conflicting claims are kept in the
    candidate, but cannot authorize this limited formal derivation.
    """
    path, need = candidate["path_nodes"], candidate["relation_proposal"]
    if len(path) < 3 or not cfg.composition_rules:
        return None
    if (need["polarity"] != "positive" or need["direction"] == "undirected"
            or need["independent_negation_anchors"] or not candidate["structural_support_qualified"]):
        return None
    qualified_bindings = {_binding_key(row) for support in candidate["qualified_edge_support"]
                          for row in support["provenance"]}
    choices, relevant_unknown = [], False
    for a, b in zip(path, path[1:]):
        directed = []
        for binding in candidate["edge_bindings"]:
            check()
            if _binding_key(binding) not in qualified_bindings:
                continue
            if (binding["graph_from"], binding["graph_to"]) != (a, b):
                continue
            claim = binding["fact"]
            if (claim["subject"], claim["object"]) != (a, b):
                continue
            qualifiers_ok, unknown = True, False
            for key in ("time", "scope"):
                anchor = need[f"literal_{key}"]
                literal = anchor["surface"] if anchor is not None else None
                if claim.get(key) != literal:
                    qualifiers_ok = False
                    unknown |= claim.get(key) is None or literal is None
            if claim.get("conditions", []):
                qualifiers_ok, unknown = False, True
            relevant_unknown |= unknown
            if qualifiers_ok:
                directed.append(binding)
        positive = []
        for binding in directed:
            relation = binding["fact"]["relation"]
            polarities = {row["fact"]["polarity"] for row in directed
                          if row["fact"]["relation"] == relation}
            if polarities == {"positive"}:
                positive.append(binding)
        if not positive:
            return None
        choices.append(positive)
    if relevant_unknown:
        return None
    # Bounded states are (current relation, concrete fact binding chain, rules).
    states = [(row["fact"]["relation"], [row], []) for row in choices[0]]
    for step in choices[1:]:
        following = []
        for relation, facts, rule_ids in states:
            for fact in step:
                for rule in cfg.composition_rules:
                    check()
                    stats["expansions"] += 1
                    if stats["expansions"] > cfg.max_expansions:
                        raise _Stop("expansions")
                    if rule["relation1"] == relation and rule["relation2"] == fact["fact"]["relation"]:
                        if len(following) >= cfg.max_candidates:
                            raise _Stop("formal_derivations")
                        following.append((rule["result"], facts + [fact], rule_ids + [rule["rule_id"]]))
        states = following
        if not states:
            return None
    for relation, facts, rule_ids in states:
        if relation == need["relation"]:
            return {"status": "formal_under_declared_rule", "rule_ids": rule_ids,
                    "fact_bindings": [dict(row, record_id=row["source_record_id"]) for row in facts],
                    "result": relation, "proof": False,
                    "semantic_support_verified": False, "truth_verified": False}
    return None


def _source_version_groups(round_, source):
    """Only frozen identities: no path resolution or current source lookup."""
    binding = source["binding"]
    version = binding["source_version"]
    scope = round_["record"]["scope"]
    if version["kind"] == "knowledge_normalized_text":
        identity = (version["original_file_bytes_sha256"], version["normalized_text_sha256"])
        groups = [("source_id", scope, binding["source_id"])]
        metadata = round_["record"]["sources"][binding["source_id"]]
        # Native path is a frozen relative POSIX string. Exact comparison is
        # deterministic on every replay platform; no case/alias guessing.
        if type(metadata.get("path")) is str and metadata["path"]:
            groups.append(("source_path", scope, metadata["path"]))
            # This is an ambiguity hint, never a file-equivalence assertion.
            # Frozen receipts have no filesystem case/alias contract. A hint
            # collision with different exact paths and versions is refused.
            groups.append(("ambiguous_source_path_hint", scope, metadata["path"].replace("\\", "/").casefold()))
    else:
        # Different selected chunks of one source have different fingerprints.
        # A changed version of the same record cannot be silently mixed.
        groups = [("record_content", scope, binding["source_id"], binding["source_record_id"])]
        identity = (version["fingerprint"],)
    return groups, identity


def _frontier_claim_reasons(endpoint, neighbor, need, qualification, selected):
    """A typed half-edge cannot be used in the opposite claimed direction."""
    if need["polarity"] != "positive" or need["independent_negation_anchors"]:
        return ["frontier_query_negation_unknown"]
    direction = ((endpoint, neighbor) if endpoint == need["from"] else (neighbor, endpoint))
    facts = [item["fact"] for binding in qualification["provenance"]
             for item in selected[binding["source_record_id"]]["facts"]
             if {item["fact"]["subject"], item["fact"]["object"]} == {endpoint, neighbor}]
    # Untyped literal co-occurrence is only an explicit exploratory clue.
    if not facts:
        return []
    directed = [fact for fact in facts if (fact["subject"], fact["object"]) == direction]
    if not directed:
        return ["frontier_direction_conflict"]
    reasons = []
    if any(fact["polarity"] != "positive" for fact in directed):
        reasons.append("frontier_polarity_conflict")
    for fact in directed:
        if fact.get("conditions", []):
            reasons.append("frontier_conditions_unknown")
        for key in ("time", "scope"):
            anchor = need[f"literal_{key}"]
            literal = anchor["surface"] if anchor is not None else None
            if fact.get(key) != literal:
                reasons.append(f"frontier_literal_{key}_unknown_or_conflict")
    return sorted(set(reasons))


def _accumulated_candidate(path, meeting, need, observations, cfg, check, stats):
    """Compose structural arcs whose bodies were qualified in their own round.

    Keep each native edge instance and each versioned source separately. The
    union never changes a score or assigns a later quote to an earlier edge.
    """
    bindings, source_bindings, edge_support, qualifications, path_edges = [], [], [], [], []
    reasons = ["relation_composition_not_authorized"] if len(path) != 2 else []
    if need["independent_negation_anchors"]:
        reasons.append("query_negation_scope_unknown")
    for a, b in zip(path, path[1:]):
        pair = tuple(sorted((a, b)))
        instances, step_facts = [], []
        for observation in observations[pair]:
            check()
            round_, edge, selected, qualification = observation
            qualified_ids = {row["source_record_id"] for row in qualification["provenance"]}
            origin = {"round_index": round_["round_index"], "request_id": round_["request_id"],
                      "record_sha256": round_["record_sha256"], "edge_id": edge["edge_id"],
                      "static_weight": edge["weight"]}
            instances.append(origin)
            qualification = deepcopy(qualification)
            qualification.update(origin, **{"from": a, "to": b})
            qualifications.append(qualification)
            support = dict(origin, **{"from": a, "to": b,
                "declared_support_ids": list(edge["support"]),
                "resolved_support_ids": sorted(qualified_ids),
                "unresolved_support_ids": [item for item in edge["support"] if item not in qualified_ids]})
            edge_support.append(support)
            for record_id in sorted(qualified_ids):
                check()
                if stats["bindings"] >= cfg.max_bindings:
                    raise _Stop("bindings")
                stats["bindings"] += 1
                source = selected[record_id]
                body_binding = dict(source["binding"], **origin, graph_from=a, graph_to=b,
                    parse_status="invalid" if source["parse_error"] else "typed" if source["facts"] else "no_typed_facts")
                source_bindings.append(body_binding)
                if source["parse_error"]:
                    reasons.append(source["parse_error"])
                facts = [item for item in source["facts"]
                         if {item["fact"]["subject"], item["fact"]["object"]} == {a, b}]
                if not facts:
                    reasons.append("typed_fact_unavailable")
                for item in facts:
                    check()
                    if stats["bindings"] >= cfg.max_bindings:
                        raise _Stop("bindings")
                    stats["bindings"] += 1
                    fact = deepcopy(item["fact"])
                    binding = dict(source["binding"], **origin, graph_from=a, graph_to=b,
                        fact=fact, fact_line_span=list(item["line_span"]), fact_line_sha256=item["line_sha256"],
                        fact_subject=fact["subject"], fact_object=fact["object"],
                        graph_direction="undirected_cooccurrence")
                    bindings.append(binding)
                    step_facts.append(fact)
        path_edges.append({"from": a, "to": b, "observations": instances})
        if step_facts:
            directed = [fact for fact in step_facts if (fact["subject"], fact["object"]) == (a, b)]
            if not directed:
                reasons.append("direction_conflict")
            if any(fact["polarity"] == "negative" for fact in directed):
                reasons.append("negative_edge_claim_present")
            for relation in {fact["relation"] for fact in directed}:
                if len({fact["polarity"] for fact in directed if fact["relation"] == relation}) > 1:
                    reasons.append("polarity_conflict")
            for fact in directed:
                for key in ("time", "scope"):
                    anchor = need[f"literal_{key}"]
                    literal = anchor["surface"] if anchor is not None else None
                    if literal != fact.get(key):
                        reasons.append(f"literal_{key}_conflict" if literal is not None and fact.get(key) is not None
                                       else f"literal_{key}_unknown")
                if fact.get("conditions", []):
                    reasons.append("conditions_not_declared_in_query")
    # Direct paths may accumulate contradictory claims from separate rounds.
    # Evaluate the demand without converting any exploratory arc to proof.
    if len(path) == 2:
        applicable = [row["fact"] for row in bindings
            if row["fact"]["relation"] == need["relation"]
            and (row["fact"]["subject"], row["fact"]["object"]) == (need["from"], need["to"])]
        if not applicable:
            reasons.append("requested_relation_unavailable")
        elif {fact["polarity"] for fact in applicable} != {need["polarity"]}:
            reasons.append("polarity_conflict")
        if need["direction"] == "undirected":
            reasons.append("query_direction_undetermined")
    indices = sorted({row["round_index"] for row in source_bindings})
    return {"path_nodes": list(path), "meeting_node": meeting,
        "round_index": None, "round_indices": indices, "request_id": None, "planning_call_id": None,
        "query_call_binding": "prospective_current_task_frozen_evidence",
        "analysis_scope": "current_task_frozen_rounds", "evaluation_role": "current_task_accumulated",
        "relation_proposal": need, "status": "unknown" if reasons else "pending_hypothesis",
        "reasons": sorted(set(reasons)), "edge_bindings": bindings, "edge_source_bindings": source_bindings,
        "edge_support": edge_support, "qualified_edge_support": qualifications,
        "path_edge_observations": path_edges, "structural_support_qualified": True,
        "edge_ids": list(dict.fromkeys(row["edge_id"] for row in edge_support)),
        "qualification_reasons": sorted({reason for row in qualifications for reason in row["reasons"]}),
        "formal_derivation": None, "proof": False, "truth_verified": False, "semantic_support_verified": False}


def build_path_feedback(frozen_bundle_or_record, action, *, planning_call_id,
                        config: PathRequeryLoopConfig | None = None, deadline=None):
    """Return a hash-bound demand/graph receipt and safe next-retrieval terms.

    The caller validates original anchors, applies the terms to real retrieval,
    feeds the bound receipts to its next planner, and owns shared cumulative admission,
    duplicate-query detection, usage reconciliation and the enclosing deadline.
    Disabled calls do not inspect inputs; callers retain their dev10 terms.
    """
    cfg = config or PathRequeryLoopConfig()
    _require(type(cfg) is PathRequeryLoopConfig, "invalid_path_loop_config")
    limits = asdict(cfg)
    if not cfg.accumulate_rounds:
        limits.pop("accumulate_rounds")
    receipt = {"schema": SCHEMA, "version": 1, "policy": POLICY, "enabled": cfg.enabled,
        "status": "disabled", "reason": "disabled", "reasons": [], "demand_binding": None,
        "baseline_terms": [], "effective_terms": [], "added_terms": [], "mapping": [], "term_mapping": [],
        "requirements": [], "candidates": [], "clues": [], "frontier_clues": [], "gaps": [],
        "graph_scopes": [], "limits": limits, "incomplete": False,
        "proof": False, "truth_verified": False, "semantic_support_verified": False,
        "changes_base_weights": False, "additive_score": False,
        "query_anchor_validation": "upstream_only", "analysis_scope": "each_frozen_round",
        "stats": {"nodes": 0, "arcs": 0, "expansions": 0, "candidates": 0, "bindings": 0,
                  "elapsed_seconds": 0.0}}
    if cfg.accumulate_rounds and cfg.enabled:
        receipt.update(analysis_scope="current_task_frozen_rounds", accumulated_candidate_indices=[],
                       accumulation_scope=None)
    if not cfg.enabled:
        receipt["feedback_sha256"] = digest(receipt)
        return receipt
    started, qualified_cache, clues_by_term = time.monotonic(), {}, {}
    stop_at = started + cfg.max_seconds
    # Preserve a safely bounded baseline even when a deadline already expired.
    if type(action) is dict and type(action.get("query")) is dict:
        terms = action["query"].get("canonical_terms")
        if type(terms) is list and 1 <= len(terms) <= 8 and all(_safe_term(x) for x in terms):
            receipt["baseline_terms"] = list(terms)
            receipt["effective_terms"] = list(terms)
    def check():
        if time.monotonic() >= stop_at:
            raise _Stop("seconds")
    def gap(reason, round_index=None, requirement_index=None):
        item = {"reason": reason, "round_index": round_index, "requirement_index": requirement_index,
                "semantic_support_verified": False}
        if item not in receipt["gaps"]:
            receipt["gaps"].append(item)
    def cutoff(reason):
        # Clock cutoffs cannot be replayed deterministically. They carry only
        # the baseline and cutoff diagnostic, never partly evaluated claims.
        receipt.update(status="budget_stop", reason=reason, incomplete=True)
        receipt["effective_terms"] = list(receipt["baseline_terms"])
        for key in ("added_terms", "clues", "frontier_clues", "candidates", "requirements", "mapping", "term_mapping", "graph_scopes"):
            receipt[key] = []
        if cfg.accumulate_rounds:
            receipt["accumulated_candidate_indices"] = []
            receipt["accumulation_scope"] = None
        receipt["gaps"] = [{"reason": reason, "round_index": None, "requirement_index": None,
                            "semantic_support_verified": False}]
        receipt["reasons"] = [reason]
    def edge_qualification(index, pair, edges, selected):
        key = (index, pair)
        if key not in qualified_cache:
            qualified_cache[key] = _qualified_edge(edges[pair], selected, cfg, check, receipt["stats"])
        return qualified_cache[key]
    def clue(term, provenance, candidate_index=None, frontier=None):
        if not _safe_term(term) or _canonical(term) in {_canonical(x) for x in receipt["baseline_terms"]}:
            return
        if term not in clues_by_term:
            if (len(clues_by_term) >= cfg.max_added_terms
                    or len(receipt["baseline_terms"]) + len(clues_by_term) >= cfg.max_terms):
                gap("clue_terms_capped")
                return
            clues_by_term[term] = {"term": term, "candidate_indices": [], "provenance": [],
                "purpose": "structural_exploration_only", "semantic_support_verified": False}
        item = clues_by_term[term]
        if candidate_index is not None and candidate_index not in item["candidate_indices"]:
            item["candidate_indices"].append(candidate_index)
        seen = {_binding_key(x) for x in item["provenance"]}
        for binding in provenance:
            if _binding_key(binding) not in seen:
                item["provenance"].append(binding)
                seen.add(_binding_key(binding))
        if frontier is not None and frontier not in receipt["frontier_clues"]:
            if len(receipt["frontier_clues"]) >= cfg.max_candidates:
                raise _Stop("frontier_clues")
            receipt["frontier_clues"].append(frontier)
    try:
        if deadline is not None:
            _require(type(deadline) in (int, float) and math.isfinite(deadline), "invalid_deadline")
            stop_at = min(stop_at, deadline)
        check()
        _require(_text(planning_call_id, 512), "invalid_planning_identity")
        _require(bool(receipt["baseline_terms"]), "invalid_canonical_terms")
        if len(receipt["baseline_terms"]) > cfg.max_terms:
            raise _Stop("terms")
        action_hash = _hash(action, cfg, check)
        needs = _query(action, cfg, check)
        parent_hash = _hash(frozen_bundle_or_record, cfg, check)
        receipt["demand_binding"] = {"binding_kind": "prospective_demand_on_parent_evidence",
            "planning_call_id": planning_call_id, "action_sha256": action_hash,
            "parent_evidence_sha256": parent_hash}
        rounds, catalog = _rounds(frozen_bundle_or_record, cfg, check)
        if catalog is None:
            validate_retrieval(frozen_bundle_or_record)
            validate_graph_audit(frozen_bundle_or_record["graph_audit"], frozen_bundle_or_record)
            check()
        complete_cache, all_entries, entity_seen = [], [], set()
        query = action["query"]
        for round_ in rounds:
            check()
            edges, adjacency, _ = _graph(round_["record"], cfg, check)
            selected = _material_catalog(round_, catalog, cfg, check)
            if cfg.accumulate_rounds:
                for source in selected.values():
                    source["binding"].update(round_index=round_["round_index"], request_id=round_["request_id"])
                    metadata = round_["record"]["sources"][source["binding"]["source_id"]]
                    if metadata.get("metadata_available") is True:
                        source["binding"]["source_path"] = metadata.get("path")
            nodes = set(adjacency)
            nodes.update(round_["record"]["graph_audit"]["selection"].get("mark_frequencies", {}))
            nodes.update(node for source in selected.values() for node in source["material"]["marks"])
            _require(all(_safe_term(node) and _canonical(node) == node for node in nodes), "noncanonical_graph_endpoint")
            receipt["stats"]["nodes"] += len(nodes)
            receipt["stats"]["arcs"] += len(edges) * 2
            if receipt["stats"]["nodes"] > cfg.max_nodes:
                raise _Stop("nodes")
            if receipt["stats"]["arcs"] > cfg.max_arcs:
                raise _Stop("arcs")
            scope = _declared_scope(round_["record"], nodes, edges)
            scope.update(round_index=round_["round_index"], record_sha256=round_["record_sha256"],
                         scope=round_["record"]["scope"])
            receipt["graph_scopes"].append(scope)
            mapped = {}
            for entity in query["entities"]:
                row = _map(entity["canonical"], nodes, cfg.aliases)
                row.update(kind="entity", entity_id=entity["id"], round_index=round_["round_index"])
                receipt["mapping"].append(row)
                mapped[entity["canonical"]] = row["graph_endpoint"]
                if row["status"] == "mapped":
                    entity_seen.add(entity["id"])
            for term in receipt["baseline_terms"]:
                row = _map(term, nodes, cfg.aliases)
                row.update(kind="canonical_term", entity_id=None, round_index=round_["round_index"])
                receipt["term_mapping"].append(row)
            entry = (round_, edges, adjacency, selected, mapped, scope)
            all_entries.append(entry)
            if all(mapped[entity["canonical"]] is not None for entity in query["entities"]):
                complete_cache.append(entry)
            else:
                gap("proposed_endpoint_not_in_this_frozen_round", round_["round_index"])
        version_conflicts, bad_uids, unavailable_source_paths = [], set(), set()
        if cfg.accumulate_rounds:
            versions = {}
            for round_, _, _, selected, _, _ in all_entries:
                for source in selected.values():
                    check()
                    groups, identity = _source_version_groups(round_, source)
                    if (len(rounds) > 1 and source["binding"]["source_version"]["kind"] == "knowledge_normalized_text"
                            and not any(group[0] == "source_path" for group in groups)):
                        uid = source["binding"]["evidence_uid"]
                        bad_uids.add(uid)
                        unavailable_source_paths.add(uid)
                        gap("knowledge_source_path_unavailable_for_accumulation", round_["round_index"])
                    for group in groups:
                        versions.setdefault(group, {}).setdefault(identity, []).append(source["binding"])
            for group, choices in sorted(versions.items(), key=lambda item: repr(item[0])):
                if len(choices) > 1:
                    bindings = [row for rows in choices.values() for row in rows]
                    if (group[0] == "ambiguous_source_path_hint"
                            and len({row.get("source_path") for row in bindings}) <= 1):
                        continue
                    bad_uids.update(row["evidence_uid"] for row in bindings)
                    version_conflicts.append({"identity_kind": group[0], "identity": list(group[1:]),
                        "source_versions": [list(version) for version in sorted(choices)],
                        "evidence_uids": sorted({row["evidence_uid"] for row in bindings}),
                        "round_indices": sorted({row["round_index"] for row in bindings}),
                        "semantic_support_verified": False})
                    gap("source_path_identity_ambiguous" if group[0] == "ambiguous_source_path_hint"
                        else "conflicting_frozen_source_versions")
            receipt["accumulation_scope"] = {
                "kind": "current_task_authorized_frozen_evidence_union_v1",
                "scope": rounds[0]["record"]["scope"], "parent_evidence_sha256": parent_hash,
                "rounds": [{key: round_[key] for key in ("round_index", "request_id", "planning_call_id", "record_sha256")}
                           for round_ in rounds],
                "source_version_conflicts": version_conflicts,
                "unavailable_source_path_evidence_uids": sorted(unavailable_source_paths),
                "same_round_body_qualification_required": True, "later_body_backfill_allowed": False,
                "complete": False, "corpus_completeness_verified": False}
        if not complete_cache and not cfg.accumulate_rounds:
            reason = ("proposed_endpoint_not_in_frozen_graph"
                if entity_seen != {entity["id"] for entity in query["entities"]}
                else "no_complete_same_round_endpoint_mapping")
            gap(reason)
            if len(rounds) > 1:
                gap("cross_round_path_not_implemented")
            receipt.update(status="unknown", reason=reason, incomplete=True)
        else:
            for round_, edges, adjacency, selected, mapped, scope in (all_entries if cfg.accumulate_rounds else complete_cache):
                index = round_["round_index"]
                for relation_index, proposal in enumerate(needs):
                    check()
                    need = deepcopy(proposal)
                    need["from"], need["to"] = mapped[proposal["from"]], mapped[proposal["to"]]
                    requirement_index = len(receipt["requirements"])
                    requirement = {"round_index": index, "relation_index": relation_index,
                        "proposal": proposal, "mapped_proposal": need, "candidate_indices": [],
                        "status": "unknown", "reasons": [], "proof": False}
                    receipt["requirements"].append(requirement)
                    paths = []
                    if need["from"] is not None and need["to"] is not None:
                        left = _paths(need["from"], adjacency, (cfg.max_depth + 1) // 2, cfg, check, receipt["stats"])
                        right = _paths(need["to"], adjacency, cfg.max_depth // 2, cfg, check, receipt["stats"])
                        seen = set()
                        for meeting in sorted(left.keys() & right.keys()):
                            for a in left[meeting]:
                                for b in right[meeting]:
                                    check()
                                    path = a + tuple(reversed(b[:-1]))
                                    if path not in seen and 0 < len(path) - 1 <= cfg.max_depth and len(set(path)) == len(path):
                                        if len(paths) + len(receipt["candidates"]) >= cfg.max_candidates:
                                            raise _Stop("candidates")
                                        seen.add(path)
                                        paths.append((path, meeting))
                    for path, meeting in paths:
                        if len(receipt["candidates"]) >= cfg.max_candidates:
                            raise _Stop("candidates")
                        prospective = dict(round_, query_call_binding="prospective_parent_evidence")
                        candidate = _candidate(path, meeting, need, prospective, edges, selected, cfg, check, receipt["stats"])
                        qualifications = [edge_qualification(index, tuple(sorted((a, b))), edges, selected)
                                          for a, b in zip(path, path[1:])]
                        candidate.update(demand_planning_call_id=planning_call_id, demand_action_sha256=action_hash,
                            parent_evidence_sha256=parent_hash, edge_ids=[x["edge_id"] for x in qualifications],
                            structural_support_qualified=all(x["qualified"] for x in qualifications),
                            qualification_reasons=sorted({r for x in qualifications for r in x["reasons"]}),
                            qualified_edge_support=qualifications, formal_derivation=None)
                        if not candidate["structural_support_qualified"]:
                            candidate["reasons"] = sorted(set(candidate["reasons"] + ["structural_support_unqualified"]))
                            candidate["status"] = "unknown"
                        elif candidate["status"] == "pending_hypothesis" and not any(
                                _binding_key(row) in {_binding_key(binding) for qualification in qualifications
                                    for binding in qualification["provenance"]} for row in candidate["edge_bindings"]):
                            candidate["status"] = "unknown"
                            candidate["reasons"] = sorted(set(candidate["reasons"] + ["typed_fact_support_unqualified"]))
                        candidate["formal_derivation"] = _formal_derivation(candidate, cfg, check, receipt["stats"])
                        if cfg.accumulate_rounds and any(row["evidence_uid"] in bad_uids
                                                       for row in candidate["edge_source_bindings"]):
                            candidate["structural_support_qualified"] = False
                            candidate["formal_derivation"] = None
                            candidate["status"] = "unknown"
                            candidate["reasons"] = sorted(set(candidate["reasons"])
                                | {"conflicting_frozen_source_versions"})
                        if candidate["formal_derivation"] is not None:
                            candidate["status"] = "formal_under_declared_rule"
                            candidate["reasons"] = sorted((set(candidate["reasons"])
                                - {"relation_composition_not_authorized"}) | {"composition_only_under_declared_rule"})
                        if cfg.accumulate_rounds and len(rounds) > 1:
                            candidate.update(analysis_scope="each_frozen_round",
                                evaluation_role="round_local_observation",
                                round_local_status=candidate["status"],
                                round_local_formal_derivation=candidate["formal_derivation"],
                                status="unknown", formal_derivation=None)
                            candidate["reasons"] = sorted(set(candidate["reasons"])
                                | {"round_local_only_use_accumulated_result"})
                        candidate_index = len(receipt["candidates"])
                        receipt["candidates"].append(candidate)
                        receipt["stats"]["candidates"] += 1
                        requirement["candidate_indices"].append(candidate_index)
                        if candidate["structural_support_qualified"] and (not cfg.accumulate_rounds or len(rounds) == 1):
                            provenance = [binding for row in qualifications for binding in row["provenance"]]
                            for node in path:
                                clue(node, provenance, candidate_index)
                        for reason in candidate["reasons"]:
                            gap(reason, index, requirement_index)
                    # A supported frontier remains useful even if the complete
                    # proposed path is unavailable or contains unsupported arcs.
                    for endpoint in dict.fromkeys((need["from"], need["to"])):
                        if endpoint is None:
                            continue
                        for neighbor in adjacency.get(endpoint, ()):
                            check()
                            receipt["stats"]["expansions"] += 1
                            if receipt["stats"]["expansions"] > cfg.max_expansions:
                                raise _Stop("expansions")
                            qualification = edge_qualification(index, tuple(sorted((endpoint, neighbor))), edges, selected)
                            if cfg.accumulate_rounds:
                                qualification = deepcopy(qualification)
                                qualification["provenance"] = [row for row in qualification["provenance"]
                                                               if row["evidence_uid"] not in bad_uids]
                                qualification["qualified"] = bool(qualification["provenance"])
                                claim_reasons = _frontier_claim_reasons(endpoint, neighbor, need, qualification, selected)
                                if claim_reasons:
                                    for reason in claim_reasons:
                                        gap(reason, index, requirement_index)
                                    continue
                            if qualification["qualified"]:
                                row = {"term": neighbor, "from_endpoint": endpoint, "round_index": index,
                                    "edge_id": qualification["edge_id"], "provenance": qualification["provenance"],
                                    "unresolved_support_ids": qualification["unresolved_support_ids"],
                                    "semantic_support_verified": False, "purpose": "supported_frontier_gap"}
                                if cfg.accumulate_rounds:
                                    row.update(request_id=round_["request_id"], record_sha256=round_["record_sha256"],
                                        missing_endpoints=[proposal[key] for key in ("from", "to") if need[key] is None],
                                        endpoint_role="prefix_from_known_subject" if endpoint == need["from"] else "suffix_to_known_object",
                                        mapping_complete=need["from"] is not None and need["to"] is not None,
                                        direction_verified=False, proof=False)
                                clue(neighbor, qualification["provenance"], frontier=row)
                                # Explicit aliases can also contribute the
                                # supported start endpoint to real query terms.
                                clue(endpoint, qualification["provenance"])
                    if not paths:
                        if need["from"] is None or need["to"] is None:
                            reason = "proposed_endpoint_not_in_this_frozen_round"
                        elif (scope["declared_complete"] and need["from"] is not None and need["to"] is not None
                                and _component_exhausted(need["from"], adjacency, cfg.max_depth, cfg, check, receipt["stats"])):
                            reason = "declared_full_graph_no_path"
                            requirement["status"] = "structurally_disconnected_under_declaration"
                        else:
                            reason = "no_path_in_frozen_neighborhood" if not scope["declared_complete"] else "depth_cut_unknown"
                        requirement["reasons"].append(reason)
                        gap(reason, index, requirement_index)
                    else:
                        statuses = {receipt["candidates"][i]["status"] for i in requirement["candidate_indices"]}
                        requirement["status"] = ("formal_under_declared_rule" if "formal_under_declared_rule" in statuses
                            else "pending_hypothesis" if "pending_hypothesis" in statuses else
                            "unknown" if "unknown" in statuses else "rejected")
            if cfg.accumulate_rounds and len(rounds) > 1:
                observations, union_adjacency, union_nodes = {}, {}, set()
                for round_, edges, adjacency, selected, _, _ in all_entries:
                    index = round_["round_index"]
                    union_nodes.update(adjacency)
                    union_nodes.update(round_["record"]["graph_audit"]["selection"].get("mark_frequencies", {}))
                    union_nodes.update(node for source in selected.values() for node in source["material"]["marks"])
                    for pair in sorted(edges):
                        check()
                        receipt["stats"]["expansions"] += 1
                        if receipt["stats"]["expansions"] > cfg.max_expansions:
                            raise _Stop("expansions")
                        qualification = deepcopy(edge_qualification(index, pair, edges, selected))
                        qualification["provenance"] = [row for row in qualification["provenance"]
                                                       if row["evidence_uid"] not in bad_uids]
                        qualification["qualified"] = bool(qualification["provenance"])
                        if not qualification["qualified"]:
                            continue
                        observations.setdefault(pair, []).append((round_, edges[pair], selected, qualification))
                        for a, b in (pair, pair[::-1]):
                            union_adjacency.setdefault(a, set()).add(b)
                union_adjacency = {node: tuple(sorted(neighbors)) for node, neighbors in union_adjacency.items()}
                mapped = {entity["canonical"]: _map(entity["canonical"], union_nodes, cfg.aliases)
                          for entity in query["entities"]}
                receipt["accumulation_scope"].update(
                    observed_node_labels=sorted(union_nodes), supported_node_labels=sorted(union_adjacency),
                    qualified_edge_observations=sum(len(rows) for rows in observations.values()),
                    mapping=[dict(row, kind="entity", entity_id=entity["id"])
                             for entity in query["entities"] for row in (mapped[entity["canonical"]],)])
                for relation_index, proposal in enumerate(needs):
                    check()
                    need = deepcopy(proposal)
                    need["from"], need["to"] = mapped[proposal["from"]]["graph_endpoint"], mapped[proposal["to"]]["graph_endpoint"]
                    requirement_index = len(receipt["requirements"])
                    requirement = {"round_index": None, "round_indices": [round_["round_index"] for round_ in rounds],
                        "relation_index": relation_index, "proposal": proposal, "mapped_proposal": need,
                        "analysis_scope": "current_task_frozen_rounds", "candidate_indices": [],
                        "status": "unknown", "reasons": [], "proof": False}
                    receipt["requirements"].append(requirement)
                    paths = []
                    if need["from"] is not None and need["to"] is not None:
                        left = _paths(need["from"], union_adjacency, (cfg.max_depth + 1) // 2, cfg, check, receipt["stats"])
                        right = _paths(need["to"], union_adjacency, cfg.max_depth // 2, cfg, check, receipt["stats"])
                        seen = set()
                        for meeting in sorted(left.keys() & right.keys()):
                            for a in left[meeting]:
                                for b in right[meeting]:
                                    check()
                                    path = a + tuple(reversed(b[:-1]))
                                    if path not in seen and 0 < len(path) - 1 <= cfg.max_depth and len(set(path)) == len(path):
                                        if len(paths) + len(receipt["candidates"]) >= cfg.max_candidates:
                                            raise _Stop("candidates")
                                        seen.add(path)
                                        paths.append((path, meeting))
                    for path, meeting in paths:
                        candidate = _accumulated_candidate(path, meeting, need, observations, cfg, check, receipt["stats"])
                        candidate.update(demand_planning_call_id=planning_call_id, demand_action_sha256=action_hash,
                                         parent_evidence_sha256=parent_hash)
                        candidate["formal_derivation"] = _formal_derivation(candidate, cfg, check, receipt["stats"])
                        if candidate["formal_derivation"] is not None:
                            candidate["status"] = "formal_under_declared_rule"
                            candidate["reasons"] = sorted((set(candidate["reasons"])
                                - {"relation_composition_not_authorized"}) | {"composition_only_under_declared_rule"})
                        candidate_index = len(receipt["candidates"])
                        receipt["candidates"].append(candidate)
                        receipt["accumulated_candidate_indices"].append(candidate_index)
                        requirement["candidate_indices"].append(candidate_index)
                        receipt["stats"]["candidates"] += 1
                        provenance = [binding for row in candidate["qualified_edge_support"] for binding in row["provenance"]]
                        for node in path:
                            clue(node, provenance, candidate_index)
                        for reason in candidate["reasons"]:
                            gap(reason, None, requirement_index)
                    if not paths:
                        reason = ("proposed_endpoint_not_in_accumulated_frozen_graph"
                            if need["from"] is None or need["to"] is None else "no_path_in_accumulated_frozen_neighborhood")
                        requirement["reasons"].append(reason)
                        gap(reason, None, requirement_index)
                    else:
                        statuses = {receipt["candidates"][i]["status"] for i in requirement["candidate_indices"]}
                        requirement["status"] = ("formal_under_declared_rule" if "formal_under_declared_rule" in statuses
                            else "pending_hypothesis" if "pending_hypothesis" in statuses else "unknown")
            if not needs:
                gap("no_relation_proposal")
            receipt["clues"] = list(clues_by_term.values())
            receipt["added_terms"] = list(clues_by_term)
            receipt["effective_terms"] += receipt["added_terms"]
            receipt["incomplete"] = bool(receipt["gaps"])
            receipt["status"] = "ready" if receipt["added_terms"] or any(
                row["status"] in ("pending_hypothesis", "formal_under_declared_rule") for row in receipt["candidates"]) else "unknown"
            receipt["reason"] = "sourced_graph_clues" if receipt["added_terms"] else "no_added_graph_terms"
        check()
    except _Stop as exc:
        cutoff(str(exc))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        receipt.update(status="unknown", reason="invalid_or_unbound_frozen_evidence", incomplete=True)
        gap("invalid_or_unbound_frozen_evidence")
        receipt["effective_terms"] = list(receipt["baseline_terms"])
        receipt["added_terms"], receipt["clues"], receipt["frontier_clues"] = [], [], []
        if cfg.accumulate_rounds:
            for key in ("candidates", "requirements", "mapping", "term_mapping", "graph_scopes", "accumulated_candidate_indices"):
                receipt[key] = []
            receipt["accumulation_scope"] = None
    receipt["reasons"] = sorted({row["reason"] for row in receipt["gaps"]})
    receipt["stats"]["elapsed_seconds"] = max(0.0, time.monotonic() - started)
    try:
        receipt["feedback_sha256"] = _hash(receipt, cfg, check)
    except _Stop as exc:
        # No partly serialized feedback can influence a later retrieval.
        cutoff(str(exc))
        receipt["reasons"] = [str(exc), "partial_receipt_omitted_on_limit"]
        receipt["feedback_sha256"] = digest(receipt)
    return receipt


def path_feedback_summary(receipt):
    """Concise untrusted planner data bound to the complete local receipt.

    This projection carries no quote bodies and at most eight candidate paths,
    sixteen gaps and four clues. Omitted counts remain visible; omission never
    converts an unknown/conflicting observation into semantic certainty.
    """
    _require(type(receipt) is dict and receipt.get("schema") == SCHEMA
             and receipt.get("feedback_sha256") == digest({k: v for k, v in receipt.items() if k != "feedback_sha256"}),
             "invalid_path_feedback_hash")
    candidates = receipt["candidates"][:8]
    clues = receipt["clues"][:4]
    return {"schema": "path_requery_planner_feedback_v1", "version": 1, "policy": POLICY,
        "feedback_sha256": receipt["feedback_sha256"], "demand_binding": deepcopy(receipt["demand_binding"]),
        "status": receipt["status"], "reason": receipt["reason"],
        "effective_terms": list(receipt["effective_terms"]), "added_terms": list(receipt["added_terms"]),
        "gaps": deepcopy(receipt["gaps"][:16]), "omitted_gaps": max(0, len(receipt["gaps"]) - 16),
        "hypotheses": [{"path_nodes": list(row["path_nodes"]), "status": row["status"],
            "reasons": list(row["reasons"]), "edge_ids": list(row["edge_ids"]),
            "citation_ids": sorted({x["citation_id"] for x in row["edge_source_bindings"]}),
            "rule_ids": list(row["formal_derivation"]["rule_ids"]) if row["formal_derivation"] else [],
            "semantic_support_verified": False} for row in candidates],
        "omitted_hypotheses": max(0, len(receipt["candidates"]) - 8),
        "clues": [{"term": row["term"], "evidence_uids": sorted({x["evidence_uid"] for x in row["provenance"]})[:8],
            "citation_ids": sorted({x["citation_id"] for x in row["provenance"]})[:8],
            "omitted_provenance": max(0, len(row["provenance"]) - 8), "semantic_support_verified": False} for row in clues],
        "proof": False, "semantic_support_verified": False, "truth_verified": False}
