"""Locate the chunks a claim came from.

`Citation.chunk_ids` is the hop that makes a claim *checkable*: claim → chunks →
`char_start`/`char_end` → the exact span of normalized markdown a reader can be
shown. It was `[]` at every production write site, so the wire format existed
end-to-end and carried nothing, and any grounding view built on it would have
rendered an empty chain.

This module is the deterministic half of fixing that: given a claim's text and
the chunks of the document it was extracted from, decide which chunks actually
support it. No LLM — the claim is already derived from the document, so this is
a retrieval problem, not a judgement one, and a deterministic answer is
auditable in a way a model's is not.

Matching is token-overlap based rather than substring based, because an
extracted claim is a *paraphrase* of its source sentence far more often than a
quotation of it. A substring match would return nothing on almost every real
claim and the field would stay empty in a subtler way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Words too common to be evidence of anything. Kept deliberately small: an
#: aggressive list throws away the domain terms that make a match meaningful.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "which",
        "with",
    ]
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")


def _content_tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


@dataclass(frozen=True)
class ChunkRef:
    """Just enough of a chunk to match against, so callers need no ORM here."""

    id: object
    text: str
    ordinal: int


def rank_chunks_for_claim(claim_text: str, chunks: list[ChunkRef]) -> list[tuple[ChunkRef, float]]:
    """`(chunk, score)` sorted best-first. Score is claim-coverage in `[0, 1]`.

    Coverage of the *claim* rather than similarity: the question is "how much of
    what this claim asserts appears in this chunk", and chunks are far longer
    than claims, so a symmetric measure would penalise exactly the long chunks
    that do contain the answer.
    """
    claim_tokens = _content_tokens(claim_text)
    if not claim_tokens:
        return []
    scored: list[tuple[ChunkRef, float]] = []
    for c in chunks:
        overlap = claim_tokens & _content_tokens(c.text)
        if overlap:
            scored.append((c, len(overlap) / len(claim_tokens)))
    scored.sort(key=lambda t: (-t[1], t[0].ordinal))
    return scored


def chunks_for_claim(
    claim_text: str,
    chunks: list[ChunkRef],
    *,
    min_coverage: float = 0.5,
    max_chunks: int = 3,
) -> list[object]:
    """Chunk ids supporting `claim_text`, best first; `[]` when nothing does.

    Returning `[]` is a real answer, not a failure: a claim the extractor
    invented, or one that restates the document's overall thesis without
    matching any single chunk, genuinely has no supporting span. Recording an
    empty list is honest, and a weak match recorded as grounding would be worse
    than none — it is exactly the confident-wrongness this codebase keeps
    producing.

    `min_coverage` is deliberately high. A claim sharing half its content words
    with a chunk is strong evidence given both come from the same document; the
    failure to avoid is a spurious link, not a missed one.
    """
    ranked = rank_chunks_for_claim(claim_text, chunks)
    if not ranked:
        return []
    best = ranked[0][1]
    if best < min_coverage:
        return []
    # Keep near-ties: a claim spanning a chunk boundary is genuinely supported
    # by both, and dropping one would understate its grounding.
    cutoff = max(min_coverage, best * 0.8)
    return [c.id for c, score in ranked[:max_chunks] if score >= cutoff]
