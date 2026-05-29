"""WikiService logic — pure functions (idempotency, hand-edit splicing).

The full integration test for commit_revision lands in tests/e2e once a
live DB is available; this file covers the deterministic helpers.
"""

from __future__ import annotations

from aleph_wiki.wiki_service import (
    _hash,
    _slugify,
    _splice_protected_sections,
    _split_sections,
)


def test_split_sections_no_headings() -> None:
    sections = _split_sections("Just a paragraph with no heading.")
    assert len(sections) == 1
    assert sections[0].anchor == "content"


def test_split_sections_anchors() -> None:
    body = "# A\n\nbody a\n\n## B\n\nbody b\n\n### C\n\nbody c\n"
    sections = _split_sections(body)
    anchors = [s.anchor for s in sections]
    assert anchors == ["a", "b", "c"]


def test_slugify_basic() -> None:
    assert _slugify("Program Counter") == "program-counter"
    assert _slugify("Hello, World!") == "hello-world"
    assert _slugify("") == "page"
    assert _slugify("###") == "page"


def test_splice_preserves_protected_section() -> None:
    prior = "# A\n\nORIGINAL A\n\n## B\n\nORIGINAL B\n"
    new = "# A\n\nNEW A\n\n## B\n\nNEW B\n"
    # Locate the prior section char ranges.
    prior_sections = {s.anchor: (s.char_start, s.char_end) for s in _split_sections(prior)}
    merged = _splice_protected_sections(
        new,
        prior_body=prior,
        prior_sections=prior_sections,
        protected_anchors={"b"},
    )
    assert "NEW A" in merged
    assert "ORIGINAL B" in merged
    assert "NEW B" not in merged


def test_splice_no_protections_returns_new() -> None:
    prior = "# A\n\nORIGINAL\n"
    new = "# A\n\nNEW\n"
    merged = _splice_protected_sections(
        new, prior_body=prior, prior_sections={}, protected_anchors=set()
    )
    assert merged == new


def test_hash_changes_with_content() -> None:
    assert _hash("a") != _hash("b")
    assert _hash("xy") == _hash("xy")
