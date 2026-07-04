"""Deterministic, LLM-free markdown citation style pass.

`style_pass`:

1. Renumbers `[N]` citation markers to first-appearance order
   (`[3][1][3]` becomes `[1][2][1]`).
2. Rebuilds the `## References` numbered list to match the new order.
   Entries whose number is never cited in the body move verbatim under a
   `### Further reading` stanza (unnumbered, preserved text).
3. Collapses runs of more than 2 blank lines down to 2.

Pure function; property-tested idempotent:
`style_pass(style_pass(x)) == style_pass(x)`. Documents without markers or
without a references section pass through (modulo blank-line collapse).
"""

from __future__ import annotations

import re

# A citation marker is [digits] NOT followed by "(" (markdown link) and NOT
# part of a link text like [1](http...). Reference-list definition lines
# ("[1] Some entry" at line start) are handled separately.
_MARKER_RE = re.compile(r"\[(\d+)\](?!\()")
_REFERENCES_HEADING_RE = re.compile(r"^##\s+references\s*$", re.IGNORECASE)
_FURTHER_READING_HEADING_RE = re.compile(r"^#{2,4}\s+further\s+reading\s*$", re.IGNORECASE)
_ENTRY_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)\.)\s+(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s")


def _split_references(lines: list[str]) -> tuple[list[str], list[str], int] | None:
    """Split into (body_lines, references_section_lines, resume_index).

    The references section runs from the `## References` heading to the next
    `##`-or-shallower heading (exclusive) or EOF. Returns None when the
    document has no references section.
    """
    for i, line in enumerate(lines):
        if _REFERENCES_HEADING_RE.match(line.strip()):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if (
                    _HEADING_RE.match(stripped)
                    and not stripped.startswith("###")
                    and not _REFERENCES_HEADING_RE.match(stripped)
                ):
                    end = j
                    break
            return lines[:i], lines[i:end], end
    return None


def _parse_entries(section_lines: list[str]) -> tuple[dict[int, str], list[str]]:
    """Parse `[N] text` / `N. text` entries; keep a Further-reading tail verbatim.

    Continuation lines (non-empty, non-entry) append to the current entry.
    """
    entries: dict[int, str] = {}
    further: list[str] = []
    current: int | None = None
    in_further = False
    for line in section_lines[1:]:  # skip the "## References" heading itself
        if _FURTHER_READING_HEADING_RE.match(line.strip()):
            in_further = True
            current = None
            continue
        if in_further:
            further.append(line)
            continue
        match = _ENTRY_RE.match(line)
        if match:
            number = int(match.group(1) or match.group(2))
            entries[number] = match.group(3).strip()
            current = number
        elif line.strip() and current is not None:
            entries[current] = f"{entries[current]} {line.strip()}"
        else:
            current = None
    while further and not further[0].strip():
        further.pop(0)
    while further and not further[-1].strip():
        further.pop()
    return entries, further


def _collapse_blank_lines(text: str) -> str:
    """Collapse runs of more than 2 blank lines down to 2."""
    out: list[str] = []
    blanks = 0
    for line in text.split("\n"):
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 2:
                out.append(line)
    return "\n".join(out)


def style_pass(markdown: str) -> str:
    """Apply the deterministic citation style pass. Idempotent."""
    lines = markdown.split("\n")
    split = _split_references(lines)
    if split is None:
        body_lines, section_lines, tail_lines = lines, [], []
    else:
        body_lines, section_lines, resume = split
        tail_lines = lines[resume:]

    body = "\n".join(body_lines)

    # First-appearance renumbering over the body only.
    order: dict[int, int] = {}

    def _renumber(match: re.Match[str]) -> str:
        old = int(match.group(1))
        if old not in order:
            order[old] = len(order) + 1
        return f"[{order[old]}]"

    body = _MARKER_RE.sub(_renumber, body)

    segments: list[str] = []
    if body.strip() or not section_lines:
        segments.append(body.rstrip("\n") if section_lines else body)
    if section_lines:
        entries, further = _parse_entries(section_lines)
        ref_lines: list[str] = ["## References", ""]
        cited = sorted(order.items(), key=lambda kv: kv[1])  # (old, new) by new number
        for old, new in cited:
            if old in entries:
                ref_lines.append(f"[{new}] {entries[old]}")
        orphaned = [entries[n] for n in sorted(entries) if n not in order]
        if orphaned or further:
            ref_lines.append("")
            ref_lines.append("### Further reading")
            ref_lines.append("")
            ref_lines.extend(orphaned)
            ref_lines.extend(further)
        segments.append("\n".join(ref_lines))
    if tail_lines:
        segments.append("\n".join(tail_lines).rstrip("\n"))

    return _collapse_blank_lines("\n\n".join(segments))
