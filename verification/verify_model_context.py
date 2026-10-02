"""Synthetic reproduction of graph evidence overflowing fixed reply context.

No live service, provider, private configuration or runtime data is accessed.
The old freezer is compiled from the pinned public 0.12.3 source commit.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = "de07bdb7437893e8009d1e74075bea8f92828ddc"


def main():
    from indeces import __version__, prompts
    from indeces.adapter import reservation
    from indeces.config import load_config
    from indeces.context import reply_messages, raw_capacity
    from indeces.contracts import GovernedError, IncomingMessage
    from indeces.memory import MemoryGraph
    from indeces.run_records import digest, freeze_retrieval, validate_graph_audit, validate_retrieval
    from indeces.scratch import canonical
    from indeces.store import Store

    old_source = subprocess.check_output(
        ["git", "show", BASELINE + ":indeces/run_records.py"], cwd=ROOT)
    legacy = {"__name__": "indeces._public_0123_baseline", "__package__": "indeces"}
    exec(compile(old_source, "public-0.12.3-run_records.py", "exec"), legacy)
    config = load_config(ROOT / "config.example.toml")
    message = IncomingMessage("synthetic-current", "channel", config.discord.guild_id,
        "synthetic-peer", "Example Bot", "请解释材料中的问题。" * 55 + " alpha beta gamma delta",
        "2026-10-02T00:00:00+00:00", author_is_bot=True)
    samples = []
    for count in (25, 100, 200):
        with TemporaryDirectory(prefix="indeces-model-context-") as directory:
            store = Store(Path(directory))
            try:
                scope = config.discord.guild_id + ":knowledge"
                graph = MemoryGraph(store.db, self_marks=(config.name,))
                facts = [{"text": f"synthetic material {i:04d}: " + "source text " * 30,
                          "quote": f"synthetic material {i:04d}: " + "source text " * 30,
                          "marks": ["alpha", "beta", "gamma", "delta"]} for i in range(count)]
                graph.add(scope, "synthetic-source", "knowledge:synthetic.md", facts, 1.0)
                audit = {}
                selected = graph.retrieve(scope, [], message.text, 2.0,
                                          event_id=message.message_id, audit=audit, ranking_mode="static")
                audit_hash, result_hash = digest(audit), digest(selected)
                audit_bytes, result_bytes = canonical(audit), canonical(selected)
                db_before = list(store.db.iterdump())
                db_hash = digest(db_before)
                before = legacy["freeze_retrieval"](store.db, scope, message.message_id,
                                                    message.text, selected, audit)
                after = freeze_retrieval(store.db, scope, message.message_id,
                                         message.text, selected, audit)
                validate_retrieval(after)
                validate_graph_audit(after["graph_audit"], after)
                assert audit_hash == digest(audit) == digest(after["graph_audit"]) == digest(before["graph_audit"])
                assert audit_bytes == canonical(audit) == canonical(after["graph_audit"]) == canonical(before["graph_audit"])
                assert result_bytes == canonical(selected)
                assert db_before == list(store.db.iterdump())
                assert before["sources"] == after["sources"]
                for old, new in zip(before["materials"], after["materials"]):
                    assert {k: v for k, v in old.items() if k != "model_payload"} == {
                        k: v for k, v in new.items() if k != "model_payload"}
                    assert all(old["model_payload"][k] == v for k, v in new["model_payload"].items())
                sample = {"active_records": count, "recalled_materials": len(selected),
                          "graph_audit_sha256": audit_hash, "selection_sha256": result_hash,
                          "memory_database_sha256": db_hash,
                          "full_graph_audit_bytes_equal": True,
                          "retrieval_results_and_database_unchanged": True,
                          "full_materials_and_sources_unchanged": before["sources"] == after["sources"],
                          "projected_values_identical_to_original_fields": True}
                for label, receipt in (("before", before), ("after", after)):
                    payload = receipt["model_materials"]
                    fixed = reservation(prompts.bot_reply_instructions(config.name),
                        reply_messages("", [], payload, message), prompts.BOT_REPLY_SCHEMA)
                    try:
                        capacity = raw_capacity(config, prompts.bot_reply_instructions(config.name),
                                                message, payload, prompts.BOT_REPLY_SCHEMA)
                        failure = None
                    except GovernedError as error:
                        capacity, failure = None, error.code
                    sample[label] = {"model_material_utf8_bytes": len(json.dumps(payload,
                        ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                        "fixed_byte_reservation": fixed, "raw_history_capacity": capacity,
                        "planning_failure": failure}
                samples.append(sample)
            finally:
                store.close()
    assert samples[-1]["before"]["planning_failure"] == "fixed_context_too_large"
    assert all(s["after"]["raw_history_capacity"] > 0 for s in samples)
    report = {"version": __version__, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_public_source_commit": BASELINE, "live_model_calls": False,
        "live_discord_connection": False, "private_runtime_data_included": False,
        "private_config_or_runtime_read_in_this_verification": False,
        "service_started_stopped_or_hotpatched": False,
        "reply_input_limit": config.adapter.budgets["reply"].input_tokens,
        "summary_reserved_utf8_bytes": config.runtime.summary_max_bytes,
        "local_memory_seconds": config.runtime.local_seconds,
        "model_concurrency": 1, "budgets_or_graph_formulas_changed": False,
        "planning_units": "Conservative UTF-8 byte reservation, not provider-measured tokens.",
        "scope": "Synthetic four-mark records, unchanged full quote, three citations, bot reply schema and zero raw history.",
        "samples": samples, "passed": True,
        "limits": "Does not prove live answers, delivery, semantic support or fit for arbitrarily large current messages/material text."}
    target = ROOT / "verification" / ("MODEL_CONTEXT_" + __version__.replace(".", "") + ".json")
    if target.exists():
        target = target.with_name(target.stem + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + target.suffix)
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"report": target.name, "passed": True, "samples": samples}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
