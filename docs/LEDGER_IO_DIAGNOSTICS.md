# Ledger I/O diagnostics

Experimental `0.17.0.dev8` records bounded metadata when the per-turn budget
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
remain in force, with no model replay, refund or file-write retry loop.

The preceding real failure occurred after a completed query response with known
usage, while saving the call-end ledger. The older implementation discarded the
underlying operating-system error. Its original record remains unchanged and
fails full audit because the provider-end event was never saved. This version
does not manufacture a replacement event or retroactively mark it complete.

The writer flushes and synchronizes its temporary file, closes its handle, then
replaces the target. The supported runtime serializes messages within a state
instance, holds its instance lease through cleanup, and deduplicates the same
message before constructing a new ledger. Different message identities have
different target paths. A deliberately unsupported pair of concurrent writers
to the same target can race on the fixed temporary filename; demonstrating that
mechanism does not establish that the historical runtime had two writers.

Offline Windows experiments can reproduce replacement failure while a target
handle is open and preserve the previous complete JSON. Closing the handle can
allow a later independent synthetic write. Sharing-delete flags alone did not
make the tested replacement succeed on this machine. These experiments use
temporary fixtures and establish possible mechanisms, not the cause of the
historical incident. No permissions, sharing flags, atomic-write algorithm or
retry policy have been changed on this evidence.

For the next naturally occurring failure, inspect the matching trace's
`requery_ledger_io_failure`, its stop or failed-turn metadata, and the existing
provider usage receipts. The new operation/error codes distinguish an open,
flush/sync or replacement failure without exposing the private target. Scratch
retains its existing rolling window; this feature does not archive raw data or
create a new monitoring task. No further real model question is required to
install the diagnostic patch.

To roll back, normally stop and finish cleanup of the owned Console, restore the
preserved dev7 entry and load its verified package. Keep the approved query
configuration and all ledger/scratch records; no index reset or replay is needed.
