"""Pinned v3.0.2 data loader for the isolated Richard v1.1 integration.

Only reviewed repository-owned parsing/immutable-graph helpers are imported.
No release scripts are executed, imported, extracted or added to sys.path.
The loader establishes snapshot provenance and numeric derivation, not chemical
meaning, relevance, live-corpus coverage, or solver protocol conformance.

Book rationale: Bojie Li, AI Agents in Depth: Design Principles and Engineering
Practice, v2.0 September 23, 2026. S7 ch.3 pp.111-113/PDF119-121 (provenance),
A4/A5 ch.4 pp.127-129/PDF135-137 (strict contract and fidelity), E1 ch.7
pp.232-233/PDF240-241 (independent reconstruction). Standards not assessed.
"""

import json
from pathlib import Path
from types import MappingProxyType

from .richard_data import (GraphData, SOURCE_MEMBERS, SOURCE_SHA256, _integer,
                           _json, _read_archive, _rebuild_edges, _rows, _string,
                           from_rows)


RELEASE_SHA256 = "7b9b3184c11612bde8fc3a25ca54a3cf3d1126ad37265657729d7fe7c22cd820"
RELEASE_MEMBERS = MappingProxyType({
    "release_v3.0.2/graph_v3/nodes_v3.jsonl": "2a3e3f7ff2ce1f954fce53aa2b83a4f214f27e1e7d7cae32e5fdc94f4a7599c7",
    "release_v3.0.2/graph_v3/edges_v3.jsonl": "c408b67c45c1b73d6bf04af7212169f3155b1538a20990751e27dc4375d1bf18",
    "release_v3.0.2/graph_v3/member_to_cid.json": "290befb5d5dfc500ab0ddf45a78bc828191651f7b877fef5e4a50ea8b92f06d1",
    "release_v3.0.2/graph_v3/MANIFEST_v3_dev.json": "58312b03b8880efc50a64463633103e149aa92c0798960a82df2ea5d5f174e17",
})
ARTIFACT_VERSION = "v3.0.2"
LOADER_SCHEMA = "indeces_richard_v11_snapshot_v1"
VERIFICATION_STATUS = "verified_pinned_release_and_rebuilt"


def load_release(release_zip: str | Path, source_zip: str | Path) -> GraphData:
    """Return an immutable, version-bound graph after an independent rebuild.

    Both archives and required data members must match the pinned SHA-256s.
    Names, CIDs, member IDs, source IDs, locations and version fingerprints remain
    unchanged. The graph's exact-name mapping performs no formula/alias guessing.
    Archive size, member size, rows, JSON keys and finite values use the reviewed
    reader bounds. Unexpected bytes fail before data parsing; no extraction,
    writes, network/model calls or supplied-code execution occurs.
    """
    release = _read_archive(release_zip, RELEASE_SHA256, RELEASE_MEMBERS)
    source = _read_archive(source_zip, SOURCE_SHA256, SOURCE_MEMBERS)
    prefix = "release_v3.0.2/graph_v3/"
    nodes = _rows(release[prefix + "nodes_v3.jsonl"])
    edges = _rows(release[prefix + "edges_v3.jsonl"])
    mapping = _json(release[prefix + "member_to_cid.json"])
    release_manifest = _json(release[prefix + "MANIFEST_v3_dev.json"])
    source_manifest = _json(source["MANIFEST.json"])
    if (len(nodes), len(mapping), len(edges)) != (8971, 9623, 61749):
        raise ValueError("pinned v3.0.2 release count mismatch")
    expected_counts = {"concepts": 8971, "original_nodes": 9623,
                       "edges_positive_npmi": 61749, "records": 4756}
    if any(release_manifest.get(k) != v for k, v in expected_counts.items()):
        raise ValueError("pinned v3.0.2 manifest count mismatch")
    if release_manifest.get("source_graph_zip_sha256") != SOURCE_SHA256:
        raise ValueError("release provenance mismatch")
    if release_manifest.get("files") != {name.removeprefix(prefix): digest
                                          for name, digest in RELEASE_MEMBERS.items()
                                          if not name.endswith("MANIFEST_v3_dev.json")}:
        raise ValueError("release member manifest mismatch")
    original_nodes = _rows(source["nodes.jsonl"])
    record_rows = _rows(source["records.jsonl"])
    marks = _rows(source["word_records.jsonl"])
    locations = _rows(source["chunk_locations.jsonl"])
    versions = _rows(source["versions.jsonl"])
    if (len(original_nodes), len(record_rows), len(marks), len(locations), len(versions)) != (9623, 4756, 30111, 4756, 37):
        raise ValueError("pinned source count mismatch")
    original = {}
    for row in original_nodes:
        if set(row) != {"node_id", "name", "record_frequency"}:
            raise ValueError("source node schema mismatch")
        mid = _integer(row["node_id"], "node_id")
        if mid in original:
            raise ValueError("duplicate source node ID")
        original[mid] = row
    member_names = {m: row["name"] for m, row in original.items()}
    structural = from_rows(nodes, edges, member_names=member_names)
    for member, cid in mapping.items():
        if not member.isascii() or not member.isdigit() or str(int(member)) != member:
            raise ValueError("member mapping keys must be canonical integer strings")
        _integer(cid, "member mapping CID")
    if mapping != {str(m): cid for m, cid in structural.member_to_cid.items()}:
        raise ValueError("released mapping differs from member partition")
    records = {}
    for row in record_rows:
        if set(row) != {"record_id", "source_id"}:
            raise ValueError("source record schema mismatch")
        rid = _integer(row["record_id"], "record_id")
        _string(row["source_id"], "source_id")
        if rid in records:
            raise ValueError("duplicate source record ID")
        records[rid] = dict(row, chunk_locations=[])
    version_map = {}
    for row in versions:
        sid = _string(row.get("source_id"), "source_id")
        if sid in version_map:
            raise ValueError("duplicate version source_id")
        _integer(row.get("pdf_page_count"), "pdf_page_count")
        for field in ("converted_markdown_sha256", "material_sha256", "original_pdf_sha256"):
            value = row.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("invalid version fingerprint")
        version_map[sid] = row
    if {r["source_id"] for r in records.values()} != set(version_map):
        raise ValueError("record/version source identity mismatch")
    for row in locations:
        rid = _integer(row.get("record_id"), "location record_id")
        if rid not in records or row.get("source_id") != records[rid]["source_id"]:
            raise ValueError("chunk location/source identity mismatch")
        _integer(row.get("chunk_index"), "chunk_index", 0)
        start = _integer(row.get("start_character"), "start_character", 0)
        end = _integer(row.get("end_character"), "end_character", 0)
        pages = row.get("pdf_page_numbers")
        if end < start or not isinstance(pages, list) or not pages:
            raise ValueError("invalid chunk interval/pages")
        limit = version_map[row["source_id"]]["pdf_page_count"]
        if any(type(p) is not int or not 1 <= p <= limit for p in pages):
            raise ValueError("PDF page lies outside source version")
        records[rid]["chunk_locations"].append(row)
    for row in records.values():
        if not row["chunk_locations"]:
            raise ValueError("record lacks chunk identity")
        row["version"] = version_map[row["source_id"]]
    source_sets = {m: set() for m in original}
    seen_marks = set()
    for row in marks:
        if set(row) != {"node_id", "record_id"}:
            raise ValueError("word_records schema mismatch")
        mid, rid = row["node_id"], row["record_id"]
        _integer(mid, "mark node_id")
        _integer(rid, "mark record_id")
        if mid not in original or rid not in records or (mid, rid) in seen_marks:
            raise ValueError("unknown or duplicate record/mark association")
        seen_marks.add((mid, rid))
        source_sets[mid].add(rid)
    for mid, row in original.items():
        if len(source_sets[mid]) != _integer(row["record_frequency"], "source frequency", 0):
            raise ValueError("original node frequency differs from source associations")
    concept_sets = {n.cid: set().union(*(source_sets[m] for m in n.members)) for n in structural.nodes}
    rebuilt = _rebuild_edges(concept_sets, set(records))
    rebuilt_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rebuilt).encode("utf-8")
    if rebuilt_bytes != release[prefix + "edges_v3.jsonl"]:
        raise ValueError("independent source rebuild differs from released edge bytes")
    manifest = {"artifact_version": ARTIFACT_VERSION, "loader_schema": LOADER_SCHEMA,
                "verification_status": VERIFICATION_STATUS,
                "release_zip_sha256": RELEASE_SHA256, "source_zip_sha256": SOURCE_SHA256,
                "release_members": dict(RELEASE_MEMBERS), "source_members": dict(SOURCE_MEMBERS),
                "release_manifest": release_manifest, "source_manifest": source_manifest,
                "source_text_available": False, "live_corpus_verified": False,
                "chemical_semantics_verified": False, "solver_protocol_verified": False}
    graph = from_rows(nodes, edges, member_names=member_names, record_rows=records.values(),
                      concept_records=concept_sets, source_manifest=manifest)
    return GraphData(graph.nodes, graph.edges, graph.cid_to_index, graph.index_to_cid,
                     graph.name_to_cid, graph.member_to_cid, graph.concept_records,
                     graph.records, graph.adjacency, graph.source_manifest,
                     graph.identity_sha256, VERIFICATION_STATUS)
