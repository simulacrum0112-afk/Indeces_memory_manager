# Nonempty evidence-stagnation final reply: 0.17.0.dev10

This experimental change is based on `15912cfbda6e986123efce7995ac1ef6fffb3ad5`
(`0.17.0.dev9`). It is prepared for review, not activated by the source change.
The version in `pyproject.toml` and `indeces.__version__` is `0.17.0.dev10`;
the Console uses that same imported version. Existing processes do not reload it.

## Behavior

Previously a successful query returning only previously frozen materials stopped
with `no_new_evidence` and a fixed host notice. The new policy
`no_new_evidence_final_v1` stops retrieval but permits at most one ordinary final
reply attempt when all of these conditions hold:

- The cumulative frozen evidence chain is valid and nonempty.
- The last additional retrieval is nonempty and added no unique material.
- Its per-turn ledger is open and unhalted, with complete known usage, no active
  call, and no recorded ledger I/O or ownership failure.

The original stop reason remains `no_new_evidence`; it is not relabelled as the
planner choosing `answer` or as established evidence sufficiency. All saved
quotes, source-version bindings and global citation IDs remain in the final
input. The trusted final instructions require the reply to identify insufficient
evidence and remaining gaps, rather than invent support or infer absence from a
failed search. The context also records the retrieval limitation and preserves
any prior continuity limitation. This is an instruction and byte contract, not
a proof of semantic compliance or answer quality.

`duplicate_query`, `query_round_budget`, `empty_results`, planner clarification
or stop, malformed actions and operational faults retain their existing host
stop behavior. A stopped historical run is never reopened or retried.

## Budget and failure boundary

No configured stage or cumulative limit changes. The existing query reserve
remains one call slot, 512 input tokens and 256 output tokens. It is a minimum
allowance, not a reservation for an entire final prompt, output, fee or latency.
The final attempt passes the existing call, input-count, output, upper-cost,
serial-slot and remaining-time gates. A rejected gate makes no generation
request; unknown generation usage stops further requests. There are no automatic
retries, replacement queries or budget refunds.

This narrow policy intentionally does not change every query's effective budget
to reserve the full reply stage in advance. Such reservation would need a
separately assessed allocation across selection, summary, planning and delivery
within the same total caps. In particular, reserving a full 45-second reply in a
60-second envelope could consume most of the planning allowance. The present
change permits an attempt and does not promise a substantive answer or successful
completion. Input-count and generation transports remain distinct in the audit.

## Audit

New active turns declare `requery_final_policy`. Each stop declares its policy
and `final_reply_mode`: `host_notice`, `ordinary_answer`, or
`no_new_evidence_consolidation`. Final context, generation, delivery and terminal
metadata bind that mode to the preserved `retrieval_stop_reason`.

`final_status` distinguishes model reply delivery, host notice delivery, model
skip, failed final admission/generation, cancellation and unknown delivery. An
actual model reply keeps `final_origin=model`; a host notice keeps
`final_origin=bounded_stop`. A completed ledger says nothing about semantic
sufficiency. Existing failure codes, usage, reservation and delivery evidence
remain authoritative.

The offline verifier reconstructs the final append and checks the nonempty
stagnation trigger, open known-usage ledger, policy and final-input bindings,
and at most one reply attempt. It rejects marker downgrades, incompatible modes
and new queries after the stop. Retained suffixes check the remaining metadata
consistency and remain explicitly `retention_partial` when trigger evidence has
expired. Unmarked historical host stops and ordinary planner-answer records
keep their original interpretation; a newly unmarked non-answer stop cannot
authorize a model reply.

## Validation and activation

Synthetic offline regressions cover both final delivery and insufficient-evidence
replies, preserved frozen citations, host-only stop branches, closed/unknown/I/O
states, final admission failures, unknown final usage, delivery uncertainty and
policy/metadata tampering. No question IDs, target documents, private questions,
answers or gold are fixtures. Exact commands and final results are recorded in
the [offline results](../verification/no-new-evidence-final/RESULTS.json),
[source manifest](../verification/no-new-evidence-final/SOURCE_MANIFEST.json) and
[independent review](../verification/no-new-evidence-final/REVIEW.json) after
verification; test success is not a real API/Discord
latency or semantic acceptance result.

The prepared wheel is installed into a separate target and checked against
source bytes and wheel RECORD. Activation waits for review and coordination with
an idle test line. No service stop/start, live configuration change, ingestion,
database migration, history reset, paid retest or main-branch change is part of
this implementation task.

Rollback selects the preserved dev9 package through a separately coordinated
normal Console switch. Keep the existing configuration, all historical ledgers
and audit records; do not replay a stopped run. An older verifier does not know
the new policy, so preserve the dev10 verifier for new records when rolling back.
