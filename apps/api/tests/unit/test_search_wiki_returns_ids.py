"""`search_wiki` must return something `open_page` can be called with.

`open_page` takes a page id. The scan formatter emitted title, kind, score and
summary and no identifier of any kind, so the agent had no way to obtain one —
`open_page` had no reachable success path at all, and the agent could only guess
an id or give up. A scan that cannot be acted on wastes the turn it costs.

This drives the real tool with a stubbed IndexService rather than asserting on a
formatting helper, because the defect was in what the tool RETURNS.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest

from aleph_api import copilot_agent
from aleph_wiki.index_service import PageSelectionResult

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

PAGES = [
    PageSelectionResult(
        page_id=uuid.uuid4(),
        title="Retrieval-Augmented Generation",
        slug="retrieval-augmented-generation",
        summary="Grounding an answer in retrieved passages.",
        score=0.91,
        wikilinks_out=[],
        page_kind="topic",
        is_stub=False,
    ),
    PageSelectionResult(
        page_id=uuid.uuid4(),
        title="Reranking",
        slug="reranking",
        summary="",
        score=0.42,
        wikilinks_out=[],
        page_kind="topic",
        is_stub=True,
    ),
]


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _IndexService:
    def __init__(self, _session: Any, *, hits: list[PageSelectionResult]) -> None:
        self._hits = hits

    async def select_pages(self, **_kw: Any) -> list[PageSelectionResult]:
        return self._hits

    async def list_pages(self, **_kw: Any) -> list[PageSelectionResult]:
        return self._hits


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setitem(copilot_agent._runtime, "session_maker", lambda: _Session())

    async def _project(_config: Any) -> uuid.UUID:
        return project_id

    monkeypatch.setattr(copilot_agent, "_project_id_from_config", _project)


async def _search(monkeypatch: pytest.MonkeyPatch, hits: list[PageSelectionResult]) -> str:
    monkeypatch.setattr(
        copilot_agent, "IndexService", lambda session: _IndexService(session, hits=hits)
    )
    return await copilot_agent.search_wiki.ainvoke({"query": "rag", "config": {}})


async def test_every_result_line_carries_a_page_id(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = await _search(monkeypatch, PAGES)
    for page in PAGES:
        assert str(page.page_id) in out, f"{page.title} came back with no id"


async def test_the_id_is_parseable_as_a_uuid(wired: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare number or a truncated hex prefix would satisfy "contains an id"
    and would not be usable by `open_page`."""
    out = await _search(monkeypatch, PAGES)
    found = {uuid.UUID(m) for m in UUID_RE.findall(out)}
    assert {p.page_id for p in PAGES} <= found


async def test_a_page_with_no_summary_still_carries_its_id(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub with an empty summary is the row most likely to be formatted
    down to nothing."""
    out = await _search(monkeypatch, [PAGES[1]])
    assert str(PAGES[1].page_id) in out


async def test_the_scan_still_names_the_tool_that_uses_the_id(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = await _search(monkeypatch, PAGES)
    assert "open_page" in out, "the agent is handed an id and not told what takes one"


async def test_an_empty_wiki_says_so_rather_than_inventing_a_row(
    wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = await _search(monkeypatch, [])
    assert "no pages yet" in out.lower()
    assert not UUID_RE.findall(out)
