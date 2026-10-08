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
import traceback
from types import SimpleNamespace
from unittest.mock import patch


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path,
                        help="Directory containing indeces/; default is this checkout")
    parser.add_argument("--output", type=Path, help="Save compact synthetic JSON receipt")
    parser.add_argument("--communicability", action="store_true",
                        help="Also exercise the independent frozen graph numerical diagnostic")
    parser.add_argument("--automatic-paths", action="store_true",
                        help="Also discover typed paths automatically from frozen synthetic facts")
    return parser.parse_args()


def _action(term, original):
    return {"action": "query", "query": {
        "entities": [{"id": "e1", "surface": original, "canonical": term,
                      "span": [0, len(original)]}],
        "relations": [], "negations": [], "time": None, "scope": None,
        "canonical_terms": [term], "ambiguities": []},
        "missing_evidence": ["Synthetic missing observation"],
        "clarification": "", "stop_reason": ""}


def _path_action(terms, original):
    def anchor(surface):
        start = original.index(surface)
        return {"surface": surface, "span": [start, start + len(surface)]}
    value = _action(terms[0], original)
    value["query"].update({"entities": [
        dict(anchor("mockamber"), id="e1", canonical="alpha"),
        dict(anchor("endpoint"), id="e2", canonical="beta")],
        "relations": [dict(anchor("follows"), subject_id="e1", object_id="e2",
            predicate="links", negated=False, direction="subject_to_object")],
        "canonical_terms": terms})
    return value


async def _smoke(package_root, communicability=False, automatic_paths=False):
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
    original = "mockamber follows endpoint" if automatic_paths else "mockamber"
    make_action = (lambda term: _path_action([term, term + "diagnostic"], original)
                   if automatic_paths else _action(term, original))
    outputs = [make_action("mockcobalt"), make_action("mockdelta"),
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
        elif stage == "selection":
            data = json.loads(payload["input"][0]["content"])
            text = json.dumps({"selected_record_ids": [
                c["record_id"] for c in data["candidates"][:data["required_selection_count"]]]})
        elif stage == "summary":
            text = json.dumps({"summary": "Synthetic bounded continuity summary."})
        elif stage == "reply":
            historical = json.loads(payload["input"][0]["content"].split("\n", 1)[1])
            markers = " ".join(material["citation_marker"] for material in historical["memory_citations"])
            text = "Simulation only: synthetic observations " + markers + "."
        else:
            raise AssertionError("unexpected synthetic model stage")
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
            runtime_options = {"active_requery_enabled": True,
                "summary_max_bytes": 256, "active_requery_output_tokens": 1024,
                "communicability_enabled": communicability}
            if automatic_paths:
                runtime_options["path_hypotheses_enabled"] = True
            config = SimpleNamespace(name="Indeces", state_dir=root / "state",
                discord=DiscordConfig("10"),
                runtime=RuntimeConfig(**runtime_options),
                adapter=AdapterConfig("gpt-6.1-sol", "https://api.openai.com/v1", {
                    stage: Budget(5500 if stage == "reply" else 16384, 2048, 15, "medium")
                    for stage in ("label", "summary", "reply")}))
            adapter = OpenAIAdapter(config.adapter, scratch, request=transport)
            adapter.offline_mock = True
            runtime = create_runtime(config, store, adapter, scratch)
            if (config.runtime.retrieval_policy != "concept_v1"
                    or not config.runtime.model_selection_enabled):
                raise AssertionError("default retrieval and selector flags were changed")
            for suffix, mark, companion in (("amber", "mockamber", "amberdiagnostic"),
                                            ("cobalt", "mockcobalt", "mockcobaltdiagnostic"),
                                            ("delta", "mockdelta", "mockdeltadiagnostic")):
                text = f"Synthetic {suffix} observation is an isolated mock fixture."
                runtime.graph.add("10:knowledge", "synthetic:" + suffix,
                    "synthetic-source-" + suffix,
                    [{"text": text, "quote": text, "marks": [mark, companion]}], 1.0)
            if automatic_paths:
                for suffix, subject, target in (("fact-left", "alpha", "beta"),
                                                ("fact-right", "beta", "gamma")):
                    text = "typed_facts_v1: " + json.dumps({"facts": [{"subject": subject,
                        "object": target, "relation": "links", "polarity": "positive"}]})
                    runtime.graph.add("10:knowledge", "synthetic:" + suffix,
                        "synthetic-source-" + suffix,
                        [{"text": text, "quote": text, "marks": [subject, target]}], 1.0)
            static_before = list(store.db.execute("SELECT * FROM memory_static"))
            message = IncomingMessage("synthetic-requery-smoke", "20", "10", "30",
                "Synthetic operator", original, "2026-10-08T00:00:00Z")
            previous = IncomingMessage("synthetic-prior-smoke", "20", "10", "30",
                "Synthetic operator", "Synthetic continuity " + "x" * 1200,
                "2026-10-08T00:00:00Z")
            store.begin(previous)
            store.generated(previous.message_id, "Synthetic preceding observation.")
            store.finish(previous, DeliveryReceipt(("synthetic-prior-delivery",),
                "Synthetic preceding observation."))
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
            if [m["citation_id"] for m in materials][:2] != ["M1", "M2"]:
                raise AssertionError("stable cumulative citations missing")
            if materials[0]["source_id"] != "synthetic:amber" or materials[0]["round_index"] != 0:
                raise AssertionError("initial citation was replaced")
            if [r["round_index"] for r in bundle["rounds"]] != [0, 1, 2]:
                raise AssertionError("two additional graph query rounds missing")
            if any(not any(m["round_index"] == index for m in materials) for index in (1, 2)):
                raise AssertionError("a query round added no new material")
            if any(r["record"]["graph_audit"]["selection"]["concept_policy"]["policy"]
                   != "concept_v1" for r in bundle["rounds"]):
                raise AssertionError("native query bypassed configured concept policy")
            stages = [stage for path, stage, _ in requests if path == "/responses"]
            if stages != ["selection", "summary", "query", "query", "query", "reply"]:
                raise AssertionError("unexpected model stage order")
            if (len(events("model_selection_decision")) != 1
                    or not events("model_selection_decision")[0]["model_call_performed"]
                    or len(events("checkpoint_saved")) != 1):
                raise AssertionError("real selector or summary execution missing")
            reply = next(payload for path, stage, payload in requests
                         if path == "/responses" and stage == "reply")
            historical = json.loads(reply["input"][0]["content"].split("\n", 1)[1])
            if historical["memory_citations"] != materials:
                raise AssertionError("new evidence did not reach final model input")
            if historical["continuity_summary"] != "Synthetic bounded continuity summary.":
                raise AssertionError("actual summary did not reach final model input")
            if len(delivered) != 1 or events("turn_end")[0]["status"] != "delivered":
                raise AssertionError("single synthetic delivery missing")
            report = verify_runs(paths)
            envelope = [verify(path) for path in paths]
            if report["issues"] or report["counts"]["complete"] != 1:
                raise AssertionError("synthetic run-record verification failed")
            ledger = events("requery_budget_event")[-1]["ledger"]
            diagnostic_fields = {}
            if communicability:
                from indeces.run_records import digest
                diagnostics = events("communicability_diagnostic")
                if len(diagnostics) != 1 or diagnostics[0]["evidence_sha256"] != digest(bundle):
                    raise AssertionError("diagnostic is not bound to the completed bundle")
                diagnostic = diagnostics[0]["receipt"]
                numerical = diagnostic.get("numerical", {})
                if (not diagnostic.get("edges") or numerical.get("matvecs", 0) <= 0
                        or diagnostic["proof"] is not False
                        or diagnostic["semantic_status"] != "insufficient_relation_semantics"):
                    raise AssertionError("association diagnostic contract failed")
                if diagnostic["status"] == "unknown" and numerical.get("reason") != "krylov_or_matvec_limit":
                    raise AssertionError("unexpected diagnostic metadata or numeric error")
                diagnostic_fields = {"communicability": {
                    "status": diagnostic["status"], "edge_count": len(diagnostic["edges"]),
                    "evidence_kind": diagnostics[0]["evidence_kind"],
                    "semantic_status": diagnostic["semantic_status"], "proof": False,
                    "numerical_accepted_observation": diagnostic["numerical"].get("accepted", False),
                    "matvecs": numerical["matvecs"], "numerical_stop_reason": numerical.get("reason"),
                    "certified_total_error_bound": diagnostic["numerical"].get("certified_total_error_bound")}}
            if automatic_paths:
                from indeces.run_records import digest
                diagnostics = events("path_hypotheses_diagnostic")
                if (len(diagnostics) != 1 or diagnostics[0]["evidence_sha256"] != digest(bundle)
                        or not diagnostics[0]["independent_diagnostic"]
                        or diagnostics[0]["query_actions_sha256"] != digest(diagnostics[0]["query_actions"])):
                    raise AssertionError("automatic paths diagnostic binding failed")
                actions = diagnostics[0]["query_actions"]
                if (len(actions) != 2 or [a["evidence_round_index"] for a in actions] != [1, 2]
                        or any(a["anchors_validated"] is not True for a in actions)):
                    raise AssertionError("automatic query wrappers are not bound to both actual rounds")
                automatic = diagnostics[0]["receipt"]
                candidates = automatic.get("candidates", [])
                pending = [candidate for candidate in candidates if candidate["status"] == "pending_hypothesis"]
                if (automatic["status"] != "pending_hypothesis" or not pending
                        or not automatic.get("meetings")
                        or any(item["automatic"] is not True for item in automatic["meetings"])
                        or automatic["proof"] is not False):
                    raise AssertionError("automatic frozen graph meeting was not discovered")
                catalog = {m["evidence_uid"]: m for m in bundle["materials"]}
                for candidate in pending:
                    if not candidate["edge_bindings"] or candidate["proof"] is not False:
                        raise AssertionError("pending path has no frozen source binding")
                    for binding in candidate["edge_bindings"]:
                        material = catalog[binding["evidence_uid"]]
                        if (binding["citation_id"] != material["citation_id"]
                                or binding["source_id"] != material["material"]["source_id"]
                                or binding["quote_sha256"] != hashlib.sha256(
                                    material["material"]["quote"].encode("utf-8")).hexdigest()):
                            raise AssertionError("automatic path source binding does not match frozen evidence")
                diagnostic_fields["automatic_paths"] = {
                    "status": automatic["status"], "receipt_sha256": digest(automatic),
                    "query_action_count": len(actions), "candidate_count": len(candidates),
                    "automatic_meeting_count": len(automatic["meetings"]),
                    "pending_hypothesis_count": len(pending),
                    "unknown_candidate_count": sum(c["status"] == "unknown" for c in candidates),
                    "rejected_candidate_count": sum(c["status"] == "rejected" for c in candidates),
                    "source_bindings_verified_against_bundle": True,
                    "proof": False, "truth_verified": automatic["truth_verified"],
                    "semantic_support_verified": automatic["semantic_support_verified"],
                    "proposals_verified": automatic["proposals_verified"],
                    "incomplete": automatic["incomplete"],
                    "incomplete_reasons": automatic["incomplete_reasons"],
                    "stats": automatic["stats"],
                    "candidates": [{"candidate_sha256": digest(candidate),
                        "status": candidate["status"], "reasons": candidate["reasons"],
                        "round_index": candidate["round_index"], "request_id": candidate["request_id"],
                        "planning_call_id": candidate["planning_call_id"],
                        "path_node_count": len(candidate["path_nodes"]),
                        "meeting_node_sha256": hashlib.sha256(candidate["meeting_node"].encode()).hexdigest(),
                        "bindings": [{key: binding[key] for key in (
                            "edge_id", "evidence_uid", "citation_id", "source_id",
                            "quote_sha256", "stored_text_sha256", "fact_line_sha256")}
                            for binding in candidate["edge_bindings"]]}
                        for candidate in candidates]}
            return {"schema": "active_requery_mock_smoke_v1", "simulation_only": True,
                "package_version": __version__, "package_root_verified": True,
                "package_init_sha256": hashlib.sha256(Path(indeces.__file__).read_bytes()).hexdigest(),
                "console_factory": "indeces.console.create_runtime",
                "service_started": False, "discord_gateway_started": False,
                "credentials_loaded": False, "production_data_read": False,
                "real_network_calls": 0, "real_model_calls": 0,
                "initial_model_selector_enabled": True,
                "retrieval_policy": config.runtime.retrieval_policy,
                "actual_summary_checkpoint_saved": True,
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
                **diagnostic_fields,
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
    try:
        receipt = asyncio.run(_smoke(package_root, arguments.communicability, arguments.automatic_paths))
        exit_code = 0
    except Exception as error:
        # Never print arbitrary exception text: imported components may carry
        # source bodies. A failed receipt remains useful without private data.
        frame = traceback.extract_tb(error.__traceback__)[-1]
        receipt = {"schema": "active_requery_mock_smoke_v1", "simulation_only": True,
                   "status": "failed", "error_type": type(error).__name__,
                   "error_location": {"module": Path(frame.filename).name,
                                      "function": frame.name, "line": frame.lineno},
                   "automatic_paths_requested": arguments.automatic_paths,
                   "communicability_requested": arguments.communicability,
                   "real_network_calls": 0, "real_model_calls": 0}
        exit_code = 1
    text = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
