"""Deterministic public notices; technical reasons stay in the run record."""
from __future__ import annotations

NOTICE_POLICY = "natural_notice_v1"
_PROCESSING_NOTICE = "这次处理暂时未能完成，我还无法给出可靠答复。"

_INPUT_LIMITS = frozenset({
    "input_token_limit", "selection_input_budget", "fixed_context_too_large",
    "summary_prefix_too_large", "context_backlog_requires_next_turn",
})
_RESOURCE_LIMITS = frozenset({
    "query_round_budget", "requery_call_budget", "requery_token_budget",
    "requery_cost_budget",
})
_TIME_LIMITS = frozenset({
    "stage_timeout", "model_slot_timeout", "turn_timeout", "local_memory_timeout",
    "requery_local_timeout", "requery_time_budget",
})
_NO_ADDITION = frozenset({"empty_results", "no_new_evidence", "duplicate_query"})


def user_notice(reason, *, clarification=None):
    """Describe only the known outcome, without interpolating a code or trace.

    A planner stop does not establish absence of evidence or confirm a user's
    stopping intent. Incomplete provider output does not identify its cause.
    Unknown/dynamic reasons therefore receive a neutral processing notice.
    """
    if not isinstance(reason, str):
        return _PROCESSING_NOTICE
    if reason == "ambiguity":
        if isinstance(clarification, str) and clarification.strip():
            return "想确认一下：" + clarification
        return "这个问题的指向还不够明确，可以补充具体对象或范围吗？"
    if reason == "model_stop":
        return "我目前还不能依据现有材料可靠确认这个问题。"
    if reason in _NO_ADDITION:
        return "这轮没有获得更多可核验材料，目前还不能进一步确认。"
    if reason in _INPUT_LIMITS:
        return "这轮需要核对的内容超出了当前处理范围，暂时未能完成核验。可以把问题缩小到一个具体点。"
    if reason in _RESOURCE_LIMITS:
        return "这轮核验已达到处理额度，暂时还未能完成确认。可以把问题缩小到一个具体点。"
    if reason in _TIME_LIMITS:
        return "这次处理耗时较长，未能在本轮完成。我暂时无法给出可靠答复。"
    if reason == "incomplete_response":
        return "这次没有收到完整结果，我暂时还无法给出可靠答复。"
    if reason == "cancelled":
        return "这轮核验已暂停，目前没有新的结论。"
    return _PROCESSING_NOTICE
