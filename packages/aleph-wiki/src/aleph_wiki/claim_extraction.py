"""Turn a source's chunks into claims that carry the sentence supporting them.

The belief layer's write path has been complete and correct for a long time and
has never run, because nothing produced evidence for it to check. The producer
that existed asked a model for `{text, citation_marker}` and built a citation
with `chunk_ids=[]`, no quote and no span — which is why the live database holds
796 citations that cite nothing in particular.

**What this does differently, and why each part is load-bearing.**

*The model is shown the chunks, not the document.* A claim has to be traceable
to a passage, and a passage is what a chunk is. Handing over 50,000 characters
of markdown and asking for citation markers produces markers assigned by
position in a list, which is what the old path did and what cannot be verified
afterwards.

*The model is asked to COPY, not to summarize.* Every quote is checked against
the chunk it claims to come from — `BeliefService._attach_evidence` refuses any
quote that is not verbatim-present. So the prompt's job is to make copying the
easy path, and the check's job is to make paraphrase fail closed. A rejected
quote costs a claim; an accepted paraphrase would cost the trail its meaning.

*Grounding is scoped to one chunk.* Searching the whole document anchors a
repeated sentence to its first occurrence, which is the wrong place. The chunk
narrows it, and `EvidenceDraft.char_offset` translates back into document
coordinates.

*Nothing here writes.* It returns `ClaimUpsert` drafts. `BeliefService` decides
what survives, and the separation is what lets the extractor be swapped for a
fixture — or for a rule set — without touching the write path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from aleph_wiki.belief_service import ClaimUpsert, EvidenceDraft

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

_log = structlog.get_logger(__name__)

#: How many chunks go into one extraction call.
#:
#: Not a tuning knob so much as a tradeoff with a direction: more chunks per call
#: is cheaper and gives the model more context to spot a claim spanning two
#: passages; fewer keeps each quote near its source in the prompt, which is what
#: makes verbatim copying reliable. Twelve keeps a batch well inside any
#: context window while still being a page or two of real text.
CHUNKS_PER_CALL = 12

#: Claims requested per batch. A cap, not a target: a batch with two claims in it
#: should produce two.
MAX_CLAIMS_PER_BATCH = 8

#: Quotes shorter than this are rejected before the model's output is trusted.
#: A three-word quote grounds almost anywhere and anchors nothing — it passes the
#: verbatim check while carrying no evidence, which is the one failure mode the
#: check itself cannot catch.
MIN_QUOTE_CHARS = 25

SYSTEM_PROMPT = """\
You extract factual claims from source passages and anchor each one to the exact \
sentence that supports it.

You are given numbered passages from a single document. For each substantive \
factual assertion the passages make, emit one claim.

Rules, in order of importance:

1. QUOTE VERBATIM. Every quote must be copied CHARACTER FOR CHARACTER from the \
passage you cite. Do not fix typos, do not normalise punctuation, do not \
shorten with an ellipsis, do not join two sentences. A quote that is not an \
exact substring of its passage is discarded, and the claim goes with it.
2. One claim, one statement. "Rates rose and salinity fell" is two claims.
3. The claim text is your own restatement; the quote is the source's words. \
They are different fields and must not be swapped.
4. Quote at least one full sentence. A fragment of a few words anchors nothing.
5. Extract what the source ASSERTS, not what it cites others as saying, and not \
your own inference from it. If the passage says "X may indicate Y", the claim \
is that the source suggests Y may follow from X — not that Y is true.
6. If a passage contains no factual assertion — a heading, a reference list, \
boilerplate — emit nothing for it. Returning fewer claims is correct and \
expected.

Return JSON: {"claims": [{"text": "...", "passage": <number>, "quote": "..."}]}\
"""


@dataclass(frozen=True)
class ChunkRef:
    """One indexed passage, as the extractor sees it."""

    chunk_id: UUID
    text: str
    #: Where this chunk starts in the whole document, so a span inside it can be
    #: reported in document coordinates.
    char_start: int


#: The model call, injected. Takes (system_prompt, user_payload, purpose) and
#: returns parsed JSON. Injected rather than imported so the extractor can be
#: tested with no gateway — and so the caller keeps ownership of capability
#: routing, cost attribution and the project's model profile, none of which
#: belong in an extractor.
JsonCaller = Callable[..., Awaitable[dict[str, Any]]]


def _batches(chunks: Sequence[ChunkRef], size: int) -> list[list[ChunkRef]]:
    return [list(chunks[i : i + size]) for i in range(0, len(chunks), size)]


def _payload(batch: Sequence[ChunkRef], title: str) -> str:
    parts = [f"Document: {title}\n"] if title else []
    for number, chunk in enumerate(batch, start=1):
        parts.append(f"--- Passage {number} ---\n{chunk.text}\n")
    parts.append(
        f"\nExtract up to {MAX_CLAIMS_PER_BATCH} claims. Quote verbatim from the passage you name."
    )
    return "\n".join(parts)


def drafts_from_response(
    response: dict[str, Any],
    batch: Sequence[ChunkRef],
    *,
    source_id: UUID,
    page_id: UUID,
    revision_id: UUID | None = None,
) -> list[ClaimUpsert]:
    """Convert one model response into drafts, discarding what cannot be checked.

    Everything refused here is refused for a reason that the verbatim check
    downstream could not catch on its own:

    - a passage number the batch does not contain: the quote would be grounded
      against the WRONG chunk, and might well succeed there;
    - a quote below `MIN_QUOTE_CHARS`: it grounds almost anywhere, so it passes
      the verbatim check while anchoring nothing;
    - a claim whose text is its own quote: no restatement happened, so there is
      nothing to check the source against.

    Malformed output is dropped, never repaired. A repaired claim is a claim the
    harness wrote, attributed to a model, anchored to a source — three
    provenance errors in one gesture.
    """
    raw = response.get("claims")
    if not isinstance(raw, list):
        return []

    drafts: list[ClaimUpsert] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        quote = str(entry.get("quote") or "").strip()
        if not text or not quote:
            continue
        try:
            number = int(entry.get("passage"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= len(batch):
            _log.debug("claim_extraction.bad_passage", passage=number, batch=len(batch))
            continue
        if len(quote) < MIN_QUOTE_CHARS:
            _log.debug("claim_extraction.quote_too_short", chars=len(quote))
            continue
        if quote == text:
            _log.debug("claim_extraction.quote_is_the_claim")
            continue

        chunk = batch[number - 1]
        drafts.append(
            ClaimUpsert(
                text=text,
                page_id=page_id,
                origin="agent",
                evidence_tier="cited",
                section_anchor="key-claims",
                revision_id=revision_id,
                evidence=[
                    EvidenceDraft(
                        source_id=source_id,
                        quote=quote,
                        # The CHUNK, not the document. Grounding a repeated
                        # sentence against the whole document anchors it to the
                        # first occurrence, which is the wrong sentence.
                        source_text=chunk.text,
                        chunk_id=chunk.chunk_id,
                        char_offset=chunk.char_start,
                        citation_marker=f"[c{number}]",
                    )
                ],
            )
        )
    return drafts


async def extract_claims(
    chunks: Sequence[ChunkRef],
    *,
    source_id: UUID,
    page_id: UUID,
    call_json: JsonCaller,
    title: str = "",
    revision_id: UUID | None = None,
    max_chunks: int | None = None,
    purpose: str = "wiki.claim_extraction",
) -> list[ClaimUpsert]:
    """Extract evidence-anchored claims from a source's chunks.

    ``max_chunks`` bounds spend on a very long document. It is a real cap, so a
    truncated run is logged with what it skipped rather than reported as a
    complete extraction — a source half-read that claims to be fully read is how
    a knowledge base develops confident gaps.

    A batch that fails is skipped, not fatal. One bad model response should cost
    its own passages and nothing else; raising would throw away every claim
    already extracted from the document.
    """
    considered = list(chunks)
    skipped = 0
    if max_chunks is not None and len(considered) > max_chunks:
        skipped = len(considered) - max_chunks
        considered = considered[:max_chunks]
        _log.warning(
            "claim_extraction.truncated",
            source_id=str(source_id),
            read=len(considered),
            skipped=skipped,
        )

    drafts: list[ClaimUpsert] = []
    for batch in _batches(considered, CHUNKS_PER_CALL):
        try:
            response = await call_json(
                system_prompt=SYSTEM_PROMPT,
                user_payload=_payload(batch, title),
                purpose=purpose,
            )
        except Exception:
            _log.exception("claim_extraction.batch_failed", source_id=str(source_id))
            continue
        drafts.extend(
            drafts_from_response(
                response,
                batch,
                source_id=source_id,
                page_id=page_id,
                revision_id=revision_id,
            )
        )
    return drafts
