"""Batched embedding for chunks via the LiteLLM gateway.

We batch at 64 inputs per call (headroom under typical provider batch limits).
The embedding model is whatever the project's `ModelProfile.embedding` binding
resolves to (the default profiles use `titan-embed-v2`, 1024-dim — matching the
`Vector(1024)` chunk column). Every call routes through the same `LiteLLMClient`
the rest of Aleph uses, so cost is ledgered and OTEL spans are emitted.

If `ModelProfile.embedding` for a project changes, the resulting chunks would
have a different `embedder_model`. The re-embed worker
(`aleph_rks.retrieval.reembed_for_project`, enqueued by the model-profile
switch/update routes on embed-model change) detects the mismatch via
`RetrievalIndexRecord` and re-embeds the source's chunks.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from aleph_rks.models import EMBEDDING_DIM

if TYPE_CHECKING:
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


#: Known output dimensions for embedding models we may resolve from a project's
#: ``embedding`` binding. Used to reject a dimension-mismatched embedder *before*
#: any billed embed call (the store column is fixed at ``EMBEDDING_DIM``). Models
#: absent here are unknown-dimension and fall through to the normal embed path.
KNOWN_EMBEDDING_DIMS: dict[str, int] = {
    "titan-embed-v2": 1024,
    "amazon.titan-embed-text-v2:0": 1024,
    "amazon.titan-embed-text-v1": 1536,
    "cohere.embed-english-v3": 1024,
    "cohere.embed-multilingual-v3": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def embedding_dim_mismatch(model: str, *, column_dim: int = EMBEDDING_DIM) -> int | None:
    """Return the model's known output dimension iff it is known *and* differs
    from ``column_dim`` (a mismatch that must be rejected before billing).

    Returns ``None`` when the model matches the column width or when its
    dimension is unknown (nothing to reject on the metadata alone).
    """
    known = KNOWN_EMBEDDING_DIMS.get(model)
    if known is not None and known != column_dim:
        return known
    return None


def is_known_embedding_model(model: str) -> bool:
    """True iff the model's output dim is in the static registry.

    When False, callers that must reject-before-paying probe the model with a
    single trivial input to learn its dim (see `chunk_embed`), so an unknown
    embedder with a wrong dim wastes one token, not a full document's batch.
    """
    return model in KNOWN_EMBEDDING_DIMS


@dataclass(frozen=True)
class EmbedBatchResult:
    embeddings: list[list[float]]
    model: str
    input_tokens: int
    cost_usd: str


async def embed_texts(
    *,
    client: LiteLLMClient,
    principal: Principal,
    project_id: UUID,
    agent_run_id: UUID | None,
    profile_bindings: dict,
    texts: Iterable[str],
    purpose: str = "rks.embed",
    batch_size: int = 64,
) -> EmbedBatchResult:
    """Embed an iterable of texts. Single resulting embedding list, ordered to match input."""
    items = list(texts)
    if not items:
        return EmbedBatchResult(embeddings=[], model="", input_tokens=0, cost_usd="0")

    all_embeddings: list[list[float]] = []
    total_tokens = 0
    total_cost = 0.0
    model = ""

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        resp = await client.embed(
            principal=principal,
            project_id=project_id,
            agent_run_id=agent_run_id,
            profile_bindings=profile_bindings,
            input=batch,
            purpose=purpose,
        )
        all_embeddings.extend(resp.embeddings)
        total_tokens += resp.input_tokens
        with contextlib.suppress(ValueError):
            total_cost += float(resp.cost_usd)
        model = resp.model

    return EmbedBatchResult(
        embeddings=all_embeddings,
        model=model,
        input_tokens=total_tokens,
        cost_usd=str(total_cost),
    )
