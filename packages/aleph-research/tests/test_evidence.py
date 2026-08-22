"""The evidence pack: budget policy, the quote protocol, grounding, claims.

Everything here is pure — no Postgres, no gateway. The half that needs both
(retrieval, the index barrier, the whole compose node against a real corpus) is
`tests/e2e/test_research_reads_sources.py`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from aleph_research.evidence import (
    MIN_CARD_CHARS,
    ChunkRef,
    FabricatedQuote,
    anchor_body,
    extract_claims,
    render_pack,
    renumber_markers,
    section_anchor_for,
    select_cards,
    split_evidence_block,
)


def _ref(
    *,
    source: UUID | None = None,
    text: str = "A passage about tardigrades and cryobiosis.",
    short_id: str = "S0001",
    char_start: int = 0,
    section_path: str | None = None,
) -> ChunkRef:
    return ChunkRef(
        chunk_id=uuid4(),
        source_id=source or uuid4(),
        source_short_id=short_id,
        title="A paper",
        url=None,
        section_path=section_path,
        text=text,
        char_start=char_start,
        char_end=char_start + len(text),
    )


class TestSelectCards:
    def test_round_robin_gives_every_question_evidence_first(self) -> None:
        """Concatenating the per-question lists would spend the whole budget on
        question one whenever question one happens to match a lot."""
        q1 = [_ref(text=f"one-{i}") for i in range(5)]
        q2 = [_ref(text=f"two-{i}") for i in range(5)]
        cards = select_cards(breadth=[q1, q2], max_cards=4, per_source_cap=99)
        assert [c.ref.text for c in cards] == ["one-0", "two-0", "one-1", "two-1"]

    def test_markers_number_from_one_in_output_order(self) -> None:
        cards = select_cards(breadth=[[_ref(text="a"), _ref(text="b")]], per_source_cap=99)
        assert [c.marker for c in cards] == ["c1", "c2"]

    def test_one_source_cannot_fill_the_pack(self) -> None:
        """Nine matching chunks in one long document would otherwise fill the
        window and the report is written from one document's view of the topic
        while looking corpus-wide."""
        hog = uuid4()
        refs = [_ref(source=hog, text=f"hog-{i}") for i in range(9)]
        other = _ref(text="other")
        cards = select_cards(breadth=[[*refs, other]], per_source_cap=3)
        assert sum(1 for c in cards if c.ref.source_id == hog) == 3
        assert any(c.ref.text == "other" for c in cards)

    def test_character_budget_is_enforced(self) -> None:
        big = "x" * 2_000
        refs = [_ref(text=big, source=uuid4()) for _ in range(20)]
        cards = select_cards(breadth=[refs], budget_chars=3_000, max_cards=99, max_card_chars=1_000)
        assert sum(len(c.display_text) for c in cards) <= 3_000
        assert cards, "a budget that admits nothing is not a budget"

    def test_the_last_card_is_truncated_to_the_remaining_budget(self) -> None:
        """Not stopped short and not overshot: a card takes what is left."""
        first = _ref(text="F" * 400, source=uuid4())
        second = _ref(text="S" * 400, source=uuid4())
        cards = select_cards(
            breadth=[[first, second]],
            budget_chars=650,
            max_card_chars=400,
            max_cards=99,
        )
        assert len(cards) == 2
        assert not cards[0].truncated
        assert cards[1].truncated
        assert sum(len(c.display_text) for c in cards) <= 650

    def test_a_card_too_small_to_quote_is_never_offered(self) -> None:
        """A marker the composer cannot quote from is worse than one fewer."""
        refs = [_ref(text="x" * 400, source=uuid4()) for _ in range(5)]
        cards = select_cards(
            breadth=[refs],
            budget_chars=900,
            max_card_chars=400,
            max_cards=99,
        )
        assert all(len(c.display_text) >= MIN_CARD_CHARS for c in cards)

    def test_truncation_marks_itself_and_never_invents_text(self) -> None:
        text = "word " * 1_000
        cards = select_cards(breadth=[[_ref(text=text)]], max_card_chars=200)
        card = cards[0]
        assert card.truncated
        assert card.display_text.endswith("[…]")
        # The prefix is real source text: grounding still runs against
        # `ref.text`, so truncation can never turn a real quote into a fake one.
        assert text.startswith(card.display_text.removesuffix(" […]"))

    def test_the_same_chunk_is_never_carded_twice(self) -> None:
        shared = _ref(text="shared")
        cards = select_cards(breadth=[[shared], [shared]], per_source_cap=99)
        assert len(cards) == 1

    def test_empty_input_is_an_empty_pack_not_an_error(self) -> None:
        assert select_cards(breadth=[]) == []

    def test_budget_smaller_than_a_card_yields_nothing(self) -> None:
        cards = select_cards(breadth=[[_ref(text="x" * 500)]], budget_chars=MIN_CARD_CHARS - 1)
        assert cards == []


class TestRenderPack:
    def test_stanza_carries_marker_short_id_and_text(self) -> None:
        cards = select_cards(breadth=[[_ref(text="the passage", section_path="Results")]])
        rendered = render_pack(cards)
        assert "[c1]" in rendered
        assert "S0001" in rendered
        assert "Results" in rendered
        assert "the passage" in rendered


class TestSplitEvidenceBlock:
    def test_html_comment_block(self) -> None:
        body, quotes = split_evidence_block(
            'Body [c1].\n\n<!--aleph:evidence\n{"quotes": {"c1": "hello"}}\n-->'
        )
        assert quotes == {"c1": "hello"}
        assert "aleph:evidence" not in body
        assert body == "Body [c1]."

    def test_fenced_json_fallback(self) -> None:
        body, quotes = split_evidence_block('Body [c1].\n\n```json\n{"quotes": {"c1": "hi"}}\n```')
        assert quotes == {"c1": "hi"}
        assert "quotes" not in body

    def test_last_block_wins(self) -> None:
        _, quotes = split_evidence_block(
            '<!--aleph:evidence\n{"quotes": {"c1": "first"}}\n-->\n'
            '<!--aleph:evidence\n{"quotes": {"c1": "second"}}\n-->'
        )
        assert quotes == {"c1": "second"}

    def test_bracketed_keys_are_normalised(self) -> None:
        _, quotes = split_evidence_block('x\n<!--aleph:evidence\n{"quotes": {"[c1]": "q"}}\n-->')
        assert quotes == {"c1": "q"}

    def test_no_block_is_not_an_error(self) -> None:
        body, quotes = split_evidence_block("Just a body [c1].")
        assert quotes == {}
        assert body == "Just a body [c1]."

    def test_unparseable_block_is_stripped_and_empty(self) -> None:
        body, quotes = split_evidence_block("Body.\n\n<!--aleph:evidence\n{not json}\n-->")
        assert quotes == {}
        assert "aleph:evidence" not in body


_PASSAGE = (
    "Tardigrades survive desiccation by entering cryobiosis. "
    "The trehalose hypothesis does not explain every species."
)


def _one_card_pack() -> list:
    return select_cards(breadth=[[_ref(text=_PASSAGE, char_start=500)]])


class TestAnchorBody:
    def test_a_grounded_quote_yields_a_document_span(self) -> None:
        cards = _one_card_pack()
        out = anchor_body(
            body_md="Tardigrades dry out and survive [c1].",
            cards=cards,
            quotes={"c1": "The trehalose hypothesis does not explain every species."},
        )
        ref = out.refs_by_marker["c1"]
        assert ref.chunk_id == cards[0].ref.chunk_id
        # 500 is the chunk's own offset into the document; the quote starts at
        # the chunk's second sentence.
        assert ref.char_start == 500 + _PASSAGE.index("The trehalose")
        assert ref.char_end == 500 + len(_PASSAGE)
        assert ref.quote == "The trehalose hypothesis does not explain every species."

    def test_the_quote_stored_is_the_sources_spelling(self) -> None:
        """A smart-quoted paraphrase of the punctuation still grounds, and what
        is stored is what the document says, not what the model typed."""
        cards = select_cards(breadth=[[_ref(text='He said "cryobiosis" often.')]])
        out = anchor_body(
            body_md="A claim [c1].",
            cards=cards,
            quotes={"c1": "He said “cryobiosis” often."},
        )
        assert out.refs_by_marker["c1"].quote == 'He said "cryobiosis" often.'

    def test_a_fabricated_quote_raises(self) -> None:
        cards = _one_card_pack()
        with pytest.raises(FabricatedQuote) as exc:
            anchor_body(
                body_md="A claim [c1].",
                cards=cards,
                quotes={"c1": "Tardigrades were first observed on Europa."},
            )
        assert exc.value.marker == "c1"
        assert "S0001" in str(exc.value)

    def test_a_paraphrase_is_a_fabrication(self) -> None:
        """The whole point of grounding is that its answer is not a judgement
        call — a close paraphrase is still not what the source says."""
        cards = _one_card_pack()
        with pytest.raises(FabricatedQuote):
            anchor_body(
                body_md="A claim [c1].",
                cards=cards,
                quotes={"c1": "Tardigrades survive drying out by entering cryobiosis."},
            )

    def test_a_cited_but_unquoted_marker_is_dropped_and_named(self) -> None:
        cards = _one_card_pack()
        out = anchor_body(body_md="A claim [c1].", cards=cards, quotes={})
        assert out.refs_by_marker == {}
        assert out.unquoted_markers == ["c1"]
        assert "[c1]" not in out.body_md

    def test_an_unknown_marker_is_dropped(self) -> None:
        cards = _one_card_pack()
        out = anchor_body(body_md="A claim [c9].", cards=cards, quotes={"c9": "anything"})
        assert out.refs_by_marker == {}
        assert "[c9]" not in out.body_md

    def test_an_empty_quote_grounds_nothing(self) -> None:
        cards = _one_card_pack()
        out = anchor_body(body_md="A claim [c1].", cards=cards, quotes={"c1": "   "})
        assert out.unquoted_markers == ["c1"]


class TestRenumberMarkers:
    def test_first_appearance_order(self) -> None:
        refs = {
            "c3": _ref().source_short_id,  # placeholder, replaced below
        }
        del refs
        cards = select_cards(breadth=[[_ref(text=_PASSAGE), _ref(text=_PASSAGE, short_id="S0002")]])
        anchored = anchor_body(
            body_md="B [c2] then A [c1] then B again [c2].",
            cards=cards,
            quotes={
                "c1": "Tardigrades survive desiccation by entering cryobiosis.",
                "c2": "The trehalose hypothesis does not explain every species.",
            },
        )
        body, refs2 = renumber_markers(anchored.body_md, anchored.refs_by_marker)
        assert body == "B [c1] then A [c2] then B again [c1]."
        # The map moved with the body: what was c2 is now c1.
        assert refs2["c1"].source_short_id == "S0002"
        assert refs2["c2"].source_short_id == "S0001"

    def test_style_pass_renumbering_is_then_a_no_op(self) -> None:
        """`style_pass` renumbers `[cN]` to first-appearance order and knows
        nothing about the marker→chunk map. Doing it here first is what stops it
        from re-pointing every citation at the wrong chunk."""
        from aleph_scholar.style import style_pass

        body, _ = renumber_markers("B [c2] A [c1] B [c2].", {})
        assert style_pass(body) == body


class TestSectionAnchor:
    def test_agrees_with_the_wiki_slugifier(self) -> None:
        """`wiki_claims.section_anchor` is joined against the anchors the wiki
        derives from the same body at commit time. A divergence here makes every
        research claim point at a section that does not exist."""
        from aleph_wiki.wiki_service import _slugify

        for heading in (
            "Findings",
            "Results & Discussion",
            "  Spaced  Heading  ",
            "CO2 sequestration (2024)",
            "---",
            "Ünicode Headings",
        ):
            assert section_anchor_for(heading) == _slugify(heading), heading


class TestExtractClaims:
    def test_a_cited_sentence_becomes_a_claim_without_its_markers(self) -> None:
        body = "## Findings\n\nTardigrades survive freezing [c1]. Nothing else is known."
        claims = extract_claims(body, known_markers={"c1"})
        assert len(claims) == 1
        assert claims[0].text == "Tardigrades survive freezing."
        assert claims[0].citation_markers == ["c1"]
        assert claims[0].section_anchor == "findings"

    def test_uncited_sentences_are_not_claims(self) -> None:
        assert extract_claims("Plain prose with no marker.", known_markers=set()) == []

    def test_a_marker_whose_card_was_dropped_is_not_a_claim(self) -> None:
        body = "A statement [c4]."
        assert extract_claims(body, known_markers={"c1"}) == []

    def test_multiple_markers_on_one_sentence(self) -> None:
        body = "Two sources agree [c1][c2]."
        claims = extract_claims(body, known_markers={"c1", "c2"})
        assert claims[0].citation_markers == ["c1", "c2"]

    def test_bullets_are_separate_claims(self) -> None:
        body = "## S\n\n- First finding here [c1]\n- Second finding here [c2]\n"
        claims = extract_claims(body, known_markers={"c1", "c2"})
        assert len(claims) == 2
        assert claims[0].text == "First finding here"

    def test_code_fences_are_not_prose(self) -> None:
        body = "```\nprint('a very long line inside code [c1]')\n```\n"
        assert extract_claims(body, known_markers={"c1"}) == []

    def test_headings_switch_the_anchor(self) -> None:
        body = "## Alpha\n\nA first finding here [c1].\n\n## Beta\n\nA second finding here [c1].\n"
        claims = extract_claims(body, known_markers={"c1"})
        assert [c.section_anchor for c in claims] == ["alpha", "beta"]

    def test_a_bare_marker_asserts_nothing(self) -> None:
        assert extract_claims("[c1].", known_markers={"c1"}) == []

    def test_claim_count_is_bounded(self) -> None:
        body = "\n\n".join(f"Finding number {i} is worth stating [c1]." for i in range(200))
        assert len(extract_claims(body, known_markers={"c1"}, max_claims=5)) == 5
