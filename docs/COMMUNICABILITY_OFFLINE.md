# Offline communicability checkpoint

This independent feature is disabled by default. It adds diagnostics after an
existing frozen retrieval; its scores are not placed in reply context and do
not change NPMI, graph publication, selector decisions, selected materials or
model parameters. It consumes local time from the shared turn deadline and
appends a diagnostic event; timeout or scratch-write failure can affect turn
completion. Thus unchanged answer content or delivery is not guaranteed.
No production state,
historical question/answer, fixed evidence or gold was used to develop it.

## Graph semantics and provenance

`memory.py` builds canonical unordered mark pairs with
`combinations(sorted(marks), 2)`, then stores positive rounded NPMI weights.
`memory_index.py` gives symmetric NPMI two addressable directed coordinates.
These coordinates are not causal, temporal, negated or typed semantic edges.

`analyze_frozen_graph` reads only existing `graph_audit.selection.edge_statistics`
(`a`, `b`, `static_score`, `source_record_ids`) and direct-hit labels. Each
positive association produces two sparse arcs. The original audit and weights
remain unchanged. Edge identity is a digest of copied edge metadata; supporting
record IDs are preserved. Missing support IDs, conflicting frozen edge versions,
cross-scope bundles and non-static audits are explicit. No original text is
fetched. A frozen neighborhood remains a neighborhood; this layer cannot claim
communicability on the omitted full graph.

The adapter also accepts the actual `active_requery_evidence_v1`, version 1
dictionary and projects each bounded `rounds[].record.graph_audit`. It checks
round order, unique request identities, planning-identity shape, declared hash shape, record and
graph scope, and metadata shape before combining same-scope frozen edges. Its
receipt preserves round/request/call/hash metadata. It does not read query text,
material bodies or citation bindings, and does not replay full-bundle hash or
semantic validation; that binding remains the upstream Runtime's responsibility.
`bundle_binding_validation=upstream_only_not_replayed` makes this boundary explicit.
Native graph scope is `graph_audit.scope`; native mode is
`graph_audit.selection.ranking_mode`. Explicit legacy/synthetic request metadata
is accepted only when no native value exists, and contradictory values reject.
Unmarked or dynamic audits are rejected instead of silently assumed static.

The association adapter always reports `insufficient_relation_semantics` and
`pending_unverified`, even when its numerical calculation converges. It cannot
manufacture a typed relation from a cooccurrence edge.

## Numerical operator and experimental parameters

The implementation independently follows the Krylov projection principle in
[Roger B. Sidje, Expokit, ACM TOMS 24(1), 1998](https://www.maths.uq.edu.au/expokit/paper.pdf):
Arnoldi obtains a small Hessenberg projection, and the projected exponential
action is lifted to the original vector space. This implementation uses two
modified Gram-Schmidt passes and a bounded Taylor **action** on the projection;
it is not the Expokit implementation or its adaptive restart algorithm.

| Parameter | Original → experimental value | Scope |
|---|---|---|
| Base NPMI weights / storage / selector | unchanged → unchanged | Existing behavior |
| Feature enabled | absent → `False` | Independent, zero-work default |
| Operator | existing graph unchanged → copied sparse association adjacency | Additive diagnostic |
| Arc orientation | no causal meaning → `A[dst,src] = weight` | Explicit numerical convention |
| Scale | no previous operator → `1.0` | Counts factorial-weighted walks of copied graph |
| Normalization | no previous operator → `none` | Raw copied weights by default |
| Optional normalization | absent → `max_row_sum` | Divide operator scale by `max(1,row norm)`; explicitly changes experiment semantics, not base weights |
| Tolerance / near-breakdown | absent → `1e-10` / `1e-13` | Absolute error criterion / diagnostic threshold; small nonzero residual alone never accepts |
| Krylov / sparse matvecs | absent → 32 / 32 | Local resource caps |
| Nodes / arcs | absent → 4096 / 16384 | Local resource caps |
| Active evidence bundle rounds | absent → 16 | Metadata projection cap, independent of upstream model-call admission |
| Evidence IDs per edge / total | absent → 1024 / 32768 | Copy/provenance resource caps; reject overflow without partial provenance |
| Stored vector elements / projected entries | absent → 262144 / 4096 | Local resource caps |
| Projected Taylor terms / wall time | absent → 96 / 0.5 seconds | Cooperative local stops |

No dense graph `A`, `exp(A)` or matrix inverse is formed. Storage consists of
sparse arcs, up to O(nm) vector elements and O(m²) projection entries. The
reserved basis/work-vector count and actual projection dimensions are recorded.
Time checks are cooperative, not operating-system preemption. Overflow, invalid
input, exhausted projection terms, node/arc/storage/time limits and Krylov
nonconvergence stop explicitly. No dense fallback or hidden retry occurs.

The audit includes Arnoldi residual, endpoint defect estimate, successive
projection difference, orthogonality error, projected Taylor tail and a
conservative polynomial-tail bound in **exact arithmetic**. Only a computed zero
Arnoldi residual with a sufficiently small projected-action error, or the
beta-aware sum of polynomial and projected-action bounds, marks observed convergence.
Small nonzero residuals continue or remain unknown; input-vector scaling cannot
bypass the absolute tolerance. Simultaneous H and copied projected-H storage
counts toward the projection-entry cap; the old projection is released before
the next copy. Input, lifting and orthogonality loops check the cooperative
deadline, and every accepted return checks it again.
Neither defect nor successive difference is a certified error estimate. The
reported total certified error bound remains `None`: rounding, approximate
invariance and orthogonalization error are not certified. Budget/nonconverged
approximations are labeled unaccepted; they must not drive selection.

## Typed path meeting

`TypedEdge` is a separate synthetic/expert-metadata interface containing source,
target, relation, evidence IDs, polarity, time interval and scope.
`PathRequirement` constrains an individual directed edge. The demand path's last
target must equal the data path's first source. The combined chain must be
connected in the supplied directions; each edge must have declared provenance.
Checks preserve negation, relation, direction, time and scope, detect opposing
polarities for the same relation in overlapping scope/time, and reject unmet
requirements or missing metadata. The whole chain also requires a common known
scope and overlapping known time interval; unknown scope/time or conflicts stay
insufficient even without explicit requirements. Invalid metadata is rejected
before comparisons and omitted from the JSON-safe receipt. All constraints for one requirement must be
met by the same edge.

Success is only `pending_hypothesis`, with `proof=False` and
`source_truth_verified=False`. Evidence IDs establish declared traceability;
this module does not resolve those IDs or verify source truth, entity identity,
logical implication or model interpretation. It does not automatically compose
edge relations into a novel relation.

## Running and rollback

The second local checkpoint uses `0.17.0.dev2` and the independent Runtime flag
`runtime.communicability_enabled=false`. When explicitly enabled in an isolated
configuration, the ordinary Console Runtime diagnoses the frozen retrieval, or
the completed active-query bundle, just before final generation. It writes one
`communicability_diagnostic` event bound to that evidence digest. Scores are not
inserted into model context or used to choose materials. Numeric or metadata
errors produce an unknown receipt; scratch persistence errors still follow the
existing Runtime audit-failure contract. The local 0.5-second cooperative budget
is narrowed by the remaining turn deadline.

```python
from indeces.communicability import CommunicabilityConfig, analyze_frozen_graph

receipt = analyze_frozen_graph(
    frozen_retrieval,
    config=CommunicabilityConfig(enabled=True),
)
```

Run synthetic regressions from the isolated checkout:

```text
D:\Indeces\.venv\Scripts\python.exe -B -X utf8 -m unittest tests.test_communicability -v
```

Coverage includes two-node hyperbolic-function and directed nilpotent analytic
oracles, a 512-node sparse chain with independent factorial-series oracle,
resource stops, invalid data, nonconvergence, zero-work disabled mode,
normalization receipts, immutable original weights, frozen edge provenance,
duplicate/conflicting/scope fixtures, and typed path direction/negation/time/
scope/conflict/missing-source checks. These tests prove synthetic wiring and
specific numeric fixtures, not real-model benefit or semantic proof.

Rollback is to leave `CommunicabilityConfig.enabled=False` and
`runtime.communicability_enabled=false`, or return to checkpoint one. No database schema,
weight migration, dependency installation, API call or production switch is
required. Known unknowns: certified floating-point error, full-graph behavior
beyond the frozen neighborhood, real relation semantics, end-to-end latency and
real-model usefulness remain unvalidated.

## Review failures and fixes

Review reproduced two pre-checkpoint failures: a nonzero `1e-14` Arnoldi residual
was accepted even when vector scaling made the missing component `1.0`; an
invalid `{}` time interval raised `KeyError` during repeated-edge comparison.
Both are now covered by synthetic regressions. A separate triangular operator
shows why even beta-scaled residual alone is inadequate: later `exp(50)` growth
can amplify tiny coupling. That fixture stops unaccepted at the default projected
term budget; an explicitly larger test-only budget matches its analytic oracle.
Scope/time conflict and deadline/storage accounting fixes are also tested. None
of these synthetic outcomes certify general floating-point error or model gains.
Final review also found Python integers outside the finite time domain could
overflow `math.isfinite`; edge and requirement checks now safely reject them,
with separate synthetic regressions.
The same safe finite-number check covers configuration, deadlines, vector/arc
inputs and frozen weights, so out-of-range integers cannot escape those public
entry points as conversion overflow.
The first Console integration run also exposed a synthetic/native schema
mismatch: the adapter looked for scope and mode under `request`, rejecting real
active bundles. Native top-level scope and selection mode are now authoritative;
fresh Store/MemoryGraph-generated two-round bundles cover this path, including
dynamic, missing/contradictory mode and cross-scope rejection. Earlier failed
integration evidence remains distinct from subsequent passing runs.
