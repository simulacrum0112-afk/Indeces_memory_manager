# Natural fallback notices

Experimental `0.17.0.dev7` uses deterministic Chinese fallback messages instead
of showing technical failure codes or trace identifiers in Discord. No extra
model request is used to write or polish these messages.

The planner's stop means a reliable conclusion is not being provided. It does
not itself prove that evidence is absent or that a user's stopping intent was
verified. The public message says that the current question cannot yet be
reliably confirmed from the available materials. Valid clarification questions
remain available. Empty or repeated additional retrieval says that no further
verifiable materials were obtained, without denying the initial evidence.

Input capacity, per-turn resource limits, elapsed-time limits and incomplete or
failed processing receive distinct explanations. An incomplete provider result
does not identify its cause; unknown codes receive a fixed processing notice.
Neither class is presented as missing evidence. The original reason, trace,
diagnostics, usage and ledger remain in the internal run record.

`natural_notice_v1` identifies the new presentation contract. New audit records
bind the internal reason to the exact public template and receipt. Removing a
new policy marker, changing the reason or substituting a different message fails
validation. Historical unmarked records retain their previous contract; their
original messages and failed experiments are preserved.

Stopping conditions, budgets, retrieval, graph rules, source text and citations
are unchanged. Bot fallback notices keep mentions disabled. Model skip remains
silent; queue rejection, cancellation and an uncertain or already-started send
do not gain a new response or retry. The owner's separate local query-input
override from 4096 to 4608 is retained; configuration and runtime data are not
part of this source change.

The preceding real negative-case smoke exercised query, appended retrieval,
another completed planning call and a safe stop with actual Discord delivery.
It did not exercise the final model-answer stage or prove a semantic path. The
new wording and its audit compatibility are validated offline; no automatic
additional Discord question is sent as part of this fix.

To roll back presentation, normally stop and clean up the owned Console and
load the prior verified dev6 package and entrypoint. Keep the approved query
configuration, all old/new records and usage ledgers unless a separate exact
configuration rollback is explicitly requested. No knowledge or index reset is
needed.
