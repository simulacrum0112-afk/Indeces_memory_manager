# Retrieval coverage policy (0.15.0)

The user authorized a generic repair of length-based mark loss and keyword-density
ranking, retaining Sol/medium, original request budgets, non-fabrication and [M]
provenance. No paper names, answer values, record IDs or corpus-specific synonym
map occur in the implementation. This is deterministic lexical relevance, not a
semantic entailment model or a guarantee of relevance for every future question.

## Original → revised settings

| Setting | Original | Revised | Classification |
|---|---|---|---|
| Static selection | `legacy_v1`, implicit | Runtime `concept_v1`; explicit `legacy_v1` rollback | Task-essential algorithm |
| Direct marks | Four longest literal matches | At most 64, question focus and scope IDF | Task-essential algorithm |
| Match spelling | Literal case behavior | Case, Unicode compatibility, hyphen/space, PDF line-wrap normalization | Task-essential algorithm |
| Body coverage | Label postings only | Label postings plus at most 128 body candidates, at most 32 lexical query terms | Task-essential algorithm |
| Candidate order | Direct count, NPMI, ID | Concept relevance, existing NPMI, ID | Task-essential algorithm |
| References/full quotes | Three; original complete quotes | Three; original complete quotes | Unchanged |
| Summary bytes | 4096, soft prompt and hard rejection | 4096; character schema, compact target, explicit raw-tail fallback | Runtime/context preservation |
| Bounded maintenance grant | Exactly four documents | One to four; three pilot blocks per document | Runtime/recovery |
| Models/reasoning/caps/slots | Sol/medium, existing caps, one slot | Unchanged | Unchanged |

No scientific computing or physical-model parameter changes are included.

## Match and ranking rules

`idf = 1 + log((N+1)/(df+1))`, with active record counts and frequencies from
the same knowledge scope. A matching mark receives its IDF in the question
focus, or 0.15 times its IDF when only in publication identity. Explicit quoted
publication identity is separated from the actual question; formatting requests
are omitted from body query terms. Direct marks are sorted by this weight, not
length. Overlapping phrases count once at ranking time. Stars and the chemical
minus sign remain significant; normalized strings never replace original text.

The added SQLite FTS5 index supplements missing labels from actual body text.
Its scope key is indexed, so another scope cannot enter the posting pool. Pool
selection uses scope IDF presence, without global-corpus BM25 statistics. Original
label identities, frequencies, graph coordinates and source publications remain
authoritative. Derived indexes are built at publication/startup, backed up before
migration, and never repaired by scanning the whole library during a query.

The primary score is:

`metadata_factor * (mark_relevance + body_relevance + answer_form_bonus) + source_prior`.

Mark relevance is 0.25 times the non-overlapping mark weight sum; a label not
literally present in the body receives a further factor of 0.15. Body relevance
adds scope IDF once per matched term. References receive factor 0.25, publication
footers 0.55, DOI/year identity blocks 0.7, and body or ordinary titles 1.0.
Numbered methodological steps do not count as bibliographic entries. Metadata
stays eligible and can win a genuinely relevant title query.

Generic enumeration questions receive a 10-point bonus for a matching list form;
definition questions receive six for a matching definition form. These are
heuristics, not validated semantic support. A source prior is 12 for an explicit
DOI or quoted title, otherwise four for an explicit year found in source identity.
Identity consists of the first 4000 unique source characters, up to 4000
publication metadata characters, and the bound source path. Anaphoric questions
such as “this analysis” may inherit missing identity from the immediately preceding
user question. An explicit new identity takes precedence; ordinary questions do
not inherit. The contextual query is frozen in the new audit contract.

Existing NPMI arithmetic, one-hop/context gates, two extra marks, directed graph
IDs, static/dynamic separation, self-name exclusions and reply instructions remain
unchanged. Static queries do not update dynamic shadow. Relevance and graph scores
are separately recorded; the validator recomputes IDF, arithmetic, order and
selected-body/source decisions. Old receipts retain their old arithmetic. A saved
event cannot be reinterpreted under another selection policy or contextual input.

## Summary and diagnostic audit

The schema permits at most `byte_limit // 4` Unicode scalars. Generation receives
a target of 75% of that character bound, and must compress chronology into complete
sentences. The local 4096-byte check remains authoritative. A string reaching the
schema boundary is rejected for checkpoint purposes even if its byte size fits:
an initial live trial showed the provider could finish JSON after clipping the
summary sentence at that boundary. No clipped summary advances source coverage.

On byte overflow or boundary collision, the prior checkpoint and all original rows
remain intact. The reply can use a contiguous tail of whole raw interactions within
the original input capacity, with an explicit incomplete-continuity warning and a
scratch receipt of retained/omitted sequence IDs. There is no automatic retry or
second generation. Malformed JSON and empty summaries still fail validation.

Independent trials have explicit diagnostic start/end events. They are validated
against the real call lifecycle and transport, separately from chat turns. Reply
trials bind exact instructions, inputs, output, usage and frozen citations. The old
summary-deadline trial remains compatible. A missing chat `turn_start` is still
invalid without retention evidence. Trials cannot claim Discord delivery or a
checkpoint write.

The supported Responses string constraints are described in the official
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
Local checks handle provider boundary behavior as well.

## Validation and limits

Synthetic regression covers short concepts, morphological aliases, unlabelled
body hits, metadata density, legitimate titles, method numbering, enumeration,
source anaphora, scope isolation, archives, publication invalidation, rollback,
immutable event policy, score tampering, summary overflow/boundary preservation
and diagnostic classification. Full-suite and wheel receipts are linked in
`CHECKPOINT.md`; private source data never enter those receipts or Git.

The original eight retained gold questions were replayed once each against the
actual model with original reply budgets. The exact input quotes and visible
outputs were inspected. Each acquired a relevant answer or explicit negative
evidence, with resolved [M] markers. Some secondary formulas/captions still rank
below three; a more complete answer passage supplied the needed evidence instead.
The paired private record ranks and excerpts remain in managed scratch, not this
public document. The comparison uses original retained receipts as “before”; the
“after” library also includes the separately authorized restored document, which
can slightly change scope frequencies.

The restored document completed its pilot and full bounded publication without new
failures or unknown usage. Old data/checkpoints and original PDF digest were
checked; original unknown usage remains unknown. An old four-document wording in
the first single-document grant receipt was preserved and clarified by an additive
operator-scope event; new grants use selected-source wording.

Two original failing summary inputs were tried under the final compact prompt:
both returned complete sentences below byte and character boundaries. Earlier
boundary-clipped trial outputs remain recorded and were never checkpoints.
Diagnostic model trials did not connect Discord, deliver a message, or prove an
existing Console loaded this source. PDF extraction, equations and semantic
entailment are not certified by hashes, citations or these limited trials.

Rollback selects `runtime.retrieval_policy="legacy_v1"` under the state lease after
the service stops. Original records and graph parameters are compatible; additional
derived tables can remain. Do not roll back by replacing or deleting the database,
discarding publication history, or reusing a concept-policy event ID as legacy.
