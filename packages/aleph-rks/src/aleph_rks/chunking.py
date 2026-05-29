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


def _split_sentences(text: str) -> list[str]:
    if not text.strip():
        return []
    parts = _SENTENCE_END.split(text)
    # Strip trailing empties; keep order.
    return [p.strip() for p in parts if p.strip()]


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
    enc = tiktoken.get_encoding("cl100k_base")
    out: list[Chunk] = []
    ordinal = 0

    for section_path, block, block_start, _block_end in _walk_blocks(markdown):
        sentences = _split_sentences(block)
        if not sentences:
            continue
        buf: list[str] = []
        buf_tokens = 0
        buf_start = block_start  # char offset of buf[0] within markdown
        cursor_in_block = 0
        for sent in sentences:
            sent_tokens = _count_tokens(sent, enc)
            # Code/table-style line: kept whole if not too large.
            if sent.startswith(("```", "|")) and sent_tokens > target_tokens * 2:
                # Oversized code/table: split on newlines.
                for line in sent.splitlines():
                    line_tokens = _count_tokens(line, enc)
                    if buf_tokens + line_tokens > target_tokens and buf:
                        text = " ".join(buf).strip()
                        out.append(
                            Chunk(
                                ordinal=ordinal,
                                text=text,
                                section_path=section_path,
                                char_start=buf_start,
                                char_end=buf_start + len(text),
                                token_count=buf_tokens,
                            )
                        )
                        ordinal += 1
                        tail = _carry_overlap(buf, overlap_tokens, enc)
                        buf = list(tail)
                        buf_tokens = sum(_count_tokens(t, enc) for t in buf)
                        buf_start = buf_start + len(text) - len(" ".join(buf))
                    buf.append(line)
                    buf_tokens += line_tokens
                continue
            if buf_tokens + sent_tokens > target_tokens and buf:
                text = " ".join(buf).strip()
                out.append(
                    Chunk(
                        ordinal=ordinal,
                        text=text,
                        section_path=section_path,
                        char_start=buf_start,
                        char_end=buf_start + len(text),
                        token_count=buf_tokens,
                    )
                )
                ordinal += 1
                tail = _carry_overlap(buf, overlap_tokens, enc)
                buf = list(tail)
                buf_tokens = sum(_count_tokens(t, enc) for t in buf)
                buf_start = buf_start + len(text) - len(" ".join(buf))
            buf.append(sent)
            buf_tokens += sent_tokens
            cursor_in_block += len(sent)
        if buf:
            text = " ".join(buf).strip()
            out.append(
                Chunk(
                    ordinal=ordinal,
                    text=text,
                    section_path=section_path,
                    char_start=buf_start,
                    char_end=buf_start + len(text),
                    token_count=buf_tokens,
                )
            )
            ordinal += 1
    return out


def _carry_overlap(buf: list[str], overlap_tokens: int, enc: tiktoken.Encoding) -> list[str]:
    """Pick the trailing sentences whose combined token count <= overlap_tokens."""
    tail: list[str] = []
    tokens = 0
    for sent in reversed(buf):
        st = _count_tokens(sent, enc)
        if tokens + st > overlap_tokens:
            break
        tail.append(sent)
        tokens += st
    return list(reversed(tail))
