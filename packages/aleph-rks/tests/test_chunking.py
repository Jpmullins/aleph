"""Chunking determinism and overlap correctness tests."""

from __future__ import annotations

from itertools import pairwise

from aleph_rks.chunking import chunk_markdown

SAMPLE = """# Methods

## Sample Selection

We selected 47 participants from the registry. Inclusion criteria
included age 18 to 65, no prior diagnosis of X, and willingness to
travel to the study site. Three were later excluded due to scheduling
conflicts. The final cohort had 44 participants.

## Measurement

Participants completed the protocol over two visits separated by 7 to
10 days. We recorded blood pressure, heart rate, and cortisol levels.

# Results

## Primary outcome

The primary outcome was reduced by 18% in the treatment arm. The
control arm showed no significant change. We replicate the finding
across two subgroups.
"""


def test_chunk_count_reasonable() -> None:
    out = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    assert len(out) >= 2
    for c in out:
        assert c.text.strip()
        assert c.token_count > 0


def test_chunk_preserves_section_paths() -> None:
    out = chunk_markdown(SAMPLE, target_tokens=80, overlap_tokens=12)
    paths = {c.section_path for c in out}
    # We expect at least sample-selection and primary-outcome paths.
    assert any("sample-selection" in (p or "") for p in paths)
    assert any("primary-outcome" in (p or "") for p in paths)


def test_chunk_is_deterministic() -> None:
    a = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    b = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    assert [c.text for c in a] == [c.text for c in b]
    assert [c.section_path for c in a] == [c.section_path for c in b]


def test_empty_markdown_returns_empty() -> None:
    assert chunk_markdown("", target_tokens=64) == []
    assert chunk_markdown("   \n\n  \n", target_tokens=64) == []


def test_single_short_paragraph_one_chunk() -> None:
    md = "# T\n\nOne short sentence."
    out = chunk_markdown(md, target_tokens=128)
    assert len(out) == 1
    assert "One short sentence." in out[0].text


# ---------------------------------------------------------------------------
# The chunker has an upper bound (found while running the retrieval eval)
# ---------------------------------------------------------------------------
#
# `target_tokens` was enforced by flushing the BUFFER before a sentence was
# added, so a sentence bigger than the target landed in an empty buffer and was
# emitted at whatever size it happened to be. Measured on the live instance:
# 19 of 29,206 chunks exceed 8,192 tokens, the largest is 21,543, and all 19
# have `embedding IS NULL` — the embedder answers
# `Too many input tokens. Max input tokens: 8192` and the chunk is permanently
# invisible to the dense leg. It also killed `python -m aleph_evals
# .retrieval_eval` outright with an HTTP 400 partway through seeding.


def _no_sentence_terminators(words: int = 20_000) -> str:
    """A document that is one 'sentence'. This is not a contrived input.

    `_SENTENCE_END` needs `.!?` followed by whitespace and a capital or digit.
    A scanned PDF's text layer, a scraped page that lost its punctuation and a
    long reference list all fail to provide one.
    """
    return "word " * words


def test_no_chunk_exceeds_twice_the_target_however_unsplittable_the_text() -> None:
    text = _no_sentence_terminators()
    chunks = chunk_markdown(text, target_tokens=512)

    assert len(chunks) > 1, "a 20,000-word run came back as one chunk"
    worst = max(c.token_count for c in chunks)
    assert worst <= 1024, (
        f"largest chunk is {worst} tokens; the bound is 2 x target because the "
        "buffer is flushed BEFORE an over-budget piece is added"
    )


def test_text_with_no_whitespace_at_all_is_still_cut() -> None:
    """A base64 blob has no newline and no space to break on.

    The character-boundary fallback is what stops the bound from being
    "unless the document is really awkward", which is the shape of every
    guarantee this repository has had to withdraw.
    """
    blob = "A" * 60_000
    chunks = chunk_markdown(blob, target_tokens=512)

    assert len(chunks) > 1
    assert max(c.token_count for c in chunks) <= 1024


def test_the_split_still_slices_the_document_exactly() -> None:
    """The invariant `test_chunk_offsets.py` protects, on the new code path.

    Sub-spans are contiguous, so `emit`'s `markdown[first_start:last_end]` is
    still a real slice. A cut that produced overlapping or gapped spans would
    make a grounding view highlight the wrong characters — confidently, and
    wrongly, which is worse than having no offsets.
    """
    for text in (_no_sentence_terminators(3_000), "A" * 40_000, "x\n" * 20_000):
        for chunk in chunk_markdown(text, target_tokens=512):
            assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_the_pieces_cover_the_run_without_overlapping() -> None:
    """Cut, not sampled: nothing is dropped and nothing is duplicated.

    A splitter that lost a slice would silently make part of a document
    unsearchable, and nothing downstream would notice — the chunk count would
    simply be lower.
    """
    text = _no_sentence_terminators(4_000)
    chunks = chunk_markdown(text, target_tokens=512, overlap_tokens=0)

    assert chunks[0].char_start == 0
    for earlier, later in pairwise(chunks):
        assert later.char_start == earlier.char_end, (
            "sub-spans must be contiguous; a gap loses text and an overlap double-counts it"
        )
    assert chunks[-1].char_end == len(text.rstrip())


def test_an_ordinary_document_is_chunked_the_way_it_always_was() -> None:
    """The cap must not reshape text that was never over it.

    Every sentence here is well under the target, so the new branch has to be
    a pass-through — otherwise this change silently re-chunks the whole corpus
    and every measured retrieval number moves for a reason nobody recorded.
    """
    prose = " ".join(f"Sentence number {n} says something about retrieval." for n in range(40))
    chunks = chunk_markdown(prose, target_tokens=512)

    assert [c.text for c in chunks] == [prose]


def test_a_span_whose_density_changes_partway_is_still_capped() -> None:
    """The cut point is ESTIMATED from characters-per-token, then verified.

    The estimate is measured across the whole remaining span, so a run that is
    dense at the front and sparse behind it — CJK followed by English, a base64
    header followed by prose — gets an average ratio that overshoots badly at
    the front. Measured with the verification removed, this input produces a
    single 6,034-token chunk against a 512-token target: back over the
    embedder's 8,192 limit for a slightly longer document, and back to the
    defect the cap exists to remove.

    Only the shrink loop catches this, and nothing else in this file does — the
    even-density cases pass either way, which is why this one is written out.
    """
    text = "漢" * 3_000 + " " + "word " * 6_000
    chunks = chunk_markdown(text, target_tokens=512)

    worst = max(c.token_count for c in chunks)
    assert worst <= 1024, f"largest chunk is {worst} tokens; the ratio estimate was not verified"
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
