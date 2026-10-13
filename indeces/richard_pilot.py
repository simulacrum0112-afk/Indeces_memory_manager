"""Explicit offline solver_v1_1 entry; never a chat/record selection policy."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType


SCHEMA = "indeces_richard_pilot_result_v1"
METHODS = frozenset(("alpha_meet", "alpha_nomeet", "beta_meet", "beta_nomeet", "frequency"))
MAX_REQUEST_BYTES = 65536
TOP_K = 10
_KEYS = frozenset(("namespace", "seeds_A", "seeds_B", "method", "level", "target"))


class PilotError(ValueError):
    """Public error codes contain no exception text or private paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _error(code, **fields):
    return {"schema": SCHEMA, "status": "error", "error": {"code": code, **fields}}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotError("invalid_json")
        result[key] = value
    return result


def parse_request(text):
    """Parse bounded strict JSON; namespace and all schema fields stay explicit."""
    if not isinstance(text, str):
        raise PilotError("invalid_json")
    try:
        if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise PilotError("request_too_large")
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(PilotError("invalid_json")))
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, PilotError):
            raise
        raise PilotError("invalid_json") from None


@dataclass(frozen=True, slots=True)
class RichardPilot:
    """Preloaded immutable graph/ranker; calls perform no file or provider I/O."""

    graph: object = field(repr=False)
    _ranker: object = field(repr=False)
    _solver_metadata: object = field(repr=False)

    def __post_init__(self):
        # A typed CID must never become a different concept on the author's
        # second, identity-only lookup. The reviewed loader proves this too.
        if any(self.graph.member_to_cid.get(cid) != cid for cid in self.graph.cid_to_index):
            raise PilotError("invalid_graph_namespace")
        object.__setattr__(self, "_solver_metadata", MappingProxyType(dict(self._solver_metadata)))

    def _normalize(self, request):
        if type(request) is not dict or set(request) != _KEYS:
            raise PilotError("invalid_request")
        if type(request["namespace"]) is not str or request["namespace"] not in ("member", "cid"):
            raise PilotError("invalid_namespace")
        if type(request["method"]) is not str or request["method"] not in METHODS:
            raise PilotError("invalid_method")
        if type(request["level"]) is not str or request["level"] not in ("coarse", "fine"):
            raise PilotError("invalid_target")
        if type(request["target"]) is not str or not request["target"] or len(request["target"]) > 64:
            raise PilotError("invalid_target")
        labels = {getattr(node, request["level"]) for node in self.graph.nodes}
        if request["target"] not in labels:
            raise PilotError("unknown_target")
        domain = (self.graph.member_to_cid if request["namespace"] == "member"
                  else self.graph.cid_to_index)
        normalized = {}
        for group in ("seeds_A", "seeds_B"):
            values = request[group]
            if type(values) is not list or len(values) > len(domain):
                raise PilotError("invalid_seed_groups")
            cids = set()
            for value in values:
                if type(value) is not int or value <= 0:
                    raise PilotError("invalid_seed_id")
                if value not in domain:
                    raise PilotError("unknown_seed_id")
                cids.add(self.graph.member_to_cid[value] if request["namespace"] == "member" else value)
            normalized[group] = sorted(cids)
        if request["method"].endswith("_meet"):
            if not normalized["seeds_A"] or not normalized["seeds_B"]:
                raise PilotError("invalid_seed_groups")
        elif request["method"] != "frequency" and not (normalized["seeds_A"] or normalized["seeds_B"]):
            raise PilotError("invalid_seed_groups")
        normalized.update(namespace="cid", method=request["method"], level=request["level"], target=request["target"])
        try:
            encoded = _json(request)
            if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise PilotError("request_too_large")
            original = json.loads(encoded)
        except PilotError:
            raise
        except (ValueError, UnicodeError, RecursionError):
            raise PilotError("invalid_request") from None
        return original, normalized

    def __call__(self, request):
        try:
            original, normalized = self._normalize(request)
        except PilotError as error:
            return _error(error.code)
        excluded = set(normalized["seeds_A"]) | set(normalized["seeds_B"])
        pool = {node.cid for node in self.graph.nodes
                if getattr(node, normalized["level"]) == normalized["target"] and node.cid not in excluded}
        manifest = self.graph.source_manifest
        provenance = {"graph_identity_sha256": self.graph.identity_sha256,
            "release_zip_sha256": manifest.get("release_zip_sha256"),
            "source_zip_sha256": manifest.get("source_zip_sha256"),
            "graph_verification_status": self.graph.verification_status,
            "solver": dict(self._solver_metadata), "original_request": original,
            "normalized_request": normalized, "input_sha256": _digest(original),
            "normalized_input_sha256": _digest(normalized)}
        def ranking_error(code):
            result = _error(code)
            result.update(pool_size=len(pool), provenance=provenance)
            return result
        if len(pool) < TOP_K:
            result = _error("insufficient_candidates", required=TOP_K, available=len(pool))
            result.update(pool_size=len(pool), provenance=provenance)
            return result
        try:
            rows = self._ranker.rank(normalized["seeds_A"], normalized["seeds_B"],
                target=normalized["target"], level=normalized["level"], variant=normalized["method"])
        except Exception:
            return ranking_error("ranker_failed")
        # Bound evaluation even if a future incompatible ranker returns an
        # iterator. Its public contract is a finite tuple of ranked concepts.
        if type(rows) is not tuple or len(rows) != TOP_K:
            return ranking_error("invalid_ranker_result")
        seen, results, previous = set(), [], None
        for row in rows:
            cid, score = getattr(row, "cid", None), getattr(row, "score", None)
            try:
                finite = type(score) in (int, float) and math.isfinite(score)
            except OverflowError:
                finite = False
            if (type(cid) is not int or cid not in pool or cid in seen
                    or not finite or score < 0):
                return ranking_error("invalid_ranker_result")
            order = (-score, cid)
            if previous is not None and order < previous:
                return ranking_error("invalid_ranker_result")
            previous = order
            seen.add(cid)
            node = self.graph.node(cid)
            results.append({"rank": len(results) + 1, "cid": cid, "name": node.name,
                            "coarse": node.coarse, "fine": node.fine, "score": score})
        result = {"schema": SCHEMA, "status": "ok", "pool_size": len(pool),
                  "results": results, "provenance": provenance}
        result["result_sha256"] = _digest(result)
        return result


def create_richard_pilot(graph_zip, source_zip):
    """Load exactly the two explicit pinned archives, then construct the ranker."""
    if not isinstance(graph_zip, (str, Path)) or not isinstance(source_zip, (str, Path)):
        raise PilotError("graph_load_failed")
    try:
        from .richard_v11_data import load_release
        from .richard_v11_ranker import (FrozenRanker, SPEC_ID, VARIANTS,
                                        ALPHA_RESTART, ALPHA_STEPS, BETA_ORDERS)
        if frozenset(VARIANTS) != METHODS:
            raise PilotError("incompatible_ranker_api")
        graph = load_release(graph_zip, source_zip)
        return RichardPilot(graph, FrozenRanker(graph), {"spec_id": SPEC_ID,
            "alpha_restart": ALPHA_RESTART, "alpha_steps": ALPHA_STEPS,
            "beta_orders": BETA_ORDERS, "top_k": TOP_K})
    except PilotError:
        raise
    except Exception:
        raise PilotError("graph_load_failed") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PilotError("invalid_cli_arguments")


def main(argv=None):
    parser = _Parser(description="Offline typed Richard solver_v1_1 concept pilot")
    parser.add_argument("--graph", required=True, help="explicit pinned v3.0.2 release ZIP")
    parser.add_argument("--source", required=True, help="explicit pinned source-data ZIP")
    parser.add_argument("--request", required=True, help="literal JSON with namespace, seeds_A/B, method, level, target")
    try:
        args = parser.parse_args(argv)
        request = parse_request(args.request)
        pilot = create_richard_pilot(args.graph, args.source)
        result = pilot(request)
    except PilotError as error:
        result = _error(error.code)
    # ASCII JSON also preserves deterministic output in Windows terminals
    # whose current code page cannot represent every graph concept name.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True,
                     separators=(",", ":"), allow_nan=False))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
