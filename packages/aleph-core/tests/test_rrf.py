"""Reciprocal rank fusion — the properties that make it worth using."""

from __future__ import annotations

import pytest

from aleph_core.rrf import DEFAULT_K, fuse, rrf_score


def test_a_single_ranking_is_preserved() -> None:
    assert [item for item, _ in fuse([["a", "b", "c"]])] == ["a", "b", "c"]


def test_agreement_beats_confidence() -> None:
    """THE property, and the reason RRF is used instead of a weighted sum.

    A document two independent rankers both place mid-list outranks one that a
    single ranker puts first. Agreement between rankings is better evidence than
    confidence within one — which a score blend cannot express, because it has
    no way to know the two scores mean different things.
    """
    lexical = ["top-of-lexical", "agreed", "x"]
    dense = ["y", "agreed", "z"]
    ranked = [item for item, _ in fuse([lexical, dense])]
    assert ranked[0] == "agreed", ranked


def test_absence_is_not_a_zero_score() -> None:
    """An item one ranker never saw must not be penalised as if it ranked last."""
    only_in_first = fuse([["a"], ["b", "c", "d", "e", "f"]])
    scores = dict(only_in_first)
    assert scores["a"] == pytest.approx(rrf_score(1))
    assert scores["b"] == pytest.approx(rrf_score(1))


def test_k_flattens_the_curve() -> None:
    """With k=60, 1st vs 3rd is a small edge — that is what lets agreement win."""
    gap_large_k = rrf_score(1, k=60) - rrf_score(3, k=60)
    gap_small_k = rrf_score(1, k=1) - rrf_score(3, k=1)
    assert gap_large_k < gap_small_k / 10


def test_default_k_is_sixty() -> None:
    """Cormack et al. (2009); also what claude-science's shipped ranker uses."""
    assert DEFAULT_K == 60
    assert rrf_score(1) == pytest.approx(1.0 / 61)


def test_disjoint_rankings_interleave_by_position() -> None:
    ranked = [item for item, _ in fuse([["a", "b"], ["c", "d"]])]
    assert set(ranked[:2]) == {"a", "c"}
    assert set(ranked[2:]) == {"b", "d"}


def test_ties_are_deterministic_by_first_appearance() -> None:
    once = fuse([["a", "b"], ["c", "d"]])
    twice = fuse([["a", "b"], ["c", "d"]])
    assert once == twice
    assert next(i for i, _ in once) == "a"


def test_weights_scale_a_rankers_contribution() -> None:
    unweighted = [i for i, _ in fuse([["a"], ["b"]])]
    weighted = [i for i, _ in fuse([["a"], ["b"]], weights=[0.1, 1.0])]
    assert unweighted[0] == "a"
    assert weighted[0] == "b"


def test_mismatched_weight_count_is_refused() -> None:
    with pytest.raises(ValueError, match="2 weights for 1 rankings"):
        fuse([["a"]], weights=[1.0, 1.0])


def test_rank_is_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        rrf_score(0)


def test_empty_input_is_empty_output() -> None:
    assert fuse([]) == []
    assert fuse([[], []]) == []
