"""The evidence section, put through the real vault exporter and validator.

`packages/aleph-wiki/tests/test_export_evidence.py` checks the section and the
sidecar in isolation. What it cannot check is the join, because the two halves
live in packages that must not import each other: `aleph_artifacts` depends on
`aleph_wiki`, so a test inside `aleph-wiki` cannot reach `render_vault` without
inverting the dependency. This file sits above both.

Everything asserted here is a property of the *combination*, and each one has
already been a defect somewhere in this tree:

* a fenced quote survives the okf dialect's `[[` escaping byte-for-byte, so the
  export does not quietly edit somebody else's words;
* the bundle still validates against OKF v0.1 with the evidence in it — run as
  a subprocess over real bytes, because a claim about a file format is worth
  what a validator says about the bytes and nothing else; and
* export → import → export stays byte-identical with an evidence section
  present, which is the only way to know the section does not accumulate.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aleph_artifacts.exporters.vault import VaultPage, parse_vault, render_vault
from aleph_wiki.export_evidence import (
    EVIDENCE_FILENAME,
    ClaimEvidence,
    EvidenceCitation,
    PageEvidence,
    attach_evidence,
    evidence_files,
    parse_evidence_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_OKF = REPO_ROOT / "scripts" / "check-okf.py"

DIALECTS = ("obsidian", "okf")

#: A quote that would be corrupted if it were not fenced. `[[Attention]]` is
#: the exact shape `render_vault` rewrites, and a source that discusses wiki
#: syntax is not exotic — this corpus ingests documentation.
QUOTE_WITH_BRACKETS = "the page linked it as [[Attention]] in the body"
QUOTE_PLAIN = "We propose a new simple network architecture, the Transformer"


CLAIM_TEXT = "The transformer removed recurrence entirely."
SOURCE_TITLE = "Attention Is All You Need"


def _evidence(
    quote: str,
    *,
    slug: str = "attention",
    title: str = "Attention",
    claim_text: str = CLAIM_TEXT,
    source_title: str = SOURCE_TITLE,
) -> PageEvidence:
    return PageEvidence(
        slug=slug,
        title=title,
        claims=(
            ClaimEvidence(
                claim_id="01a02799-6486-7f4c-8a26-a58cc2a01999",
                text=claim_text,
                confidence="weakly_supported",
                evidence_tier="cited",
                origin="agent",
                status="active",
                citations=(
                    EvidenceCitation(
                        marker="c1",
                        stance="supports",
                        weight=1.0,
                        verbatim=True,
                        source_id="01a02790-c0e6-7eea-a5f1-ca3c87b89f0d",
                        source_short_id="S0002",
                        source_title=source_title,
                        source_url="https://arxiv.org/abs/1706.03762",
                        chunk_id="01a02791-2c83-76a1-823f-e2907e99eb40",
                        quote=quote,
                        char_start=1024,
                        char_end=1024 + len(quote),
                    ),
                ),
            ),
        ),
    )


def _pages(
    quote: str, *, claim_text: str = CLAIM_TEXT, source_title: str = SOURCE_TITLE
) -> list[VaultPage]:
    evidence = _evidence(quote, claim_text=claim_text, source_title=source_title)
    return [
        VaultPage(
            title="Attention",
            slug="attention",
            body_md=attach_evidence(
                "The paper that introduced the transformer. See [[Recurrent Networks]].\n",
                evidence,
            ),
            page_type="concept",
            category="architectures",
        ),
        VaultPage(
            title="Recurrent Networks",
            slug="recurrent-networks",
            body_md="Networks with a hidden state carried across steps.\n",
        ),
    ]


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    out = tmp_path / "vault"
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return out


def _bundle(
    dialect: str, quote: str, *, claim_text: str = CLAIM_TEXT, source_title: str = SOURCE_TITLE
) -> dict[str, str]:
    export = render_vault(
        _pages(quote, claim_text=claim_text, source_title=source_title),
        dialect=dialect,
        project_title="Test Wiki",
    )
    extra = evidence_files(
        [_evidence(quote, claim_text=claim_text, source_title=source_title)],
        project_title="Test Wiki",
        dialect=export.dialect,
    )
    return {**export.files, **extra}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_quote_reaches_the_exported_file_unedited(dialect: str) -> None:
    """The bytes of the quote, after the exporter has run over the page.

    Without the fence, the okf dialect writes `\\[\\[Attention]]` here and the
    obsidian dialect writes a working link to a page the source never mentioned
    — in both cases the export has changed what the source said while calling
    the citation verbatim.
    """
    page = _bundle(dialect, QUOTE_PLAIN)["attention.md"]
    assert QUOTE_PLAIN in page


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_bracketed_quote_is_never_pasted_into_the_prose(dialect: str) -> None:
    """A quote the section refuses to inline must not reach the page by any
    other route, and the reader must be told where it went."""
    page = _bundle(dialect, QUOTE_WITH_BRACKETS)["attention.md"]
    assert QUOTE_WITH_BRACKETS not in page
    assert EVIDENCE_FILENAME in page
    assert QUOTE_WITH_BRACKETS in _bundle(dialect, QUOTE_WITH_BRACKETS)[EVIDENCE_FILENAME]


def test_the_page_body_still_links_after_the_evidence_section() -> None:
    """The fence the quote sits in has to close.

    An unclosed fence switches off link rewriting for everything after it. The
    ordinary wikilink in the prose above the section is the canary — it is
    rewritten, and the assertion is that the exporter still resolved it.
    """
    page = _bundle("okf", QUOTE_PLAIN)["attention.md"]
    assert "[Recurrent Networks](./recurrent-networks.md)" in page


def test_the_okf_bundle_has_no_wikilink_syntax_with_evidence_in_it() -> None:
    """The WS-H8 criterion, restated over a bundle that carries evidence."""
    files = _bundle("okf", QUOTE_PLAIN)
    for name, text in files.items():
        if name.endswith(".md"):
            assert "[[" not in text, name


def test_the_okf_validator_accepts_a_bundle_carrying_evidence(tmp_path: Path) -> None:
    """Run over the real bytes, as a subprocess. The evidence section is new
    markdown inside every concept file, and "it should still be fine" is not
    something a format claim gets to assert about itself."""
    directory = _write(tmp_path, _bundle("okf", QUOTE_PLAIN))
    result = subprocess.run(
        [sys.executable, str(CHECK_OKF), str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_validator_still_goes_red_on_a_bundle_with_evidence(tmp_path: Path) -> None:
    """The pair to the test above: a validator that cannot fail proves nothing
    about the one that passed. `type` is removed from one concept, which is the
    state 599 of 843 live pages were in."""
    files = _bundle("okf", QUOTE_PLAIN)
    # The frontmatter renders as a flow mapping (`{title: …, type: …}`), so the
    # naive line-oriented mutation removes nothing and the test passes against
    # an unmutated bundle — which is a mutation proving the opposite of what it
    # claims. Asserted below rather than assumed.
    mutated = files["attention.md"].replace("type: concept, ", "", 1)
    assert mutated != files["attention.md"], "the mutation changed nothing"
    files["attention.md"] = mutated
    directory = _write(tmp_path, files)
    result = subprocess.run(
        [sys.executable, str(CHECK_OKF), str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "type" in result.stdout


@pytest.mark.parametrize("dialect", DIALECTS)
def test_export_import_export_is_byte_identical_with_evidence(dialect: str) -> None:
    """The round-trip criterion, with the section present.

    A section that accumulated on re-export, or a fence the importer mangled,
    shows up here as a diff and nowhere else.
    """
    first = render_vault(_pages(QUOTE_PLAIN), dialect=dialect, project_title="Test Wiki")
    second = render_vault(
        parse_vault(dict(first.files)), dialect=dialect, project_title="Test Wiki"
    )
    assert dict(first.files) == dict(second.files)


def test_the_sidecar_names_only_pages_that_are_in_the_bundle() -> None:
    """A slug in `evidence.json` with no `slug.md` next to it is the same
    dangling reference the OKF link rule exists to catch, one level up."""
    import json

    files = _bundle("okf", QUOTE_PLAIN)
    document = json.loads(files[EVIDENCE_FILENAME])
    for page in document["pages"]:
        assert f"{page['slug']}.md" in files, page["slug"]


# ---------------------------------------------------------------------------
# The sidecar's validator
#
# `scripts/check-okf.py` reads only `.md` files for the OKF rules, so before
# these the evidence sidecar was bytes no command looked at — a file format
# with no validator, which is the thing that workstream's own docstring says a
# format claim is worth nothing without. Each rule is driven over a real
# bundle, mutated one way, and each mutation is asserted to have changed the
# file first: a mutation that changes nothing produces a green run that proves
# the opposite of what it claims.
# ---------------------------------------------------------------------------


def _okf(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_OKF), str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )


def _bundle_with_sidecar(mutate: object) -> dict[str, str]:
    """A real okf bundle whose `evidence.json` has been edited by `mutate`."""
    import json

    files = _bundle("okf", QUOTE_PLAIN)
    document = json.loads(files[EVIDENCE_FILENAME])
    assert callable(mutate)
    mutate(document)
    changed = json.dumps(document, indent=2) + "\n"
    assert changed != files[EVIDENCE_FILENAME], "the mutation changed nothing"
    files[EVIDENCE_FILENAME] = changed
    return files


def test_the_ok_line_says_whether_a_sidecar_was_checked(tmp_path: Path) -> None:
    """ "Conforms to OKF v0.1" must not be read as "and the evidence is fine"
    when the bundle carries no evidence at all."""
    with_evidence = _okf(_write(tmp_path / "a", _bundle("okf", QUOTE_PLAIN)))
    assert with_evidence.returncode == 0, with_evidence.stdout
    assert "evidence sidecar" in with_evidence.stdout

    files = _bundle("okf", QUOTE_PLAIN)
    del files[EVIDENCE_FILENAME]
    without = _okf(_write(tmp_path / "b", files))
    assert without.returncode == 0, without.stdout
    assert "no evidence sidecar" in without.stdout


def test_a_sidecar_naming_a_page_that_is_not_in_the_bundle_is_reported(
    tmp_path: Path,
) -> None:
    """The dangling reference one level up from the OKF link rule, and the
    exact shape a stub filter that stopped matching would produce."""
    files = _bundle_with_sidecar(lambda d: d["pages"][0].update(slug="ghost"))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-page" in result.stdout


def test_a_backwards_character_span_is_reported(tmp_path: Path) -> None:
    """`char_end < char_start` cannot be sliced out of any document, so a
    consumer following the chain gets an empty string and no error."""
    files = _bundle_with_sidecar(
        lambda d: d["pages"][0]["claims"][0]["citations"][0].update(char_start=99, char_end=1)
    )
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-span" in result.stdout


def test_half_a_span_is_reported(tmp_path: Path) -> None:
    files = _bundle_with_sidecar(
        lambda d: d["pages"][0]["claims"][0]["citations"][0].update(char_end=None)
    )
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-span" in result.stdout


def test_a_header_count_that_disagrees_with_the_body_is_reported(tmp_path: Path) -> None:
    """A stated count nobody re-derives is how "the export carries 8,056
    claims" becomes a number that is simply wrong."""
    files = _bundle_with_sidecar(lambda d: d.update(claim_count=999))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-count" in result.stdout


def test_an_unknown_sidecar_version_is_reported(tmp_path: Path) -> None:
    files = _bundle_with_sidecar(lambda d: d.update(aleph_evidence_version="99"))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-version" in result.stdout


def test_an_unparseable_sidecar_is_reported(tmp_path: Path) -> None:
    files = _bundle("okf", QUOTE_PLAIN)
    files[EVIDENCE_FILENAME] = "{not json"
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-parse" in result.stdout


# ---------------------------------------------------------------------------
# Free text that is not a quote, through the real exporter
#
# `_quote_block` guarded the quote and only the quote. Claim text and source
# titles reach the section straight out of `wiki_claims.text` and
# `sources.title`, and both are unbounded. Reproduced before the fix: a claim
# reading `The paper called it [[Recurrent Networks]] throughout.` left the okf
# bundle as `[Recurrent Networks](./recurrent-networks.md)` and the obsidian
# bundle as `[[recurrent-networks|Recurrent Networks]]`. The exporter had
# rewritten the claim's own words into a link the project never wrote.
#
# These run the REAL exporter over the section, which is where the rewrite
# happened — the section-level pins live in
# `packages/aleph-wiki/tests/test_export_evidence.py`.
# ---------------------------------------------------------------------------

CLAIM_WITH_WIKILINK = "The paper called it [[Recurrent Networks]] throughout."
TITLE_WITH_WIKILINK = "A survey of [[wikilink]] syntax"

#: One fence line, deliberately unbalanced. Two would toggle `render_vault`'s
#: line-by-line fence tracker back to where it started and the corruption would
#: hide; one leaves every link below it unrewritten for the rest of the page.
CLAIM_WITH_FENCE = "the example reads\n```python\nx = 1"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_claim_is_not_turned_into_a_link_by_the_exporter(dialect: str) -> None:
    """The words of the claim, after the exporter has run over the page.

    `recurrent-networks.md` IS in this bundle, so the link would resolve and
    look entirely correct — which is what makes it the bad case rather than a
    dangling-link report somebody would notice.
    """
    page = _bundle(dialect, QUOTE_PLAIN, claim_text=CLAIM_WITH_WIKILINK)["attention.md"]
    block = page.split("## Evidence", 1)[1]
    assert "[Recurrent Networks](./recurrent-networks.md)" not in block
    assert "[[recurrent-networks|Recurrent Networks]]" not in block
    assert "The paper called it \\[\\[Recurrent Networks\\]\\] throughout." in block


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_source_title_is_not_turned_into_a_link_by_the_exporter(dialect: str) -> None:
    """In okf the exporter could not resolve the invented target, so it wrote
    the display text alone: `A survey of wikilink syntax`, brackets deleted
    from somebody else's title. In obsidian it emitted a working wikilink to a
    page the title never named."""
    page = _bundle(dialect, QUOTE_PLAIN, source_title=TITLE_WITH_WIKILINK)["attention.md"]
    block = page.split("## Evidence", 1)[1]
    assert "A survey of wikilink syntax" not in block
    assert "A survey of \\[\\[wikilink\\]\\] syntax" in block


def test_a_fence_in_a_claim_does_not_switch_off_link_rewriting(tmp_path: Path) -> None:
    """The canary is the ordinary wikilink in the prose ABOVE the section.

    `render_vault` decides where not to rewrite links by counting fence lines,
    so one leaked from a claim leaves the tracker inside a fence that never
    opened — and every link from there to the end of the page silently stops
    being a link. Asserted on a link that must still be rewritten, and on the
    validator, which sees the residual `[[`.
    """
    pages = _pages(QUOTE_PLAIN, claim_text=CLAIM_WITH_FENCE)
    # A second wikilink AFTER the evidence section: the one above it is
    # rewritten before the tracker can desync, so it proves nothing.
    pages[0] = VaultPage(
        title=pages[0].title,
        slug=pages[0].slug,
        body_md=pages[0].body_md + "\nAnd afterwards, see [[Recurrent Networks]].\n",
        page_type=pages[0].page_type,
        category=pages[0].category,
    )
    export = render_vault(pages, dialect="okf", project_title="Test Wiki")
    page = export.files["attention.md"]
    assert page.count("[Recurrent Networks](./recurrent-networks.md)") == 2, page
    assert "[[" not in page
    files = {
        **export.files,
        **evidence_files(
            [_evidence(QUOTE_PLAIN, claim_text=CLAIM_WITH_FENCE)],
            project_title="Test Wiki",
            dialect="okf",
        ),
    }
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "claim_text",
    [CLAIM_WITH_WIKILINK, CLAIM_WITH_FENCE, "plain"],
    ids=["wikilink", "fence", "plain"],
)
def test_the_sidecar_still_carries_the_claim_byte_exact(claim_text: str) -> None:
    """Whatever the section does to make a string safe to render, the lossless
    copy is unchanged. Otherwise "safe" would mean "edited everywhere"."""
    files = _bundle("okf", QUOTE_PLAIN, claim_text=claim_text)
    _, pages = parse_evidence_json(files[EVIDENCE_FILENAME])
    assert pages[0].claims[0].text == claim_text


# ---------------------------------------------------------------------------
# The whole bundle round-trips, sidecar included
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_whole_bundle_including_the_sidecar_round_trips(dialect: str) -> None:
    """`parse_vault` reads `.md` only, so `evidence.json` sat outside the round
    trip that is supposed to prove no field is dropped in silence — the sidecar
    had a writer and, anywhere in Aleph, no reader at all.

    Both halves are read back and re-rendered here: the markdown through
    `parse_vault`, the sidecar through `parse_evidence_json`, and the header
    the second one returns is what supplies `project_title` and `dialect` to
    the re-render rather than the constants this test started from.
    """
    first = _bundle(dialect, QUOTE_PLAIN)
    header, pages = parse_evidence_json(first[EVIDENCE_FILENAME])
    assert (header.project_title, header.dialect) == ("Test Wiki", dialect)
    second = {
        **render_vault(
            parse_vault(first), dialect=header.dialect, project_title=header.project_title
        ).files,
        **evidence_files(list(pages), project_title=header.project_title, dialect=header.dialect),
    }
    assert second == first


# ---------------------------------------------------------------------------
# The rules that had no test
# ---------------------------------------------------------------------------


def test_every_evidence_rule_is_declared_and_driven_by_a_test() -> None:
    """The count in the report said five; the code shipped six; `evidence-claim`
    had no test anywhere.

    Two assertions, because the list and the tests can drift apart
    independently: `EVIDENCE_RULES` must name every `evidence-*` rule the
    script can emit, and every rule in it must appear in this file. The second
    is a naming check, not a proof that the test drives the rule — but it makes
    adding a rule with no test a build failure rather than a thing somebody
    counts by hand.
    """
    import re

    source = CHECK_OKF.read_text(encoding="utf-8")
    emitted = set(re.findall(r'"(evidence-[a-z-]+)"', source))
    declared = set(re.findall(r'^    "(evidence-[a-z-]+)",$', source, re.MULTILINE))
    assert declared, "EVIDENCE_RULES no longer parses — this check has stopped checking"
    assert emitted == declared, f"undeclared: {emitted - declared}; stale: {declared - emitted}"
    here = Path(__file__).read_text(encoding="utf-8")
    for rule in sorted(declared):
        assert rule in here, f"{rule} is emitted by check-okf.py and named by no test here"


def test_a_claim_with_no_text_is_reported(tmp_path: Path) -> None:
    """A claim is a sentence somebody can check. An empty one exports a
    citation chain anchoring nothing, and the page above it renders
    `**Claim.** ` followed by a blank."""
    files = _bundle_with_sidecar(lambda d: d["pages"][0]["claims"][0].update(text="   "))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-claim" in result.stdout


def test_a_page_entry_with_no_slug_is_reported(tmp_path: Path) -> None:
    """Distinct from a slug that names a missing file: an entry with no slug
    names nothing at all, so its claims cannot be attached to any page in the
    bundle and the `slug not in stems` branch never even runs."""
    files = _bundle_with_sidecar(lambda d: d["pages"][0].update(slug=""))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-page" in result.stdout
    assert "carries no slug" in result.stdout


@pytest.mark.parametrize("field", ["project_title", "dialect"])
def test_a_sidecar_that_dropped_a_header_field_is_reported(tmp_path: Path, field: str) -> None:
    """Both were write-only: written by `evidence_document`, re-supplied by
    both call sites, read by nobody. Deleting either left 163 tests green,
    because a round trip cannot see a field neither half carries."""
    files = _bundle_with_sidecar(lambda d: d.pop(field))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-header" in result.stdout
    assert field in result.stdout


@pytest.mark.parametrize("field", ["claim_count", "citation_count", "anchored_citation_count"])
def test_a_sidecar_that_dropped_a_count_is_reported(tmp_path: Path, field: str) -> None:
    """The mismatch rule ran under `isinstance(stated, int)`, so *changing* a
    count failed and *deleting* it passed."""
    files = _bundle_with_sidecar(lambda d: d.pop(field))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-count" in result.stdout
    assert field in result.stdout


def test_an_anchored_count_that_disagrees_with_the_body_is_reported(tmp_path: Path) -> None:
    """`anchored_citation_count` was the one count nothing re-derived — the
    rule only ever looped over claims and citations, so the number that
    separates "cites a paper" from "quotes it at an offset" was unchecked."""
    files = _bundle_with_sidecar(lambda d: d.update(anchored_citation_count=7))
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-count" in result.stdout
    assert "anchored_citation_count" in result.stdout


def test_a_page_whose_summary_sentence_disagrees_with_the_sidecar_is_reported(
    tmp_path: Path,
) -> None:
    """The numbers a PERSON reads, which no rule looked at.

    `render_evidence_section` could report `EvidenceCounts()` — "0 claims, 0
    citations, 0 anchored" over twenty anchored citations — and every test
    stayed green, because `evidence-count` compares the sidecar's header to the
    sidecar's body and never opens the markdown.
    """
    files = _bundle("okf", QUOTE_PLAIN)
    mutated = files["attention.md"].replace(
        "1 claim, 1 citation, 1 anchored", "0 claims, 0 citations, 0 anchored", 1
    )
    assert mutated != files["attention.md"], "the mutation changed nothing"
    files["attention.md"] = mutated
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-summary" in result.stdout


def test_a_page_whose_summary_sentence_is_gone_is_reported(tmp_path: Path) -> None:
    """The guard on the guard. The rule finds the sentence by its wording, so a
    reworded summary would switch it off in silence — which is the exact
    failure it exists to catch, one level up."""
    files = _bundle("okf", QUOTE_PLAIN)
    mutated = files["attention.md"].replace("1 claim, 1 citation, 1 anchored", "some evidence", 1)
    assert mutated != files["attention.md"], "the mutation changed nothing"
    files["attention.md"] = mutated
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-summary" in result.stdout


def test_a_page_with_no_evidence_block_at_all_is_reported(tmp_path: Path) -> None:
    """The sidecar says this page has a claim; the page shows the reader
    nothing. That is the export shipping the conclusion with the reasoning
    deleted from the half a person actually opens."""
    files = _bundle("okf", QUOTE_PLAIN)
    body = files["attention.md"]
    start = body.index("<!-- aleph:evidence -->")
    end = body.index("<!-- /aleph:evidence -->") + len("<!-- /aleph:evidence -->")
    files["attention.md"] = body[:start] + body[end:]
    assert "<!-- aleph:evidence -->" not in files["attention.md"], "the mutation changed nothing"
    result = _okf(_write(tmp_path, files))
    assert result.returncode == 1
    assert "evidence-summary" in result.stdout
