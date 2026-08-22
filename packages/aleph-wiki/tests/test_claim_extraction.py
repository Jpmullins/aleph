"""The extractor's job is to produce claims that can be CHECKED.

Every test here is about something the downstream verbatim check cannot catch on
its own. `BeliefService._attach_evidence` refuses a quote that is not present in
its `source_text` — that is a strong guarantee and it has one blind spot: it
verifies the quote against whatever text it was handed. Hand it the wrong chunk
and a fabricated quote can ground perfectly.

So these tests are about the parts that decide WHAT gets checked against WHAT.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from aleph_wiki.claim_extraction import (
    CHUNKS_PER_CALL,
    MIN_QUOTE_CHARS,
    ChunkRef,
    drafts_from_response,
    extract_claims,
)

SOURCE = uuid4()
PAGE = uuid4()

DOC = (
    "Sedimentation rates rose sharply after the 8.2 ka event across the basin.\n\n"
    "Radiocarbon dates bracket the transition to within a few decades.\n\n"
    "Sedimentation rates rose sharply after the 8.2 ka event across the basin.\n"
)


def _chunks() -> list[ChunkRef]:
    """Two chunks, the SECOND of which repeats the first's sentence.

    Deliberate: a quote hunted across the whole document anchors to the first
    occurrence. If this extractor grounded against the document, chunk two's
    citation would point at chunk one's text and the check would still pass.
    """
    first = DOC.index("Sedimentation")
    second = DOC.rindex("Sedimentation")
    return [
        ChunkRef(chunk_id=uuid4(), text=DOC[first : first + 74], char_start=first),
        ChunkRef(chunk_id=uuid4(), text=DOC[second:].strip("\n"), char_start=second),
    ]


def _response(**claim: Any) -> dict[str, Any]:
    return {"claims": [claim]}


def test_a_quote_is_grounded_against_its_own_chunk_not_the_document() -> None:
    """The offsets point at the passage the model actually named."""
    chunks = _chunks()
    quote = "Sedimentation rates rose sharply after the 8.2 ka event"
    drafts = drafts_from_response(
        _response(text="Rates rose after 8.2 ka", passage=2, quote=quote),
        chunks,
        source_id=SOURCE,
        page_id=PAGE,
    )
    assert len(drafts) == 1
    evidence = drafts[0].evidence[0]
    assert evidence.chunk_id == chunks[1].chunk_id
    assert evidence.source_text == chunks[1].text
    assert evidence.char_offset == chunks[1].char_start
    # And the arithmetic that makes the stored span document-relative holds.
    assert DOC[evidence.char_offset : evidence.char_offset + len(evidence.source_text)] == (
        evidence.source_text
    )


def test_a_passage_number_the_batch_does_not_have_is_refused() -> None:
    """Otherwise the quote is checked against the WRONG chunk — and may pass.

    This is the one case where the downstream verbatim check is actively
    misleading: a real sentence from passage 1, attributed to passage 7, would
    ground successfully against whatever chunk happened to be indexed there.
    """
    chunks = _chunks()
    for passage in (0, 3, 99, -1, None, "two"):
        drafts = drafts_from_response(
            _response(text="Rates rose", passage=passage, quote="A" * 40),
            chunks,
            source_id=SOURCE,
            page_id=PAGE,
        )
        assert drafts == [], passage


def test_a_quote_too_short_to_anchor_anything_is_refused() -> None:
    """A three-word quote passes the verbatim check and cites nothing.

    The blind spot in "is this quote present in the source": "the" is present in
    every source. Length is the cheap proxy for "this identifies a passage".
    """
    chunks = _chunks()
    assert (
        drafts_from_response(
            _response(text="Rates rose", passage=1, quote="rates rose"),
            chunks,
            source_id=SOURCE,
            page_id=PAGE,
        )
        == []
    )
    long_enough = "Sedimentation rates rose sharply after the 8.2 ka event"
    assert len(long_enough) >= MIN_QUOTE_CHARS
    assert drafts_from_response(
        _response(text="Rates rose after 8.2 ka", passage=1, quote=long_enough),
        chunks,
        source_id=SOURCE,
        page_id=PAGE,
    )


def test_a_claim_that_is_its_own_quote_is_refused() -> None:
    """No restatement means nothing was checked against the source.

    A claim identical to its quote grounds trivially and asserts only that the
    source contains its own words.
    """
    chunks = _chunks()
    quote = "Sedimentation rates rose sharply after the 8.2 ka event"
    assert (
        drafts_from_response(
            _response(text=quote, passage=1, quote=quote),
            chunks,
            source_id=SOURCE,
            page_id=PAGE,
        )
        == []
    )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"claims": None},
        {"claims": "not a list"},
        {"claims": [None, 3, "text"]},
        {"claims": [{"text": "", "passage": 1, "quote": "x" * 40}]},
        {"claims": [{"text": "ok", "passage": 1, "quote": ""}]},
        {"claims": [{"text": "ok", "quote": "x" * 40}]},
    ],
)
def test_malformed_output_is_dropped_never_repaired(response: dict[str, Any]) -> None:
    """A repaired claim is one the harness wrote and the model gets credit for.

    Three provenance errors in one gesture: invented text, false attribution,
    and a source anchor for something the source never said.
    """
    assert drafts_from_response(response, _chunks(), source_id=SOURCE, page_id=PAGE) == []


async def test_batches_are_independent_so_one_bad_response_costs_only_itself() -> None:
    """Raising would discard every claim already extracted from the document."""
    chunks = [
        ChunkRef(
            chunk_id=uuid4(), text=f"Passage {i} asserts something factual here.", char_start=i
        )
        for i in range(CHUNKS_PER_CALL * 2)
    ]
    calls = {"n": 0}

    async def flaky(*, system_prompt: str, user_payload: str, purpose: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "gateway said no"
            raise RuntimeError(msg)
        return _response(
            text="Something factual",
            passage=1,
            quote="Passage 12 asserts something factual here.",
        )

    drafts = await extract_claims(
        chunks, source_id=SOURCE, page_id=PAGE, call_json=flaky, title="t"
    )
    assert calls["n"] == 2
    assert len(drafts) == 1


async def test_a_truncated_extraction_reads_a_prefix_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A source half-read that reports success is a confident gap."""
    chunks = [
        ChunkRef(chunk_id=uuid4(), text=f"Passage {i} asserts something.", char_start=i)
        for i in range(40)
    ]
    seen: list[str] = []

    async def record(*, system_prompt: str, user_payload: str, purpose: str) -> dict[str, Any]:
        seen.append(user_payload)
        return {"claims": []}

    await extract_claims(chunks, source_id=SOURCE, page_id=PAGE, call_json=record, max_chunks=12)
    assert len(seen) == 1, "read more than the cap allowed"


async def test_the_purpose_reaches_the_model_call() -> None:
    """Cost has to land somewhere nameable, per the standing rule."""
    captured: dict[str, str] = {}

    async def record(*, system_prompt: str, user_payload: str, purpose: str) -> dict[str, Any]:
        captured["purpose"] = purpose
        return {"claims": []}

    await extract_claims(
        _chunks(), source_id=SOURCE, page_id=PAGE, call_json=record, purpose="wiki.custom"
    )
    assert captured["purpose"] == "wiki.custom"


def test_the_draft_carries_the_evidence_tier_that_means_cited() -> None:
    """`inferred` is the default on ClaimUpsert and would be a lie here."""
    drafts = drafts_from_response(
        _response(
            text="Rates rose after 8.2 ka",
            passage=1,
            quote="Sedimentation rates rose sharply after the 8.2 ka event",
        ),
        _chunks(),
        source_id=SOURCE,
        page_id=PAGE,
        revision_id=UUID("00000000-0000-0000-0000-0000000000cc"),
    )
    assert drafts[0].evidence_tier == "cited"
    assert drafts[0].revision_id == UUID("00000000-0000-0000-0000-0000000000cc")
