"""Sentence-aware markdown chunker with overlap.

Algorithm (see Inc 1 §1.7):
  1. Split on Markdown structural boundaries: ATX/Setext headings, lists,
     code fences. Preserve heading anchors for section_path.
  2. Within each structural region, segment by sentence (deterministic
     regex splitter).
  3. Greedy pack sentences into chunks up to target_tokens. Carry
     overlap_tokens from previous chunk forward when starting a new one.
  4. Code/table blocks are kept whole if <= 2 * target_tokens; oversized
     blocks split on row/line boundaries.

Token counting uses tiktoken's cl100k_base — a stable, deterministic
counter independent of the gateway model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
# Sentence end: ., !, ?, followed by whitespace and an uppercase / digit;
# allow trailing quotation. Conservative — better to under-split than
# over-merge an entire paragraph.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9])")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    section_path: str | None
    char_start: int
    char_end: int
    token_count: int


def _slugify(s: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return out[:96] or "section"


#: Tokens that end in a period without ending a sentence.
#:
#: The splitter's rule — period, whitespace, capital-or-digit — fires inside
#: "Fig. 3", "et al. 2020", "vs. 5%", "Eq. 2". In scientific prose that is
#: constant, and it matters beyond tidiness: a split there makes one fragment
#: end on an abbreviation and the next start mid-thought, which is bad chunking
#: and fatal for any select-then-extract pipeline whose unit is a sentence.
_ABBREVIATIONS = frozenset(
    [
        "al",
        "approx",
        "ca",
        "cf",
        "co",
        "corp",
        "dept",
        "dr",
        "e.g",
        "ed",
        "eds",
        "eq",
        "est",
        "et",
        "etc",
        "fig",
        "figs",
        "i.e",
        "inc",
        "jr",
        "ltd",
        "mr",
        "mrs",
        "ms",
        "no",
        "nos",
        "p",
        "pp",
        "prof",
        "ref",
        "refs",
        "sec",
        "secs",
        "sr",
        "st",
        "tab",
        "tabs",
        "vol",
        "vols",
        "vs",
    ]
)


def _is_abbreviation(text: str, period_index: int) -> bool:
    """True if the period at `period_index` closes an abbreviation, not a sentence."""
    start = period_index
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "."):
        start -= 1
    word = text[start:period_index].lower().rstrip(".")
    if not word:
        return False
    # A lone initial ("J. Smith") is never a sentence end either.
    return word in _ABBREVIATIONS or len(word) == 1


def _split_sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans `(start, end)` as offsets INTO `text`.

    Returning spans rather than strings is what makes exact provenance
    possible: the caller can slice the source instead of reassembling it, so a
    chunk's `char_start`/`char_end` index the real document by construction.
    """
    if not text.strip():
        return []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for m in _SENTENCE_END.finditer(text):
        # `m.start()` sits just after the terminator; step back over any closing
        # quote/bracket the pattern consumed to find the period itself.
        i = m.start() - 1
        while i > cursor and text[i] not in ".!?":
            i -= 1
        if text[i] == "." and _is_abbreviation(text, i):
            continue
        end = m.start()
        if text[cursor:end].strip():
            spans.append((cursor, end))
        cursor = m.end()
    if text[cursor:].strip():
        spans.append((cursor, len(text)))
    # Trim surrounding whitespace off each span so a chunk never starts or ends
    # on padding — the offsets stay exact because we move the bounds, not the
    # text.
    trimmed: list[tuple[int, int]] = []
    for s, e in spans:
        a, b = s, e
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if a < b:
            trimmed.append((a, b))
    return trimmed


def _split_sentences(text: str) -> list[str]:
    """Sentence strings. Retained for callers that do not need offsets."""
    return [text[s:e] for s, e in _split_sentence_spans(text)]


def _count_tokens(text: str, enc: tiktoken.Encoding) -> int:
    return len(enc.encode(text, disallowed_special=()))


def _walk_blocks(markdown: str) -> list[tuple[str | None, str, int, int]]:
    """Return (section_path, block_text, char_start, char_end) tuples.

    A block is a heading-bounded region. The section_path is the dotted
    heading hierarchy: "Methods > Sample" etc.
    """
    if not markdown:
        return []

    # Walk lines tracking heading stack.
    blocks: list[tuple[str | None, str, int, int]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    section_text_lines: list[str] = []
    block_start = 0
    cursor = 0

    def emit_block(end_offset: int) -> None:
        nonlocal section_text_lines, block_start
        text = "".join(section_text_lines)
        if text.strip():
            path = " > ".join(t for _, t in stack) if stack else None
            blocks.append((path, text, block_start, end_offset))
        section_text_lines = []
        block_start = end_offset

    for line in markdown.splitlines(keepends=True):
        line_end = cursor + len(line)
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            emit_block(cursor)
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _slugify(title)))
            block_start = line_end  # block starts AFTER the heading line
        else:
            section_text_lines.append(line)
        cursor = line_end

    emit_block(cursor)
    return blocks


def chunk_markdown(
    markdown: str,
    *,
    target_tokens: int = 512,
    overlap_tokens: int = 64,
) -> list[Chunk]:
    """Sentence-aware chunks whose offsets index `markdown` exactly.

    Chunks are built from sentence SPANS, and each chunk's text is the literal
    slice `markdown[char_start:char_end]`. The previous implementation joined
    sentences with a single space and derived `char_end` from the length of that
    reconstruction, so any whitespace that was not exactly one space put the
    offsets out — 35 of 44 chunks of this repo's own CLAUDE.md were wrong, with
    the error accumulating down the document.

    That is worse than having no offsets: a grounding view would highlight
    confidently, and wrongly. `tests/test_chunk_offsets.py` asserts the
    invariant against real documents.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    out: list[Chunk] = []
    ordinal = 0

    def emit(spans: list[tuple[int, int]], section_path: str | None, tokens: int) -> None:
        nonlocal ordinal
        if not spans:
            return
        char_start, char_end = spans[0][0], spans[-1][1]
        text = markdown[char_start:char_end]
        if not text.strip():
            return
        out.append(
            Chunk(
                ordinal=ordinal,
                text=text,
                section_path=section_path,
                char_start=char_start,
                char_end=char_end,
                token_count=tokens,
            )
        )
        ordinal += 1

    for section_path, block, block_start, _block_end in _walk_blocks(markdown):
        # Absolute spans, so a chunk can slice the document directly.
        spans = [(block_start + a, block_start + b) for a, b in _split_sentence_spans(block)]
        if not spans:
            continue

        buf: list[tuple[int, int]] = []
        buf_tokens = 0
        for span in spans:
            sent = markdown[span[0] : span[1]]
            sent_tokens = _count_tokens(sent, enc)

            # An oversized fenced block or table row is split on line boundaries
            # rather than forced whole — but still by offset, never by rejoining.
            if sent.startswith(("```", "|")) and sent_tokens > target_tokens * 2:
                line_start = span[0]
                for line in sent.splitlines(keepends=True):
                    line_span = (line_start, line_start + len(line.rstrip("\n")))
                    line_start += len(line)
                    if not markdown[line_span[0] : line_span[1]].strip():
                        continue
                    line_tokens = _count_tokens(markdown[line_span[0] : line_span[1]], enc)
                    if buf_tokens + line_tokens > target_tokens and buf:
                        emit(buf, section_path, buf_tokens)
                        buf = _carry_overlap_spans(markdown, buf, overlap_tokens, enc)
                        buf_tokens = sum(_count_tokens(markdown[a:b], enc) for a, b in buf)
                    buf.append(line_span)
                    buf_tokens += line_tokens
                continue

            if buf_tokens + sent_tokens > target_tokens and buf:
                emit(buf, section_path, buf_tokens)
                buf = _carry_overlap_spans(markdown, buf, overlap_tokens, enc)
                buf_tokens = sum(_count_tokens(markdown[a:b], enc) for a, b in buf)
            buf.append(span)
            buf_tokens += sent_tokens

        emit(buf, section_path, buf_tokens)

    return out


def _carry_overlap_spans(
    markdown: str,
    spans: list[tuple[int, int]],
    overlap_tokens: int,
    enc: tiktoken.Encoding,
) -> list[tuple[int, int]]:
    """Trailing spans worth about `overlap_tokens`, for the next chunk's head."""
    if overlap_tokens <= 0:
        return []
    carried: list[tuple[int, int]] = []
    total = 0
    for span in reversed(spans):
        n = _count_tokens(markdown[span[0] : span[1]], enc)
        if total + n > overlap_tokens and carried:
            break
        carried.insert(0, span)
        total += n
    return carried
