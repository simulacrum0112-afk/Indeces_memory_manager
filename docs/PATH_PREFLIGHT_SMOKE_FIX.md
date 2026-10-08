# Offline diagnosis of remaining live-smoke outcomes

`0.17.0.dev6` optimizes bounded path preflight without changing any resource cap
or activating a new live service. The existing visible Console remains the
verified `0.17.0.dev5` package; its original source identities, switches and failed
or stopped smoke records are retained.

## Model stop is the established safe contract

In the synthetic alpha/beta case, the planner explicitly reported missing
relationship evidence and a directed evidence path. It chose `stop` with an empty
`stop_reason`, which the current schema permits. `stop` produces a fixed host
notice and skips final model generation; `answer` is the distinct action entering
final model generation. The stop does not establish that the relationship or
path is absent, and it does not create a global unknown-usage halt. Regression
tests confirm a later new message can still follow query and reply normally.

No prompt, action schema or stop-to-host control flow changes. An expanded reason
taxonomy would require a separate versioned protocol and compatibility for
historical prompt/schema verification. This fix does not force insufficient
evidence into an answer or echo model-provided missing-evidence strings.

## Path timing and redundant work

The path clock starts immediately inside the diagnostic. Runtime supplies a new
deadline of the earlier turn deadline or the current time plus 0.25 seconds.
Earlier model network waits are not charged to that 0.25-second window.

The measured frozen input had two native records of 173544 and 483700 canonical
UTF-8 bytes, three cumulative materials and one logical query action. Preflight
made eight canonical hash passes, then ran the complete append validator. In
three bounded baseline offline runs, character scanning and JSON hashing took
about 0.222–0.227 seconds before complete validation; the time check after that
validation stopped before graph construction or search. The original real
receipt recorded 0.281 seconds. These are cooperative checks, not hard preemption.

Only the Python per-character UTF-8/escaping preflight loop changes. It now scans
strict UTF-8 in 256-character blocks, preserving the original conservative byte
count, field lengths, structure/depth/cycle checks and deadline check frequency.
Malformed Unicode uses the old scalar loop to preserve rejection ordering.
Canonical encoding and hashes, full bundle validation, graph construction,
search, source/citation bindings and the 0.25-second cap remain unchanged.

Five offline runs on the same frozen input completed in approximately
0.122–0.125 seconds under the original cap. The result remained `unknown`: the
proposed endpoints were not present in the frozen graph, with zero path
expansions. Every receipt field except elapsed time matched the old algorithm's
longer-budget offline diagnostic reference. That reference was a measurement
only; no live or default budget was raised. These measurements do not guarantee
arbitrary larger inputs fit the existing limit or validate a semantic path.

The implementation and rejection boundaries were also compared on 440 synthetic
cases. The five affected offline suites pass 65 tests, including nine new hash,
guard, full-validation and fresh-clock regressions. Two separate new stop-contract
tests pass as well; these are distinct targeted runs, not a full-suite result.
Initial test-fixture failures are retained separately. Exact source identity,
targeted regression commands and package
verification belong to the adjacent local receipts. No further model/API call,
database write, old-question replay or live service switch was made for this fix.
