# Summary deadline correction (0.14.1)

Read-only inspection of the user's identified calls found that they timed out
in summary generation, before any reply call. Exact input counting completed,
input admission passed, and the model slot had no wait. No generation response
body or validated generation usage was received. There is therefore no saved
generation raw output to reconstruct for those calls; their usage stays unknown.
The records establish the client-side wait and cancellation, not whether its
remaining time was spent in server generation, provider queuing or the network.

The failed summaries used the same previous checkpoint and identical count/
generation payloads. Summary failure correctly left the checkpoint and original
history unchanged. A later newly admitted chat consequently needed the same
compaction again. This is an explicit new chat call, not an automatic retry
loop; the implementation is not changed to skip compaction, discard history,
advance coverage on failure or manufacture a completed summary.

After reviewing the following difference, the user explicitly approved two
deadline changes and one real summary trial:

| Parameter | Original → approved | Classification |
|---|---|---|
| `adapter.budgets.summary.seconds` | `20` → `60` | Runtime allowance |
| `runtime.turn_seconds` | `100` → `130` | Runtime allowance |
| Model / label, summary, reply reasoning | `gpt-6.1-sol` / `medium` → same | Unchanged |
| Stage input/output token caps, other stage deadlines, verbosity, queue/delivery limits, request serialization, prompts/schema, watermark/coverage, graph and retention rules | Original → same | Unchanged |

The 130-second turn accommodates summary 60 + reply 45 + local processing 5 +
delivery 10, leaving 10 seconds for remaining orchestration under the existing
cooperative deadline contract. The allowances are upper bounds, not latency
guarantees. Existing explicit values are not silently overridden by loading.
Local migration first acquires the state lease, checks the original bytes,
saves an exclusive backup, and changes only these two parsed fields and their
numeric text. No existing process is stopped or hotpatched.

The approved trial submits the same failed summary input once under the new
60-second bound. It owns the state lease, uses the usual input-count/generation
path and scratch receipts, and never writes a conversation checkpoint or sends
a Discord message. Old failed calls are neither reset nor reconciled to zero.
Trial failure does not trigger another generation request. Private input,
output, call identifiers and usage remain in the existing local scratch only.

The authorized real trial returned a completed response with validated usage,
nonempty schema-compliant summary and a valid UTF-8 size. Its total duration
exceeded the original summary bound and remained within the approved bound;
the output token cap was not exhausted. Medium reasoning is therefore retained
for this delivery. This result validates the identified compaction input only;
it does not prove the earlier server-side timings or future response latency.

The user additionally authorized a conditional fallback: if the longer deadline
does not solve the issue, reduce reasoning to `low` and restore the original
deadlines. This is a user decision to apply when supported by the trial/result,
not an automatic per-call model or effort fallback. No silent fallback or retry
is added to the runtime. A single successful trial does not establish all
future latency, response quality or Discord delivery.

The synthetic regression reproduces a valid 25-second summary completion: the
old 20-second configuration rejects it while the approved 60-second one accepts
it, preserving one count request, one generation request, medium reasoning and
the original output cap. Existing summary failure/coverage tests retain the
same checkpoint and raw-history preservation rules.
