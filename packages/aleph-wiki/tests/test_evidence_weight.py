"""A citation this service writes weighs what a GROUNDED citation weighs.

WS-RS9's reviewer found this by mutation and it survived everything: changing

    weight: float = weight_for_tier(TrustTier.EARNED)     # 1.5
    weight: float = 1.0                                   # TrustTier.ASSERTED

left 1,293 unit, 161 integration and 53 e2e tests green — while restoring the
exact defect the workstream exists to remove.

The default is not cosmetic. `WELL_SUPPORTED` requires one piece of evidence
weighing at least 1.5, and while every writer produced a flat 1.0 that state was
STRUCTURALLY UNREACHABLE: the confidence machine had a top tier nothing could
ever reach, and nothing said so.

`EARNED` is the honest tier because it is the one the write path enforces.
`_attach_evidence` refuses any quote `ground()` cannot locate verbatim in its
source, so a citation that exists has had its text checked against the document
— which is what EARNED means, and is strictly more than an agent's say-so.
"""

from __future__ import annotations

from uuid import uuid4

from aleph_belief.trust import TrustTier
from aleph_hypotheses.confidence import weight_for_tier
from aleph_wiki.belief_service import EvidenceDraft


def test_the_default_weight_is_the_earned_tier_not_a_flat_one() -> None:
    draft = EvidenceDraft(source_id=uuid4(), quote="q", source_text="q")
    assert draft.weight == weight_for_tier(TrustTier.EARNED)
    assert draft.weight != weight_for_tier(TrustTier.ASSERTED)


def test_the_default_clears_the_bar_well_supported_requires() -> None:
    """The property that makes the top confidence state reachable at all.

    Asserted against the threshold rather than against the literal 1.5, so
    moving either one alone fails here instead of silently making the state
    unreachable again.
    """
    from aleph_hypotheses.confidence import HIGH_TIER_WEIGHT

    draft = EvidenceDraft(source_id=uuid4(), quote="q", source_text="q")
    assert draft.weight >= HIGH_TIER_WEIGHT


def test_a_caller_can_still_weigh_a_citation_down() -> None:
    """EARNED is the default, not a floor. Weaker evidence must be recordable."""
    draft = EvidenceDraft(
        source_id=uuid4(),
        quote="q",
        source_text="q",
        weight=weight_for_tier(TrustTier.ASSERTED),
    )
    assert draft.weight == weight_for_tier(TrustTier.ASSERTED)


def test_the_production_extractor_does_not_override_the_default() -> None:
    """`claim_extraction` is the one production constructor of `EvidenceDraft`.

    If it passed an explicit weight, the default above would be dead code and
    this whole property would be decided somewhere else — which is how the
    reviewer's mutation survived in the first place.
    """
    import ast
    import pathlib

    src = pathlib.Path("packages/aleph-wiki/src/aleph_wiki/claim_extraction.py")
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "EvidenceDraft"
        ):
            named = {kw.arg for kw in node.keywords}
            assert "weight" not in named, (
                "the extractor sets its own weight, so EvidenceDraft's default is "
                "dead and the EARNED tier is decided somewhere unpinned"
            )
