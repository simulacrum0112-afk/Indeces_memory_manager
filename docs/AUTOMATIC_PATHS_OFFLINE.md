# Automatic frozen path hypotheses: offline checkpoint

This independent feature defaults off and performs zero input iteration when
disabled. It makes no model/network/database calls, changes no NPMI weight or
selector, and does not feed path scores or hypotheses into reply context. When
wired, it consumes shared local turn time and writes an audit event; it cannot
guarantee unchanged delivery if a deadline or audit write fails.

`analyze_path_hypotheses(frozen, query_actions, config=PathHypothesesConfig(...),
deadline=...)` accepts a native frozen retrieval or version-1
`active_requery_evidence_v1` bundle. No caller-provided paths or TypedEdges are
accepted. The query-action wrappers contain `evidence_round_index`,
`planning_call_id`, the complete validated `action`, `action_sha256`, and
`anchors_validated=True`. Runtime supplies only actions already parsed and
successfully appended. Action hashes are checked here. Bundles independently
bind the action to the same-round planning call; flat retrievals retain the
explicit `declared_upstream_query_call` declaration because their native records
contain no planning-call identity. Original character-span validation remains an
explicitly recorded upstream responsibility, not semantic validation.

The module automatically builds the positive static NPMI adjacency of each
action's frozen round. It runs deterministic bounded BFS from both proposed
endpoints, combines simple paths at common nodes, and records automatic meetings.
The graph is undirected association; traversal direction is not a causal edge.
For n frozen nodes, sparse arcs e and expansion cap E, search work is bounded by
E and stored frontier/path caps rather than all possible graph paths. Multiple
relations/actions receive separate outcomes. Top-level `pending_hypothesis`
requires every declared relation demand to have a matching pending candidate;
an empty or unresolved demand cannot be hidden by a successful one.

## Explicit quote grammar and provenance

Only a complete single-line marker at the beginning of a quote line is parsed:

```text
typed_facts_v1: {"facts":[{"subject":"alpha","object":"beta","relation":"links","polarity":"positive"}]}
```

Facts require subject, object, relation and positive/negative polarity. Optional
`time` and `scope` are literal strings; optional `conditions` is a list of literal
strings. JSON must be valid and must not contain duplicate keys or extra fields.
Decoded strings must contain Unicode scalar values: lone high or low surrogates,
including JSON-escaped surrogates, yield `invalid_typed_facts` and unknown facts.
Valid Chinese, emoji and correctly paired JSON escapes are preserved. The frozen
quote is retained; invalid decoded text is never copied into the UTF-8 receipt.
Fact endpoints must exactly match the graph nodes and the supporting selected
material's marks. Ordinary cooccurrence prose supplies no typed relation.

Every declared edge-support ID is resolved only against that round's selected
frozen material. Missing IDs remain named unresolved IDs; later rounds and the
global catalog never supply their bodies. A source-binding receipt exists even
for ordinary prose, containing source ID/version kind, record fingerprint,
quote/text hashes, local/global citation and evidence UID. Typed fact bindings
also contain the exact quote-line character span and hash. Native material/model
arrays and citation markers are checked. Bundle UID/citation maps are verified
against the same-round material; global entries establish citation identity only.
After bounded component/field-count preflight, bundle input is independently
replayed through `validate_bundle`. This checks the complete immutable append
chain, canonical first-seen M1..Mn numbering and global uniqueness, including
simultaneous catalog/binding tampering. Deadline checks surround the replay;
they do not preempt an individual validation operation.

Published knowledge metadata requires ready/published source identity, normalized
raw-text hash, original-byte digest shape and quote occurrence in that frozen
raw body. Original file bytes/PDF semantics are not verified. Native non-knowledge
sources without file metadata instead use an explicitly named
`record_content_version` (source ID plus frozen content fingerprint/hashes);
this does not assert a knowledge document version. Missing required metadata or
any failed binding stays unknown. No file is fetched to complete a binding.
The positive tests execute the native `record_content_version` path. Knowledge
version metadata is checked in code, but real knowledge versions, PDF original
bytes and PDF semantics have not been exercised or accepted in this checkpoint.

## Applicability and limits

Canonical entity labels and predicate labels remain unverified proposals.
Time/scope anchors preserve their exact surface and spans. Alignment compares
literal strings only, without interpreting a year, converting scope to a database
scope, normalizing synonyms or guessing entity roles. Independent negation anchors
have no declared relation scope and therefore remain unknown. Unmodeled fact
conditions and missing qualifiers also remain unknown. Explicit applicable
direction/polarity or literal qualifier conflicts reject an alignment; this does
not prove the underlying statement false. Unrelated predicates, reverse claims
and explicitly different time/scope claims do not invalidate an applicable
matching claim. Applicability-unknown claims cannot be silently excluded.

A single relation demand is never satisfied by a multi-hop path, even when all
edges have the same predicate. Those structural candidates are recorded unknown
with `relation_composition_not_authorized`. There is no automatic relation
composition rule. Success is at most `pending_hypothesis`; `proof`, truth,
semantic support and proposal verification all remain false.

Default caps: 256 cumulative frozen nodes, 1024 cumulative arcs, depth 4,
frontier 256, candidates 64, expansions 4096, copied bindings 1024, rounds 8,
actions 8, facts per material 16, support IDs per edge 128, quote/text 16384
characters, frozen source 262144 characters, and 1048576 serialized bytes per
individual hash/record check, with 0.25 seconds. The byte cap is not an aggregate
whole-bundle byte limit; the separate round cap bounds the number of records.
Config values are separate experimental resource controls;
base graph weights, production budgets and numerical-layer parameters are unchanged.
Stops are cooperative, not operating-system wall-clock preemption. A bounded
iterative preflight checks JSON structure, depth, cycles, keys, strings and
escaped/UTF-8 byte sizes before encoding/hash allocation. Binding loops check
deadlines and caps before copying. An interrupted candidate is not appended as
complete. Frozen neighborhood/depth/resource limits are reported as incomplete;
results never claim exhaustive full-graph paths.

## Verification and rollback

`python -B -X utf8 -m unittest tests.test_path_hypotheses -v` uses freshly written
synthetic Store/MemoryGraph/freeze/append inputs and actual `parse_action`; it
does not reuse historical questions, answers, evidence or gold. It covers automatic
positive provenance and meetings, multi-round UID maps, ordinary prose, named
unresolved support, later-round non-backfill, multi-hop refusal, independent
negation, literal applicability, partial demands, model-material tampering,
resource stops and encoding preflight. Network/model calls are absent. Synthetic
wiring is not real-model utility or semantic correctness validation.

The first unit run's resource-cap classification failure is retained separately:
an oversized structural container returned generic unknown instead of budget
stop. The classification was corrected; the original JSON/log is not overwritten.
Other diagnosed review risks and their regressions include partial-demand hiding,
unrelated facts overriding matches, applicability-unknown opposing claims,
missing model-material/marker binding and encoding allocation before size checks.

Rollback leaves the independent flag off or returns to the prior checkpoint.
No schema migration, source rewrite, index change, model call or production
switch is required. Remaining unknowns include real relation/role equivalence,
source truth, unrestricted conditions, relation-composition rules, actual-model
benefit and broader graphs outside frozen neighborhoods.
