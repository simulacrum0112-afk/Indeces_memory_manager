# Renewing a verified native-prune price snapshot

The native prune ledger stores its trusted price expiry in the canonical
`prune_meta.policy`. A private caller sidecar contains the matching
`PrunePriceEnvelope`. Expiry rejects new admissions; changing only the sidecar
also rejects admission because the persisted policy no longer matches.

After independently checking the official model pricing, free token counting,
Standard service tier and global inference, the controlled operator can call:

```python
receipt = PruneBudget.renew_existing_price(
    binding, previous_price, newly_verified_price, offline_accepted=True
)
```

Both envelopes must have exactly the same positive input/output rates, model
and API base URL. The new UTC expiry must be later than the old expiry and the
current time. This operation validates the original grant provenance, receipt,
file identity, exact schema and all accounting under the existing writer lease
and one SQLite `BEGIN IMMEDIATE` transaction. It requires no pending or
unfinished rounds and no halt. It updates only the prune policy expiry, keeping
the original session policy, ceiling, spent balance, completed attempt, round
history and file identity unchanged. It performs no model request.

Save the returned old/new policy hashes and unchanged accounting metadata in
the private operator receipt, then atomically update the matching private
sidecar envelope. If interrupted between these steps, an old sidecar fails
closed; reconcile the durable policy and renewal receipt before use. Do not
repeat a renewal blindly after an uncertain commit. A changed tariff requires
a separately reviewed historical price-epoch migration; this operation will
not reprice earlier calls or clear unknown usage or halt state.
