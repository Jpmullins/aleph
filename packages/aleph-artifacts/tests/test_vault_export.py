"""Vault export (WS-H8): the bundle, the two dialects, and the OKF validator.

Every assertion here corresponds to a WS-H8 criterion, and every one can fail:
the dialect tests are checked in both directions (okf has no `[[`, obsidian
does), the type test uses a page with `page_type=None` — 40 of the 65 live
non-stub pages are in that state — and the OKF validator is driven as a
subprocess over a real export and then over a mutated one, so a validator that
cannot go red would be caught here rather than in a review.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

from aleph_artifacts.exporters.vault import (
    DEFAULT_PAGE_TYPE,
    INDEX_FILENAME,
    VaultPage,
    parse_vault,
    render_vault,
    vault_bytes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_OKF = REPO_ROOT / "scripts" / "check-okf.py"
CHECK_LINT_COUNT = REPO_ROOT / "scripts" / "check-lint-count.sh"

DIALECTS = ("obsidian", "okf")


def _corpus() -> list[VaultPage]:
    """A small corpus with every shape the live one has.

    Deliberately includes a page with no `page_type` and no `category`, a link
    written as a title, a link written as a slug, an anchored link, a link with
    display text, and a link to a title nobody wrote.
    """
    return [
        VaultPage(
            title="Attention Is All You Need",
            slug="attention-is-all-you-need",
            body_md=(
                "The paper that introduced the transformer.\n\n"
                "It builds on [[Sequence to Sequence Learning]] and is compared in "
                "[[recurrent-networks|recurrent models]].\n\n"
                "See also [[Sequence to Sequence Learning#Encoder]] and "
                "[[A Page Nobody Wrote]].\n"
            ),
            page_type="concept",
            category="architectures",
            tags=("transformers", "attention"),
            related=("recurrent-networks",),
            aliases=("Transformer paper",),
            confidence="high",
            created=date(2026, 1, 2),
            updated=date(2026, 3, 4),
        ),
        VaultPage(
            title="Sequence to Sequence Learning",
            slug="sequence-to-sequence-learning",
            body_md="Encoder-decoder models.\n\n## Encoder\n\nMaps a sequence to a vector.\n",
            page_type="concept",
            category="architectures",
            tags=("seq2seq",),
        ),
        # No page_type, no category, no tags — the majority state on the live
        # corpus, and the one that makes or breaks OKF conformance.
        VaultPage(
            title="Recurrent Networks",
            slug="recurrent-networks",
            body_md="Networks with a hidden state carried across steps.\n",
        ),
    ]


def _files(dialect: str) -> dict[str, str]:
    return dict(render_vault(_corpus(), dialect=dialect, project_title="Test Wiki").files)


# --- criterion: a vault can be exported ------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_one_markdown_file_per_page_plus_an_index(dialect: str) -> None:
    files = _files(dialect)
    assert INDEX_FILENAME in files
    concepts = sorted(n for n in files if n != INDEX_FILENAME)
    assert concepts == [
        "attention-is-all-you-need.md",
        "recurrent-networks.md",
        "sequence-to-sequence-learning.md",
    ]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_zip_contains_exactly_those_files(dialect: str) -> None:
    payload = vault_bytes(_corpus(), dialect=dialect, project_title="Test Wiki")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert sorted(zf.namelist()) == sorted(_files(dialect))


def test_the_zip_is_byte_identical_across_runs() -> None:
    """Pinned timestamps, sorted entries.

    Without this, two exports of the same corpus differ in every entry header
    and the round-trip criterion below can only ever pass by accident.
    """
    first = vault_bytes(_corpus(), dialect="okf", project_title="Test Wiki")
    second = vault_bytes(_corpus(), dialect="okf", project_title="Test Wiki")
    assert first == second


# --- criterion: OKF output contains no Obsidian-only syntax ----------------


def test_okf_output_has_no_wikilink_syntax() -> None:
    for name, text in _files("okf").items():
        assert "[[" not in text, f"{name} still carries Obsidian syntax"


def test_obsidian_output_does_use_wikilinks() -> None:
    """The other direction, so the check above is not passing on empty output."""
    body = _files("obsidian")["attention-is-all-you-need.md"]
    assert "[[sequence-to-sequence-learning|Sequence to Sequence Learning]]" in body
    assert "(./" not in body


def test_okf_escapes_a_bracket_pair_the_wikilink_pattern_cannot_match() -> None:
    """Two live hub pages carry a summary truncated mid-wikilink.

    `aleph_wiki.navigation` cuts a hub entry's summary at 140 characters and
    can cut it inside a `[[link]]`, leaving an unclosed `[[A…` in the stored
    body. `_WIKILINK` cannot match that, so without escaping it survives into
    an OKF bundle as syntax an OKF reader would try to interpret.
    """
    pages = [
        VaultPage(
            title="Hub",
            slug="hub",
            page_type="hub",
            body_md="- Entry — a summary that was cut inside [[A\n",
        )
    ]
    body = render_vault(pages, dialect="okf").files["hub.md"]
    assert "[[" not in body
    assert "\\[\\[A" in body


def test_okf_leaves_brackets_inside_a_code_fence_alone() -> None:
    """A fence shows raw text; a backslash added there is a visible edit."""
    pages = [
        VaultPage(
            title="Code",
            slug="code",
            page_type="concept",
            body_md="Example:\n\n```python\nm = [[1, 2], [3, 4]]\n```\n",
        )
    ]
    body = render_vault(pages, dialect="okf").files["code.md"]
    assert "m = [[1, 2], [3, 4]]" in body


def test_okf_links_point_at_files_that_exist() -> None:
    files = _files("okf")
    body = files["attention-is-all-you-need.md"]
    assert "[Sequence to Sequence Learning](./sequence-to-sequence-learning.md)" in body
    assert "[recurrent models](./recurrent-networks.md)" in body
    # The anchor survives the translation; a link to a section that loses its
    # section lands the reader at the top of the page and looks like it worked.
    assert "(./sequence-to-sequence-learning.md#Encoder)" in body


# --- criterion: every exported file validates against OKF v0.1 -------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_concept_declares_a_non_empty_type(dialect: str) -> None:
    files = _files(dialect)
    for name, text in files.items():
        if name == INDEX_FILENAME:
            continue
        assert f"type: {DEFAULT_PAGE_TYPE}" in text or "type: concept" in text, name
    # The untyped page is the one that matters: it has no `page_type` at all.
    assert f"type: {DEFAULT_PAGE_TYPE}" in files["recurrent-networks.md"]


def test_only_the_okf_bundle_claims_okf_conformance() -> None:
    """An obsidian bundle is not a conforming OKF bundle and must not say it is.

    This is the WS-H8 defect in miniature: a marker asserting a property the
    bytes do not have.
    """
    assert 'okf_version: "0.1"' in _files("okf")[INDEX_FILENAME]
    assert "okf_version" not in _files("obsidian")[INDEX_FILENAME]


# --- criterion: dangling links are reported, not emitted -------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_link_to_a_page_nobody_wrote_is_reported(dialect: str) -> None:
    export = render_vault(_corpus(), dialect=dialect, project_title="Test Wiki")
    assert [d.target for d in export.dangling] == ["A Page Nobody Wrote"]
    assert [d.from_slug for d in export.dangling] == ["attention-is-all-you-need"]


def test_okf_does_not_emit_a_link_into_a_file_that_is_not_there() -> None:
    """The words survive; the link does not.

    A markdown link into a missing file fails in the reader instead of in the
    exporter, and a markdown reader has no way to signal that it did.
    """
    body = _files("okf")["attention-is-all-you-need.md"]
    assert "A Page Nobody Wrote" in body
    assert "./a-page-nobody-wrote.md" not in body


def test_obsidian_keeps_the_red_link() -> None:
    """A wikilink to a note that does not exist is a red link, not a defect.

    It is how `docs/wiki-schema.md` models `stub`, and it is the writing queue:
    945 of the 1,025 links in the live corpus point at stub pages, which the
    export excludes. Dropping them would delete the queue and the graph view's
    entire frontier.
    """
    body = _files("obsidian")["attention-is-all-you-need.md"]
    assert "[[A Page Nobody Wrote]]" in body


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_resolvable_link_is_not_reported_as_dangling(dialect: str) -> None:
    export = render_vault(_corpus()[1:], dialect=dialect, project_title="Test Wiki")
    assert export.dangling == ()


# --- criterion: the format round-trips -------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_export_import_export_is_byte_identical(dialect: str) -> None:
    """The property that says the format is lossless.

    A field the format drops shows up here as a diff. Note what this does NOT
    claim: `parse_vault` reads a bundle into view models, it does not write a
    revision, so this is a format round trip and not a database import.
    """
    first = render_vault(_corpus(), dialect=dialect, project_title="Test Wiki")
    reimported = parse_vault(first.files)
    second = render_vault(reimported, dialect=dialect, project_title="Test Wiki")
    assert second.files == first.files
    assert second.to_bytes() == first.to_bytes()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_round_trip_preserves_the_governance_fields(dialect: str) -> None:
    """Byte-identity could in principle hold by dropping a field on both passes."""
    first = render_vault(_corpus(), dialect=dialect, project_title="Test Wiki")
    back = {p.slug: p for p in parse_vault(first.files)}
    page = back["attention-is-all-you-need"]
    assert page.title == "Attention Is All You Need"
    assert page.category == "architectures"
    assert page.tags == ("transformers", "attention")
    assert page.related == ("recurrent-networks",)
    assert page.aliases == ("Transformer paper",)
    assert page.confidence == "high"
    assert page.created == date(2026, 1, 2)
    assert page.updated == date(2026, 3, 4)


# --- safety: a slug is a filename, and filenames are dangerous -------------


def test_a_page_slugged_index_does_not_clobber_the_directory_listing() -> None:
    pages = [VaultPage(title="Index", slug="index", body_md="A page about indexes.\n")]
    files = render_vault(pages, dialect="okf").files
    assert "index-concept.md" in files
    assert 'okf_version: "0.1"' in files[INDEX_FILENAME]


def test_a_slug_cannot_escape_the_extraction_directory() -> None:
    pages = [VaultPage(title="Evil", slug="../../etc/passwd", body_md="x\n")]
    names = set(render_vault(pages, dialect="okf").files)
    assert names == {INDEX_FILENAME, "etc-passwd.md"}


def test_two_slugs_that_normalise_alike_both_survive() -> None:
    pages = [
        VaultPage(title="A B", slug="a b", body_md="one\n"),
        VaultPage(title="A/B", slug="a/b", body_md="two\n"),
    ]
    files = render_vault(pages, dialect="okf").files
    assert sorted(n for n in files if n != INDEX_FILENAME) == ["a-b-2.md", "a-b.md"]


def test_an_unknown_dialect_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown vault dialect"):
        render_vault(_corpus(), dialect="markdown")


# --- the sweeps, driven as subprocesses ------------------------------------


def _run_okf(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK_OKF), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_okf_validator_accepts_the_okf_export(tmp_path: Path) -> None:
    for name, text in _files("okf").items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    result = _run_okf(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_okf_validator_rejects_a_concept_with_no_type(tmp_path: Path) -> None:
    """The mutation the WS-H8 review names, as a test rather than as a ritual."""
    for name, text in _files("okf").items():
        stripped = text.replace(f"type: {DEFAULT_PAGE_TYPE}\n", "")
        (tmp_path / name).write_text(stripped, encoding="utf-8")
    result = _run_okf(tmp_path)
    assert result.returncode == 1
    assert "no non-empty `type`" in result.stdout


def test_okf_validator_rejects_an_obsidian_bundle(tmp_path: Path) -> None:
    """Wikilinks are not OKF relationships, and the validator has to say so."""
    for name, text in _files("obsidian").items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    result = _run_okf(tmp_path)
    assert result.returncode == 1
    assert "wikilink" in result.stdout


def test_okf_validator_rejects_a_link_into_nowhere(tmp_path: Path) -> None:
    files = _files("okf")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "sequence-to-sequence-learning.md").unlink()
    result = _run_okf(tmp_path)
    assert result.returncode == 1
    assert "dangling-link" in result.stdout


def test_okf_validator_rejects_a_bundle_with_no_version_marker(tmp_path: Path) -> None:
    files = _files("okf")
    files[INDEX_FILENAME] = files[INDEX_FILENAME].replace('okf_version: "0.1"', "")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    result = _run_okf(tmp_path)
    assert result.returncode == 1
    assert "okf_version" in result.stdout


def test_okf_validator_reads_a_zip_as_well_as_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "vault.zip"
    target.write_bytes(vault_bytes(_corpus(), dialect="okf", project_title="Test Wiki"))
    assert _run_okf(target).returncode == 0


def test_documented_lint_count_matches_the_code() -> None:
    """WS-H8 criterion 6, run rather than asserted from memory."""
    result = subprocess.run(
        ["bash", str(CHECK_LINT_COUNT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_source_file_claims_the_schema_runs_on_the_write_path() -> None:
    """WS-H8 criterion 5.

    `WikiSchema.validate_page` has exactly one non-test caller,
    `aleph_wiki.lint`, which reports and never repairs. Two places in the tree
    said it ran at commit time. The claim is gone; this keeps it gone.
    """
    hits: list[str] = []
    # CLAUDE.md is in the scan. It was not, and the claim survived there for a
    # week after being removed from the two places this test was written to
    # cover — in the file whose opening paragraph exists to say that asserting
    # an invariant the code does not hold is the single reason a broken
    # retrieval path survived seven work packages. A pin whose scope excludes
    # the most load-bearing document in the repository pins the wrong thing.
    roots = (
        REPO_ROOT / "docs",
        REPO_ROOT / "packages" / "aleph-wiki" / "src",
        REPO_ROOT / "CLAUDE.md",
    )
    for root in roots:
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.suffix not in {".md", ".py"}:
                continue
            # docs/plan.md is where the criterion itself is written down.
            if path.name == "plan.md":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # A bare substring match on the CLAIM. Both places that used to
            # make it now say the opposite in words that do not contain the
            # phrase ("is NOT on the write path", "is NOT wired into the write
            # path"), so no exclusion is needed — and an exclusion here was
            # actively wrong: a first attempt skipped any line containing
            # " NOT ", which the sentence "stored as data, not as a document,
            # so validate_page runs on the write path" satisfies. The mutation
            # that reintroduced the exact claim stayed green.
            for line in text.splitlines():
                if "runs on the write path" in line:
                    hits.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()[:90]}")
    assert hits == [], "\n".join(hits)


def _simple_page(*, slug: str, title: str, body_md: str) -> VaultPage:
    """The minimum a page needs, for tests about body rendering only."""
    return VaultPage(title=title, slug=slug, body_md=body_md, page_type="concept")


# ---------------------------------------------------------------------------
# A wikilink inside a fence is an example, not a link
#
# A page documenting wikilink syntax contains wikilinks it does not mean.
# Rewriting `[[Attention]]` inside a ```markdown fence turns somebody's example
# of the syntax into something that is no longer the thing being shown — and on
# the OKF side it also invents a dangling-link report for a link that was never
# a link. `_escape_residual_brackets` already skipped fences; the rewrite did
# not, so the two halves of one rule disagreed.
# ---------------------------------------------------------------------------


def _fenced_page() -> VaultPage:
    return _simple_page(
        slug="wikilink-syntax",
        title="Wikilink Syntax",
        body_md=(
            "# Wikilink Syntax\n\n"
            "A real link: [[Attention]]\n\n"
            "```markdown\n"
            "Use [[Attention]] to link a page.\n"
            "```\n\n"
            "After the fence: [[Attention]]\n"
        ),
    )


def test_a_wikilink_in_a_code_fence_is_left_alone() -> None:
    target = _simple_page(slug="attention", title="Attention", body_md="# Attention\n")
    files = render_vault([_fenced_page(), target], dialect="okf").files
    body = files["wikilink-syntax.md"]

    fenced = body.split("```markdown")[1].split("```")[0]
    assert "[[Attention]]" in fenced, (
        "the example inside the fence was rewritten into a link — it is no "
        f"longer the syntax it was demonstrating: {fenced!r}"
    )


def test_wikilinks_outside_the_fence_are_still_rewritten() -> None:
    """The guard must not become "stop rewriting" — both links outside the fence
    still have to become markdown links, or the fix has broken the feature."""
    target = _simple_page(slug="attention", title="Attention", body_md="# Attention\n")
    body = render_vault([_fenced_page(), target], dialect="okf").files["wikilink-syntax.md"]

    outside = body.split("```markdown")[0] + body.split("```")[-1]
    assert "[[Attention]]" not in outside
    assert outside.count("](./attention.md)") == 2


def test_a_fenced_wikilink_does_not_produce_a_dangling_report() -> None:
    """A link that was never a link cannot be dangling."""
    result = render_vault(
        [
            _simple_page(
                slug="only-fenced",
                title="Only Fenced",
                body_md="# Only Fenced\n\n```\n[[No Such Page]]\n```\n",
            )
        ],
        dialect="okf",
    )
    assert not result.dangling, f"a fenced example was reported as a broken link: {result.dangling}"


def test_the_obsidian_dialect_leaves_fences_alone_too() -> None:
    """Obsidian keeps wikilinks, but it still NORMALISES them to the target's
    stem — `[[Attention]]` becomes `[[attention|Attention]]`. So the fence guard
    matters here too: the example inside the fence must stay exactly as
    written."""
    target = _simple_page(slug="attention", title="Attention", body_md="# Attention\n")
    body = render_vault([_fenced_page(), target], dialect="obsidian").files["wikilink-syntax.md"]

    fenced = body.split("```markdown")[1].split("```")[0]
    assert "[[Attention]]" in fenced
    assert "|" not in fenced, f"the fenced example was normalised: {fenced!r}"
    # And outside it, the normalisation still happens.
    assert body.count("[[attention|Attention]]") == 2
