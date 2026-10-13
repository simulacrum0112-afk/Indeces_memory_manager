# npmi_prune provenance

The three files under `indeces/_vendor/npmi_prune` are unchanged bytes from the
user-supplied `npmi_prune_v0.1.2.zip` (SHA256
`107a4869fcf5877133a1d6589f5e06b5cb384ed0541ec64ca4258043b15a1f40`).

| File | SHA256 |
|---|---|
| `__init__.py` | `0435c7d5a7e32b480fe936a7adddf974c3bddf4c15b621ceea2e9f526d0cee70` |
| `core.py` | `2e79da4adc37f80a33c07101c4b95d940b58e647c532ecf2f9551e778762d143` |
| `tools.py` | `a1ca377f922882f26ff060efdc17579b8fa981a31d1ff030926157683b9a9dc3` |

The namespace initializer, native Claude loop, factory and financial adapter are
host integration code. They do not alter ranking, pruning, citation verification
or package token-budget semantics. Fixed graph/evidence data and conformance
evaluation files are not included in this repository.
