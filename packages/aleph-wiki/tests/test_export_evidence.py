"""The evidence half of the vault export (WS-H8): the section and the sidecar.

Pure format, no database — `tests/integration/test_vault_evidence_export.py`
drives the query and the route. Every assertion here is on production code in
`aleph_wiki.export_evidence`; the dataclasses are the module's own view model,
not a stand-in for it.

The two assertions that matter most are the ones that look like formatting
trivia and are not:

* a quote is rendered inside a code fence, because the vault exporter rewrites
  `[[` everywhere except inside a fence, and a rewritten quote is no longer the
  words the source used; and
* `render_evidence_json(parse_evidence_json(x)) == x`, because a field written
  and never read is this codebase's dominant defect class, and in a file format
  it is completely silent.
"""

from __future__ import annotations

import json

import pytest

from aleph_wiki.export_evidence import (
    EVIDENCE_FILENAME,
    EVIDENCE_FORMAT_VERSION,
    HEADER_FIELDS,
    MARKER_CLOSE,
    MARKER_OPEN,
    ClaimEvidence,
    EvidenceCitation,
    EvidenceHeader,
    PageEvidence,
    _citation_line,
    attach_evidence,
    count_evidence,
    evidence_files,
    normalize_marker,
    parse_evidence_json,
    render_evidence_json,
    render_evidence_section,
    strip_evidence_section,
)

QUOTE = "We propose a new simple network architecture, the Transformer"


def _citation(**overrides: object) -> EvidenceCitation:
    base: dict[str, object] = {
        "marker": "c1",
        "stance": "supports",
        "weight": 1.0,
        "verbatim": True,
        "source_id": "01a02790-c0e6-7eea-a5f1-ca3c87b89f0d",
        "source_short_id": "S0002",
        "source_title": "Attention Is All You Need",
        "source_url": "https://arxiv.org/abs/1706.03762",
        "chunk_id": "01a02791-2c83-76a1-823f-e2907e99eb40",
        "quote": QUOTE,
        "char_start": 1024,
        "char_end": 1024 + len(QUOTE),
    }
    base.update(overrides)
    return EvidenceCitation(**base)  # pyright: ignore[reportArgumentType]


def _claim(**overrides: object) -> ClaimEvidence:
    base: dict[str, object] = {
        "claim_id": "01a02799-6486-7f4c-8a26-a58cc2a01999",
        "text": "The transformer removed recurrence entirely.",
        "confidence": "weakly_supported",
        "evidence_tier": "cited",
        "origin": "agent",
        "status": "active",
        "section_anchor": "architecture",
        "citations": (_citation(),),
    }
    base.update(overrides)
    return ClaimEvidence(**base)  # pyright: ignore[reportArgumentType]


def _page(**overrides: object) -> PageEvidence:
    base: dict[str, object] = {
        "slug": "attention-is-all-you-need",
        "title": "Attention Is All You Need",
        "claims": (_claim(),),
    }
    base.update(overrides)
    return PageEvidence(**base)  # pyright: ignore[reportArgumentType]


# --- the section a person reads --------------------------------------------


def test_the_section_carries_the_whole_chain() -> None:
    """Claim, source, span, chunk and the quote itself — in one block.

    Each of these is a link in claim → citation → source → chunk → character
    span. A section missing any one of them exports a bibliography: you can see
    that the page cites a paper and not why the sentence is believed.
    """
    section = render_evidence_section(_page())
    assert "The transformer removed recurrence entirely." in section
    assert "S0002" in section
    assert "Attention Is All You Need" in section
    assert f"chars 1024-{1024 + len(QUOTE)}" in section
    assert "01a02791-2c83-76a1-823f-e2907e99eb40" in section
    assert QUOTE in section
    assert section.startswith(MARKER_OPEN)
    assert section.rstrip("\n").endswith(MARKER_CLOSE)


def test_a_page_with_no_claims_gets_no_heading() -> None:
    """An empty `## Evidence` heading reads as "checked, found nothing".

    What it would actually mean is that the belief layer has never run over the
    page — 709 non-stub pages against a claim spine that covers a fraction of
    them. The counts in the dry-run report are where "measured, and zero"
    belongs; a heading cannot say it.
    """
    assert render_evidence_section(_page(claims=())) == ""
    assert attach_evidence("Body.\n", _page(claims=())) == "Body.\n"


def test_the_quote_sits_inside_a_code_fence() -> None:
    """Not cosmetic: the vault exporter rewrites `[[` outside fences only.

    `render_vault` turns `[[x]]` into a link and escapes any residual `[[`,
    skipping fenced blocks. A quote is bytes somebody else wrote — rewriting
    them would edit the source's words and, in the obsidian dialect, invent a
    link the source never contained.
    """
    section = render_evidence_section(_page())
    lines = section.splitlines()
    quote_line = lines.index(QUOTE)
    before = [i for i, line in enumerate(lines[:quote_line]) if line.startswith("```")]
    after = [i for i, line in enumerate(lines[quote_line:]) if line.startswith("```")]
    assert len(before) % 2 == 1, "the quote is not inside an open fence"
    assert after, "the fence around the quote is never closed"


def test_a_quote_containing_backticks_gets_a_longer_fence() -> None:
    """CommonMark closes a fence on a run at least as long as the opener, so a
    three-backtick fence around a quote containing ``` ends at the quote."""
    quote = "the ``` marker opens a block"
    section = render_evidence_section(_page(claims=(_claim(citations=(_citation(quote=quote),)),)))
    assert quote in section
    assert "````text" in section


def test_a_quote_whose_own_line_opens_a_fence_is_not_inlined() -> None:
    """The one case a longer fence cannot save.

    A quote line starting with ``` closes this block early AND desyncs the
    exporter's fence tracker for the rest of the page — from there down,
    `[[wikilinks]]` silently stop being rewritten. The sidecar still carries
    the quote byte-exact, so nothing is lost; it is not pasted into the prose.
    """
    quote = "before\n```python\nx = 1\n```\nafter"
    page = _page(claims=(_claim(citations=(_citation(quote=quote),)),))
    section = render_evidence_section(page)
    assert "```python" not in section
    assert EVIDENCE_FILENAME in section
    # And the sidecar really does carry it, so the pointer is not a dead end.
    _, parsed = parse_evidence_json(render_evidence_json([page], project_title="T", dialect="okf"))
    assert parsed[0].claims[0].citations[0].quote == quote


def test_a_retracted_claim_says_so() -> None:
    """It stays in the export — "we believed this and withdrew it" is part of
    the record — and it must never read as support. 810 of the 8,056 live
    claims are retracted."""
    section = render_evidence_section(_page(claims=(_claim(status="retracted"),)))
    assert "`status: retracted`" in section
    # An active claim carries no status noise: `contested: false` on every page
    # is what trains a reader to skip the block.
    assert "status:" not in render_evidence_section(_page())


def test_an_unverified_citation_is_flagged() -> None:
    """`verbatim=False` means nobody located the quote in the source. The rest
    of the section reads as though every quote was checked."""
    section = render_evidence_section(
        _page(claims=(_claim(citations=(_citation(verbatim=False),)),))
    )
    assert "*unverified*" in section


def test_a_claim_with_no_citation_says_that_too() -> None:
    section = render_evidence_section(_page(claims=(_claim(citations=()),)))
    assert "No citation anchors this claim." in section


def test_a_source_row_that_is_gone_still_renders() -> None:
    """Evidence pointing at a deleted source is a fact worth exporting; a
    dropped line is not."""
    section = render_evidence_section(
        _page(
            claims=(
                _claim(
                    citations=(
                        _citation(source_short_id=None, source_title=None, source_url=None),
                    ),
                ),
            )
        )
    )
    assert "01a02790-c0e6-7eea-a5f1-ca3c87b89f0d" in section


# --- attaching to a body ----------------------------------------------------


def test_attaching_twice_does_not_stack_two_sections() -> None:
    """The iterate step of WS-H8 is a worker that re-exports continuously, so
    re-export over a previous export is the steady state, not an edge case."""
    once = attach_evidence("Body.\n", _page())
    twice = attach_evidence(once, _page())
    assert once == twice
    assert once.count(MARKER_OPEN) == 1


def test_the_prose_survives_attaching() -> None:
    attached = attach_evidence("# Title\n\nProse the page was written with.\n", _page())
    assert attached.startswith("# Title\n\nProse the page was written with.")
    assert MARKER_OPEN in attached


def test_stripping_removes_the_block_and_nothing_else() -> None:
    body = "Before.\n"
    attached = attach_evidence(body, _page())
    assert strip_evidence_section(attached).rstrip("\n") == "Before."


def test_a_bodyless_page_still_gets_its_evidence() -> None:
    """One live page is non-stub with no revision at all. Its claims are the
    only content it has, and dropping them because the body is empty would
    export a file that says nothing about a page the belief layer knows."""
    attached = attach_evidence("", _page())
    assert attached.startswith(MARKER_OPEN)


# --- the sidecar ------------------------------------------------------------


def test_the_sidecar_round_trips_byte_for_byte() -> None:
    """A field written and never read is invisible in a file format. This is
    the check that makes it a diff."""
    pages = [_page(), _page(slug="beta", title="Beta", claims=(_claim(citations=()),))]
    once = render_evidence_json(pages, project_title="Test Wiki", dialect="okf")
    _, parsed = parse_evidence_json(once)
    twice = render_evidence_json(parsed, project_title="Test Wiki", dialect="okf")
    assert once == twice


def test_every_field_of_a_citation_survives_the_round_trip() -> None:
    """Byte-identity alone would pass if both directions dropped the same
    field, so the values are compared as well."""
    _, parsed = parse_evidence_json(
        render_evidence_json([_page()], project_title="T", dialect="okf")
    )
    got = parsed[0].claims[0].citations[0]
    assert got == _citation()


def test_a_span_starting_at_offset_zero_is_still_anchored() -> None:
    """`if char_end` would report the first sentence of a document — the
    best-anchored citation there is — as unanchored."""
    counts = count_evidence(
        [_page(claims=(_claim(citations=(_citation(char_start=0, char_end=0),)),))]
    )
    assert counts.anchored_citations == 1


def test_a_citation_with_no_quote_is_counted_but_not_anchored() -> None:
    """Half the live corpus's citations are in this state: 8,079 citations,
    4,082 of them carrying a quote and a span. One number would report the
    corpus as fully evidenced."""
    counts = count_evidence(
        [
            _page(
                claims=(_claim(citations=(_citation(quote=None, char_start=None, char_end=None),)),)
            )
        ]
    )
    assert (counts.citations, counts.anchored_citations) == (1, 0)


def test_no_sidecar_for_a_project_with_no_claims() -> None:
    assert evidence_files([_page(claims=())], project_title="T", dialect="okf") == {}


def test_the_sidecar_is_named_and_versioned() -> None:
    files = evidence_files([_page()], project_title="T", dialect="okf")
    assert set(files) == {EVIDENCE_FILENAME}
    assert json.loads(files[EVIDENCE_FILENAME])["aleph_evidence_version"] == (
        EVIDENCE_FORMAT_VERSION
    )


def test_the_sidecar_is_not_markdown() -> None:
    """OKF v0.1 reads every `.md` that is not `index.md` or `log.md` as a
    concept, so a markdown sidecar would be a page the wiki does not have."""
    assert not EVIDENCE_FILENAME.endswith(".md")


def test_the_sidecar_carries_no_timestamp() -> None:
    """A "generated at" stamp makes two exports of an unchanged corpus differ,
    which destroys the byte-identical round trip that is the only evidence the
    format loses nothing."""
    rendered = render_evidence_json([_page()], project_title="T", dialect="okf")
    document = json.loads(rendered)
    assert not [k for k in document if "time" in k or "date" in k or k.endswith("_at")]


def test_a_version_this_reader_does_not_know_is_refused() -> None:
    """Parsing an unknown version on the assumption the keys line up is how a
    format silently loses a field across an upgrade."""
    rendered = render_evidence_json([_page()], project_title="T", dialect="okf")
    bumped = rendered.replace(f'"{EVIDENCE_FORMAT_VERSION}"', '"99"', 1)
    with pytest.raises(ValueError, match="version"):
        parse_evidence_json(bumped)


def test_a_hand_edited_sidecar_reports_a_loss_instead_of_crashing() -> None:
    """`claims: null` is a plausible hand edit. A traceback three frames down
    in a private helper tells the person nothing; an empty claim list shows up
    as a diff against the export they started from."""
    rendered = render_evidence_json([_page()], project_title="T", dialect="okf")
    document = json.loads(rendered)
    document["pages"][0]["claims"] = None
    _, parsed = parse_evidence_json(json.dumps(document))
    assert parsed[0].claims == ()


def test_a_quote_containing_a_wikilink_is_not_inlined_either() -> None:
    """The okf criterion is `grep -c '[[' over the bundle returns 0`, and
    `scripts/check-okf.py` reports a residual `[[` even inside a code fence —
    deliberately, because an OKF consumer cannot tell a wikilink from a list
    literal either. Escaping it would satisfy the validator by editing the
    source's words, so the quote is carried in the sidecar instead.
    """
    quote = "the notation [[x]] denotes a nested list"
    page = _page(claims=(_claim(citations=(_citation(quote=quote),)),))
    section = render_evidence_section(page)
    assert "[[" not in section
    assert EVIDENCE_FILENAME in section
    _, parsed = parse_evidence_json(render_evidence_json([page], project_title="T", dialect="okf"))
    assert parsed[0].claims[0].citations[0].quote == quote


def test_markdown_inside_a_quote_is_shown_and_not_obeyed() -> None:
    """The fence's actual job, asserted on the bytes between the fences.

    Unfenced, `# Introduction` becomes a heading in the page's outline and the
    asterisks around `*Attention*` are deleted by the renderer — so a section
    that says "verbatim" shows a quote missing characters the source had. The
    fenced block is compared to the quote exactly, so removing the fence, or
    trimming a line inside it, fails here.
    """
    quote = "# Introduction\n\n*Attention* is all you need.\n- and a list item"
    section = render_evidence_section(_page(claims=(_claim(citations=(_citation(quote=quote),)),)))
    lines = section.splitlines()
    opens = [i for i, line in enumerate(lines) if line.startswith("```")]
    assert len(opens) == 2, "the quote is not wrapped in exactly one fenced block"
    assert "\n".join(lines[opens[0] + 1 : opens[1]]) == quote


# ---------------------------------------------------------------------------
# The marker shape production actually writes
# ---------------------------------------------------------------------------
#
# Every fixture above builds an `EvidenceCitation` directly, with `marker="c1"`.
# Production does not: `claim_extraction` writes `citation_marker=f"[c{n}]"`
# and 8,017 of 8,081 markers in the live database are bracketed. The exporter
# wrapped that in brackets AGAIN, so a real export rendered `**[[c4]]**` — a
# live Obsidian wikilink to a page Aleph invented, one per citation, and in the
# OKF dialect a marker silently rewritten from `[c4]` to `c4`. 163 tests passed
# over a format corrupt in 100% of real exports, because no fixture ever used
# the shape the writer produces.


def test_the_stored_bracketed_marker_is_unwrapped_exactly_once() -> None:
    assert normalize_marker("[c4]") == "c4"
    assert normalize_marker("c4") == "c4", "already-bare markers must survive"
    assert normalize_marker("[[c4]]") == "c4", "a doubly-wrapped marker is still c4"
    assert normalize_marker("  [c10] ") == "c10"
    assert normalize_marker(None) == ""
    # Not every marker is `cN`. A label that is neither shape is left alone
    # rather than mangled into something else.
    assert normalize_marker("fig-2") == "fig-2"
    assert normalize_marker("4") == "4"


def test_a_production_shaped_marker_does_not_render_a_wikilink() -> None:
    """`[c4]` in the row must not become `[[c4]]` on the page.

    Asserted on the rendered line rather than on the field, because the field
    was never the bug — the bug was one more pair of brackets at render time.
    """
    citation = _citation(marker=normalize_marker("[c4]"))
    line = _citation_line(citation)
    assert "[[" not in line, f"the exporter emitted a wikilink: {line}"
    assert "**[c4]**" in line, line


def test_the_evidence_section_of_a_real_page_carries_no_invented_wikilink() -> None:
    """End to end, at the section level, with the production marker shape."""
    page = _page(
        claims=(
            _claim(
                citations=tuple(_citation(marker=normalize_marker(f"[c{n}]")) for n in (1, 2, 10))
            ),
        )
    )
    section = render_evidence_section(page)
    assert "[[" not in section, (
        "the evidence section contains a wikilink the corpus never wrote — "
        "every one of these becomes a dangling link in the export report"
    )
    for number in (1, 2, 10):
        assert f"**[c{number}]**" in section, f"c{number} is missing from the section"


# ---------------------------------------------------------------------------
# Free text that is not a quote
#
# `_quote_block` guarded the quote and nothing else, and the section is built
# from four database strings. Claim text and source titles are unbounded free
# text and were interpolated raw, so the exporter rewrote them: a claim reading
# `The paper called it [[Recurrent Networks]] throughout.` left the okf bundle
# as `[Recurrent Networks](./recurrent-networks.md)`. These pin the section
# half; `tests/unit/test_vault_evidence_bundle.py` pins the same properties
# after the real exporter has run over the page, which is where the rewrite
# actually happened.
# ---------------------------------------------------------------------------


def test_a_claim_that_mentions_a_wikilink_does_not_become_one() -> None:
    """The claim's words are the project's own sentence. Nothing between the
    row and the reader may turn part of it into a link to somewhere else."""
    text = "The paper called it [[Recurrent Networks]] throughout."
    section = render_evidence_section(_page(claims=(_claim(text=text),)))
    assert "[[" not in section
    # The words survive: `\[\[` renders as the literal characters `[[`.
    assert "The paper called it \\[\\[Recurrent Networks\\]\\] throughout." in section


def test_a_source_title_that_mentions_a_wikilink_does_not_become_one() -> None:
    """Measured before the fix: `A survey of [[wikilink]] syntax` reached the
    okf bundle as `A survey of wikilink syntax` — the exporter could not
    resolve the invented target, so it wrote the display text and deleted the
    brackets from somebody's title."""
    section = render_evidence_section(
        _page(
            claims=(_claim(citations=(_citation(source_title="A survey of [[wikilink]] syntax"),)),)
        )
    )
    assert "[[" not in section
    assert "A survey of \\[\\[wikilink\\]\\] syntax" in section


def test_a_claim_containing_a_code_fence_cannot_open_one() -> None:
    """A fence line inside the claim text desyncs `render_vault`'s line-by-line
    fence tracker, which switches off link rewriting for the rest of the page.
    Whitespace is collapsed, so the claim occupies one line and the only fence
    lines in the section are the pair this module emits around the quote."""
    text = "before\n```python\nx = 1"
    section = render_evidence_section(_page(claims=(_claim(text=text),)))
    fences = [line for line in section.splitlines() if line.lstrip().startswith(("```", "~~~"))]
    assert len(fences) == 2, fences
    assert "**Claim.** before ```python x = 1" in section


def test_a_backtick_in_an_identifier_cannot_escape_its_code_span() -> None:
    """`S0002` and the chunk id are rendered as code spans and both are
    columns. A backtick in one closes the span and the rest of the line — the
    source, the offsets, the url — becomes code."""
    section = render_evidence_section(
        _page(claims=(_claim(citations=(_citation(source_short_id="S`0002"),)),))
    )
    line = next(line for line in section.splitlines() if "0002" in line)
    assert "``S`0002``" in line, line
    assert "supports" in line, "the rest of the citation line was swallowed by the code span"


def test_the_summary_sentence_states_the_counts_it_measured() -> None:
    """The sentence a person reads, checked against the page it describes.

    `count_evidence` is covered on its own above; what is unpinned is that the
    sentence reports *that* number. Reporting `EvidenceCounts()` here — a page
    announcing "0 claims, 0 citations, 0 anchored" above twenty anchored
    citations — left all 163 tests green.
    """
    page = PageEvidence(
        slug="two",
        title="Two",
        claims=(
            _claim(citations=(_citation(), _citation(quote=None, char_start=None, char_end=None))),
            _claim(text="A second claim.", citations=(_citation(),)),
        ),
    )
    assert "2 claims, 3 citations, 2 anchored" in render_evidence_section(page)


def test_the_summary_sentence_is_singular_for_one_of_each() -> None:
    assert "1 claim, 1 citation, 1 anchored" in render_evidence_section(_page())


# --- the header -------------------------------------------------------------


def test_the_header_is_read_back_as_values() -> None:
    """`project_title` and `dialect` were written by `evidence_document`,
    re-supplied by both call sites and read by nobody — deleting either from
    the writer left every test green. They are read here."""
    header, _ = parse_evidence_json(
        render_evidence_json([_page()], project_title="Test Wiki", dialect="obsidian")
    )
    assert header == EvidenceHeader(
        version=EVIDENCE_FORMAT_VERSION,
        project_title="Test Wiki",
        dialect="obsidian",
        claim_count=1,
        citation_count=1,
        anchored_citation_count=1,
    )


def test_the_bytes_are_rebuilt_from_the_header_the_reader_returned() -> None:
    """The round trip, driven by the parsed header rather than by the constants
    the test passed in. A writer that stopped emitting `dialect` would produce
    bytes this cannot reconstruct."""
    once = render_evidence_json([_page()], project_title="Test Wiki", dialect="okf")
    header, pages = parse_evidence_json(once)
    twice = render_evidence_json(pages, project_title=header.project_title, dialect=header.dialect)
    assert once == twice


@pytest.mark.parametrize("field", HEADER_FIELDS)
def test_a_sidecar_missing_a_header_field_is_refused(field: str) -> None:
    """Byte-identity cannot see a field dropped from BOTH directions at once:
    the writer stops writing it, the reader never asked, and the two agree
    about a file that has lost something. Only a required key can."""
    document = json.loads(render_evidence_json([_page()], project_title="T", dialect="okf"))
    del document[field]
    with pytest.raises(ValueError, match=r"version|missing"):
        parse_evidence_json(json.dumps(document))


def test_the_header_counts_are_the_counts_of_the_pages_it_carries() -> None:
    """A header nobody re-derives is how "the export carries 8,056 claims"
    becomes a number that is simply wrong."""
    pages = [_page(), _page(slug="beta", title="Beta", claims=(_claim(citations=()),))]
    header, parsed = parse_evidence_json(
        render_evidence_json(pages, project_title="T", dialect="okf")
    )
    counted = count_evidence(parsed)
    assert header.counts.claims == counted.claims == 2
    assert header.counts.citations == counted.citations == 1
    assert header.counts.anchored_citations == counted.anchored_citations == 1
