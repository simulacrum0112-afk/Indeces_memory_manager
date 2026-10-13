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
This add-on does not connect the tool loop to Console, Discord, background workers,
or active/path requery. Claude's ordinary controlled-session wrapper remains
unsupported and refuses activation. A source update does not start a service.

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
