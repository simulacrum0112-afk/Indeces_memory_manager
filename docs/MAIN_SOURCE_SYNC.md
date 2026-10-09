# Physical source synchronization: 0.17.0.dev11+realentry2

This release integrates the local NPMI execution telemetry and run-record
validation changes with the guarded real-entry release whose exact parent is
`da68ecda717aabb063b07d8c18d7cfb31e2e4c9e` (`0.17.0.dev11+realentry1`).
The main working directory and Console must load the same reviewed application
files. A version string alone is not evidence of that identity. The publication
commit, remote verification and completed physical synchronization are recorded
in the local final delivery receipt; this source document does not assert that
an operation has already happened.

## Final behavior

The initial retrieval uses the existing asynchronous prepare/choose/finish
flow. NPMI telemetry measures actual local calls and weights used. It excludes
model-selection waiting, restores the enclosing ContextVar on cancellation or
failure, and does not aggregate later native requery into the initial receipt.
The original graph audit and selected candidates remain unchanged. Early budget
or ledger initialization failure produces one zero-count `failed/not_entered`
receipt. A secondary diagnostic write failure preserves original failure
cleanup and never retries; missing telemetry remains an audit failure.

The common validator checks declared telemetry before branching into v1, v3
or retention-partial handling. It rejects duplicate, missing, mismatched and
inconsistent receipts against the initial graph. A prefix waiting for its first
selection remains valid partial evidence. Retained start plus downstream reply
or retrieval evidence cannot bypass the receipt requirement. Older undeclared
records keep their original compatibility rules. See [RUN_RECORDS.md](RUN_RECORDS.md).

NPMI formulas, candidate ordering, graph rules, model identity, asynchronous
selection, serial slot, stage and cumulative budgets, provider count gates and
unknown-usage halts are preserved. Path loop remains default OFF. No model-led
keyword/NPMI feasibility proposal is enabled by this telemetry integration.

## Verification

Eighteen new synthetic integration methods passed, including success, failure,
cancellation, excluded selection wait, initial-only counters, partial tampering,
early initialization and diagnostic-write faults. Eighty-eight methods passed
against the isolated installed package. All 55 published application and
resource files are byte-identical across source, wheel and installed target;
source and distribution versions both declare `0.17.0.dev11+realentry2`.

Two independent source processes compared the exact parent with this release
using frozen synthetic fixtures and fake providers. All nine compared fields
matched exactly: planning and retrieval inputs, wire requests, frozen records,
choices, deliveries and call counts. This only establishes those two OFF
scenarios. The final source suite ran 1578 methods: 1557 passed,
21 skipped, 0 failures and 0 errors, using the normal source working
directory and a Windows spawn-safe verification entry. External network
attempts were blocked; none occurred. The isolated tree reused existing local
dependencies for launcher tests without installing or updating them.
Original failed harness and fixture attempts remain in local verification
records; none are rewritten as passing application tests.

No paid model call, production database write, ingestion, Discord delivery or
service start/stop is part of this release validation. Real model behavior,
answer quality, actual cost, latency and original incomplete acceptance items
remain unverified. Previous experiments and failed records remain intact.

## Local synchronization and rollback

The authorized operation copies the reviewed public source into the main
working directory while preserving its Git branch and HEAD. It does not merge
remote `main`. Before copying, preserve raw existing target files and original
user edits in a recoverable backup. Hold the project's existing Console and
runtime kernel leases; reject active ownership and source changes during the
operation. Protected knowledge, state, scratch, credentials, configuration,
virtual environment and local assets are excluded. The public empty
`knowledge/.gitkeep` sentinel is not overwritten.

The normal local launcher verifies the published application bytes, imports
physical main-directory source, and derives its title and menu version from
that module. The preserved local offline evidence-selection module is listed
separately; it is not added to the published package. Menu verification sends
only `quit` and does not start a service.

The local rollback helper restores an old file only if its current SHA equals
the synchronized SHA, and removes only newly created files that still match
the synchronized version. Subsequent user edits cause a refusal requiring
review. It never recursively removes protected directories or changes Git
refs. Restoring files does not reload any already running process; do not
silently stop an instance to roll back.
