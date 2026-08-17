"""Provenance trust tiers for belief nodes.

Ported from graphify (MIT, Copyright (c) 2026 Safi Shamsi) —
``src/ontology-patch.ts`` ``OntologyNodeTrustTier`` and ``violatesTrustTier``.

The tier answers one question: *how did this node come to be believed?* It
exists so that an assertion can never quietly take over a node the corpus
earned. A claim extracted from ingested source text (``EARNED``) outranks a
claim an LLM curator asserted during a merge (``ASSERTED``); a patch that would
downgrade the former to the latter is rejected rather than applied.

This is deliberately NOT the same axis as confidence. Confidence is *how well
supported* a belief is and is derived from its evidence edges; trust is *who
put it there* and is a fixed property of the writer. A well-supported asserted
claim and a poorly-supported earned claim are both coherent states.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "TrustTier",
    "outranks",
    "violates_trust_tier",
]


class TrustTier(StrEnum):
    """How a belief node came to be believed, ordered weakest to strongest.

    ``UNVERIFIED`` — coordination/bookkeeping evidence with no corpus backing.
    ``ASSERTED``   — written by an agent's judgment (LLM curation, adjudication).
    ``EARNED``     — extracted from, and verbatim-groundable in, ingested source text.
    ``SIGNED``     — affirmed by an authenticated human principal.

    The absence of a tier is UNKNOWN and is **not** the same as ``EARNED``;
    callers must treat a missing tier as unrankable rather than defaulting it.
    """

    UNVERIFIED = "unverified"
    ASSERTED = "asserted"
    EARNED = "earned"
    SIGNED = "signed"


# Explicit rank rather than declaration order: StrEnum ordering is not
# semantic, and a future tier inserted in the middle must not silently
# renumber the lattice.
_RANK: dict[TrustTier, int] = {
    TrustTier.UNVERIFIED: 0,
    TrustTier.ASSERTED: 1,
    TrustTier.EARNED: 2,
    TrustTier.SIGNED: 3,
}


def outranks(candidate: TrustTier, incumbent: TrustTier) -> bool:
    """True when ``candidate`` may overwrite a node currently held at ``incumbent``."""
    return _RANK[candidate] >= _RANK[incumbent]


def violates_trust_tier(
    *,
    incoming: TrustTier | None,
    existing: TrustTier | None,
) -> bool:
    """True when writing ``incoming`` over ``existing`` would be a downgrade.

    An unknown tier on either side is unrankable, so the write is refused: we
    cannot prove it is safe, and silently allowing it is exactly the failure
    this guard exists to prevent.
    """
    if incoming is None or existing is None:
        return True
    return not outranks(incoming, existing)
