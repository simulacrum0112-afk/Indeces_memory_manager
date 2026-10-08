# Independent offline feasibility delivery

This snapshot is confined to
`D:\Indeces\build\active-requery-feasibility-20261008\checkout`, branch
`work/active-requery-feasibility-20261008`. Its published base is
`152503bf99b2fb092257b703b53f52470dc2fcd3` (`0.16.1`). The original dirty
`D:\Indeces` checkout, installed production package, configuration, databases,
and process have not been switched by this work.

Checkpoint one is local commit
`fc2fd952a3d1b0457b1c77bf133de26748b8311c` (`0.17.0.dev1`): bounded structured
actions, actual local static graph queries, immutable cumulative evidence and
budget/run-record verification. Checkpoint two adds the independent sparse
Krylov and typed-path diagnostics (`0.17.0.dev2`). Its final full commit and
source manifest are in the adjacent `CHECKPOINT_TWO.json` / `DELIVERY.json`;
they are not inferred from this version string. Neither checkpoint was pushed.

Both Runtime feature flags default to false:

```toml
[runtime]
active_requery_enabled = false
communicability_enabled = false
```

Real active-query admission is additionally disabled, with zero configured
cost cap/prices. This task permits simulation, not real provider calls.

## Actual validation

| Command / scope | Final result |
| --- | --- |
| `python -X utf8 -B verification/run_active_requery_offline.py --phase one` at checkpoint one | 203 tests, 0 failures/errors/skips |
| Same selected regression group after diagnostic wiring | 203 tests, 0 failures/errors/skips |
| `python -X utf8 -B verification/run_active_requery_offline.py --phase two` | 44 tests: 34 numerical/metadata/typed, 10 Console Runtime integration; 0 failures/errors/skips |
| `python -X utf8 -B verification/mock_active_requery.py` | Two additional native retrievals, cumulative M1/M2 in final mock input; run-record complete 1/issues 0 |
| Same script with `--communicability` from source and isolated installed wheel | Native multi-round bundle diagnosed; one final mock delivery; unchanged static weights; complete 1/issues 0 |
| `verification/build_offline_snapshot.py` with explicit local wheelhouse | Build tools, wheel build, isolated target install: each exit 0; `--no-index`, no dependency download |
| `verification/verify_offline_package.py` | 46 Python source files match tested identities; 59 RECORD entries checked; CLI help exit 0; real Console run banner prints `0.17.0.dev2` under synthetic immediate quit |

There are **247 distinct tests across the two selected groups**, rather than a
full-suite claim. Repeated runs are not added to this total. Command receipts,
exact module lists and source hashes are committed under `verification/`.
Original successful and unsuccessful full harness logs remain beside the
checkout. Failure histories identify earlier logs that were not retained in
full, instead of reconstructing them.

The build used an existing local cached setuptools 84.0.0 wheel, SHA256
`51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670`.
It installed build tools and the application into fresh artifact directories,
without changing the active virtual environment. The application wheel is
`indeces_memory_manager-0.17.0.dev2-py3-none-any.whl`, SHA256
`715f317224f8d2544337ffefdffa45bd9ef09068694d6ac2da7edf1f8c7a3548`.
The package verifier checks pip's two generated launcher relocations inside
the isolated target directory and refuses other escaping RECORD paths.

## Simulation stages and limits

The standalone smoke disables the initial model selector. Its original
synthetic query has no result; additional query one contributes M1, query two
contributes M2, then the planner selects answer. Generation stages are
`query/query/query/reply`, eight injected count/generation transport requests,
synthetic usage 256 input / 128 output. All new evidence reaches the final mock
input, earlier rounds remain intact, and no static weight changes. No historical
questions, answers, gold, user sources or production state enter this fixture.

The diagnostic actually performs four sparse matvecs on two copied association
edges. With unchanged default numerical caps it reports `unknown`,
`accepted=false`, reason `krylov_or_matvec_limit`. This is a valid bounded
unaccepted observation, not numerical convergence. It does not change the
selected evidence or final mock response. Analytic numerical fixtures in the
module group separately observe convergence. Certified floating-point error
remains unknown (`certified_total_error_bound=None`) in every case.

The graph's NPMI edges are undirected cooccurrence, even though the index has
addressable directed keys. The graph diagnostic therefore always reports
`insufficient_relation_semantics`. The separate explicitly typed synthetic
path fixture returns only `pending_hypothesis`, `proof=false`, and
`source_truth_verified=false`. It verifies declared constraints and traceability,
not source truth, entailment, a derived scientific relation, or private reasoning.

The active-query ledger includes the initial selector and any summary when
those stages execute. The unchanged selector can reject long raw history
before summary; summary execution is tested independently with initial
selection disabled. No single fixture is claimed to have executed all four
stages. Stops preserve first cause and usage, unknown generation usage remains
sticky, terminal ledgers cannot be reopened, and there is no retry or refund.

## Reproduce and rollback

From this isolated checkout, using existing dependencies:

```powershell
D:\Indeces\.venv\Scripts\python.exe -X utf8 -B verification\run_active_requery_offline.py --phase one
D:\Indeces\.venv\Scripts\python.exe -X utf8 -B verification\run_active_requery_offline.py --phase two
D:\Indeces\.venv\Scripts\python.exe -X utf8 -B verification\mock_active_requery.py --communicability
```

The smoke supports `--package-root <isolated-installed-directory>`. Build and
package verification scripts expose their exact required arguments through
`--help`. Builds require a fresh output directory and an explicit local
setuptools wheelhouse; they refuse to overwrite earlier artifacts.

Leave both flags off to retain the established reply path. In a clean isolated
worktree, switching to checkpoint one removes the diagnostic layer; switching
to the published base removes both experiments. Do not reset the original
dirty checkout, delete existing state, or change a running instance. There is
no schema migration, production configuration change, or service rollback to
perform for this offline delivery.

## Current Console and unrun acceptance

Read-only checks find a visible responsive WindowsTerminal titled
`Indeces Console 0.16.1`, with existing Python PID 61316 still alive. The current
launcher help exits 0 and its 43 installed Python files match the published
manifest. This is a process/window check, not a fresh Discord or model-health
test. The generic launcher uses the installed package's version. The old
`Indeces-Console-04.cmd` still names an old development package; it was not
modified here, and no current startup failure was reproduced from the generic
entry. The exact entry used for the user's failure remains unconfirmed.

Not run: full repository unittest discovery, remote CI, old 20/7-question or
Q1/Q2/Q3 replay, gold-based relevance/answer scoring, real model calls, real API
latency, Discord questions, production switch, commit/tag publication, push,
merge or deployment. Earlier failed quality acceptance has not been reclassified.
The resulting code is a runnable offline feasibility snapshot, not evidence
of real answer-quality improvement or production readiness.
