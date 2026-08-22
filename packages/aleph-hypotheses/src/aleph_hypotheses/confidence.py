"""Confidence-state machine for hypotheses and claims.

States:
  under_investigation → weakly_supported → well_supported
                     \\→ contested ↘
                                   refuted | abandoned

The alphabet lives in :mod:`aleph_core.confidence` — the leaf — so the A2UI
catalog, the HTML compiler and the ``wiki_claims.confidence`` column can all
name the same six values without depending on this package. This module owns the
*transitions*; it no longer owns the words. ``Confidence`` is re-exported here
because every existing caller imports it from this module.

Transitions are driven by aggregated evidence:
  net_support = sum(weight * sign(stance)) where stance ∈ {supports: +1,
                                                            contradicts: -1,
                                                            contextualizes: 0}

Thresholds:
  net ≥  3 and one piece of evidence weighing ≥ 1.5  → well_supported
  net ≥  1                                           → weakly_supported
  net ≤ -1                                           → contested
  net ≤ -3                                           → refuted
  zero evidence                                      → under_investigation

**Where the weights come from, and why the top state used to be unreachable.**
The 1.5 in the first rule exists so that ``well_supported`` requires at least one
piece of evidence that is better than "somebody attached a citation" — three weak
supports should not out-vote one grounded one. But every writer in the tree
stamped ``weight = 1.0``, so ``max_pos`` never reached 1.5 and ``WELL_SUPPORTED``
was structurally unreachable: 850 live claims, not one of them in the top state,
with no error anywhere. :func:`weight_for_tier` is the missing half — it turns
the ``aleph_belief`` provenance lattice into the number this machine reads, and
``TrustTier.EARNED`` (a quote verified verbatim against ingested source text) is
worth exactly the threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from aleph_belief.trust import TrustTier
from aleph_core.confidence import Confidence

__all__ = [
    "HIGH_TIER_WEIGHT",
    "TIER_WEIGHTS",
    "Confidence",
    "EvidenceRow",
    "next_confidence_from_evidence",
    "weight_for_tier",
]


#: The weight one piece of evidence must carry, on its own, before a claim may
#: reach ``WELL_SUPPORTED``. Named rather than inlined because it is the same
#: number as ``TIER_WEIGHTS[TrustTier.EARNED]`` by design, and the assertion
#: below pins that so the two cannot drift apart.
HIGH_TIER_WEIGHT = 1.5


#: How much a piece of evidence counts, by how it came to be believed.
#:
#: The lattice itself (``aleph_belief.trust.TrustTier``) answers *who put this
#: here*; this table is the only place that turns that into arithmetic. The
#: shape matters more than the exact numbers:
#:
#: * ``UNVERIFIED`` (bookkeeping with no corpus backing) counts, but two of them
#:   do not add up to one real citation;
#: * ``ASSERTED`` (an agent's judgement during curation) is the old universal
#:   default, and is deliberately still 1.0 so nothing already written moves;
#: * ``EARNED`` (a quote located verbatim in ingested source text by
#:   ``aleph_core.grounding.ground``) is exactly ``HIGH_TIER_WEIGHT`` — this is
#:   the bar ``well_supported`` was written to require;
#: * ``SIGNED`` (affirmed by an authenticated human) outranks all of it.
TIER_WEIGHTS: dict[TrustTier, float] = {
    TrustTier.UNVERIFIED: 0.5,
    TrustTier.ASSERTED: 1.0,
    TrustTier.EARNED: HIGH_TIER_WEIGHT,
    TrustTier.SIGNED: 2.0,
}

# A tier added to the lattice with no weight here would fall through
# `weight_for_tier` and raise a KeyError at whatever call site happened to hit it
# first — or, worse, be given a default and quietly counted as an agent's
# opinion. Fail at import, where the missing entry is the whole message. Not an
# `assert`: `python -O` strips those, and a guard that vanishes under a flag is
# not a guard.
if set(TIER_WEIGHTS) != set(TrustTier):
    _missing = sorted(t.value for t in TrustTier if t not in TIER_WEIGHTS)
    _MSG = f"TrustTier members with no evidence weight: {_missing}"
    raise RuntimeError(_MSG)


def weight_for_tier(tier: TrustTier | None) -> float:
    """Evidence weight for a provenance tier.

    ``None`` is UNKNOWN, not EARNED: the trust lattice is explicit that a
    missing tier is unrankable, so it scores as an assertion rather than as
    something the corpus earned. Defaulting the other way is how an unchecked
    citation would carry a claim to ``well_supported``.
    """
    if tier is None:
        return TIER_WEIGHTS[TrustTier.ASSERTED]
    return TIER_WEIGHTS[tier]


@dataclass(frozen=True)
class EvidenceRow:
    stance: str  # "supports" | "contradicts" | "contextualizes"
    weight: float

    @classmethod
    def at_tier(cls, stance: str, tier: TrustTier | None) -> EvidenceRow:
        """Build a row whose weight comes from the trust lattice, not a literal."""
        return cls(stance=stance, weight=weight_for_tier(tier))


def next_confidence_from_evidence(
    evidence: Iterable[EvidenceRow],
) -> Confidence:
    pos = 0.0
    neg = 0.0
    max_pos = 0.0
    for e in evidence:
        if e.stance == "supports":
            pos += e.weight
            max_pos = max(max_pos, e.weight)
        elif e.stance == "contradicts":
            neg += e.weight
    net = pos - neg
    if not pos and not neg:
        return Confidence.UNDER_INVESTIGATION
    if net <= -3:
        return Confidence.REFUTED
    if net <= -1:
        return Confidence.CONTESTED
    if net >= 3 and max_pos >= HIGH_TIER_WEIGHT:
        return Confidence.WELL_SUPPORTED
    if net >= 1:
        return Confidence.WEAKLY_SUPPORTED
    return Confidence.UNDER_INVESTIGATION
