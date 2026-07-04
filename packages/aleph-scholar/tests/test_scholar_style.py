"""style_pass: renumbering, references rebuild, orphans, idempotence."""

from __future__ import annotations

import pytest

from aleph_scholar.style import style_pass

DOC = """# Findings

Claim A [3]. Claim B [1]. Claim A again [3], and C [7].

## References

[1] Alpha, A. (2001). First cited second. Journal One.
[2] Orphan, O. (2002). Never cited. Journal Two.
[3] Gamma, G. (2003). Cited first. Journal Three.
[7] Eta, E. (2007). Cited last. Journal Seven.
"""


def test_renumbers_markers_to_first_appearance_order() -> None:
    out = style_pass(DOC)
    assert "Claim A [1]. Claim B [2]. Claim A again [1], and C [3]." in out


def test_rebuilds_references_to_match_new_numbering() -> None:
    out = style_pass(DOC)
    lines = out.split("\n")
    ref_lines = [ln for ln in lines if ln.startswith("[")]
    assert ref_lines == [
        "[1] Gamma, G. (2003). Cited first. Journal Three.",
        "[2] Alpha, A. (2001). First cited second. Journal One.",
        "[3] Eta, E. (2007). Cited last. Journal Seven.",
    ]


def test_orphaned_entries_move_to_further_reading_verbatim() -> None:
    out = style_pass(DOC)
    assert "### Further reading" in out
    assert "Orphan, O. (2002). Never cited. Journal Two." in out
    # ...and the orphan carries no citation number anymore.
    assert "[2] Orphan" not in out
    # The stanza comes after the renumbered entries.
    assert out.index("### Further reading") > out.index("[3] Eta")


@pytest.mark.parametrize(
    "doc",
    [
        DOC,
        "plain text, no citations at all\n",
        "markers [2] and [1] but no references section",
        "## References\n\n[1] Lonely entry never cited\n",
        "",
    ],
)
def test_idempotent(doc: str) -> None:
    once = style_pass(doc)
    assert style_pass(once) == once


def test_no_markers_no_references_is_identity_modulo_blank_lines() -> None:
    doc = "# Title\n\nJust prose.\n"
    assert style_pass(doc) == doc


def test_collapses_runs_of_blank_lines() -> None:
    doc = "line one\n\n\n\n\nline two\n"
    out = style_pass(doc)
    assert out == "line one\n\n\nline two\n"


def test_markdown_links_are_not_markers() -> None:
    doc = "See [1](https://example.org) and [details](https://example.org) plus [2]."
    out = style_pass(doc)
    # [1](...) is a link, untouched; [2] is the first (only) marker -> [1].
    assert "[1](https://example.org)" in out
    assert "[details](https://example.org)" in out
    assert out.endswith("plus [1].")


def test_no_references_section_still_renumbers() -> None:
    doc = "First [9], second [4], first again [9]."
    assert style_pass(doc) == "First [1], second [2], first again [1]."


def test_references_with_dot_number_format() -> None:
    doc = "Cited [2].\n\n## References\n\n1. Unused entry\n2. Used entry\n"
    out = style_pass(doc)
    assert "[1] Used entry" in out
    assert "### Further reading" in out
    assert "Unused entry" in out


def test_multiline_reference_entries_are_joined() -> None:
    doc = (
        "Cited [1].\n\n## References\n\n"
        "[1] Long, L. (2020). A very long title\n    spanning two lines. Journal.\n"
    )
    out = style_pass(doc)
    assert "[1] Long, L. (2020). A very long title spanning two lines. Journal." in out


def test_content_after_references_section_is_preserved() -> None:
    doc = DOC + "\n# Appendix\n\nExtra prose.\n"
    out = style_pass(doc)
    assert "# Appendix" in out
    assert "Extra prose." in out
    assert out.index("# Appendix") > out.index("### Further reading")
    assert style_pass(out) == out
