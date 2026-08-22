"""Repair passes for an index that was never built, or was built badly.

Two failures produced the same symptom on the deployed stack — an empty
``document_chunks`` — and they need different repairs:

* **Never chunked.** A normalized document exists and no chunk row does, because
  the job that would have written them died on an embed call it made too early.
  :func:`backfill_unindexed_for_project` finds those and indexes them.
* **Chunked but not embedded.** Chunks exist with ``embedding IS NULL`` because
  the embedder was unbound or down. That is repaired by
  ``aleph_rks.retrieval.reembed_for_project``, which now selects a NULL
  ``embedder_model`` as stale for exactly this reason.

Both are idempotent, and the idempotence is the contract: a repair you cannot
run twice is a repair nobody runs at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import exists, select

from aleph_rks.indexing import AssetReader, index_normalized_document
from aleph_rks.models import DocumentChunk, NormalizedDocument

if TYPE_CHECKING:
    from collections.abc import Callable

    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)


async def unindexed_document_ids(session: Any, *, project_id: UUID) -> list[UUID]:
    """Normalized documents in this project with no chunk rows at all."""
    has_chunk = exists().where(DocumentChunk.normalized_document_id == NormalizedDocument.id)
    rows = await session.execute(
        select(NormalizedDocument.id)
        .where(NormalizedDocument.project_id == project_id, ~has_chunk)
        .order_by(NormalizedDocument.id)
    )
    return [r[0] for r in rows.all()]


async def backfill_unindexed_for_project(
    *,
    maker: Callable[[], Any],
    project_id: UUID,
    asset_store: AssetReader,
    litellm: LiteLLMClient,
    principal: Principal,
    profile_bindings: dict[str, Any],
    agent_run_id: UUID | None = None,
    purpose: str = "rks.backfill",
) -> tuple[int, int]:
    """Index every normalized document in the project that has no chunks.

    Returns ``(documents_indexed, chunks_written)``; a document that cannot be
    indexed at all is counted in neither and logged by name. Running it a second time
    with nothing new to do returns ``(0, 0)`` — the selection is "has no chunk
    rows", so a document that came out ``lexical_only`` (chunks written, dense
    leg missing) is *done* as far as this pass is concerned and is not
    re-attempted. Repairing the dense leg is ``reembed_for_project``'s job, and
    keeping them separate is what makes each one safe to re-run.
    """
    async with maker() as session:
        pending = await unindexed_document_ids(session, project_id=project_id)

    documents = 0
    chunks = 0
    failed = 0
    for normalized_id in pending:
        # One unindexable document must not take the batch with it. A single PDF
        # carrying a NUL byte aborted a 34-document repair pass at whatever
        # position it happened to occupy, and the documents behind it looked
        # exactly like documents that had never been asked for.
        try:
            outcome = await index_normalized_document(
                maker=maker,
                normalized_id=normalized_id,
                asset_store=asset_store,
                litellm=litellm,
                principal=principal,
                profile_bindings=profile_bindings,
                agent_run_id=agent_run_id,
                purpose=purpose,
            )
        except Exception as exc:
            failed += 1
            _log.warning(
                "rks.backfill.document_failed",
                normalized_document_id=str(normalized_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        documents += 1
        chunks += outcome.chunk_count
        if not outcome.ok:
            _log.warning(
                "rks.backfill.degraded",
                normalized_document_id=str(normalized_id),
                state=outcome.state,
                reason=outcome.reason,
            )
    if documents or failed:
        _log.info(
            "rks.backfill.done",
            project_id=str(project_id),
            documents=documents,
            chunks=chunks,
            failed=failed,
        )
    return documents, chunks
