# Bounded active graph-query feasibility snapshot

This is an independent local implementation snapshot based on published
`0.16.1` (`152503bf99b2fb092257b703b53f52470dc2fcd3`). The first code checkpoint
uses `0.17.0.dev1`; it is an experimental version, not a production release.
The existing production installation remains `0.16.1`. No existing process,
configuration, knowledge database, model budget, or publication is changed by
creating this snapshot. Exact local checkpoint identities and validation results
belong to the accompanying delivery receipt, rather than being inferred from a
version string.

The ordinary Console Runtime can optionally ask the existing single model for a
bounded structured action before answering. A `query` action executes the
existing local static `MemoryGraph.retrieve` path, freezes its selected source
material, and returns the cumulative evidence to the next action call. An
`answer` action permits the ordinary final reply. `clarify` and `stop` produce a
bounded host response. This planner is a separate `query` stage; it is not the
post-retrieval `ModelNPMILabelSelector`, and its native graph-query rounds do not
call that selector. If initial model selection is enabled, its existing call is
included in the same per-turn cumulative ledger.

The graph continues to use the existing static NPMI score and context/one-hop
rules. Queries read those weights and do not change the graph formula or
reinforce the graph. A selected association edge is not a semantic or causal
relationship. This work does not implement dense communicability, ingestion,
automatic keyword labelling, a new service, or an external search tool.

## Defaults and admission

The configuration loader accepts the following optional `[runtime]` fields.
All live activation flags are off by default; an old configuration does not
enable this feature.

| Field | Default | Purpose |
| --- | --- | --- |
| `active_requery_enabled` | `false` | Enable the additional Runtime action loop |
| `active_requery_max_rounds` | `2` | Maximum additional native graph-query rounds |
| `active_requery_max_calls` | `6` | Cumulative model calls, including initial selection, summary, query, and reply |
| `active_requery_input_tokens` | `32768` | Cumulative known input allowance, with outstanding reservations |
| `active_requery_output_tokens` | `4096` | Cumulative known output allowance, with outstanding reservations |
| `active_requery_seconds` | `60.0` | Total active turn envelope, including delivery; narrowed by the existing turn deadline |
| `active_requery_cost_usd` | `0.0` | Configured cumulative cost cap for an explicitly permitted real run |
| `active_requery_allow_real_calls` | `false` | Additional admission gate before any real provider intent |
| `active_requery_input_usd_per_million` | `0.0` | Explicit conservative input unit price for real admission |
| `active_requery_output_usd_per_million` | `0.0` | Explicit conservative output unit price for real admission |

These cumulative limits narrow existing stage budgets; they do not borrow or
increase them. If no explicit `query` stage exists, its budget is derived from
the reply stage with ceilings of 4096 input tokens, 512 output tokens, and 15
seconds, retaining the configured reply reasoning effort. The original model,
serial request slot, reply/summary/label budgets, and model-selection admission
remain in force. A planner call reserves a remaining reply slot and a minimum
reply allowance; the final reply still must pass its complete input gate.

A real adapter is rejected unless the real-call flag, positive cost cap, and
both positive unit prices are set before the first provider intent. These are
technical gates, not permission to spend: this offline task authorizes no real
model call. Configured unit-price estimates are not an invoice. The runnable
smoke instead sets `offline_mock=True` on an adapter with an explicit in-memory
request override. Its zero synthetic cost makes no claim about actual API cost.

The outer Runtime timeout includes retrieval, planning, summary, final generation,
and delivery. Database progress callbacks and numerical/local checks remain
cooperative checks; they are not operating-system hard preemption. No claim is
made about real API latency or arbitrary database scale.

## Action and evidence contract

An action contains only `action`, `query`, `missing_evidence`, `clarification`,
and `stop_reason`. A query includes bounded entities, relations, negation
anchors, explicit time/scope anchors or `null`, canonical terms, and ambiguity
declarations. Every declared entity, relation, negation, time, and scope is
checked against an exact character span of the original question. Relations
retain subject/object identity, predicate, direction, and negation. The original
question and all declared qualifiers also remain in the graph-query audit input.
The schema rejects undeclared fields and duplicate JSON keys. No private thought
trace or reasoning transcript is requested or stored.

Span and schema checks establish syntax and literal anchoring. They do not prove
that a proposed canonical term is semantically equivalent, or that every
important qualifier was identified by a model. An action declaring ambiguity
must clarify instead of issuing a query. Canonical repetition checks normalize
case, Unicode, and ordering for the stored structured query identity; they are
not a universal semantic-equivalence detector.

The initial retrieval is round 0. Each additional native retrieval gets a
distinct request ID and its action's planning-call ID. Its native three-material
contract, complete frozen source text, quote, source/version digests, graph
audit, and local citation IDs remain intact. The cumulative evidence bundle is
pure append: earlier rounds and citations are never replaced by later results.
Stable material identities include scope, record/source identity, source-version
digests, fingerprint, and text/quote digests. Repeated material reuses its first
global citation; a changed frozen version remains distinct. Global `[M1]`,
`[M2]`, and later markers map back to their original local citation and round.
The complete quotes and stable identities are inserted into the actual final
model input. Hash and citation verification establishes local byte consistency
and literal bindings, not relevance, semantic entailment, or truth.

Empty retrievals, repeated structured queries, queries adding no new evidence,
round/call/token/time/cost limits, invalid actions, and local errors stop the
bounded loop. Earlier frozen evidence is retained. Budget/error stops use a
fixed host notice without an additional generation; clarification uses the
bounded structured question. Bot host notices retain the existing failure-notice
delivery contract and do not create a model-driven continuation.

Before admission and provider intent, a metadata-only JSON sidecar is written
under the isolated state's `active_requery_ledger/` directory by atomic replace.
It records call identities, stage reservations, transport intents, actual known
usage, unresolved usage, cumulative limits, and terminal status; it contains no
question, source material, or generated answer. Known usage is persisted before
later parsing/validation can fail. Unknown generation usage is sticky and keeps
its reservation. A recorded message cannot be reopened to recover allowance.
Failed calls are not refunded and there is no automatic retry. Normal stops,
admission failures, delivery failures, and unknown-usage halts remain visible in
the ledger and run records.

## Reproduce without external side effects

Run this from the isolated checkout with its available Python dependencies:

```powershell
D:\Indeces\.venv\Scripts\python.exe -X utf8 -B verification\mock_active_requery.py --output D:\Indeces\build\active-requery-feasibility-20261008\MOCK_LINE_ONE.json
```

`--package-root <directory-containing-indeces>` selects an isolated installed
wheel ahead of the checkout and checks the loaded package path. The script does
not import repository tests. It uses `indeces.console.create_runtime`, fresh
temporary SQLite/Scratch state, and a synthetic graph with two independent
co-occurrence pairs. Socket and real aiohttp session creation are blocked.
It does not load configuration, credentials, existing state, old questions,
answers, gold, or user material, and starts no Console menu, Gateway, or service.
The temporary adapter, ScratchLog, and Store are closed before cleanup.

The scripted planner requests two graph queries, then chooses `answer`. Only
the model transport and delivery are synthetic; both queries run the actual
local static graph retrieval. Initial model selection is disabled for this
demonstration. The output reports query/answer stage order, round/request IDs,
cumulative `[M1]`/`[M2]` identities, synthetic usage, unchanged static weights,
the final-input evidence binding, and run-record verification. It omits source,
question, and answer text. This smoke demonstrates wiring and record consistency,
not real relevance, user-answer quality, model canonicalization, API reliability,
or latency improvement.

The offline regression harness is `verification/run_active_requery_offline.py`.
It lists its exact module selection and writes source identities and actual
results to an explicitly requested local output. Retain unsuccessful receipts
beside successful reruns. A long raw history may still be rejected by the
unchanged initial selector's 4096-byte admission before summary runs. Summary
execution was initially tested separately with initial selection disabled.
The revision adds a short synthetic history and narrower test-only reply
input budget, allowing one fixture to execute initial selection, summary,
two native queries and final reply with the default `concept_v1` policy.
Production defaults and the selector admission are unchanged. The fixture
checks one five-call cumulative ledger and stable local-to-global citations;
it does not establish real latency or semantic correctness.

All finished in-memory budget objects now reject further admission, including
successful `completed` objects. Repeated `finish`, late `halt` and late
`reject` preserve the original durable terminal bytes and first stop cause.
Unknown usage retains its reservation and cannot buy a retry or refund.

## Rollback and remaining validation

Set `active_requery_enabled=false` in an independently authorized configuration
to retain the established reply path and its legacy record verification. The
new code is confined to the isolated feasibility branch/worktree; reverting
that local checkout to the base commit above removes the experiment. Keep all
existing user changes, original experiments, failed receipts, and state intact.
Do not use a hard reset in the original dirty workspace.

No real model, Discord, historical-candidate replay, private/public gold, or
production deployment has been evaluated here. The feature requires separate
authorization and a bounded evaluation plan before real spending or activation.
The original initial-selector and summary boundaries are preserved. Local
hashes and simulation success cannot be described as deployment, a loaded
production version, a semantic proof, or improved real answer quality.

The first checkpoint's selected regression command ran 203 tests with zero
failures, errors, or skips. `verification/ACTIVE_REQUERY_OFFLINE.json` binds
that result to the tested application source hashes. The two unsuccessful
regression receipts and their full logs remain beside the checkout; their
identities and fixes are recorded in `ACTIVE_REQUERY_FAILURE_HISTORY.json`.
This is a local result; no remote CI or real-model evaluation was run.
