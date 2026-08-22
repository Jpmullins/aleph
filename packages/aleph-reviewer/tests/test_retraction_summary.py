"""A retraction's second hop must not report a zero it never measured — WS-RS9 c4.

`describe_impact` writes the sentence that lands in the `retracted_source`
finding, in the ledger payload, and in front of whoever asks what a retraction
touched. It said:

    Reached 3 claim(s): 3 citing it directly, 0 derived from those (deepest hop 0).

On this instance that "0" has never once meant "nothing depends on those
claims". It means `claim_edges` holds no `derived_from` row at all — two rows
exist in the whole table and both are `supersedes` — because nothing in
production writes one. `aleph_wiki.derivation.record_derivations` is a complete,
tested writer whose only callers are tests.

Two agents declined to invent a caller for it, and that judgement is right: a
derivation edge asserts that belief B rests on belief A, and Aleph's extractors
never learn that — every claim is anchored to a source chunk, not to another
claim. Writing the edge from a model's guess would make `retraction_impact`
propagate a retraction along dependencies nobody verified, which is strictly
worse than an empty graph.

What was NOT right was reporting it as a measured zero. This pins the
distinction, which is the same one `abstain n/a` and `lexical_only` exist to
make: an absence standing in for a state is the defect class this repository
keeps shipping.
"""

from __future__ import annotations

from uuid import uuid4

from aleph_reviewer.retraction import RetractionImpact, describe_impact

CLAIM_A = uuid4()
CLAIM_B = uuid4()


def _impact(*, empty_graph: bool, derived: set[object] | None = None) -> RetractionImpact:
    reached = set(derived or set())
    return RetractionImpact(
        directly_cited={CLAIM_A},
        derived=reached,  # type: ignore[arg-type]
        unsupported={CLAIM_A},
        weakened=set(),
        depth_by_claim={CLAIM_A: 0},
        derivation_graph_is_empty=empty_graph,
    )


def test_a_zero_second_hop_says_whether_there_was_a_graph_to_walk() -> None:
    summary = describe_impact(
        _impact(empty_graph=True), short_id="s7", reason="retracted by the publisher"
    )
    assert "0 derived from those" in summary
    assert "no 'derived_from' edge at all" in summary
    assert "WS-RS9 c4" in summary


def test_a_real_zero_over_a_real_graph_is_reported_as_a_result() -> None:
    """The other half. Once something writes edges, the qualifier must go away —
    otherwise the sentence is a permanent apology and stops carrying news."""
    summary = describe_impact(
        _impact(empty_graph=False), short_id="s7", reason="retracted by the publisher"
    )
    assert "0 derived from those (deepest hop 0)" in summary
    assert "no 'derived_from' edge at all" not in summary


def test_a_non_zero_second_hop_reports_the_count_and_the_depth() -> None:
    impact = RetractionImpact(
        directly_cited={CLAIM_A},
        derived={CLAIM_B},
        unsupported={CLAIM_A, CLAIM_B},
        weakened=set(),
        depth_by_claim={CLAIM_A: 0, CLAIM_B: 2},
        derivation_graph_is_empty=False,
    )
    summary = describe_impact(impact, short_id="s7", reason="withdrawn")
    assert "1 derived from those (deepest hop 2)" in summary
    assert "Reached 2 claim(s)" in summary


def test_the_default_is_the_pessimistic_one() -> None:
    """A `RetractionImpact` built without the flag must not claim a graph exists.

    `retraction_impact` fills it from a query; anything else constructing one
    (a test, a future caller) gets the reading that cannot overstate what was
    measured.
    """
    impact = RetractionImpact(set(), set(), set(), set(), {})
    assert impact.derivation_graph_is_empty is True
