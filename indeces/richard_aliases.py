"""Conservative, corpus-bound lexical rules for the Richard v3 shadow layer.

These reviewed rules preserve element case. They do not infer chemistry,
assign types, add chemical synonym pairs, or modify native mark identity.
Released membership is read separately from the immutable provenance loader.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ALIAS_POLICY = "lexical_case_preserving_v1"
_SCRIPT_DIGITS = str.maketrans("⁺⁻₀₁₂₃₄₅₆₇₈₉₊₋", "+-0123456789+-")
_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—−"})
_SPELLINGS = {"centre": "center", "ionisation": "ionization", "polarisation": "polarization", "electrolyser": "electrolyzer"}


def _lexical(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError("invalid lexical mark")
    value = unicodedata.normalize("NFC", name).strip().translate(_SCRIPT_DIGITS).translate(_DASHES)
    value = re.sub(r"\s+", " ", value)
    # Compact upper/mixed-case identifiers and charge/numeric tokens remain
    # case-sensitive. This is syntactic protection, not a formula parser.
    compact = not any(c.isspace() for c in value) or re.fullmatch(r"\S+\s+[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]*[+-]", value)
    if compact and (any(c.isupper() for c in value)
            or any(c.isdigit() or c in "+-*()[]" for c in value)):
        return re.sub(r"\s+([+-])$", r"\1", value)
    value = value.lower()
    return re.sub(r"\b(?:centre|ionisation|polarisation|electrolyser)\b", lambda m: _SPELLINGS[m[0]], value)


@dataclass(frozen=True)
class LexicalAliasPolicy:
    """A frozen corpus supplies plural evidence; no mutable global name set."""

    existing_names: frozenset[str]

    def __init__(self, existing_names):
        object.__setattr__(self, "existing_names", frozenset(_lexical(name) for name in existing_names))

    def canonical(self, name: str) -> str:
        value = _lexical(name)
        if len(value) > 4 and re.fullmatch(r"[a-z -]+", value):
            if value.endswith("ies") and len(value) > 5:
                singular = value[:-3] + "y"
            elif value.endswith("sses"):
                singular = value[:-2]
            elif value.endswith("s") and not value.endswith(("ss", "us", "is", "sis")):
                singular = value[:-1]
            else:
                singular = value
            if singular in self.existing_names:
                # Only one explicit corpus relation is considered. Reject
                # chains that could make repeated canonicalization drift.
                if singular.endswith("s") and singular[:-1] in self.existing_names:
                    return value
                value = singular
        return value
