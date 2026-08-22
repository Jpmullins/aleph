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


#: How far past `target_tokens` a single unsplittable run is allowed to go
#: before it is cut by character offset.
#:
#: Any margin at all is a choice, and this one is 1.0 — no margin. A chunk is a
#: retrieval unit and a 3,000-token "passage" is a chapter: it dilutes its own
#: embedding, it fills the reranker's window on its own, and it cannot be shown
#: to a reader as a quote.
_HARD_SPLIT_RATIO = 1.0


def _split_oversized_span(
    markdown: str,
    span: tuple[int, int],
    *,
    max_tokens: int,
    enc: tiktoken.Encoding,
) -> list[tuple[int, int]]:
    """Cut one over-long span into contiguous sub-spans under `max_tokens`.

    The chunker had no upper bound. `target_tokens` is enforced by flushing the
    BUFFER before a sentence is added, so a single sentence bigger than the
    target lands in an empty buffer and is emitted whole at whatever size it
    is. Only fenced blocks and table rows were special-cased.

    A document with no sentence terminators — a scanned PDF's text layer, a
    scraped page that lost its punctuation, a long reference list — is one
    "sentence", so it becomes one chunk of arbitrary size. Measured on this
    instance: **19 of 29,206 chunks exceed 8,192 tokens, the largest is 21,543
    (42x the 512 target), and every one of the 19 has `embedding IS NULL`** —
    the embedder refuses them (`Too many input tokens. Max input tokens: 8192`)
    so they are permanently invisible to the dense leg. It also breaks the
    eval, which embeds its seeded corpus in one pass and dies on the first such
    chunk with an HTTP 400.

    Contiguous by construction: sub-span *n* ends exactly where *n+1* begins,
    so `emit`'s `markdown[spans[0][0]:spans[-1][1]]` still slices the document
    and `test_chunk_offsets.py`'s invariant holds unchanged.

    Cut points prefer a newline, then a space, and fall back to a bare
    character boundary. The fallback is not optional: a 20,000-character run
    with no whitespace at all is a real thing (a base64 blob in a scraped
    page), and refusing to cut it would put the unbounded chunk straight back.

    The guarantee this buys is on the SPAN, not on the finished chunk: the
    packing loop flushes the buffer *before* adding a piece that would exceed
    the target, so a chunk can still reach just under `2 * target_tokens`.
    Measured on 738 real documents, the largest chunk goes from 10,732 tokens
    to 1,023. Stated as a bound rather than left implied because "under the
    target" would be the natural reading and it is not true.
    """
    start, end = span
    if start >= end or _count_tokens(markdown[start:end], enc) <= max_tokens:
        return [span]

    out: list[tuple[int, int]] = []
    while start < end:
        remainder = markdown[start:end]
        if _count_tokens(remainder, enc) <= max_tokens:
            out.append((start, end))
            break
        # Characters per token, measured on THIS text rather than assumed: a
        # table of numbers and a paragraph of English differ by more than 2x,
        # and a fixed ratio would either overshoot (and need many shrink
        # passes) or undershoot (and produce chunks a quarter of the target).
        ratio = len(remainder) / max(1, _count_tokens(remainder, enc))
        cut = start + max(1, int(max_tokens * ratio))
        cut = min(cut, end)
        # Only look for a boundary in the last fifth of the window, so a cut
        # never gives up most of a chunk to find a prettier place to break.
        floor = start + max(1, (cut - start) * 4 // 5)
        for sep in ("\n", " "):
            found = markdown.rfind(sep, floor, cut)
            if found > start:
                cut = found + len(sep)
                break
        # The ratio is an estimate, so verify and shrink. Geometric, so it
        # terminates; `> start + 1` keeps it from ever producing an empty span.
        while cut > start + 1 and _count_tokens(markdown[start:cut], enc) > max_tokens:
            cut = start + max(1, (cut - start) * 3 // 4)
        out.append((start, cut))
        start = cut
    return out


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
    confidently, and wrongly. `packages/aleph-rks/tests/test_chunk_offsets.py`
    asserts the invariant against real documents.
    """
    enc = tiktoken.get_encoding("cl100k_base")
    hard_max = max(1, int(target_tokens * _HARD_SPLIT_RATIO))
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
                    # A single LINE can be over the target too — one enormous
                    # table row, or a fenced block written without newlines.
                    # Splitting the fence into lines and then emitting a line
                    # whole reintroduces the unbounded chunk one level down.
                    for piece in _split_oversized_span(
                        markdown, line_span, max_tokens=hard_max, enc=enc
                    ):
                        piece_tokens = _count_tokens(markdown[piece[0] : piece[1]], enc)
                        if buf_tokens + piece_tokens > target_tokens and buf:
                            emit(buf, section_path, buf_tokens)
                            buf = _carry_overlap_spans(markdown, buf, overlap_tokens, enc)
                            buf_tokens = sum(_count_tokens(markdown[a:b], enc) for a, b in buf)
                        buf.append(piece)
                        buf_tokens += piece_tokens
                continue

            # One sentence bigger than the target used to be emitted at
            # whatever size it happened to be, because the size check flushes
            # the BUFFER and an oversized sentence lands in an empty one. See
            # `_split_oversized_span` for what that costs on real documents.
            for piece in _split_oversized_span(markdown, span, max_tokens=hard_max, enc=enc):
                piece_tokens = _count_tokens(markdown[piece[0] : piece[1]], enc)
                if buf_tokens + piece_tokens > target_tokens and buf:
                    emit(buf, section_path, buf_tokens)
                    buf = _carry_overlap_spans(markdown, buf, overlap_tokens, enc)
                    buf_tokens = sum(_count_tokens(markdown[a:b], enc) for a, b in buf)
                buf.append(piece)
                buf_tokens += piece_tokens

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
