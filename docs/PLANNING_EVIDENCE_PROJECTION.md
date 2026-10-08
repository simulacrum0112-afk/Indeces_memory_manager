# Planning evidence projection smoke fix

Version `0.17.0.dev5` removes duplicate evidence from active-query planning.
Previously every planning request contained the complete cumulative materials
twice: in the outer `evidence` value and in historical `memory_citations`.
After one real appended retrieval, the next planning input counted 4332 tokens
and was rejected before generation by the unchanged 4096-token stage limit.

Planning now keeps every complete quote and stable citation in outer `evidence`.
Historical context instead contains a versioned `memory_citations_ref` identifying
that field and its canonical SHA-256 digest. Other historical fields and the
original question remain intact. Final reply context still carries the complete
materials. No graph ranking, association-to-relation rule, retrieval limit,
scientific parameter or default stage budget changes.

The audit accepts both the historical two-copy representation and the new strict
reference representation. New references must match schema, integer version,
field and digest, and cannot coexist with a second full material copy. Outer
evidence must still match the frozen source versions and append chain; a matching
reference alone does not establish relevance or semantic support.

The owner's local configuration separately overrides only query output from 512
to 2048. Query input remains 4096, time remains 15 seconds and reasoning remains
medium. The cumulative message envelope remains 4096 output tokens, six admitted
calls and 60 seconds; later requests can be narrowed by actual remaining usage.
Query reserves 256 output tokens for a reply, rather than guaranteeing its full
2048-token configured maximum. No deadline or cumulative cap is increased.

The original 512-token real smoke stopped at `max_output_tokens` with known usage.
The 2048-token follow-up completed a 640-token planning action and an actual
static local retrieval, then exposed the duplicated-input problem above. Both
failed experiment receipts are retained. Offline synthesis and real API runs
are recorded separately; locally captured delivery is not Discord delivery.

Before this fix, 256 selected offline tests passed on the dev4 source. After the
fix, the two affected modules pass 53 tests, including the combined diagnostics,
new-reference tampering and old full-history compatibility. These are separate
test runs, not a combined full-suite result. Early harness/assertion failures are
preserved separately and corrected in their respective external receipts.

Deployment validation and exact source/package identities are stored in the
adjacent private build receipts. No private config, credentials, source documents,
stored evidence, model output or runtime database belongs in this commit.
To roll back, normally stop and clean up the owned service, restore the verified
prior package/launcher and exact configuration backup, and explicitly start.
Retain failed/new turn records; do not reset the knowledge index or usage ledger.
