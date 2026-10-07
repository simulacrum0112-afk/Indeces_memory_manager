# Optional evidence selector integration candidate

This checkpoint adds a strategy boundary to the existing Runtime retrieval path, starting from clean main commit `94ae7bd15120070fb24f88d0e7bd8d4e3d622e20`. It does not enable the failed offline selectors, change the default retrieval policy, or claim an answer-quality improvement. The original workspace and earlier experiments remain untouched. Their uncommitted telemetry and offline-selection changes are not dependencies of this protocol and are excluded from this checkpoint.

## Actual current path and strategy boundary

`Runtime.__init__(..., selector=None)` forwards an optional selector to `MemoryGraph`. `Runtime.process()` still performs static retrieval followed by immediate `freeze_retrieval()` before summary/reply processing. In `MemoryGraph._selection()`, the selector receives the entire eligible ranked candidate tuple **before** the three-reference cutoff and text-preview allocation. Eligibility already reflects the existing local match/body-candidate and graph gates; this is not the entire library and not recovery of omitted historical candidates.

The default configuration uses `retrieval_policy="concept_v1"`. Its primary local ranking value is concept relevance, including the existing body/mark relevance, source prior, answer-form bonus, and metadata factor, followed by effective score, static score, and descending record ID. The `legacy_v1` branch uses direct-match count followed by effective score, static score, and descending record ID. Both keep existing literal/one-hop/context gates and graph arithmetic. With `selector=None`, the original ranked-prefix cutoff and preview allocation execute unchanged and retain the original audit and material projection contract.

The preserved failed offline experiments are separate from this runtime route and are not packaged here. A report of overlap/NPMI ranking on another machine does not identify this workspace's active policy. The three earlier historical traces still have unknown loaded commit/build. Their answer failures do not establish a selector, score coefficient, or reranker as the cause. No reranker or NPMI requery is added here.

Relevant implementation: [runtime.py](../indeces/runtime.py), [memory.py](../indeces/memory.py), [selection_policy.py](../indeces/selection_policy.py), and [run_records.py](../indeces/run_records.py).

## Injected strategy contract

A trusted, synchronous selector declares `policy_id` and `policy_version`, and implements `select(SelectionRequest) -> SelectionDecision`. Request/candidate/limit objects are frozen dataclasses and candidates, marks, and decisions use tuples. Candidates include stable record/scope/source/fingerprint identity, full stored text and quote, rank, marks, and existing scores. A decision returns an ordered tuple of selected record IDs plus a reason code for every excluded candidate. It may choose at most three records from the provided tuple. Unknown IDs, duplicates, incomplete or overlapping partitions, unsafe reason codes, and unsupported/async results fail explicitly.

Selection keeps complete individual-record quotes and source bindings. This initial boundary does not support merging, rewriting, or generating evidence. Its purpose is to make a future independently validated strategy replaceable without bypassing Runtime, freezing, or actual adapter request checks.

`None` is the unchanged product default. `RankedTopThreeSelector` is an explicit opt-in strategy useful for comparing the new contract with default material behavior. No Console or production-config activation flag is added.

## Receipts and independent verification

Default frozen material records remain retrieval version 1 with `model_projection="citation_material_v1"`. Injected selections use retrieval version 2, `model_projection="citation_material_selector_v1"`, and a top-level `selector_receipt_sha256` binding to `graph_audit.selection.selector_receipt`. The graph schema is unchanged. Injected graph selection declares `selector_contract="selector_receipt_v1"` together with `selector_receipt`; either field alone or an unknown marker fails even in graph-only replay validation. The selector receipt schema is `selector_receipt_v1` and contains:

- The actual declared policy ID and version.
- Scope/event/query identity, frozen context query and hash, ranking mode, retrieval policy, unchanged limits, and an input digest.
- An ordered inventory of every eligible ranked candidate supplied to the selector, its count, and its canonical digest. Inventory identities include source/scope/fingerprint, text and quote hashes/lengths, marks, ranks, and scores.
- The ordered selected IDs, a complete selected/excluded partition with safe exclusion reason codes, and final quote/preview/source/fingerprint bindings.

`run_records.py` independently compares the inventory with the graph's ranked candidates, validates the partition and limits, and maps actual selected IDs to frozen source/quote materials. It retains the ranked-first-three requirement for the old contract. The new version, marker, receipt, and receipt digest are coupled: missing, unknown, partially changed, or downgraded declarations fail. Graph-only validation also checks the selector receipt without requiring a model response.

The model payload shape is unchanged: each `[M]` contains its full selected quote, a separate short preview, marks, identity, and ranking metadata. Existing reply-context and HTTP input/instruction equality checks still bind this payload to the actual mocked request; answer/citation validation remains separate from semantic support. Unselected bodies are not duplicated into the receipt: their inventory hashes identify the invocation's candidates but do not establish PDF semantics or authenticity of a log whose writer can replace every version, hash, and payload.

## Resource, failure, and replay boundaries

Model, reasoning/verbosity, stage/cumulative token and time limits, the single request slot, graph parameters, and knowledge publication rules are unchanged. Selection keeps three references and the original 400-character total / 110-character per-entry preview limits. These preview limits do not truncate quotes. Existing adapter input counting and output reservation continue to enforce model budgets; the new interface does not grant extra requests or retries.

The selector is trusted in-process code, not an execution sandbox. Freezing prevents normal mutation of candidate objects; it does not make arbitrary Python code safe. Runtime's existing local deadline and SQLite progress checks remain cooperative; they do not provide operating-system preemption of a blocking selector. An invalid/throwing selector propagates to the existing failure path, with no ranked-prefix fallback and no model request. Failed selection does not create a successful retrieval receipt.

The durable event audit binds an injected event to its policy identity and input/candidate-set identity. Injected-only event headers encode a bounded, versioned selector-binding envelope in the existing `observation_status` TEXT column; default headers retain their original status strings and default replay remains a header-only lookup. This needs no schema migration. Older code may reject selector-backed synthetic headers, so the candidate must retain its isolated test state rather than reopening it as a legacy database. Reusing that event with a changed policy, input, or candidate set is rejected rather than silently replayed. Ordinary Runtime message deduplication and accepted-turn accounting remain unchanged. All tests operate on synthetic temporary stores and mocked transport, not existing user databases.

## Acceptance and rollback

The candidate's integration acceptance is a synthetic Runtime traversal through the pre-cutoff interface with more than three candidates, a deliberate non-prefix selection, unchanged full quote/source freezing, matching scratch/reply-context/HTTP materials, and independently valid receipts. Regression checks cover default behavior, failure-without-fallback, tampering/downgrade rejection, budgets, and replay/deduplication. Actual command results and precise source hashes belong in the adjacent delivery receipts; this document does not infer test success from source inspection.

Effect acceptance remains separate: the failed experiments are preserved, complete historical pools and answer support are not established, and this protocol's synthetic checks do not measure real API latency, answer quality or an online Discord run. The stub proves architectural wiring only. A real manual test must have a concrete owner-approved budget and must bind its loaded source, prompt and actual usage in its own receipt. Deployment remains a separate operation and must not overwrite unrelated services or uncommitted source.

Passing `selector=None` selects the original default branch within a newly constructed Runtime. Existing selector-backed audit events retain their contract and must not be relabeled as old records. This protocol adds no database migration, production-config write, knowledge reingestion or service control. Rolling back an installed checkpoint is separate from source checkpoint publication: retain the prior version and do not reopen selector-backed test state using older code that may reject its versioned headers.
