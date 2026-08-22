"""`LiteLLMClient.rerank` — the third verb, and the first that may not exist.

WS-RS6. `Capability.RERANK` has been an enum member since the model-profile
schema was written and `packages/aleph-models/src/aleph_models/client.py` had
`chat` and `embed` and nothing else, so a project could bind a model to a job
Aleph would never ask anyone to do.

The case that matters most here is the one this deployment is in: `/v1/rerank`
is **routed** and serves **no reranker**. Verified by hand against the
configured gateway — a chat model gets

    400 {"error": "/rerank: Invalid model name passed in model=claude-haiku-4-5"}

not a 404. So the interesting behaviour is not "does a rerank succeed", it is
"does a gateway that cannot rerank produce something a caller can act on".
A generic transport error would be retried three times and reported as an
outage; `RerankUnsupported` is what lets `aleph_rks.rerank` fall back to the LLM
reranker once and remember.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest

from aleph_models.client import (
    LiteLLMClient,
    RerankUnsupported,
    _rerank_input_tokens,
    _rerank_results,
)
from aleph_models.pricing import PricingTable
from aleph_models.testing import (
    DEFAULT_MODELS,
    FakeGateway,
    FakeModel,
    GatewayConfig,
    RecordingSessions,
    rate_limited,
    server_error,
)
from aleph_security.principal import Principal

RERANKER = "rerank-v3-fake"
CHAT_MODEL = "claude-haiku-4-5"

DOCUMENTS = [
    "the offside rule in association football",
    "cores were retrieved using a vibracorer from a shallow-draft vessel",
    "grain-size distribution measured aboard the vessel",
]
QUERY = "vibracorer shallow-draft vessel"


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(), subject="rs6", email="rs6@example.com", actor_kind="aleph_agent"
    )


async def _no_sleep(_seconds: float) -> None:
    """Retries are real; waiting for them is not what this file tests."""


def _client(fake: FakeGateway, http: Any, sessions: RecordingSessions) -> LiteLLMClient:
    return LiteLLMClient(
        base_url=fake.base_url,
        api_key=fake.api_key,
        http_client=http,
        pricing=PricingTable(),
        session_maker=cast("Any", sessions),
        retry_sleep=_no_sleep,
    )


def _with_reranker(**overrides: Any) -> GatewayConfig:
    return GatewayConfig(
        models=(*DEFAULT_MODELS, FakeModel(id=RERANKER, mode="rerank")), **overrides
    )


async def _rerank(client: LiteLLMClient, model: str, sessions: RecordingSessions) -> Any:
    del sessions
    return await client.rerank(
        principal=_principal(),
        project_id=uuid4(),
        agent_run_id=None,
        profile_bindings={"rerank": {"model": model}},
        query=QUERY,
        documents=DOCUMENTS,
        top_n=2,
        purpose="test.rs6.rerank",
    )


# --- the happy path --------------------------------------------------------


async def test_a_served_reranker_returns_results_ordered_by_relevance() -> None:
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        response = await _rerank(_client(fake, http, sessions), RERANKER, sessions)
    assert [r.index for r in response.results] == [1, 2]
    assert response.results[0].relevance_score > response.results[1].relevance_score


async def test_top_n_reaches_the_gateway() -> None:
    """Without it the whole candidate window comes back and the caller pays to
    transport results it will discard."""
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        await _rerank(_client(fake, http, sessions), RERANKER, sessions)
    body = fake.requests[-1].body or {}
    assert body["top_n"] == 2
    assert body["documents"] == DOCUMENTS
    assert body["query"] == QUERY


async def test_the_call_is_ledgered_even_though_the_gateway_reports_no_tokens() -> None:
    """Rule 5 is that every gateway call writes a `ModelCall`.

    Rerank endpoints bill in search units and report no tokens, so the honest
    row has zero tokens and `pricing_source='unknown'` — never a silent $0 with
    no row at all, which is how 159 uncosted calls became invisible.
    """
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        await _rerank(_client(fake, http, sessions), RERANKER, sessions)
    calls = sessions.model_calls()
    assert len(calls) == 1
    assert calls[0].capability == "rerank"
    assert calls[0].model == RERANKER
    assert calls[0].purpose == "test.rs6.rerank"
    assert calls[0].pricing_source == "unknown"
    assert len(sessions.ledger_events()) == 1


# --- the case this deployment is actually in -------------------------------


async def test_a_chat_model_on_the_rerank_route_is_reported_as_unsupported() -> None:
    """The measured behaviour of the configured gateway, reproduced.

    A `mode="chat"` model posted to `/v1/rerank` gets a 400. If this surfaced as
    a plain `HTTPStatusError` the caller could not tell it from an outage, and
    `AdaptiveReranker` would keep probing on every single search.
    """
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        with pytest.raises(RerankUnsupported) as caught:
            await _rerank(_client(fake, http, sessions), CHAT_MODEL, sessions)
    assert CHAT_MODEL in str(caught.value)
    assert "400" in str(caught.value)


async def test_a_gateway_with_no_rerank_route_at_all_is_also_unsupported() -> None:
    """404 rather than 400 — an OpenAI-compatible server that simply has no
    such endpoint. Same conclusion, same fallback."""
    fake = FakeGateway(GatewayConfig(models=DEFAULT_MODELS))
    sessions = RecordingSessions()
    async with fake.client() as http:
        with pytest.raises(RerankUnsupported):
            await _rerank(_client(fake, http, sessions), CHAT_MODEL, sessions)


async def test_an_unsupported_model_is_not_retried() -> None:
    """A 400 will be a 400 next time. Three attempts is three times the latency
    on the path that always fails on this deployment."""
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        with pytest.raises(RerankUnsupported):
            await _rerank(_client(fake, http, sessions), CHAT_MODEL, sessions)
    assert fake.count("/v1/rerank") == 1


async def test_no_ledger_row_is_written_when_the_gateway_refuses() -> None:
    """Nothing was computed and nothing is owed."""
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        with pytest.raises(RerankUnsupported):
            await _rerank(_client(fake, http, sessions), CHAT_MODEL, sessions)
    assert sessions.model_calls() == []


# --- outages must NOT be reported as a capability gap ----------------------


async def test_a_bad_credential_is_an_outage_and_not_a_capability_gap() -> None:
    """401 stays a transport error.

    Translating it would send an operator to the model profile to fix an
    expired key, and `AdaptiveReranker` would permanently stop trying the fast
    path because somebody rotated a secret.
    """
    fake = FakeGateway(_with_reranker())
    sessions = RecordingSessions()
    async with fake.client() as http:
        client = LiteLLMClient(
            base_url=fake.base_url,
            api_key="sk-wrong",
            http_client=http,
            pricing=PricingTable(),
            session_maker=cast("Any", sessions),
            retry_sleep=_no_sleep,
        )
        # The type IS the assertion: whatever it is, it must not be RerankUnsupported.
        with pytest.raises(Exception) as caught:
            await _rerank(client, RERANKER, sessions)
    assert not isinstance(caught.value, RerankUnsupported)


async def test_a_503_is_retried_and_stays_an_outage() -> None:
    fake = FakeGateway(_with_reranker(invoke_script=(server_error(times=3),)))
    sessions = RecordingSessions()
    async with fake.client() as http:
        # The type IS the assertion: whatever it is, it must not be RerankUnsupported.
        with pytest.raises(Exception) as caught:
            await _rerank(_client(fake, http, sessions), RERANKER, sessions)
    assert not isinstance(caught.value, RerankUnsupported)
    assert fake.count("/v1/rerank") == 3


async def test_a_429_is_retried_before_it_succeeds() -> None:
    fake = FakeGateway(_with_reranker(invoke_script=(rate_limited(times=1),)))
    sessions = RecordingSessions()
    async with fake.client() as http:
        response = await _rerank(_client(fake, http, sessions), RERANKER, sessions)
    assert response.results
    assert fake.count("/v1/rerank") == 2


# --- parsing: an index is the only link back to the caller's list ----------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"results": [{"index": 0, "relevance_score": 0.5}]}, [(0, 0.5)]),
        # Some servers spell it `score`.
        ({"results": [{"index": 1, "score": 0.25}]}, [(1, 0.25)]),
        # Out of range for the documents that were SENT. Dropped, not clamped:
        # a clamped index reorders a passage the reranker never judged.
        ({"results": [{"index": 9, "relevance_score": 0.5}]}, []),
        ({"results": [{"index": -1, "relevance_score": 0.5}]}, []),
        # No index at all — unusable, because Cohere-shaped responses omit the
        # document text and the index is the only handle.
        ({"results": [{"relevance_score": 0.5}]}, []),
        ({"results": [{"index": True, "relevance_score": 0.5}]}, []),
        ({"results": [{"index": 0, "relevance_score": "high"}]}, []),
        ({"results": "everything"}, []),
        ({}, []),
    ],
)
def test_rerank_results_drop_anything_that_cannot_address_a_document(
    body: dict[str, Any], expected: list[tuple[int, float]]
) -> None:
    parsed = _rerank_results(body, 3)
    assert [(r.index, r.relevance_score) for r in parsed] == expected


@pytest.mark.parametrize(
    ("body", "tokens"),
    [
        ({"usage": {"prompt_tokens": 42}}, 42),
        ({"meta": {"billed_units": {"input_tokens": 7}}}, 7),
        # Cohere's actual default: search units, no tokens anywhere. Zero is
        # the honest answer and `pricing_source` records that it is not the
        # gateway's number.
        ({"meta": {"billed_units": {"search_units": 1}}}, 0),
        ({}, 0),
    ],
)
def test_rerank_token_count_reads_whichever_shape_the_gateway_used(
    body: dict[str, Any], tokens: int
) -> None:
    assert _rerank_input_tokens(body) == tokens
