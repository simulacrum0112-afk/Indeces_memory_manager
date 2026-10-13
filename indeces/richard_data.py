"""Read the pinned Richard step-1 graph as data, with no bundled-code execution.

This is an opt-in snapshot loader, not a live corpus or a semantic authority.
Types are preserved as supplied metadata. Record text is absent from the source.
Book: Bojie Li, AI Agents in Depth: Design Principles and Engineering Practice,
v2.0, September 23, 2026. Rationale: S7 (provenance, ch.3 pp.111-113/PDF119-121), A4 (contracts,
ch.4 pp.127-128/PDF135-136), A5 (fidelity, pp.128-129/PDF136-137), E1
(independent non-regression, ch.7 pp.232-233/PDF240-241). These are engineering
applications, not certification or independent standards assessments.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping
import zipfile


RELEASE_SHA256 = "d86dce8acd8b417fc1197cc3cf30b7a7c28551ea9f9c12264b6757551eb7d370"
SOURCE_SHA256 = "3ee3ce4bc16357571d6d2b35fffa6c08dbb096508f1a0b3f5005d097cbfdab81"
RELEASE_MEMBERS = MappingProxyType({
    "release_v3.0.1/graph_v3/nodes_v3.jsonl": "93a9de1754b3ea5917cbc9bdd2db8eb6668f92d00b66b30ad336b1415112fd6b",
    "release_v3.0.1/graph_v3/edges_v3.jsonl": "86d96919811934082f1a619ddf48a20b5ffabeffe0ca9b57319bcab539c6d95a",
    "release_v3.0.1/graph_v3/member_to_cid.json": "f6fb59d576aaacc8386a14e78b68afbee07d120cf1c1591b7101ea457f0c0bde",
    "release_v3.0.1/graph_v3/MANIFEST_v3_dev.json": "7ddec907e9ce5053d93569a8f1865ef2fabb91822a486843c56af354cc5e69d1",
})
SOURCE_MEMBERS = MappingProxyType({
    "nodes.jsonl": "5b367011e35c1b54762dfb8e7fb51b74596b19780504999967cb9ce29b339460",
    "word_records.jsonl": "da7f74a07e68445acc715762f90be038ad909e16bccda875dbe15f9f12c17cf6",
    "records.jsonl": "58d970cee1950b8d82d1eaea26e66e0f08c7beb4f44816716e33b140d7dad3c2",
    "chunk_locations.jsonl": "c96f31c98ad0caed98aa2b760460a6617402b767d523376e20319328fa1fd238",
    "versions.jsonl": "09162a777f8d13c0108ad9dfcace18e813db85b58f119153d424c76a7e1c255b",
    "MANIFEST.json": "950e3b8a056680756c0e4af24d4452f64dd992903a945ab46635ab248b5d9ba1",
})
_MAX_ARCHIVE = 2 * 1024 * 1024
_MAX_MEMBER = 8 * 1024 * 1024
_MAX_TOTAL = 16 * 1024 * 1024
_MAX_ROWS = 100_000


@dataclass(frozen=True, slots=True)
class Node:
    cid: int
    name: str
    members: tuple[int, ...]
    frequency: int
    coarse: str
    fine: str


@dataclass(frozen=True, slots=True)
class Edge:
    a: int
    b: int
    npmi: float
    co_count: int


@dataclass(frozen=True, slots=True)
class GraphData:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    cid_to_index: Mapping[int, int]
    index_to_cid: tuple[int, ...]
    name_to_cid: Mapping[str, int]
    member_to_cid: Mapping[int, int]
    concept_records: tuple[frozenset[int], ...]
    records: Mapping[int, Mapping[str, Any]]
    adjacency: tuple[tuple[tuple[int, float], ...], ...]
    source_manifest: Mapping[str, Any]
    identity_sha256: str
    verification_status: str

    def node(self, cid: int) -> Node:
        return self.nodes[self.cid_to_index[cid]]

    def records_for(self, cid: int) -> frozenset[int]:
        return self.concept_records[self.cid_to_index[cid]]


def _integer(value: Any, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{label} must be a nonempty bounded string")
    return value


def _freeze(value: Any, depth: int = 0) -> Any:
    if depth > 40:
        raise ValueError("metadata nesting exceeds limit")
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("metadata object keys must be strings")
        return MappingProxyType({k: _freeze(v, depth + 1) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v, depth + 1) for v in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("metadata must contain finite JSON data only")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _json(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_bad_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON row must be an object")
    _freeze(value)
    return value


def _rows(data: bytes) -> list[dict[str, Any]]:
    lines = data.splitlines()
    if len(lines) > _MAX_ROWS or any(not line or len(line) > 512 * 1024 for line in lines):
        raise ValueError("JSONL row count, blank row, or row size violates bounds")
    return [_json(line) for line in lines]


def _read_archive(path: str | Path, digest: str,
                  required: Mapping[str, str]) -> dict[str, bytes]:
    """Read only pinned data members; never extract or import the archive."""
    with Path(path).open("rb") as stream:
        if stream.seek(0, 2) > _MAX_ARCHIVE:
            raise ValueError("archive exceeds size limit")
        stream.seek(0)
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("archive SHA-256 mismatch")
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if len(entries) > 64 or len(set(names)) != len(names):
                    raise ValueError("duplicate ZIP members or member count exceeds limit")
                if sum(e.file_size for e in entries) > _MAX_TOTAL:
                    raise ValueError("archive uncompressed size exceeds limit")
                if any(e.file_size > _MAX_MEMBER or e.flag_bits & 1 for e in entries):
                    raise ValueError("oversized or encrypted ZIP member")
                result = {}
                for name, expected in required.items():
                    if name not in names:
                        raise ValueError(f"missing required data member: {name}")
                    with archive.open(name) as member:
                        data = member.read(_MAX_MEMBER + 1)
                    if len(data) > _MAX_MEMBER:
                        raise ValueError("ZIP member exceeds read limit")
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise ValueError(f"member SHA-256 mismatch: {name}")
                    result[name] = data
                return result
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid ZIP archive") from exc


def from_rows(node_rows: Iterable[Mapping[str, Any]],
              edge_rows: Iterable[Mapping[str, Any]], *,
              member_names: Mapping[int, str] | None = None,
              record_rows: Iterable[Mapping[str, Any]] = (),
              concept_records: Mapping[int, Iterable[int]] | None = None,
              source_manifest: Mapping[str, Any] | None = None) -> GraphData:
    """Build a validated immutable synthetic snapshot without release verification.

    Rows use released fields: cid/name/members/record_frequency/coarse/fine and
    a/b/co_count/npmi. IDs remain sparse; CID equals the minimum member ID. Exact
    mark names are mapped without case folding or chemical interpretation. When
    record sets are omitted only structure/numeric constraints are checked.
    """
    nodes, member_map = [], {}
    last_cid = 0
    for row in node_rows:
        if len(nodes) >= _MAX_ROWS or set(row) != {"cid", "name", "members", "record_frequency", "coarse", "fine"}:
            raise ValueError("invalid concept row schema or row count")
        cid = _integer(row["cid"], "cid")
        members = tuple(row["members"])
        if not members or any(type(m) is not int or m < 1 for m in members):
            raise ValueError("members must be nonempty positive integer IDs")
        if members != tuple(sorted(set(members))) or cid != members[0] or cid <= last_cid:
            raise ValueError("concept IDs/members must be sorted, unique, with cid=min(members)")
        if any(m in member_map for m in members):
            raise ValueError("member belongs to more than one concept")
        member_map.update({m: cid for m in members})
        nodes.append(Node(cid, _string(row["name"], "name"), members,
                          _integer(row["record_frequency"], "record_frequency", 0),
                          _string(row["coarse"], "coarse"), _string(row["fine"], "fine")))
        last_cid = cid
    if not nodes:
        raise ValueError("graph must contain concepts")
    indexes = {node.cid: i for i, node in enumerate(nodes)}
    if len({node.name for node in nodes}) != len(nodes):
        raise ValueError("concept names must be unique")
    names = ({m: node.name for node in nodes for m in node.members}
             if member_names is None else dict(member_names))
    if set(names) != set(member_map):
        raise ValueError("member_names must cover the exact member partition")
    name_map = {}
    for member, name in sorted(names.items()):
        _integer(member, "member ID")
        _string(name, "member name")
        if name in name_map and name_map[name] != member_map[member]:
            raise ValueError("same exact mark name maps to multiple concepts")
        name_map[name] = member_map[member]
    records = {}
    for row in record_rows:
        rid = _integer(row.get("record_id"), "record_id")
        if rid in records or len(records) >= _MAX_ROWS:
            raise ValueError("duplicate record ID or record count exceeds limit")
        records[rid] = _freeze(dict(row))
    sets = tuple(frozenset() for _ in nodes)
    if concept_records is not None:
        for cid in concept_records:
            _integer(cid, "concept_records CID")
        if set(concept_records) != set(indexes):
            raise ValueError("concept_records must cover every CID")
        raw_sets = tuple(tuple(concept_records[node.cid]) for node in nodes)
        if any(type(r) is not int or r < 1 for values in raw_sets for r in values):
            raise ValueError("concept record IDs must be positive integers")
        sets = tuple(frozenset(values) for values in raw_sets)
        for node, record_set in zip(nodes, sets):
            if not record_set <= records.keys() or len(record_set) != node.frequency:
                raise ValueError("record set/frequency/reference mismatch")
    edges, adjacency = [], [[] for _ in nodes]
    last_pair = (0, 0)
    for row in edge_rows:
        if len(edges) >= _MAX_ROWS or set(row) != {"a", "b", "co_count", "npmi"}:
            raise ValueError("invalid edge schema or row count")
        a, b = _integer(row["a"], "edge a"), _integer(row["b"], "edge b")
        pair = (a, b)
        if a >= b or pair <= last_pair or a not in indexes or b not in indexes:
            raise ValueError("edges need sorted unique known unordered endpoints")
        weight = row["npmi"]
        if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 < weight <= 1:
            raise ValueError("edge NPMI must be finite and in (0,1]")
        count = _integer(row["co_count"], "co_count")
        if count > min(nodes[indexes[a]].frequency, nodes[indexes[b]].frequency):
            raise ValueError("co_count exceeds endpoint frequency")
        if concept_records is not None and count != len(sets[indexes[a]] & sets[indexes[b]]):
            raise ValueError("co_count differs from supplied record intersection")
        edge = Edge(a, b, float(weight), count)
        edges.append(edge)
        adjacency[indexes[a]].append((b, edge.npmi))
        adjacency[indexes[b]].append((a, edge.npmi))
        last_pair = pair
    manifest = _freeze(dict(source_manifest or {}))
    identity = {"schema": "indeces_richard_snapshot_v1", "nodes": [
        [n.cid, n.name, list(n.members), n.frequency, n.coarse, n.fine] for n in nodes],
        "edges": [[e.a, e.b, e.co_count, e.npmi] for e in edges],
        "member_names": sorted(names.items()), "concept_records_supplied": concept_records is not None,
        "concept_records": [sorted(s) for s in sets],
        "records": [_plain(records[r]) for r in sorted(records)], "source_manifest": _plain(manifest)}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return GraphData(tuple(nodes), tuple(edges), MappingProxyType(indexes), tuple(indexes),
                     MappingProxyType(name_map), MappingProxyType(member_map), sets,
                     MappingProxyType(records), tuple(tuple(sorted(a)) for a in adjacency),
                     manifest, fingerprint, "synthetic_unverified")


def _rebuild_edges(records_by_cid: Mapping[int, set[int]], record_ids: set[int]) -> list[dict[str, Any]]:
    """Independent record-set rebuild; no reference builder or original edges used."""
    by_record = defaultdict(set)
    for cid, record_set in records_by_cid.items():
        for rid in record_set:
            by_record[rid].add(cid)
    if set(by_record) != record_ids:
        raise ValueError("every source record must contain at least one mark")
    counts = Counter(pair for cids in by_record.values() for pair in combinations(sorted(cids), 2))
    total = len(record_ids)
    result = []
    for (a, b), count in sorted(counts.items()):
        p_ab = count / total
        weight = (1.0 if p_ab == 1 else math.log(p_ab / (
            (len(records_by_cid[a]) / total) * (len(records_by_cid[b]) / total))) / -math.log(p_ab))
        if weight > 0:
            result.append({"a": a, "b": b, "co_count": count, "npmi": round(weight, 4)})
    return result


def load_release(release_zip: str | Path, source_zip: str | Path) -> GraphData:
    """Verify pinned ZIP/member bytes, provenance and all released NPMI edges.

    No extraction, writes, network access, model calls or bundled-code imports.
    This proves derivation for this pinned source, not live-corpus completeness,
    classifications, relevance, or the unfrozen solver_v2_pre protocol.
    """
    release = _read_archive(release_zip, RELEASE_SHA256, RELEASE_MEMBERS)
    source = _read_archive(source_zip, SOURCE_SHA256, SOURCE_MEMBERS)
    prefix = "release_v3.0.1/graph_v3/"
    nodes = _rows(release[prefix + "nodes_v3.jsonl"])
    edges = _rows(release[prefix + "edges_v3.jsonl"])
    mapping = _json(release[prefix + "member_to_cid.json"])
    release_manifest = _json(release[prefix + "MANIFEST_v3_dev.json"])
    source_manifest = _json(source["MANIFEST.json"])
    if (len(nodes), len(mapping), len(edges)) != (8979, 9623, 61798):
        raise ValueError("pinned release count mismatch")
    if release_manifest["source_graph_zip_sha256"] != SOURCE_SHA256:
        raise ValueError("release provenance mismatch")
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
    structural = from_rows(nodes, edges, member_names={m: row["name"] for m, row in original.items()})
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
    manifest = {"artifact_version": "v3.0.1", "verification_status": "verified_pinned_release_and_rebuilt",
                "release_zip_sha256": RELEASE_SHA256, "source_zip_sha256": SOURCE_SHA256,
                "release_members": dict(RELEASE_MEMBERS), "source_members": dict(SOURCE_MEMBERS),
                "release_manifest": release_manifest, "source_manifest": source_manifest,
                "source_text_available": False, "live_corpus_verified": False}
    graph = from_rows(nodes, edges, member_names={m: row["name"] for m, row in original.items()},
                      record_rows=records.values(), concept_records=concept_sets, source_manifest=manifest)
    return GraphData(graph.nodes, graph.edges, graph.cid_to_index, graph.index_to_cid,
                     graph.name_to_cid, graph.member_to_cid, graph.concept_records,
                     graph.records, graph.adjacency, graph.source_manifest,
                     graph.identity_sha256, "verified_pinned_release_and_rebuilt")
