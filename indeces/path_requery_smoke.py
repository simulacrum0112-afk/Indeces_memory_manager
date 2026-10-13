"""Separately authorized, isolated synthetic path-loop smoke entry.

The CLI defaults to a side-effect-free plan. No model call is authorized by
importing this module, running the plan, or by the presence of an API key.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sys

from . import __version__


MODEL = "gpt-6.1-sol"
BASE_URL = "https://api.openai.com/v1"
STAGES = {
    "label": {"input_tokens": 4096, "output_tokens": 512, "seconds": 15.0},
    "selection": {"input_tokens": 4096, "output_tokens": 512, "seconds": 15.0},
    "query": {"input_tokens": 4096, "output_tokens": 512, "seconds": 15.0},
    "summary": {"input_tokens": 16384, "output_tokens": 2048, "seconds": 60.0},
    "reply": {"input_tokens": 16384, "output_tokens": 2048, "seconds": 45.0},
}
LIMITS = {"calls": 6, "query_rounds": 2, "input_tokens": 32768,
          "output_tokens": 4096, "seconds": 60.0, "local_seconds": 5.0}
QUESTION = "alpha links gamma"
SOURCE_A = "synthetic:c9-source-A"
SOURCE_B = "synthetic:c9-source-B"
PROTECTED_FOLDERS = frozenset({"knowledge", "state", "scratch", ".git", ".indeces", "_staging"})


class SmokeRefused(ValueError):
    """Stable diagnostics never include environment or arbitrary provider text."""


def _positive(value, name):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise SmokeRefused("invalid_" + name) from None
    if not parsed.is_finite() or not 0 < parsed <= 1000 or float(parsed) <= 0:
        raise SmokeRefused("invalid_" + name)
    return parsed


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--execute", action="store_true",
                        help="Requires separate explicit approval and all three positive budget values.")
    result.add_argument("--run-dir", help="A new absolute directory with an existing unlinked parent.")
    result.add_argument("--approved-cost-usd", default="0")
    result.add_argument("--input-upper-usd-per-million", default="0")
    result.add_argument("--output-upper-usd-per-million", default="0")
    return result


def make_plan(args):
    """Pure metadata: do not inspect paths, environment, credentials or state."""
    supplied = (args.approved_cost_usd, args.input_upper_usd_per_million,
                args.output_upper_usd_per_million)
    if args.execute:
        prices = [_positive(value, name) for value, name in zip(supplied, (
            "approved_cost_usd", "input_upper_usd_per_million", "output_upper_usd_per_million"))]
        if not args.run_dir:
            raise SmokeRefused("new_run_directory_required")
    elif all(value == "0" for value in supplied):
        prices = None
    else:
        prices = [_positive(value, name) for value, name in zip(supplied, (
            "approved_cost_usd", "input_upper_usd_per_million", "output_upper_usd_per_million"))]
    theoretical = (None if prices is None else
        (Decimal(LIMITS["input_tokens"]) * prices[1]
         + Decimal(LIMITS["output_tokens"]) * prices[2]) / 1000000)
    return {
        "schema": "path_requery_smoke_plan_v1", "version": __version__,
        "mode": "execute_requested" if args.execute else "dry_run",
        "run_dir": args.run_dir, "model": MODEL, "base_url": BASE_URL,
        "reasoning": "medium", "verbosity": "high", "model_concurrency": 1,
        "stage_caps": {name: dict(cap, reasoning="medium") for name, cap in STAGES.items()},
        "cumulative_caps": dict(LIMITS),
        "approved_cost_usd": None if prices is None else str(prices[0]),
        "input_upper_usd_per_million": None if prices is None else str(prices[1]),
        "output_upper_usd_per_million": None if prices is None else str(prices[2]),
        "configured_token_cap_cost_usd": None if theoretical is None else str(theoretical),
        "admission_cost_ceiling_usd": None if theoretical is None else str(min(prices[0], theoretical)),
        "cost_basis": "user_supplied_upper_unit_prices_not_verified_tariff_or_invoice",
        "synthetic_fixture": "strict_two_source_two_round_c9_v1",
        "question": QUESTION, "source_records": 20, "automatic_retries": 0,
        "environment_read": False, "state_created": False, "network_issued": False,
        "requires_separate_execution_approval": True,
        "cli_flags_are_not_proof_of_user_approval": True,
        "provider_quota_status": "unknown",
        "scientific_or_answer_quality_verified": False,
    }


def _new_directory(value):
    # Refuse re-entry before reading any environment, including path-policy
    # OneDrive roots. A new name cannot reopen an existing ledger.
    path = Path(value)
    if not path.is_absolute():
        raise SmokeRefused("absolute_run_directory_required")
    # Never plant this fixture in an existing material/management tree, even
    # under a new final name. Preserve the raw spelling check and also check
    # normalized ancestors; dot traversal must not bypass this boundary.
    for candidate in (path, Path(os.path.normpath(path))):
        if any(part.casefold() in PROTECTED_FOLDERS for part in candidate.parts):
            raise SmokeRefused("protected_run_directory_ancestor")
    if os.path.lexists(path):
        raise SmokeRefused("run_directory_already_exists")
    if not path.parent.is_dir():
        raise SmokeRefused("run_directory_parent_required")
    from .path_policy import validate_managed_path
    path = validate_managed_path(path, "isolated path smoke")
    # Existing-prefix canonicalization can expose short-name/path aliases.
    if any(part.casefold() in PROTECTED_FOLDERS for part in path.parts):
        raise SmokeRefused("protected_run_directory_ancestor")
    if os.path.lexists(path):
        raise SmokeRefused("run_directory_already_exists")
    return path


def _read_key():
    # This is the only explicit credential read; CLI execute gates precede it.
    return os.environ.get("OPENAI_API_KEY", "")


def _write_new_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _config_text(plan):
    lines = ['name = "Indeces"', 'state_dir = "state"', 'scratch_dir = "scratch"',
             'knowledge_dir = "knowledge"', '', '[discord]', 'guild_id = "10"',
             '', '[runtime]', 'turn_seconds = 60.0', 'local_seconds = 5.0',
             'retrieval_policy = "legacy_v1"', 'model_selection_enabled = true',
             'active_requery_enabled = true', 'path_requery_loop_enabled = true',
             'active_requery_allow_real_calls = true', 'active_requery_max_rounds = 2',
             'active_requery_max_calls = 6', 'active_requery_input_tokens = 32768',
             'active_requery_output_tokens = 4096', 'active_requery_seconds = 60.0',
             'active_requery_cost_usd = ' + plan['approved_cost_usd'],
             'active_requery_input_usd_per_million = ' + plan['input_upper_usd_per_million'],
             'active_requery_output_usd_per_million = ' + plan['output_upper_usd_per_million'],
             '', '[adapter]', 'model = "' + MODEL + '"',
             'base_url = "' + BASE_URL + '"', 'verbosity = "high"']
    for stage, cap in STAGES.items():
        lines.extend(['', '[adapter.budgets.' + stage + ']'] +
            [key + ' = ' + str(value) for key, value in cap.items()] + ['reasoning = "medium"'])
    return "\n".join(lines) + "\n"


def _seed(graph):
    """Only synthetic observations; no ingestion, labels, PDFs or file sources."""
    scope = "10:knowledge"
    ids = {}
    def add(name, quote, marks):
        row = graph.add(scope, name, "synthetic-author", [{
            "text": quote, "quote": quote, "marks": marks}], 1.0)[0]
        ids[name] = row["id"]
    for mark in ("amber", "cobalt"):
        add("synthetic:" + mark, "Synthetic " + mark + " background observation.", [mark])
    for name, subject, object_ in ((SOURCE_B, "beta", "gamma"), (SOURCE_A, "alpha", "beta")):
        quote = "typed_facts_v1: " + json.dumps({"facts": [{
            "subject": subject, "object": object_, "relation": "links",
            "polarity": "positive"}]}, separators=(",", ":"))
        add(name, quote, [subject, object_])
    for name in ("entry-one", "entry-two"):
        add("synthetic:c9-" + name, "Bibliography\nSynthetic alpha gamma publication " + name + ".",
            ["alpha", "gamma"])
    add("synthetic:c9-alpha-only", "Synthetic alpha endpoint observation.", ["alpha"])
    for number in range(13):
        add("synthetic:c9-background-" + str(number),
            "Synthetic unrelated background observation " + str(number) + ".", ["unrelated" + str(number)])
    return ids


def _safe_error(error):
    code = getattr(error, "code", None)
    if isinstance(error, SmokeRefused):
        code = str(error)
    return {"type": type(error).__name__, "code": code if type(code) is str
            and re.fullmatch(r"[a-z0-9_]{1,96}", code) else "unexpected_failure"}


def _observation(run_dir, *, live_provider, failure=None):
    """Summarize this new run only; raw model output stays in its scratch log."""
    from .run_records import digest, verify_runs
    from .requery_records import bundle_model_materials
    logs = sorted((run_dir / "scratch").glob("*.jsonl"))
    entries = [json.loads(line) for log in logs for line in log.read_text(encoding="utf-8").splitlines()]
    def fields(event):
        return [item["fields"] for item in entries if item["event"] == event]
    retrievals = fields("requery_retrieval")
    pre, post = fields("path_requery_feedback"), fields("path_requery_feedback_result")
    bundles = fields("requery_evidence")
    bundle = bundles[-1]["bundle"] if bundles else None
    materials = bundle_model_materials(bundle) if bundle is not None else []
    accumulated = [row for item in post for index in item["result_receipt"].get(
        "accumulated_candidate_indices", []) for row in [item["result_receipt"]["candidates"][index]]]
    planning_inputs, final_sources = [], set()
    for item in fields("http_request"):
        if item["path"] != "/responses":
            continue
        payload = item["payload"]
        stage = payload.get("text", {}).get("format", {}).get("name", "reply")
        if stage == "query":
            planning_inputs.append(json.loads(payload["input"][-1]["content"]))
        elif stage == "reply":
            prefix = "Historical context data:\n"
            first = payload["input"][0]["content"]
            if first.startswith(prefix):
                context = json.loads(first[len(prefix):])
                final_sources.update(row["source_id"] for row in context.get("memory_citations", []))
    next_feedback_observed = bool(post and any(
        plan.get("path_feedback") == {"input": pre_item["receipt"], "result": post_item["result_receipt"]}
        and plan.get("path_feedback_sha256") == digest(plan["path_feedback"])
        for plan in planning_inputs for pre_item, post_item in zip(pre, post)))
    round_sources = [[row["source_id"] for row in item["record"]["materials"]] for item in retrievals]
    strict = bool(len(round_sources) >= 2 and SOURCE_A in round_sources[0]
        and SOURCE_B not in round_sources[0] and any(SOURCE_B in row and SOURCE_A not in row
                                                   for row in round_sources[1:]))
    selected_sources = {row["source_id"] for row in materials}
    two_sources_final = {SOURCE_A, SOURCE_B} <= selected_sources and {SOURCE_A, SOURCE_B} <= final_sources
    ledgers = sorted((run_dir / "state" / "active_requery_ledger").glob("*.json"))
    ledger = json.loads(ledgers[0].read_text(encoding="utf-8")) if len(ledgers) == 1 else None
    validation = verify_runs(logs) if logs else None
    complete = bool(failure is None and ledger and ledger["usage_complete"] and ledger["phase"] == "completed"
                    and validation and not validation["issues"] and fields("answer_delivered"))
    qualified = [row for row in accumulated if row.get("path_nodes") == ["alpha", "beta", "gamma"]
        and row.get("structural_support_qualified") is True
        and len(row.get("round_indices", [])) >= 2
        and {SOURCE_A, SOURCE_B} <= {binding["source_id"]
            for binding in row.get("edge_source_bindings", [])}]
    formal = [row for row in qualified if (row.get("formal_derivation") or {}).get(
        "status") == "formal_under_declared_rule"]
    observed = bool(strict and formal and next_feedback_observed and two_sources_final and complete)
    path_status = ("observed" if observed else "partial" if post else "not_executed")
    return {
        "schema": "path_requery_smoke_result_v1", "version": __version__,
        "status": "completed" if complete else "incomplete",
        "live_provider_transport": live_provider,
        "path_loop_observation": path_status, "strict_split_source_rounds_observed": strict,
        "round_source_ids": round_sources, "native_requery_rounds": max(0, len(retrievals) - 1),
        "path_feedback_events": len(pre), "path_feedback_results": len(post),
        "accumulated_candidate_count": len(accumulated),
        "qualified_synthetic_chain_count": len(qualified),
        "declared_synthetic_formal_chain_count": len(formal),
        "next_planner_received_bound_feedback": next_feedback_observed,
        "both_source_bodies_in_final_context": two_sources_final,
        "bundle_sha256": digest(bundle) if bundle is not None else None,
        "final_evidence_uid_count": len(materials),
        "usage": None if ledger is None else {key: ledger[key] for key in (
            "mode", "phase", "limits", "input_tokens", "output_tokens", "estimated_cost_usd",
            "cost_basis", "usage_complete", "unknown_generation_count", "halted", "automatic_retries")},
        "provider_stages": [] if ledger is None else [call["stage"] for call in ledger["calls"]],
        "turn_end": [{key: item[key] for key in (
            "status", "code", "stop_reason", "bundle_sha256", "evidence_gain_by_round")
            if key in item} for item in fields("turn_end")], "failure": failure,
        "record_validation": validation,
        "proof": False, "truth_verified": False, "semantic_support_verified": False,
        "scientific_or_answer_quality_verified": False,
        "automatic_retries": 0, "existing_run_reentry_allowed": False,
        "cli_flags_are_not_proof_of_user_approval": True,
    }


async def execute_plan(plan, *, request=None, key_reader=None):
    """Test injection is not exposed through CLI; normal execution uses HTTP."""
    if plan.get("mode") != "execute_requested":
        raise SmokeRefused("explicit_execute_required")
    # Independently revalidate the permission values before path or key access.
    args = parser().parse_args(["--execute", "--run-dir", str(plan.get("run_dir") or ""),
        "--approved-cost-usd", str(plan.get("approved_cost_usd") or "0"),
        "--input-upper-usd-per-million", str(plan.get("input_upper_usd_per_million") or "0"),
        "--output-upper-usd-per-million", str(plan.get("output_upper_usd_per_million") or "0")])
    approved = make_plan(args)
    run_dir = _new_directory(approved["run_dir"])
    key = (key_reader or _read_key)()
    if type(key) is not str or not key.strip():
        raise SmokeRefused("existing_environment_api_key_required")
    # Exclusive create is the run lease. It is never removed on failure.
    try:
        run_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        raise SmokeRefused("run_directory_already_exists") from None
    _write_new_json(run_dir / "RUN_INTENT.json", dict(approved, mode="execution_intent",
        environment_read=True, state_created=False, network_issued=False,
        automatic_retries=0, future_reentry_allowed=False))
    from .adapter import OpenAIAdapter
    from .config import load_config
    from .console import create_runtime
    from .contracts import DeliveryReceipt, FailureNotice, IncomingMessage
    from .path_requery_loop import PathRequeryLoopConfig
    from .scratch import ScratchLog
    from .store import Store
    config_path = run_dir / "SYNTHETIC_CONFIG.toml"
    with config_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_config_text(approved))
    scratch = store = adapter = None
    failure = None
    try:
        config = load_config(config_path)
        scratch = ScratchLog(config.scratch_dir)
        store = Store(config.state_dir)
        adapter = OpenAIAdapter(config.adapter, scratch, key, request=request)
        runtime = create_runtime(config, store, adapter, scratch)
        runtime.path_requery_loop_config = PathRequeryLoopConfig(enabled=True,
            composition_rules=({"rule_id": "synthetic-links-composition", "relation1": "links",
                                "relation2": "links", "result": "links"},))
        ids = _seed(runtime.graph)
        _write_new_json(run_dir / "SYNTHETIC_SOURCE_IDS.json", ids)
        message = IncomingMessage("isolated-c9-smoke", "20", "10", "30", "Synthetic operator",
            QUESTION, datetime.now(timezone.utc).isoformat())
        async def deliver(value):
            # Delivery is a local receipt: no Discord bridge or webhook exists.
            return DeliveryReceipt(("isolated-local-receipt",),
                value.text if isinstance(value, FailureNotice) else value)
        await runtime.process(message, deliver)
    except Exception as error:
        failure = _safe_error(error)
    finally:
        for resource, asynchronous in ((adapter, True), (scratch, False), (store, False)):
            if resource is None:
                continue
            try:
                if asynchronous:
                    await resource.close()
                else:
                    resource.close()
            except Exception as error:
                if failure is None:
                    failure = _safe_error(error)
        key = ""
    report = _observation(run_dir, live_provider=request is None, failure=failure)
    _write_new_json(run_dir / "RESULT.json", report)
    return report


def main(argv=None, *, _request=None, _key_reader=None):
    try:
        plan = make_plan(parser().parse_args(argv))
        report = (asyncio.run(execute_plan(plan, request=_request, key_reader=_key_reader))
                  if plan["mode"] == "execute_requested" else plan)
    except (SmokeRefused, ValueError, OSError) as error:
        report = {"schema": "path_requery_smoke_refusal_v1", "status": "refused",
                  "error": _safe_error(error), "automatic_retries": 0}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if report.get("mode") == "dry_run" or (
        report.get("status") == "completed" and report.get("path_loop_observation") == "observed") else 1


if __name__ == "__main__":
    sys.exit(main())
