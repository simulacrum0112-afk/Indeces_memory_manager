"""Frozen, independently inspectable recall and visible-citation receipts.

Checks establish internal consistency, not semantic entailment or authenticity
against a writer who can replace the entire local log. No model call is made.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import itertools
import math
from pathlib import Path
import re

from .scratch import canonical, read_records, retention_checkpoint


MAX_PDF_METADATA_BYTES = 512 * 1024


def pdf_conversion_metadata(encoded):
    """Decode bounded provenance strictly, without reading archived PDF bytes."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate PDF conversion metadata key")
            result[key] = value
        return result
    try:
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_PDF_METADATA_BYTES:
            raise ValueError("PDF conversion metadata is unavailable or exceeds the limit")
        result = json.loads(encoded, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite PDF metadata")))
        if not isinstance(result, dict):
            raise ValueError("PDF conversion metadata must be an object")
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 8:
                raise ValueError("PDF conversion metadata nesting exceeds the limit")
            if isinstance(value, dict):
                pending.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                pending.extend((item, depth + 1) for item in value)
        # Escaped lone surrogates and extreme nesting must not escape as a
        # response serialization error, even for corrupt local metadata.
        canonical(result)
    except (UnicodeError, RecursionError):
        raise ValueError("invalid PDF conversion metadata encoding or nesting") from None
    return result


def _pdf_occurrences(source, text):
    from .pdf_import import page_occurrences
    return page_occurrences(source["chunks"], text, source["pdf_conversion"])


def _occurrence_pages(occurrences):
    return sorted({page for occurrence in occurrences for page in occurrence["page_numbers"]})


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def freeze_retrieval(db, scope, event_id, query, records, graph_audit):
    """Called synchronously immediately after retrieval, before any await.

    Full source text belongs only in scratch. The exact bounded model payload
    is kept separately; version metadata is never fetched again after delivery.
    """
    model_materials, materials, sources = [], [], {}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for index, record in enumerate(records, 1):
        citation_id = f"M{index}"
        payload = {**deepcopy(record), "citation_id": citation_id, "citation_marker": f"[{citation_id}]"}
        model_materials.append(payload)
        row = db.execute("SELECT text,quote,marks_json,fingerprint,active,scope,source_id "
                         "FROM memory_records WHERE id=?", (record["id"],)).fetchone()
        if row is None or not row[4] or row[5] != scope or row[6] != record["source_id"]:
            raise ValueError("selected material is unavailable or crosses scope")
        source_id = record["source_id"]
        if source_id not in sources:
            source = {"source_id": source_id, "metadata_available": False}
            if {"knowledge_versions", "knowledge_chunks", "knowledge_desired", "knowledge_published"} <= tables:
                version = db.execute("SELECT path,digest,raw_text,status,created_at,scope "
                                     "FROM knowledge_versions WHERE source_id=?", (source_id,)).fetchone()
                if version is not None:
                    chunks = [{"chunk_index": r[0], "start_character": r[1], "text": r[2],
                               "marks": json.loads(r[3]) if r[3] is not None else None}
                              for r in db.execute("SELECT chunk_index,start_character,text,marks_json "
                                                  "FROM knowledge_chunks WHERE source_id=? ORDER BY chunk_index", (source_id,))]
                    def pointer(table):
                        head = db.execute(f"SELECT source_id FROM {table} WHERE scope=? AND path=?", (scope, version[0])).fetchone()
                        return head[0] if head else None
                    source.update(metadata_available=True, path=version[0], original_file_bytes_sha256=version[1],
                                  raw_text=version[2], normalized_text_sha256=text_digest(version[2]),
                                  status=version[3], created_at=version[4], scope=version[5], chunks=chunks,
                                  desired_source_id=pointer("knowledge_desired"), published_source_id=pointer("knowledge_published"),
                                  original_file_bytes_verifiable=False)
                    if "knowledge_pdf_versions" in tables:
                        pdf = db.execute("""SELECT CASE WHEN length(CAST(metadata_json AS BLOB))<=?
                            THEN metadata_json ELSE NULL END FROM knowledge_pdf_versions WHERE source_id=?""",
                            (MAX_PDF_METADATA_BYTES, source_id)).fetchone()
                        if pdf is not None:
                            from .pdf_import import validate_conversion
                            metadata = pdf_conversion_metadata(pdf[0])
                            validate_conversion(source["raw_text"], metadata, source["original_file_bytes_sha256"])
                            source.update(pdf_conversion=metadata, original_pdf_bytes_verifiable=False)
            sources[source_id] = source
        source = sources[source_id]
        chunk_index = None
        if source["metadata_available"]:
            matches = [c for c in source["chunks"] if c["text"] == row[0] == row[1]]
            if matches:
                chunk_index = matches[0]["chunk_index"]
        material = {"citation_id": citation_id, "record_id": record["id"], "source_id": source_id,
                          "scope": scope, "stored_text": row[0], "quote": row[1], "marks": json.loads(row[2]),
                          "fingerprint": row[3], "active_at_retrieval": bool(row[4]), "chunk_index": chunk_index,
                          "model_payload": payload}
        if "pdf_conversion" in source:
            occurrences = _pdf_occurrences(source, row[0])
            material.update(pdf_page_occurrences=occurrences, pdf_page_numbers=_occurrence_pages(occurrences))
            payload["pdf_page_numbers"] = deepcopy(material["pdf_page_numbers"])
        materials.append(material)
    result = {"version": 1, "scope": scope, "event_id": event_id, "query": query,
              "model_materials": model_materials, "materials": materials, "sources": sources,
              "graph_audit": deepcopy(graph_audit), "graph_audit_sha256": digest(graph_audit)}
    validate_retrieval(result)
    return result


_MARKER = re.compile(r"\[M[^\]\n]*\]")


def answer_record(text, retrieval):
    """Locate literal markers and their containing paragraph; no NLP inference.

    Even quoted/code-fenced markers are literal occurrences, not proof that the
    model adopted a claim. Offsets are Python Unicode character offsets.
    """
    bindings = {m["citation_id"]: m for m in retrieval["materials"]}
    citations, cited = [], set()
    for match in _MARKER.finditer(text):
        identity = match.group()[1:-1]
        material = bindings.get(identity)
        start = text.rfind("\n\n", 0, match.start()) + 2
        if start == 1:
            start = 0
        end = text.find("\n\n", match.end())
        if end < 0:
            end = len(text)
        citation = {"citation_id": identity, "marker": match.group(), "start_character": match.start(),
                    "end_character": match.end(), "paragraph_start": start, "paragraph_end": end,
                    "paragraph": text[start:end], "status": "resolved" if material else "unresolved"}
        if material:
            citation.update(source_id=material["source_id"], record_id=material["record_id"])
            if "pdf_page_numbers" in material:
                citation["pdf_page_numbers"] = deepcopy(material["pdf_page_numbers"])
            cited.add(identity)
        citations.append(citation)
    return {"version": 1, "text": text, "text_sha256": text_digest(text), "citations": citations,
            "cited_material_ids": sorted(cited), "uncited_material_ids": sorted(set(bindings) - cited),
            "unresolved_markers": [c["marker"] for c in citations if c["status"] == "unresolved"],
            "semantic_support": "not_evaluated", "citation_coverage": "not_established"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_retrieval(record):
    _require(record["version"] == 1, "unsupported retrieval record")
    _require(digest(record["graph_audit"]) == record["graph_audit_sha256"], "graph audit digest mismatch")
    materials = record["materials"]
    _require(len(materials) <= 3, "too many recalled materials")
    _require([m["model_payload"] for m in materials] == record["model_materials"], "model/material binding mismatch")
    _require(set(record["sources"]) == {m["source_id"] for m in materials}, "source catalog mismatch")
    for index, material in enumerate(materials, 1):
        payload, source = material["model_payload"], record["sources"][material["source_id"]]
        _require(material["citation_id"] == f"M{index}" and payload["citation_id"] == f"M{index}"
                 and payload["citation_marker"] == f"[M{index}]", "citation binding mismatch")
        _require(material["scope"] == record["scope"] == payload["scope"], "material scope mismatch")
        _require(material["record_id"] == payload["id"] and material["source_id"] == payload["source_id"]
                 == source["source_id"], "material identity mismatch")
        _require(material["active_at_retrieval"] is True, "inactive retrieval")
        _require(material["quote"] == payload["quote"] and material["marks"] == payload["marks"], "model quote/marks mismatch")
        full = material["stored_text"]
        preview = full[:109] + "…" if len(full) > 110 else full
        _require(payload["text"] == preview and payload["text_truncated"] == (len(full) > 110), "model preview mismatch")
        _require(digest([material["stored_text"], material["quote"]]) == material["fingerprint"], "material fingerprint mismatch")
        if not source["metadata_available"]:
            _require(not material["source_id"].startswith("kb:"), "knowledge version metadata missing")
            _require(not any(key in material for key in ("pdf_page_occurrences", "pdf_page_numbers"))
                     and "pdf_page_numbers" not in payload, "PDF pages have no frozen conversion metadata")
            continue
        _require(source["scope"] == record["scope"] and source["status"] == "ready"
                 and source["published_source_id"] == material["source_id"], "unpublished knowledge version")
        raw = source["raw_text"]
        _require(text_digest(raw) == source["normalized_text_sha256"], "normalized source digest mismatch")
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", source["original_file_bytes_sha256"])), "invalid original-file digest")
        _require(source["original_file_bytes_verifiable"] is False, "original bytes not frozen")
        _require(not source["path"].casefold().endswith(".pdf") or "pdf_conversion" in source,
                 "PDF source lacks frozen conversion metadata")
        if "pdf_conversion" in source:
            from .pdf_import import validate_conversion
            validate_conversion(raw, source["pdf_conversion"], source["original_file_bytes_sha256"])
            _require(source.get("original_pdf_bytes_verifiable") is False, "PDF original bytes not frozen")
            occurrences = _pdf_occurrences(source, material["stored_text"])
            _require(bool(occurrences) and isinstance(material.get("pdf_page_occurrences"), list)
                     and digest(material["pdf_page_occurrences"]) == digest(occurrences),
                     "PDF material page occurrences mismatch")
            pages = _occurrence_pages(occurrences)
            _require(isinstance(material.get("pdf_page_numbers"), list) and isinstance(payload.get("pdf_page_numbers"), list)
                     and digest(material["pdf_page_numbers"]) == digest(pages)
                     and digest(payload["pdf_page_numbers"]) == digest(pages),
                     "PDF material/model page binding mismatch")
        else:
            _require(not any(key in material for key in ("pdf_page_occurrences", "pdf_page_numbers"))
                     and "pdf_page_numbers" not in payload and "original_pdf_bytes_verifiable" not in source,
                     "PDF pages have no frozen conversion metadata")
        previous_end, previous_index = 0, -1
        for chunk in source["chunks"]:
            start, text = chunk["start_character"], chunk["text"]
            _require(type(start) is int and start >= previous_end and type(chunk["chunk_index"]) is int
                     and chunk["chunk_index"] > previous_index,
                     "invalid chunk ordering")
            _require(not raw[previous_end:start].strip() and raw[start:start + len(text)] == text,
                     "chunk does not match normalized source")
            _require(isinstance(chunk["marks"], list) and bool(chunk["marks"]), "incomplete annotation snapshot")
            previous_end = start + len(text)
            previous_index = chunk["chunk_index"]
        _require(not raw[previous_end:].strip(), "source chunk coverage incomplete")
        matches = [c for c in source["chunks"] if c["chunk_index"] == material["chunk_index"]]
        _require(len(matches) == 1 and matches[0]["text"] == material["stored_text"] == material["quote"],
                 "material does not bind to source chunk")
        # Self-name exclusion is an indexing policy, not an annotation change.
        _require(set(material["marks"]) <= {m.strip().casefold() for m in matches[0]["marks"]}, "material mark mismatch")


def validate_answer(record, retrieval):
    _require(record == answer_record(record["text"], retrieval), "answer citation receipt mismatch")


def _validate_call(events):
    starts = [f for e, f in events if e == "call_start"]
    ends = [f for e, f in events if e in {"call_end", "call_rejected"}]
    _require(len(starts) == 1 and len(ends) <= 1, "call lifecycle mismatch")
    if not ends:
        return "incomplete"
    start, end = starts[0], ends[0]
    _require(start["stage"] == end["stage"] and start["trace_id"] == end["trace_id"], "call stage/trace mismatch")
    if end.get("status") != "completed":
        return "failed"
    requests = [f for e, f in events if e == "http_request"]
    responses = [f for e, f in events if e == "http_response"]
    _require([f["path"] for f in requests] == ["/responses/input_tokens", "/responses"]
             and [f["path"] for f in responses] == ["/responses/input_tokens", "/responses"], "completed call transport evidence missing")
    count_request, request = requests[0]["payload"], requests[1]["payload"]
    _require({k: v for k, v in request.items() if k not in {"max_output_tokens", "store", "stream", "truncation", "tools"}}
             == count_request, "count/generation input mismatch")
    budget = start["budget"]
    _require(request["model"] == start["model"] and request["max_output_tokens"] == budget["output_tokens"]
             and request["reasoning"]["effort"] == budget["reasoning"] and request["tools"] == []
             and request["truncation"] == "disabled" and request["store"] is False and request["stream"] is False,
             "request budget/policy mismatch")
    # Prior receipts did not declare verbosity; preserve that contract only
    # when neither the call nor its actual HTTP payload declares this field.
    text_options = request.get("text", {})
    if "verbosity" in start or "verbosity" in text_options:
        _require(isinstance(start.get("verbosity"), str) and start["verbosity"] in {"low", "medium", "high"}
                 and text_options.get("verbosity") == start["verbosity"], "request verbosity binding mismatch")
    gates = [f for e, f in events if e == "input_gate"]
    counted = responses[0]["payload"]["input_tokens"]
    _require(type(counted) is int and 0 <= counted <= budget["input_tokens"] and len(gates) == 1
             and gates[0]["input_tokens"] == counted and gates[0]["limit"] == budget["input_tokens"]
             and gates[0]["admitted"] is True, "input budget gate mismatch")
    response, result = responses[1]["payload"], end["result"]
    _require(response["status"] == "completed", "provider completion mismatch")
    usage = response["usage"]
    for key in ("input_tokens", "output_tokens"):
        _require(type(usage[key]) is int and 0 <= usage[key] <= budget[key] and usage[key] == result[key], "response usage mismatch")
    text = "".join(part["text"] for item in response["output"]
                   if item.get("type") == "message" and item.get("role") == "assistant"
                   for part in item["content"] if part.get("type") == "output_text")
    _require(text == result["text"] and bool(text.strip()), "response output mismatch")
    _require(str(response.get("id", "")) == result["response_id"], "response identifier mismatch")
    _require(0 <= result["elapsed_seconds"] < budget["seconds"], "call time budget mismatch")
    return "complete"


def _validate_partial_call(events):
    """Check available transport receipts without inventing expired budgets."""
    ends = [f for e, f in events if e in {"call_end", "call_rejected"}]
    _require(len(ends) <= 1, "duplicate retained call end")
    _require(len({f["trace_id"] for _, f in events}) == 1, "retained call trace mismatch")
    requests, responses = {}, {}
    for event, fields in events:
        if event not in {"http_request", "http_response"}:
            continue
        target = requests if event == "http_request" else responses
        path = fields["path"]
        _require(path in {"/responses/input_tokens", "/responses"} and path not in target,
                 "duplicate/unknown retained transport stage")
        _require(isinstance(fields["payload"], dict), "retained transport schema invalid")
        target[path] = fields["payload"]
    if len(requests) == 2:
        _require({k: v for k, v in requests["/responses"].items()
                  if k not in {"max_output_tokens", "store", "stream", "truncation", "tools"}}
                 == requests["/responses/input_tokens"], "retained count/generation input mismatch")
    gates = [f for e, f in events if e == "input_gate"]
    _require(len(gates) <= 1, "duplicate retained input gate")
    if gates:
        gate = gates[0]
        _require(type(gate["input_tokens"]) is int and gate["input_tokens"] >= 0
                 and type(gate["limit"]) is int and gate["limit"] > 0 and type(gate["admitted"]) is bool
                 and gate["admitted"] == (gate["input_tokens"] <= gate["limit"]), "retained input gate policy mismatch")
        if "/responses" in requests:
            _require(gate["admitted"] is True, "retained generation bypassed input gate")
    if gates and "/responses/input_tokens" in responses:
        _require(gates[0]["input_tokens"] == responses["/responses/input_tokens"]["input_tokens"],
                 "retained input gate mismatch")
    completed = ends and ends[0].get("status") == "completed"
    if completed:
        result = ends[0]["result"]
        _require(isinstance(result["text"], str) and bool(result["text"].strip())
                 and all(type(result[k]) is int and result[k] >= 0 for k in ("input_tokens", "output_tokens"))
                 and isinstance(result["response_id"], str) and result["elapsed_seconds"] >= 0,
                 "retained call result invalid")
        if "/responses" in requests:
            cap = requests["/responses"]["max_output_tokens"]
            _require(type(cap) is int and cap > 0 and result["output_tokens"] <= cap,
                     "retained output token cap mismatch")
        if "/responses" in responses:
            response = responses["/responses"]
            text = "".join(part["text"] for item in response["output"]
                           if item.get("type") == "message" and item.get("role") == "assistant"
                           for part in item["content"] if part.get("type") == "output_text")
            _require(response["status"] == "completed" and text == result["text"]
                     and str(response.get("id", "")) == result["response_id"], "retained provider output mismatch")
            _require(all(response["usage"][k] == result[k] for k in ("input_tokens", "output_tokens")),
                     "retained provider usage mismatch")
    return "retention_partial"


def _validate_unbound_answer(record):
    """Verify literal spans when the source-binding snapshot has expired."""
    _require(record["version"] == 1 and text_digest(record["text"]) == record["text_sha256"],
             "retained answer text digest mismatch")
    expected = answer_record(record["text"], {"materials": []})
    citations = record["citations"]
    _require(len(citations) == len(expected["citations"]), "retained citation count mismatch")
    bindings = {}
    for actual, literal in zip(citations, expected["citations"]):
        _require({k: v for k, v in actual.items() if k not in {"status", "source_id", "record_id"}}
                 == {k: v for k, v in literal.items() if k != "status"}, "retained citation span mismatch")
        _require(actual["status"] in {"resolved", "unresolved"}, "retained citation status invalid")
        if actual["status"] == "resolved":
            _require(actual["citation_id"] in {"M1", "M2", "M3"} and isinstance(actual["source_id"], str)
                     and bool(actual["source_id"]) and type(actual["record_id"]) is int and actual["record_id"] > 0,
                     "retained citation binding invalid")
            binding = (actual["source_id"], actual["record_id"])
            _require(actual["citation_id"] not in bindings or bindings[actual["citation_id"]] == binding,
                     "retained citation binding conflict")
            bindings[actual["citation_id"]] = binding
        else:
            _require("source_id" not in actual and "record_id" not in actual, "unresolved citation has a binding")
    _require(record["cited_material_ids"] == sorted(bindings) and record["unresolved_markers"]
             == [c["marker"] for c in citations if c["status"] == "unresolved"], "retained citation summary mismatch")
    uncited = record["uncited_material_ids"]
    _require(uncited == sorted(set(uncited)) and all(c in {"M1", "M2", "M3"} for c in uncited)
             and not set(uncited) & set(bindings), "retained uncited material IDs invalid")
    _require(record["semantic_support"] == "not_evaluated" and record["citation_coverage"] == "not_established",
             "retained answer claims unevaluated support")


def _validate_partial_turn(events, report):
    stages = {"memory_observation": 1, "retrieval_record": 2, "reply_context": 3,
              "answer_generated": 4, "delivery_start": 5, "answer_delivered": 6, "turn_end": 7}
    present = [stages[e] for e, _ in events if e in stages]
    _require(present == sorted(present) and len(present) == len(set(present)), "retained turn stage ordering mismatch")
    by_event = {e: f for e, f in events if e in stages}
    observation = by_event.get("memory_observation")
    if observation:
        _require(digest(observation["audit"]) == observation["audit_sha256"], "retained graph observation digest mismatch")
        validate_graph_audit(observation["audit"])
    retrieval_fields = by_event.get("retrieval_record")
    retrieval = retrieval_fields["record"] if retrieval_fields else None
    if retrieval is not None:
        _require(digest(retrieval) == retrieval_fields["record_sha256"], "retained retrieval digest mismatch")
        validate_retrieval(retrieval)
        validate_graph_audit(retrieval["graph_audit"], retrieval)
        if observation:
            _require(observation["audit"] == retrieval["graph_audit"], "retained graph/retrieval mismatch")
        context = by_event.get("reply_context")
        if context:
            _require(context["retrieval_sha256"] == digest(retrieval), "retained context link mismatch")
            data = json.loads(context["messages"][0]["content"].split("\n", 1)[1])
            _require(data["memory_citations"] == retrieval["model_materials"], "retained model materials mismatch")
    for stage in ("answer_generated", "answer_delivered"):
        response = by_event.get(stage)
        if response:
            if retrieval is not None:
                _require(response["retrieval_sha256"] == digest(retrieval), "retained answer retrieval link mismatch")
                validate_answer(response["record"], retrieval)
            else:
                _validate_unbound_answer(response["record"])
                report["warnings"].append("expired_material_binding")
            if response["record"]["unresolved_markers"]:
                report["warnings"].append("unresolved_citation")
    generated, delivered, end = (by_event.get(e) for e in ("answer_generated", "answer_delivered", "turn_end"))
    if generated:
        _require(generated["model_result"]["text"] == generated["record"]["text"], "retained generated output mismatch")
        calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply" and f.get("status") == "completed"]
        if calls:
            _require(len(calls) == 1 and calls[0]["result"] == generated["model_result"], "retained generated receipt mismatch")
    if end and delivered:
        _require(end["status"] == "delivered" and end["receipt"]["text"] == delivered["record"]["text"]
                 and end["receipt"]["ids"] == delivered["receipt_ids"], "retained delivery receipt mismatch")
    context = by_event.get("reply_context")
    if context:
        requests = [f for e, f in events if e == "http_request" and f["path"] == "/responses"]
        reply_call_ids = {f["call_id"] for e, f in events if e == "call_end" and f.get("stage") == "reply"}
        for request in requests:
            if request["call_id"] in reply_call_ids:
                _require(request["payload"]["input"] == context["messages"] and request["payload"]["instructions"] == context["instructions"],
                         "retained actual reply context mismatch")


def verify_runs(paths: list[Path]):
    """Verify new record contracts across files; report legacy/incomplete turns.

    Call scratch.verify first for envelope/hash integrity. No runtime SQLite or
    current knowledge files are required, and private text is never printed.
    """
    turns, issues, calls = {}, [], {}
    partial_traces, partial_calls, checkpoints = set(), set(), []
    for path in paths:
        checkpoint = retention_checkpoint(path)
        if checkpoint:
            checkpoints.append({"file": path.name, **checkpoint})
            partial_traces.update(checkpoint["partial_trace_ids"])
            partial_calls.update(checkpoint["partial_call_ids"])
        for item in read_records(path):
            fields = item["fields"]
            trace = fields.get("trace_id")
            if trace is not None and (not isinstance(trace, str) or not trace):
                issues.append({"trace_id": None, "reason": "invalid trace identifier"})
                continue
            call_id = fields.get("call_id")
            if call_id is not None and (not isinstance(call_id, str) or not call_id):
                issues.append({"trace_id": trace, "reason": "invalid call identifier"})
                continue
            if trace:
                turns.setdefault(trace, []).append((item["event"], fields))
            if fields.get("call_id"):
                calls.setdefault(fields["call_id"], []).append((item["event"], fields))
    call_reports = []
    for call_id, events in calls.items():
        trace = events[0][1].get("trace_id")
        try:
            missing_start = not any(e == "call_start" for e, _ in events)
            status = _validate_partial_call(events) if missing_start and call_id in partial_calls else _validate_call(events)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, ZeroDivisionError) as error:
            status = "invalid"
            issues.append({"trace_id": trace, "reason": str(error) if type(error) is ValueError else "call schema invalid"})
        call_reports.append({"call_id": call_id, "trace_id": trace, "status": status})
    reports = []
    for trace, events in turns.items():
        starts = [f for e, f in events if e == "turn_start"]
        if not starts:
            # Passive label traces do not have conversation stages.
            is_turn = any(e in {"memory_observation", "retrieval_record", "knowledge_retrieved", "reply_context",
                                 "answer_generated", "delivery_start", "answer_delivered", "turn_end"}
                          or f.get("stage") in {"reply", "summary"} for e, f in events)
            if not is_turn:
                continue
            report = {"trace_id": trace, "status": "retention_partial", "warnings": ["expired_turn_start"]}
            reports.append(report)
            try:
                _require(trace in partial_traces, "turn start missing without retention evidence")
                _validate_partial_turn(events, report)
                _require(not any(c["status"] == "invalid" for c in call_reports if c["trace_id"] == trace),
                         "retained turn has invalid call evidence")
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, ZeroDivisionError) as error:
                report["status"] = "invalid"
                issues.append({"trace_id": trace, "reason": str(error) if type(error) is ValueError else "retained record schema invalid"})
            continue
        report = {"trace_id": trace, "status": "legacy", "warnings": []}
        reports.append(report)
        if starts[0].get("run_record_version") != 1:
            continue
        report["status"] = "incomplete"
        try:
            _require(len(starts) == 1, "duplicate turn start")
            ends = [f for e, f in events if e == "turn_end"]
            retrievals = [f for e, f in events if e == "retrieval_record"]
            generated = [f for e, f in events if e == "answer_generated"]
            delivered = [f for e, f in events if e == "answer_delivered"]
            observations = [f for e, f in events if e == "memory_observation"]
            _require(len(ends) <= 1 and len(retrievals) <= 1 and len(generated) <= 1 and len(delivered) <= 1,
                     "duplicate run stage")
            _require(len(observations) <= 1, "duplicate graph observation")
            stage_order = {"turn_start": 0, "memory_observation": 1, "retrieval_record": 2,
                           "reply_context": 3, "answer_generated": 4, "delivery_start": 5,
                           "answer_delivered": 6, "turn_end": 7}
            stages = [stage_order[e] for e, _ in events if e in stage_order]
            _require(stages == sorted(stages), "run stage ordering mismatch")
            if observations:
                graph = observations[0]["audit"]
                _require(observations[0]["audit_sha256"] == digest(graph), "graph observation digest mismatch")
                _require(graph["event_id"] == starts[0]["message_id"] and graph["scope"] == starts[0]["knowledge_scope"]
                         and graph["request"]["query"] == starts[0]["input"]["text"], "graph observation input mismatch")
                validate_graph_audit(graph)
            if retrievals:
                retrieval = retrievals[0]["record"]
                validate_retrieval(retrieval)
                _require(retrieval["event_id"] == starts[0]["message_id"] and retrieval["query"] == starts[0]["input"]["text"],
                         "retrieval input mismatch")
                _require(retrieval["scope"] == starts[0]["knowledge_scope"], "knowledge scope mismatch")
                _require(retrievals[0]["record_sha256"] == digest(retrieval), "retrieval digest mismatch")
                validate_graph_audit(retrieval["graph_audit"], retrieval)
                _require(len(observations) == 1 and observations[0]["audit"] == retrieval["graph_audit"], "graph/retrieval receipt mismatch")
                if not retrieval["graph_audit"]["observation"]["original_changes_known"]:
                    report["warnings"].append("original_weight_changes_unavailable")
                if any(not s["metadata_available"] for s in retrieval["sources"].values()):
                    report["warnings"].append("source_version_metadata_unavailable")
                for response in generated + delivered:
                    _require(response["retrieval_sha256"] == digest(retrieval), "answer retrieval link mismatch")
                    validate_answer(response["record"], retrieval)
                    if response["record"]["unresolved_markers"]:
                        report["warnings"].append("unresolved_citation")
                if not generated and delivered:
                    raise ValueError("delivery without generated answer")
            else:
                _require(not generated and not delivered, "answer without retrieval")
            if not ends:
                report["warnings"].append("missing_turn_end")
                continue
            if ends[0]["status"] == "delivered":
                _require(len(retrievals) == len(generated) == len(delivered) == 1, "delivered turn missing evidence")
                _require(delivered[0]["record"]["text"] == ends[0]["receipt"]["text"]
                         and delivered[0]["receipt_ids"] == ends[0]["receipt"]["ids"], "delivery receipt mismatch")
                model_inputs = [f for e, f in events if e == "reply_context"]
                _require(len(model_inputs) == 1, "reply input evidence missing")
                context = json.loads(model_inputs[0]["messages"][0]["content"].split("\n", 1)[1])
                _require(context["memory_citations"] == retrieval["model_materials"], "actual reply context mismatch")
                _require(model_inputs[0]["retrieval_sha256"] == digest(retrieval), "reply context retrieval mismatch")
                calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply" and f.get("status") == "completed"]
                _require(len(calls) == 1 and calls[0]["result"]["text"] == generated[0]["record"]["text"], "reply call evidence missing or mismatched")
                _require(generated[0]["model_result"] == calls[0]["result"], "generated model receipt mismatch")
                _require(not any(c["status"] != "complete" for c in call_reports if c["trace_id"] == trace),
                         "delivered turn has incomplete/failed call evidence")
                requests = [f for e, f in events if e == "http_request" and f["call_id"] == calls[0]["call_id"] and f["path"] == "/responses"]
                _require(len(requests) == 1 and requests[0]["payload"]["input"] == model_inputs[0]["messages"]
                         and requests[0]["payload"]["instructions"] == model_inputs[0]["instructions"], "actual adaptor input mismatch")
                report["status"] = "complete"
            else:
                _require(not delivered, "confirmed receipt conflicts with failed turn")
                report["status"] = "failed"
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, ZeroDivisionError) as error:
            report["status"] = "invalid"
            # Fixed diagnostic messages; never echo private source/reply text.
            issues.append({"trace_id": trace, "reason": str(error) if type(error) is ValueError else "record schema invalid"})
    return {"turns": reports, "counts": {s: sum(r["status"] == s for r in reports)
            for s in ("complete", "failed", "incomplete", "invalid", "legacy", "retention_partial")}, "issues": issues,
            "calls": call_reports, "call_counts": {s: sum(c["status"] == s for c in call_reports)
            for s in ("complete", "failed", "incomplete", "invalid", "retention_partial")}, "retention_checkpoints": checkpoints}


def validate_graph_audit(audit, retrieval=None):
    _require(audit["schema_version"] == 1, "graph audit version mismatch")
    if retrieval is not None:
        _require(audit["scope"] == retrieval["scope"] and audit["event_id"] == retrieval["event_id"]
                 and audit["request"]["query"] == retrieval["query"], "graph audit input mismatch")
    durable = audit["durable_payload_sha256"]
    if not audit["replay"] or "original_event" not in audit:
        _require(digest({k: v for k, v in audit.items() if k != "durable_payload_sha256"}) == durable,
                 "durable graph audit mismatch")
    else:
        _require(audit["original_event"]["payload_sha256"] == durable, "replayed graph audit link mismatch")
    observation, selection = audit["observation"], audit["selection"]
    hits = audit["match"]["direct_hits"]
    _require(hits == audit["match"]["literal_matches"][:4], "direct hit limit mismatch")
    _require(selection["limits"] == {"direct_marks": 4, "neighbors_per_hit": 5, "expanded_marks": 2,
             "references": 3, "total_text_characters": 400, "entry_text_characters": 110}, "graph limits changed")
    transitions = observation["changed_edges"]
    if observation["applied"]:
        _require(not audit["replay"] and observation["status"] == "applied", "replay cannot mutate weights")
        parameters = observation["parameters"]
        _require(parameters["eta"] == 1.0 and parameters["decay"] == 0.99
                 and parameters["dynamic_round_digits"] == 6 and parameters["static_round_digits"] == 4,
                 "Hebbian parameter mismatch")
        reinforced, seen = set(), set()
        for edge in transitions:
            pair = (edge["a"], edge["b"])
            _require(pair[0] < pair[1] and pair not in seen, "invalid edge identity")
            seen.add(pair)
            before, creation = edge["before_weight"], edge["created_by"]
            if creation == "static_seed":
                _require(before is None and edge["after_seed"] == edge["static_score"] == edge["seed_weight"]
                         and edge["static_score"] > 0, "seed transition mismatch")
                base = edge["after_seed"]
            elif creation == "direct_reinforcement":
                _require(before is None and edge["after_seed"] is None and edge["seed_weight"] == 0,
                         "new direct edge mismatch")
                base = 0.0
            else:
                _require(creation is None and isinstance(before, (int, float)) and before >= 0
                         and edge["after_seed"] is None, "prior weight missing")
                base = before
            if edge["decay_applied"]:
                _require(creation != "direct_reinforcement" and edge["after_decay"] == round(base * 0.99, 6),
                         "decay transition mismatch")
                base = edge["after_decay"]
            else:
                _require(creation == "direct_reinforcement" and edge["after_decay"] is None, "decay omission mismatch")
            addition = edge["reinforcement_added"]
            _require(addition in (0.0, 1.0), "reinforcement amount mismatch")
            if addition:
                _require(pair in set(itertools.combinations(sorted(hits), 2)) and edge["reinforcement_before"] == base,
                         "reinforcement hit mismatch")
                reinforced.add(pair)
            _require(edge["after_weight"] == (round(base + 1.0, 6) if addition else base)
                     and edge["after_last_event_id"] == audit["event_id"], "final weight mismatch")
            _require(edge["weight_changed"] == (before != edge["after_weight"]), "weight change flag mismatch")
            _require(edge["active_source_support"] == bool(edge["source_record_ids"])
                     and len(edge["source_record_ids"]) == edge["co_count"], "edge source support mismatch")
        _require(reinforced == set(itertools.combinations(sorted(hits), 2)), "reinforcement pair missing")
        _require(observation["seeded_edges"] == sum(e["created_by"] == "static_seed" for e in transitions), "seed count mismatch")
    else:
        _require(not transitions and observation["seeded_edges"] == 0, "nonmutating event has weight changes")
    n, frequencies = selection["active_record_count"], selection["mark_frequencies"]
    _require(type(n) is int and n >= 0 and all(type(v) is int and 0 < v <= n for v in frequencies.values()),
             "invalid static frequency counts")
    live_edges = {}
    for edge in selection["edge_statistics"]:
        pair = (edge["a"], edge["b"])
        _require(pair[0] < pair[1] and pair not in live_edges, "live edge identity mismatch")
        live_edges[pair] = edge
        count = edge["co_count"]
        _require(edge["active_source_support"] is True and count == len(edge["source_record_ids"])
                 and 0 < count <= min(frequencies[edge["a"]], frequencies[edge["b"]]), "static source count mismatch")
        p_ab = count / n
        npmi = 1.0 if p_ab == 1.0 else math.log(p_ab / ((frequencies[edge["a"]] / n) * (frequencies[edge["b"]] / n))) / -math.log(p_ab)
        expected = round(npmi, 4) if npmi > 0 else 0.0
        _require(edge["static_score"] == expected, "static NPMI mismatch")
        _require(edge["effective_score"] == (edge["dynamic_score"] or edge["static_score"]), "effective weight mismatch")
    if observation["applied"]:
        for edge in transitions:
            pair = (edge["a"], edge["b"])
            if edge["active_source_support"] and edge["after_weight"] > 0:
                _require(pair in live_edges and live_edges[pair]["dynamic_score"] == edge["after_weight"]
                         and live_edges[pair]["source_record_ids"] == edge["source_record_ids"]
                         and live_edges[pair]["static_score"] == edge["static_score"], "committed/live weight mismatch")
            else:
                _require(pair not in live_edges, "unsupported/zero dynamic edge entered ranking")
    ranked = selection["ranked_candidates"]
    expected_order = sorted(ranked, key=lambda c: (-c["direct_match_count"], -c["effective_score"], -c["static_score"], -c["record_id"]))
    _require(ranked == expected_order and [c["rank"] for c in ranked] == list(range(1, len(ranked) + 1)), "ranking order mismatch")
    for candidate in ranked:
        evidence = candidate["evidence"]
        for item in evidence:
            pair = tuple(sorted((item["from_mark"], item["mark"])))
            edge = live_edges.get(pair)
            _require(edge is not None, "ranking evidence has no live edge")
            _require(all(item[key] == edge[key] for key in ("source_record_ids", "co_count", "static_score", "dynamic_score", "effective_score", "context", "dynamic_last_event_id")),
                     "ranking evidence disagrees with live edge")
        _require(candidate["direct_match_count"] == len(candidate["direct_marks"])
                 and candidate["effective_score"] == sum(e["effective_score"] for e in evidence)
                 and candidate["static_score"] == sum(e["static_score"] for e in evidence)
                 and candidate["dynamic_score"] == round(sum(e["dynamic_score"] for e in evidence), 6), "ranking arithmetic mismatch")
    if retrieval is None:
        return
    materials = retrieval["materials"]
    _require(selection["selected_record_ids"] == [m["record_id"] for m in materials]
             == [c["record_id"] for c in ranked[:3]], "selected ranking mismatch")
    _require(len(selection["selected"]) == len(materials), "selected receipt mismatch")
    for material, selected, candidate in zip(materials, selection["selected"], ranked):
        payload = material["model_payload"]
        _require(selected["source_id"] == candidate["source_id"] == material["source_id"]
                 and candidate["text_sha256"] == text_digest(material["stored_text"])
                 and candidate["quote_sha256"] == text_digest(material["quote"])
                 and selected["preview_sha256"] == text_digest(payload["text"]), "selected content digest mismatch")
        _require(candidate["marks"] == material["marks"] and candidate["evidence"] == payload["evidence"]
                 and round(candidate["effective_score"], 6) == payload["ranking_score"], "selected evidence mismatch")
