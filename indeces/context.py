"""100%/70% hysteresis of remaining raw-history capacity, whole turns only."""
from __future__ import annotations

import json

from .adapter import reservation
from .contracts import GovernedError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def groups(rows):
    result = []
    for row in rows:
        if not result or result[-1][-1]["turn_id"] != row["turn_id"]:
            result.append([])
        result[-1].append(row)
    return result


def history_data(rows):
    return [{"source_seq": r["seq"], "message_id": r["turn_id"], "role": r["role"],
             "author_id": r["author_id"], "text": r["content"]} for r in rows]


def history_cost(rows):
    return len(encode(history_data(rows)).encode("utf-8")) + 64 * len(rows)


def compaction_prefix(rows, capacity, low_watermark):
    if history_cost(rows) <= capacity:
        return []
    interactions = groups(rows)
    prefix = []
    while interactions and history_cost([r for g in interactions for r in g]) > int(capacity * low_watermark):
        prefix.extend(interactions.pop(0))
    return prefix


def reply_messages(summary, rows, knowledge, current):
    # Separate metadata/data from current instruction, without promoting history
    # belonging to another author to the current user role.
    data = {"continuity_summary": summary, "recent_observations": history_data(rows),
            "memory_citations": knowledge}
    return [{"role": "user", "content": "Historical context data:\n" + encode(data)},
            {"role": "user", "content": encode({"current_message_id": current.message_id,
                "current_author_id": current.author_id, "current_author_name": current.author_name,
                "created_at": current.created_at, "text": current.text})}]


def raw_capacity(config, instructions, current, knowledge):
    # Output is already separately reserved by the adaptor's context validation.
    # Reserve a full bounded checkpoint, so maintenance cannot shrink the window
    # unexpectedly when its result replaces the previous summary.
    fixed = reply_messages("", [], knowledge, current)
    capacity = config.adapter.budgets["reply"].input_tokens - reservation(instructions, fixed) - config.runtime.summary_max_bytes
    if capacity <= 0:
        raise GovernedError("fixed_context_too_large")
    return capacity
