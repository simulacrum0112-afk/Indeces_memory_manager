# DEV11 isolated real transport entry

`python -m indeces.path_requery_smoke` is a separately authorized smoke entry for
the DEV11 path loop. Its default is a dry-run JSON plan. The current delivery
has not executed this entry against a provider or performed paid validation.
Console version comes from the same DEV11 package version; this entry does not
install the package, switch a launcher, or change a running Console.

## Experimental release checkpoint: 0.17.0.dev11+realentry1

This prepared experimental release is authorized for publication on the existing
experimental branch and for updating the main working directory. Updating that
directory does not imply a merge into Git `main`, a production activation or a
restart of an existing service. The final release receipt binds the exact source,
publication commit and remote verification; this document does not assert a
completed push or loaded-process identity.

Free source verification covered 347 methods (346 passed, 1 skipped, 0 failures
and 0 errors); the isolated installed subset passed all 48 methods. The 54
application source/resource files matched byte-for-byte across source, wheel and
installed target. Console version, offline check and the default dry-run plan
passed. The preceding C9 full run of 1525 methods is preserved historical
evidence and is not a complete-suite result for this guarded real entry.

The user chose to skip the separately authorized real synthetic smoke after
preflight could not verify the charge for `/responses/input_tokens`. That
endpoint is required by the current adapter before generation, and its cost is
not represented by a verified separate tariff in the existing ledger. Preflight
did not treat the absence of a listed fee as evidence of free use. The smoke made
0 count requests and
0 generation requests, incurred USD 0 and did not consume its authorized attempt
budget. No later invocation is implicitly authorized by this publication.

The path-loop feature remains default OFF. Explicit real-call authorization,
positive cost and upper-price gates, the original cumulative budgets, persistent
unknown-usage stop and zero automatic retries remain required. Real model
behavior, actual usage and fees, latency, answer quality, Discord delivery and
production acceptance remain untested. The original 16 behavior contracts remain
12 passed and 4 partial. Neither offline tests nor a future `observed` toy result
would establish real-corpus recall, semantic support or answer quality.

## Default plan and execution approval

Run this exact free command from the reviewed DEV11 checkout or isolated package:

```powershell
python -m indeces.path_requery_smoke --run-dir D:\Indeces\build\dev11-real-smoke-001
```

No key or other environment value is read by this plan. It does not inspect the
target directory, create state/configuration, construct an adapter, or issue
network requests. Supplying prices without `--execute` still only makes a plan.

Real execution needs separate explicit approval of the command, a new absolute
run directory, an approved positive dollar cap, and positive input/output upper
unit prices. All three must be finite, representable and at most 1000. Price
values are supplied bounds, not a verified current tariff or a billed-cost claim.
There is no implicit USD 1 authorization and no default paid budget.
Positive CLI flags are admission configuration, not evidence that the user
approved an API budget. The operator must obtain that separate approval first.
Current official quota status remains **unknown**: the preceding quota probe
ended with proxy exit and stdio read timeout. This entry does not query quotas
and must not reuse the earlier 6% estimate.

After those exact values are approved, the command template is:

```powershell
python -m indeces.path_requery_smoke --execute `
  --run-dir D:\Indeces\build\dev11-real-smoke-001 `
  --approved-cost-usd $approvedSmokeCost `
  --input-upper-usd-per-million $approvedInputUpper `
  --output-upper-usd-per-million $approvedOutputUpper
```

The three variables must contain the separately approved numeric values. Until
then this is an unexecuted template. Use the same arguments without `--execute`
to review the resulting plan. The selected directory must have an existing
parent and must not already exist, even if empty. OneDrive, linked paths and
junction ancestors are rejected by the existing managed path policy. The target
itself and all ancestors must also avoid directory components `knowledge`,
`state`, `scratch`, `.git`, `.indeces` and `_staging`, case-insensitively. The entry
checks the original spelling, lexical normalization (including `..`) and the
managed policy's canonical path before reading the key or creating anything.
This prevents a fresh child directory from planting toy state/configuration in
an existing material or management tree. The run's own newly created internal
`state`/`scratch` children remain isolated inside its approved new root. Directory
checks are metadata checks, not an operating-system guarantee against a hostile
concurrent replacement of a parent directory.

Only after execution flags, budgets and the new directory pass validation does
the entry read the existing `OPENAI_API_KEY`. It does not ask for, persist, migrate
or print credentials. No key means refusal before creating the run directory.
The entry supports only the existing official Responses adapter for
`gpt-6.1-sol`, `medium` reasoning and `high` verbosity. There is one adapter and
one serial request slot for all model stages. The CLI exposes no mock transport,
alternate model/provider, production config, source-file or resume argument.

## Isolation and fixed caps

The exclusively created run directory contains its own synthetic TOML, Store,
scratch, persistent admission ledger and results. The TOML goes through the
existing configuration loader. Nothing reads the production configuration,
knowledge directory, databases, PDF files or private answers. No ingest service,
Discord connection, webhook, console IPC, observer, background watcher or model
labelling is started. Delivery is a local receipt.

The fixture contains 20 toy observations. A and B are two distinct sources for
`alpha -> beta` and `beta -> gamma`. Two book-entry distractors and genuine
synthetic DF/NPMI values enforce the native initial ranking `[decoy, decoy, A]`;
adding `beta` gives `[decoy, decoy, B]`. Book entries cannot supply path evidence.
A model selector may choose differently, and a real planner may answer, clarify,
stop, emit an invalid action or decline to request the gap. The entry preserves
what actually happened and does not force the model action or invent a pass.
The only composition rule is explicitly declared for these toy `links` facts;
it is not a scientific ontology or semantic entailment proof.

| Bound | Fixed maximum |
| --- | --- |
| Selection / query stage | 4096 input, 512 output, 15 seconds each |
| Reply stage | 16384 input, 2048 output, 45 seconds |
| Summary stage | 16384 input, 2048 output, 60 seconds |
| Label stage, unused by this entry | 4096 input, 512 output, 15 seconds |
| Entire turn | 6 generation calls, 32768 input, 4096 output, 60 seconds |
| Native query rounds after initial retrieval | 2 |
| Local step | 5 seconds, also subordinate to the shared deadline |

Input counting and generation share each stage deadline. Selection, any summary,
all planning and final reply share the original cumulative ledger. There is one
generation at most per admission, no automatic retry and no changed NPMI weight.
Unknown generation usage halts the turn, forbids a final model request and keeps
the admission record. Existing query/final-reply reservation rules still apply.
The CLI has no resource-limit overrides.

The token-cap price estimate is

`(32768 * input_upper_usd_per_million + 4096 * output_upper_usd_per_million) / 1000000`.

The configured admission ceiling is the smaller of that value and the approved
cap. Each stage first reserves its full permitted cost. A positive but small cap
can reject the first call before even input counting. Actual input size is
provider-counted during an authorized run; the estimate is not a tokenizer or
API latency measurement. Raw bounded path receipts may exceed a later query
stage's input limit, resulting in a recorded budget stop rather than an increase.

## Receipts, interpretation and repeat protection

`RUN_INTENT.json` is written once before constructing runtime/provider resources;
`SYNTHETIC_CONFIG.toml` and `SYNTHETIC_SOURCE_IDS.json` are public toy metadata.
`state/active_requery_ledger/` holds the normal persistent admissions and usage.
Raw model inputs, outputs, frozen evidence, citations and all path feedback stay
in the existing rolling 24-hour scratch contract. `RESULT.json` contains safe
metadata, source identities, observed stage order, usage, verification status and
the path-loop observation; it does not duplicate the raw generated answer.

- `observed`: strict separated A/B retrieval, qualified accumulated toy path,
  declared formal composition, bound feedback in the next planner, both source
  bodies in final context, completed known usage and valid record bindings.
- `partial`: a path feedback result occurred but one or more of those observations
  is absent. Review the actual ledger and scratch rather than infer a pass.
- `not_executed`: no path feedback result occurred; this includes a planner
  choosing to answer directly and a gate stopping before native requery.

`proof`, `truth_verified`, `semantic_support_verified` and
`scientific_or_answer_quality_verified` remain false in every report. Even a
successful toy smoke is not a real-corpus recall or final-answer benchmark.
An exit code of 0 means a dry plan or a completed run with `observed`; 1 means the
execution did not meet that observed smoke contract; 2 means CLI refusal.
Injected free-test transports are reported as such and are not live API proof.

The new directory itself is the exclusive run lease. Every subsequent invocation
with that path is refused before credential access, regardless of success,
failure, interrupted execution, unknown usage or an incomplete results file.
The entry never deletes, overwrites, resumes or retries a prior run. Its directory
and any partial intent/ledger are preserved. Starting another paid attempt under
a different directory requires a new authorization and must not evade unresolved
usage or the earlier approved campaign budget.

Rollback is to leave the production entry untouched and not execute this isolated
module. No live configuration or database migration is needed. Offline test
counts and paid-validation status must be taken from the final delivery receipt;
this document alone is not evidence that any provider call happened.
The broader baseline of 16 contracts remains 12 passed and 4 partial; adding this
entry and passing shaped-transport tests does not upgrade those partial items.
