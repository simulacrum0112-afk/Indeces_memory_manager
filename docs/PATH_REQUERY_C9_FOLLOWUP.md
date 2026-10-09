# DEV11 C9 cross-round offline follow-up

This revision is `c9-cross-round-r1`, version `0.17.0.dev11`, in the independent
`work/path-requery-loop-c9-offline-20261009` worktree. Its Git base is
`5e2612147c0623a2c2184dd57531f6dbf8fb64fd`. The prior round-local DEV11 checkout,
patch, source ZIP, wheel and failure receipts remain in the parent directory.
No production configuration, launcher, database or service was changed here;
no commit, push, deployment, card operation or paid API request was made.

## Diagnosed gap and implemented contract

The prior engine searched each frozen retrieval round independently. It could
not join A in round 0 with B first acquired in round 1, and a missing endpoint
could prevent a qualified known half-edge from supplying a frontier clue.
The earlier native A/B had both chain bodies in the first round and one source,
so its additional beta-only observation did not validate the required C9 flow.

The enabled offline loop now defaults to `accumulate_rounds=True` and reports
`analysis_scope=current_task_frozen_rounds`. It first validates each original
round's selected body, graph edge, UID, source version and citation binding, then
builds a bounded union of qualified edge observations. Each observation keeps
its origin round, request, original record hash, edge ID, static weight, complete
source/fact binding and global citation ID. Later-round bodies never backfill
an earlier edge's unresolved support IDs. The bundle append chain and scope
are checked before accumulation; no current store or previous task is consulted.

Qualified known half-edges can supply frontier terms while a missing endpoint
remains explicitly unknown. Direction, polarity, conditions, time/scope and
conflicts remain in the feedback. These are exploration clues; they are not a
new fact or semantic proof. A declared ordered synthetic composition rule may
produce a formal candidate, with proof/truth/semantic verification all false.
Composition rules remain empty by default.

Current accumulated candidates are listed through `accumulated_candidate_indices`
and `accumulation_scope`. Old round-local formal observations are labelled
historical and removed from current formal status after accumulation; a later
conflict cannot leave an old local formal result looking current.

Incompatible knowledge versions are detected by exact frozen source ID and
scope/path with byte/text digests; quote span differences do not create false
version conflicts. Record-content sources are bound by source ID, record ID and
fingerprint, with file version explicitly unavailable. Missing knowledge path
identity or differing versions behind case/slash collision hints cause a
conservative unknown veto. Such hints do not assert path equivalence and do not
inspect the running filesystem. This can reject genuinely distinct files;
no filesystem equivalence contract is invented.

The existing cumulative call/input/output/cost/deadline ledger and local caps
still govern every step. A cutoff clears all partial accumulated claims and
terms, then stops. Unknown generation usage closes further calls and reopening.
There is no path-only model allowance, provider request, retry or base NPMI
weight change. Feature OFF preserves the dev10 request and receipt contract.

`accumulate_rounds=False` explicitly replays the old round-local policy. Old
saved receipt limits lacking this field are replayed as False by the verifier;
they are never silently reinterpreted under the newer default.
The original `FROZEN_SYNTHETIC.json` remains unchanged and its script selects
legacy mode explicitly.

## Actual strict synthetic C9 A/B

Only the HTTP-shaped model transport is synthetic. Native NPMI/DF, retrieval,
ranking, three-material selection, freeze/append, ledger and context are exercised.
The corpus has 20 records: DF(alpha,beta,gamma)=(4,2,3), with original weights
AB=.3059, BG=.4019 and AG=.5229. Source A is record 4; distinct source B is
record 3. Two bibliography decoys occupy IDs 6 and 5 and cannot qualify as
path support. A native tie and subsequent direct beta scoring change selection;
no scores, ranks or weights are injected.

| Stage | OFF | ON |
| --- | --- | --- |
| Initial selected bodies | 6, 5, A4 | identical 6, 5, A4 |
| Initial qualified chain support | A only; B body not frozen | A only; frontier beta from A |
| Next actual static query | original alpha/gamma | original plus sourced beta |
| Next selected bodies | 6, 5, A4 | 6, 5, B3; A absent |
| Unique material gains | [3,0] | [3,1] |
| Accumulated chain | none | A round0 + B round1 |
| Next planner consumes accumulated receipt | no feature | yes, exact pre/post digest |
| Final frozen context | three citations | four citations, both chain quotes and independent [M] |
| Generation stages | selection/query/reply | selection/query/query/reply |
| Mock generations / shaped requests | 3 / 6 | 4 / 8 |
| Terminal ledger / unknown usage | completed / 0 | completed / 0 |

The initial native graph audit already contains B's edge statistics and ranked
candidate ID. B is unselected and its body cannot support a path in that round.
The validated claim is that only A's chain-edge body support is qualified
initially, and only B's is selected in the second round. No individual round
contains both chain bodies. This establishes task-local accumulation and
source/context plumbing, not answer quality or newly discovering an edge statistic.

The frozen native replay has three cases: first-source frontier with no formal
chain; two-source/two-round accumulated chain under an explicit toy rule; and
the same split bundle under old round-local mode with no formal chain.
All three reproduce their saved stable receipts without a store/provider.
A separate strict fixture executes original dev10 ActiveRequery code versus
current feature OFF: planner inputs, full shaped requests, retrieve inputs and
frozen records all compare exactly. Static table bytes remain unchanged.

## Tests, review and remaining limits

Final offline suite: 1525 tests, 21 skips, zero failures/errors,
305.953 seconds including runner work. Application/test hashes did not
change during the run. The parent-process network guard saw zero external
attempts and 700 synthetic loopback connections. This revision adds 26
methods: 18 pure contracts, six native C9 controls and two receipt-forgery
controls; all 64 feature methods are included in the full suite. Targeted
pure36, native6 and compatibility/forgery22 also passed. Negative controls retain reverse/negative/conditions/time/scope,
conflicting facts/versions, source identity ambiguity and missing body support;
rehashed origin/UID/version/fact/mode forgery is rejected by frozen replay.

Independent review found and reproduced a real case/slash source-identity gap,
which was repaired and independently reproduced as unknown/formal0/added[].
The reviewer ran two temporary probes, not the full unittest suite or wheel
verification. Earlier C2 and native ID/tie/full-audit fixture failures and export
driver failures are preserved separately; none is relabelled as a successful run.

A final preflight check exposed a legacy 8616-byte envelope boundary regression:
False mode counted the new mode flag although its receipt omits it. The minimal
fix removes that flag from False-mode preflight accounting only; True still
counts it and no cap changed. One new test verifies acceptance, actual expired
refusal size/hash, and continued True-mode rejection. The first C9 full run
(1524 tests) and pre-fix package remain preserved; final RUN3 and PACKAGE_C9_FINAL
use the corrected source. RUN2 retains its one error: root launched from the
artifact directory, so an existing test could not read relative config.example.toml.
The same frozen source/tests/package was rerun from checkout as RUN3.
The final package verification driver initially used
an incorrect main(argv) signature; that failed attempt is retained. Its corrected
sys.argv/main() invocation passed without changing application or wheel bytes.

The new final wheel SHA256 is
`303e4a48ffc8fb093bbb924c98aaf174b21234f60e643d17a481787a9ab3eba5`.
First-party tools verified all 53 source/wheel/isolated-installed application
files byte-for-byte and the installed read-only Console check reports 0.17.0.dev11.
The external reviewer could not read the prior wheel (Access denied).
Independent external three-way wheel verification remains unestablished.
No permission was changed or denial bypassed; the prior wheel was not read/copied
in this follow-up. New-wheel external independent read was not performed.

Real models, API latency/tokenization, Discord, PDF semantics, answer quality,
Q1/Q2 dev11 effects and the 19 sealed questions remain untested. Mock counts and
local elapsed times do not substitute for these measurements. Accumulated no-path
is unknown, not complete-corpus absence. Clock cutoffs cannot be replayed
deterministically. Raw bounded receipts can still hit the existing input gate.
Frozen bytes establish local consistency, not authenticity against a writer able
to replace the entire local record.

DELIVERY_C9.json, SOURCE_C9_MANIFEST.json, EVIDENCE_C9_SHA256.json,
DEV11_C9_REVIEWABLE.patch, DEV11_C9_SOURCE_SNAPSHOT.zip and PACKAGE_C9_FINAL reside
beside this checkout. Keep the runtime feature flag False or do not adopt this
isolated revision to roll back. For explicit historical offline replay use
accumulate_rounds=False; no production rollback action is necessary.
