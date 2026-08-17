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

# A citation marker is [digits] or [cdigits], NOT followed by "(" (markdown
# link) and NOT part of a link text like [1](http...). Reference-list
# definition lines ("[1] Some entry" at line start) are handled separately.
#
# Both forms are accepted because the two producers in this repo disagree:
# hand-authored and reviewer prose use `[N]`, while the research composer emits
# `[cN]` (aleph_research.research_workflow builds `c1..cN` and its prompt
# mandates that form). This module previously matched `[N]` only, so every
# research report passed through the renumbering and reference-rebuild
# untouched — the pass ran and did nothing. Whichever form a marker uses is
# preserved on output; a document is not rewritten into the other convention.
_MARKER_RE = re.compile(r"\[(c?)(\d+)\](?!\()")
_REFERENCES_HEADING_RE = re.compile(r"^##\s+references\s*$", re.IGNORECASE)
_FURTHER_READING_HEADING_RE = re.compile(r"^#{2,4}\s+further\s+reading\s*$", re.IGNORECASE)
_ENTRY_RE = re.compile(r"^\s*(?:\[(c?)(\d+)\]|(\d+)\.)\s+(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s")


def _marker_key(prefix: str, number: int | str) -> str:
    """Identity of a citation. `[1]` and `[c1]` are distinct citations, not aliases."""
    return f"{prefix}{number}"


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


def _parse_entries(section_lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse `[N] text` / `[cN] text` / `N. text` entries; keep Further-reading verbatim.

    Keys are marker identities (`"1"`, `"c1"`), matching what `_MARKER_RE`
    yields, so an entry is looked up by the exact citation that cites it.
    Continuation lines (non-empty, non-entry) append to the current entry.
    """
    entries: dict[str, str] = {}
    further: list[str] = []
    current: str | None = None
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
            prefix, bracketed, dotted, text = match.groups()
            key = _marker_key(prefix or "", bracketed or dotted)
            entries[key] = text.strip()
            current = key
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

    # First-appearance renumbering over the body only. Keyed by marker identity
    # so `[1]` and `[c1]` renumber independently, and each keeps its own form.
    order: dict[str, int] = {}

    def _renumber(match: re.Match[str]) -> str:
        prefix, number = match.group(1), match.group(2)
        key = _marker_key(prefix, number)
        if key not in order:
            order[key] = len(order) + 1
        return f"[{prefix}{order[key]}]"

    body = _MARKER_RE.sub(_renumber, body)

    segments: list[str] = []
    if body.strip() or not section_lines:
        segments.append(body.rstrip("\n") if section_lines else body)
    if section_lines:
        entries, further = _parse_entries(section_lines)
        ref_lines: list[str] = ["## References", ""]
        cited = sorted(order.items(), key=lambda kv: kv[1])  # (key, new) by new number
        for key, new in cited:
            if key in entries:
                prefix = "c" if key.startswith("c") else ""
                ref_lines.append(f"[{prefix}{new}] {entries[key]}")
        orphaned = [entries[k] for k in sorted(entries) if k not in order]
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
