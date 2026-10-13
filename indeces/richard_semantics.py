"""Evidence-scoped abstentions; no chemical parsing or authoritative types.

Case-loss observations were independently derived from the supplied v3.0.1
and source ZIPs. Multiple tokenizations under the supplied element vocabulary
are ambiguity evidence, not proof of a formula's intended meaning. The list
is frozen and incomplete for future corpora. See the delivery semantic review.
Book A5 ch.4 section4.2.3 pp128-129/PDF136-137 and S7 ch.3 section3.3.3
pp111-113/PDF119-121 support fidelity/provenance separation, not chemistry.
"""
SEMANTIC_POLICY = "no_types_case_abstention_v1"
CASE_LOST_LABELS = frozenset({
    "ba0.5sr0.5co0.8fe0.2o3", "baco3", "bias", "ccs", "co", "co2", "co3", "coal",
    "cofe", "cohp", "coo", "coo2", "cop", "cose2", "hco3", "hocl", "hoo", "irni",
    "k2oscl6", "lacoo3", "na2co3", "nahco3", "naso4", "ni", "ni3", "ni3s2", "nicl2",
    "nicl3", "nicl4", "nico2o4", "nife", "niii", "niir", "nimo", "nio", "nip", "nis",
    "nis2", "os", "pdos", "pdsn", "pnas", "psii", "sb", "sn", "sno2", "srcoo2.5",
    "srcoo3", "ptpb", "pt3co", "ho", "co3o4", "coo4", "lanio3", "sno", "srnio3",
})
# These additional named conflicts remain unresolved. Abstaining is preferable
# to promoting formula/name consistency guesses into a semantic equivalence.
UNRESOLVED_LABELS = CASE_LOST_LABELS | {"no", "n2o", "h2o2", "hclo"}


def type_advisory(node):
    return {"cid": node.cid, "released_coarse": node.coarse, "released_fine": node.fine,
            "reviewed_type": "unknown", "type_filtering": "disabled",
            "semantic_policy": SEMANTIC_POLICY}
