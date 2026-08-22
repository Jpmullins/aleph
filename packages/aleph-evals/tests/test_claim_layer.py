"""`_claim_layer` — how a claim is attributed to a document, and what is refused.

The claim surface is scored against the same labels as the chunk surface, so
every claim has to resolve to exactly one document of the eval corpus. Getting
that mapping wrong does not raise; it silently moves the number, and it moves
it in the direction that flatters the newer index. These pin the three
decisions that keep it honest.
"""

from __future__ import annotations

from typing import Any

from aleph_evals.build_retrieval_set import TRAVERSABLE_EDGE_KINDS, _claim_layer


def _doc(doc_id: str, chunk_ids: list[str]) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "title": "t",
        "text": "x" * 2000,
        "provenance": {
            "source_id": doc_id.split("#", maxsplit=1)[0],
            "part": 0,
            "chunk_ids": chunk_ids,
        },
    }


def test_a_claim_is_attributed_to_the_document_holding_its_anchors() -> None:
    docs = [_doc("s1#0", ["c1", "c2"]), _doc("s1#1", ["c3"])]
    claims, _edges, dropped = _claim_layer(docs, [("claim-a", "Bees pollinate.", ["c3"])])

    assert dropped == 0
    assert claims == [{"claim_id": "claim-a", "text": "Bees pollinate.", "doc_id": "s1#1"}]


def test_a_claim_spanning_two_parts_lands_on_one_of_them_only() -> None:
    """One document per claim, or one retrieval counts as several hits.

    The eval scores a hit by the document a result resolves to. A claim
    credited to both parts of its paper would be correct for a question about
    either, which is a hit the retriever did not earn — and the inflation would
    land entirely on the claims column of a chunks-vs-claims comparison.
    """
    docs = [_doc("s1#0", ["c1", "c2"]), _doc("s1#1", ["c3"])]
    claims, _edges, _dropped = _claim_layer(docs, [("claim-a", "spans both", ["c1", "c2", "c3"])])

    (claim,) = claims
    # Majority of anchors, so the part carrying two of the three wins.
    assert claim["doc_id"] == "s1#0"


def test_ties_break_deterministically_on_corpus_order() -> None:
    """Re-running the builder over an unchanged database must give one answer.

    A tie broken by dict iteration order would make two datasets built from the
    same corpus score differently, and nothing would say why.
    """
    docs = [_doc("s1#0", ["c1"]), _doc("s1#1", ["c2"])]
    rows = [("claim-a", "one anchor each", ["c1", "c2"])]

    first, _e1, _d1 = _claim_layer(docs, rows)
    second, _e2, _d2 = _claim_layer(list(reversed(docs)), rows)

    assert first[0]["doc_id"] == "s1#0"
    # Reversing the corpus reverses which part is "first", and the rule is
    # stated as lowest position in the corpus — so the answer follows the
    # corpus rather than being arbitrary.
    assert second[0]["doc_id"] == "s1#1"


def test_a_claim_with_no_chunk_anchor_is_dropped_not_guessed() -> None:
    """The obvious fallback is wrong and is deliberately absent.

    `Citation.source_id` is populated far more often than `chunk_id`, so
    falling back to it would rescue most of the dropped claims — by crediting
    each one to every part of its source. That turns "this claim is about
    section 4" into "this claim answers any question about the paper", which
    manufactures hits for the claims arm out of nothing.
    """
    docs = [_doc("s1#0", ["c1"])]
    claims, _edges, dropped = _claim_layer(
        docs,
        [
            ("anchored", "in the corpus", ["c1"]),
            ("unanchored", "same paper, no chunk", []),
            ("elsewhere", "anchored to a chunk we did not emit", ["c99"]),
        ],
    )

    assert [c["claim_id"] for c in claims] == ["anchored"]
    assert dropped == 2


def test_an_edge_is_kept_only_when_both_ends_survive() -> None:
    """`claim_edges` has a foreign key on each side.

    A dangling edge cannot be seeded — the insert fails on the constraint and
    takes the whole run with it — so the filter belongs at build time, where
    the drop can be counted, rather than at seed time as an exception.
    """
    docs = [_doc("s1#0", ["c1", "c2"])]
    claims, edges, _dropped = _claim_layer(
        docs,
        [("a", "kept", ["c1"]), ("b", "kept too", ["c2"]), ("c", "dropped", [])],
        [("a", "b", "supports"), ("a", "c", "supports")],
    )

    assert {c["claim_id"] for c in claims} == {"a", "b"}
    assert edges == [{"src": "a", "dst": "b", "kind": "supports"}]


def test_the_builder_and_the_search_agree_on_traversable_edges() -> None:
    """Two lists, one meaning. Drift here silences the graph hop.

    `build_retrieval_set` decides which edges go into the data file;
    `search_claims` decides which it walks. If the builder stopped emitting a
    kind the search follows, the hop would measure zero for a reason that has
    nothing to do with the graph, and the eval would report it as a finding
    about retrieval.

    The eval raises on this mismatch at seed time. This asserts the two are in
    fact equal today, so the guard is never the thing that fires.
    """
    from aleph_wiki.claim_search import TRAVERSABLE_EDGES

    assert set(TRAVERSABLE_EDGE_KINDS) == set(TRAVERSABLE_EDGES)


def test_supersedes_is_not_traversable() -> None:
    """A withdrawn belief must not be reachable as a search result.

    Stated here as well as in `claim_search` because this module writes a data
    FILE: a dataset that contained `supersedes` edges would let a future eval
    walk into superseded claims even if the search's own list was later fixed.
    """
    assert "supersedes" not in TRAVERSABLE_EDGE_KINDS
