# Finite manual acceptance checkpoint

This checkpoint adds an opt-in manual test entry and a selector protocol. It does not replace the normal Console or deploy a running service. Its version is `0.15.1.dev2026100703`; the default reply path keeps the original static retrieval behavior unless a selector is explicitly supplied. The failed offline v1/v2/v3 selectors and keyword-driven NPMI requery are not enabled or included.

## Commands and operator prerequisites

The default command is an offline synthetic mock:

```powershell
python -m indeces.manual_trial --mock --output D:\Indeces\build\manual-pilot-runs\new-mock-directory
```

Real execution requires `--live` and all of the following, supplied by the operator later: the existing project config path, Guild and test channel, OpenAI organization/project IDs, a frozen input file, an approved descriptor, and a tariff with evidence for both generation and the input-count request. Do not put a key or bot token in these JSON files. The shipped approval and tariff templates are incomplete and cannot activate a request; USD 1 and two generations are a proposal, not an existing spending authorization.

```powershell
python -m indeces.manual_trial --describe-input --inputs <independent-frozen-input.json>
python -m indeces.manual_trial --live --delivery discord `
  --config D:\Indeces\config.local.toml --channel <channel-id> --account <organization-id> `
  --inputs <independent-frozen-input.json> --approval <approved-descriptor.json> `
  --tariff <verified-tariff.json>
```

`--describe-input` prints digests, counts and proposed limits only. It does not read credentials, approve expenses or make a request. The descriptor binds the input digest, exact Python source identity, config-byte digest, target, organization/project, delivery mode, expiry, tariff and every limit. `config_source_sha256` is the SHA256 of the exact existing config bytes, calculated locally by the operator. `tariff_sha256` is `canonical_hash(asdict(Tariff(**tariff_json)))` using the packaged gate helper; do not substitute a hash of pretty-printed JSON. `candidate_identity` is the value printed by `--describe-input`.

The existing Console must be inactive before the operator runs this independent session. The entry acquires its original state lease and rejects an active owner; it has no Console IPC injection interface and never stops or takes over another process. It compares full loaded config and byte fingerprints before/after acquiring the lease and before credential/network steps. Credentials are read only at manual execution, using the existing environment → saved current-user DPAPI → hidden prompt order; they are not saved by this entry.

The account binding is an operator attestation, not authentication of an opaque key's billing owner. The transport routes the approved organization/project through the existing HTTP request headers; OpenAI documents these headers in its [API reference](https://developers.openai.com/api/reference/overview). Unexpected reported organization/model, unverified pricing or incomplete metadata stops the batch. No account, key or project is created.

The Discord mode accepts only one or two unique approved texts from human literal mentions in the chosen Guild/channel; unknown texts, bots and duplicate message IDs do not consume a model slot. The two texts must differ. It reuses existing Discord dispatch/delivery and automatically closes the finite session. `--delivery api` is an explicit alternative for local output using the same real model gate; it does not claim Discord delivery. Output directories are claimed exclusively and must be new children of `D:\Indeces\build\manual-pilot-runs`; existing run records are never overwritten.

## Resource and failure contract

The canonical ledger is fixed at `D:\Indeces\build\manual-pilot-approval-ledgers\<SHA256(approval_id)>.json`, independent of `--output`, extraction folder and delivery mode. An immutable approval ID binds the complete normalized descriptor; changing its account, bundle, tariff or source fails on reopening. Concurrent writers are excluded. A different ID requires a distinct explicit owner approval; local files and assertions are not cryptographic protection against a privileged operator rewriting them.

At most two input-count requests and two reply generations are admitted. Each generation reserves 16384 input/2048 output tokens before HTTP, with cumulative generation caps of 32768/4096. Each item's count plus generation shares at most 45 seconds; the whole session has at most 90 seconds, including waiting/delivery. Smaller approved cumulative/time/money limits are allowed. All deadlines are cooperative checks and asyncio cancellation, not operating-system preemption.

Money uses exact Decimal values. Counting supports only an explicitly verified flat fee, including verified zero; variable or missing counting prices are rejected before a request. Generation reserves the approved uncached input/full output cost. Fees recorded after usage are calculations from the approved tariff, not verified provider invoices or a provider-enforced account cap. No current price is asserted by the fixtures or templates.

Admission is durably recorded before the normal OpenAIAdapter `session.post`. The existing adapter still builds/counts/parses the requests, preserves raw scratch/audit and owns its serial request slot. The wrapper binds complete count/generation input and options, prohibits tools/redirects, uses the approved model only, and retains known usage/cost when a later error or expiry occurs. Unknown usage/fee, timeout, cancellation, malformed response, delivery failure, persistent-state failure or pending reopening halts the whole batch. Model calls are never retried or refunded. Gateway automatic reconnect is disabled; native discord.py login/send HTTP behavior is retained inside finite deadlines and is not claimed to be universally retry-free.

No KnowledgeService, scan, PDF conversion, labeling worker or summary call is started. Each input uses a fresh isolated Store seeded only with the approved frozen materials. The production Store, knowledge files and checkpoints are not opened or migrated. Unexpected summary/label stages are rejected before counting.

## Independent held-out evaluation

The JSON schema is `indeces_finite_pilot_inputs_v1`: `kind=approved_frozen`, nonempty `owner_evidence`, one or two `{item_id, question}` rows, and at most 128 `{source_id, text, marks}` materials. Item IDs must be safe ASCII; questions are 1–8192 characters with no surrounding whitespace. Each material is 1–400 characters with 1–8 marks of 1–40 characters. Gold answers and scoring fields are rejected; keep the independent scorer's key outside this file and outside the development checkout.

Botaaa owns the new held-out test. Developers do not request, read, copy or publish its questions or answers. This checkpoint includes only synthetic fixtures and a generic contract. The evaluator freezes the source identity, supplies the private inputs at manual execution, and separately reads the run's answers, selector/material receipts, scratch linkage and ledger. Local API delivery writes private `answers.jsonl`; Discord delivery and the normal runtime are recorded in the isolated Store/scratch. These runtime records must never be committed or uploaded as build artifacts.

Hash validity and citation linkage establish internal consistency, not semantic support or answer truth. No real model, real Discord, held-out quality, API latency or running production-build identity is established by mock tests. Old Q1–Q3 effect failures remain failures; this checkpoint does not claim improved answer quality.

## Verification and rollback

Run the complete offline suite in this checkout with the existing project Python, then CLI check against `config.example.toml`; targeted manual tests live in `tests/test_manual_trial.py` and `tests/test_manual_trial_gate.py`. Build with the installed setuptools backend using `--no-index --no-deps --no-build-isolation`, and install only to an isolated target. The delivery report records precise test counts, wheel/RECORD/source checks, commit and visible CI metadata. Do not infer test counts from CI status.

Normal Console behavior is unchanged. Stop using this independent checkout/isolated installation to roll back. Do not delete original experiments or alter original user files, production state or another owner's service. An approval ledger that stopped on unknown usage cannot be bypassed by changing output folders; any later exception requires separate explicit owner review.
