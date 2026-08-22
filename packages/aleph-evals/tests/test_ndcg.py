"""nDCG is a ratio in [0, 1]. The shipped one was not.

`_ndcg_at` takes a ranked list of SOURCE ids read off chunk hits, so one source
appears once per chunk it contributed. The numerator added a gain term for every
occurrence while the denominator counted distinct sources, which made the metric
unbounded above — a retrieval that returned the same correct source ten times
scored 4.54 out of a possible 1.00, and scored higher the more it repeated
itself.

Nothing caught it because nothing tested the function directly: it was only ever
called on real retrieval output, where a number over 1.0 reads as a good result
rather than as an impossible one. The headline recorded as "nDCG@10 0.681" came
out of it and was not an nDCG.

The bound is the whole point of a normalised metric, so it is asserted first and
over generated input, not over three hand-picked cases.
"""

from __future__ import annotations

import math

from aleph_evals.retrieval_eval import NDCG_K, _ndcg_at


def test_a_repeated_correct_source_cannot_score_above_one() -> None:
    """The exact defect. 1, 3 and 10 copies must all be a perfect 1.0."""
    for copies in (1, 2, 3, 5, 10, 50):
        score = _ndcg_at(["src-A"] * copies, {"src-A"}, 10)
        assert score == 1.0, f"{copies} copies of the one wanted source scored {score}"


def test_the_metric_is_bounded_in_zero_to_one_over_every_shape() -> None:
    """Exhaustive over short lists, because a bound is a claim about all inputs.

    Enumerates every ranking of up to four hits drawn from three sources
    against every non-empty wanted set — duplicates included, which is the case
    that broke it.
    """
    sources = ["a", "b", "c"]
    wanted_sets = [{"a"}, {"a", "b"}, {"a", "b", "c"}]
    checked = 0
    for length in range(0, 5):
        for i in range(len(sources) ** length):
            ordered: list[str] = []
            n = i
            for _ in range(length):
                ordered.append(sources[n % len(sources)])
                n //= len(sources)
            for wanted in wanted_sets:
                score = _ndcg_at(ordered, wanted, 10)
                assert 0.0 <= score <= 1.0, f"{ordered} vs {wanted} scored {score}"
                checked += 1
    assert checked > 300, f"only {checked} combinations exercised — the sweep is too small"


def test_a_perfect_ranking_scores_one_and_an_empty_one_scores_zero() -> None:
    assert _ndcg_at(["a", "b"], {"a", "b"}, 10) == 1.0
    assert _ndcg_at(["b", "a"], {"a", "b"}, 10) == 1.0, "order within the ideal set is free"
    assert _ndcg_at([], {"a"}, 10) == 0.0
    assert _ndcg_at(["x", "y"], {"a"}, 10) == 0.0


def test_rank_still_matters() -> None:
    """The dedup fix must not flatten the metric into recall.

    Without this, returning the wanted source at position 9 would score the
    same as returning it first, and the number would stop measuring ranking.
    """
    first = _ndcg_at(["a", "x", "x"], {"a"}, 10)
    third = _ndcg_at(["x", "x", "a"], {"a"}, 10)
    assert first == 1.0
    assert 0.0 < third < first, f"position made no difference: {first} vs {third}"
    assert math.isclose(third, 1.0 / math.log2(4))


def test_the_cutoff_is_honoured() -> None:
    """A hit past the cutoff contributes nothing."""
    assert _ndcg_at(["x"] * 10 + ["a"], {"a"}, 10) == 0.0
    assert _ndcg_at(["x"] * 10 + ["a"], {"a"}, 11) > 0.0


def test_the_reported_cutoff_is_the_one_the_report_names() -> None:
    """`NDCG_K` is what the summary line prints; a mismatch would label the
    number with a cutoff it was not computed at."""
    assert NDCG_K == 10
