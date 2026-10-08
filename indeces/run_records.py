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

from .scratch import CanonicalSnapshot, canonical, read_records, retention_checkpoint
from .contracts import validated_token_usage


MAX_PDF_METADATA_BYTES = 512 * 1024
MODEL_PROJECTION = "citation_material_v1"
SELECTOR_MODEL_PROJECTION = "citation_material_selector_v1"
SELECTOR_RECEIPT_SCHEMA = "selector_receipt_v1"
NEIGHBORHOOD_AUDIT_SCOPE = "direct_hit_neighborhood_v1"
NEIGHBORHOOD_FREQUENCY_SCOPE = "edge_endpoints_v1"
_MODEL_MATERIAL_FIELDS = (
    "id", "source_id", "scope", "text", "text_truncated", "quote", "marks",
    "direct_marks", "expanded_marks", "ranking_mode", "weight_basis",
    "static_score", "dynamic_score", "ranking_score",
)


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


def _native_json_tree(value):
    """Keep deepcopy semantics for JSON-compatible nonnative caller objects."""
    pending = [value]
    while pending:
        item = pending.pop()
        item_type = type(item)
        if item_type is dict:
            if any(type(key) is not str for key in item):
                return False
            pending.extend(item.values())
        elif item_type is list:
            pending.extend(item)
        elif item_type not in (str, int, float, bool, type(None)):
            return False
    return True


def freeze_retrieval(db, scope, event_id, query, records, graph_audit):
    """Called synchronously immediately after retrieval, before any await.

    Source-version catalogs and the declared graph evidence belong in scratch. The
    model view retains complete selected quotes, separately from that audit;
    version metadata is never fetched again after delivery.
    Schema-2 graph bytes cover only the direct-hit neighborhood; freezing this
    receipt does not fetch or reconstruct an omitted global graph snapshot.
    An internal CanonicalSnapshot owns immutable, validated audit bytes; decode
    them into an independent receipt while preserving the fresh digest check.
    """
    model_materials, materials, sources = [], [], {}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for index, record in enumerate(records, 1):
        citation_id = f"M{index}"
        # Graph evidence can contain every supporting record ID and context
        # mark. It remains frozen in graph_audit, rather than growing the
        # model request each time the knowledge library grows.
        payload = {key: deepcopy(record[key]) for key in _MODEL_MATERIAL_FIELDS}
        payload.update(citation_id=citation_id, citation_marker=f"[{citation_id}]")
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
    if type(graph_audit) is CanonicalSnapshot:
        audit_snapshot = graph_audit.decode()
        if type(audit_snapshot) is not dict:
            raise ValueError("graph audit snapshot must contain an object")
        audit_sha256 = graph_audit.sha256
    else:
        audit_content = canonical(graph_audit)
        audit_snapshot = json.loads(audit_content) if _native_json_tree(graph_audit) else deepcopy(graph_audit)
        audit_sha256 = hashlib.sha256(audit_content).hexdigest()
    selected_contract = "selector_receipt" in audit_snapshot.get("selection", {})
    result = {"version": 2 if selected_contract else 1, "scope": scope, "event_id": event_id, "query": query,
              "model_projection": SELECTOR_MODEL_PROJECTION if selected_contract else MODEL_PROJECTION,
              "model_materials": model_materials, "materials": materials, "sources": sources,
              "graph_audit": audit_snapshot, "graph_audit_sha256": audit_sha256}
    if selected_contract:
        result["selector_receipt_sha256"] = digest(audit_snapshot["selection"]["selector_receipt"])
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


def _has_model_projection(record):
    if "model_projection" not in record:
        return False
    _require(record["model_projection"] in (MODEL_PROJECTION, SELECTOR_MODEL_PROJECTION),
             "unsupported model material projection")
    return True


def _validate_selection_contract(audit, retrieval=None):
    """Check optional selector receipts without trusting the strategy code.

    The inventory covers the eligible ranked input, rather than all library
    records. Unselected bodies are represented by identities and hashes; this
    establishes receipt consistency, not their semantic correctness.
    """
    selection = audit.get("selection", {})
    ranked = selection.get("ranked_candidates", [])
    present = "selector_receipt" in selection
    declared = "selector_contract" in selection
    _require(present == declared, "selector graph contract missing")
    if declared:
        _require(selection["selector_contract"] == SELECTOR_RECEIPT_SCHEMA,
                 "unsupported selector graph contract")
    if retrieval is not None:
        _require(type(retrieval["version"]) is int and retrieval["version"] in (1, 2),
                 "unsupported retrieval record")
        modern = retrieval["version"] == 2
        if modern:
            _require(retrieval.get("model_projection") == SELECTOR_MODEL_PROJECTION
                     and present and "selector_receipt_sha256" in retrieval,
                     "selector material contract missing")
            _require(retrieval["selector_receipt_sha256"] == digest(selection["selector_receipt"]),
                     "selector material receipt digest mismatch")
        else:
            _require(not present and "selector_receipt_sha256" not in retrieval
                     and retrieval.get("model_projection") != SELECTOR_MODEL_PROJECTION,
                     "selector material contract downgrade")
    if not present:
        return ranked[:3]
    receipt = selection["selector_receipt"]
    _require(type(receipt) is dict and set(receipt) == {
        "schema", "policy", "input", "candidate_set", "input_sha256",
        "selected_record_ids", "exclusions", "result_bindings"}, "selector receipt fields mismatch")
    _require(receipt["schema"] == SELECTOR_RECEIPT_SCHEMA, "unsupported selector receipt")
    _require(selection["limits"] == {
        "direct_marks": 64 if "concept_policy" in selection else 4,
        "neighbors_per_hit": 5, "expanded_marks": 2, "references": 3,
        "total_text_characters": 400, "entry_text_characters": 110},
        "selector limits changed")
    policy = receipt["policy"]
    _require(type(policy) is dict and set(policy) == {"id", "version"}
             and all(type(policy[key]) is str
                     and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", policy[key])
                     for key in ("id", "version")), "selector policy identity invalid")
    request = receipt["input"]
    _require(type(request) is dict and set(request) == {
        "scope", "event_id", "query_sha256", "context_query", "context_query_sha256",
        "ranking_mode", "retrieval_policy"}, "selector input fields mismatch")
    _require(type(request["context_query"]) is str
             and request["context_query_sha256"] == text_digest(request["context_query"]),
             "selector context identity mismatch")
    _require(request["scope"] == audit["scope"] and request["event_id"] == audit["event_id"]
             and request["query_sha256"] == text_digest(audit["request"]["query"])
             and request["ranking_mode"] == selection.get("ranking_mode", "dynamic")
             and request["retrieval_policy"] == (
                 "concept_v1" if "concept_policy" in selection else "legacy_v1"),
             "selector input identity mismatch")
    if "context_query" in audit["request"]:
        _require(request["context_query"] == audit["request"]["context_query"],
                 "selector context input mismatch")
    candidate_set = receipt["candidate_set"]
    _require(type(candidate_set) is dict and set(candidate_set) == {
        "scope", "count", "inventory", "sha256"}, "selector candidate set fields mismatch")
    inventory = candidate_set["inventory"]
    _require(candidate_set["scope"] == "eligible_ranked_candidates_v1"
             and type(candidate_set["count"]) is int and type(inventory) is list
             and candidate_set["count"] == len(inventory) == len(ranked)
             and candidate_set["sha256"] == digest(inventory), "selector candidate set identity mismatch")
    _require(receipt["input_sha256"] == digest({"input": request,
             "candidate_set_sha256": candidate_set["sha256"], "limits": selection["limits"]}),
             "selector input digest mismatch")
    identity_fields = {
        "record_id", "scope", "source_id", "fingerprint", "rank", "marks", "direct_marks",
        "expanded_marks", "direct_match_count", "effective_score", "static_score", "dynamic_score",
        "ranking_score", "text_characters", "quote_characters", "text_sha256", "quote_sha256"}
    candidate_by_id, identity_by_id = {}, {}
    for identity, candidate in zip(inventory, ranked):
        _require(type(identity) is dict and set(identity) == identity_fields,
                 "selector candidate identity fields mismatch")
        record_id = identity["record_id"]
        _require(type(record_id) is int and record_id > 0 and record_id not in identity_by_id
                 and identity["scope"] == audit["scope"] and type(identity["source_id"]) is str
                 and bool(identity["source_id"]), "selector candidate identity invalid")
        _require(all(type(identity[key]) is str and re.fullmatch(r"[0-9a-f]{64}", identity[key])
                     for key in ("fingerprint", "text_sha256", "quote_sha256")),
                 "selector candidate hash invalid")
        _require(all(type(identity[key]) is int and identity[key] >= 0
                     for key in ("text_characters", "quote_characters")),
                 "selector candidate length invalid")
        expected = {key: candidate[key] for key in identity_fields}
        _require(canonical(identity) == canonical(expected), "selector candidate inventory mismatch")
        ranking_score = (candidate["relevance_score"] if "concept_policy" in selection
                         else round(candidate["effective_score"], 6))
        _require(type(identity["ranking_score"]) in (int, float)
                 and canonical(identity["ranking_score"]) == canonical(ranking_score),
                 "selector candidate ranking score mismatch")
        candidate_by_id[record_id] = candidate
        identity_by_id[record_id] = identity
    selected_ids, exclusions = receipt["selected_record_ids"], receipt["exclusions"]
    _require(type(selected_ids) is list and all(type(value) is int for value in selected_ids)
             and len(selected_ids) <= selection["limits"]["references"]
             and len(selected_ids) == len(set(selected_ids))
             and set(selected_ids) <= set(identity_by_id), "selector selected identities invalid")
    _require(type(exclusions) is list, "selector exclusions invalid")
    excluded_ids = []
    for exclusion in exclusions:
        _require(type(exclusion) is dict and set(exclusion) == {"record_id", "reason"}
                 and type(exclusion["record_id"]) is int and type(exclusion["reason"]) is str
                 and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exclusion["reason"]),
                 "selector exclusion invalid")
        excluded_ids.append(exclusion["record_id"])
    _require(len(excluded_ids) == len(set(excluded_ids))
             and not set(excluded_ids) & set(selected_ids)
             and set(excluded_ids) | set(selected_ids) == set(identity_by_id),
             "selector candidate partition mismatch")
    _require(selection["selected_record_ids"] == selected_ids
             and len(selection["selected"]) == len(selected_ids), "selector selection receipt mismatch")
    bindings = receipt["result_bindings"]
    _require(type(bindings) is list and len(bindings) == len(selected_ids),
             "selector result bindings mismatch")
    for index, (record_id, binding, selected) in enumerate(zip(
            selected_ids, bindings, selection["selected"]), 1):
        identity = identity_by_id[record_id]
        _require(type(binding) is dict and set(binding) == {
            "record_id", "source_id", "scope", "fingerprint", "quote_sha256", "preview_sha256",
            "text_characters", "text_truncated"}, "selector result binding fields mismatch")
        binding_identity_keys = ("record_id", "source_id", "scope", "fingerprint", "quote_sha256")
        _require(canonical({key: binding[key] for key in binding_identity_keys})
                 == canonical({key: identity[key] for key in binding_identity_keys}),
            "selector result identity mismatch")
        characters = min(identity["text_characters"], selection["limits"]["entry_text_characters"])
        _require(type(binding["text_characters"]) is int and binding["text_characters"] == characters
                 and type(binding["text_truncated"]) is bool
                 and binding["text_truncated"] == (characters < identity["text_characters"])
                 and type(binding["preview_sha256"]) is str
                 and re.fullmatch(r"[0-9a-f]{64}", binding["preview_sha256"]),
                 "selector result preview invalid")
        expected_selected = {"rank": index, "record_id": record_id, "source_id": binding["source_id"],
                             **{key: binding[key] for key in (
                                 "text_characters", "text_truncated", "preview_sha256")}}
        _require(canonical(selected) == canonical(expected_selected),
                 "selector selected content mismatch")
    used_characters = sum(binding["text_characters"] for binding in bindings)
    _require(selection["used_text_characters"] == used_characters
             and used_characters <= selection["limits"]["total_text_characters"],
             "selector text budget mismatch")
    if retrieval is not None:
        materials = retrieval["materials"]
        _require([material["record_id"] for material in materials] == selected_ids,
                 "selector material identities mismatch")
        for material, binding in zip(materials, bindings):
            identity = identity_by_id[material["record_id"]]
            _require(material["scope"] == identity["scope"]
                     and material["source_id"] == identity["source_id"]
                     and material["fingerprint"] == identity["fingerprint"]
                     and text_digest(material["stored_text"]) == identity["text_sha256"]
                     and len(material["stored_text"]) == identity["text_characters"]
                     and text_digest(material["quote"]) == identity["quote_sha256"]
                     and len(material["quote"]) == identity["quote_characters"]
                     and text_digest(material["model_payload"]["text"]) == binding["preview_sha256"],
                     "selector frozen material identity mismatch")
    return [candidate_by_id[record_id] for record_id in selected_ids]


def _validate_projection_keys(payload, material):
    expected = set(_MODEL_MATERIAL_FIELDS) | {"citation_id", "citation_marker"}
    if "pdf_page_numbers" in material:
        expected.add("pdf_page_numbers")
    elif isinstance(payload, dict):
        _require("pdf_page_numbers" not in payload, "PDF pages have no frozen conversion metadata")
    _require(isinstance(payload, dict) and set(payload) == expected, "model material projection fields mismatch")


def _validate_model_projection(payload, material, candidate, selection):
    """Reconstruct the exact model view from independent frozen evidence."""
    _validate_projection_keys(payload, material)
    _require(candidate["record_id"] == material["record_id"]
             and candidate["source_id"] == material["source_id"]
             and candidate["marks"] == material["marks"], "model projection candidate mismatch")
    full = material["stored_text"]
    citation_id = material["citation_id"]
    expected = {
        "citation_id": citation_id, "citation_marker": f"[{citation_id}]",
        "id": material["record_id"], "source_id": material["source_id"], "scope": material["scope"],
        "text": full[:109] + "…" if len(full) > 110 else full,
        "text_truncated": len(full) > 110, "quote": material["quote"], "marks": material["marks"],
        "direct_marks": candidate["direct_marks"], "expanded_marks": candidate["expanded_marks"],
        "ranking_mode": selection.get("ranking_mode", "dynamic"),
        "weight_basis": selection.get("weight_basis", "dynamic_or_static"),
        "static_score": round(candidate["static_score"], 6),
        "dynamic_score": candidate["dynamic_score"],
        "ranking_score": round(candidate.get('relevance_score', candidate["effective_score"]), 6),
    }
    if "pdf_page_numbers" in material:
        expected["pdf_page_numbers"] = material["pdf_page_numbers"]
    # Exact JSON bytes also distinguish bool/int and signed-zero changes.
    _require(canonical(payload) == canonical(expected), "model material projection mismatch")


def _validate_legacy_model_material(payload, material, candidate, selection):
    """Keep the complete-evidence contract for already retained receipts."""
    _require("evidence" in payload, "legacy model evidence missing")
    _require(candidate["record_id"] == material["record_id"]
             and candidate["source_id"] == material["source_id"]
             and candidate["marks"] == material["marks"], "legacy model candidate mismatch")
    if "ranking_mode" in selection:
        _require(payload.get("ranking_mode") == selection["ranking_mode"]
                 and payload.get("weight_basis") == selection["weight_basis"], "model material weight basis mismatch")
    _require(candidate["evidence"] == payload["evidence"]
             and candidate["direct_marks"] == payload["direct_marks"]
             and candidate["expanded_marks"] == payload["expanded_marks"]
             and round(candidate["static_score"], 6) == payload["static_score"]
             and candidate["dynamic_score"] == payload["dynamic_score"]
             and round(candidate["effective_score"], 6) == payload["ranking_score"], "selected evidence mismatch")


def validate_retrieval(record):
    _require(type(record["version"]) is int and record["version"] in (1, 2), "unsupported retrieval record")
    projected = _has_model_projection(record)
    selected_candidates = _validate_selection_contract(record["graph_audit"], record)
    _require(digest(record["graph_audit"]) == record["graph_audit_sha256"], "graph audit digest mismatch")
    materials = record["materials"]
    _require(len(materials) <= 3, "too many recalled materials")
    _require([m["model_payload"] for m in materials] == record["model_materials"], "model/material binding mismatch")
    _require(set(record["sources"]) == {m["source_id"] for m in materials}, "source catalog mismatch")
    for index, material in enumerate(materials, 1):
        payload, source = material["model_payload"], record["sources"][material["source_id"]]
        if projected:
            _validate_projection_keys(payload, material)
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
        selection = record["graph_audit"]["selection"]
        _require(len(selected_candidates) >= index, "model material candidate missing")
        if projected:
            _validate_model_projection(payload, material, selected_candidates[index - 1], selection)
        else:
            # Missing the projection marker cannot downgrade a compact view
            # to the historical complete-evidence contract.
            _validate_legacy_model_material(payload, material, selected_candidates[index - 1], selection)
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


def _bot_reply_data(text):
    """Decode the provider output independently of the runtime decision receipt."""
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate bot decision JSON key")
            result[key] = value
        return result
    _require(isinstance(text, str), "bot decision provider text invalid")
    try:
        data = json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite bot decision JSON")))
    except (ValueError, RecursionError):
        raise ValueError("bot decision provider JSON invalid") from None
    _require(isinstance(data, dict) and set(data) == {"action", "text"}
             and data["action"] in {"reply", "skip"} and isinstance(data["text"], str),
             "bot decision provider schema invalid")
    _require((data["action"] == "reply" and bool(data["text"].strip()))
             or (data["action"] == "skip" and data["text"] == ""), "bot decision action/text invalid")
    return data


def _validate_reply_request(context, request, *, bot=False, partial=False):
    label = "retained actual reply context mismatch" if partial else "actual adaptor input mismatch"
    _require(request["input"] == context["messages"] and request["instructions"] == context["instructions"], label)
    if bot:
        from .prompts import BOT_REPLY_SCHEMA
        _require(context.get("response_schema") == BOT_REPLY_SCHEMA, "bot reply context schema mismatch")
        _require(request.get("text", {}).get("format")
                 == {"type": "json_schema", "name": "reply", "strict": True, "schema": BOT_REPLY_SCHEMA},
                 "actual bot reply schema mismatch")


_MODEL_SELECTION_POLICY = {"id": "model_npmi_labels", "version": "v1"}
_MODEL_SELECTION_BINDING = (
    "policy", "scope", "event_id", "query_sha256", "context_query_sha256",
    "candidate_count", "candidate_set_sha256", "seen_record_ids",
    "seen_candidates_sha256", "omitted_record_ids", "omitted_candidates_sha256",
    "messages_sha256", "instructions_sha256", "schema_sha256", "input_sha256",
)


def _model_selection_json(text, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate selection JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid selection JSON constant")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(label) from None


def _model_selection_binding(fields):
    binding = {key: fields[key] for key in _MODEL_SELECTION_BINDING}
    _require(binding["policy"] == _MODEL_SELECTION_POLICY, "unknown model selection policy")
    _require(all(type(binding[key]) is str and binding[key] for key in ("scope", "event_id")),
             "model selection scope/event invalid")
    _require(all(type(binding[key]) is str and re.fullmatch(r"[0-9a-f]{64}", binding[key])
                 for key in _MODEL_SELECTION_BINDING if key.endswith("sha256")),
             "model selection hash invalid")
    _require(binding["input_sha256"] == digest({key: value for key, value in binding.items()
                                                if key != "input_sha256"}),
             "model selection input digest mismatch")
    seen, omitted = binding["seen_record_ids"], binding["omitted_record_ids"]
    _require(type(seen) is list and type(omitted) is list
             and all(type(record_id) is int and record_id > 0 for record_id in seen + omitted)
             and len(set(seen + omitted)) == len(seen + omitted)
             and type(binding["candidate_count"]) is int
             and binding["candidate_count"] == len(seen) + len(omitted),
             "model selection candidate partition invalid")
    return binding


def _validate_model_selection(events, report, *, start=None, graph=None, partial=False):
    """Bind one bounded model choice to its input, transport and frozen receipt.

    Available retained evidence is checked, but expired input/call/material
    evidence never establishes a complete model-selection traversal.
    """
    inputs = [f for e, f in events if e == "model_selection_input"]
    decisions = [f for e, f in events if e == "model_selection_decision"]
    call_fields = [f for _, f in events if f.get("stage") == "selection"]
    marker = start.get("model_selection_policy") if start is not None else None
    receipt = graph.get("selection", {}).get("selector_receipt") if graph else None
    model_receipt = receipt is not None and receipt.get("policy", {}).get("id") == "model_npmi_labels"
    active = marker is not None or bool(inputs or decisions or call_fields) or model_receipt
    if not active:
        return False
    _require(len(inputs) <= 1 and len(decisions) <= 1, "duplicate model selection stage")
    if marker is not None:
        _require(marker == _MODEL_SELECTION_POLICY, "unknown turn model selection policy")
    ends = [f for e, f in events if e == "turn_end"]
    successful = bool(ends and ends[0].get("status") in ("delivered", "skipped"))
    if not inputs and not decisions and not call_fields and graph is None and not successful:
        # Retrieval may fail before choose writes any input. An unfinished or
        # failed turn does not establish that model selection was performed.
        return True
    if graph is not None:
        _require(model_receipt and receipt["policy"] == _MODEL_SELECTION_POLICY,
                 "model selection receipt missing or downgraded")
    bindings = [_model_selection_binding(fields) for fields in inputs + decisions]
    _require(not bindings or all(binding == bindings[0] for binding in bindings),
             "model selection decision input mismatch")
    binding = bindings[0] if bindings else None
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
        _require(type(fields["required_selection_count"]) is int
                 and fields["required_selection_count"] == required
                 and canonical(schema) == canonical({
                     "type": "object", "additionalProperties": False,
                     "properties": {"selected_record_ids": {"type": "array", "minItems": required,
                         "maxItems": required, "items": {"type": "integer"}}},
                     "required": ["selected_record_ids"]}), "model selection output schema mismatch")
        _require(type(fields["planning_reservation"]) is int and fields["planning_reservation"] >= 0
                 and type(fields["planning_input_limit"]) is int and fields["planning_input_limit"] > 0
                 and fields["omission_reason"] == ("input_budget" if binding["omitted_record_ids"] else None),
                 "model selection planning declaration invalid")
        payload = _model_selection_json(messages[0]["content"], "model selection payload invalid")
        _require(type(payload) is dict and set(payload) == {
            "query", "context_query", "ranking_mode", "retrieval_policy", "required_selection_count",
            "candidate_pool_count", "model_visible_count", "model_omitted_count", "candidates"},
            "model selection payload fields mismatch")
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
    if not partial:
        _require(len(inputs) == 1, "model selection input missing")
        _require(not selection_events or len(starts) == 1, "model selection call start missing")
    if inputs and starts:
        _require(starts[0]["planning_reservation"] == inputs[0]["planning_reservation"]
                 and starts[0]["budget"]["input_tokens"] == inputs[0]["planning_input_limit"],
                 "model selection call planning mismatch")
    requests = [f for e, f in selection_events if e == "http_request"]
    _require(len(requests) <= 2 and len({f["path"] for f in requests}) == len(requests),
             "duplicate model selection transport")
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
        for fields in requests:
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
                _require(type(data) is dict and set(data) == {"selected_record_ids"}
                         and canonical(data["selected_record_ids"]) == canonical(selected),
                         "model selection output/decision mismatch")
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
        _require(type(data) is dict and set(data) == {"selected_record_ids"}
                 and canonical(data["selected_record_ids"]) == canonical(receipt["selected_record_ids"]),
                 "retained model selection output/receipt mismatch")
    if partial:
        if not inputs:
            report["warnings"].append("expired_model_selection_input")
        if not decisions:
            report["warnings"].append("expired_model_selection_decision")
        if graph is None:
            report["warnings"].append("expired_model_selection_material_binding")
    return True


def _validate_bot_decision(decision, events, *, retrieval=None, context=None, partial=False):
    data = _bot_reply_data(decision["model_result"]["text"])
    _require(decision["action"] == data["action"] and decision["text"] == data["text"],
             "bot decision/provider output mismatch")
    if retrieval is not None:
        _require(decision["retrieval_sha256"] == digest(retrieval), "bot decision retrieval link mismatch")
    calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply" and f.get("status") == "completed"]
    _require(len(calls) <= 1 and (partial or len(calls) == 1), "bot decision call evidence missing")
    if calls:
        _require(calls[0]["result"] == decision["model_result"], "bot decision model receipt mismatch")
    _require(partial or context is not None, "bot decision reply context missing")
    if context is not None:
        from .prompts import BOT_REPLY_SCHEMA
        _require(context.get("response_schema") == BOT_REPLY_SCHEMA, "bot reply context schema mismatch")
        requests = [f for e, f in events if e == "http_request" and f.get("path") == "/responses"
                    and (not calls or f["call_id"] == calls[0]["call_id"])]
        _require(len(requests) <= 1 and (partial or len(requests) == 1), "bot decision request evidence missing")
        if requests:
            _validate_reply_request(context, requests[0]["payload"], bot=True, partial=partial)
    return data


def _validate_known_usage(events, end, *, partial=False):
    # Older call receipts did not carry usage separately from a successful
    # result. Preserve them; new receipts bind failed/cancelled usage as well.
    if "known_usage" not in end:
        return
    known = end["known_usage"]
    _require(known is None or (validated_token_usage(known) == known
             and set(known) == {"input_tokens", "output_tokens"}), "known generation usage schema invalid")
    responses = [fields for event, fields in events
                 if event == "http_response" and fields.get("path") == "/responses"]
    _require(len(responses) <= 1, "duplicate generation usage receipt")
    if responses:
        payload = responses[0]["payload"]
        observed = validated_token_usage(payload.get("usage")) if isinstance(payload, dict) else None
        _require(known == observed, "known generation usage/transport mismatch")
    elif known is not None and not partial:
        raise ValueError("known generation usage transport evidence missing")
    if known is not None and not partial:
        requests = [fields for event, fields in events
                    if event == "http_request" and fields.get("path") == "/responses"]
        _require(len(requests) == 1, "known generation usage request evidence missing")
    if "generation_usage_received" in end:
        _require(type(end["generation_usage_received"]) is bool
                 and end["generation_usage_received"] == (known is not None), "generation usage receipt flag mismatch")
    if "generation_request_started" in end and "remote_usage_unknown" in end:
        started, unknown = end["generation_request_started"], end["remote_usage_unknown"]
        _require(type(started) is bool and type(unknown) is bool
                 and unknown == (started and known is None), "unknown generation usage classification mismatch")
        _require(known is None or started, "known usage without generation request")


def _declared_input_limit(start):
    """Bind a narrower per-call gate while preserving prior stage-only receipts."""
    stage_limit = start["budget"]["input_tokens"]
    _require(type(stage_limit) is int and stage_limit > 0, "input stage budget invalid")
    modern = "effective_input_limit" in start or "requested_input_limit" in start
    if not modern:
        return stage_limit, False
    _require("effective_input_limit" in start and "requested_input_limit" in start,
             "input allowance declaration incomplete")
    requested, effective = start["requested_input_limit"], start["effective_input_limit"]
    _require(requested is None or (type(requested) is int and requested > 0),
             "requested input allowance invalid")
    _require(type(effective) is int and 0 < effective <= stage_limit
             and effective == (min(stage_limit, requested) if requested is not None else stage_limit),
             "effective input allowance mismatch")
    return effective, True


def _validate_call(events):
    starts = [f for e, f in events if e == "call_start"]
    ends = [f for e, f in events if e in {"call_end", "call_rejected"}]
    _require(len(starts) == 1 and len(ends) <= 1, "call lifecycle mismatch")
    _require(all(f.get("trace_id") == starts[0]["trace_id"]
                 and f.get("stage") in (None, starts[0]["stage"]) for _, f in events),
             "call event stage/trace mismatch")
    if not ends:
        return "incomplete"
    start, end = starts[0], ends[0]
    _require(start["stage"] == end["stage"] and start["trace_id"] == end["trace_id"], "call stage/trace mismatch")
    effective_input_limit, modern_input_gate = _declared_input_limit(start)
    if "usage_receipt_version" in start:
        _require(type(start["usage_receipt_version"]) is int and start["usage_receipt_version"] == 1
                 and "known_usage" in end, "generation usage receipt version/schema mismatch")
    _validate_known_usage(events, end)
    gates = [f for e, f in events if e == "input_gate"]
    if modern_input_gate:
        _require(len(gates) <= 1, "duplicate input budget gate")
        if gates:
            gate = gates[0]
            _require(type(gate["input_tokens"]) is int and gate["input_tokens"] >= 0
                     and type(gate["limit"]) is int and gate["limit"] == effective_input_limit
                     and type(gate.get("stage_limit")) is int
                     and gate["stage_limit"] == start["budget"]["input_tokens"]
                     and type(gate["admitted"]) is bool
                     and gate["admitted"] == (gate["input_tokens"] <= effective_input_limit),
                     "effective input budget gate mismatch")
            count_responses = [f for e, f in events if e == "http_response"
                               and f.get("path") == "/responses/input_tokens"]
            _require(len(count_responses) == 1
                     and type(count_responses[0]["payload"]["input_tokens"]) is int
                     and gate["input_tokens"] == count_responses[0]["payload"]["input_tokens"],
                     "effective input count receipt mismatch")
        generation_requests = [f for e, f in events if e == "http_request" and f.get("path") == "/responses"]
        _require(not generation_requests or (len(gates) == 1 and gates[0]["admitted"] is True),
                 "generation bypassed effective input gate")
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
    counted = responses[0]["payload"]["input_tokens"]
    _require(type(counted) is int and 0 <= counted <= effective_input_limit and len(gates) == 1
             and gates[0]["input_tokens"] == counted and gates[0]["limit"] == effective_input_limit
             and gates[0]["admitted"] is True, "input budget gate mismatch")
    response, result = responses[1]["payload"], end["result"]
    _require(response["status"] == "completed", "provider completion mismatch")
    usage = response["usage"]
    for key in ("input_tokens", "output_tokens"):
        limit = effective_input_limit if key == "input_tokens" else budget[key]
        _require(type(usage[key]) is int and 0 <= usage[key] <= limit and usage[key] == result[key], "response usage mismatch")
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
    if ends:
        _validate_known_usage(events, ends[0], partial=True)
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
        if "stage_limit" in gate:
            _require(type(gate["stage_limit"]) is int and gate["stage_limit"] > 0
                     and gate["limit"] <= gate["stage_limit"], "retained input stage cap mismatch")
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
        if gates:
            _require(result["input_tokens"] <= gates[0]["limit"], "retained input usage cap mismatch")
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


def _validate_partial_turn(events, report, *, start=None):
    stages = {"model_selection_input": -1, "model_selection_decision": 0,
              "memory_observation": 1, "retrieval_record": 2, "reply_context": 3,
              "bot_reply_decision": 4, "answer_generated": 5, "delivery_start": 6,
              "answer_delivered": 7, "turn_end": 8}
    present = [stages[e] for e, _ in events if e in stages]
    _require(present == sorted(present) and len(present) == len(set(present)), "retained turn stage ordering mismatch")
    by_event = {e: f for e, f in events if e in stages}
    observation = by_event.get("memory_observation")
    if observation:
        _require(digest(observation["audit"]) == observation["audit_sha256"], "retained graph observation digest mismatch")
        validate_graph_audit(observation["audit"])
        if observation["audit"]["schema_version"] == 2:
            report["warnings"].append("graph_audit_hit_neighborhood_only")
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
    _validate_model_selection(events, report, start=start,
                              graph=retrieval["graph_audit"] if retrieval is not None
                              else observation["audit"] if observation else None, partial=True)
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
    decision, context = (by_event.get(e) for e in ("bot_reply_decision", "reply_context"))
    bot_data = _validate_bot_decision(decision, events, retrieval=retrieval, context=context, partial=True) if decision else None
    if bot_data and bot_data["action"] == "skip":
        _require(not any(e in {"answer_generated", "delivery_start", "answer_delivered", "failure_notice_delivered",
                              "failure_notice_unknown"} for e, _ in events), "skipped bot turn has generation/delivery evidence")
        if end:
            _require(end["status"] == "skipped" and end.get("reason") == "bot_reply_skipped", "bot skip terminal receipt mismatch")
    if end and end.get("status") == "skipped":
        _require(end.get("reason") == "bot_reply_skipped", "bot skip terminal receipt mismatch")
        _require(not any(e in {"answer_generated", "delivery_start", "answer_delivered", "failure_notice_delivered",
                              "failure_notice_unknown"} for e, _ in events), "skipped bot turn has generation/delivery evidence")
        _require(not bot_data or bot_data["action"] == "skip", "bot skip conflicts with reply decision")
        if not decision:
            report["warnings"].append("expired_bot_reply_decision")
    if generated:
        if bot_data:
            _require(bot_data["action"] == "reply" and generated["record"]["text"] == bot_data["text"]
                     and generated["model_result"] == decision["model_result"], "retained bot generated output mismatch")
        elif generated["model_result"]["text"] != generated["record"]["text"]:
            # An expired prefix can include the decision. Validate the remaining
            # raw/decoded binding, without claiming that its missing receipt or
            # author identity has been recovered.
            bot_data = _bot_reply_data(generated["model_result"]["text"])
            _require(bot_data["action"] == "reply" and bot_data["text"] == generated["record"]["text"],
                     "retained generated output mismatch")
            report["warnings"].append("expired_bot_reply_decision")
        calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply" and f.get("status") == "completed"]
        if calls:
            _require(len(calls) == 1 and calls[0]["result"] == generated["model_result"], "retained generated receipt mismatch")
    if end and delivered:
        _require(end["status"] == "delivered" and end["receipt"]["text"] == delivered["record"]["text"]
                 and end["receipt"]["ids"] == delivered["receipt_ids"], "retained delivery receipt mismatch")
    if by_event.get("delivery_start") and generated:
        _require(by_event["delivery_start"]["text"] == generated["record"]["text"], "retained delivery start output mismatch")
    if context:
        requests = [f for e, f in events if e == "http_request" and f["path"] == "/responses"]
        reply_call_ids = {f["call_id"] for e, f in events if e == "call_end" and f.get("stage") == "reply"}
        for request in requests:
            if request["call_id"] in reply_call_ids:
                _validate_reply_request(context, request["payload"], bot=bot_data is not None, partial=True)


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
    reports, diagnostics = [], []
    for trace, events in turns.items():
        starts = [f for e, f in events if e == "turn_start"]
        if not starts:
            diagnostic_starts = [f for e, f in events if e in ('diagnostic_summary_trial_start', 'diagnostic_trial_start')]
            if diagnostic_starts:
                report = {'trace_id': trace, 'status': 'invalid'}
                diagnostics.append(report)
                try:
                    endings = [f for e, f in events if e in ('diagnostic_summary_trial_end', 'diagnostic_trial_end')]
                    _require(len(diagnostic_starts) == len(endings) == 1, 'diagnostic boundaries missing or duplicated')
                    start, end = diagnostic_starts[0], endings[0]
                    _require(start['operator_approved'] is True and start['automatic_retries'] == 0
                             and start['checkpoint_update'] is False and end['checkpoint_updated'] is False,
                             'diagnostic authorization or checkpoint contract mismatch')
                    _require(not any(e in ('checkpoint_saved', 'answer_delivered', 'delivery_start', 'turn_end') for e, _ in events), 'diagnostic cannot claim chat delivery or checkpoint')
                    associated = [c for c in call_reports if c['trace_id'] == trace]
                    _require(len(associated) == 1 and associated[0]['status'] == 'complete'
                             and end['status'] == 'completed', 'diagnostic call incomplete or invalid')
                    if start.get('kind', 'summary') == 'summary':
                        _require(all(f.get('stage') in (None, 'summary') for e, f in events), 'diagnostic stage mismatch')
                        call = next(f for e, f in events if e == 'call_end')
                        _require(end['input_tokens'] == call['result']['input_tokens']
                                 and end['output_tokens'] == call['result']['output_tokens'], 'diagnostic usage mismatch')
                    elif start['kind'] == 'retrieval_reply':
                        _validate_partial_turn(events, {'warnings': []})
                        retrieved = next(f['record'] for e, f in events if e == 'retrieval_record')
                        context = next(f['messages'] for e, f in events if e == 'reply_context')
                        request = next(f['payload'] for e, f in events if e == 'http_request' and f['path'] == '/responses')
                        _require(request['input'] == context, 'diagnostic actual model context mismatch')
                        generated = next(f for e, f in events if e == 'answer_generated')
                        call = next(f for e, f in events if e == 'call_end')
                        _require(generated['model_result'] == call['result']
                                 and generated['record']['text'] == call['result']['text'], 'diagnostic generated text mismatch')
                        context_fields = next(f for e, f in events if e == 'reply_context')
                        _require(request['instructions'] == context_fields['instructions'], 'diagnostic instructions mismatch')
                        _require(end['input_tokens'] == call['result']['input_tokens']
                                 and end['output_tokens'] == call['result']['output_tokens'], 'diagnostic reply usage mismatch')
                        validate_answer(generated['record'], retrieved)
                    else:
                        raise ValueError('unknown diagnostic kind')
                    report['status'] = 'complete'
                except (ValueError, KeyError, TypeError, IndexError, AttributeError, StopIteration) as error:
                    issues.append({'trace_id': trace, 'reason': str(error) if type(error) is ValueError else 'diagnostic schema invalid'})
                continue
            # Passive label traces do not have conversation stages.
            is_turn = any(e in {"model_selection_input", "model_selection_decision",
                                  "memory_observation", "retrieval_record", "knowledge_retrieved", "reply_context",
                                 "bot_reply_decision", "answer_generated", "delivery_start", "answer_delivered", "turn_end"}
                          or f.get("stage") in {"reply", "summary", "selection"} for e, f in events)
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
            if trace in partial_traces and (starts[0].get("model_selection_policy") is not None
                    or any(e in {"model_selection_input", "model_selection_decision"}
                           or f.get("stage") == "selection" for e, f in events)):
                _validate_partial_turn(events, report, start=starts[0])
                _require(not any(c["status"] == "invalid" for c in call_reports if c["trace_id"] == trace),
                         "retained turn has invalid call evidence")
                report["status"] = "retention_partial"
                report["warnings"].append("retained_model_selection_contract")
                continue
            ends = [f for e, f in events if e == "turn_end"]
            retrievals = [f for e, f in events if e == "retrieval_record"]
            generated = [f for e, f in events if e == "answer_generated"]
            delivered = [f for e, f in events if e == "answer_delivered"]
            observations = [f for e, f in events if e == "memory_observation"]
            decisions = [f for e, f in events if e == "bot_reply_decision"]
            model_inputs = [f for e, f in events if e == "reply_context"]
            delivery_starts = [f for e, f in events if e == "delivery_start"]
            _require(all(len(stage) <= 1 for stage in (ends, retrievals, generated, delivered, decisions, model_inputs, delivery_starts)),
                     "duplicate run stage")
            _require(len(observations) <= 1, "duplicate graph observation")
            is_bot = starts[0]["input"].get("author_is_bot", False)
            _require(type(is_bot) is bool, "turn bot author flag invalid")
            _require(not decisions or is_bot, "bot decision has no bot author declaration")
            stage_order = {"turn_start": -2, "model_selection_input": -1, "model_selection_decision": 0,
                           "memory_observation": 1, "retrieval_record": 2,
                           "reply_context": 3, "bot_reply_decision": 4, "answer_generated": 5,
                           "delivery_start": 6, "answer_delivered": 7, "turn_end": 8}
            stages = [stage_order[e] for e, _ in events if e in stage_order]
            _require(stages == sorted(stages), "run stage ordering mismatch")
            if observations:
                graph = observations[0]["audit"]
                _require(observations[0]["audit_sha256"] == digest(graph), "graph observation digest mismatch")
                _require(graph["event_id"] == starts[0]["message_id"] and graph["scope"] == starts[0]["knowledge_scope"]
                         and graph["request"]["query"] == starts[0]["input"]["text"], "graph observation input mismatch")
                validate_graph_audit(graph)
                if graph["schema_version"] == 2:
                    report["warnings"].append("graph_audit_hit_neighborhood_only")
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
                _require(not generated and not delivered and not decisions, "answer/decision without retrieval")
            _validate_model_selection(events, report, start=starts[0],
                                      graph=retrieval["graph_audit"] if retrievals
                                      else observations[0]["audit"] if observations else None)
            bot_data = None
            if decisions:
                _require(len(retrievals) == len(model_inputs) == 1, "bot decision missing input evidence")
                bot_data = _validate_bot_decision(decisions[0], events, retrieval=retrieval,
                                                 context=model_inputs[0])
                context = json.loads(model_inputs[0]["messages"][0]["content"].split("\n", 1)[1])
                _require(context["memory_citations"] == retrieval["model_materials"], "actual reply context mismatch")
                _require(model_inputs[0]["retrieval_sha256"] == digest(retrieval), "reply context retrieval mismatch")
                _require(not any(c["status"] != "complete" for c in call_reports if c["trace_id"] == trace),
                         "bot decision has incomplete/failed call evidence")
                if bot_data["action"] == "skip":
                    _require(not generated and not delivered and not delivery_starts
                             and not any(e in {"failure_notice_delivered", "failure_notice_unknown"} for e, _ in events),
                             "skipped bot turn has generation/delivery evidence")
                    _require(not ends or (ends[0]["status"] == "skipped" and ends[0].get("reason") == "bot_reply_skipped"),
                             "bot skip terminal receipt mismatch")
                if generated:
                    _require(bot_data["action"] == "reply" and generated[0]["record"]["text"] == bot_data["text"]
                             and generated[0]["model_result"] == decisions[0]["model_result"], "bot generated output mismatch")
            elif is_bot:
                _require(not generated and not delivered and not delivery_starts, "bot answer without decision")
            if generated and delivery_starts:
                _require(delivery_starts[0]["text"] == generated[0]["record"]["text"], "delivery start output mismatch")
            if not ends:
                report["warnings"].append("missing_turn_end")
                continue
            if ends[0]["status"] == "delivered":
                _require(len(retrievals) == len(generated) == len(delivered) == 1, "delivered turn missing evidence")
                _require(delivered[0]["record"]["text"] == ends[0]["receipt"]["text"]
                         and delivered[0]["receipt_ids"] == ends[0]["receipt"]["ids"], "delivery receipt mismatch")
                _require(len(model_inputs) == 1, "reply input evidence missing")
                context = json.loads(model_inputs[0]["messages"][0]["content"].split("\n", 1)[1])
                _require(context["memory_citations"] == retrieval["model_materials"], "actual reply context mismatch")
                _require(model_inputs[0]["retrieval_sha256"] == digest(retrieval), "reply context retrieval mismatch")
                calls = [f for e, f in events if e == "call_end" and f.get("stage") == "reply" and f.get("status") == "completed"]
                _require(len(calls) == 1 and (bot_data is not None or calls[0]["result"]["text"] == generated[0]["record"]["text"]),
                         "reply call evidence missing or mismatched")
                _require(generated[0]["model_result"] == calls[0]["result"], "generated model receipt mismatch")
                _require(not any(c["status"] != "complete" for c in call_reports if c["trace_id"] == trace),
                         "delivered turn has incomplete/failed call evidence")
                requests = [f for e, f in events if e == "http_request" and f["call_id"] == calls[0]["call_id"] and f["path"] == "/responses"]
                _require(len(requests) == 1, "actual adaptor input mismatch")
                _validate_reply_request(model_inputs[0], requests[0]["payload"], bot=is_bot)
                report["status"] = "complete"
            elif ends[0]["status"] == "skipped":
                _require(is_bot and bot_data is not None and bot_data["action"] == "skip"
                         and ends[0].get("reason") == "bot_reply_skipped", "skipped turn missing bot decision evidence")
                report["status"] = "skipped"
            else:
                _require(not delivered, "confirmed receipt conflicts with failed turn")
                report["status"] = "failed"
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, ZeroDivisionError) as error:
            report["status"] = "invalid"
            # Fixed diagnostic messages; never echo private source/reply text.
            issues.append({"trace_id": trace, "reason": str(error) if type(error) is ValueError else "record schema invalid"})
    return {"turns": reports, 'diagnostics': diagnostics,
            'diagnostic_counts': {s: sum(r['status'] == s for r in diagnostics) for s in ('complete', 'invalid')}, "counts": {s: sum(r["status"] == s for r in reports)
            for s in ("complete", "skipped", "failed", "incomplete", "invalid", "legacy", "retention_partial")}, "issues": issues,
            "calls": call_reports, "call_counts": {s: sum(c["status"] == s for c in call_reports)
            for s in ("complete", "failed", "incomplete", "invalid", "retention_partial")}, "retention_checkpoints": checkpoints}


def validate_graph_audit(audit, retrieval=None):
    version = audit["schema_version"]
    _require(type(version) is int and version in (1, 2), "graph audit version mismatch")
    neighborhood = version == 2
    if neighborhood:
        _require(audit.get("audit_scope") == NEIGHBORHOOD_AUDIT_SCOPE,
                 "unknown graph audit scope")
    else:
        _require("audit_scope" not in audit, "legacy graph audit cannot declare neighborhood scope")
    if retrieval is not None:
        projected = _has_model_projection(retrieval)
        _require(audit["scope"] == retrieval["scope"] and audit["event_id"] == retrieval["event_id"]
                 and audit["request"]["query"] == retrieval["query"], "graph audit input mismatch")
    durable = audit["durable_payload_sha256"]
    if not audit["replay"] or "original_event" not in audit:
        _require(digest({k: v for k, v in audit.items() if k != "durable_payload_sha256"}) == durable,
                 "durable graph audit mismatch")
    else:
        _require(audit["original_event"]["payload_sha256"] == durable, "replayed graph audit link mismatch")
    observation, selection = audit["observation"], audit["selection"]
    if neighborhood:
        _require(selection.get("mark_frequencies_scope") == NEIGHBORHOOD_FREQUENCY_SCOPE,
                 "unknown mark frequency scope")
        _require(selection.get("edge_statistics_scope") == "direct_hit_incident_v1",
                 "unknown edge statistics scope")
        _require(observation.get("dynamic_shadow_enabled") is False
                 and observation["applied"] is False and not observation["changed_edges"]
                 and observation["seeded_edges"] == 0
                 and observation["seeded_after"] == observation["seeded_before"],
                 "neighborhood audit cannot update dynamic shadow")
        statuses = ("replay", "legacy_replay") if audit["replay"] else ("disabled",)
        _require(observation["status"] in statuses, "neighborhood observation status mismatch")
    else:
        _require("mark_frequencies_scope" not in selection
                 and "dynamic_shadow_enabled" not in observation,
                 "legacy graph audit cannot declare neighborhood fields")
    # Older immutable schema-1 records predate the static wiring repair and
    # used dynamic ranking. Preserve their historical arithmetic contract.
    if "ranking_mode" not in selection and "weight_basis" not in selection:
        ranking_mode = "dynamic"
    else:
        ranking_mode = selection.get("ranking_mode")
        _require(ranking_mode in ("static", "dynamic"), "invalid graph ranking mode")
        expected_basis = "static_npmi" if ranking_mode == "static" else "dynamic_or_static"
        _require(selection.get("weight_basis") == expected_basis, "graph weight basis mismatch")
    if neighborhood:
        _require(ranking_mode == "static", "neighborhood audit requires static ranking")
    if audit.get("original_event", {}).get("ranking_mode") is not None:
        _require(audit["original_event"]["ranking_mode"] == ranking_mode, "replayed graph ranking mode mismatch")
    hits = audit["match"]["direct_hits"]
    concept = selection.get('concept_policy')
    direct_limit = 64 if concept else 4
    if concept:
        from .relevance import plan, idf
        _require(concept['policy'] == 'concept_v1' and ranking_mode == 'static', 'unknown concept policy')
        from .relevance import normalize
        expected_plan = plan(audit['request']['query'], audit['request'].get('context_query', ''))
        expected_plan['terms'] = [t for t in expected_plan['terms'] if t not in {normalize(m) for m in audit['match']['excluded_self_marks']}]
        _require(concept['query_plan'] == expected_plan, 'concept query plan mismatch')
        _require(audit['match']['direct_limit'] == direct_limit, 'concept direct limit mismatch')
        _require(set(concept['mark_weights']) == set(hits), 'concept weights mismatch')
        _require(all(type(v) is int and 0 < v <= selection['active_record_count'] for counts in (concept['mark_counts'], concept['term_counts']) for v in counts.values()), 'concept frequency invalid')
        _require(concept['term_weights'] == {t: round(idf(selection['active_record_count'], count), 6)
                 for t, count in concept['term_counts'].items()}, 'body idf mismatch')
        _require(len(concept['lexical_ids']) <= 128 and len(set(concept['lexical_ids'])) == len(concept['lexical_ids']), 'body candidate limit invalid')
        from .relevance import contains
        weights = {m: round(idf(selection['active_record_count'], count) *
                   (1.0 if contains(m, concept['query_plan']['focus']) else 0.15), 6)
                   for m, count in concept['mark_counts'].items()}
        _require(concept['mark_weights'] == {m: weights[m] for m in hits}, 'concept idf mismatch')
        _require(audit['match']['literal_matches'] == sorted(weights, key=lambda m: (-weights[m], m)), 'concept match order mismatch')
    _require(hits == audit["match"]["literal_matches"][:direct_limit], "direct hit limit mismatch")
    _require(selection["limits"] == {"direct_marks": direct_limit, "neighbors_per_hit": 5, "expanded_marks": 2,
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
    _require(type(n) is int and n >= 0 and isinstance(frequencies, dict)
             and all(type(k) is str and type(v) is int and 0 < v <= n for k, v in frequencies.items()),
             "invalid static frequency counts")
    if neighborhood:
        endpoints = {edge[key] for edge in selection["edge_statistics"] for key in ("a", "b")}
        _require(set(frequencies) == endpoints, "neighborhood frequency endpoints mismatch")
    scoped_edges = "edge_statistics_scope" in selection
    if scoped_edges:
        _require(selection["edge_statistics_scope"] == "direct_hit_incident_v1",
                 "unknown edge statistics scope")
        _require(ranking_mode == "static", "scoped edge statistics require static ranking")
        count = selection["live_edge_count"]
        _require(type(count) is int and count >= 0 and len(selection["edge_statistics"]) <= count,
                 "scoped live edge count mismatch")
        pairs = [(edge["a"], edge["b"]) for edge in selection["edge_statistics"]]
        _require(pairs == sorted(pairs), "scoped edge statistics order mismatch")
        _require(all(a in hits or b in hits for a, b in pairs), "edge outside direct hit scope")
    live_edges = {}
    for edge in selection["edge_statistics"]:
        pair = (edge["a"], edge["b"])
        _require(pair[0] < pair[1] and pair not in live_edges, "live edge identity mismatch")
        live_edges[pair] = edge
        _require(edge["a"] in frequencies and edge["b"] in frequencies,
                 "static edge frequency missing")
        count = edge["co_count"]
        _require(edge["active_source_support"] is True and count == len(edge["source_record_ids"])
                 and 0 < count <= min(frequencies[edge["a"]], frequencies[edge["b"]]), "static source count mismatch")
        p_ab = count / n
        npmi = 1.0 if p_ab == 1.0 else math.log(p_ab / ((frequencies[edge["a"]] / n) * (frequencies[edge["b"]] / n))) / -math.log(p_ab)
        expected = round(npmi, 4) if npmi > 0 else 0.0
        _require(edge["static_score"] == expected, "static NPMI mismatch")
        if ranking_mode == "static":
            _require(edge["static_score"] > 0 and edge["effective_score"] == edge["static_score"],
                     "static ranking weight mismatch")
        else:
            _require(edge["effective_score"] == (edge["dynamic_score"] or edge["static_score"]), "effective weight mismatch")
    if observation["applied"]:
        for edge in transitions:
            pair = (edge["a"], edge["b"])
            eligible = edge["active_source_support"] and (
                edge["static_score"] > 0 if ranking_mode == "static" else edge["after_weight"] > 0)
            in_scope = not scoped_edges or pair[0] in hits or pair[1] in hits
            if eligible and in_scope:
                _require(pair in live_edges and live_edges[pair]["dynamic_score"] == edge["after_weight"]
                         and live_edges[pair]["source_record_ids"] == edge["source_record_ids"]
                         and live_edges[pair]["static_score"] == edge["static_score"], "committed/live weight mismatch")
            else:
                _require(pair not in live_edges, "unsupported or excluded edge entered ranking")
    if scoped_edges:
        # The complete one-hop decisions include neighbors rejected by the
        # top-five or context gates. Their source evidence remains bound to
        # the same scoped statistics as the accepted ranking evidence.
        for candidate in selection["expansion_candidates"]:
            _require(candidate["from_mark"] in hits and candidate["mark"] not in hits,
                     "expansion evidence outside direct hit scope")
            pair = tuple(sorted((candidate["from_mark"], candidate["mark"])))
            edge = live_edges.get(pair)
            _require(edge is not None, "expansion evidence has no live edge")
            _require(all(candidate[key] == edge[key] for key in (
                "source_record_ids", "co_count", "static_score", "dynamic_score",
                "effective_score", "context", "dynamic_last_event_id")),
                "expansion evidence disagrees with live edge")
    ranked = selection["ranked_candidates"]
    expected_order = sorted(ranked, key=lambda c: (-c['relevance_score'] if concept else -c["direct_match_count"], -c["effective_score"], -c["static_score"], -c["record_id"]))
    _require(ranked == expected_order and [c["rank"] for c in ranked] == list(range(1, len(ranked) + 1)), "ranking order mismatch")
    for candidate in ranked:
        if concept:
            from .relevance import score
            inputs = candidate['relevance_inputs']
            _require(candidate['relevance_score'] == score(inputs), 'concept score mismatch')
            _require(inputs['metadata_factor'] in (0.25, 0.55, 0.7, 1.0)
                     and inputs['source_prior'] in (0.0, 4.0, 12.0)
                     and inputs['answer_form_bonus'] in (0.0, 6.0, 10.0), 'concept coefficients mismatch')
            _require(inputs['mark_relevance'] == round(0.25 * sum(concept['mark_weights'][m] * (1.0 if m in inputs['literal_mark_terms'] else 0.15) for m in inputs['mark_terms']), 6)
                     and inputs['body_relevance'] == round(sum(concept['term_weights'][t] for t in inputs['body_terms']), 6), 'concept arithmetic mismatch')
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
    selected_candidates = _validate_selection_contract(audit, retrieval)
    if retrieval is None:
        return
    materials = retrieval["materials"]
    _require(selection["selected_record_ids"] == [m["record_id"] for m in materials]
             == [c["record_id"] for c in selected_candidates], "selected ranking mismatch")
    _require(len(selection["selected"]) == len(materials), "selected receipt mismatch")
    for material, selected, candidate in zip(materials, selection["selected"], selected_candidates):
        if concept:
            from .relevance import metadata_factor, contains
            inputs = candidate['relevance_inputs']
            _require(metadata_factor(material['stored_text']) == (inputs['metadata_kind'], inputs['metadata_factor']), 'metadata classification mismatch')
            _require(inputs['body_terms'] == sorted(t for t in concept['term_weights'] if contains(t, material['stored_text'])), 'body match mismatch')
            _require(inputs['literal_mark_terms'] == [m for m in inputs['mark_terms'] if contains(m, material['stored_text'])], 'literal annotation mismatch')
            source = retrieval['sources'][material['source_id']]
            if source['metadata_available']:
                from .relevance import relevance, source_hint
                hint = source_hint([c['text'] for c in source['chunks']], source['path'])
                expected, _ = relevance(material['stored_text'], candidate['direct_marks'],
                    concept['mark_weights'], concept['term_weights'], hint, concept['query_plan'])
                _require(inputs == expected, 'frozen concept relevance mismatch')
        payload = material["model_payload"]
        if projected:
            _validate_projection_keys(payload, material)
        if "ranking_mode" in selection:
            _require(payload.get("ranking_mode") == ranking_mode
                     and payload.get("weight_basis") == selection["weight_basis"], "model material weight basis mismatch")
        _require(selected["source_id"] == candidate["source_id"] == material["source_id"]
                 and candidate["text_sha256"] == text_digest(material["stored_text"])
                 and candidate["quote_sha256"] == text_digest(material["quote"])
                 and selected["preview_sha256"] == text_digest(payload["text"]), "selected content digest mismatch")
        if projected:
            _validate_model_projection(payload, material, candidate, selection)
        else:
            _validate_legacy_model_material(payload, material, candidate, selection)
