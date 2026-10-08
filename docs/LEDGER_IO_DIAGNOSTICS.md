# Ledger I/O diagnostics

Experimental `0.17.0.dev8` introduced bounded metadata when the per-turn budget
ledger cannot be persisted. The operation is one of `open`, `write`, `flush`,
`fsync`, `close` or `replace`; `errno` and `winerror` are integers or null.
Exception text, filenames, private paths, credentials and model text are not
part of this metadata. The first failure remains attached to the stop receipt;
later cleanup failures cannot replace its cause.

A dedicated `requery_ledger_io_failure` scratch event identifies the attempted
audit stage and provider call, when available. Constructor failures can also
appear in the failed `turn_end`. These events are diagnostic evidence. They do
not establish that a provider call ended, that a ledger update committed, or
that the run passed full audit. Known provider usage remains charged; missing
usage remains unknown. All existing stopping conditions and resource limits
remain in force, with no model replay or refund. The dev8 implementation had no
file-write recovery loop.

The preceding real failure occurred after a completed query response with known
usage, while saving the call-end ledger. The older implementation discarded the
underlying operating-system error. Its original record remains unchanged and
fails full audit because the provider-end event was never saved. The diagnostic
patch does not manufacture a replacement event or retroactively mark it complete.

The writer flushes and synchronizes its temporary file, closes its handle, then
replaces the target. The supported runtime serializes messages within a state
instance, holds its instance lease through cleanup, and deduplicates the same
message before constructing a new ledger. Different message identities have
different target paths. A deliberately unsupported pair of concurrent dev8
writers to the same target can race on its fixed temporary filename;
demonstrating that mechanism does not establish that the historical runtime had
two writers.

Offline Windows experiments can reproduce replacement failure while a target
handle is open and preserve the previous complete JSON. Closing the handle can
allow a later independent synthetic write. Sharing-delete flags alone did not
make the tested replacement succeed on this machine. These experiments use
temporary fixtures and establish possible mechanisms, not the cause of the
historical incident. A later development regression captured a Windows
`replace` failure with `errno=13`, `winerror=5` during summary input admission.
Generation had not started for that summary. Its real provider and ledger ends
were preserved, along with known usage and the failed result. The operating
system denial alone does not identify a reader, permissions or another process.

## Guarded local recovery in dev9

`0.17.0.dev9` keeps model, token, time, cost and stopping limits unchanged. The
ledger writer uses a unique exclusively created temporary file and a
nonblocking target writer lease. It writes, flushes, synchronizes and closes the
snapshot once. Only a Windows replacement failure with the exact integer pair
`errno=13`, `winerror=5` permits additional replacement attempts: at most three
in total, with intentional waits of 10 then 20 milliseconds. The same prepared
snapshot is used; no provider request, usage calculation or model call is
replayed. These short synchronous waits and I/O remain charged to the existing
turn deadline; they are not an operating-system hard time guarantee.

Source and target file identities and bytes are checked before further attempts.
Changes or unavailable guards stop recovery. Thread and process leases exclude
writers using the same protocol; existing service state ownership and message
deduplication remain necessary. File checks are not an OS compare-and-swap and
cannot guarantee exclusion of an external writer that ignores the protocol and
races between checking and replacement. Failed unique temporary snapshots are
retained as ledger metadata, including an externally changed snapshot; later
writes do not overwrite or remove them. Successful replacement consumes only
its own temporary file. No ACL, sharing flag or system security setting is
relaxed.

Each adapter save checks the digest of its last successful ledger write. If
ownership is lost, later cleanup, halt and finish operations cannot write that
ledger again. Known usage remains in memory without inventing a durable terminal
record.

A successful local recovery attempts to record `requery_ledger_io_recovered`
when its diagnostic sink is available, with the first
safe error, replacement attempt count and configured intentional wait. Its
provider association uses the existing fixed stage/event/call-ID fields.
Recovery metadata cannot replace provider ends or confirm a terminal ledger on
its own. Permanent denial preserves the original safe failure and halts; after
a permanent failure the adapter does not revive the recovery loop during later
cleanup. `automatic_retries=0` continues to mean zero automatic model retries.

Offline fixtures cover transient denial, permanent denial, changed snapshots,
competing writers and provider audit boundaries. They establish recovery and
failure behavior within this contract; they do not establish the specific
external cause of the real Windows denial or a successful real-model retest.

For the next naturally occurring failure, inspect the matching trace's
`requery_ledger_io_failure`, its stop or failed-turn metadata, and the existing
provider usage receipts. The new operation/error codes distinguish an open,
flush/sync or replacement failure without exposing the private target. Scratch
retains its existing rolling window; this feature does not archive raw data or
create a new monitoring task. No further real model question is required to
install the diagnostic patch.

To roll back, normally stop and finish cleanup of the owned Console, restore the
preserved dev8 entry and load its verified package. Keep the approved query
configuration and all ledger/scratch records; no index reset or replay is needed.
