# Frozen path API hotfix: 0.17.0.dev4

This finite follow-up closes two reproduced defects in local checkpoint
`53b01f1b9224b08c91e39e812e5170a391a3ef4f` (`0.17.0.dev3`). It changes only
the independent path parser/validator and necessary tests, version and delivery
artifacts. Runtime, graph weights, model budgets, production configuration and
all three default-off flags are unchanged. No provider, Discord, database or
service operation was performed against production.

## Reproduced causes and fixes

JSON decoding accepted an escaped lone surrogate such as `\ud800`. A bounded
raw quote could therefore decode to text that could not be written to UTF-8
scratch. The baseline whole-turn test recorded `UnicodeEncodeError`; four test
methods had 13 failing subcases/assertions and zero unittest errors. All decoded
fact strings now reject high/low lone surrogates. Invalid facts become
`invalid_typed_facts`/unknown without copying the invalid text into the receipt.
The original escaped quote remains frozen. A real Runtime fixture now saves
the unknown receipt, completes the ledger/reply and verifies one complete run
with zero issues. Ordinary ASCII, Chinese and correctly decoded emoji retain
their exact text and normal pending result.

The independent API previously compared catalog/binding citation IDs only with
each other. Changing both could pass while native records stayed untouched.
Four baseline methods had six failing mutation subcases: skipped, duplicated,
noncanonical or invalid IDs. Bundle input now receives bounded record and
metadata preflight followed by the existing pure-memory `validate_bundle`
replay. It validates the complete append chain and canonical globally unique
M1..Mn assignment. Original records and valid multi-round bundles still pass.
Shape counts precede metadata comprehensions. Time checks surround replay;
this remains a cooperative deadline, not operating-system preemption.

## Final verification and exact artifacts

Run from `D:\Indeces\build\active-requery-feasibility-20261008\checkout` with
`D:\Indeces\.venv\Scripts\python.exe -X utf8 -B`:

| Command / scope | Result on final dev4 source |
| --- | --- |
| `verification/run_active_requery_offline.py --phase hotfix --output <receipt>` | 52 distinct selected tests, zero failures/errors/skips; 41 injected count + 41 injected generation requests, zero real provider/socket attempts |
| `verification/mock_active_requery.py --automatic-paths --communicability --output <receipt>` | Source and isolated installed package exit 0; complete 1/issues 0 |
| `verification/build_offline_snapshot.py --wheelhouse <local-cache> --output-root <fresh-root> --source-receipt <receipt>` | Local tools, wheel build and isolated install each exit 0; no index/download |
| `verification/verify_offline_package.py --package-root <installed> --source-receipt <receipt> --output <receipt>` | 47 Python identities and 60 RECORD entries verified; CLI help exit 0; actual Console run banner `0.17.0.dev4` under synthetic immediate quit |

The selected modules are `tests.test_path_hypotheses`,
`tests.test_active_path_hypotheses`, `tests.test_path_hypotheses_unicode`,
`tests.test_path_hypotheses_citations` and `tests.test_requery_records`.
The frozen receipt is `FINAL_PATH_HOTFIX_RUN2.json`; RUN1 remains preserved.
Baseline `PATH_UNICODE_RUN1` and `PATH_CITATIONS_RUN1` JSON/full logs remain
beside checkout with their original source/test/log hashes. The baseline
Unicode fixture was subsequently cleaned without reconstructing its earlier
bytes or rewriting its receipt. Failure counts include unittest subcases,
not distinct test methods.

Wheel `indeces_memory_manager-0.17.0.dev4-py3-none-any.whl` SHA256:
`cf3dc4bcc3980c5266a0f6400dd83ef59ceb507c07aa205d5fc5267456de9399`.
`indeces/path_hypotheses.py` SHA256:
`56425093d5dc885ae23f04de40ae51d2a3f33023633df78e8f09418791c5301f`.
`CHECKPOINT_FOUR.json` pins the local commit and complete application manifest;
`DELIVERY_HOTFIX.json` embeds package/smoke results. The dev4 zip/patch and
`ARTIFACTS_HOTFIX.json` record CRC, hashes and exact published-base patch check.
The changed-file secret scan is limited; it is not a public-history audit.

Prior 283 selected tests belong to dev3 and were not rerun. Full repository
discovery, remote CI, historical Q1/Q2/Q3 or 20/7 gold replay, real model/API
latency/Discord and real knowledge/PDF acceptance remain unrun. Existing quality
acceptance stays failed/unresolved. Pending synthetic path declarations do not
establish truth, relation equivalence, scientific proof or answer improvement.
Default numerical diagnostics continue to report unknown; no score enters
selection or reply context.

## Rollback and Console limits

Leave `active_requery_enabled`, `communicability_enabled` and
`path_hypotheses_enabled` false for the established path. An isolated worktree
may return to prior checkpoint `53b01f1b9224b08c91e39e812e5170a391a3ef4f`
or published base `152503bf99b2fb092257b703b53f52470dc2fcd3`.
Do not reset the original dirty checkout or replace the running installation.
No production migration or remote publication occurred.

The installed dev4 Console version is verified independently. Production was
not switched and its last observed window still showed `Indeces Console 0.16.1`.
The user's original failing entry remains unconfirmed; no reproduced startup
cause or repair is claimed, and the version check is not fresh Discord health.
