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
    # No claims → vacuously healthy.
    assert _citation_health([], set()) == 25.0


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
    page = _page(verified_at=NOW)
    rev = _rev(NOW - timedelta(days=1))
    assert _verification(page, rev, []) == 25.0


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
