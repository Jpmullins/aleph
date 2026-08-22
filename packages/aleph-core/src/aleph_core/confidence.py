"""The one confidence vocabulary.

How confident the system is in a claim used to be written down in three
vocabularies that did not agree. The derived engine
(``aleph_hypotheses.confidence.next_confidence_from_evidence``) emitted six
underscore-spelled words. The A2UI catalog permitted six *different* words, two
of them hyphen-spelled. The HTML compiler mapped some of each and had no styling
for three of the engine's states. And 806 of the 850 claims in the live database
carried ``"cited"``, which is a member of none of those sets — it is what the
old write path asserted, not what any evidence implied.

A knowledge layer whose confidence field means three things depending on which
component reads it is not a knowledge layer, so this module is the single
definition. It lives in ``aleph-core`` — the leaf — because every package that
renders, stores or derives a confidence has to be able to import it without
inverting the dependency DAG: ``aleph-a2ui`` (the catalog and the card builders)
and ``aleph-wiki`` (the column and the HTML compiler) cannot depend on
``aleph-hypotheses``, where the state machine lives.

The **values** are the state machine's, unchanged. ``aleph_hypotheses`` still
owns the transitions; it just no longer owns the alphabet.

Enforced by ``scripts/check-confidence-vocabulary.sh``, which diffs this enum
against the catalog, the web renderer and the HTML compiler.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CONFIDENCE_VALUES",
    "LEGACY_CONFIDENCE",
    "Confidence",
    "is_canonical_confidence",
]


class Confidence(StrEnum):
    """What the evidence says about a claim. Derived, never asserted.

    This is NOT a status. A claim resting on a source that was withdrawn is
    ``retracted`` in ``WikiClaim.status``; what it is now *worth* is whatever
    its remaining evidence says, which is one of these six. Putting
    ``"retracted"`` in the confidence column — as the old A2UI catalog allowed —
    writes a value the state machine can never produce, so the next recompute
    silently erases it.
    """

    #: No evidence either way yet. The starting state, and where a claim
    #: returns when its only support is withdrawn.
    UNDER_INVESTIGATION = "under_investigation"
    #: Net positive evidence, but nothing corpus-grounded enough to carry it.
    WEAKLY_SUPPORTED = "weakly_supported"
    #: Net positive evidence including at least one high-trust-tier piece.
    WELL_SUPPORTED = "well_supported"
    #: Evidence points both ways.
    CONTESTED = "contested"
    #: Evidence points decisively against.
    REFUTED = "refuted"
    #: Withdrawn by a person. The only state no evidence can produce, and so
    #: the only one a recompute must never overwrite on its own.
    ABANDONED = "abandoned"


#: Declaration order, as strings. The sweep and the catalog compare against
#: this, so the order is part of the contract: a reader diffing two lists
#: should see a genuine difference, not a reordering.
CONFIDENCE_VALUES: tuple[str, ...] = tuple(c.value for c in Confidence)


#: Every spelling the tree wrote before there was one vocabulary, and what it
#: becomes. Used by the data migration
#: (``20260822_..._rs9_one_confidence_vocabulary``) and by the sweep, which
#: names the legacy spelling it found rather than only saying "mismatch".
#:
#: The two judgement calls, stated rather than buried:
#:
#: * ``cited`` meant "this claim has at least one citation attached", with no
#:   look at what the citation said or how well it grounded. That is exactly
#:   ``weakly_supported`` — net positive evidence, nothing that has earned more.
#:   Mapping it to ``well_supported`` would promote 806 unexamined rows.
#: * ``retracted`` was never a confidence. A withdrawn support leaves the claim
#:   with no standing evidence, which is ``under_investigation``; the retraction
#:   itself is recorded in ``WikiClaim.status`` by ``aleph_reviewer.retraction``.
LEGACY_CONFIDENCE: dict[str, Confidence] = {
    "cited": Confidence.WEAKLY_SUPPORTED,
    "uncited": Confidence.UNDER_INVESTIGATION,
    "well-supported": Confidence.WELL_SUPPORTED,
    "weakly-supported": Confidence.WEAKLY_SUPPORTED,
    "retracted": Confidence.UNDER_INVESTIGATION,
    "initial": Confidence.UNDER_INVESTIGATION,
}


def is_canonical_confidence(value: str) -> bool:
    """True when ``value`` is a member of the canonical vocabulary."""
    return value in CONFIDENCE_VALUES
