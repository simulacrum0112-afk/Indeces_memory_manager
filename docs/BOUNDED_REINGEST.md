# Explicit bounded four-document reingestion

This maintenance path is implemented in `indeces.reingest`; it does not start
Discord, discover additional files, modify configuration, or reset failed
knowledge versions. A production run requires an operator's explicit decision
to close the four old unknown-usage calls as an unquantified loss. That decision
is an additional ledger event, not a claim that those calls used zero tokens or
that their exact usage was reconciled. Normal retry and digest quarantine retain
their existing restrictions.

## Grant and accounting

The grant binds exactly four current failed versions to their existing scope,
relative paths, complete file digests, converted snapshots and 400-character
chunks. The saved PDF bytes and conversion metadata are verified locally.
All chunks are labelled afresh with new source and attempt IDs. Old versions,
chunks, PDF metadata, failure audit, known counters and pre-existing checkpoints
are fingerprinted and retained. New checkpoints do not invalidate that historical
fingerprint. No original file is moved or rewritten.

Each scope/path/digest can receive only one maintenance attempt. Repeating the
same grant returns its saved batch ID; changing a grant's targets cannot allocate
another allowance for any already-bound digest. Reopening a batch retains all
confirmed labels, consumed resources and unsettled reservations. The tables
`reingest_batches`, `reingest_documents`, `reingest_calls`, `reingest_requests`,
`reingest_labels`, and append-only `reingest_events` are separate from the old
ledger. SQLite backup, including committed WAL content and an integrity check,
precedes additive schema changes and each new grant. The runner owns the same
exclusive state lease as the runtime through HTTP/resource cleanup.

| Scope | Input tokens | Output tokens | Seconds |
|---|---:|---:|---:|
| One label call | 4096 (unchanged) | 512 (unchanged) | 15 → 45, task only |
| One new document attempt | 65536 | 16384 | 1800 cumulative |
| One four-document batch | 262144 | 65536 | 7200 cumulative |

These are explicitly authorized runtime/recovery settings. They do not change
the scientific model, original global/version budgets, GPT-6-Luna, low reasoning,
high verbosity, schema, graph rules, or single serialized request slot. The
effective call deadline/input admission can shrink to the remaining allowance.
Full output capacity must fit before a generation is issued. No automatic retry
or resource increase occurs.

Each HTTP request has a fresh `X-Client-Request-Id` committed before possible
issuance. Generation admission follows the exact input count and commits its
input/output reservation first. Provider request headers are persisted before
reading the body. Response ID and validated actual input/output usage are
persisted before scratch writes and output/label validation. Actual usage
settles reservations; caps and reservations are never substituted for measured
usage. A known-usage failed output is still charged. Durable accounting contains
metadata, not an additional private prompt/output archive. Existing scratch
retains its ordinary rolling 24-hour policy and `store:false` is unchanged.

The cumulative execution timer includes input counting, slot wait, preparation,
generation, validation/persistence, publication and orchestration. Setup/grant
validation and status viewing are outside execution. Live admission includes
uncommitted invocation time. Normal timings use a high-resolution monotonic
counter. After a crash, an unmeasurable unfinished call is conservatively charged
its saved deadline, explicitly marked as an elapsed allowance, not measured
elapsed or actual token usage. These are cooperative deadline checks, not an
operating-system wall-clock preemption guarantee.

## Stops, pilot and publication

- No generation intent: save the error/phase/time; no unknown model usage is
  invented. Explicit known-failure resume may continue within the same grant.
- Generation intent without validated usage: stop the entire batch, retain the
  reservation and nullable actual counters, and prohibit automatic replay.
  A crash after intent is treated conservatively as possibly issued. A stale
  batch status cannot bypass any unknown call. This grant does not authorize
  closing newly unknown calls as loss.
- Known usage but invalid output: actual counters remain charged, and the batch
  stops. A requested known-failure resume retains its already valid labels and
  must fit the original remaining allowance.
- Crash/restart: orphaned calls become explicit failures. Unknown status takes
  precedence over every known failure. Reopening never silently resumes them.
- Exhausted allowance or changed source: stop; never reset budgets or transplant
  old labels to a changed document.

The pilot labels three blocks per document in three passes across all four
documents. Its twelve calls and labels count toward the full attempt. The pilot
does not publish. Completion requires a passed pilot and skips its saved blocks.
Each complete document is independently published in a transaction containing
new versions/chunks/PDF provenance, graph records, publication/desired pointers
and its receipt. Any failed graph publication rolls back the entire transaction.
Previous published graph support is retired; original failed version rows are
not rewritten. Future same-digest ordinary scans recognize this certified
publication without overwriting the original path-keyed failure audit. Later
ordinary observations for that protected audit are append-only successor events;
new unknown usage still acquires its own digest quarantine.

## Entry point and verification

```
python -m indeces.reingest --config CONFIG --source-id OLD_SOURCE_1 \
  --source-id OLD_SOURCE_2 --source-id OLD_SOURCE_3 --source-id OLD_SOURCE_4 \
  --confirm-old-unknown-loss --phase pilot
python -m indeces.reingest --config CONFIG --batch-id BATCH --phase complete
python -m indeces.reingest --config CONFIG --batch-id BATCH --phase status
```

Status uses a read-only database connection and does not load credentials,
initialize graph/scratch, back up/migrate, or make requests. Production filenames,
digests, credentials, counters and raw material are not included in this public
documentation or its synthetic fixtures.

Before production, synthetic tests cover old-record preservation, backup,
single-grant binding, the 12/388 workflow, actual usage on rejected labels,
known/unknown crashes (including stale batch-status windows), resource gates,
changed files, atomic rollback and subsequent ordinary scans. Offline success
does not establish live model behavior or recall quality. After production,
report each paper's complete chunk count, label appearances/distinct labels,
actual newly measured usage and graph records, and distinguish literal text
presence, label membership and actual static retrieval selections. Missing
concept labels must be reported, never fabricated to satisfy a checklist.
