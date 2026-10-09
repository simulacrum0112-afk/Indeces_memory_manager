"""Append-only, version-bound evidence for optional active graph queries.

The receipts establish local byte consistency and literal citation bindings.
They do not establish relevance, semantic support, or authenticity against a
writer able to replace an entire local log. No database or model is consulted.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math

from .run_records import (answer_record, digest, text_digest, validate_graph_audit,
                          validate_retrieval)


BUNDLE_SCHEMA = "active_requery_evidence_v1"
PLANNING_EVIDENCE_REF_SCHEMA = "active_requery_planning_evidence_ref_v1"
FINALIZATION_POLICY = "no_new_evidence_final_v1"
REPLY_LIMITATION = (
    "Local retrieval stopped because it added no new evidence. The saved materials "
    "are retained, but their sufficiency and semantic support are not established."
)
FINAL_REPLY_INSTRUCTIONS = (
    "\nLocal retrieval has stopped without adding evidence. Attempt one final reply "
    "using the frozen materials, preserving their citation IDs. State clearly when "
    "those materials are insufficient to answer, identify the remaining gaps, and "
    "do not infer that a failed search proves absence. Do not invent supporting "
    "facts or claim that the retrieval stop established evidence sufficiency."
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _identity(material, source):
    """Keep source versions distinct, including byte/normalized-text digests."""
    return digest({"scope": material["scope"], "record_id": material["record_id"],
                   "source_id": material["source_id"],
                   "source_version": {
                       key: source.get(key) for key in (
                           "original_file_bytes_sha256", "normalized_text_sha256")},
                   "fingerprint": material["fingerprint"],
                   "stored_text_sha256": text_digest(material["stored_text"]),
                   "quote_sha256": text_digest(material["quote"])})


def _append(bundle, retrieval, *, round_index, request_id, planning_call_id):
    """Pure append; callers validate both independent inputs beforehand."""
    parent = digest(bundle) if bundle is not None else None
    result = deepcopy(bundle) if bundle is not None else {
        "schema": BUNDLE_SCHEMA, "version": 1, "scope": retrieval["scope"],
        "rounds": [], "materials": [], "semantic_support": "not_evaluated",
        "citation_coverage": "not_established"}
    _require(type(round_index) is int and round_index == len(result["rounds"]),
             "evidence round is not the next append")
    _require(type(request_id) is str and bool(request_id), "invalid evidence request identifier")
    _require(request_id not in {item["request_id"] for item in result["rounds"]},
             "duplicate evidence request identifier")
    _require((round_index == 0 and planning_call_id is None)
             or (round_index > 0 and type(planning_call_id) is str and bool(planning_call_id)),
             "invalid evidence planning call identifier")
    _require(retrieval["scope"] == result["scope"], "evidence scope changed")
    _require(round_index == 0 or retrieval["event_id"] == request_id,
             "query retrieval/request identity mismatch")
    existing = {item["evidence_uid"]: item for item in result["materials"]}
    bindings = []
    retrieval_hash = digest(retrieval)
    for material in retrieval["materials"]:
        source = retrieval["sources"][material["source_id"]]
        uid = _identity(material, source)
        if uid not in existing:
            item = {"evidence_uid": uid, "citation_id": f"M{len(existing) + 1}",
                    "round_index": round_index, "request_id": request_id,
                    "local_citation_id": material["citation_id"],
                    "retrieval_sha256": retrieval_hash,
                    "material": deepcopy(material), "source": deepcopy(source)}
            result["materials"].append(item)
            existing[uid] = item
        bindings.append({"local_citation_id": material["citation_id"],
                         "evidence_uid": uid, "citation_id": existing[uid]["citation_id"]})
    result["rounds"].append({"round_index": round_index, "request_id": request_id,
                              "planning_call_id": planning_call_id,
                              "parent_evidence_sha256": parent,
                              "record": deepcopy(retrieval),
                              "record_sha256": retrieval_hash, "bindings": bindings})
    return result


def append_retrieval(bundle_or_none, retrieval, *, round_index, request_id, planning_call_id):
    """Return independent data; never mutate an earlier bundle or native receipt.

    Each native retrieval retains its own three-material contract. The bundle's
    citations grow monotonically and an already frozen version is not replaced
    when another query finds it again. Empty rounds are also recorded.
    """
    if bundle_or_none is not None:
        validate_bundle(bundle_or_none)
    validate_retrieval(retrieval)
    validate_graph_audit(retrieval["graph_audit"], retrieval)
    return _append(bundle_or_none, retrieval, round_index=round_index,
                   request_id=request_id, planning_call_id=planning_call_id)


def validate_bundle(bundle):
    _require(type(bundle) is dict and bundle.get("schema") == BUNDLE_SCHEMA
             and type(bundle.get("version")) is int and bundle["version"] == 1,
             "unsupported active evidence bundle")
    _require(type(bundle.get("rounds")) is list and bool(bundle["rounds"]),
             "active evidence bundle has no initial round")
    rebuilt = None
    for item in bundle["rounds"]:
        _require(type(item) is dict, "invalid evidence round")
        retrieval = item["record"]
        validate_retrieval(retrieval)
        validate_graph_audit(retrieval["graph_audit"], retrieval)
        rebuilt = _append(rebuilt, retrieval, round_index=item["round_index"],
                          request_id=item["request_id"], planning_call_id=item["planning_call_id"])
        _require(rebuilt["rounds"][-1] == item, "evidence append chain or binding mismatch")
    _require(rebuilt == bundle, "evidence bundle does not match its immutable rounds")


def bundle_model_materials(bundle):
    """Expose complete quotes with globally stable and locally inspectable IDs."""
    validate_bundle(bundle)
    result = []
    for item in bundle["materials"]:
        payload = deepcopy(item["material"]["model_payload"])
        payload.update(citation_id=item["citation_id"], citation_marker=f'[{item["citation_id"]}]',
                       evidence_uid=item["evidence_uid"], round_index=item["round_index"],
                       request_id=item["request_id"], local_citation_id=item["local_citation_id"],
                       retrieval_sha256=item["retrieval_sha256"])
        result.append(payload)
    return result


def planning_evidence_reference(materials):
    """Bind planning history to the complete, single outer evidence value."""
    return {"schema": PLANNING_EVIDENCE_REF_SCHEMA, "version": 1,
            "field": "evidence", "sha256": digest(materials)}


def answer_record_bundle(text, bundle):
    validate_bundle(bundle)
    materials = []
    for item in bundle["materials"]:
        material = deepcopy(item["material"])
        material["citation_id"] = item["citation_id"]
        materials.append(material)
    result = answer_record(text, {"materials": materials})
    result.update(version=3, bundle_sha256=digest(bundle))
    identities = {item["citation_id"]: item for item in bundle["materials"]}
    for citation in result["citations"]:
        if citation["status"] == "resolved":
            identity = identities[citation["citation_id"]]
            citation.update(evidence_uid=identity["evidence_uid"],
                            retrieval_sha256=identity["retrieval_sha256"],
                            round_index=identity["round_index"],
                            request_id=identity["request_id"],
                            local_citation_id=identity["local_citation_id"])
    return result


def validate_answer_bundle(record, bundle):
    _require(record == answer_record_bundle(record["text"], bundle),
             "active answer citation receipt mismatch")


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate active query JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError, TypeError):
        raise ValueError("invalid active query JSON") from None


def _validate_budget_ledger(ledger, calls, *, partial=False):
    """Independently bind cumulative usage/reservations to provider receipts."""
    from .contracts import validated_token_usage
    _require(type(ledger) is dict and ledger.get("schema") == "active_requery_budget_v1",
             "active budget ledger schema invalid")
    limits = ledger["limits"]
    _require(type(limits) is dict and all(type(limits.get(key)) is int and limits[key] > 0
                                        for key in ("calls", "input_tokens", "output_tokens"))
             and type(limits["seconds"]) in (int, float) and math.isfinite(limits["seconds"])
             and limits["seconds"] > 0, "active cumulative budget limits invalid")
    try:
        cost_limit = Decimal(str(limits["cost_usd"]))
        cost = Decimal(ledger["estimated_cost_usd"])
        _require(cost_limit.is_finite() and cost_limit >= 0 and cost.is_finite() and cost >= 0,
                 "active cumulative cost invalid")
    except (InvalidOperation, TypeError):
        raise ValueError("active cumulative cost invalid") from None
    _require(ledger["mode"] in {"mock", "configured_real"}
             and ledger["phase"] in {"open", "completed", "stopped", "halted"}
             and ledger["automatic_retries"] == 0
             and type(ledger["elapsed_seconds"]) in (int, float)
             and math.isfinite(ledger["elapsed_seconds"]) and ledger["elapsed_seconds"] >= 0,
             "active cumulative budget declarations invalid")
    _require(ledger["halted"] is None or (type(ledger["halted"]) is str and bool(ledger["halted"].strip())),
             "active cumulative stop reason invalid")
    _require(ledger["phase"] not in {"stopped", "halted"} or ledger["halted"] is not None,
             "active terminal budget phase has no stop reason")
    _require(ledger["phase"] != "completed" or ledger["halted"] is None,
             "active completed budget phase hides stop reason")
    _require((ledger["mode"] == "mock" and ledger["cost_basis"] == "synthetic_zero" and cost == 0)
             or (ledger["mode"] == "configured_real"
                 and ledger["cost_basis"] == "configured_upper_unit_prices_not_invoice"),
             "active cumulative cost basis mismatch")
    entries = ledger["calls"]
    _require(type(entries) is list and len(entries) <= limits["calls"]
             and len({item["call_id"] for item in entries}) == len(entries),
             "active cumulative call allowance mismatch")
    counted_input, counted_output = 0, 0
    for item in entries:
        _require(type(item["call_id"]) is str and bool(item["call_id"])
                 and item["stage"] in {"selection", "summary", "query", "reply"}
                 and item["status"] in {"admitted", "completed", "failed", "cancelled"}
                 and all(type(item[key]) is int and item[key] > 0 for key in (
                     "reserved_input_tokens", "reserved_output_tokens"))
                 and type(item["generation_started"]) is bool
                 and item["transport_intents"] in ([], ["/responses/input_tokens"],
                                                    ["/responses/input_tokens", "/responses"])
                 and item["generation_started"] == ("/responses" in item["transport_intents"]),
                 "active cumulative call admission invalid")
        try:
            reserved_cost = Decimal(item["reserved_cost_usd"])
            _require(reserved_cost.is_finite() and reserved_cost >= 0
                     and (ledger["mode"] != "mock" or reserved_cost == 0),
                     "active cumulative cost reservation invalid")
        except (InvalidOperation, TypeError):
            raise ValueError("active cumulative cost reservation invalid") from None
        usage = item["usage"]
        _require(usage is None or (validated_token_usage(usage) == usage
                 and set(usage) == {"input_tokens", "output_tokens"}),
                 "active cumulative usage schema invalid")
        if usage is not None:
            _require(item["generation_started"], "active usage without generation intent")
            counted_input += usage["input_tokens"]
            counted_output += usage["output_tokens"]
        elif item["status"] == "admitted":
            known_input = sum(entry["usage"]["input_tokens"] for entry in entries if entry["usage"] is not None)
            known_output = sum(entry["usage"]["output_tokens"] for entry in entries if entry["usage"] is not None)
            _require(known_input + item["reserved_input_tokens"] <= limits["input_tokens"]
                     and known_output + item["reserved_output_tokens"] <= limits["output_tokens"]
                     and cost + reserved_cost <= cost_limit,
                     "active admission exceeded cumulative reservation allowance")
        provider_events = calls.get(item["call_id"], [])
        starts = [f for e, f in provider_events if e == "call_start"]
        if starts:
            _require(starts[0]["stage"] == item["stage"]
                     and starts[0]["budget"]["input_tokens"] == item["reserved_input_tokens"]
                     and starts[0]["budget"]["output_tokens"] == item["reserved_output_tokens"],
                     "active cumulative reservation/provider mismatch")
        elif not partial:
            _require(item["status"] != "completed", "active completed ledger call has no provider start")
        if item["status"] in {"completed", "failed", "cancelled"}:
            _require(not item["generation_started"] or usage is not None
                     or ledger["halted"] == "requery_usage_unknown",
                     "active unknown generation usage was not halted")
            ends = [f for e, f in provider_events if e in {"call_end", "call_rejected"}]
            if ends:
                provider_status = "failed" if "status" not in ends[0] else ends[0]["status"]
                _require(provider_status == item["status"] and ends[0].get("known_usage") == usage,
                         "active cumulative terminal usage/provider mismatch")
            elif not partial:
                _require(item["status"] != "completed", "active completed ledger call has no provider end")
    _require(type(ledger["input_tokens"]) is int and type(ledger["output_tokens"]) is int
             and ledger["input_tokens"] == counted_input and ledger["output_tokens"] == counted_output,
             "active cumulative usage total mismatch")
    breached = counted_input > limits["input_tokens"] or counted_output > limits["output_tokens"] or cost > cost_limit
    _require(not breached or ledger["halted"] == "requery_provider_budget_breach",
             "active cumulative allowance breached without halt")


def _validate_budget_history(events, calls, report, *, partial=False):
    ledger_events = [(event, fields) for event, fields in events
               if event in {"requery_budget_event", "requery_stop"}]
    if not ledger_events:
        _require(partial or not calls, "active cumulative budget evidence missing")
        report["warnings"].append("active_cumulative_budget_evidence_unavailable")
        return
    previous = None
    budget_closed = False
    for event, fields in events:
        if event in {"requery_budget_event", "requery_stop"}:
            budget_closed |= fields["ledger"].get("phase") in {"completed", "stopped", "halted"}
        _require(not budget_closed or event not in {
            "call_start", "http_request", "requery_action", "requery_retrieval"},
            "active provider or query event after terminal budget phase")
    for event, fields in ledger_events:
        ledger = fields["ledger"]
        _validate_budget_ledger(ledger, calls, partial=partial)
        _require(ledger["trace_id"] == fields["trace_id"], "active cumulative trace binding mismatch")
        if event == "requery_budget_event":
            if fields.get("provider_call_id") is None:
                _require(fields.get("budget_event") == "turn_end" and ledger["phase"] != "open",
                         "active terminal budget event declaration mismatch")
            else:
                _require(fields["provider_call_id"] in {item["call_id"] for item in ledger["calls"]},
                         "active budget event has no admitted provider call")
        if previous is not None:
            _require(ledger["trace_id"] == previous["trace_id"] and ledger["limits"] == previous["limits"]
                     and ledger["mode"] == previous["mode"] and ledger["cost_basis"] == previous["cost_basis"]
                     and ledger["elapsed_seconds"] >= previous["elapsed_seconds"]
                     and Decimal(ledger["estimated_cost_usd"]) >= Decimal(previous["estimated_cost_usd"])
                     and len(ledger["calls"]) >= len(previous["calls"]),
                     "active cumulative ledger history mismatch")
            for old, new in zip(previous["calls"], ledger["calls"]):
                _require(all(old[key] == new[key] for key in (
                    "call_id", "stage", "reserved_input_tokens", "reserved_output_tokens", "reserved_cost_usd"))
                         and new["transport_intents"][:len(old["transport_intents"])] == old["transport_intents"]
                         and (old["usage"] is None or new["usage"] == old["usage"])
                         and (old["status"] == "admitted" or new["status"] == old["status"]),
                         "active cumulative ledger rewrote an admitted call")
            _require(previous["halted"] is None or ledger["halted"] == previous["halted"],
                     "active cumulative halt was cleared")
            _require(previous["halted"] is None or len(ledger["calls"]) == len(previous["calls"]),
                     "active cumulative call admitted after halt")
            if previous["phase"] != "open":
                _require(ledger["phase"] == previous["phase"] and ledger["calls"] == previous["calls"],
                         "active terminal budget phase was reopened or changed")
        previous = ledger
    if previous is not None and not partial:
        _require({item["call_id"] for item in previous["calls"]} == set(calls),
                 "active provider call bypassed cumulative ledger")
        for item in previous["calls"]:
            ends = [fields for event, fields in calls[item["call_id"]]
                    if event in {"call_end", "call_rejected"}]
            if ends:
                status = ends[0].get("status", "failed")
                _require(item["status"] == status and item["usage"] == ends[0].get("known_usage"),
                         "active final ledger omitted completed provider status or usage")
                if "generation_request_started" in ends[0]:
                    _require(item["generation_started"] == ends[0]["generation_request_started"],
                             "active final ledger/provider generation intent mismatch")
                _require(not ends[0].get("remote_usage_unknown")
                         or previous["halted"] == "requery_usage_unknown",
                         "active provider unknown usage was not halted")


def _validate_finalization_policy(events, start=None, *, partial=False):
    """A retrieval stop permits a reply only under an explicit, bound policy."""
    relevant = {"turn_start", "requery_stop", "reply_context", "answer_generated",
                "answer_delivered", "turn_end"}
    starts = [f for e, f in events if e == "turn_start"]
    markers = [f["requery_final_policy"] for e, f in events
               if e in relevant and "requery_final_policy" in f]
    if start is not None and "requery_final_policy" in start:
        markers.append(start["requery_final_policy"])
    has_final_metadata = any(e in relevant and any(key in f for key in (
        "final_reply_mode", "retrieval_stop_reason", "final_status")) for e, f in events)
    _require(not has_final_metadata or bool(markers), "active finalization policy missing")
    _require(all(value == FINALIZATION_POLICY for value in markers), "active finalization policy invalid")
    declared = bool(markers)
    if declared and starts:
        _require(len(starts) == 1 and starts[0].get("requery_final_policy") == FINALIZATION_POLICY,
                 "active finalization turn policy missing or downgraded")
    if declared and not partial:
        _require(start is not None and start.get("requery_final_policy") == FINALIZATION_POLICY
                 and len(starts) == 1 and starts[0].get("requery_final_policy") == FINALIZATION_POLICY,
                 "active finalization turn policy missing or downgraded")
    stops = [f for e, f in events if e == "requery_stop"]
    for stop in stops:
        if not declared:
            _require(stop.get("fallback") is not False or stop.get("reason") == "evidence_sufficient",
                     "active unmarked finalization stop invalid")
            continue
        _require(stop.get("requery_final_policy") == FINALIZATION_POLICY,
                 "active finalization stop policy missing")
        fallback = stop.get("fallback")
        expected = ("host_notice" if fallback is True else
                    "no_new_evidence_consolidation" if stop.get("reason") == "no_new_evidence" else
                    "ordinary_answer" if stop.get("reason") == "evidence_sufficient" else None)
        _require(type(fallback) is bool and expected is not None
                 and stop.get("final_reply_mode") == expected, "active finalization stop mode invalid")
        if expected == "no_new_evidence_consolidation":
            ledger = stop.get("ledger", {})
            _require(ledger.get("phase") == "open" and ledger.get("halted") is None
                     and ledger.get("usage_complete") is True and ledger.get("unknown_generation_count") == 0
                     and all(c.get("status") == "completed" and c.get("usage") is not None
                             for c in ledger.get("calls", []))
                     and "ledger_io_failure" not in stop,
                     "active finalization ledger not eligible")
    if declared:
        _require(len(stops) <= 1, "duplicate active finalization stop")
        _require(sum(e == "call_start" and f.get("stage") == "reply" for e, f in events) <= 1,
                 "active finalization attempted more than once")
        # A retained suffix can lose its stop receipt. Its remaining policy,
        # modes and terminal states must still be mutually consistent, while
        # the absent trigger proof remains explicitly retention_partial.
        binding = (stops[0]["final_reply_mode"], stops[0]["reason"]) if stops else None
        for event, fields in events:
            if event not in {"reply_context", "answer_generated", "answer_delivered", "turn_end"}:
                continue
            if not stops and event == "turn_end" and not any(key in fields for key in (
                    "requery_final_policy", "final_reply_mode", "retrieval_stop_reason")) and binding is None:
                continue  # Failure before a retrieval stop/final reply existed.
            mode, reason = fields.get("final_reply_mode"), fields.get("retrieval_stop_reason")
            _require(fields.get("requery_final_policy") == FINALIZATION_POLICY
                     and type(reason) is str and bool(reason)
                     and (mode == "host_notice" or
                          mode == "ordinary_answer" and reason == "evidence_sufficient" or
                          mode == "no_new_evidence_consolidation" and reason == "no_new_evidence"),
                     "active retained finalization mode invalid")
            current = (mode, reason)
            _require(binding is None or binding == current, "active retained finalization binding mismatch")
            binding = current
            if event == "reply_context":
                _require(fields.get("no_provider_reply") is (mode == "host_notice"),
                         "active finalization provider mode mismatch")
                if mode == "no_new_evidence_consolidation":
                    context = _json(fields["messages"][0]["content"].split("\n", 1)[1])
                    _require(type(context.get("context_limitation")) is str
                             and context["context_limitation"].endswith(REPLY_LIMITATION)
                             and fields.get("instructions", "").endswith(FINAL_REPLY_INSTRUCTIONS),
                             "active finalization limitation missing")
            elif event == "turn_end":
                status = fields.get("status")
                _require(status in {"delivered", "skipped", "failed", "cancelled", "delivery_unknown"}
                         and fields.get("final_status") in {"model_reply_delivered", "host_notice_delivered",
                             "model_skip", "failed", "cancelled", "delivery_unknown"},
                         "active finalization final status invalid")
                expected = ("host_notice_delivered" if mode == "host_notice" else "model_reply_delivered") if status == "delivered" else (
                    "model_skip" if status == "skipped" else
                    "delivery_unknown" if status == "failed" and str(fields.get("code", "")).startswith("delivery_unknown:") else status)
                _require(fields.get("final_status") == expected, "active finalization final status mismatch")


def _validate_host_notices(events, start=None, *, partial=False):
    """New notices bind a preserved stop cause; unmarked historical turns keep v3."""
    from .user_notices import NOTICE_POLICY, user_notice

    recorded_starts = [fields for event, fields in events if event == "turn_start"]
    starts = recorded_starts + ([start] if start is not None else [])
    markers = [fields["notice_policy"] for fields in starts if "notice_policy" in fields]
    declared = bool(markers)
    if declared:
        _require(all(marker == NOTICE_POLICY for marker in markers), "active notice policy invalid")
    for event, stop in events:
        if event != "requery_stop":
            continue
        marked = "notice_policy" in stop
        if not declared and not marked:
            continue
        if marked and not partial:
            _require(start is not None and start.get("notice_policy") == NOTICE_POLICY
                     and len(recorded_starts) == 1
                     and recorded_starts[0].get("notice_policy") == NOTICE_POLICY,
                     "active notice turn policy missing or downgraded")
        if stop.get("fallback") is False:
            _require(not marked and "notice_clarification" not in stop,
                     "active answer stop has notice policy")
            continue
        _require(stop.get("fallback") is True and stop.get("notice_policy") == NOTICE_POLICY,
                 "active notice policy missing or invalid")
        reason = stop.get("reason")
        ledger = stop.get("ledger")
        _require(type(reason) is str and bool(reason) and type(ledger) is dict
                 and type(ledger.get("halted")) is str and bool(ledger["halted"]),
                 "active notice lost stop reason or halt")
        _require(ledger["halted"] == reason or ledger["halted"] in {
                     "requery_usage_unknown", "requery_ledger_audit_failed", "requery_provider_budget_breach"},
                 "active notice stop reason/ledger mismatch")
        if not partial and ledger["halted"] != reason:
            failures = [fields for name, fields in events if name in {"call_end", "call_rejected"}
                        and fields.get("status") != "completed"]
            if failures:
                _require(reason == failures[-1].get("code") or
                         (reason == "cancelled" and failures[-1].get("status") == "cancelled"),
                         "active notice stop reason/provider failure mismatch")
        clarification = stop.get("notice_clarification")
        if reason == "ambiguity" or "notice_clarification" in stop:
            _require(reason == "ambiguity" and type(clarification) is str
                     and bool(clarification.strip()) and len(clarification) <= 240,
                     "active notice clarification invalid")
        if not partial and reason in {"ambiguity", "model_stop"}:
            actions = [fields["action"] for name, fields in events if name == "requery_action"]
            expected_action = "clarify" if reason == "ambiguity" else "stop"
            _require(bool(actions) and actions[-1]["action"] == expected_action,
                     "active notice stop action mismatch")
            if reason == "ambiguity":
                _require(clarification == actions[-1]["clarification"],
                         "active notice clarification binding mismatch")
        expected = user_notice(reason, clarification=clarification)
        _require(not any(name == "call_start" and fields.get("stage") == "reply"
                         for name, fields in events), "active notice has provider reply call")
        for name, fields in events:
            if name in {"answer_generated", "answer_delivered"}:
                _require(fields.get("record", {}).get("text") == expected,
                         "active notice template mismatch")
            elif name == "delivery_start":
                _require(fields.get("text") == expected, "active notice template mismatch")
            elif name == "turn_end" and fields.get("status") == "delivered":
                _require(fields.get("receipt", {}).get("text") == expected,
                         "active notice template mismatch")


def _validate_ledger_io_recoveries(events, calls, report, *, partial=False):
    """A local replace receipt needs independent provider and budget evidence."""
    recoveries = [(i, fields) for i, (event, fields) in enumerate(events)
                  if event == "requery_ledger_io_recovered"]
    if not recoveries:
        return False
    trace_ids = {fields["trace_id"] for _, fields in recoveries}
    trace_ids.update(fields["trace_id"] for event, fields in events
                     if event in {"turn_start", "turn_end", "requery_ledger_io_failure"}
                     or "ledger_io_failure" in fields)
    _require(len(trace_ids) == 1, "ledger I/O recovery trace binding mismatch")
    ends = [i for i, (event, _) in enumerate(events) if event == "turn_end"]
    bound_budget_events = set()
    close_unconfirmed = False
    for index, fields in recoveries:
        _require(not any(event == "requery_ledger_io_failure" or "ledger_io_failure" in value
                         for event, value in events[:index]),
                 "ledger I/O recovery after permanent failure")
        identity = fields["provider_call_id"]
        if ends and index > ends[0]:
            _require(fields["audit_event"] is None and identity is None and fields["stage"] is None,
                     "post-turn ledger I/O recovery is not cleanup metadata")
        if identity is not None:
            _require(not any(event in {"requery_budget_event", "requery_stop"}
                             and value.get("ledger", {}).get("phase") in {"completed", "stopped", "halted"}
                             for event, value in events[:index]),
                     "ledger I/O recovery provider context after budget closure")
            provider = calls.get(identity, [])
            starts = [value for event, value in provider if event == "call_start"]
            if starts:
                _require(all(value.get("stage") == fields["stage"]
                             and value.get("trace_id") == fields["trace_id"] for value in starts),
                         "ledger I/O recovery provider stage/trace mismatch")
            elif partial and fields["audit_event"] != "call_start":
                report["warnings"].append("expired_ledger_io_recovery_provider_binding")
            else:
                raise ValueError("ledger I/O recovery provider start missing")
            provider_ends = [(i, value) for i, (event, value) in enumerate(events)
                             if event in {"call_end", "call_rejected"}
                             and value.get("call_id") == identity]
            _require(len(provider_ends) == 1, "ledger I/O recovery provider end missing")
            following = next(((i, value) for i, (event, value) in enumerate(events)
                              if i > index and event == "requery_budget_event"), None)
            _require(following is not None, "ledger I/O recovery budget binding missing")
            if following is not None:
                budget_at, budget = following
                _require(budget_at not in bound_budget_events
                         and budget.get("trace_id") == fields["trace_id"]
                         and budget.get("provider_call_id") == identity
                         and budget.get("budget_event") == fields["audit_event"],
                         "ledger I/O recovery budget binding mismatch")
                entries = [item for item in budget.get("ledger", {}).get("calls", [])
                           if item.get("call_id") == identity]
                _require(len(entries) == 1 and entries[0].get("stage") == fields["stage"],
                         "ledger I/O recovery budget stage binding mismatch")
                _require(provider_ends[0][0] > budget_at,
                         "ledger I/O recovery provider end ordering mismatch")
                if fields["audit_event"] == "call_start":
                    _require(any(i > budget_at and event == "call_start"
                                 and value.get("call_id") == identity
                                 for i, (event, value) in enumerate(events)),
                             "ledger I/O recovery provider start ordering mismatch")
                if fields["audit_event"] == "call_end":
                    end = provider_ends[0][1]
                    _require(entries[0].get("status") == end.get("status", "failed")
                             and entries[0].get("status") in {"completed", "failed", "cancelled"}
                             and entries[0].get("usage") == end.get("known_usage"),
                             "ledger I/O recovery terminal usage/provider mismatch")
                else:
                    _require(entries[0].get("status") == "admitted",
                             "ledger I/O recovery preterminal status mismatch")
                bound_budget_events.add(budget_at)
        else:
            closing = next(((i, value) for i, (event, value) in enumerate(events)
                            if i > index and event == "requery_budget_event"
                            and value.get("trace_id") == fields["trace_id"]
                            and value.get("budget_event") == "turn_end"
                            and value.get("provider_call_id") is None
                            and value.get("ledger", {}).get("phase") in {"completed", "stopped", "halted"}), None)
            if closing is None:
                close_unconfirmed = True
                report["warnings"].append("ledger_io_recovery_terminal_save_unconfirmed")
            elif ends and index > ends[0]:
                _require(closing[0] not in bound_budget_events,
                         "ledger I/O recovery cleanup budget binding reused")
                bound_budget_events.add(closing[0])
    return close_unconfirmed


def _validate_ledger_io_records(events, calls, report, *, partial=False):
    """I/O diagnostics explain failure without replacing provider/ledger ends."""
    from .run_records import _validate_ledger_io_fields

    _validate_ledger_io_fields(events)
    recovery_close_unconfirmed = _validate_ledger_io_recoveries(events, calls, report, partial=partial)
    recovered = any(event == "requery_ledger_io_recovered" for event, _ in events)
    failures = [(i, fields) for i, (event, fields) in enumerate(events)
                if event == "requery_ledger_io_failure"]
    attached = [(i, event, fields) for i, (event, fields) in enumerate(events)
                if "ledger_io_failure" in fields]
    if not failures and not attached and not recovered:
        return False
    if partial and failures and not attached:
        report["warnings"].append("expired_ledger_io_first_failure_binding")
    ends = [(i, fields) for i, (event, fields) in enumerate(events) if event == "turn_end"]
    first = failures[0] if failures else None
    trace_ids = {fields["trace_id"] for _, fields in failures}
    trace_ids.update(fields["trace_id"] for event, fields in events
                     if event == "requery_ledger_io_recovered")
    trace_ids.update(fields["trace_id"] for _, _, fields in attached)
    trace_ids.update(fields["trace_id"] for _, fields in ends)
    _require(len(trace_ids) == 1, "ledger I/O trace binding mismatch")
    for index, fields in failures:
        identity = fields["provider_call_id"]
        if identity is not None:
            provider = calls.get(identity, [])
            starts = [f for e, f in provider if e == "call_start"]
            if starts:
                _require(all(f.get("stage") == fields["stage"] for f in starts),
                         "ledger I/O provider stage mismatch")
            else:
                report["warnings"].append("ledger_io_provider_binding_unavailable")
        if ends and index > ends[0][0]:
            _require(fields["audit_event"] is None and identity is None and fields["stage"] is None,
                     "post-turn ledger I/O event is not cleanup metadata")
    for index, event, fields in attached:
        if first is not None and first[0] < index:
            _require(fields["ledger_io_failure"] == first[1]["io_failure"],
                     "ledger I/O first failure binding mismatch")
        elif partial:
            report["warnings"].append("expired_ledger_io_first_failure_binding")
        else:
            # Constructor failure can retain its original exception metadata
            # even when the best-effort diagnostic sink itself was unavailable.
            _require(event == "turn_end" and not calls
                     and not any(e == "requery_stop" for e, _ in events),
                     "ledger I/O first failure event missing")
            report["warnings"].append("ledger_io_failure_event_unavailable")
    if first is not None:
        for index, (event, fields) in enumerate(events):
            if index <= first[0]:
                continue
            if event == "requery_stop" or (event == "turn_end" and fields.get("code") == "requery_ledger_write_failed"):
                _require("ledger_io_failure" in fields, "ledger I/O first failure attachment missing")
    if not partial:
        for provider in calls.values():
            _require(any(e in {"call_end", "call_rejected"} for e, _ in provider),
                     "ledger I/O turn has missing provider end")
    # A completed ledger claim still needs a real provider end. Failure metadata
    # cannot fill a missing call_end, including in a retained suffix.
    for event, fields in events:
        if event not in {"requery_budget_event", "requery_stop"}:
            continue
        for item in fields.get("ledger", {}).get("calls", []):
            if item.get("status") == "completed":
                _require(any(e in {"call_end", "call_rejected"}
                             for e, _ in calls.get(item.get("call_id"), [])),
                         "ledger I/O completed call has no provider end")
    if not failures and not attached:
        report["warnings"] = list(dict.fromkeys(report["warnings"]))
        return recovery_close_unconfirmed
    last_failure_at = max([i for i, _ in failures] + [i for i, _, _ in attached])
    closed = any(i > last_failure_at and event == "requery_budget_event"
                 and fields.get("budget_event") == "turn_end"
                 and fields.get("provider_call_id") is None
                 and fields.get("ledger", {}).get("phase") in {"completed", "stopped", "halted"}
                 for i, (event, fields) in enumerate(events))
    if not closed:
        report["warnings"].append("ledger_io_terminal_save_unconfirmed")
    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    return not closed or recovery_close_unconfirmed


def verify_active_turn(events, start=None, report=None, *, partial=False):
    """Check version 3 without accepting single-retrieval assumptions.

    A retained suffix validates available frozen bytes only and is explicitly
    partial, even if a retained bundle happens to contain earlier materials.
    Provider usage/lifecycle validators are shared with versions 1 and 2.
    """
    from .run_records import (_validate_bot_decision, _validate_call,
                              _validate_model_selection, _validate_partial_call,
                              _validate_reply_request, _validate_failure_notices)

    report = report if report is not None else {"status": "incomplete", "warnings": []}
    _validate_failure_notices(events, report, partial=partial)
    report["warnings"] = list(dict.fromkeys(report["warnings"]))
    _validate_host_notices(events, start, partial=partial)
    _validate_finalization_policy(events, start, partial=partial)
    ends = [f for e, f in events if e == "turn_end"]
    contexts = [f for e, f in events if e == "reply_context"]
    generated = [f for e, f in events if e == "answer_generated"]
    delivered = [f for e, f in events if e == "answer_delivered"]
    delivery_starts = [f for e, f in events if e == "delivery_start"]
    decisions = [f for e, f in events if e == "bot_reply_decision"]
    stops = [f for e, f in events if e == "requery_stop"]
    _require(all(len(items) <= 1 for items in (
        ends, contexts, generated, delivered, delivery_starts, decisions, stops)),
        "duplicate active turn stage")
    calls = {}
    for event, fields in events:
        if fields.get("call_id"):
            calls.setdefault(fields["call_id"], []).append((event, fields))
    for call_events in calls.values():
        call_status = None
        if partial and not any(e == "call_start" for e, _ in call_events):
            call_status = _validate_partial_call(call_events)
        else:
            call_status = _validate_call(call_events)
        if call_status == "incomplete":
            report["warnings"].append("active_provider_call_incomplete")
        if any(e in {"call_end", "call_rejected"} and f.get("remote_usage_unknown") is True
               for e, f in call_events):
            report["warnings"].append("remote_generation_usage_unknown")
        if any(e in {"call_end", "call_rejected"} and f.get("status") != "completed"
               for e, f in call_events):
            report["warnings"].append("active_query_or_other_call_failed")
    ledger_close_unconfirmed = _validate_ledger_io_records(events, calls, report, partial=partial)

    if not partial:
        _require(start is not None and type(start.get("run_record_version")) is int
                 and start["run_record_version"] == 3,
                 "active turn declaration missing")
        _require(sum(e == "turn_start" for e, _ in events) == 1, "duplicate active turn start")
        _require(events[0][0] == "turn_start", "active turn start ordering mismatch")
    # Retained bundles are self-contained frozen snapshots; verify each even
    # when the preceding action/retrieval event has expired.
    snapshots = {}
    for event, fields in events:
        if event == "requery_evidence":
            validate_bundle(fields["bundle"])
            _require(digest(fields["bundle"]) == fields["bundle_sha256"], "active evidence digest mismatch")
            snapshots[fields["bundle_sha256"]] = fields["bundle"]
    if partial:
        for event, fields in events:
            if event == "requery_retrieval":
                validate_retrieval(fields["record"])
                validate_graph_audit(fields["record"]["graph_audit"], fields["record"])
                _require(digest(fields["record"]) == fields["record_sha256"], "retained active retrieval digest mismatch")
            elif event == "requery_action":
                _require(digest(fields["action"]) == fields["action_sha256"], "retained active action digest mismatch")
        for fields in generated + delivered:
            bundle = snapshots.get(fields["bundle_sha256"])
            if bundle is not None:
                validate_answer_bundle(fields["record"], bundle)
            else:
                report["warnings"].append("expired_active_evidence_binding")
        report["status"] = "retention_partial"
        report["warnings"].append("retained_active_query_contract")
        _validate_budget_history(events, calls, report, partial=True)
        return report

    bundle = None
    pending_retrieval = False
    pending_action = None
    action_round = -1
    used_planning_calls = set()
    declared_query_rounds = None
    final_started = False
    stopped = False
    budget_closed = False
    generated_seen = False
    delivery_attempted = False
    delivered_seen = False
    initial_observed = False
    initial_recorded = False
    terminal = False
    last_retrieval_added = None
    for position, (event, fields) in enumerate(events):
        closing_budget_metadata = (event == "requery_budget_event" and fields.get("budget_event") == "turn_end"
                                   and fields.get("provider_call_id") is None
                                   and fields.get("ledger", {}).get("phase") in {"completed", "stopped", "halted"})
        _require(not terminal or event in {"failure_notice_delivered", "failure_notice_unknown", "failure_notice_skipped"}
                 or event in {"requery_ledger_io_failure", "requery_ledger_io_recovered"}
                 or closing_budget_metadata,
                 "active events after turn end")
        if event in {"requery_budget_event", "requery_stop"}:
            if fields["ledger"].get("phase") in {"completed", "stopped", "halted"}:
                budget_closed = True
        if event == "http_request":
            _require(not budget_closed, "active provider transport after budget closure")
        elif event == "memory_observation":
            _require(bundle is None and not initial_recorded, "active initial observation ordering mismatch")
            initial_observed = True
        elif event == "retrieval_record":
            _require(bundle is None and initial_observed, "active initial native retrieval ordering mismatch")
            initial_recorded = True
        elif event == "call_start":
            _require(not budget_closed, "active provider call after budget closure")
            if fields["stage"] == "query":
                _require(bundle is not None and not final_started and not stopped,
                         "active planning call ordering mismatch")
            elif fields["stage"] == "reply":
                _require(final_started and not generated_seen, "active reply call ordering mismatch")
        elif event == "requery_action":
            _require(not final_started and not stopped and not budget_closed
                     and pending_action is None and bundle is not None,
                     "active action ordering mismatch")
            _require(type(fields["round_index"]) is int and fields["round_index"] == action_round + 1,
                     "active planning round mismatch")
            action_round = fields["round_index"]
            _require(digest(fields["action"]) == fields["action_sha256"], "active action digest mismatch")
            call_id = fields["planning_call_id"]
            _require(call_id in calls and call_id not in used_planning_calls,
                     "active planning call evidence missing or reused")
            used_planning_calls.add(call_id)
            planning_events = calls[call_id]
            _require(_validate_call(planning_events) == "complete", "active planning call incomplete")
            starts = [f for e, f in planning_events if e == "call_start"]
            completed = [f for e, f in planning_events if e == "call_end"]
            _require(starts[0]["stage"] == "query" and len(completed) == 1,
                     "active planning stage mismatch")
            _require(_json(completed[0]["result"]["text"]) == fields["action"],
                     "active planning output/action mismatch")
            from .active_requery import ACTION_SCHEMA, INSTRUCTIONS, parse_action
            from .contracts import GovernedError
            try:
                _require(parse_action(completed[0]["result"]["text"], start["input"]["text"])
                         == fields["action"], "active planning schema/action mismatch")
            except GovernedError:
                raise ValueError("active planning schema/action mismatch") from None
            requests = [f for e, f in planning_events if e == "http_request" and f.get("path") == "/responses"]
            _require(len(requests) == 1, "active planning input transport missing")
            request = requests[0]["payload"]
            _require(request["instructions"] == INSTRUCTIONS
                     and request.get("text", {}).get("format") == {
                         "type": "json_schema", "name": "query", "strict": True, "schema": ACTION_SCHEMA}
                     and type(request["input"]) is list and len(request["input"]) == 1
                     and request["input"][0]["role"] == "user", "active actual planning prompt mismatch")
            payload = _json(request["input"][0]["content"])
            _require(type(payload) is dict and set(payload) == {
                "original_text", "round_index", "evidence", "remaining_query_rounds", "history_context"}
                     and payload["original_text"] == start["input"]["text"]
                     and type(payload["round_index"]) is int and payload["round_index"] == action_round
                     and payload["evidence"] == bundle_model_materials(bundle)
                     and type(payload["remaining_query_rounds"]) is int and payload["remaining_query_rounds"] >= 0,
                     "active planning question/round/evidence binding mismatch")
            total_query_rounds = payload["remaining_query_rounds"] + action_round
            _require(declared_query_rounds is None or declared_query_rounds == total_query_rounds,
                     "active query round budget changed")
            declared_query_rounds = total_query_rounds
            history_context = payload["history_context"]
            _require(type(history_context) is list and bool(history_context)
                     and type(history_context[0]) is dict
                     and history_context[0].get("role") == "user"
                     and type(history_context[0].get("content")) is str
                     and history_context[0]["content"].startswith("Historical context data:\n"),
                     "active planning history context mismatch")
            history = _json(history_context[0]["content"].split("\n", 1)[1])
            _require(type(history) is dict, "active planning history context mismatch")
            if "memory_citations_ref" in history:
                reference = history["memory_citations_ref"]
                _require("memory_citations" not in history
                         and type(reference) is dict
                         and type(reference.get("version")) is int
                         and reference == planning_evidence_reference(payload["evidence"]),
                         "active planning history evidence mismatch")
            else:
                # Existing receipts used two complete copies. Preserve their
                # byte binding while new planning requests carry one copy.
                _require(history.get("memory_citations") == payload["evidence"],
                         "active planning history evidence mismatch")
            _require(all(i < position for i, (_, f) in enumerate(events)
                         if f.get("call_id") == call_id), "active planning output ordering mismatch")
            pending_action = fields
        elif event == "requery_retrieval":
            _require(not final_started and not stopped and not budget_closed, "active retrieval ordering mismatch")
            round_index = fields["round_index"]
            _require(type(round_index) is int and round_index == (len(bundle["rounds"]) if bundle else 0),
                     "active retrieval round mismatch")
            retrieval = fields["record"]
            _require(retrieval["graph_audit"]["selection"].get("ranking_mode") == "static"
                     and retrieval["graph_audit"]["selection"].get("weight_basis") == "static_npmi",
                     "active retrieval is not the declared static NPMI path")
            _require(digest(retrieval) == fields["record_sha256"], "active retrieval digest mismatch")
            _require(fields["parent_evidence_sha256"] == (digest(bundle) if bundle else None),
                     "active retrieval parent evidence mismatch")
            if round_index == 0:
                _require(initial_observed and initial_recorded
                         and retrieval["event_id"] == start["message_id"]
                         and retrieval["query"] == start["input"]["text"]
                         and fields["request_id"] == f'{start["message_id"]}:initial'
                         and fields["planning_call_id"] is None, "active initial retrieval identity mismatch")
            else:
                _require(pending_action is not None and pending_action["round_index"] == round_index - 1
                         and pending_action["planning_call_id"] == fields["planning_call_id"],
                         "active retrieval/action association mismatch")
                action = pending_action["action"]
                expected_query = start["input"]["text"] + "\nCanonical query data:\n" + json.dumps(
                    action["query"], ensure_ascii=False, sort_keys=True)
                _require(action.get("action") == "query"
                         and retrieval["query"] == expected_query, "active retrieval query mismatch")
                _require(declared_query_rounds is not None and round_index <= declared_query_rounds,
                         "active retrieval exceeded declared query rounds")
                _require(fields["request_id"] == f'{start["message_id"]}:query:{round_index}',
                         "active retrieval request identity mismatch")
            _require(retrieval["scope"] == start["knowledge_scope"], "active knowledge scope mismatch")
            previous_count = len(bundle["materials"]) if bundle else 0
            bundle = append_retrieval(bundle, retrieval, round_index=round_index,
                                      request_id=fields["request_id"], planning_call_id=fields["planning_call_id"])
            last_retrieval_added = len(bundle["materials"]) - previous_count
            pending_retrieval = True
            pending_action = None
        elif event == "requery_evidence":
            _require(not final_started and fields["bundle"] == bundle
                     and fields["bundle_sha256"] == digest(bundle), "active evidence append mismatch")
            pending_retrieval = False
        elif event == "requery_stop":
            _require(not final_started and type(fields["reason"]) is str
                     and bool(fields["reason"]) and type(fields["ledger"]) is dict,
                     "active stop receipt invalid")
            if fields.get("fallback") is False:
                if fields.get("reason") == "no_new_evidence":
                    _require(fields.get("requery_final_policy") == FINALIZATION_POLICY
                             and bundle is not None and bool(bundle["materials"])
                             and len(bundle["rounds"]) > 1
                             and bool(bundle["rounds"][-1]["record"]["materials"])
                             and last_retrieval_added == 0,
                             "active finalization evidence not eligible")
                else:
                    _require(fields["reason"] == "evidence_sufficient" and pending_action is not None
                             and pending_action["action"].get("action") == "answer",
                             "active ordinary answer stop missing action")
            stopped = True
        elif event == "reply_context":
            _require(bundle is not None and not pending_retrieval and stopped,
                     "active reply before evidence completion/stop")
            _require(fields["bundle_sha256"] == digest(bundle), "active reply evidence link mismatch")
            context = _json(fields["messages"][0]["content"].split("\n", 1)[1])
            _require(context["memory_citations"] == bundle_model_materials(bundle),
                     "active model evidence context mismatch")
            final_started = True
        elif event in {"answer_generated", "answer_delivered"}:
            _require(final_started and fields["bundle_sha256"] == digest(bundle),
                     "active answer evidence link mismatch")
            if event == "answer_generated":
                _require(not generated_seen and not delivery_attempted and not delivered_seen,
                         "active generated answer ordering mismatch")
                generated_seen = True
                reply_ids = {f["call_id"] for e, f in events if e == "call_start" and f.get("stage") == "reply"}
                _require(all(i < position for i, (_, f) in enumerate(events)
                             if f.get("call_id") in reply_ids), "active generation before provider reply completed")
            else:
                _require(generated_seen and delivery_attempted and not delivered_seen,
                         "active answer delivery ordering mismatch")
                delivered_seen = True
            validate_answer_bundle(fields["record"], bundle)
            if fields["record"]["unresolved_markers"]:
                report["warnings"].append("unresolved_citation")
        elif event == "delivery_start":
            _require(generated_seen and not delivery_attempted and not delivered_seen,
                     "active delivery attempt ordering mismatch")
            delivery_attempted = True
        elif event == "bot_reply_decision":
            _require(final_started and not generated_seen and not delivery_attempted,
                     "active bot decision ordering mismatch")
        elif event == "turn_end":
            terminal = True
    _require(not pending_retrieval or not ends or ends[0]["status"] != "delivered",
             "delivered active turn has incomplete evidence append")
    initial = bundle["rounds"][0]["record"] if bundle else None
    retrievals = [f for e, f in events if e == "retrieval_record"]
    observations = [f for e, f in events if e == "memory_observation"]
    _require(len(retrievals) <= 1 and len(observations) <= 1, "duplicate active initial retrieval")
    if initial is not None:
        _require(len(retrievals) == len(observations) == 1
                 and retrievals[0]["record"] == initial and retrievals[0]["record_sha256"] == digest(initial)
                 and observations[0]["audit"] == initial["graph_audit"]
                 and observations[0]["audit_sha256"] == initial["graph_audit_sha256"],
                 "active initial native receipt mismatch")
    _validate_model_selection(events, report, start=start,
                              graph=initial["graph_audit"] if initial else
                              observations[0]["audit"] if observations else None,
                              allow_budget_narrowing=True)
    _validate_budget_history(events, calls, report)
    is_bot = start["input"].get("author_is_bot", False)
    _require(type(is_bot) is bool and (not decisions or is_bot), "active bot declaration mismatch")
    bot_data = None
    if decisions:
        _require(len(contexts) == 1 and bundle is not None
                 and decisions[0]["bundle_sha256"] == digest(bundle), "active bot decision input missing")
        bot_data = _validate_bot_decision(decisions[0], events, context=contexts[0])
    if not ends:
        report["status"] = "incomplete"
        report["warnings"].append("missing_turn_end")
    elif ends[0]["status"] == "delivered":
        _require(len(contexts) == len(generated) == len(delivered) == len(delivery_starts) == 1,
                 "delivered active turn missing evidence")
        host_fallback = contexts[0].get("no_provider_reply") is True
        if host_fallback:
            _require(contexts[0].get("final_origin") == "bounded_stop"
                     and generated[0].get("final_origin") == "bounded_stop"
                     and delivered[0].get("final_origin") == "bounded_stop"
                     and generated[0].get("model_result") is None
                     and not decisions, "active host fallback origin mismatch")
        else:
            _require(not is_bot or (bot_data and bot_data["action"] == "reply"),
                     "active bot answer without reply decision")
        _require(delivery_starts[0]["text"] == generated[0]["record"]["text"]
                 and delivered[0]["record"]["text"] == ends[0]["receipt"]["text"]
                 and delivered[0]["receipt_ids"] == ends[0]["receipt"]["ids"],
                 "active delivery receipt mismatch")
        reply_calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply"
                       and f.get("status") == "completed"]
        if host_fallback:
            _require(not reply_calls, "active host fallback has completed provider reply")
            _require(bool(stops), "active host fallback has no stop receipt")
        else:
            _require(len(reply_calls) == 1 and generated[0]["model_result"] == reply_calls[0]["result"],
                     "active reply model receipt mismatch")
            _require(generated[0]["record"]["text"] == (bot_data["text"] if is_bot else reply_calls[0]["result"]["text"]),
                     "active generated answer mismatch")
            for call_events in calls.values():
                _require(_validate_call(call_events) == "complete", "delivered active turn has unsuccessful call")
            requests = [f for e, f in calls[reply_calls[0]["call_id"]]
                        if e == "http_request" and f.get("path") == "/responses"]
            _require(len(requests) == 1, "active actual reply input missing")
            _validate_reply_request(contexts[0], requests[0]["payload"], bot=is_bot)
        report["status"] = "complete"
    elif ends[0]["status"] == "skipped":
        _require(is_bot and bot_data is not None and bot_data["action"] == "skip"
                 and not generated and not delivered and not delivery_starts
                 and ends[0].get("reason") == "bot_reply_skipped", "active bot skip mismatch")
        report["status"] = "skipped"
    else:
        _require(not delivered, "active failed turn has confirmed answer delivery")
        report["status"] = "failed"
    if ledger_close_unconfirmed and report["status"] in {"complete", "skipped"}:
        report["status"] = "incomplete"
    return report
