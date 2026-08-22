"""`claim_embedder_for` — the batch that fills `wiki_claims.embedding`.

18,038 of 18,038 claims carried a NULL vector and the HNSW index over that
column had never had anything to index. Not because the embedder was
unreachable: because two of the three claim writers had no way to pass one.
`BeliefService.upsert_claim` takes `embed` and the page-compile path now
supplies it; this is what supplies it.

Three properties, and each one is a way this could go wrong quietly:

* it is BATCHED, so a page compile costs one gateway round trip and not one
  per claim;
* a failure costs the vectors and never the claims — a belief written without
  a vector is still findable by the lexical leg, and refusing to record it
  because a model was down trades the thing for the index of it;
* a SHORT batch is refused outright, because zipping it would attach vectors to
  the wrong claims, which is worse than attaching none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_security.principal import Principal
from aleph_wiki.belief_service import claim_embedder_for

PROJECT = uuid4()
PRINCIPAL = Principal(
    user_id=uuid4(), subject="rs10", email="rs10@example.test", actor_kind="aleph_agent"
)
BINDINGS: dict[str, Any] = {"embedding": {"model": "bedrock-titan-embed-text"}}


class _Gateway:
    """Stands in for LiteLLMClient. Records every batch it was handed."""

    def __init__(self, *, drop: int = 0, fail: bool = False) -> None:
        self.batches: list[list[str]] = []
        self._drop = drop
        self._fail = fail

    async def embed(self, **kw: Any) -> Any:
        texts = list(kw["input"])
        self.batches.append(texts)
        if self._fail:
            msg = "litellm.APIConnectionError: the embedder is down"
            raise RuntimeError(msg)
        kept = texts[: len(texts) - self._drop]
        return SimpleNamespace(
            embeddings=[[float(len(t))] * 4 for t in kept],
            model="bedrock-titan-embed-text",
            input_tokens=len(texts),
            cost_usd="0",
        )


async def _embedder(gateway: _Gateway, texts: list[str]) -> Any:
    return await claim_embedder_for(
        client=gateway,
        principal=PRINCIPAL,
        project_id=PROJECT,
        agent_run_id=None,
        profile_bindings=BINDINGS,
        texts=texts,
    )


@pytest.mark.asyncio
async def test_one_round_trip_for_a_page_of_claims() -> None:
    gateway = _Gateway()
    embed = await _embedder(gateway, ["alpha", "beta", "gamma"])
    assert len(gateway.batches) == 1, "one call per claim, not one per page"
    assert embed("alpha") == [5.0] * 4
    assert embed("gamma") == [5.0] * 4


@pytest.mark.asyncio
async def test_a_repeated_proposition_is_embedded_once() -> None:
    """Claims are deduped by `claim_key` downstream; paying twice for the same
    string here would be spend with no effect."""
    gateway = _Gateway()
    embed = await _embedder(gateway, ["alpha", "alpha", "beta"])
    assert gateway.batches == [["alpha", "beta"]]
    assert embed("alpha") == [5.0] * 4


@pytest.mark.asyncio
async def test_an_unknown_claim_gets_no_vector_rather_than_an_error() -> None:
    gateway = _Gateway()
    embed = await _embedder(gateway, ["alpha"])
    assert embed("a claim nobody asked to embed") is None


@pytest.mark.asyncio
async def test_a_dead_gateway_costs_the_vectors_and_not_the_claims() -> None:
    gateway = _Gateway(fail=True)
    embed = await _embedder(gateway, ["alpha", "beta"])
    assert embed("alpha") is None, "the failure must not propagate into the write path"


@pytest.mark.asyncio
async def test_a_short_batch_is_refused_rather_than_zipped() -> None:
    """The silent-corruption case. Two claims in, one vector back: zipping
    would give claim 1 its own vector and claim 2 nothing, or — with a
    different ordering — give claim 2 claim 1's vector, which is a belief
    indexed as a different belief."""
    gateway = _Gateway(drop=1)
    embed = await _embedder(gateway, ["alpha", "beta"])
    assert embed("alpha") is None
    assert embed("beta") is None


@pytest.mark.asyncio
async def test_no_claims_costs_no_call() -> None:
    gateway = _Gateway()
    embed = await _embedder(gateway, [])
    assert gateway.batches == []
    assert embed("anything") is None
