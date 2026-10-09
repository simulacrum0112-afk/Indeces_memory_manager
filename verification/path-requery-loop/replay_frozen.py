"""Replay saved synthetic native graph bytes; no database, provider, or gold.

Run from the checkout: python verification/path-requery-loop/replay_frozen.py
"""
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from indeces.path_requery_loop import PathRequeryLoopConfig, build_path_feedback
from indeces.run_records import digest


def stable(receipt):
    value = deepcopy(receipt)
    value.pop("feedback_sha256", None)
    value["stats"].pop("elapsed_seconds", None)
    return value


def main():
    fixture = json.loads((Path(__file__).with_name("FROZEN_SYNTHETIC.json")).read_text(encoding="utf-8"))
    before = digest(fixture)
    rows = []
    with patch.object(socket.socket, "connect", side_effect=AssertionError("offline replay forbids network")):
        for case in fixture["cases"]:
            # This saved fixture belongs to the original round-local dev11
            # snapshot. Keep that policy, rather than rewriting its hashes.
            cfg = PathRequeryLoopConfig(**dict(case["config"], accumulate_rounds=False))
            result = build_path_feedback(case["frozen"], case["action"],
                planning_call_id=case["planning_call_id"], config=cfg)
            assert digest(stable(result)) == case["expected_stable_receipt_sha256"], case["name"]
            assert result["proof"] is False and result["semantic_support_verified"] is False
            rows.append({"name": case["name"], "status": result["status"],
                         "added_terms": result["added_terms"], "path_count": len(result["candidates"]),
                         "replayed": True, "semantic_support_verified": False})
    assert digest(fixture) == before
    print(json.dumps({"schema": "path_requery_frozen_synthetic_replay_v1", "cases": rows,
                      "input_unchanged": True, "database_access": False, "provider_calls": 0,
                      "gold_or_private_questions": False}, indent=2))


if __name__ == "__main__":
    main()
