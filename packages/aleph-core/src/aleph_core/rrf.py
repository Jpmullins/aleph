"""Reciprocal rank fusion.

Combines several independent rankings into one without needing their scores to
be comparable — which is the whole problem with blending a cosine similarity
against a `ts_rank`. The previous approach, `0.6 * cosine + 0.4 * fts_rank`,
required normalising two quantities that have no shared scale, and the
normalisation it used (divide by the max in the batch) makes a document's score
depend on which other documents happened to be retrieved.

RRF sidesteps that entirely: only the *ordinal position* in each ranking is
used.

    score(d) = Σ_r  1 / (k + rank_r(d))

`k = 60` is the value from Cormack et al. (2009) and is what claude-science's
shipped ranker uses. It is deliberately large relative to typical result-set
sizes, which flattens the curve so that being 1st rather than 3rd in one ranker
does not dominate being present in several. That property is the point: a
document found by two rankers at moderate rank should outrank one found by a
single ranker at the top, because agreement between independent rankings is
better evidence than confidence within one.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence

__all__ = ["DEFAULT_K", "fuse", "rrf_score"]

#: Cormack et al. (2009). Large relative to result-set size on purpose.
DEFAULT_K = 60


def rrf_score(rank: int, *, k: int = DEFAULT_K) -> float:
    """Contribution of a single 1-based rank. Rank 1 is the best."""
    if rank < 1:
        msg = f"rank is 1-based; got {rank}"
        raise ValueError(msg)
    return 1.0 / (k + rank)


def fuse[T: Hashable](
    rankings: Iterable[Sequence[T]],
    *,
    k: int = DEFAULT_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[T, float]]:
    """Fuse ordered rankings into one, best first.

    Each ranking is a sequence ordered best-to-worst; only position matters, so
    the rankers need no shared scale and may return different lengths or
    disjoint sets. An item absent from a ranking contributes nothing from it —
    not a zero score, which would wrongly penalise an item one ranker simply
    never saw.

    ``weights`` scales each ranking's contribution, for the case where one
    ranker is known to be more trustworthy. Omit it and every ranker counts
    equally, which is the right default.

    Ties break on first appearance, so the result is deterministic for a given
    input order rather than depending on dict iteration.
    """
    ranking_list = [list(r) for r in rankings]
    if weights is None:
        weights = [1.0] * len(ranking_list)
    elif len(weights) != len(ranking_list):
        msg = f"got {len(weights)} weights for {len(ranking_list)} rankings"
        raise ValueError(msg)

    scores: dict[T, float] = {}
    first_seen: dict[T, int] = {}
    order = 0
    for ranking, weight in zip(ranking_list, weights, strict=True):
        for position, item in enumerate(ranking, start=1):
            if item not in first_seen:
                first_seen[item] = order
                order += 1
            scores[item] = scores.get(item, 0.0) + weight * rrf_score(position, k=k)

    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
