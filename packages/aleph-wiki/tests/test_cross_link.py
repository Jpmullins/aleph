"""inject_cross_links — pure prose→wikilink injection (curator cross_link step)."""

from __future__ import annotations

from aleph_wiki.curator_service import inject_cross_links, rewrite_wikilink_target


def test_links_first_plain_occurrence() -> None:
    body = "Transformers rely on Attention Mechanisms heavily. Attention Mechanisms again."
    new, linked = inject_cross_links(body, ["Attention Mechanisms"])
    assert linked == ["Attention Mechanisms"]
    # Only the FIRST occurrence is wrapped.
    assert new.count("[[Attention Mechanisms]]") == 1
    assert new.startswith("Transformers rely on [[Attention Mechanisms]] heavily.")


def test_idempotent_on_already_linked() -> None:
    body = "See [[Attention Mechanisms]] for details."
    new, linked = inject_cross_links(body, ["Attention Mechanisms"])
    assert linked == []
    assert new == body


def test_skips_code_spans() -> None:
    body = "Use `Attention Mechanisms` inline and\n```\nAttention Mechanisms\n```\nfence."
    new, linked = inject_cross_links(body, ["Attention Mechanisms"])
    assert linked == []
    assert new == body


def test_word_boundary_no_partial_match() -> None:
    body = "The Counterattack was decisive."  # must not match "Counter"
    new, linked = inject_cross_links(body, ["Counter"])
    assert linked == []
    assert new == body


def test_longer_surface_wins_overlap() -> None:
    body = "Knowledge Distillation Methods are useful."
    new, linked = inject_cross_links(
        body, ["Knowledge Distillation", "Knowledge Distillation Methods"]
    )
    # The longer, more specific surface is linked; the shorter one overlaps and is skipped.
    assert "[[Knowledge Distillation Methods]]" in new
    assert linked == ["Knowledge Distillation Methods"]


def test_multiple_distinct_targets() -> None:
    body = "Alpha relates to Beta and also to Gamma."
    new, linked = inject_cross_links(body, ["Alpha", "Beta", "Gamma"])
    assert set(linked) == {"Alpha", "Beta", "Gamma"}
    assert "[[Alpha]]" in new and "[[Beta]]" in new and "[[Gamma]]" in new


def test_respects_max_targets() -> None:
    body = "Alpha Beta Gamma Delta"
    new, linked = inject_cross_links(body, ["Alpha", "Beta", "Gamma", "Delta"], max_targets=2)
    assert len(linked) == 2
    assert new.count("[[") == 2


def test_rewrite_wikilink_target_plain() -> None:
    new, n = rewrite_wikilink_target("See [[Old Page]] here.", "Old Page", "New Page")
    assert n == 1
    assert new == "See [[New Page]] here."


def test_rewrite_wikilink_target_preserves_label() -> None:
    new, n = rewrite_wikilink_target("See [[Old Page|the label]].", "Old Page", "New Page")
    assert n == 1
    assert new == "See [[New Page|the label]]."


def test_rewrite_wikilink_target_no_partial_match() -> None:
    # "[[Old Pages]]" must NOT be rewritten when old_title is "Old Page".
    new, n = rewrite_wikilink_target("[[Old Pages]] and [[Old Page]]", "Old Page", "New Page")
    assert n == 1
    assert new == "[[Old Pages]] and [[New Page]]"
