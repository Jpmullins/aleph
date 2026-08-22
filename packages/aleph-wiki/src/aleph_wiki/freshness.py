"""Deterministic wiki-page freshness scoring (WP-6 §2).

``compute_freshness`` is a **pure function** returning an int 0-100 as the sum of
four 0-25 dimensions:

- **Recency** — half-life decay on ``verified_at`` (fallback ``last_compiled_at``),
  half-life by volatility (hot 30d / warm 90d / cold 365d): ``25 * 0.5**(age/halflife)``.
- **Citation health** — fraction of the page's claims that resolvably cite a
  non-retracted source, scaled to 25.
- **Source freshness** — half-life decay (same volatility half-life) on the
  *oldest* contributing ``SourceVersion.fetched_at``.
- **Verification** — 25 if ``verified_at`` is set and newer than the current
  revision's ``created_at`` (a human/agent affirmed the page since its last
  edit); otherwise a partial by the page's cited-claim fraction.

**Retracted-source override:** if *any* contributing source is retracted the
whole score is forced to 0 — a retracted source poisons the page regardless of
the other dimensions (WP-6 §2 + §4).

Deterministic: ``now`` is injected; no wall-clock reads, no DB.

Currently UNPINNED. The property tests went with the wiki test suite in the
harness reset, including the regression test for the defect where a grounded
page and a claimless one both scored 50. CLAUDE.md records the same gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from aleph_core.confidence import Confidence

#: The confidence states in which the evidence stands behind the claim.
#: `CONTESTED` is deliberately absent — evidence pointing both ways is not
#: verification — and so is `UNDER_INVESTIGATION`, which is the absence of a
#: judgement rather than a negative one.
_SUPPORTED = frozenset({Confidence.WEAKLY_SUPPORTED.value, Confidence.WELL_SUPPORTED.value})

if TYPE_CHECKING:
    from aleph_wiki.models import WikiPage, WikiRevision

#: Freshness half-life (days) per volatility class. Unknown → warm.
_HALFLIFE_DAYS: dict[str, float] = {"hot": 30.0, "warm": 90.0, "cold": 365.0}

#: Per-dimension ceiling; four dimensions sum to the 0-100 score.
_DIM_MAX = 25.0

_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class ClaimCitation:
    """One of a page's claims, with the sources it resolvably cites.

    ``source_ids`` are the (non-null) ``Source`` ids this claim cites through a
    ``Citation`` → ``SourcePage`` → ``Source`` chain; an empty tuple is an
    uncited claim. ``confidence`` is the ``WikiClaim.confidence`` string — a
    member of ``aleph_core.confidence.Confidence``.
    """

    claim_id: UUID
    confidence: str = Confidence.UNDER_INVESTIGATION.value
    source_ids: tuple[UUID, ...] = ()


def _halflife_days(volatility: str) -> float:
    return _HALFLIFE_DAYS.get(volatility, _HALFLIFE_DAYS["warm"])


def _decay(anchor: datetime | None, now: datetime, halflife_days: float) -> float:
    """``_DIM_MAX * 0.5**(age/halflife)``; 0 when there is no anchor timestamp."""
    if anchor is None:
        return 0.0
    age_days = max(0.0, (now - anchor).total_seconds() / _SECONDS_PER_DAY)
    return _DIM_MAX * (0.5 ** (age_days / halflife_days))


def _recency(page: WikiPage, now: datetime, halflife_days: float) -> float:
    anchor = page.verified_at or page.last_compiled_at
    return _decay(anchor, now, halflife_days)


def _citation_health(citations: list[ClaimCitation], retracted_source_ids: set[UUID]) -> float:
    if not citations:
        # A page that asserts nothing has no citation health to measure, and
        # awarding it full marks made it indistinguishable from a fully-cited
        # one: both scored 100. `all([]) is True` is the shape of this bug
        # wherever it appears — an empty collection satisfying a universal
        # quantifier and being *scored* as a success.
        #
        # Zero is the honest reading. Freshness drives refresh prioritisation,
        # so an unevidenced page ranking as stale is also the useful direction.
        return 0.0
    healthy = sum(
        1 for c in citations if any(sid not in retracted_source_ids for sid in c.source_ids)
    )
    return _DIM_MAX * healthy / len(citations)


def _source_freshness(
    source_versions: list[datetime], now: datetime, halflife_days: float
) -> float:
    if not source_versions:
        return 0.0
    return _decay(min(source_versions), now, halflife_days)


def _verification(
    page: WikiPage,
    revision: WikiRevision | None,
    citations: list[ClaimCitation],
) -> float:
    if not citations:
        # Nothing to verify. Checked BEFORE the human-verification
        # short-circuit below, because a reviewer ticking "verified" on a page
        # that makes no claims has verified nothing — and letting that award
        # full marks was the second half of why a claimless page scored the
        # same as a fully-grounded one.
        return 0.0
    if (
        page.verified_at is not None
        and revision is not None
        and page.verified_at > revision.created_at
    ):
        return _DIM_MAX
    # WS-RS9: was `c.confidence == "cited"`, a word the confidence column no
    # longer holds and that the state machine could never have produced. The
    # question this dimension asks is "does the evidence stand behind the
    # claim", and the two states that answer yes are the two supported ones.
    # Matching on a string the migration renamed would have scored every page
    # zero on this dimension with nothing reporting a problem.
    supported = sum(1 for c in citations if c.confidence in _SUPPORTED)
    return _DIM_MAX * supported / len(citations)


def compute_freshness(
    *,
    page: WikiPage,
    revision: WikiRevision | None,
    citations: list[ClaimCitation],
    source_versions: list[datetime],
    retracted_source_ids: set[UUID] | None = None,
    now: datetime,
) -> int:
    """Compute the page's 0-100 freshness score. Pure + deterministic.

    ``retracted_source_ids`` is the set of contributing ``Source`` ids that are
    currently retracted; a non-empty intersection with the page's contributing
    sources forces the score to 0.
    """
    retracted = set(retracted_source_ids or ())
    contributing: set[UUID] = set()
    for c in citations:
        contributing.update(c.source_ids)
    # Retracted source poisons the whole page (WP-6 §2).
    if contributing & retracted:
        return 0

    halflife = _halflife_days(page.volatility)
    score = (
        _recency(page, now, halflife)
        + _citation_health(citations, retracted)
        + _source_freshness(source_versions, now, halflife)
        + _verification(page, revision, citations)
    )
    return max(0, min(100, round(score)))
