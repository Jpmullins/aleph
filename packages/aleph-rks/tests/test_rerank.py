"""The second pass, and the ways a second pass lies.

WS-RS6. Every test here drives production code in `aleph_rks.rerank` or
`aleph_rks.retrieval`; the only thing faked is the gateway's reply, because the
gateway is the one part that cannot be made deterministic.

The defect class being guarded is specific and this repository has shipped it
repeatedly: a component that is *computed and then dropped*. A reranker whose
output is discarded looks exactly like a reranker that agreed with fusion, and
the eval would report the difference as noise. So the tests that matter most are
the ones that mutate the model's answer and require the result to move.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aleph_core.errors import ValidationFailed
from aleph_models.client import (
    ChatChoice,
    ChatMessage,
    ChatResponse,
    ChatUsage,
    LiteLLMClient,
    RerankResponse,
    RerankResult,
    RerankUnsupported,
)
from aleph_rks import retrieval as retrieval_mod
from aleph_rks.rerank import (
    REASON_UNBOUND,
    AdaptiveReranker,
    CrossEncoderReranker,
    ListwiseLlmReranker,
    NoReranker,
    _loads_lenient,
    apply_ranking,
    parse_judgement,
    parse_scores,
    reranker_for,
)
from aleph_rks.retrieval import ChunkHit, search_corpus
from aleph_security.principal import Principal

PROJECT = UUID("01a02575-2a98-7335-92d7-fe8dfb5bafd0")
PRINCIPAL = Principal(user_id=uuid4(), subject="rs6", email="rs6@example.test", actor_kind="user")
BINDINGS = {"rerank": {"model": "a-judge-model"}}


def hits(n: int) -> list[ChunkHit]:
    """`n` fused hits, in fusion order, each from its own source."""
    return [
        ChunkHit(
            chunk_id=UUID(int=i, version=4),
            ordinal=i,
            text=f"passage {i}",
            section_path=None,
            score=1.0 / (60 + i + 1),
            source_id=UUID(int=1000 + i, version=4),
        )
        for i in range(n)
    ]


# --- apply_ranking: the decisions, not the plumbing ------------------------


def test_the_model_order_wins_over_the_fused_order() -> None:
    """If this fails, the reranker is being computed and thrown away."""
    ranked = apply_ranking(hits(4), [(3, 3.0), (0, 1.0)], top_k=4, keep_unranked=True)
    assert [h.text for h in ranked[:2]] == ["passage 3", "passage 0"]


def test_a_higher_score_outranks_an_earlier_fused_position() -> None:
    ranked = apply_ranking(hits(3), [(0, 1.0), (2, 3.0)], top_k=3, keep_unranked=True)
    assert ranked[0].text == "passage 2"


def test_ties_fall_back_to_the_fused_order() -> None:
    """A coarse 0-3 scale produces ties constantly; the fallback must be stable."""
    ranked = apply_ranking(hits(4), [(2, 2.0), (1, 2.0)], top_k=4, keep_unranked=True)
    assert [h.text for h in ranked[:2]] == ["passage 1", "passage 2"]


def test_judging_nothing_relevant_returns_nothing() -> None:
    """The abstention signal. A cosine floor cannot produce this (decisions D10)."""
    assert apply_ranking(hits(5), [], top_k=5, keep_unranked=True) == []


def test_a_partial_judgement_does_not_lose_recall() -> None:
    """Unjudged candidates keep their fused order BEHIND the judged ones."""
    ranked = apply_ranking(hits(5), [(4, 3.0)], top_k=5, keep_unranked=True)
    assert [h.text for h in ranked] == [
        "passage 4",
        "passage 0",
        "passage 1",
        "passage 2",
        "passage 3",
    ]


def test_the_strict_variant_drops_what_the_model_did_not_list() -> None:
    ranked = apply_ranking(hits(5), [(4, 3.0)], top_k=5, keep_unranked=False)
    assert [h.text for h in ranked] == ["passage 4"]


def test_the_judgement_is_kept_on_the_hit_and_not_discarded() -> None:
    """`cosine_distance` and `lexical_rank` were computed and dropped once each.

    A rerank score is a stronger signal than either and the same file is where
    it would go missing.
    """
    ranked = apply_ranking(hits(3), [(1, 3.0)], top_k=3, keep_unranked=True)
    assert ranked[0].rerank_score == 3.0
    assert ranked[0].rerank_position == 0
    # An unjudged survivor carries no score — absence of a judgement is not a
    # judgement of zero.
    assert ranked[1].rerank_score is None
    assert ranked[1].rerank_position == 1


def test_the_fused_score_is_left_alone() -> None:
    """Two incomparable scales must not share one field."""
    original = hits(2)
    ranked = apply_ranking(original, [(1, 3.0)], top_k=2, keep_unranked=True)
    assert ranked[0].score == original[1].score


def test_top_k_is_applied_after_the_judgement() -> None:
    ranked = apply_ranking(hits(10), [(9, 3.0), (8, 2.0)], top_k=2, keep_unranked=True)
    assert [h.text for h in ranked] == ["passage 9", "passage 8"]


# --- parse_scores: everything the model can get wrong ----------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"relevant": [{"id": 1, "score": 3}]}, [(1, 3.0)]),
        # An id that names no candidate. Dropped, never clamped: a clamped id
        # reorders a passage the model never judged, wearing its authority.
        ({"relevant": [{"id": 99, "score": 3}]}, []),
        ({"relevant": [{"id": -1, "score": 3}]}, []),
        # The same id twice — first judgement stands.
        ({"relevant": [{"id": 1, "score": 3}, {"id": 1, "score": 1}]}, [(1, 3.0)]),
        # Below the relevance floor: the model listed it and scored it 0.
        ({"relevant": [{"id": 1, "score": 0}]}, []),
        ({"relevant": [{"id": "1", "score": 3}]}, []),
        ({"relevant": [{"id": 1, "score": "high"}]}, []),
        # `True` is an int in Python. It is not a passage id.
        ({"relevant": [{"id": True, "score": 3}]}, []),
        ({"relevant": "everything"}, []),
        ({}, []),
    ],
)
def test_parse_scores_drops_what_it_cannot_trust(
    payload: dict[str, Any], expected: list[tuple[int, float]]
) -> None:
    assert parse_scores(payload, 3) == expected


def test_json_inside_a_fence_still_parses() -> None:
    """Measured on this deployment: most replies arrive fenced despite
    `response_format=json_object`, and a parse failure presents as an
    abstention — the worst way for a bug to look correct."""
    assert _loads_lenient('```json\n{"relevant": []}\n```') == {"relevant": []}
    assert _loads_lenient('Sure!\n{"relevant": [{"id": 0, "score": 2}]}\n') == {
        "relevant": [{"id": 0, "score": 2}]
    }


def test_a_reply_with_no_json_raises_rather_than_reading_as_empty() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        _loads_lenient("I am unable to help with that.")


# --- the LLM backend, against a fake gateway -------------------------------


class FakeChatClient:
    """A gateway that returns one canned completion. Only the reply is faked."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> ChatResponse:
        self.calls.append(kwargs)
        return ChatResponse(
            id="fake",
            model="a-judge-model",
            choices=[
                ChatChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=ChatUsage(),
            cost_usd="0",
            cache_savings_usd="0",
            latency_ms=1,
            model_call_id=str(uuid4()),
        )


def llm_reranker(content: str, *, keep_unranked: bool = True) -> tuple[Any, FakeChatClient]:
    fake = FakeChatClient(content)
    return (
        ListwiseLlmReranker(
            client=cast("LiteLLMClient", fake),
            principal=PRINCIPAL,
            project_id=PROJECT,
            profile_bindings=BINDINGS,
            keep_unranked=keep_unranked,
        ),
        fake,
    )


@pytest.mark.asyncio
async def test_the_llm_reranker_consumes_the_model_answer() -> None:
    reranker, _ = llm_reranker('{"relevant": [{"id": 2, "score": 3}, {"id": 0, "score": 1}]}')
    ranked = await reranker.rank(query="q", hits=hits(3), top_k=3)
    assert [h.text for h in ranked[:2]] == ["passage 2", "passage 0"]


@pytest.mark.asyncio
async def test_reversing_the_model_answer_reverses_the_result() -> None:
    """The mutation the plan's Review step names, run as a test.

    Two identical candidate lists, two opposite judgements, two opposite
    outputs. If a future change computed the ranking and returned fusion order,
    every other test here could still pass and this one could not.
    """
    forward, _ = llm_reranker('{"relevant": [{"id": 0, "score": 3}, {"id": 2, "score": 1}]}')
    backward, _ = llm_reranker('{"relevant": [{"id": 2, "score": 3}, {"id": 0, "score": 1}]}')
    a = await forward.rank(query="q", hits=hits(3), top_k=3)
    b = await backward.rank(query="q", hits=hits(3), top_k=3)
    assert [h.text for h in a[:2]] == list(reversed([h.text for h in b[:2]]))


@pytest.mark.asyncio
async def test_an_empty_judgement_abstains() -> None:
    reranker, _ = llm_reranker('{"relevant": []}')
    assert await reranker.rank(query="q", hits=hits(5), top_k=5) == []


@pytest.mark.asyncio
async def test_an_unparseable_reply_is_not_an_abstention() -> None:
    """A broken reranker must not present as perfect humility."""
    reranker, _ = llm_reranker("the passages are all quite interesting")
    ranked = await reranker.rank(query="q", hits=hits(4), top_k=4)
    assert [h.text for h in ranked] == [f"passage {i}" for i in range(4)]


@pytest.mark.asyncio
async def test_the_judge_is_asked_at_temperature_zero() -> None:
    """A judgement that changes between two identical searches is unmeasurable."""
    reranker, fake = llm_reranker('{"relevant": []}')
    await reranker.rank(query="q", hits=hits(2), top_k=2)
    assert fake.calls[0]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_the_prompt_carries_the_passage_text_and_its_id() -> None:
    reranker, fake = llm_reranker('{"relevant": []}')
    await reranker.rank(query="how were the cores collected", hits=hits(2), top_k=2)
    user = fake.calls[0]["messages"][-1].content
    assert "how were the cores collected" in user
    assert "[0] passage 0" in user
    assert "[1] passage 1" in user


@pytest.mark.asyncio
async def test_an_empty_candidate_list_costs_no_model_call() -> None:
    reranker, fake = llm_reranker('{"relevant": []}')
    assert await reranker.rank(query="q", hits=[], top_k=5) == []
    assert fake.calls == []


# --- the cross-encoder backend and the adaptive probe ----------------------


class FakeRerankClient:
    """A gateway whose `/v1/rerank` either answers or refuses the model."""

    def __init__(self, *, results: list[RerankResult] | None, unsupported: bool = False) -> None:
        self.results = results
        self.unsupported = unsupported
        self.chat_client = FakeChatClient('{"relevant": [{"id": 1, "score": 3}]}')
        self.rerank_calls = 0

    async def rerank(self, **kwargs: Any) -> RerankResponse:
        self.rerank_calls += 1
        if self.unsupported:
            msg = "the gateway will not rerank with 'a-judge-model'"
            raise RerankUnsupported(msg)
        del kwargs
        return RerankResponse(
            model="a-judge-model",
            results=self.results or [],
            latency_ms=1,
            model_call_id=str(uuid4()),
        )

    async def chat(self, **kwargs: Any) -> ChatResponse:
        return await self.chat_client.chat(**kwargs)


def adaptive(fake: FakeRerankClient) -> AdaptiveReranker:
    client = cast("LiteLLMClient", fake)
    return AdaptiveReranker(
        native=CrossEncoderReranker(
            client=client, principal=PRINCIPAL, project_id=PROJECT, profile_bindings=BINDINGS
        ),
        fallback=ListwiseLlmReranker(
            client=client, principal=PRINCIPAL, project_id=PROJECT, profile_bindings=BINDINGS
        ),
    )


@pytest.mark.asyncio
async def test_the_cross_encoder_ordering_is_consumed() -> None:
    fake = FakeRerankClient(
        results=[
            RerankResult(index=2, relevance_score=0.9),
            RerankResult(index=0, relevance_score=0.4),
        ]
    )
    ranked = await adaptive(fake).rank(query="q", hits=hits(3), top_k=3)
    assert [h.text for h in ranked[:2]] == ["passage 2", "passage 0"]
    assert fake.chat_client.calls == [], "the native path answered; no LLM call is warranted"


@pytest.mark.asyncio
async def test_an_empty_cross_encoder_result_is_not_an_abstention() -> None:
    """A cross-encoder scores everything it is shown, so nothing back means the
    transport returned nothing — not that nothing is relevant."""
    ranked = await adaptive(FakeRerankClient(results=[])).rank(query="q", hits=hits(3), top_k=3)
    assert [h.text for h in ranked] == ["passage 0", "passage 1", "passage 2"]


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_rerank_falls_back_to_the_llm() -> None:
    """The case that actually runs: this deployment serves no reranker."""
    fake = FakeRerankClient(results=None, unsupported=True)
    reranker = adaptive(fake)
    ranked = await reranker.rank(query="q", hits=hits(3), top_k=3)
    assert ranked[0].text == "passage 1"
    assert reranker.name == "llm-listwise"


@pytest.mark.asyncio
async def test_the_unsupported_probe_happens_once_per_process_not_once_per_search() -> None:
    fake = FakeRerankClient(results=None, unsupported=True)
    reranker = adaptive(fake)
    for _ in range(3):
        await reranker.rank(query="q", hits=hits(3), top_k=3)
    assert fake.rerank_calls == 1
    assert len(fake.chat_client.calls) == 3


# --- reranker_for: an unbound capability degrades, it does not raise -------


def test_no_rerank_binding_yields_a_reason_not_an_exception() -> None:
    reranker = reranker_for(
        client=cast("LiteLLMClient", object()),
        principal=PRINCIPAL,
        project_id=PROJECT,
        profile_bindings={"synthesis": {"model": "m"}},
    )
    assert isinstance(reranker, NoReranker)
    assert reranker.skipped_reason == REASON_UNBOUND
    with pytest.raises(ValidationFailed):
        # The same lookup, unguarded, to show the degradation is deliberate
        # rather than a binding that happens to exist.
        from aleph_models.profile import resolve_binding

        resolve_binding({"synthesis": {"model": "m"}}, "rerank")


def test_a_rerank_binding_yields_a_real_reranker() -> None:
    reranker = reranker_for(
        client=cast("LiteLLMClient", object()),
        principal=PRINCIPAL,
        project_id=PROJECT,
        profile_bindings=BINDINGS,
    )
    assert isinstance(reranker, AdaptiveReranker)
    assert reranker.skipped_reason is None


@pytest.mark.asyncio
async def test_the_null_reranker_returns_the_fused_order() -> None:
    reranker = NoReranker(skipped_reason="because")
    assert await reranker.rank(query="q", hits=hits(5), top_k=3) == hits(5)[:3]


# --- the span: reranking is never skipped in silence -----------------------


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """Attach to whatever SDK provider this process ended up with.

    `set_tracer_provider` refuses to be overridden and pytest shares one
    process, so installing a provider is not something a test can assume.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        trace.set_tracer_provider(TracerProvider())
        provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp


def _search_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = [s for s in exporter.get_finished_spans() if s.name == "rks.search"]
    assert spans, "search_corpus emitted no rks.search span"
    return spans[-1]


@pytest.fixture
def fused(monkeypatch: pytest.MonkeyPatch) -> list[ChunkHit]:
    """Fusion, stubbed — so the span assertions need no Postgres.

    `_hybrid_search` is covered against a real database by
    `tests/e2e/test_search_corpus.py` and by `test_vector_scan.py` in this
    directory. What is under test HERE is the decision layer above it, which is
    pure and which is where the silence lived.
    """
    candidates = hits(30)

    async def _stub(_session: Any, **kwargs: Any) -> list[ChunkHit]:
        return candidates[: kwargs["top_k"]]

    monkeypatch.setattr(retrieval_mod, "_hybrid_search", _stub)
    return candidates


@pytest.mark.asyncio
async def test_a_search_with_no_reranker_says_so_on_the_span(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    del fused
    await search_corpus(
        cast("Any", object()), project_id=PROJECT, query_text="q", query_embedding=None, top_k=5
    )
    attrs = _search_span(exporter).attributes or {}
    assert attrs["retrieval.rerank.skipped"] == retrieval_mod.REASON_RERANK_NOT_REQUESTED


@pytest.mark.asyncio
async def test_an_unbound_rerank_capability_states_its_reason_on_the_span(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    """WS-RS6 criterion: rerank is never silently skipped."""
    del fused
    await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=reranker_for(
            client=cast("LiteLLMClient", object()),
            principal=PRINCIPAL,
            project_id=PROJECT,
            profile_bindings={},
        ),
    )
    attrs = _search_span(exporter).attributes or {}
    skipped = attrs["retrieval.rerank.skipped"]
    assert skipped == REASON_UNBOUND
    assert isinstance(skipped, str) and len(skipped) > 20, (
        "the attribute must carry a reason a person can act on, not a boolean"
    )


@pytest.mark.asyncio
async def test_a_search_that_reranks_carries_no_skip_attribute(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    del fused
    reranker, _ = llm_reranker('{"relevant": [{"id": 7, "score": 3}]}')
    ranked = await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=reranker,
    )
    attrs = _search_span(exporter).attributes or {}
    assert "retrieval.rerank.skipped" not in attrs
    assert attrs["retrieval.rerank.backend"] == "llm-listwise"
    assert attrs["retrieval.rerank.candidates"] == 30
    assert ranked[0].text == "passage 7"


@pytest.mark.asyncio
async def test_the_reranker_sees_the_window_and_not_just_top_k(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    """A reranker shown only `top_k` rows can reorder them and nothing more.

    The hit at fused rank 25 is the one it exists to promote — and with
    `rerank_window` ignored it would never be a candidate at all. Asserted
    through the promotion, not just the span, so a window that is fetched and
    then truncated before the judgement still fails.
    """
    del fused
    reranker, fake = llm_reranker('{"relevant": [{"id": 24, "score": 3}]}')
    ranked = await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=reranker,
        rerank_window=30,
    )
    assert _search_span(exporter).attributes is not None
    assert "[24] passage 24" in fake.calls[0]["messages"][-1].content
    assert ranked[0].text == "passage 24"


@pytest.mark.asyncio
async def test_an_abstention_is_recorded_as_an_abstention(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    """Zero results after a non-empty candidate list is the one outcome that
    looks like a broken retriever and is not."""
    del fused
    reranker, _ = llm_reranker('{"relevant": []}')
    ranked = await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=reranker,
    )
    assert ranked == []
    attrs = _search_span(exporter).attributes or {}
    assert attrs["retrieval.rerank.abstained"] is True


# ---------------------------------------------------------------------------
# A reranker may not take down the search it is decorating
# ---------------------------------------------------------------------------


class _ExplodingReranker:
    """A reranker whose transport fails, the way the real one does.

    Not hypothetical: the reference gateway answers `POST /v1/rerank` with
    **500** "Unsupported provider: bedrock_mantle" — not the 4xx that
    `RerankUnsupported` is written for — so on the deployment this repository
    is developed against, binding `Capability.RERANK` turned every corpus
    search into an unhandled `HTTPStatusError`. Retrieval is the primary
    function; reranking is a second stage over its output.
    """

    name = "exploding"
    skipped_reason: str | None = None

    async def rank(self, *, query: str, hits: list[ChunkHit], top_k: int) -> list[ChunkHit]:
        del query, hits, top_k
        raise RuntimeError("Server error '500 Internal Server Error' for url '/v1/rerank'")


@pytest.mark.asyncio
async def test_a_failing_reranker_degrades_to_fused_order(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    ranked = await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=cast("Any", _ExplodingReranker()),
    )
    assert len(ranked) == 5, "the search returned nothing because the reranker failed"
    assert [h.text for h in ranked] == [h.text for h in fused[:5]], (
        "a failed rerank must leave fusion order untouched"
    )


@pytest.mark.asyncio
async def test_a_failing_reranker_says_so_rather_than_failing_quietly(
    exporter: InMemorySpanExporter, fused: list[ChunkHit]
) -> None:
    """Degrading silently is indistinguishable from a reranker that ran and
    agreed with fusion — the exact confusion `retrieval.rerank.skipped` exists
    to prevent."""
    del fused
    await search_corpus(
        cast("Any", object()),
        project_id=PROJECT,
        query_text="q",
        query_embedding=None,
        top_k=5,
        reranker=cast("Any", _ExplodingReranker()),
    )
    attrs = _search_span(exporter).attributes or {}
    assert attrs.get("retrieval.rerank.failed") is True
    skipped = attrs.get("retrieval.rerank.skipped")
    assert isinstance(skipped, str)
    assert "exploding" in skipped and "500" in skipped, (
        f"the reason must name the backend and what it said, got {skipped!r}"
    )
    assert attrs["retrieval.rerank.backend"] == "exploding"


@pytest.mark.asyncio
async def test_the_adaptive_reranker_falls_back_on_a_5xx_not_only_a_4xx() -> None:
    """The reference gateway 500s where the code expected 4xx.

    `POST /v1/rerank` answers 500 "litellm.APIConnectionError: Unsupported
    provider: bedrock_mantle" — semantically "this model cannot rerank",
    syntactically a server error. Catching only `RerankUnsupported` meant the
    LLM fallback that exists precisely for this deployment never ran on it: the
    exception escaped `AdaptiveReranker` and the whole search degraded to fused
    order with a reranker sitting right there, unused.
    """
    candidates = hits(5)

    class _Native:
        name = "cross-encoder"
        calls = 0

        async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
            type(self).calls += 1
            raise RuntimeError("Server error '500 Internal Server Error' for url '/v1/rerank'")

    class _Fallback:
        name = "llm-listwise"
        calls = 0

        async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
            type(self).calls += 1
            return list(reversed(list(hits)))[:top_k]

    adaptive = AdaptiveReranker(native=cast("Any", _Native()), fallback=cast("Any", _Fallback()))
    out = await adaptive.rank(query="q", hits=candidates, top_k=3)

    assert _Fallback.calls == 1, "the LLM reranker was never reached"
    assert [h.text for h in out] == [h.text for h in reversed(candidates)][:3]
    assert adaptive.name == "llm-listwise", "the span would name a backend that did not run"

    # Latched: the dead route is not retried on every later search.
    await adaptive.rank(query="q", hits=candidates, top_k=3)
    assert _Native.calls == 1, "the cross-encoder was probed again after it failed"
    assert _Fallback.calls == 2


# ---------------------------------------------------------------------------
# An unintelligible reply is not an abstention
# ---------------------------------------------------------------------------
#
# `parse_scores` returned `[]` for BOTH "the model returned a well-formed empty
# list" and "the model returned something we could not read", and
# `apply_ranking` treats `[]` as a confident "none of these are relevant" —
# which empties the result. So a reranker that could not be understood looked
# exactly like a reranker exercising perfect judgement.
#
# Measured, not hypothetical: with `gemma-4-e2b` bound to Capability.RERANK,
# this took the 45-question eval from nDCG@10 0.970 to 0.133 and recall@20 from
# 1.00 to 0.13. Retrieval was not returning worse answers — it was returning
# nothing, and reporting a high abstention rate while doing it.


def test_an_empty_relevant_list_is_a_real_abstention() -> None:
    judgement = parse_judgement({"relevant": []}, 5)
    assert judgement.scores == []
    assert judgement.malformed is None, (
        "a well-formed empty list is the abstention signal and must survive"
    )


def test_a_missing_relevant_key_is_malformed_not_an_abstention() -> None:
    judgement = parse_judgement({"results": [{"id": 1, "score": 3}]}, 5)
    assert judgement.scores == []
    assert judgement.malformed is not None
    assert "relevant" in judgement.malformed
    assert "results" in judgement.malformed, "the reason should name what WAS there"


def test_a_list_whose_entries_are_all_unusable_is_malformed() -> None:
    """The model answered with something; none of it could be used.

    That is a broken reply, not a judgement — and it is the common shape from a
    small model, which emits `{"relevant": [{"index": 3, "relevance": 0.9}]}`
    or ids that name no candidate.
    """
    judgement = parse_judgement({"relevant": [{"index": 3, "relevance": 0.9}]}, 5)
    assert judgement.scores == []
    assert judgement.malformed is not None
    assert "1 entries" in judgement.malformed

    out_of_range = parse_judgement({"relevant": [{"id": 99, "score": 3}]}, 5)
    assert out_of_range.scores == []
    assert out_of_range.malformed is not None


def test_a_partially_usable_list_is_not_malformed() -> None:
    """One good entry among rubbish is a judgement, and the rubbish is dropped."""
    judgement = parse_judgement(
        {"relevant": [{"id": 1, "score": 3}, {"id": 99, "score": 3}, "nonsense"]}, 5
    )
    assert judgement.scores == [(1, 3.0)]
    assert judgement.malformed is None


@pytest.mark.asyncio
async def test_an_unreadable_reply_keeps_fused_order_rather_than_emptying() -> None:
    """The end-to-end consequence, through the real reranker."""
    reranker, _fake = llm_reranker('{"results": [{"index": 3, "relevance": 0.9}]}')
    candidates = hits(10)
    out = await reranker.rank(query="q", hits=candidates, top_k=5)
    assert [h.text for h in out] == [h.text for h in candidates[:5]], (
        "an unreadable reranker reply emptied the search instead of leaving fusion order alone"
    )


@pytest.mark.asyncio
async def test_a_genuine_abstention_still_empties_the_result() -> None:
    """The fix must not remove the abstention signal it is protecting.

    Without this, treating everything as malformed would pass every test above
    and quietly delete the only thing in Aleph that can tell an answerable
    question from an unanswerable one.
    """
    reranker, _fake = llm_reranker('{"relevant": []}')
    out = await reranker.rank(query="q", hits=hits(10), top_k=5)
    assert out == [], "a well-formed empty judgement must still abstain"
