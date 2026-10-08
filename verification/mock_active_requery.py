"""Pure synthetic Console Runtime smoke; no service, credentials, or network.

Run from any directory. --package-root can select an isolated wheel install.
The compact output contains identifiers and counters, never source/question text.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path,
                        help="Directory containing indeces/; default is this checkout")
    parser.add_argument("--output", type=Path, help="Save compact synthetic JSON receipt")
    return parser.parse_args()


def _action(term, original):
    return {"action": "query", "query": {
        "entities": [{"id": "e1", "surface": original, "canonical": term,
                      "span": [0, len(original)]}],
        "relations": [], "negations": [], "time": None, "scope": None,
        "canonical_terms": [term], "ambiguities": []},
        "missing_evidence": ["Synthetic missing observation"],
        "clarification": "", "stop_reason": ""}


async def _smoke(package_root):
    from indeces import __version__
    import indeces
    from indeces.adapter import OpenAIAdapter
    from indeces.config import AdapterConfig, Budget, DiscordConfig, RuntimeConfig
    from indeces.console import create_runtime
    from indeces.contracts import DeliveryReceipt, FailureNotice, IncomingMessage
    from indeces.requery_records import bundle_model_materials, validate_bundle
    from indeces.run_records import verify_runs
    from indeces.scratch import ScratchLog, verify
    from indeces.store import Store

    loaded_package = Path(indeces.__file__).resolve().parent
    expected_package = (package_root / "indeces").resolve()
    if loaded_package != expected_package:
        raise RuntimeError("isolated package selection failed")

    requests, delivered = [], []
    original = "unknown probe"
    outputs = [_action("mockamber", original), _action("mockcobalt", original),
               {"action": "answer", "query": None, "missing_evidence": [],
                "clarification": "", "stop_reason": "evidence_sufficient"}]
    query_index = 0

    async def transport(path, payload):
        nonlocal query_index
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        requests.append((path, stage, deepcopy(payload)))
        if path == "/responses/input_tokens":
            return {"input_tokens": 64}
        if path != "/responses":
            raise AssertionError("unexpected synthetic transport path")
        if stage == "query":
            if query_index >= len(outputs):
                raise AssertionError("unexpected extra planner generation")
            text = json.dumps(outputs[query_index])
            query_index += 1
        elif stage == "reply":
            text = "Simulation only: synthetic observations [M1] and [M2]."
        else:
            raise AssertionError("smoke has no selector, summary, or label generation")
        return {"id": f"synthetic-{stage}-{len(requests)}", "status": "completed",
                "usage": {"input_tokens": 64, "output_tokens": 32},
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": text}]}]}

    async def deliver(value):
        if isinstance(value, FailureNotice):
            raise AssertionError("successful synthetic smoke unexpectedly stopped")
        delivered.append(value)
        return DeliveryReceipt(("synthetic-delivery",), value)

    # Construct the event loop before patching socket operations. All model
    # transport below is the explicit in-memory override, not mocked DNS/TLS.
    with ExitStack() as guards, TemporaryDirectory(prefix="indeces-requery-smoke-") as directory:
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "aiohttp.ClientSession"):
            guards.enter_context(patch(target, side_effect=AssertionError("real network forbidden")))
        root = Path(directory).resolve()
        store = Store(root / "state")
        scratch = ScratchLog(root / "scratch")
        adapter = None
        try:
            config = SimpleNamespace(name="Indeces", state_dir=root / "state",
                discord=DiscordConfig("10"),
                runtime=RuntimeConfig(active_requery_enabled=True,
                    model_selection_enabled=False, retrieval_policy="legacy_v1"),
                adapter=AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {
                    stage: Budget(16384, 2048, 15, "medium")
                    for stage in ("label", "summary", "reply")}))
            adapter = OpenAIAdapter(config.adapter, scratch, request=transport)
            adapter.offline_mock = True
            runtime = create_runtime(config, store, adapter, scratch)
            for suffix, mark, companion in (("amber", "mockamber", "amberdiagnostic"),
                                            ("cobalt", "mockcobalt", "cobaltdiagnostic")):
                text = f"Synthetic {suffix} observation is an isolated mock fixture."
                runtime.graph.add("10:knowledge", "synthetic:" + suffix,
                    "synthetic-source-" + suffix,
                    [{"text": text, "quote": text, "marks": [mark, companion]}], 1.0)
            static_before = list(store.db.execute("SELECT * FROM memory_static"))
            message = IncomingMessage("synthetic-requery-smoke", "20", "10", "30",
                "Synthetic operator", original, "2026-10-08T00:00:00Z")
            await runtime.process(message, deliver)
            if adapter._session is not None:
                raise AssertionError("real HTTP session created")
            if list(store.db.execute("SELECT * FROM memory_static")) != static_before:
                raise AssertionError("static graph weights changed")
            scratch.close()
            paths = sorted((root / "scratch").glob("*.jsonl"))
            entries = [json.loads(line) for path in paths
                       for line in path.read_text(encoding="utf-8").splitlines()]
            events = lambda name: [e["fields"] for e in entries if e["event"] == name]
            bundle = events("requery_evidence")[0]["bundle"]
            validate_bundle(bundle)
            materials = bundle_model_materials(bundle)
            if [m["citation_id"] for m in materials] != ["M1", "M2"]:
                raise AssertionError("stable cumulative citations missing")
            if [m["source_id"] for m in materials] != ["synthetic:amber", "synthetic:cobalt"]:
                raise AssertionError("unexpected synthetic source selection")
            stages = [stage for path, stage, _ in requests if path == "/responses"]
            if stages != ["query", "query", "query", "reply"]:
                raise AssertionError("unexpected model stage order")
            reply = next(payload for path, stage, payload in requests
                         if path == "/responses" and stage == "reply")
            historical = json.loads(reply["input"][0]["content"].split("\n", 1)[1])
            if historical["memory_citations"] != materials:
                raise AssertionError("new evidence did not reach final model input")
            if len(delivered) != 1 or events("turn_end")[0]["status"] != "delivered":
                raise AssertionError("single synthetic delivery missing")
            report = verify_runs(paths)
            envelope = [verify(path) for path in paths]
            if report["issues"] or report["counts"]["complete"] != 1:
                raise AssertionError("synthetic run-record verification failed")
            ledger = events("requery_budget_event")[-1]["ledger"]
            return {"schema": "active_requery_mock_smoke_v1", "simulation_only": True,
                "package_version": __version__, "package_root_verified": True,
                "package_init_sha256": hashlib.sha256(Path(indeces.__file__).read_bytes()).hexdigest(),
                "console_factory": "indeces.console.create_runtime",
                "service_started": False, "discord_gateway_started": False,
                "credentials_loaded": False, "production_data_read": False,
                "real_network_calls": 0, "real_model_calls": 0,
                "initial_model_selector_enabled": False,
                "generation_stages": stages, "mock_transport_requests": len(requests),
                "retrieval_rounds": [{"round_index": item["round_index"],
                    "request_id": item["request_id"],
                    "selected_count": len(item["record"]["materials"]),
                    "cumulative_citation_ids": [m["citation_id"] for m in bundle["materials"]
                                                if m["round_index"] <= item["round_index"]]}
                    for item in bundle["rounds"]],
                "material_identities": [{key: material[key] for key in (
                    "citation_id", "evidence_uid", "source_id", "round_index", "request_id")}
                    for material in materials],
                "static_weights_unchanged": True, "final_input_evidence_verified": True,
                "delivery_count": len(delivered),
                "usage": {key: ledger[key] for key in ("input_tokens", "output_tokens",
                    "estimated_cost_usd", "cost_basis", "usage_complete", "unknown_generation_count")},
                "ledger_phase": ledger["phase"],
                "verify_runs": {"counts": report["counts"], "issues_count": len(report["issues"])},
                "scratch_envelope_verification": envelope,
                "semantic_support": "not_evaluated", "real_quality_or_latency": "not_measured"}
        finally:
            if adapter is not None:
                await adapter.close()
            scratch.close()
            store.close()


def main():
    arguments = _arguments()
    package_root = (arguments.package_root or Path(__file__).resolve().parents[1]).resolve()
    if not (package_root / "indeces" / "__init__.py").is_file():
        raise SystemExit("package root must contain indeces/__init__.py")
    sys.path.insert(0, str(package_root))
    receipt = asyncio.run(_smoke(package_root))
    text = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
