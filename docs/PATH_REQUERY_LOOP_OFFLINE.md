# Path → requery → path: offline contract v1.1

> Historical round-local DEV11 snapshot contract and results. The current C9
> cross-round follow-up is documented in [PATH_REQUERY_C9_FOLLOWUP.md](PATH_REQUERY_C9_FOLLOWUP.md).
> This preserved contract applies to explicit accumulate_rounds=False replay;
> its earlier A/B did not establish cross-round C9. The original snapshot remains unchanged.

## Status and scope

This is the implemented contract and offline acceptance map for the independent dev11
worktree `work/path-requery-loop-offline-dev11-20261009`, based on
`5e2612147c0623a2c2184dd57531f6dbf8fb64fd` (dev10). It is not a release,
activation, real-model result or semantic acceptance. The feature defaults off;
the authorized verification is synthetic/mock-only. Production, real API calls,
real knowledge stores/materials, private questions/answers/gold and the 19-question
evaluation are outside this work. This document does not authorize deployment.

Engine, active-loop wiring and record verification have passed synthetic controls.
The field names below describe the implemented interface; real-model and semantic
acceptance remain untested. No base NPMI/static
weight, numerical diagnostic, model, request slot, concurrency, token/cost/time
budget, ingestion or index rule is enlarged by this feature.

## Baseline and implemented flow

At the fixed dev10 base, `active_requery.py:541–551` passes `canonical_terms` as
the `marks` argument, but also embeds the complete query JSON after the original
message in the actual retrieval text. Static `memory.py:1067–1074` records the
requested marks; direct/concept hits at `1092–1110` come from literal/body matching
of the query text. Canonical terms therefore have an **indirect text-matching
effect**, not a guaranteed endpoint-ID mapping or forced direct-hit set. The
serialized query can also expose other proposed labels and JSON field names to
the text matcher. This contract does not silently change the low-level meaning
of the historical `marks` argument.

The existing path diagnostic maps proposed canonical entity strings to raw graph
labels (`path_hypotheses.py:387–418`) and builds only positive frozen static edges
(`267–303`). Missing endpoints skip the bounded search (`622–627`). Runtime's
existing diagnostic runs after active gathering and supplies no feedback to the
planner/selector (`runtime.py:296–313`). Those are baseline facts, not a claim
that the new loop is wired.

The implemented flow is: validated original action → frozen parent evidence →
bounded source-bound path feedback → host effective retrieval terms → ordinary
static retrieve/freeze/append → bounded feedback for the next existing planning
call. It creates no extra model call merely to compute a path. The active loop
still governs duplicate queries, rounds, usage and finalization. Root owns the
before/after planner and retrieval wiring; this document records its contract.

## Identity, mapping and feedback

`build_path_feedback(frozen, anchored_action, planning_call_id, config, deadline)`
uses `PathRequeryLoopConfig(enabled=False)` by default. A receipt has schema
`path_requery_feedback_v1`, policy `sourced_path_requery_loop_v1`, the parent
frozen/bundle digest, the prospective planning-call/action binding, and bounded
`mapping`, `requirements`, `candidates`, `gaps`, `clues`, `frontier_clues` and
`limits`. Existing rounds keep their original request and planning-call IDs.

- Keep the original action, its hash, original question/span anchors, qualifiers
  and baseline canonical terms. A host effective term list is a separate derived
  object with its own binding. It is never relabeled as an anchored model action
  or evidence that a term occurred in the original question.
- Resolve against the applicable scope and frozen round. Explicit aliases map a
  declared canonical literal to a graph label; `aliases={}` by default. No synonym,
  role, Unicode-equivalence or cross-scope guess supplies a missing endpoint.
  Ambiguous or absent mappings remain named unknowns, and baseline terms remain.
- Preserve baseline terms. Append at most four eligible graph labels within the
  twelve-term resource cap. An appended clue needs qualified same-round body
  support, record/source binding and evidence UID; graph cooccurrence or a catalog
  entry alone is insufficient. Do not drop baseline terms to make an expansion
  fit a cap; report the limit instead. A supported one-edge frontier clue can be
  reported while the proposed whole path remains unresolved.
- Current integration sends the complete bounded receipts as
  `path_feedback={input: pre_receipt, result: post_receipt}`, with
  `path_feedback_sha256=digest(path_feedback)`, to the next existing planner;
  the first planner receives null and its digest. The optional
  `path_feedback_summary` helper is not this actual payload. Receipt byte/graph
  caps and ordinary stage input counting both apply. This is advisory host data;
  neither a receipt nor `enabled=true` proves search execution, evidence use or
  a changed selection without the corresponding execution/binding records.
- Each edge reports its frozen edge ID, direction/applicability and same-round
  selected-material support. Do not borrow later-round bodies for an earlier
  support ID, merge unrelated scope snapshots, or fabricate cross-round paths.
  Cross-round path construction is an explicit unsupported gap in this version.

Alias/rule declarations and host effective terms must be hash-bound separately
from the immutable action and frozen input. The pre-retrieval
`path_requery_feedback` event binds the parent, action and planning call through
`demand_binding`. The native retrieval records `effective_terms` and
`actual_query_sha256`. After append, `path_requery_feedback_result` binds the pre
receipt SHA, new bundle SHA, result receipt/SHA, added evidence UIDs and those
same actual query/term fields. The query preserves the original message and
original canonical query JSON; only when terms are added does it append the
`Sourced graph terms` section. Synthetic integration verifies these bindings: a label
in a receipt cannot substitute for the actual retrieve input, model context or
delivered citations.

## Formal and resource limits

`composition_rules=()` by default. An explicit ordered declaration
`{rule_id, relation1, relation2, result}` can authorize a formal composition only
for applicable positive directed typed facts, preserving literal time/scope,
conditions and conflict checks. Reversing traversal does not reverse a relation.
Negation, unmodeled conditions, opposing applicable facts or absent composition
rules cannot yield a certain relation. A declared rule can at most produce
`formal_under_declared_rule`: `proof=false` and
`semantic_support_verified=false` remain mandatory. Formal derivation is not
proof that a source is true or that a model answer is supported.

Native `direct_hit_neighborhood_v1` evidence is not a full graph. A missing path
there remains unknown outside the inspected neighborhood. Synthetic fixtures may
declare a `path_graph_scope` with `kind=full_positive_static_graph_v1`, matching
`scope`, `complete=true`, `node_labels` and `edge_count` in the frozen selection;
only validated bounds and
complete component traversal permit a structural no-path result for that declared
snapshot. `corpus verification=false` still applies. Depth, frontier, expansion,
round or time truncation remains incomplete/unknown even when a path exists in
the fixture. Time checks are cooperative, not OS preemption.

Feedback is a bounded, auditable signal, not a second additive NPMI score.
`changes_base_weights=false` and `additive_score=false` are required. Report
actual rank/selected changes separately from the presence of a clue. Stop further
retrieval on cumulative/stage limits, unknown usage, duplicate query and no new
evidence. Retain dev10 finalization: only its eligible, nonempty, open/unhalted,
known-usage `no_new_evidence` path may attempt one normal final call under the
existing gates; other stops retain their existing policy. Never clear a halt,
reopen a closed ledger, auto-retry unknown generation or reserve an extra slot.
An engine `budget_stop` before retrieval or after append stops immediately through
the existing host-notice/shared-ledger close path; it does not run a baseline query
as a fallback. Unknown endpoint mapping can retain the ordinary baseline query.
The runtime feature guard requires active requery, offline mock mode and a callable
request override before any provider request. With the flag off there are no
feature fields, helper calls, events or new planning-schema fields.

## Synthetic v1.1 acceptance map

The upstream v1.1 designation is “16 contracts.” Its supplied named clauses are
the fifteen groups below plus the shared structural/semantic invariant. This is
a requirement map, not a fabricated sixteen-pass count or an extra evaluation
question. C2, C3 and C10 require multiple controls; report the actual fixture/test
method count separately after implementation.

| Clause | Synthetic setup and required observation | Status |
| --- | --- | --- |
| C1 | Exact mapped endpoints and an actual bounded path; bind every edge ID and selected support/UID, preserving direction. | Synthetic controls pass |
| C1b | Both endpoints map but no path is found; distinguish the inspected scope/completeness from missing mapping and record path count/reason. | Synthetic controls pass |
| C2 | An endpoint is missing: unknown with the named endpoint; baseline candidates/terms remain unchanged, with no invented label or path. | Synthetic controls pass |
| C3 | A declared alias resolves the intended label; the no-alias control stays unknown. Record declaration and effective-term hashes. | Synthetic controls pass |
| C4 | Reverse-direction evidence cannot establish the requested positive directed relation. | Synthetic controls pass |
| C5 | “Does not inhibit” cannot support “inhibits”; polarity remains explicit. | Synthetic controls pass |
| C6 | Evidence conditional on K cannot become an unconditional conclusion; preserve conditions and applicability gap. | Synthetic controls pass |
| C7 | Bibliographic cooccurrence supplies no typed semantic relation or certain claim. | Synthetic controls pass |
| C8 | Applicable positive/negative conflict cannot yield certainty; report conflicting support rather than silently choosing one. | Synthetic controls pass |
| C9 | N1→N2→N3 facts in two chunks require an explicit ordered positive composition rule; actual retrieval must retain both chunks and final context both citation bindings. This checks plumbing, not answer truth. | Synthetic controls pass |
| C9b | Without an applicable composition rule, a structural path cannot assert N1→N3. | Synthetic controls pass |
| C10 | A disconnected fixture fabricates no edge/path. A declared complete snapshot with exhaustive bounded component visit can report structural no-path; a fixture path cut off by budget/depth/round limits remains unknown. Native neighborhood absence never means full-graph absence. | Synthetic controls pass |
| C11 | High branching causes a bounded resource stop with actual usage/ledger closure recorded; unknown usage permits zero further model calls and no ledger reopening. | Synthetic controls pass |
| C12 | The same cooccurrence may supply a bounded, auditable path signal only where qualified; no double counting or base-weight change. Report actual rank/selected deltas. | Synthetic controls pass |
| C13 | Feature off gives the same frozen inputs/retrieval/model payloads as dev10 and performs no path iteration or extra calls. | Synthetic controls pass |
| Shared invariant | Across every control: structure/formal-rule result is distinct from semantic support; proof/semantic verification stay false and partial scope/limits remain visible. This is cross-cutting, not a new private question. | Synthetic controls pass |

Pure controls inspect mapping/status/reason, path count and edge IDs,
direction/conditions/support/conflict and bounded work; they do not make model
calls and have no runtime ledger. Native integration and saved A/B controls
add actual rank/selected-material deltas, mock calls/tokens/elapsed time, ledger
phase/stop reason and unknown-usage count. They bind original/effective query
inputs, appended rounds, the actual raw planner feedback SHA, model-visible
materials and final citation UIDs.
Mocks must forbid network/provider calls; any historical frozen metadata replay
can validate only retained structure/hash contracts. It cannot reconstruct missing
historical graph edges, source bodies or semantic support.

## Delivery and rollback

The actual pre-query and post-query wiring, receipt verification and synthetic
controls are implemented. Delivery artifacts bind the exact source/test identities
and preserve failures and prior experiments.
Real API/Discord quality, real PDF/body semantics and the 19-question evaluation
are untested and unauthorized here. Rollback keeps the feature off or returns the
isolated checkout to base dev10; no production config, data migration or stored
ledger rewrite is part of this offline delivery.

## Verified offline results

The three new test modules contain 38 methods: 18 pure path controls, 14 trace
verification controls and 6 native mock integration controls. Their targeted runs
passed. The full suite and package identity are recorded in the delivery manifest;
do not infer their counts from these targeted runs.

A native synthetic A/B uses identical initial frozen bytes. OFF adds no material:
gains [3, 0], final three citations, selection/query/reply. ON admits the sourced
interior term beta into the actual query, gains [3, 1], final four citations and
selection/query/query/reply. Its next planner consumes the bound input/result
feedback. An explicit synthetic links composition rule yields a two-edge formal
result with two source UIDs in the final full quote context. All semantic/proof
flags remain false. These are two synthetic native records; no claim about real
PDF layout or physical knowledge chunks follows.

An additional controlled run executes the original dev10 ActiveRequery class
from commit 5e261214 and the dev11 class with the feature off. Planning inputs,
complete HTTP-shaped mock requests, actual retrieval inputs and frozen native
records compare exactly under a fixed synthetic wall clock. This establishes
the tested off behavior, not real-model answer equivalence.

Saved Q1 metadata replays cumulative gains/citation counts and the reported
terminal ledger contract only. It lacks graph endpoint labels and frozen source
quotes, so dev11 mapping/path/selection effect remains unknown. Q2 native path
replay is not run; no current production data substitutes for missing history.

Remaining limits: paths cannot compose across retrieval rounds or combine source
versions; a complete same-round graph/source binding is required. Full-graph
absence is conditional on a consistent synthetic completeness declaration,
never a verified production-corpus absence. Local graph caps apply to bounded
steps inside the unchanged cumulative turn deadline/call/token/cost ledger;
there is no separate model allowance or automatic retry. Mock token counts are
synthetic protocol values, not actual model tokenization or paid latency.

## Final source, suite and package receipt

The isolated checkout is version 0.17.0.dev11, based on commit
5e2612147c0623a2c2184dd57531f6dbf8fb64fd, with an uncommitted reviewable patch.
Final RUN3 completed 1499 tests with 21 skips, zero failures and zero errors;
the runner verified application and test hashes remained unchanged. Its parent
process network guard saw zero external attempts and 692 synthetic loopback
connections. The three new modules contribute 38 of those test methods.

RUN1 completed with one minimal-runtime compatibility error: an existing
ledger I/O fault fixture had no config attribute. Feature detection now uses
nested optional getattr; the failed-case regression and final full suite pass.
RUN2 stopped before a result receipt; the cause and final counts are unknown.
Its incomplete log is preserved, and no matching test process remained before
RUN3 started. Earlier fixture/driver/build failures remain separate evidence.

The final offline wheel and its isolated install contain 53 application files
whose bytes all equal the checkout. Installed Console check --once with the
public example config reports Indeces 0.17.0.dev11; this read-only check makes
no service/IPC/model request. The final wheel SHA256 is
e3758bf5c81002f1407a33bf8f341b1008a0d6d1ce10ba76efed5e4c90a25e18.
Independent source review found no additional blocker and did not run tests.

DELIVERY.json, SOURCE_SNAPSHOT_MANIFEST.json, EVIDENCE_SHA256.json,
DEV11_REVIEWABLE.patch, DEV11_SOURCE_SNAPSHOT.zip, the wheel and all run
receipts reside in the parent of this checkout. The patch is checked against
an independent Git index loaded from the exact base; source ZIP hashes bind
the complete tracked snapshot plus the explicit new public files. There is
no new commit or deployment. Keep the flag disabled or do not adopt this
isolated snapshot to roll back; production dev10 remains outside this change.
