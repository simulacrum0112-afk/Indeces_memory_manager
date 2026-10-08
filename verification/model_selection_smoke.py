"""One synthetic Console-factory selection/reply smoke; never real transport."""
from __future__ import annotations

import asyncio
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--installed-root", type=Path,
                    help="Isolated installed package directory, searched before checkout source")
args = parser.parse_args()
if args.installed_root is not None:
    installed_root = args.installed_root.resolve()
    if not (installed_root / "indeces" / "__init__.py").is_file():
        parser.error("--installed-root must contain the installed indeces package")
    sys.path.insert(0, str(installed_root))

import indeces
from indeces import __version__
from indeces.model_selection import ModelNPMILabelSelector
from indeces.run_records import validate_graph_audit, validate_retrieval, verify_runs
from tests.test_model_selection import run_synthetic_console_trial
from tests.test_selection_policy_integration import SelectorFixture

PACKAGE_ROOT = Path(indeces.__file__).resolve().parent
if args.installed_root is not None and PACKAGE_ROOT != installed_root / "indeces":
    raise RuntimeError("Isolated installed package was not loaded")


async def smoke():
    fixture = SelectorFixture()
    fixture.setup_selector_fixture()
    try:
        fixture.seed_candidates()
        result = await run_synthetic_console_trial(fixture)
        events = lambda name: [entry["fields"] for entry in result["entries"] if entry["event"] == name]
        assert isinstance(result["runtime"].model_selector, ModelNPMILabelSelector)
        assert result["call_counts"]["prepare"] == result["call_counts"]["finish"] == 1
        assert result["call_counts"]["retrieve"] <= 1
        assert result["transaction_states"] == [False, False]
        assert [(path, stage) for path, _, stage in result["requests"]] == [
            ("/responses/input_tokens", "selection"), ("/responses", "selection"),
            ("/responses/input_tokens", "reply"), ("/responses", "reply")]
        record = events("retrieval_record")[0]["record"]
        chosen = events("model_selection_decision")[0]
        selected_ids = [item["id"] for item in record["model_materials"]]
        assert selected_ids == result["choices"] == chosen["selected_record_ids"]
        receipt = record["graph_audit"]["selection"]["selector_receipt"]
        assert selected_ids == receipt["selected_record_ids"]
        ranked = record["graph_audit"]["selection"]["ranked_candidates"]
        ranks = [next(row["rank"] for row in ranked if row["record_id"] == record_id) for record_id in selected_ids]
        assert len(ranks) == 3 and all(rank > 3 for rank in ranks)
        assert all(len(item["quote"]) == 400 for item in record["model_materials"])
        validate_retrieval(record)
        validate_graph_audit(record["graph_audit"], record)
        selection_input = events("model_selection_input")[0]
        model_input = next(payload for path, payload, stage in result["requests"]
                           if path == "/responses" and stage == "selection")
        assert selection_input["messages"] == model_input["input"]
        assert selection_input["instructions"] == model_input["instructions"]
        assert selection_input["response_schema"] == model_input["text"]["format"]["schema"]
        calls = {item["stage"]: item for item in events("call_end")}
        assert set(calls) == {"selection", "reply"}
        assert calls["selection"]["result"] == chosen["model_result"]
        assert len(result["deliveries"]) == 1
        verified = verify_runs([result["path"]])
        assert verified["issues"] == [] and verified["counts"]["complete"] == 1
        files = ("console.py", "runtime.py", "memory.py", "model_selection.py", "selection_policy.py", "adapter.py")
        return {
            "status": "pass", "version": __version__, "source_root": str(PACKAGE_ROOT.parent),
            "installed_package_selected": args.installed_root is not None,
            "source_sha256": {name: hashlib.sha256((PACKAGE_ROOT / name).read_bytes()).hexdigest() for name in files},
            "policy": receipt["policy"], "candidate_count": selection_input["candidate_count"],
            "seen_record_ids": selection_input["seen_record_ids"],
            "omitted_record_ids": selection_input["omitted_record_ids"],
            "model_selected_record_ids": chosen["selected_record_ids"],
            "frozen_record_ids": selected_ids, "selected_ranks": ranks,
            "budgets": {stage: asdict(fixture.config.adapter.budgets[stage]) for stage in ("selection", "reply")},
            "usage": {stage: {"input_tokens": item["result"]["input_tokens"],
                               "output_tokens": item["result"]["output_tokens"]} for stage, item in calls.items()},
            "mock_count_requests": 2, "mock_generation_requests": 2, "real_model_requests": 0,
            "billing_verified": False, "retrieval_calls": result["call_counts"],
            "sqlite_transaction_across_selection_await": False, "run_contract_complete": 1,
            "assertions_passed": True,
        }
    finally:
        fixture.teardown_selector_fixture()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke()), ensure_ascii=False, sort_keys=True))
