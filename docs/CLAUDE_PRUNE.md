# Claude and native Richard prune in production chat

Version `0.17.0.dev21+productionprune1` connects the frozen `npmi_prune`
v0.1.2 implementation to the existing Discord conversation surface. Console is
service administration: use its existing model/key setup and `start`. It does not
accept chat questions and no longer offers a manual `prune` command.

Send an ordinary natural-language message through the existing Discord bot entry.
The existing message handler admits the turn, Runtime forwards the question to
`PruneQuestionService`, and the service performs a bounded native Claude JSON
planning request. The host resolves returned literal concept names and target
labels against the actual pinned graph; internal concept IDs are not requested
from the user or exposed as planning catalog inputs. Unknown or ambiguous concepts
and unsupported targets are rejected instead of inventing a mapping or answer.

A grounded plan creates the unchanged graph Session. The native continuation
exposes the package's `view`, subtraction-only `prune`, and `answer` actions,
preserves ordered native content and correlated tool results, and requires a
successful verified answer receipt before returning text. Runtime returns that
text to the original Discord handler for its ordinary send/persistence path.
No user seed, graph-node, target prompt, or separate tool command is required.

The production profile is configured through `runtime.prune_profile_config`.
The referenced existing Claude profile and adjacent private
`<config-stem>.prune.local.json` supply pinned graph/evidence paths and the original
financial binding. The native route supports Anthropic Claude Opus 5.5/high at the
official endpoint. Unsupported profiles fail before a model request. Install the
optional dependency with `pip install '.[prune]'`; graph data are supplied separately.

Planning and continuation share one original cumulative financial ledger. Each
native generation is counted, serial, bounded, and never retried. A conservative
1,000,000-input-token financial reservation is distinct from smaller selected local
execution caps. Verified model and Standard/global usage settle before semantic
validation; missing or unverified usage retains the reservation and closes admission.
Cancellation preserves its primary exception and known charge or unknown hold.
The production route closes unbound background model stages and disables model
knowledge workers, preventing an ordinary-adapter allowance bypass.

The private sidecar has `graph_dir`, `evidence_dir`, `binding`, and `price` fields.
Its binding names the original ledger/file identity, policy, completed opening
attempt, opening charge, ceiling, and successful receipt/hash. Later charges stay
in that ledger. Prices must be positive verified model/endpoint upper bounds with
explicit UTC expiry. Renewal follows [the tariff renewal procedure](PRUNE_PRICE_RENEWAL.md);
no ledger reset, pending-charge discard, or expiry bypass is allowed.

The frozen algorithm does not establish fresh scientific accuracy or full solver
equivalence. Synthetic tests and earlier isolated native trials do not substitute
for acceptance through the actual Discord message interface. Rollback restores
matching source/version while retaining configuration, financial ledger, delivery
receipts, and conversation history. Do not replay a confirmed send or paid request.
