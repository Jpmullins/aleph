"""Chunk offsets must actually index the text they claim to.

`DocumentChunk.char_start/char_end` are the bottom of the provenance stack:
a claim resolves to a citation, a citation to chunks, and a chunk to a span of
the normalized markdown. Everything above them — the grounding inspector, quote
verification, any Claimify-style extraction — is only as trustworthy as this
arithmetic.

It was wrong. `chunk_markdown` built each chunk's text as `" ".join(buf)` — a
whitespace-normalized *reconstruction* of the sentences, not a substring of the
source — and then computed `char_end = buf_start + len(text)` from the length of
that reconstruction. Any run of whitespace that was not exactly one space put
the offsets out by the difference, and the error accumulated down the document.

That is worse than having no offsets, because it looks authoritative: a
grounding view would highlight confidently, and wrongly.

The invariant below is the whole point of the module and is therefore asserted
against real documents rather than a contrived string.
"""

from __future__ import annotations

import pathlib

import pytest

from aleph_rks.chunking import chunk_markdown

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

LINE_WRAPPED = """# Findings

We show in Fig. 3 that the effect holds across every arm of the trial, and
that it persists under the stricter preregistered threshold. Smith et al. 2020
report a smaller effect, but their sample was drawn from a single site.

The difference is 12% vs. 5% in the replication. See Eq. 2 for the estimator.
"""

TIGHT = "One sentence. Another sentence. A third."

MULTI_BLANK = """# A


Paragraph one, after two blank lines.


Paragraph two.
"""


def _docs() -> list[tuple[str, str]]:
    docs = [
        ("line-wrapped", LINE_WRAPPED),
        ("tight", TIGHT),
        ("multi-blank-line", MULTI_BLANK),
    ]
    readme = REPO_ROOT / "README.md"
    if readme.is_file():
        docs.append(("repo README", readme.read_text(encoding="utf-8")))
    claude = REPO_ROOT / "CLAUDE.md"
    if claude.is_file():
        docs.append(("repo CLAUDE.md", claude.read_text(encoding="utf-8")))
    return docs


@pytest.mark.parametrize(
    ("name", "markdown"), _docs(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "doc"
)
@pytest.mark.parametrize("target_tokens", [120, 512])
def test_offsets_index_the_source(name: str, markdown: str, target_tokens: int) -> None:
    """`markdown[c.char_start:c.char_end]` must BE `c.text`.

    This is the property that makes a chunk id resolvable to a highlightable
    span. Without it, `chunk_ids` on a citation point at nothing checkable.
    """
    chunks = chunk_markdown(markdown, target_tokens=target_tokens)
    assert chunks, f"{name}: chunker produced nothing"

    bad: list[str] = []
    for c in chunks:
        got = markdown[c.char_start : c.char_end]
        if got != c.text:
            bad.append(
                f"  ordinal {c.ordinal}: offsets [{c.char_start}:{c.char_end}] "
                f"select {got[:60]!r} but text is {c.text[:60]!r}"
            )
    assert not bad, (
        f"{name} @ target_tokens={target_tokens}: {len(bad)}/{len(chunks)} chunks "
        f"have offsets that do not index their own text:\n" + "\n".join(bad[:5])
    )


@pytest.mark.parametrize(
    ("name", "markdown"), _docs(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "doc"
)
def test_offsets_are_ordered_and_in_range(name: str, markdown: str) -> None:
    for c in chunk_markdown(markdown, target_tokens=256):
        assert 0 <= c.char_start <= c.char_end <= len(markdown), (
            f"{name}: chunk {c.ordinal} offsets [{c.char_start}:{c.char_end}] "
            f"outside document of length {len(markdown)}"
        )


class TestAbbreviationSafety:
    """The sentence splitter must not shatter scientific prose.

    A split inside "Fig. 3" makes the first fragment a sentence ending in an
    abbreviation and the second start mid-thought. That is bad chunking, and it
    is fatal for any select-then-extract pipeline whose unit is a sentence.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "We show in Fig. 3 that the effect holds.",
            "Smith et al. 2020 report a smaller effect.",
            "The difference is 12% vs. 5% in the replication.",
            "See Eq. 2 for the estimator.",
            "Roughly 40 subjects, i.e. half the cohort, completed it.",
            "Values were low, e.g. 3 per arm, across sites.",
        ],
    )
    def test_stays_one_chunk(self, text: str) -> None:
        chunks = chunk_markdown(text, target_tokens=512)
        assert len(chunks) == 1, (
            f"{text!r} was split into {len(chunks)} chunks: "
            f"{[c.text for c in chunks]} — an abbreviation was read as a "
            f"sentence end."
        )

    def test_real_sentence_boundaries_still_split(self) -> None:
        """The guard must not merge everything into one blob either."""
        text = "First sentence here. Second sentence here. Third sentence here."
        chunks = chunk_markdown(text, target_tokens=4)
        assert len(chunks) > 1, "genuine sentence boundaries are no longer detected"
