"""Unit tests for the pure freshness scorer (WP-6 §2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from aleph_wiki.freshness import (
    ClaimCitation,
    _citation_health,
    _halflife_days,
    _recency,
    _source_freshness,
    _verification,
    compute_freshness,
)

NOW = datetime(2026, 7, 4, tzinfo=UTC)


def _page(**kw: Any) -> Any:
    d: dict[str, Any] = {"volatility": "warm", "verified_at": None, "last_compiled_at": None}
    d.update(kw)
    return SimpleNamespace(**d)


def _rev(created_at: datetime) -> Any:
    return SimpleNamespace(created_at=created_at)


# --- dimension 1: recency (half-life decay) ---


def test_recency_halves_per_halflife() -> None:
    hl = _halflife_days("warm")  # 90d
    full = _page(verified_at=NOW)
    one = _page(verified_at=NOW - timedelta(days=90))
    two = _page(verified_at=NOW - timedelta(days=180))
    assert abs(_recency(full, NOW, hl) - 25.0) < 1e-6
    assert abs(_recency(one, NOW, hl) - 12.5) < 1e-6
    assert abs(_recency(two, NOW, hl) - 6.25) < 1e-6


def test_recency_falls_back_to_last_compiled_and_zero_when_absent() -> None:
    hl = _halflife_days("warm")
    fallback = _page(verified_at=None, last_compiled_at=NOW - timedelta(days=90))
    assert abs(_recency(fallback, NOW, hl) - 12.5) < 1e-6
    assert _recency(_page(), NOW, hl) == 0.0


def test_halflife_by_volatility() -> None:
    assert _halflife_days("hot") == 30.0
    assert _halflife_days("warm") == 90.0
    assert _halflife_days("cold") == 365.0
    assert _halflife_days("bogus") == 90.0  # unknown → warm


# --- dimension 2: citation health ---


def test_citation_health_fraction_cited() -> None:
    s1, s2 = uuid4(), uuid4()
    both = [
        ClaimCitation(uuid4(), "cited", (s1,)),
        ClaimCitation(uuid4(), "cited", (s2,)),
    ]
    half = [
        ClaimCitation(uuid4(), "cited", (s1,)),
        ClaimCitation(uuid4(), "uncited", ()),  # no resolvable source
    ]
    assert _citation_health(both, set()) == 25.0
    assert _citation_health(half, set()) == 12.5
    # No claims → no health to award. This line used to assert 25.0 ("vacuously
    # healthy"), which combined with the verification short-circuit to score a
    # claimless page 100 — identical to a fully-cited one. See
    # TestGroundednessDiscriminates.
    assert _citation_health([], set()) == 0.0


def test_citation_health_retracted_source_is_unhealthy() -> None:
    s1 = uuid4()
    only_retracted = [ClaimCitation(uuid4(), "cited", (s1,))]
    assert _citation_health(only_retracted, {s1}) == 0.0


# --- dimension 3: source freshness ---


def test_source_freshness_decays_on_oldest_and_zero_when_empty() -> None:
    hl = _halflife_days("warm")
    versions = [NOW - timedelta(days=90), NOW - timedelta(days=10)]
    assert abs(_source_freshness(versions, NOW, hl) - 12.5) < 1e-6  # oldest wins
    assert _source_freshness([], NOW, hl) == 0.0


# --- dimension 4: verification ---


def test_verification_full_when_verified_after_revision() -> None:
    """A human tick counts — but only over claims that exist to be verified."""
    page = _page(verified_at=NOW)
    rev = _rev(NOW - timedelta(days=1))
    claims = [ClaimCitation(uuid4(), "cited", (uuid4(),))]
    assert _verification(page, rev, claims) == 25.0


def test_verification_of_a_claimless_page_verifies_nothing() -> None:
    """This case previously returned full marks.

    A reviewer ticking "verified" on a page that asserts nothing has verified
    nothing, and letting that award 25 was half of why a claimless page and a
    fully-grounded one both scored 100.
    """
    page = _page(verified_at=NOW)
    rev = _rev(NOW - timedelta(days=1))
    assert _verification(page, rev, []) == 0.0


def test_verification_partial_by_cited_fraction() -> None:
    page = _page(verified_at=None)
    rev = _rev(NOW)
    cites = [
        ClaimCitation(uuid4(), "cited", (uuid4(),)),
        ClaimCitation(uuid4(), "uncited", ()),
    ]
    assert _verification(page, rev, cites) == 12.5
    # No affirmation and no claims → 0.
    assert _verification(page, rev, []) == 0.0


# --- aggregate + retracted override + idempotence ---


def _healthy_inputs() -> dict[str, Any]:
    s1 = uuid4()
    return {
        "page": _page(volatility="warm", verified_at=NOW, last_compiled_at=NOW),
        "revision": _rev(NOW - timedelta(days=1)),
        "citations": [ClaimCitation(uuid4(), "cited", (s1,))],
        "source_versions": [NOW],
        "now": NOW,
        "_s1": s1,  # smuggled for the retracted test
    }


def test_compute_freshness_full_score() -> None:
    kw = _healthy_inputs()
    kw.pop("_s1")
    assert compute_freshness(**kw) == 100


def test_compute_freshness_retracted_source_forces_zero() -> None:
    kw = _healthy_inputs()
    s1 = kw.pop("_s1")
    score = compute_freshness(retracted_source_ids={s1}, **kw)
    assert score == 0


def test_compute_freshness_is_idempotent_and_bounded() -> None:
    kw = _healthy_inputs()
    kw.pop("_s1")
    a = compute_freshness(**kw)
    b = compute_freshness(**kw)
    assert a == b
    assert 0 <= a <= 100


def test_compute_freshness_decayed_middle_score() -> None:
    # warm, verified 90d ago, sources fetched 90d ago, no affirmation-since-edit,
    # 1 cited claim: recency 12.5 + health 25 + source 12.5 + verification 25
    # (verified_at 90d ago is still > revision created_at 120d ago) = 75.
    s1 = uuid4()
    score = compute_freshness(
        page=_page(volatility="warm", verified_at=NOW - timedelta(days=90)),
        revision=_rev(NOW - timedelta(days=120)),
        citations=[ClaimCitation(uuid4(), "cited", (s1,))],
        source_versions=[NOW - timedelta(days=90)],
        now=NOW,
    )
    assert score == 75


class TestGroundednessDiscriminates:
    """E2.3 — a grounded page must score strictly above a claimless one.

    This was measured, not assumed, and the measurement is why the criterion
    exists: on the pre-fix tree both scored **50**. Two vacuous branches
    cancelled out. `_citation_health` returns full marks when there are no
    citations to be unhealthy, and `_verification` returns `0.0` on the same
    empty input — so a page asserting nothing and a page whose every claim is
    cited landed on the same number.

    A freshness score that cannot tell those apart is worse than no score: it
    is displayed next to the page as though it means something, and it ranks a
    page with zero evidence level with one that is fully sourced.
    """

    @staticmethod
    def _page_kwargs(citations: list[ClaimCitation]) -> dict[str, Any]:
        """Identical in every respect except what the page is grounded in."""
        return {
            "page": _page(volatility="warm", verified_at=NOW),
            "revision": _rev(NOW - timedelta(days=1)),
            "citations": citations,
            "source_versions": [NOW],
            "now": NOW,
        }

    def test_grounded_page_outscores_claimless_page(self) -> None:
        s1 = uuid4()
        grounded = compute_freshness(
            **self._page_kwargs([ClaimCitation(claim_id=uuid4(), confidence="cited", source_ids={s1})])
        )
        claimless = compute_freshness(**self._page_kwargs([]))
        assert grounded > claimless, (
            f"a fully-cited page scored {grounded} and a page with no claims at "
            f"all scored {claimless}. The score does not measure groundedness, "
            f"so ranking or filtering on it is meaningless."
        )

    def test_uncited_claims_score_below_cited_ones(self) -> None:
        """Asserting things without evidence must cost, not merely not-help."""
        s1 = uuid4()
        cited = compute_freshness(
            **self._page_kwargs([ClaimCitation(claim_id=uuid4(), confidence="cited", source_ids={s1})])
        )
        uncited = compute_freshness(
            **self._page_kwargs([ClaimCitation(claim_id=uuid4(), confidence="inferred", source_ids=set())])
        )
        assert cited > uncited, (
            f"cited={cited}, inferred-only={uncited} — an unevidenced page is "
            f"not penalised relative to an evidenced one"
        )

    def test_more_grounding_never_lowers_the_score(self) -> None:
        """Monotonicity: adding a cited claim cannot make a page look staler."""
        s1, s2 = uuid4(), uuid4()
        one = compute_freshness(
            **self._page_kwargs([ClaimCitation(claim_id=uuid4(), confidence="cited", source_ids={s1})])
        )
        two = compute_freshness(
            **self._page_kwargs(
                [
                    ClaimCitation(claim_id=uuid4(), confidence="cited", source_ids={s1}),
                    ClaimCitation(claim_id=uuid4(), confidence="cited", source_ids={s2}),
                ]
            )
        )
        assert two >= one, f"adding a second cited claim lowered the score: {one} -> {two}"

    def test_claimless_page_is_not_awarded_full_citation_health(self) -> None:
        """The vacuous branch, asserted directly.

        `all([]) == True` is the shape of this bug wherever it appears: an
        empty collection satisfying a universal quantifier and being scored as
        a success.
        """
        assert _citation_health([], set()) == 0.0, (
            "a page with no citations was given citation health — vacuous "
            "truth scored as evidence, which is why a claimless page and a "
            "fully-cited one both scored 100"
        )
