"""A dead embedder must be reported, not mistaken for an empty project.

`_search_corpus` used to catch every embedding failure and `return []`. That is
the same value it returns when the project genuinely has nothing on the subject,
so the assistant said "I could not find anything" during a total outage of half
its search — the most misleading answer a retrieval system can give.

These tests pin the two halves of the fix: the outage is named on the result,
and it is named *in the reply text* a person actually reads.

The dead embedder is not a stub that raises. It is a **real** `LiteLLMClient`
pointed at `aleph_models.testing.FakeGateway`, asked for `titan-embed-v2` — the
model name the deployed profile actually bound, against a gateway that actually
serves `titan-embed-text-v2`. That one-character-class difference is the live
production defect: every embed 400s, and because chunks are written only after
the embed returns it also killed the lexical leg, which needs no model at all.
A hand-written `raise RuntimeError("model not found")` asserts that the router
survives *an* exception. This asserts it survives *the* one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from aleph_assistant.retrieval.router import (
    _DEGRADED_NOTES,
    WikiFirstRetrievalRouter,
    _with_degradation,
)
from aleph_models.client import LiteLLMClient
from aleph_models.pricing import PricingTable
from aleph_models.testing import FakeGateway, RecordingSessions
from aleph_rks.models import EMBEDDING_DIM

#: What the profile bound on the deployed instance. No gateway serves it.
WRONG_EMBEDDER = "titan-embed-v2"
#: What the gateway serves. `FakeGateway`'s defaults carry this one and not the
#: one above, on purpose.
RIGHT_EMBEDDER = "titan-embed-text-v2"

EMBED_ROUTE = "/v1/embeddings"


class _Session:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[FakeGateway, LiteLLMClient]]:
    """A real gateway client, on the hostile default config."""
    fake = FakeGateway()
    async with fake.client() as http:
        yield (
            fake,
            LiteLLMClient(
                base_url=fake.base_url,
                api_key=fake.api_key,
                http_client=http,
                pricing=PricingTable(),
                session_maker=cast("Any", RecordingSessions()),
            ),
        )


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


async def test_a_dead_embedder_is_named_on_the_result(
    patched_search: dict[str, Any],
    gateway: tuple[FakeGateway, LiteLLMClient],
) -> None:
    fake, client = gateway
    router = _router(client, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={"embedding": {"model": WRONG_EMBEDDER}},
        query="what did the survey find",
        budget=8,
    )

    assert fake.models_requested() == [WRONG_EMBEDDER], (
        "the request that failed must be the one naming the unserved model"
    )
    assert fake.count(EMBED_ROUTE) == 1, (
        "a 400 is not retryable; retrying a wrong model name triples the latency "
        "of every failed turn and never succeeds"
    )
    assert router._degraded == "embedder_unavailable"


async def test_lexical_search_still_runs_when_the_embedder_is_dead(
    patched_search: dict[str, Any],
    gateway: tuple[FakeGateway, LiteLLMClient],
) -> None:
    """The keyword leg needs no model. Skipping the search entirely — which
    `return []` did — throws away the half that still works."""
    _fake, client = gateway
    router = _router(client, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={"embedding": {"model": WRONG_EMBEDDER}},
        query="quokka photosynthesis anomaly",
        budget=8,
    )

    assert patched_search["query_text"] == "quokka photosynthesis anomaly"
    assert patched_search["query_embedding"] is None, (
        "a zero vector is not a degraded dense leg, it is a meaningless one"
    )


async def test_a_healthy_embedder_reports_no_degradation(
    patched_search: dict[str, Any],
    gateway: tuple[FakeGateway, LiteLLMClient],
) -> None:
    """The check must be able to say 'fine' too, or it is a constant.

    Same gateway, same client, one correct model name — so what separates this
    case from the one above is exactly the defect, not the test scaffolding.
    """
    _fake, client = gateway
    router = _router(client, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={"embedding": {"model": RIGHT_EMBEDDER}},
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


# ---------------------------------------------------------------------------
# The reranker reaches the production search
# ---------------------------------------------------------------------------
#
# `Capability.RERANK` shipped with the reranker written, tested and unreachable.
# `search_corpus` had four production call sites and not one passed `reranker=`;
# forcing the parameter to None broke four tests inside the reranker's own file
# and nothing else in 1,450. `reranker_for` had zero non-test callers, and the
# capability was absent from `CAPABILITY_POLICIES` and from the Settings picker,
# so even a call site would have resolved to `NoReranker` on every project that
# exists.
#
# These pin the wiring at the one place production enters it.


async def test_the_production_search_is_given_a_reranker(
    patched_search: dict[str, Any],
    gateway: tuple[FakeGateway, LiteLLMClient],
) -> None:
    """`_search_corpus` must pass one, not leave the parameter at its default."""
    _fake, client = gateway
    router = _router(client, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={
            "embedding": {"model": RIGHT_EMBEDDER},
            "rerank": {"model": "bedrock-claude-haiku-4.5"},
        },
        query="what did the survey find",
        budget=8,
    )

    assert "reranker" in patched_search, (
        "search_corpus was called without a reranker at all — the capability "
        "resolves to NoReranker on every project no matter what is bound"
    )
    reranker = patched_search["reranker"]
    assert reranker is not None
    assert type(reranker).__name__ != "NoReranker", (
        "a bound rerank capability still produced a NoReranker; the binding is "
        f"not reaching reranker_for (got {reranker!r})"
    )


async def test_an_unbound_rerank_capability_degrades_rather_than_raising(
    patched_search: dict[str, Any],
    gateway: tuple[FakeGateway, LiteLLMClient],
) -> None:
    """A fresh project binds nothing until autoconfigure runs.

    That must produce fused order with a recorded reason, the way an unbound
    embedder produces lexical-only — not an exception on the search path.
    """
    _fake, client = gateway
    router = _router(client, patched_search)

    await router._search_corpus(
        principal=object(),  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        agent_run_id=None,
        profile_bindings={"embedding": {"model": RIGHT_EMBEDDER}},
        query="what did the survey find",
        budget=8,
    )

    reranker = patched_search.get("reranker")
    assert reranker is not None, "an unbound capability must still yield a reranker object"
    assert type(reranker).__name__ == "NoReranker"
    assert getattr(reranker, "skipped_reason", None), (
        "a reranker that stood down must say why; a silent skip is "
        "indistinguishable from a reranker that ran and changed nothing"
    )
