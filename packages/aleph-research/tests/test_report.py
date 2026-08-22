"""Report building ([cN] marker mapping), DOI-verdict dropping, and the
compose node's style_pass application."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_research.evidence import ChunkRef, select_cards
from aleph_research.research_workflow import (
    Candidate,
    IngestedSource,
    ResearchLimits,
    _active_ctx_var,
    _Ctx,
    _node_compose,
    apply_doi_verdicts,
    build_report,
    sanitize_markers,
    summarize_body,
)
from aleph_scholar.types import DoiVerdict
from aleph_wiki.synthesis_workflow import ResearchClaim, ResearchSourceRef


def _verdict(doi: str, ok: bool | None, retracted: bool | None = False) -> DoiVerdict:
    return DoiVerdict(
        doi=doi,
        ok=ok,
        retracted=retracted,
        title="t",
        year=2024,
        openalex_id="W1",
        checked_via="crossref+openalex",
    )


def _cand(kind: str, doi: str | None = None, url: str | None = "https://x.test/a") -> Candidate:
    return Candidate(
        kind=kind,
        external_id=doi or url or "x",
        title="T",
        url=url,
        snippet=None,
        metadata={"doi": doi} if doi else {},
    )


class TestApplyDoiVerdicts:
    def test_ok_false_dropped_before_ingest(self) -> None:
        cands = [
            _cand("openalex", doi="10.1/good"),
            _cand("openalex", doi="10.1/bad"),
            _cand("tavily"),  # no DOI: passes through
            _cand("openalex", doi="10.1/unknown"),  # ok=None: passes through
        ]
        verdicts = [
            _verdict("10.1/good", ok=True),
            _verdict("10.1/bad", ok=False),
            _verdict("10.1/unknown", ok=None),
        ]
        kept = apply_doi_verdicts(cands, verdicts)
        kept_dois = [c.metadata.get("doi") for c, _ in kept]
        assert "10.1/bad" not in kept_dois
        assert kept_dois == ["10.1/good", None, "10.1/unknown"]
        # The verified candidate carries its verdict; the DOI-less one doesn't.
        assert kept[0][1] is not None and kept[0][1].ok is True
        assert kept[1][1] is None

    def test_doi_normalization_matches_verdict(self) -> None:
        cands = [_cand("openalex", doi="https://doi.org/10.1/BAD")]
        assert apply_doi_verdicts(cands, [_verdict("10.1/bad", ok=False)]) == []


def _ref(short_id: str, title: str, url: str | None = None) -> ResearchSourceRef:
    return ResearchSourceRef(
        source_short_id=short_id,
        title=title,
        url=url,
        chunk_id=uuid4(),
        char_start=10,
        char_end=40,
        quote="a grounded quote",
    )


class TestBuildReport:
    def test_markers_map_to_chunks_and_sources_dedupe(self) -> None:
        """A marker is a CHUNK now. Two markers may share one source, and
        `sources` must not then list that source twice."""
        refs = {
            "c1": _ref("S0001", "First", "https://a.test"),
            "c2": _ref("S0002", "Second"),
            "c3": _ref("S0001", "First", "https://a.test"),
        }
        report = build_report(
            topic="T",
            body_md="Claim [c1] and [c3].",
            summary="s",
            refs_by_marker=refs,
            claims=[ResearchClaim(text="Claim.", citation_markers=["c1"], section_anchor=None)],
        )
        assert [r.source_short_id for r in report.sources] == ["S0001", "S0002"]
        assert set(report.citations_by_marker) == {"c1", "c2", "c3"}
        assert report.citations_by_marker["c2"].title == "Second"
        assert report.citations_by_marker["c1"].url == "https://a.test"

    def test_claims_are_not_discarded(self) -> None:
        """`build_report` passed `claims=[]` unconditionally, so the research
        path wrote zero claims and therefore zero citations. WS-RS7 criterion 5."""
        claims = [
            ResearchClaim(text="The first finding.", citation_markers=["c1"], section_anchor="a"),
        ]
        report = build_report(
            topic="T",
            body_md="The first finding [c1].",
            summary="s",
            refs_by_marker={"c1": _ref("S0001", "First")},
            claims=claims,
        )
        assert report.claims == claims


class TestSanitizeMarkers:
    def test_out_of_range_markers_dropped(self) -> None:
        assert sanitize_markers("A [c1] B [c9] C [c2]", 2) == "A [c1] B  C [c2]"

    def test_in_range_untouched(self) -> None:
        assert sanitize_markers("A [c1][c2]", 2) == "A [c1][c2]"


class TestSummarizeBody:
    def test_first_non_heading_paragraph(self) -> None:
        body = "# Title\n\nThe summary\nparagraph.\n\nMore."
        assert summarize_body(body, fallback="f") == "The summary paragraph."

    def test_fallback(self) -> None:
        assert summarize_body("# Only headings", fallback="f") == "f"


class _RecordingScholar:
    """style_pass spy that delegates to the real implementation."""

    def __init__(self) -> None:
        self.style_pass_calls: list[str] = []

    def style_pass(self, markdown: str) -> str:
        from aleph_scholar.style import style_pass

        self.style_pass_calls.append(markdown)
        return style_pass(markdown)


class _CannedLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


#: Two chunks of real-looking source text. The compose node's pure half is
#: exercised against these; the retrieval half that produces them from Postgres
#: is covered by tests/e2e/test_research_reads_sources.py.
_CHUNK_ONE = (
    "Quantum illumination retains an advantage in the low-brightness regime. "
    "The entangled signal survives loss that destroys the correlation itself."
)
_CHUNK_TWO = (
    "Practical quantum radar remains limited by the absence of a quantum memory. "
    "Without one the idler must be stored classically, which forfeits the gain."
)


def _cards() -> list[Any]:
    refs = [
        ChunkRef(
            chunk_id=uuid4(),
            source_id=uuid4(),
            source_short_id="S0001",
            title="One",
            url="https://a.test",
            section_path="Results",
            text=_CHUNK_ONE,
            char_start=100,
            char_end=100 + len(_CHUNK_ONE),
        ),
        ChunkRef(
            chunk_id=uuid4(),
            source_id=uuid4(),
            source_short_id="S0002",
            title="Two",
            url=None,
            section_path=None,
            text=_CHUNK_TWO,
            char_start=0,
            char_end=len(_CHUNK_TWO),
        ),
    ]
    return select_cards(breadth=[refs])


def _ctx_for(llm: Any, scholar: Any) -> _Ctx:
    return _Ctx(
        session_maker=None,  # type: ignore[arg-type]  # with_phase no-ops without agent_run_id
        litellm=llm,
        principal=SimpleNamespace(user_id=uuid4(), correlation_id="c", actor_kind="aleph_agent"),  # type: ignore[arg-type]
        scholar=scholar,
        asset_store=None,  # type: ignore[arg-type]
        tools_by_kind={},
        profile_bindings={},
        limits=ResearchLimits(),
        agent_token_secret="s",
        enqueue=None,  # type: ignore[arg-type]
    )


def _state() -> dict[str, Any]:
    return {
        "agent_run_id": None,
        "project_id": uuid4(),
        "topic": "Quantum radar",
        "ingested": [
            IngestedSource(
                short_id="S0001",
                source_id=uuid4(),
                title="One",
                url="https://a.test",
                kind="tavily",
            ),
            IngestedSource(
                short_id="S0002", source_id=uuid4(), title="Two", url=None, kind="openalex"
            ),
        ],
    }


async def _compose(
    monkeypatch: pytest.MonkeyPatch, *, response: str, cards: list[Any] | None = None
) -> tuple[Any, _RecordingScholar, _CannedLLM]:
    """Drive `_node_compose` with a fixed pack and a fixed model response.

    The retrieval half is stubbed rather than mocked at the session level:
    `_gather_evidence` is the seam between "needs Postgres and a gateway" and
    "pure", and tests/e2e/test_research_reads_sources.py covers the other side
    of it against a real corpus.
    """
    from aleph_research import research_workflow as rw

    pack = _cards() if cards is None else cards

    async def _fake_gather(_state: Any, _ingested: Any) -> list[Any]:
        return pack

    monkeypatch.setattr(rw, "_gather_evidence", _fake_gather)
    scholar = _RecordingScholar()
    llm = _CannedLLM(response)
    token = _active_ctx_var.set(_ctx_for(llm, scholar))
    try:
        out = await rw._node_compose(_state())  # type: ignore[arg-type]
    finally:
        _active_ctx_var.reset(token)
    return out, scholar, llm


_GOOD_RESPONSE = (
    "## Findings\n\n"
    "Quantum illumination keeps its edge when the signal is weak [c1] "
    "and a fake one [c7].\n\n\n\n\n"
    "The missing piece is a quantum memory for the idler [c2].\n\n"
    '<!--aleph:evidence\n{"quotes": {"c1": "The entangled signal survives loss '
    'that destroys the correlation itself.", "c2": "Practical quantum radar remains '
    'limited by the absence of a quantum memory."}}\n-->'
)


async def test_compose_prompt_carries_chunk_text_not_a_title_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composer's user message must contain the SOURCE TEXT.

    Before WS-RS7 it contained `c1: One — https://a.test` and nothing else, so
    the report was written from the model's own recollection and the citation
    numbers were positions in a download list.
    """
    _, _, llm = await _compose(monkeypatch, response=_GOOD_RESPONSE)
    user = llm.calls[0]["messages"][1].content
    assert _CHUNK_ONE in user
    assert _CHUNK_TWO in user
    assert llm.calls[0]["purpose"] == "research.compose"


async def test_compose_applies_style_pass_and_sanitizes_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, scholar, _ = await _compose(monkeypatch, response=_GOOD_RESPONSE)
    report = out["report"]
    assert scholar.style_pass_calls, "compose must run the LLM-free style_pass"
    assert "[c7]" not in report.body_md
    assert "[c1]" in report.body_md and "[c2]" in report.body_md
    # style_pass collapses runs of >2 blank lines down to 2 (the draft had 4).
    assert "\n\n\n\n" not in report.body_md
    assert set(report.citations_by_marker) == {"c1", "c2"}
    # The evidence block never reaches the wiki page.
    assert "aleph:evidence" not in report.body_md
    assert "quotes" not in report.body_md


async def test_every_citation_carries_a_chunk_and_a_grounded_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _, _ = await _compose(monkeypatch, response=_GOOD_RESPONSE)
    report = out["report"]
    assert report.citations_by_marker
    for ref in report.citations_by_marker.values():
        assert ref.chunk_id is not None
        assert ref.quote
        assert ref.char_end > ref.char_start
    # c1's card starts at document offset 100, and its quote is the second
    # sentence of that chunk — so the span is offset into the DOCUMENT, not
    # into the chunk.
    c1 = report.citations_by_marker["c1"]
    assert c1.char_start == 100 + _CHUNK_ONE.index("The entangled")


async def test_citing_out_of_order_does_not_repoint_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`style_pass` renumbers `[cN]` to first-appearance order and knows nothing
    about the marker→chunk map.

    The old code built that map by list position and then handed the body to
    `style_pass`, which shuffled the numbers underneath it — so a report citing
    its sources out of order ended up with every citation pointing at the wrong
    document, with nothing raising. Compose now renumbers first, so the map
    moves with the body.

    A report that cites markers in order cannot detect this: the renumbering is
    the identity. This one deliberately cites c2 before c1.
    """
    response = (
        "## Findings\n\nThe memory problem comes first [c2]. "
        "The low-brightness result comes second [c1].\n\n"
        '<!--aleph:evidence\n{"quotes": {"c1": "The entangled signal survives loss '
        'that destroys the correlation itself.", "c2": "Practical quantum radar remains '
        'limited by the absence of a quantum memory."}}\n-->'
    )
    out, _, _ = await _compose(monkeypatch, response=response)
    report = out["report"]
    # First marker in the body is c1 after renumbering, and it must be the card
    # the model cited FIRST — which was S0002's.
    assert report.body_md.index("[c1]") < report.body_md.index("[c2]")
    assert report.citations_by_marker["c1"].source_short_id == "S0002"
    assert report.citations_by_marker["c2"].source_short_id == "S0001"
    assert "quantum memory" in report.citations_by_marker["c1"].quote


async def test_claims_are_extracted_from_the_composed_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out, _, _ = await _compose(monkeypatch, response=_GOOD_RESPONSE)
    report = out["report"]
    assert len(report.claims) == 2
    texts = [c.text for c in report.claims]
    assert "Quantum illumination keeps its edge when the signal is weak" in texts[0]
    assert all(c.section_anchor == "findings" for c in report.claims)
    assert all(c.citation_markers for c in report.claims)


async def test_a_fabricated_quote_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from aleph_research.evidence import FabricatedQuote

    response = (
        "## Findings\n\nQuantum radar is already deployed operationally [c1].\n\n"
        '<!--aleph:evidence\n{"quotes": {"c1": "Quantum radar is already deployed '
        'operationally by three navies."}}\n-->'
    )
    with pytest.raises(FabricatedQuote, match="c1"):
        await _compose(monkeypatch, response=response)


async def test_a_report_anchored_to_nothing_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No quote block at all: every marker is unanchored, so there is no report.

    Landing it would reproduce the exact defect — prose with citation
    decoration and no evidence behind it.
    """
    with pytest.raises(RuntimeError, match="anchored to nothing"):
        await _compose(monkeypatch, response="## Findings\n\nA claim [c1].")


async def test_no_retrievable_evidence_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="retrieved no evidence"):
        await _compose(monkeypatch, response=_GOOD_RESPONSE, cards=[])


@pytest.mark.asyncio
async def test_compose_with_no_sources_raises() -> None:
    ctx = _ctx_for(_CannedLLM(""), _RecordingScholar())
    token = _active_ctx_var.set(ctx)
    try:
        with pytest.raises(RuntimeError, match="no sources"):
            await _node_compose(
                {"agent_run_id": None, "project_id": uuid4(), "topic": "T", "ingested": []}  # type: ignore[arg-type]
            )
    finally:
        _active_ctx_var.reset(token)
