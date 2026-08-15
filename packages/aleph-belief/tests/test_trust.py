"""The trust lattice decides who may overwrite whom."""

from __future__ import annotations

from itertools import pairwise

import pytest

from aleph_belief.trust import TrustTier, outranks, violates_trust_tier


def test_lattice_is_totally_ordered() -> None:
    ladder = [TrustTier.UNVERIFIED, TrustTier.ASSERTED, TrustTier.EARNED, TrustTier.SIGNED]
    for lower, higher in pairwise(ladder):
        assert outranks(higher, lower)
        assert not outranks(lower, higher)


def test_equal_tiers_may_overwrite() -> None:
    """A later extraction pass must be able to update its own earlier output."""
    for tier in TrustTier:
        assert outranks(tier, tier)
        assert not violates_trust_tier(incoming=tier, existing=tier)


def test_asserted_cannot_overwrite_earned() -> None:
    """The load-bearing guarantee: LLM curation cannot silently take over corpus-backed belief."""
    assert violates_trust_tier(incoming=TrustTier.ASSERTED, existing=TrustTier.EARNED)
    assert not violates_trust_tier(incoming=TrustTier.EARNED, existing=TrustTier.ASSERTED)


def test_signed_overwrites_everything() -> None:
    for tier in TrustTier:
        assert not violates_trust_tier(incoming=TrustTier.SIGNED, existing=tier)


@pytest.mark.parametrize(
    ("incoming", "existing"),
    [
        (None, TrustTier.EARNED),
        (TrustTier.EARNED, None),
        (None, None),
    ],
)
def test_unknown_tier_is_refused_not_defaulted(
    incoming: TrustTier | None, existing: TrustTier | None
) -> None:
    """An absent tier is unrankable. Refusing is the whole point of the guard."""
    assert violates_trust_tier(incoming=incoming, existing=existing)
