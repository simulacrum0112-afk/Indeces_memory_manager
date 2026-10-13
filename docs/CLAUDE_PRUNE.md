# Claude and npmi_prune

Version `0.17.0.dev19+prunecandidate1` adds an explicit native Claude tool loop for
the unchanged `npmi_prune` v0.1.2 package. Install the optional dependency with
`pip install '.[prune]'`. The offline CI dependency lock also includes NumPy
2.3.5. Graph and evidence data are supplied separately and checked against the
package's fixed v3.0.2 hashes; no graph or evaluation dataset is distributed here.

`create_offline_prune_loop` constructs the actual provider adapter and frozen
Graph/Session with an injected transport. `PruneToolLoop` exposes one client tool
with the package's `view`, `prune` and `answer` actions. It preserves ordered native
assistant content, including signed thinking blocks, and appends the correlated
tool result for the next Messages request. Calls are serial, counted against the
complete prefix, bounded by token and time limits, and never automatically retried.
An accepted final answer requires a successful tool answer with verified citations.

Live construction is explicit. It requires a precreated native client with retries
disabled and an existing, positively priced, expiring financial binding. Accounting
reserves maximum cost before each request, settles known usage, and retains unknown
reservations while halting admission. Private grant identity, paths, credentials and
receipts are provided by the operator outside this repository. Creating a fixture
ledger cannot enable live mode or reset an existing allowance.

The ordinary OpenAI and text-only Claude routes retain their existing contracts.
Console now exposes a finite `prune` request. Discord, background workers and
active/path requery retain their existing routes. Claude's ordinary controlled-session wrapper remains
unsupported and refuses activation. A source update does not start a service.

In the existing Console, select Anthropic / Claude Opus 5.5 / high with `model`
and configure the key with `apikey`. Run `prune`, then enter the question, A seeds,
optional B seeds, coarse/fine level and an exact target label. Seeds accept original
node IDs separated by spaces/commas, or exact concept names separated by semicolons.
Preparation validates the pinned graph and shows the protected seeds and candidate
pool. The request uses the selected reply token and time limits across the entire
tool loop; it does not renew those limits at each round. Other providers/models are
rejected before credential resolution or HTTP. No additional paid seed selection
is introduced.

The operator supplies `<config-stem>.prune.local.json` beside the existing TOML
configuration. Its four fields are `graph_dir`, `evidence_dir`, `binding` and `price`.
The binding uses `ExistingGrantBinding`'s exact fields: the original ledger path and
file identity, grant ID, original policy JSON, completed opening attempt, original
opening charge, ceiling, and original receipt path/hash. The opening charge is the
original receipt's charge; subsequent prune rows are accumulated by the ledger.
The price uses `PrunePriceEnvelope`'s positive, model/endpoint-bound token prices and
UTC expiry. Paths may be relative to the sidecar. This private file is ignored by
Git and contains no API key. The Console cannot create a new live allowance, renew
an expired tariff, discard pending charges or reopen a halted session.

Financial reservation is separate from the selected local token limits. Each
generation reserves the model's full 1,000,000-input-token bound plus its requested
maximum output, because the token-count endpoint is an estimate. Verified usage
settles that reservation before any local token-limit rejection. Unknown usage
retains the full reservation and halts the session. Consequently, a remaining
balance below that conservative hold blocks admission even for a small prompt.
The limits and estimate behavior are documented in Anthropic's
[models overview](https://platform.claude.com/docs/en/models/overview) and
[token-counting guide](https://platform.claude.com/docs/en/build-with-claude/token-counting).

`service` shows the answer, tool actions, this request's charge, cumulative charge,
remaining allowance and pending reservation. `stop` or Ctrl+C cancels the current
request and preserves any known charge or unknown reservation. While it runs,
`start`, `retry`, model changes and key changes are blocked. The finite request
does not start Discord, knowledge labelling or maintenance. Client cleanup precedes
release of the ledger and Console instance leases.

On Windows/Python 3.12, the exact private integration candidate passed the package's
24 tests and 41 native-loop/accounting tests. One separately authorized real
Claude Opus 5.5/high trial completed `view`, one current-top-ten nonseed `prune`,
`answer(none)`, then `DONE`: four generations and four token counts, no retries,
21,658 input and 250 output tokens, and USD0.091632 in usage-based API token fees.
It ended with no pending reservation. That trial returned no thinking blocks;
signed-thinking preservation is covered by synthetic tests. Public extraction and
package checks are recorded separately. These results do not establish fresh
scientific accuracy, full solver equivalence, or a running Discord integration.

Rollback restores the source and matching version together. Retain financial
ledgers, pending reservations, delivery receipts and user configuration. Never
replay a confirmed send or repeat a paid request to reconstruct a receipt.
