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

if TYPE_CHECKING:
    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal


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
