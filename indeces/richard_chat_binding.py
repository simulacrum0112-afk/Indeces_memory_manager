"""Metadata-only binding of existing native candidates to frozen graph evidence.

No retrieval, model, filesystem or network operations are performed. This is an
explicitly injected SQLite reader, not a permission or semantic-entailment check.
Call snapshot again inside the final source transaction after any await; a
catalog taken earlier is historical evidence, not authorization to reuse it.
Book: Bojie Li, AI Agents in Depth, v2.0 September 23, 2026: S7 ch.3
pp.111-113/PDF119-121, A4 ch.4 pp.127-128/PDF135-136, E1 ch.7
pp.232-233/PDF240-241. These engineering applications are not certification.
"""

import hashlib
import json
import re
import sqlite3

from .pdf_import import validate_conversion
from .richard_data import GraphData
from .run_records import pdf_conversion_metadata
from .selection_policy import SelectionCandidate, SelectionRequest, digest


SCHEMA = "richard_native_binding_v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def request_binding_digest(request: SelectionRequest) -> str:
    """Bind the scope/event and ordered native candidate identities.

    The adapter's planning receipt separately binds query/configuration/limits.
    This recipe is deliberately explicit, stable and shared with that adapter.
    """
    return digest([request.scope, request.event_id,
                   *[candidate.identity() for candidate in request.candidates]])


class _Unbound(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise _Unbound(reason)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _text_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _location(row, pages_key):
    index, start, end = row.get("chunk_index"), row.get("start_character"), row.get("end_character")
    pages = row.get(pages_key, ())
    _require(all(type(v) is int for v in (index, start, end))
             and index >= 0 and 0 <= start < end, "chunk_identity_invalid")
    _require(isinstance(pages, (tuple, list))
             and all(type(p) is int and p > 0 for p in pages), "chunk_identity_invalid")
    return (index, start, end, tuple(pages))


class NativeEvidenceBinder:
    """Read authoritative metadata for request candidates and produce pure receipts.

    Graph and native record IDs are distinct namespaces. A match requires the
    source UUID, material/conversion/PDF digests, all occurrences, and mark/CID
    membership. No path-name match, numeric-ID join, or name-only evidence join
    is used. Non-PDF matching is limited to explicitly synthetic GraphData.
    """

    def __init__(self, graph: GraphData, db: sqlite3.Connection):
        if type(graph) is not GraphData or not _hash(graph.identity_sha256):
            raise ValueError("an exact immutable GraphData is required")
        if not isinstance(db, sqlite3.Connection):
            raise ValueError("an explicitly injected SQLite connection is required")
        self.graph, self.db = graph, db
        self._records_by_source = {}
        self._cids_by_record = {rid: set() for rid in graph.records}
        for node in graph.nodes:
            for rid in graph.records_for(node.cid):
                if rid not in self._cids_by_record:
                    raise ValueError("graph contains an unknown record association")
                self._cids_by_record[rid].add(node.cid)
        for rid, record in graph.records.items():
            if type(rid) is not int or rid <= 0 or record.get("record_id") != rid:
                raise ValueError("graph record identity is invalid")
            self._records_by_source.setdefault(record.get("source_id"), []).append((rid, record))

    def _rows(self, sql, parameters):
        cursor = self.db.execute(sql, parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]

    def _candidate(self, request, candidate, tables, request_sha256):
        _require(candidate.scope == request.scope, "scope_mismatch")
        _require(isinstance(candidate.text, str) and candidate.text
                 and candidate.text == candidate.quote, "candidate_quote_mismatch")
        _require(_hash(candidate.fingerprint)
                 and candidate.fingerprint == digest([candidate.text, candidate.quote]), "candidate_fingerprint_mismatch")
        required = {"memory_records", "knowledge_versions", "knowledge_published", "knowledge_chunks"}
        _require(required <= tables, "native_metadata_unavailable")
        native = self._rows("SELECT id,scope,source_id,text,quote,marks_json,fingerprint,active "
                            "FROM main.memory_records WHERE id=? AND scope=?",
                            (candidate.record_id, request.scope))
        _require(len(native) == 1, "native_record_unavailable")
        native = native[0]
        _require(native["active"] == 1, "native_record_inactive")
        _require(native["scope"] == request.scope and native["source_id"] == candidate.source_id,
                 "native_identity_mismatch")
        _require(native["text"] == candidate.text and native["quote"] == candidate.quote
                 and native["fingerprint"] == candidate.fingerprint, "native_fingerprint_mismatch")
        try:
            stored_marks = json.loads(native["marks_json"])
        except (TypeError, ValueError, RecursionError):
            raise _Unbound("native_marks_invalid") from None
        _require(type(candidate.marks) is tuple and isinstance(stored_marks, list)
                 and all(isinstance(m, str) for m in stored_marks)
                 and tuple(stored_marks) == candidate.marks, "native_marks_mismatch")
        versions = self._rows("SELECT source_id,scope,path,digest,raw_text,status "
                              "FROM main.knowledge_versions WHERE source_id=? AND scope=?",
                              (candidate.source_id, request.scope))
        _require(len(versions) == 1, "native_version_unavailable")
        version = versions[0]
        _require(version["status"] == "ready", "native_version_not_ready")
        published = self._rows("SELECT source_id FROM main.knowledge_published WHERE scope=? AND path=?",
                               (request.scope, version["path"]))
        _require(len(published) == 1 and published[0]["source_id"] == candidate.source_id,
                 "native_version_unpublished")
        raw = version["raw_text"]
        _require(isinstance(raw, str) and _hash(version["digest"]), "native_version_invalid")
        converted_sha256 = _text_hash(raw)
        chunks = self._rows("SELECT chunk_index,start_character,text FROM main.knowledge_chunks "
                            "WHERE source_id=? AND text=? ORDER BY chunk_index",
                            (candidate.source_id, candidate.text))
        _require(bool(chunks), "native_chunk_unavailable")
        graph_records = self._records_by_source.get(candidate.source_id, ())
        _require(bool(graph_records), "source_not_in_graph")
        pdf = []
        if "knowledge_pdf_versions" in tables:
            pdf = self._rows("SELECT CASE WHEN length(CAST(metadata_json AS BLOB))<=524288 "
                             "THEN metadata_json ELSE NULL END AS metadata_json "
                             "FROM main.knowledge_pdf_versions WHERE source_id=?", (candidate.source_id,))
        _require(len(pdf) <= 1, "native_pdf_metadata_invalid")
        metadata = None
        if pdf:
            try:
                metadata = pdf_conversion_metadata(pdf[0]["metadata_json"])
                validate_conversion(raw, metadata, version["digest"])
            except (TypeError, ValueError, KeyError, UnicodeError, RecursionError):
                raise _Unbound("native_pdf_metadata_invalid") from None
        native_locations = []
        for chunk in chunks:
            start, index = chunk["start_character"], chunk["chunk_index"]
            _require(type(start) is int and type(index) is int and start >= 0 and index >= 0,
                     "chunk_identity_invalid")
            end = start + len(candidate.text)
            _require(raw[start:end] == candidate.text, "native_chunk_text_mismatch")
            pages = (() if metadata is None else tuple(p["page_number"] for p in metadata["pages"]
                     if start < p["end_character"] and end > p["start_character"]))
            native_locations.append((index, start, end, pages))
        _require(len(set(native_locations)) == len(native_locations), "native_chunk_ambiguous")
        native_locations = tuple(sorted(native_locations))
        matches = []
        for graph_rid, graph_record in graph_records:
            frozen = graph_record.get("version", {})
            if (frozen.get("source_id") != candidate.source_id
                    or frozen.get("material_sha256") != version["digest"]
                    or frozen.get("converted_markdown_sha256") != converted_sha256):
                continue
            pdf_fields = "original_pdf_sha256" in frozen or "pdf_page_count" in frozen
            if pdf_fields:
                if (metadata is None or frozen.get("original_pdf_sha256") != metadata["original_pdf_sha256"]
                        or frozen.get("pdf_page_count") != metadata["page_count"]
                        or metadata["markdown_sha256"] != converted_sha256):
                    continue
            elif (self.graph.verification_status != "synthetic_unverified" or metadata is not None
                  or version["digest"] != converted_sha256):
                continue
            locations = graph_record.get("chunk_locations", ())
            try:
                expected = tuple(sorted(_location(row, "pdf_page_numbers") for row in locations))
            except _Unbound:
                continue
            if (not expected or len(set(expected)) != len(expected)
                    or any(row.get("source_id") != candidate.source_id or row.get("record_id") != graph_rid
                           for row in locations) or expected != native_locations):
                continue
            matches.append((graph_rid, frozen))
        _require(bool(matches), "graph_provenance_mismatch")
        _require(len(matches) == 1, "graph_record_ambiguous")
        graph_rid, frozen = matches[0]
        cids = set()
        for mark in candidate.marks:
            _require(mark in self.graph.name_to_cid, "mark_not_in_graph")
            cid = self.graph.name_to_cid[mark]
            _require(cid in self._cids_by_record[graph_rid], "mark_membership_mismatch")
            cids.add(cid)
        _require(bool(cids), "no_verified_concept_marks")
        proof = {"schema": SCHEMA, "graph_identity_sha256": self.graph.identity_sha256,
                 "request_sha256": request_sha256, "native_candidate": candidate.identity(),
                 "graph_record_id": graph_rid, "native_source_id": candidate.source_id,
                 "material_sha256": version["digest"], "converted_markdown_sha256": converted_sha256,
                 "published_source_id": published[0]["source_id"], "source_status": version["status"],
                 "locations": [[i, a, b, list(p)] for i, a, b, p in native_locations],
                 "pdf_metadata_sha256": digest(metadata) if metadata is not None else None,
                 "cids": sorted(cids)}
        return {"native_record_id": candidate.record_id, "graph_record_id": graph_rid,
                "cids": sorted(cids), "proof_sha256": digest(proof)}

    def snapshot(self, request: SelectionRequest) -> dict:
        if type(request) is not SelectionRequest or type(request.candidates) is not tuple:
            raise ValueError("an exact immutable SelectionRequest is required")
        ids = []
        for candidate in request.candidates:
            if type(candidate) is not SelectionCandidate or type(candidate.record_id) is not int or candidate.record_id <= 0:
                raise ValueError("candidate record IDs must be positive integers")
            ids.append(candidate.record_id)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate record IDs")
        request_sha256 = request_binding_digest(request)
        result = {"schema": SCHEMA, "graph_identity_sha256": self.graph.identity_sha256,
                  "request_sha256": request_sha256, "bindings": [], "unbound": []}
        owned_transaction = not self.db.in_transaction
        if owned_transaction:
            self.db.execute("BEGIN")
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
            for candidate in request.candidates:
                try:
                    binding = self._candidate(request, candidate, tables, request_sha256)
                except _Unbound as error:
                    result["unbound"].append({"record_id": candidate.record_id, "reason": str(error)})
                # SQLite interruption/locking/error must retain the native
                # runtime's failure/deadline classification, not become a
                # seemingly successful unbound catalog or silent fallback.
                except (UnicodeError, TypeError, ValueError, KeyError, RecursionError):
                    result["unbound"].append({"record_id": candidate.record_id, "reason": "native_metadata_invalid"})
                else:
                    result["bindings"].append(binding)
            # One frozen record cannot silently overwrite another native identity.
            counts = {}
            for binding in result["bindings"]:
                counts[binding["graph_record_id"]] = counts.get(binding["graph_record_id"], 0) + 1
            ambiguous = {rid for rid, count in counts.items() if count > 1}
            if ambiguous:
                for binding in result["bindings"]:
                    if binding["graph_record_id"] in ambiguous:
                        result["unbound"].append({"record_id": binding["native_record_id"], "reason": "native_record_ambiguous"})
                result["bindings"] = [b for b in result["bindings"] if b["graph_record_id"] not in ambiguous]
            order = {rid: index for index, rid in enumerate(ids)}
            result["unbound"].sort(key=lambda row: order[row["record_id"]])
            if owned_transaction:
                self.db.commit()
        except BaseException:
            if owned_transaction:
                self.db.rollback()
            raise
        return result
