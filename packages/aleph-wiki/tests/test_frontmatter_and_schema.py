"""Frontmatter round-trip and schema validation.

The load-bearing property is round-tripping: a page written by Aleph must open
in Obsidian, and a page authored in Obsidian must import without loss. Anything
less makes the vault one-directional, which defeats the point of storing the
governance fields in a format Obsidian already reads.
"""

from __future__ import annotations

from datetime import date

import pytest

from aleph_wiki.frontmatter import Frontmatter, extract_wikilinks, parse, render, strip
from aleph_wiki.schema import Category, WikiSchema, default_schema

# A real page header from ~/wiki/ai-research, which is what the schema is
# modelled on. If this stops parsing, the vault stops importing.
REAL_PAGE = """---
title: Mixture of Experts
created: 2026-08-06
updated: 2026-08-06
type: concept
category: architectures
tags: [architecture, scaling, efficiency, method]
related: [mixture-of-experts-routing, dense-vs-moe, distributed-training]
sources: [arxiv:2101.03961, arxiv:2401.04088]
confidence: high
---

# Mixture of Experts

**Sparse mixture of experts (MoE)** replaces the dense feed-forward sublayer
with $N$ parallel experts. See [[dense-vs-moe]] and [[sparse-architectures]].
"""


class TestParse:
    def test_parses_a_real_vault_page(self) -> None:
        fm, body = parse(REAL_PAGE)
        assert fm.title == "Mixture of Experts"
        assert fm.type == "concept"
        assert fm.category == "architectures"
        assert fm.tags == ["architecture", "scaling", "efficiency", "method"]
        assert fm.created == date(2026, 8, 6)
        assert fm.confidence == "high"
        assert fm.contested is False
        assert body.lstrip().startswith("# Mixture of Experts")

    def test_a_body_with_no_frontmatter_is_returned_untouched(self) -> None:
        """The corpus predates the schema; 251 pages have no block yet."""
        body = "# Just a page\n\nNo frontmatter here.\n"
        fm, rest = parse(body)
        assert fm == Frontmatter()
        assert rest == body

    def test_a_horizontal_rule_is_not_a_fence(self) -> None:
        """`---` mid-document is a rule. Matching it would eat the body."""
        body = "# Title\n\nSome prose.\n\n---\n\nMore prose after a rule.\n"
        fm, rest = parse(body)
        assert fm == Frontmatter()
        assert rest == body
        assert "More prose after a rule." in rest

    def test_broken_yaml_degrades_to_no_frontmatter(self) -> None:
        """A parse error must not make the page unreadable."""
        body = "---\ntitle: [unclosed\n---\n\n# Body\n"
        fm, rest = parse(body)
        assert fm == Frontmatter()
        assert rest == body

    def test_a_scalar_where_a_list_belongs_is_accepted(self) -> None:
        """Hand-written vaults say `tags: architecture`. Rejecting loses them."""
        fm, _ = parse("---\ntitle: T\ntags: architecture, scaling\n---\n\nBody\n")
        assert fm.tags == ["architecture", "scaling"]


class TestRender:
    def test_round_trips_without_loss(self) -> None:
        original, body = parse(REAL_PAGE)
        reparsed, _ = parse(render(original, body))
        assert reparsed == original

    def test_empty_fields_are_omitted(self) -> None:
        """`contested: false` on every page trains readers to skip the block."""
        out = render(Frontmatter(title="T", tags=["method"]), "# T\n")
        assert "contested" not in out
        assert "confidence" not in out
        assert "contradictions" not in out
        assert "title: T" in out

    def test_contested_true_survives(self) -> None:
        """False is the default and dropped; true is a claim and must persist."""
        fm = Frontmatter(title="T", contested=True, contradictions=["other-page"])
        reparsed, _ = parse(render(fm, "# T\n"))
        assert reparsed.contested is True
        assert reparsed.contradictions == ["other-page"]

    def test_field_order_is_stable(self) -> None:
        """Alphabetical ordering would scramble every diff."""
        out = render(
            Frontmatter(title="T", type="concept", category="architectures", tags=["method"]),
            "# T\n",
        )
        keys = [ln.split(":")[0] for ln in out.splitlines() if ln and not ln.startswith("-")]
        assert keys.index("title") < keys.index("type") < keys.index("category")

    def test_rendering_an_unstripped_body_would_double_the_block(self) -> None:
        """Pins the documented contract: pass `parse()[1]`, not the original."""
        fm, body = parse(REAL_PAGE)
        correct = render(fm, body)
        assert correct.count("---\ntitle:") == 1
        # And the misuse the docstring warns about does produce two blocks —
        # which is why the contract is stated rather than defended at runtime.
        assert render(fm, REAL_PAGE).count("title: Mixture of Experts") == 2


class TestWikilinks:
    def test_finds_links_and_dedupes_in_order(self) -> None:
        found = extract_wikilinks("See [[b]] then [[a]] then [[b]] again.")
        assert found == ["b", "a"]

    def test_display_text_and_anchors_resolve_to_the_page(self) -> None:
        assert extract_wikilinks("[[slug|Nice Name]] and [[slug#section]]") == ["slug"]

    def test_frontmatter_related_does_not_count_as_a_link(self) -> None:
        """Otherwise a page satisfies min-3 while its body links to nothing."""
        body = "---\ntitle: T\nrelated: [a, b, c]\n---\n\n# T\n\nNo links in the prose.\n"
        assert extract_wikilinks(body) == []

    def test_strip_removes_the_block(self) -> None:
        assert not strip(REAL_PAGE).lstrip().startswith("---")


class TestSchemaValidation:
    @pytest.fixture
    def schema(self) -> WikiSchema:
        return default_schema()

    def _valid(self) -> dict[str, object]:
        return {
            "title": "Mixture of Experts",
            "page_type": "concept",
            "category": "architectures",
            "tags": ["architecture", "scaling"],
            "related": ["dense-vs-moe"],
            "confidence": "high",
            "outbound_links": 4,
            "body_lines": 120,
        }

    def test_a_conforming_page_has_no_violations(self, schema: WikiSchema) -> None:
        assert schema.validate_page(**self._valid()) == []  # type: ignore[arg-type]

    def test_a_tag_outside_the_taxonomy_is_rejected(self, schema: WikiSchema) -> None:
        """The whole point of a taxonomy. `transformers` vs `transformer` vs
        `xformer` are three unrelated facets of one corpus otherwise."""
        args = self._valid() | {"tags": ["architecture", "transformerz"]}
        violations = schema.validate_page(**args)  # type: ignore[arg-type]
        assert [v.field for v in violations] == ["tags"]
        assert "transformerz" in violations[0].message
        assert "add it to the schema" in violations[0].fix

    def test_an_unknown_category_is_rejected(self, schema: WikiSchema) -> None:
        args = self._valid() | {"category": "quantum-basketry"}
        assert any(v.field == "category" for v in schema.validate_page(**args))  # type: ignore[arg-type]

    def test_too_few_outbound_links_is_a_violation(self, schema: WikiSchema) -> None:
        """An unlinked page is unreachable by walking the wiki."""
        args = self._valid() | {"outbound_links": 1}
        v = [x for x in schema.validate_page(**args) if x.field == "wikilinks"]  # type: ignore[arg-type]
        assert len(v) == 1
        assert "minimum is 3" in v[0].message

    def test_contested_without_contradictions_is_rejected(self, schema: WikiSchema) -> None:
        """A page flagged disputed that never says what disputes it cannot be
        checked by a reader, which makes the flag worse than useless."""
        args = self._valid() | {"contested": True, "contradictions": []}
        assert any(v.field == "contradictions" for v in schema.validate_page(**args))  # type: ignore[arg-type]

    def test_an_oversized_page_is_flagged_for_splitting(self, schema: WikiSchema) -> None:
        args = self._valid() | {"body_lines": 400}
        assert any(v.field == "length" for v in schema.validate_page(**args))  # type: ignore[arg-type]

    def test_a_stub_is_exempt(self, schema: WikiSchema) -> None:
        """A stub has no body to hold links and nobody said it was finished."""
        assert (
            schema.validate_page(
                title="AstraVer",
                page_type=None,
                category=None,
                tags=None,
                related=None,
                confidence=None,
                is_stub=True,
                outbound_links=0,
            )
            == []
        )

    def test_every_violation_is_reported_not_just_the_first(self, schema: WikiSchema) -> None:
        """An agent fixing one problem per round-trip gives up."""
        violations = schema.validate_page(
            title="",
            page_type="nonsense",
            category="nowhere",
            tags=["bogus"],
            related=[],
            confidence="certain",
            outbound_links=0,
        )
        assert {v.field for v in violations} >= {
            "title",
            "type",
            "category",
            "tags",
            "confidence",
            "wikilinks",
        }


class TestSchemaSerialisation:
    def test_round_trips_through_json(self) -> None:
        original = default_schema()
        assert WikiSchema.from_dict(original.to_dict()) == original

    def test_missing_keys_fall_back_rather_than_failing(self) -> None:
        """A schema written by an older version must still open the wiki."""
        loaded = WikiSchema.from_dict({"domain": "d", "categories": [{"id": "x"}], "tags": []})
        assert loaded.min_outbound_links == 3
        assert loaded.categories[0].title == "X"

    def test_hub_slug_is_derived_not_stored(self) -> None:
        assert Category("architectures", "Architectures").hub_slug == "architectures-hub"
