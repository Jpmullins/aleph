"""Verbatim grounding — proving a quote is actually in the source.

A citation is a claim about a document: *this text appears there*. That claim is
checkable in microseconds without a model, and checking it is the difference
between provenance and decoration. Aleph's previous citation "verification"
compared model-emitted `[cN]` markers against a source registry the same
generation had supplied — it verified that the model was internally consistent
with itself, which is not a property anyone needs.

The matching is normalised, because a quote that differs from its source only by
a ligature or a smart quote is still that quote, and rejecting it teaches the
writer to stop quoting. Normalisation is deliberately conservative: it collapses
representational differences and nothing else. It does not stem, lowercase words
away, or drop punctuation that changes meaning.

The offset map is the load-bearing part. Matching happens on normalised text but
the span must be recorded against the ORIGINAL bytes, or the highlight lands in
the wrong place and the evidence link rots. `ground` returns raw offsets.

Ported in spirit from graphify's `normalizeForMatch` / `verifyVerbatim`
(MIT, Copyright (c) 2026 Safi Shamsi) — see the package NOTICE.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["GroundedSpan", "defang", "ground", "normalize_for_match"]

# Characters that are representational noise: different bytes, same reading.
_LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    "æ": "ae",
    "œ": "oe",
}
_QUOTES = dict.fromkeys("‘’‚‛′‵", "'") | dict.fromkeys(
    "“”„‟″‶", '"'
)
_DASHES = dict.fromkeys("‐‑‒–—―−", "-")

#: Zero-width and directional characters. These are invisible, so they are both
#: a normalisation problem (a quote that "looks identical" fails to match) and a
#: security one (they hide text from a human reviewer while the model reads it).
_INVISIBLE = re.compile(
    "["
    "​‌‍⁠﻿"  # zero-width space/non-joiner/joiner/word-joiner/BOM
    "‎‏"  # LTR/RTL marks
    "‪-‮"  # embedding/override
    "⁦-⁩"  # isolates
    "­"  # soft hyphen
    "]"
)

#: Line/paragraph separators. JSON-legal, and they terminate a line in most
#: renderers — so text after one can be invisible in a UI that shows the rest.
_LINE_SEPARATORS = re.compile("[  ]")


def defang(text: str) -> str:
    """Strip invisible and directional characters from untrusted text.

    Applied at the ingest boundary. These characters let a document hide
    instructions from a human reading it while a model reads them fine — the
    text a reviewer approves and the text the model consumes must be the same
    text. Also normalises the two Unicode line separators to newlines.

    Deliberately not a prompt-injection filter. It removes a class of
    *invisibility*, which is checkable, rather than trying to detect intent,
    which is not.
    """
    text = _INVISIBLE.sub("", text)
    return _LINE_SEPARATORS.sub("\n", text)


def normalize_for_match(text: str) -> tuple[str, list[int]]:
    """Normalise for comparison, and map each output char back to its input index.

    Returns ``(normalized, offsets)`` where ``offsets[i]`` is the index in the
    ORIGINAL string that produced ``normalized[i]``. That map is what lets a
    match found in normalised space be recorded as a span in the real document.

    Applied, in order: NFKD decomposition with combining marks dropped
    (so ``café`` matches ``cafe``), ligature and smart-quote and dash folding,
    invisible-character removal, lowercasing, and whitespace runs collapsed to a
    single space.
    """
    out: list[str] = []
    offsets: list[int] = []
    pending_space = False

    for index, char in enumerate(text):
        if _INVISIBLE.match(char):
            continue

        if char.isspace():
            # Collapse runs; a leading run produces nothing.
            pending_space = bool(out)
            continue

        replacement = _LIGATURES.get(char) or _QUOTES.get(char) or _DASHES.get(char)
        if replacement is None:
            decomposed = unicodedata.normalize("NFKD", char)
            replacement = "".join(c for c in decomposed if not unicodedata.combining(c))
        replacement = replacement.lower()
        if not replacement:
            continue

        if pending_space:
            out.append(" ")
            offsets.append(index)
            pending_space = False

        for produced in replacement:
            out.append(produced)
            offsets.append(index)

    return "".join(out), offsets


@dataclass(frozen=True)
class GroundedSpan:
    """Where a quote was found in the source, in ORIGINAL character offsets."""

    char_start: int
    char_end: int
    #: The source's own text for that span — not the quote as supplied. The two
    #: differ whenever normalisation did any work, and the source's version is
    #: the one worth storing.
    matched_text: str


def ground(quote: str, source_text: str) -> GroundedSpan | None:
    """Locate ``quote`` in ``source_text``, or return None.

    None means the quote is not in the source, and a citation asserting it
    should be refused. There is no fuzzy threshold and no partial credit: the
    whole value of this check is that its answer is not a judgment call.

    An empty or whitespace-only quote grounds nothing — it would trivially
    "match" everywhere and assert nothing.
    """
    needle, _ = normalize_for_match(quote)
    if not needle.strip():
        return None

    haystack, offsets = normalize_for_match(source_text)
    position = haystack.find(needle)
    if position < 0:
        return None

    start = offsets[position]
    # +1 because the map points at the character that PRODUCED the output, and
    # the span must include that whole character.
    end = offsets[position + len(needle) - 1] + 1
    return GroundedSpan(char_start=start, char_end=end, matched_text=source_text[start:end])
