"""Claim → chunk matching must ground real claims and refuse to invent links.

Two failure modes, and the second is the dangerous one:

* **Missing a link** — the claim renders ungrounded. Visible, recoverable.
* **Inventing a link** — the claim renders grounded, pointing at a chunk that
  does not support it. Invisible, and it is the exact confident-wrongness this
  whole effort exists to remove. A reader who clicks through and finds
  unrelated text loses trust in every other citation too.

So the tests below weight heavily toward "does it refuse", and use real
paraphrase rather than substring copies, because an extracted claim is almost
never a quotation of its source sentence.
"""

from __future__ import annotations

import pytest

from aleph_rks.chunking import chunk_markdown
from aleph_rks.claim_grounding import ChunkRef, chunks_for_claim, rank_chunks_for_claim

DOC = """# Chain-of-Thought Prompting

On GSM8K, a math word problem benchmark, chain-of-thought prompting with PaLM
540B achieves a 56.9% solve rate, up from 17.9% with standard prompting.

## Scale

The benefit emerges only at sufficient model scale. Models below roughly 62
billion parameters showed little improvement from chain-of-thought prompting.

## Limitations

Chain-of-thought improves accuracy but does not eliminate hallucination.
Faithful intermediate steps are not guaranteed even when the final answer is
correct.
"""


def _refs() -> list[ChunkRef]:
    return [
        ChunkRef(id=f"c{c.ordinal}", text=c.text, ordinal=c.ordinal)
        for c in chunk_markdown(DOC, target_tokens=64)
    ]


class TestGroundsRealClaims:
    @pytest.mark.parametrize(
        ("claim", "must_contain"),
        [
            ("CoT prompting with PaLM 540B reaches a 56.9% solve rate on GSM8K.", "GSM8K"),
            ("Models under about 62 billion parameters saw little benefit from CoT.", "62"),
            ("Chain-of-thought does not eliminate hallucination.", "hallucination"),
        ],
    )
    def test_paraphrased_claim_finds_its_chunk(self, claim: str, must_contain: str) -> None:
        refs = _refs()
        ids = chunks_for_claim(claim, refs)
        assert ids, f"claim {claim!r} was left ungrounded"
        matched = [r for r in refs if r.id in ids]
        assert any(must_contain in r.text for r in matched), (
            f"claim {claim!r} matched {[r.text[:50] for r in matched]}, none of "
            f"which mention {must_contain!r} — it grounded to the wrong chunk."
        )


class TestRefusesToInvent:
    @pytest.mark.parametrize(
        "claim",
        [
            "The Federal Reserve raised interest rates by 75 basis points.",
            "Penguins are flightless birds native to the southern hemisphere.",
            "",
            "the and of a",  # stopwords only
        ],
    )
    def test_unsupported_claim_returns_nothing(self, claim: str) -> None:
        assert chunks_for_claim(claim, _refs()) == [], (
            f"{claim!r} was given grounding it does not have — a reader "
            f"clicking through would find unrelated text."
        )

    def test_empty_chunk_set_is_not_an_error(self) -> None:
        assert chunks_for_claim("Anything at all.", []) == []

    def test_weak_overlap_is_refused(self) -> None:
        """Sharing a couple of words is not evidence."""
        refs = [ChunkRef(id="x", text="The benchmark was run on a cluster.", ordinal=0)]
        assert chunks_for_claim("The benchmark showed a 40 point gain in accuracy.", refs) == []


class TestRanking:
    def test_best_match_ranks_first(self) -> None:
        ranked = rank_chunks_for_claim("56.9% solve rate on GSM8K with PaLM 540B", _refs())
        assert ranked, "no chunk ranked at all"
        assert "GSM8K" in ranked[0][0].text

    def test_scores_are_coverage_fractions(self) -> None:
        for _, score in rank_chunks_for_claim("hallucination is not eliminated", _refs()):
            assert 0.0 < score <= 1.0

    def test_result_count_is_bounded(self) -> None:
        # A claim must not drag in half the document as "grounding".
        ids = chunks_for_claim("chain-of-thought prompting", _refs(), min_coverage=0.1)
        assert len(ids) <= 3


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        """Grounding is auditable only if it is reproducible."""
        claim = "CoT reaches 56.9% on GSM8K."
        a = chunks_for_claim(claim, _refs())
        b = chunks_for_claim(claim, _refs())
        assert a == b

    def test_independent_of_chunk_order(self) -> None:
        claim = "Models below 62 billion parameters showed little improvement."
        refs = _refs()
        assert set(chunks_for_claim(claim, refs)) == set(
            chunks_for_claim(claim, list(reversed(refs)))
        )


def test_matched_chunks_have_usable_offsets() -> None:
    """The point of the link: it must resolve to a highlightable span.

    Ties this module to the chunker's offset invariant — a chunk id is only
    worth storing if slicing the document by its offsets yields the chunk.
    """
    chunks = chunk_markdown(DOC, target_tokens=64)
    refs = [ChunkRef(id=c.ordinal, text=c.text, ordinal=c.ordinal) for c in chunks]
    ids = chunks_for_claim("CoT with PaLM 540B reaches 56.9% on GSM8K.", refs)
    assert ids
    for c in (c for c in chunks if c.ordinal in ids):
        assert DOC[c.char_start : c.char_end] == c.text
