"""A dead embedder must be reported, not mistaken for an empty project.

`_search_corpus` used to catch every embedding failure and `return []`. That is
the same value it returns when the project genuinely has nothing on the subject,
so the assistant said "I could not find anything" during a total outage of half
its search — the most misleading answer a retrieval system can give.

These tests pin the two halves of the fix: the outage is named on the result,
and it is named *in the reply text* a person actually reads.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from aleph_assistant.retrieval.router import (
    _DEGRADED_NOTES,
    WikiFirstRetrievalRouter,
    _with_degradation,
)
from aleph_rks.models import EMBEDDING_DIM


class _DeadEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:
        self.calls += 1
        msg = "litellm.BadRequestError: model not found"
        raise RuntimeError(msg)


@dataclass
class _EmbedResponse:
    embeddings: list[list[float]]


class _LiveEmbedder:
    async def embed(self, **_kw: Any) -> _EmbedResponse:
        return _EmbedResponse(embeddings=[[0.5] * EMBEDDING_DIM])


class _Session:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _router(embedder: Any, captured: dict[str, Any]) -> WikiFirstRetrievalRouter:
    return WikiFirstRetrievalRouter(
        session_maker=lambda: _Session(captured),  # type: ignore[arg-type]
        litellm=embedder,
    )


@pytest.fixture
def patched_search(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what `search_corpus` was asked for, and answer with nothing."""
    captured: dict[str, Any] = {}

    async def _fake_search_corpus(_session: Any, **kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    async def _fake_short_ids(_session: Any, _ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        return {}

    monkeypatch.setattr("aleph_assistant.retrieval.router.search_corpus", _fake_search_corpus)
    monkeypatch.setattr("aleph_assistant.retrieval.router._source_short_ids", _fake_short_ids)
    return captured


async def test_a_dead_embedder_is_named_on_the_result(patched_search: dict[str, Any]) -> None:
    embedder = _DeadEmbedder()
    router = _router(embedder, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={},
        query="what did the survey find",
        budget=8,
    )

    assert embedder.calls == 1
    assert router._degraded == "embedder_unavailable"


async def test_lexical_search_still_runs_when_the_embedder_is_dead(
    patched_search: dict[str, Any],
) -> None:
    """The keyword leg needs no model. Skipping the search entirely — which
    `return []` did — throws away the half that still works."""
    router = _router(_DeadEmbedder(), patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={},
        query="quokka photosynthesis anomaly",
        budget=8,
    )

    assert patched_search["query_text"] == "quokka photosynthesis anomaly"
    assert patched_search["query_embedding"] is None, (
        "a zero vector is not a degraded dense leg, it is a meaningless one"
    )


async def test_a_healthy_embedder_reports_no_degradation(
    patched_search: dict[str, Any],
) -> None:
    """The check must be able to say 'fine' too, or it is a constant."""
    router = _router(_LiveEmbedder(), patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={},
        query="anything",
        budget=8,
    )

    assert router._degraded is None
    assert patched_search["query_embedding"] == [0.5] * EMBEDDING_DIM


def test_the_composed_body_names_the_degradation() -> None:
    body = _with_degradation("Here is what I found.", "embedder_unavailable")
    assert "Here is what I found." in body
    assert _DEGRADED_NOTES["embedder_unavailable"] in body


def test_an_undegraded_body_is_returned_untouched() -> None:
    assert _with_degradation("Here is what I found.", None) == "Here is what I found."
