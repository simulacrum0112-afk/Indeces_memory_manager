"""Versioned Richard chat receipts; old model-selection v1 remains exact.

Uses the unchanged transport verifier and verifies retained raw output separately
from derived native selection. No I/O, graph execution or semantic certification.
Book: Bojie Li v2.0 September23 2026 A4/A5 pp127-129/PDF135-137;
S7 pp111-113/PDF119-121; E1/E4/E8 pp232-233,243-246,256-257.
"""
from __future__ import annotations
import math
import re
from .run_records import (_require, _model_selection_json, _validate_call,
                          _validate_partial_call, _transport_protocol, _validate_native_selection_requests,
                          _CLAUDE_COUNT, _CLAUDE_GENERATE,
                          _MODEL_SELECTION_BINDING, canonical, digest, text_digest)

_MODEL_SELECTION_POLICY = {"id": "richard_chat_v11", "version": "v2"}
_HISTORICAL_POLICY = {"id": "richard_chat_v11", "version": "v1"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BASE_PAYLOAD = {"query", "context_query", "ranking_mode", "retrieval_policy",
                 "required_selection_count", "candidate_pool_count", "model_visible_count",
                 "model_omitted_count", "candidates"}
_EXTRA_PAYLOAD = {"richard_catalog", "allowed_targets", "richard_algorithm", "graph_identity",
                  "richard_plan_schema", "richard_binding_sha256"}
_DIAG_KEYS = {"schema", "policy", "input_sha256", "response_id", "raw_model_result_sha256",
              "fallback_record_ids", "final_record_ids", "exclusions", "ranker_enabled",
              "ranker_called", "ranker_applied", "fallback_used", "reason", "graph_identity",
              "algorithm", "variant", "parsed_plan_sha256", "catalog_sha256", "binding_snapshot",
              "binding_snapshot_sha256", "ranked_concepts", "ranked_concepts_sha256",
              "native_ranked_record_ids", "reducer", "resolved_seeds"}
_LOCAL_DIGEST = "local_input_audit_sha256"
_CATALOG_KEYS = {"term", "start", "end"}

def _policy(policy):
    _require(type(policy) is dict and policy in (_MODEL_SELECTION_POLICY, _HISTORICAL_POLICY),
             "unknown Richard selection policy")
    return policy["version"]

def is_richard_trace(events, start=None, graph=None):
    values = [start or {}, *(f for _, f in events)]
    for event, fields in events:
        record = fields.get("record", {}) if isinstance(fields, dict) else {}
        retained = record.get("graph_audit") if isinstance(record, dict) else None
        if retained is None and event == "memory_observation":
            retained = fields.get("audit")
        if isinstance(retained, dict):
            values.append(retained.get("selection", {}).get("selector_receipt", {}))
    if graph is not None:
        values.append(graph.get("selection", {}).get("selector_receipt", {}))
    return any(e == "richard_chat_selection" for e, _ in events) or any(
        v.get("model_selection_policy", v.get("policy", {})).get("id") == "richard_chat_v11"
        for v in values if isinstance(v, dict) and isinstance(v.get("model_selection_policy", v.get("policy", {})), dict))

def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None

def _ids(value):
    return type(value) is list and all(type(v) is int and v > 0 for v in value) and len(set(value)) == len(value)

def _binding(fields):
    version = _policy(fields["policy"])
    keys = (*_MODEL_SELECTION_BINDING, _LOCAL_DIGEST) if version == "v2" else _MODEL_SELECTION_BINDING
    _require(version == "v2" or not ({_LOCAL_DIGEST, "local_input_audit"} & set(fields)),
             "historical Richard local audit fields invalid")
    _require(all(key in fields for key in keys), "Richard binding fields missing")
    binding = {key: fields[key] for key in keys}
    _require(all(type(binding[k]) is str and binding[k] for k in ("scope", "event_id")), "Richard scope/event invalid")
    _require(all(_hash(binding[k]) for k in binding if k.endswith("sha256")), "Richard binding hash invalid")
    _require(binding["input_sha256"] == digest({k:v for k,v in binding.items() if k != "input_sha256"}), "Richard input digest mismatch")
    seen, omitted = binding["seen_record_ids"], binding["omitted_record_ids"]
    _require(_ids(seen) and _ids(omitted) and not set(seen) & set(omitted)
             and type(binding["candidate_count"]) is int and binding["candidate_count"] == len(seen)+len(omitted),
             "Richard candidate partition invalid")
    return binding

def _validate_input_contract(payload, schema, required, *, policy=None):
    from .model_selection import ModelNPMILabelSelector
    from .richard_chat_selection import richard_schema, _spans
    _require(type(payload) is dict, "Richard payload invalid")
    version = _policy(_MODEL_SELECTION_POLICY if policy is None else policy)
    if set(payload) == _BASE_PAYLOAD:
        _require(canonical(schema) == canonical(ModelNPMILabelSelector._schema(required)), "Richard fallback schema mismatch")
        return
    _require(set(payload) == _BASE_PAYLOAD | _EXTRA_PAYLOAD, "Richard enriched payload fields mismatch")
    _require(payload["richard_algorithm"] in ("alpha", "beta") and _hash(payload["graph_identity"])
             and _hash(payload["richard_binding_sha256"]) and payload["richard_plan_schema"] == "richard_chat_plan_v1", "Richard input configuration invalid")
    catalog, targets, query = payload["richard_catalog"], payload["allowed_targets"], payload["query"]
    _require(type(query) is str and type(catalog) is list and len(catalog) <= 32
             and type(targets) is list and len(targets) <= 32, "Richard catalog bounds invalid")
    for row in catalog:
        _require(type(row) is dict and set(row) in ((_CATALOG_KEYS,) if version == "v2"
                                                   else (_CATALOG_KEYS, _CATALOG_KEYS | {"cid"}))
                 and type(row["term"]) is str and row["term"]
                 and ("cid" not in row or type(row["cid"]) is int and row["cid"] > 0)
                 and type(row["start"]) is int and type(row["end"]) is int
                 and 0 <= row["start"] < row["end"] <= len(query)
                 and query[row["start"]:row["end"]] == row["term"]
                 and (row["start"], row["end"]) in _spans(query, row["term"]), "Richard catalog anchor invalid")
    _require(len({(r["term"],r["start"],r["end"]) for r in catalog}) == len(catalog), "Richard catalog duplicated")
    _require(not catalog or all(set(row) == set(catalog[0]) for row in catalog),
             "Richard catalog mixed historical shape")
    if catalog and "cid" in catalog[0]:
        _require(all(len({r["cid"] for r in catalog if r["term"] == term}) == 1
                     for term in {r["term"] for r in catalog}), "Richard catalog concept mismatch")
    for row in targets:
        _require(type(row) is dict and set(row) == {"target", "level", "phrase", "start", "end"}
                 and type(row["target"]) is str and row["target"] and row["level"] in ("fine", "coarse")
                 and type(row["phrase"]) is str and row["phrase"]
                 and type(row["start"]) is int and type(row["end"]) is int
                 and 0 <= row["start"] < row["end"] <= len(query)
                 and query[row["start"]:row["end"]] == row["phrase"], "Richard target anchor invalid")
    # Base chooser builds schema before its deterministic visibility budget.
    # Superset seed enums remain bound by the actual schema hash and all seed
    # admission is additionally checked against the visible catalog below.
    try:
        terms = schema["properties"]["richard_plan"]["properties"]["seeds_A"]["items"]["properties"]["term"]["enum"]
    except (KeyError, TypeError):
        raise ValueError("Richard plan schema invalid") from None
    _require(type(terms) is list and len(terms) <= 32 and all(type(t) is str for t in terms)
             and len(set(terms)) == len(terms) and set(r["term"] for r in catalog) <= set(terms), "Richard schema seed enum invalid")
    _require(canonical(schema) == canonical(richard_schema(required, [{"term":t} for t in terms], targets)), "Richard plan schema mismatch")

def _validate_local_input_audit(fields, binding, payload):
    """Check local retained proof separately from the exact provider payload.

    This validates retained-record consistency. It never reloads a graph or
    establishes authenticity after arbitrary recomputation of every receipt.
    """
    if _policy(binding["policy"]) == "v1":
        return None
    local = fields.get("local_input_audit")
    _require(type(local) is dict and set(local) == {"schema", "graph_identity",
             "binding_snapshot_sha256", "catalog_cids"}
             and local["schema"] == "richard_chat_local_cids_v1"
             and _hash(local["graph_identity"]) and _hash(local["binding_snapshot_sha256"]),
             "Richard local input audit invalid")
    _require(digest(local) == binding[_LOCAL_DIGEST], "Richard local input audit digest mismatch")
    rows = local["catalog_cids"]
    _require(type(rows) is list and len(rows) <= 32, "Richard local CID catalog bounds invalid")
    for row in rows:
        _require(type(row) is dict and set(row) == _CATALOG_KEYS | {"cid"}
                 and type(row["term"]) is str and bool(row["term"])
                 and type(row["start"]) is int and type(row["end"]) is int
                 and type(row["cid"]) is int and row["cid"] > 0,
                 "Richard local CID catalog row invalid")
    _require([{key:row[key] for key in _CATALOG_KEYS} for row in rows]
             == payload.get("richard_catalog", []), "Richard local CID catalog visible anchors mismatch")
    _require(all(len({r["cid"] for r in rows if r["term"] == term}) == 1
                 for term in {r["term"] for r in rows}), "Richard local CID catalog concept mismatch")
    if "richard_catalog" in payload:
        _require(local["graph_identity"] == payload["graph_identity"]
                 and local["binding_snapshot_sha256"] == payload["richard_binding_sha256"],
                 "Richard local input pre-call graph/binding mismatch")
    return local

def _validate_raw_output(data, binding, required):
    _require(type(data) is dict and set(data) in ({"selected_record_ids"}, {"selected_record_ids", "richard_plan"}), "Richard raw output fields invalid")
    _require(_ids(data["selected_record_ids"]) and len(data["selected_record_ids"]) == required
             and set(data["selected_record_ids"]) <= set(binding["seen_record_ids"]), "Richard raw fallback IDs invalid")


def _validate_partial_transport(events, report, performed):
    """A reported ModelResult cannot replace retained provider evidence."""
    if not performed:
        return
    starts = [f for e,f in events if e == "call_start"]
    ends = [f for e,f in events if e == "call_end" and f.get("status") == "completed"]
    requests = [f.get("path") for e,f in events if e == "http_request"]
    responses = [f.get("path") for e,f in events if e == "http_response"]
    gates = [f for e,f in events if e == "input_gate"]
    native = _transport_protocol(events) == "anthropic"
    paths = [_CLAUDE_COUNT, _CLAUDE_GENERATE] if native else ["/responses/input_tokens", "/responses"]
    if len(starts) == len(ends) == len(gates) == 1 and requests == responses == paths:
        _require(_validate_call(events) == "complete", "Richard retained transport incomplete")
    else:
        if native:
            _validate_partial_call(events)
        report["warnings"].append("expired_richard_chat_transport")


def validate_model_selection(events, report, *, start=None, graph=None, partial=False,
                              allow_budget_narrowing=False):
    """Bind one bounded model choice to its input, transport and frozen receipt.

    Available retained evidence is checked, but expired input/call/material
    evidence never establishes a complete model-selection traversal.
    """
    inputs = [f for e, f in events if e == "model_selection_input"]
    decisions = [f for e, f in events if e == "model_selection_decision"]
    call_fields = [f for _, f in events if f.get("stage") == "selection"]
    marker = start.get("model_selection_policy") if start is not None else None
    receipt = graph.get("selection", {}).get("selector_receipt") if graph else None
    model_receipt = receipt is not None and receipt.get("policy", {}).get("id") == "richard_chat_v11"
    active = marker is not None or bool(inputs or decisions or call_fields) or model_receipt
    if not active:
        return False
    _require(len(inputs) <= 1 and len(decisions) <= 1, "duplicate model selection stage")
    if marker is not None:
        _policy(marker)
    ends = [f for e, f in events if e == "turn_end"]
    successful = bool(ends and ends[0].get("status") in ("delivered", "skipped"))
    if not inputs and not decisions and not call_fields and graph is None and not successful:
        # Retrieval may fail before choose writes any input. An unfinished or
        # failed turn does not establish that model selection was performed.
        return True
    if graph is not None:
        _require(model_receipt, "model selection receipt missing or downgraded")
        _policy(receipt["policy"])
    retained_policies = ([marker] if marker is not None else []) + ([receipt["policy"]] if receipt is not None else [])
    retained_policies += [fields["policy"] for event,fields in events if event in
                          ("model_selection_input", "model_selection_decision", "richard_chat_selection")]
    for policy in retained_policies:
        _policy(policy)
    _require(not retained_policies or all(policy == retained_policies[0] for policy in retained_policies),
             "Richard selection policy version mismatch")
    bindings = [_binding(fields) for fields in inputs + decisions]
    _require(not bindings or all(binding == bindings[0] for binding in bindings),
             "model selection decision input mismatch")
    binding = bindings[0] if bindings else None
    if binding is not None:
        _require((marker is None or marker == binding["policy"])
                 and (receipt is None or receipt["policy"] == binding["policy"]),
                 "Richard selection policy version mismatch")
    _require(all("local_input_audit" not in fields for fields in decisions),
             "Richard decision contains local audit object")
    if start is not None and binding is not None:
        _require(binding["scope"] == start["knowledge_scope"]
                 and binding["event_id"] == start["message_id"]
                 and binding["query_sha256"] == text_digest(start["input"]["text"]),
                 "model selection turn identity mismatch")
    if receipt is not None and binding is not None:
        request, candidate_set = receipt["input"], receipt["candidate_set"]
        _require(all(binding[key] == request[key] for key in (
            "scope", "event_id", "query_sha256", "context_query_sha256")),
            "model selection frozen input mismatch")
        inventory = candidate_set["inventory"]
        seen_count = len(binding["seen_record_ids"])
        _require(binding["candidate_count"] == len(inventory)
                 and binding["candidate_set_sha256"] == candidate_set["sha256"]
                 and binding["seen_record_ids"] == [item["record_id"] for item in inventory[:seen_count]]
                 and binding["omitted_record_ids"] == [item["record_id"] for item in inventory[seen_count:]]
                 and binding["seen_candidates_sha256"] == digest(inventory[:seen_count])
                 and binding["omitted_candidates_sha256"] == digest(inventory[seen_count:]),
                 "model selection candidate inventory mismatch")
    if inputs:
        fields = inputs[0]
        messages, instructions, schema = fields["messages"], fields["instructions"], fields["response_schema"]
        _require(type(instructions) is str and bool(instructions)
                 and text_digest(instructions) == binding["instructions_sha256"]
                 and type(messages) is list and len(messages) == 1
                 and type(messages[0]) is dict and set(messages[0]) == {"role", "content"}
                 and messages[0]["role"] == "user" and type(messages[0]["content"]) is str
                 and digest(messages) == binding["messages_sha256"]
                 and digest(schema) == binding["schema_sha256"],
                 "model selection prompt/schema digest mismatch")
        required = min(3, binding["candidate_count"])
        payload = _model_selection_json(messages[0]["content"], "Richard selection payload invalid")
        _require(type(fields["required_selection_count"]) is int
                 and fields["required_selection_count"] == required,
                 "Richard required selection count mismatch")
        _validate_input_contract(payload, schema, required, policy=binding["policy"])
        _validate_local_input_audit(fields, binding, payload)
        _require(type(fields["planning_reservation"]) is int and fields["planning_reservation"] >= 0
                 and type(fields["planning_input_limit"]) is int and fields["planning_input_limit"] > 0
                 and fields["omission_reason"] == ("input_budget" if binding["omitted_record_ids"] else None),
                 "model selection planning declaration invalid")
        payload = _model_selection_json(messages[0]["content"], "model selection payload invalid")
        _validate_input_contract(payload, schema, required, policy=binding["policy"])
        _require(type(payload["query"]) is str and type(payload["context_query"]) is str
                 and text_digest(payload["query"]) == binding["query_sha256"]
                 and text_digest(payload["context_query"]) == binding["context_query_sha256"]
                 and payload["ranking_mode"] == "static"
                 and payload["retrieval_policy"] in ("concept_v1", "legacy_v1")
                 and all(type(payload[key]) is int for key in ("required_selection_count", "candidate_pool_count",
                                                              "model_visible_count", "model_omitted_count"))
                 and payload["required_selection_count"] == required
                 and payload["candidate_pool_count"] == binding["candidate_count"]
                 and payload["model_visible_count"] == len(binding["seen_record_ids"])
                 and payload["model_omitted_count"] == len(binding["omitted_record_ids"]),
                 "model selection payload identity mismatch")
        rows = payload["candidates"]
        row_keys = {"record_id", "rank", "marks", "direct_marks", "expanded_marks", "direct_match_count",
                    "static_score", "effective_score", "ranking_score", "relevance_score"}
        _require(type(rows) is list and all(type(row) is dict and set(row) == row_keys for row in rows)
                 and [row["record_id"] for row in rows] == binding["seen_record_ids"],
                 "model selection visible labels mismatch")
        if receipt is not None:
            _require(payload["ranking_mode"] == request["ranking_mode"]
                     and payload["retrieval_policy"] == request["retrieval_policy"]
                     and payload["context_query"] == request["context_query"],
                     "model selection frozen query mismatch")
            expected_rows = [{**{key: item[key] for key in row_keys if key != "relevance_score"},
                              "relevance_score": item["ranking_score"]
                                  if request["retrieval_policy"] == "concept_v1" else None}
                             for item in inventory[:len(rows)]]
            _require(canonical(rows) == canonical(expected_rows), "model selection visible scores/marks mismatch")
    selection_call_ids = {fields["call_id"] for fields in call_fields}
    _require(len(selection_call_ids) <= 1, "multiple model selection calls")
    selection_events = [(e, f) for e, f in events if f.get("call_id") in selection_call_ids]
    starts = [f for e, f in selection_events if e == "call_start"]
    completed = [f for e, f in selection_events if e == "call_end" and f.get("status") == "completed"]
    _require(len(starts) <= 1 and len(completed) <= 1
             and all(f.get("stage") in (None, "selection") for _, f in selection_events),
             "model selection call stage mismatch")
    if completed and not partial:
        _require(_validate_call(selection_events) == "complete", "model selection call evidence incomplete")
    if partial:
        claimed_call = bool(completed or any(d.get("model_call_performed") is True for d in decisions)
                            or (receipt is not None and receipt.get("selected_record_ids")))
        _validate_partial_transport(selection_events, report, claimed_call)
    if not partial:
        _require(len(inputs) == 1, "model selection input missing")
        _require(not selection_events or len(starts) == 1, "model selection call start missing")
    if inputs and starts:
        _require(starts[0]["planning_reservation"] == inputs[0]["planning_reservation"]
                 and (0 < starts[0]["budget"]["input_tokens"] <= inputs[0]["planning_input_limit"]
                      if allow_budget_narrowing else
                      starts[0]["budget"]["input_tokens"] == inputs[0]["planning_input_limit"]),
                 "model selection call planning mismatch")
    requests = [f for e, f in selection_events if e == "http_request"]
    _require(len(requests) <= 2 and len({f["path"] for f in requests}) == len(requests),
             "duplicate model selection transport")
    native = _transport_protocol(selection_events) == "anthropic"
    if native:
        _validate_native_selection_requests(selection_events, binding, inputs, report, partial=partial)
    else:
        for fields in requests:
            _require(fields["path"] in ("/responses/input_tokens", "/responses"),
                     "unknown model selection transport")
            if binding is not None:
                payload = fields["payload"]
                form = payload.get("text", {}).get("format")
                _require(digest(payload["input"]) == binding["messages_sha256"]
                         and text_digest(payload["instructions"]) == binding["instructions_sha256"]
                         and type(form) is dict and set(form) == {"type", "name", "strict", "schema"}
                         and form["type"] == "json_schema" and form["name"] == "selection"
                         and form["strict"] is True and digest(form["schema"]) == binding["schema_sha256"],
                         "actual model selection prompt digest mismatch")
    if inputs:
        expected_format = {"type": "json_schema", "name": "selection", "strict": True,
                           "schema": inputs[0]["response_schema"]}
        for fields in (requests if not native else []):
            payload = fields["payload"]
            _require(fields["path"] in ("/responses/input_tokens", "/responses")
                     and payload["input"] == inputs[0]["messages"]
                     and payload["instructions"] == inputs[0]["instructions"]
                     and canonical(payload.get("text", {}).get("format")) == canonical(expected_format),
                     "actual model selection input/schema mismatch")
        input_position = next(i for i, (e, _) in enumerate(events) if e == "model_selection_input")
        _require(all(i > input_position for i, (_, f) in enumerate(events)
                     if f.get("call_id") in selection_call_ids), "model selection input ordering mismatch")
    if successful and not partial:
        _require(len(decisions) == 1 and decisions[0]["status"] in ("selected", "no_candidates")
                 and model_receipt, "successful turn model selection evidence missing")
    if decisions:
        decision = decisions[0]
        status, selected = decision["status"], decision["selected_record_ids"]
        _require(type(decision["model_call_performed"]) is bool and type(selected) is list
                 and all(type(record_id) is int for record_id in selected)
                 and len(set(selected)) == len(selected) and set(selected) <= set(binding["seen_record_ids"]),
                 "model selection decision invalid")
        _require(status in ("selected", "no_candidates", "failed"), "unknown model selection decision status")
        if status == "no_candidates":
            _require(binding["candidate_count"] == 0 and selected == [] and decision["exclusions"] == []
                     and decision["model_call_performed"] is False and not selection_events
                     and decision["reason"] == "empty_candidate_pool", "empty model selection decision mismatch")
        elif status == "selected":
            _require(binding["candidate_count"] > 0 and len(selected) == min(3, binding["candidate_count"])
                     and decision["model_call_performed"] is True
                     and (not inputs or inputs[0]["planning_reservation"] <= inputs[0]["planning_input_limit"]),
                     "model selected count/call mismatch")
            expected_exclusions = [{"record_id": record_id,
                "reason": "model_not_selected" if record_id in binding["seen_record_ids"] else "input_budget"}
                for record_id in binding["seen_record_ids"] + binding["omitted_record_ids"] if record_id not in selected]
            _require(canonical(decision["exclusions"]) == canonical(expected_exclusions),
                     "model selection exclusions mismatch")
        else:
            _require(not selected and not successful, "failed model selection has successful answer")
            if decision["code"] == "selection_input_budget":
                _require(decision["model_call_performed"] is False and not selection_events
                         and (len(binding["seen_record_ids"]) < min(3, binding["candidate_count"])
                              or (inputs and inputs[0]["planning_reservation"] > inputs[0]["planning_input_limit"])),
                         "model selection budget failure mismatch")
            else:
                _require(decision["code"] == "invalid_model_selection"
                         and decision["model_call_performed"] is True, "unknown model selection failure")
        if decision["model_call_performed"]:
            _require(partial or len(completed) == 1, "model selection result call evidence missing")
            _require(decision["response_id"] == decision["model_result"]["response_id"],
                     "model selection response identity mismatch")
            if completed:
                _require(canonical(decision["model_result"]) == canonical(completed[0]["result"]),
                         "model selection result receipt mismatch")
            if status == "selected":
                data = _model_selection_json(decision["model_result"]["text"], "model selection output invalid")
                _validate_raw_output(data, binding, required=min(3, binding["candidate_count"]))
        if not partial:
            _require(decision["model_call_performed"] == bool(selection_events),
                     "model selection call declaration mismatch")
        decision_position = next(i for i, (e, _) in enumerate(events) if e == "model_selection_decision")
        _require(all(i < decision_position for i, (_, f) in enumerate(events)
                     if f.get("call_id") in selection_call_ids), "model selection decision ordering mismatch")
        if receipt is not None:
            _require(status != "failed" and receipt["selected_record_ids"] == selected
                     and canonical(receipt["exclusions"]) == canonical(decision["exclusions"]),
                     "model selection frozen decision mismatch")
    elif completed and not partial:
        _require(not successful and not model_receipt, "completed model selection decision missing")
    elif completed and model_receipt:
        data = _model_selection_json(completed[0]["result"]["text"], "retained model selection output invalid")
        if binding is not None:
            _validate_raw_output(data, binding, required=min(3, binding["candidate_count"]))
        else:
            # An expired pre-call partition cannot be recovered from the
            # response or final selection; retain only its narrower shape.
            _require(partial and type(data) is dict
                     and set(data) in ({"selected_record_ids"}, {"selected_record_ids", "richard_plan"})
                     and _ids(data["selected_record_ids"])
                     and len(data["selected_record_ids"]) == min(3, len(receipt["candidate_set"]["inventory"])),
                     "Richard retained raw selection shape invalid")
            report["warnings"].append("expired_richard_chat_raw_visible_binding")
    _validate_diagnostic(events, report, inputs=inputs, decisions=decisions, binding=binding,
                         receipt=receipt, partial=partial, completed=completed, successful=successful)
    if partial:
        if not inputs:
            report["warnings"].append("expired_model_selection_input")
        if not decisions:
            report["warnings"].append("expired_model_selection_decision")
        if graph is None:
            report["warnings"].append("expired_model_selection_material_binding")
    return True

def _validate_diagnostic(events, report, *, inputs, decisions, binding, receipt,
                         partial, completed, successful):
    diagnostics = [f for e,f in events if e == "richard_chat_selection"]
    _require(len(diagnostics) <= 1, "duplicate Richard diagnostic")
    selected_decision = next((d for d in decisions if d.get("status") in ("selected", "no_candidates")), None)
    if not diagnostics:
        if any(d.get("status") == "failed" for d in decisions) and not successful and receipt is None:
            return  # Known model failure occurs before a success diagnostic.
        failed_after_model = any(e == "turn_end" and f.get("status") == "failed"
                                  and f.get("code") in ("local_memory_timeout", "turn_timeout") for e,f in events)
        if failed_after_model and completed and not successful and receipt is None and selected_decision is None:
            return  # Confirmed call is checked above; deadline prevents local commit.
        if selected_decision is not None or receipt is not None or completed:
            _require(partial, "Richard diagnostic missing")
            report["warnings"].append("expired_richard_chat_selection")
        return
    diag = diagnostics[0]
    version = _policy(diag["policy"])
    keys = _DIAG_KEYS | {_LOCAL_DIGEST} if version == "v2" else _DIAG_KEYS
    _require(set(diag) - {"trace_id"} == keys
             and diag["schema"] == "richard_chat_selection_" + version,
             "Richard diagnostic fields/policy invalid")
    if version == "v2":
        _require(_hash(diag[_LOCAL_DIGEST]), "Richard diagnostic local input digest invalid")
    _require(all(_hash(diag[k]) for k in ("input_sha256", "graph_identity", "parsed_plan_sha256", "catalog_sha256",
                                         "binding_snapshot_sha256", "ranked_concepts_sha256")), "Richard diagnostic hash invalid")
    _require(diag["algorithm"] in ("alpha", "beta") and diag["reducer"] == "ranked_cid_then_native_rank_v1", "Richard algorithm/reducer invalid")
    _require(all(type(diag[k]) is bool for k in ("ranker_enabled", "ranker_called", "ranker_applied", "fallback_used"))
             and diag["ranker_enabled"] and diag["fallback_used"] is not diag["ranker_applied"]
             and (not diag["ranker_applied"] or diag["ranker_called"]), "Richard flags invalid")
    _require(type(diag["reason"]) is str and _REASON.fullmatch(diag["reason"]), "Richard reason invalid")
    seeds = diag["resolved_seeds"]
    _require(type(seeds) is dict and set(seeds) == {"namespace", "A", "B", "normalization"}
             and seeds["namespace"] == "concept_cid"
             and seeds["normalization"] == "exact_release_name_to_cid_then_sorted_dedup"
             and all(_ids(seeds[k]) and seeds[k] == sorted(seeds[k]) for k in ("A", "B")),
             "Richard resolved seed namespace/order invalid")
    _require(_ids(diag["fallback_record_ids"]) and _ids(diag["final_record_ids"])
             and _ids(diag["native_ranked_record_ids"]), "Richard native ID list invalid")
    if binding is not None:
        _require(diag["policy"] == binding["policy"]
                 and diag["input_sha256"] == binding["input_sha256"], "Richard diagnostic input mismatch")
        if version == "v2":
            _require(diag[_LOCAL_DIGEST] == binding[_LOCAL_DIGEST],
                     "Richard diagnostic local input digest mismatch")
        _require(set(diag["fallback_record_ids"]) <= set(binding["seen_record_ids"])
                 and set(diag["final_record_ids"]) <= set(binding["seen_record_ids"])
                 and set(diag["native_ranked_record_ids"]) <= set(binding["seen_record_ids"]), "Richard diagnostic omitted/foreign IDs")
    else:
        report["warnings"].append("expired_richard_chat_input_binding")
    if selected_decision is not None:
        _require(diag["final_record_ids"] == selected_decision["selected_record_ids"]
                 and canonical(diag["exclusions"]) == canonical(selected_decision["exclusions"]), "Richard diagnostic final decision mismatch")
    elif completed:
        _require(partial, "Richard retained decision missing")
        report["warnings"].append("expired_richard_chat_decision")
    if receipt is not None:
        _require(diag["final_record_ids"] == receipt["selected_record_ids"]
                 and canonical(diag["exclusions"]) == canonical(receipt["exclusions"]), "Richard diagnostic frozen selection mismatch")
    payload = _model_selection_json(inputs[0]["messages"][0]["content"], "Richard retained input invalid") if inputs else None
    result = selected_decision.get("model_result") if selected_decision is not None else completed[0]["result"] if completed else None
    if result is not None:
        _require(diag["raw_model_result_sha256"] == digest(result)
                 and diag["response_id"] == result["response_id"], "Richard raw result binding mismatch")
        raw = _model_selection_json(result["text"], "Richard raw response invalid")
        _require(type(raw) is dict and set(raw) in ({"selected_record_ids"}, {"selected_record_ids", "richard_plan"})
                 and raw["selected_record_ids"] == diag["fallback_record_ids"], "Richard raw fallback mismatch")
        plan = raw.get("richard_plan")
    elif selected_decision is not None and selected_decision["status"] == "no_candidates":
        _require(diag["raw_model_result_sha256"] is None and diag["response_id"] is None
                 and not diag["fallback_record_ids"] and not diag["final_record_ids"], "Richard empty-pool result fabricated")
        plan = None
    else:
        _require(partial, "Richard raw result missing")
        _require(diag["raw_model_result_sha256"] is None or _hash(diag["raw_model_result_sha256"]), "Richard retained raw hash invalid")
        report["warnings"].append("expired_richard_chat_result")
        plan = None
    if result is not None or (selected_decision is not None and selected_decision["status"] == "no_candidates"):
        _require(diag["parsed_plan_sha256"] == digest(plan), "Richard parsed plan digest mismatch")
    snapshot = diag["binding_snapshot"]
    _require(type(snapshot) is dict and set(snapshot) == {"schema", "graph_identity_sha256", "request_sha256", "bindings", "unbound"}
             and snapshot["schema"] == "richard_native_binding_v1" and snapshot["graph_identity_sha256"] == diag["graph_identity"]
             and _hash(snapshot["request_sha256"]) and digest(snapshot) == diag["binding_snapshot_sha256"], "Richard native snapshot invalid")
    bound, unbound = snapshot["bindings"], snapshot["unbound"]
    _require(type(bound) is list and type(unbound) is list, "Richard snapshot partition invalid")
    for row in bound:
        _require(type(row) is dict and set(row) == {"native_record_id", "graph_record_id", "cids", "proof_sha256"}
                 and type(row["native_record_id"]) is int and row["native_record_id"] > 0
                 and type(row["graph_record_id"]) is int and row["graph_record_id"] > 0
                 and _ids(row["cids"]) and bool(row["cids"]) and row["cids"] == sorted(row["cids"])
                 and _hash(row["proof_sha256"]), "Richard binding row invalid")
    for row in unbound:
        _require(type(row) is dict and set(row) == {"record_id", "reason"}
                 and type(row["record_id"]) is int and row["record_id"] > 0
                 and type(row["reason"]) is str and _REASON.fullmatch(row["reason"]), "Richard unbound row invalid")
    snapshot_ids = [r["native_record_id"] for r in bound] + [r["record_id"] for r in unbound]
    _require(len(set(snapshot_ids)) == len(snapshot_ids)
             and len({r["graph_record_id"] for r in bound}) == len(bound), "Richard snapshot duplicate/ambiguous identity")
    inventory = receipt["candidate_set"]["inventory"] if receipt is not None else None
    if inventory is not None:
        inv_ids = [r["record_id"] for r in inventory]
        _require(set(snapshot_ids) == set(inv_ids), "Richard snapshot candidate partition mismatch")
        req = receipt["input"]
        _require(snapshot["request_sha256"] == digest([req["scope"], req["event_id"], *inventory]), "Richard snapshot request mismatch")
    elif binding is not None:
        _require(set(snapshot_ids) == set(binding["seen_record_ids"] + binding["omitted_record_ids"]), "Richard snapshot inventory IDs mismatch")
        if partial:
            report["warnings"].append("expired_richard_chat_candidate_identity")
    else:
        report["warnings"].append("expired_richard_chat_snapshot_binding")
    ranked = diag["ranked_concepts"]
    _require(type(ranked) is list and len(ranked) <= 10 and digest(ranked) == diag["ranked_concepts_sha256"], "Richard ranked concepts invalid")
    for row in ranked:
        _require(type(row) is dict and set(row) == {"cid", "score"}
                 and type(row["cid"]) is int and row["cid"] > 0
                 and type(row["score"]) in (int,float) and math.isfinite(row["score"]) and row["score"] >= 0, "Richard ranked row invalid")
    _require(len({r["cid"] for r in ranked}) == len(ranked)
             and ranked == sorted(ranked,key=lambda r:(-r["score"],r["cid"])), "Richard ranked order/duplicates invalid")
    required = min(3,binding["candidate_count"]) if binding is not None else len(diag["final_record_ids"])
    _require(len(diag["fallback_record_ids"]) == required and len(diag["final_record_ids"]) == required, "Richard native result count invalid")
    if payload is not None:
        enriched = "richard_catalog" in payload
        catalog, targets = (payload["richard_catalog"],payload["allowed_targets"]) if enriched else ([],[])
        _validate_input_contract(payload, inputs[0]["response_schema"], required, policy=diag["policy"])
        local = _validate_local_input_audit(inputs[0], binding, payload) if binding is not None else None
        if version == "v2" and local is not None:
            _require(local["graph_identity"] == diag["graph_identity"]
                     and local["binding_snapshot_sha256"] == diag["binding_snapshot_sha256"],
                     "Richard local input diagnostic graph/binding mismatch")
            visible = set(binding["seen_record_ids"])
            admitted_cids = {cid for row in bound if row["native_record_id"] in visible for cid in row["cids"]}
            _require(all(row["cid"] in admitted_cids for row in local["catalog_cids"]),
                     "Richard local CID outside visible binding inventory")
        _require(diag["catalog_sha256"] == digest(catalog), "Richard visible catalog mismatch")
        if enriched:
            if version == "v1":
                report["warnings"].append("historical_richard_chat_v1_local_cid_proof_unavailable")
                if catalog and "cid" in catalog[0]:
                    report["warnings"].append("historical_richard_chat_v1_model_visible_cids")
            _require(diag["graph_identity"] == payload["graph_identity"] and diag["algorithm"] == payload["richard_algorithm"]
                     and diag["binding_snapshot_sha256"] == payload["richard_binding_sha256"], "Richard pre-call graph/binding mismatch")
            if result is not None:
                _require(set(raw) == {"selected_record_ids", "richard_plan"}, "Richard enriched plan missing")
                from .richard_chat_selection import validate_plan_shape
                reason = validate_plan_shape(plan,payload["query"],catalog,targets)
                if reason is not None:
                    _require(not diag["ranker_called"] and not diag["ranker_applied"] and diag["reason"] == reason
                             and not seeds["A"] and not seeds["B"], "Richard declined plan flags/reason mismatch")
                else:
                    cid_rows = local["catalog_cids"] if local is not None else catalog if catalog and "cid" in catalog[0] else None
                    if cid_rows is not None:
                        catalog_cids = {(r["term"], r["start"], r["end"]): r["cid"] for r in cid_rows}
                        for group in ("A", "B"):
                            expected = sorted({catalog_cids[(s["term"], s["start"], s["end"])]
                                               for s in plan["seeds_" + group]})
                            _require(seeds[group] == expected, "Richard resolved seed catalog binding mismatch")
                    _require(diag["ranker_called"] or diag["reason"] == "ambiguous_seed_overlap", "Richard admitted plan not executed")
        else:
            _require(result is None or set(raw) == {"selected_record_ids"}, "Richard base raw output fields invalid")
            _require(plan is None and not diag["ranker_called"] and not diag["ranker_applied"]
                     and not seeds["A"] and not seeds["B"]
                     and diag["reason"] in ("no_grounded_vocabulary","no_explicit_target"), "Richard off-branch fabricated ranker")
    else:
        report["warnings"].append("expired_richard_chat_catalog")
        if version == "v2":
            report["warnings"].append("expired_richard_chat_local_cid_proof")
    if not diag["ranker_called"]:
        _require(diag["variant"] is None and not ranked and not diag["native_ranked_record_ids"], "Richard uncalled ranker fabricated output")
        if diag["reason"] == "ambiguous_seed_overlap":
            _require(type(plan) is dict and plan.get("mode") == "dual" and seeds["A"] and seeds["B"]
                     and set(seeds["A"]) & set(seeds["B"]), "Richard unresolved overlap disposition invalid")
    else:
        _require(bool(seeds["A"]) and not set(seeds["A"]) & set(seeds["B"]), "Richard ranker seed groups invalid")
        _require(diag["variant"] in (diag["algorithm"]+"_nomeet",diag["algorithm"]+"_meet"), "Richard variant invalid")
        if result is not None:
            _require(type(plan) is dict and ((plan["mode"] == "dual" and bool(seeds["B"]))
                     or (plan["mode"] == "single" and not seeds["B"]))
                     and diag["variant"] == diag["algorithm"]+("_meet" if plan["mode"] == "dual" else "_nomeet"), "Richard mode/variant mismatch")
        if len(ranked) < 10:
            _require(not diag["ranker_applied"] and diag["reason"] == "short_target_pool"
                     and not diag["native_ranked_record_ids"], "Richard short pool disposition invalid")
        else:
            reducer_inventory = inventory if inventory is not None else payload.get("candidates") if payload is not None else None
            if reducer_inventory is not None and binding is not None:
                visible = set(binding["seen_record_ids"])
                ranks = {r["record_id"]:r["rank"] for r in reducer_inventory}
                expected = []
                for row in ranked:
                    native = [r["native_record_id"] for r in bound if r["native_record_id"] in visible and row["cid"] in r["cids"]]
                    for rid in sorted(native,key=lambda rid:(ranks[rid],rid)):
                        if rid not in expected:
                            expected.append(rid)
                _require(diag["native_ranked_record_ids"] == expected, "Richard native reducer mismatch")
            else:
                report["warnings"].append("expired_richard_chat_reducer_binding")
            if diag["ranker_applied"]:
                _require(len(diag["native_ranked_record_ids"]) >= required and diag["reason"] == "ranked_native_evidence"
                         and diag["final_record_ids"] == diag["native_ranked_record_ids"][:required], "Richard applied final mismatch")
            else:
                _require(len(diag["native_ranked_record_ids"]) < required and diag["reason"] == "short_bound_evidence", "Richard bound fallback invalid")
    if diag["fallback_used"]:
        _require(diag["final_record_ids"] == diag["fallback_record_ids"], "Richard fallback differs from same-call IDs")
    if not partial:
        _require(selected_decision is not None and (not successful or receipt is not None), "Richard complete diagnostic lacks decision/materials")
    positions = [i for i,(e,_) in enumerate(events) if e == "richard_chat_selection"]
    if decisions:
        _require(positions[0] > next(i for i,(e,_) in enumerate(events) if e == "model_selection_decision"), "Richard diagnostic ordering invalid")
    if partial and result is None:
        report["warnings"].append("expired_richard_chat_transport")
