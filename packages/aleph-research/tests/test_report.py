"""Report building ([cN] marker mapping), DOI-verdict dropping, and the
compose node's style_pass application."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

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


class TestBuildReport:
    def test_sources_map_to_cn_markers_in_order(self) -> None:
        sources = [
            IngestedSource(short_id="S0001", title="First", url="https://a.test", kind="tavily"),
            IngestedSource(short_id="S0002", title="Second", url=None, kind="openalex"),
            IngestedSource(short_id="S0003", title="Third", url="https://c.test", kind="arxiv"),
        ]
        report = build_report(
            topic="T", body_md="Claim [c1] and [c3].", summary="s", sources=sources
        )
        assert [r.source_short_id for r in report.sources] == ["S0001", "S0002", "S0003"]
        assert set(report.citations_by_marker) == {"c1", "c2", "c3"}
        assert report.citations_by_marker["c2"].source_short_id == "S0002"
        assert report.citations_by_marker["c2"].title == "Second"
        assert report.citations_by_marker["c1"].url == "https://a.test"
        assert report.claims == []


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


@pytest.mark.asyncio
async def test_compose_applies_style_pass_and_sanitizes_markers() -> None:
    # Three+ blank lines collapse to two (style_pass), and the hallucinated
    # [c7] marker (only 2 sources) is dropped before verification can block.
    draft = "## Findings\n\nA claim [c1] and a fake one [c7].\n\n\n\n\nMore [c2]."
    scholar = _RecordingScholar()
    llm = _CannedLLM(draft)
    ctx = _Ctx(
        session_maker=None,  # type: ignore[arg-type]  # with_phase no-ops without agent_run_id
        litellm=llm,  # type: ignore[arg-type]
        principal=SimpleNamespace(user_id=uuid4(), correlation_id="c", actor_kind="aleph_agent"),  # type: ignore[arg-type]
        scholar=scholar,  # type: ignore[arg-type]
        asset_store=None,  # type: ignore[arg-type]
        tools_by_kind={},
        profile_bindings={},
        limits=ResearchLimits(),
        agent_token_secret="s",
        enqueue=None,  # type: ignore[arg-type]
    )
    state = {
        "agent_run_id": None,
        "project_id": uuid4(),
        "topic": "Quantum radar",
        "ingested": [
            IngestedSource(short_id="S0001", title="One", url="https://a.test", kind="tavily"),
            IngestedSource(short_id="S0002", title="Two", url=None, kind="openalex"),
        ],
    }
    token = _active_ctx_var.set(ctx)
    try:
        out = await _node_compose(state)  # type: ignore[arg-type]
    finally:
        _active_ctx_var.reset(token)

    report = out["report"]
    assert scholar.style_pass_calls, "compose must run the LLM-free style_pass"
    assert "[c7]" not in report.body_md
    assert "[c1]" in report.body_md and "[c2]" in report.body_md
    # style_pass collapses runs of >2 blank lines down to 2 (the draft had 4).
    assert "\n\n\n\n" not in report.body_md
    assert report.summary.startswith("A claim [c1]")
    assert set(report.citations_by_marker) == {"c1", "c2"}
    # The compose call is attributed to research.compose on the cost ledger.
    assert llm.calls[0]["purpose"] == "research.compose"


@pytest.mark.asyncio
async def test_compose_with_no_sources_raises() -> None:
    ctx = _Ctx(
        session_maker=None,  # type: ignore[arg-type]
        litellm=_CannedLLM(""),  # type: ignore[arg-type]
        principal=None,  # type: ignore[arg-type]
        scholar=_RecordingScholar(),  # type: ignore[arg-type]
        asset_store=None,  # type: ignore[arg-type]
        tools_by_kind={},
        profile_bindings={},
        limits=ResearchLimits(),
        agent_token_secret="s",
        enqueue=None,  # type: ignore[arg-type]
    )
    token = _active_ctx_var.set(ctx)
    try:
        with pytest.raises(RuntimeError, match="no sources"):
            await _node_compose(
                {"agent_run_id": None, "project_id": uuid4(), "topic": "T", "ingested": []}  # type: ignore[arg-type]
            )
    finally:
        _active_ctx_var.reset(token)
