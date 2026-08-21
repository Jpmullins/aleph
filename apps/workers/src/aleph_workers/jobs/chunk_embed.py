"""chunk_embed_job: NormalizedDocument → DocumentChunk rows.

1. Load markdown (verify nothing; the put_normalized writer is trusted source).
2. Chunk per aleph_rks.chunking algorithm.
3. Batch-embed via LiteLLMClient.embed.
4. Insert DocumentChunk rows.
5. Upsert RetrievalIndexRecord.
6. Update Source.status="indexed".
7. Ledger chunks.created, embeddings.completed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.profile import resolve_binding
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.chunking import chunk_markdown
from aleph_rks.embedding import embed_texts, embedding_dim_mismatch, is_known_embedding_model
from aleph_rks.models import (
    EMBEDDING_DIM,
    DocumentChunk,
    NormalizedDocument,
    RetrievalIndexRecord,
    Source,
)
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def chunk_embed_job(
    ctx: dict[str, Any], normalized_id_str: str, agent_token: str
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )
    normalized_id = UUID(normalized_id_str)

    maker = ctx["session_maker"]
    asset_store = ctx["asset_store"]
    litellm = ctx["litellm_client"]

    # AgentRun lifecycle so progress is visible to the UI Activity card.
    # Unique correlation_id per worker run (uq_agent_runs_correlation_id).
    chunk_run_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=chunk_run_id,
                project_id=claims.project_id,
                agent_kind="chunk_embed",
                correlation_id=f"chunk-{chunk_run_id.hex}",
                status="running",
                started_at=utcnow(),
                input_payload={"normalized_document_id": str(normalized_id)},
                created_by=principal.user_id,
            )
        )
        await session.commit()

    async def _finalize(
        status: str, result_payload: dict[str, Any] | None = None, error_text: str | None = None
    ) -> None:
        async with maker() as session:
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == chunk_run_id))
            ).scalar_one_or_none()
            if run is None:
                return
            run.status = status
            run.completed_at = utcnow()
            if result_payload is not None:
                run.result_payload = result_payload
            if error_text is not None:
                run.error_text = error_text[:4096]
            await session.commit()

    with start_span(
        "worker.chunk_embed",
        **{
            "aleph.normalized_document_id": str(normalized_id),
            "aleph.project_id": str(claims.project_id),
        },
    ):
        async with maker() as session:
            ledger = LedgerWriter(session)
            normalized = (
                await session.execute(
                    select(NormalizedDocument).where(NormalizedDocument.id == normalized_id)
                )
            ).scalar_one_or_none()
            if normalized is None:
                msg = f"normalized document {normalized_id} not found"
                raise RuntimeError(msg)
            source = (
                await session.execute(select(Source).where(Source.id == normalized.source_id))
            ).scalar_one_or_none()
            if source is None:
                msg = f"source {normalized.source_id} not found"
                raise RuntimeError(msg)
            profile = (
                await session.execute(
                    select(ModelProfile).where(ModelProfile.project_id == normalized.project_id)
                )
            ).scalar_one_or_none()
            if profile is None:
                msg = "project has no model profile"
                raise RuntimeError(msg)

        # Embedding-dimension guard: resolve the project's `embedding` binding and
        # reject BEFORE any (billed) embed call if the model's known output
        # dimension does not match the pgvector column width. Writing a wrong-dim
        # vector would fail the flush anyway; paying to discover that is the bug.
        embed_model = resolve_binding(profile.bindings_jsonb, "embedding").model

        async def _reject_dim(model: str, got_dim: int) -> dict[str, Any]:
            reason = (
                f"embedding model '{model}' emits {got_dim}-dim vectors, "
                f"but document_chunks.embedding is {EMBEDDING_DIM}-dim; "
                "rejected before embedding the document (no batch spend)"
            )
            async with maker() as session:
                src = (
                    await session.execute(select(Source).where(Source.id == normalized.source_id))
                ).scalar_one_or_none()
                if src is not None:
                    src.status = "failed"
                await session.commit()
            await _finalize("failed", error_text=reason)
            return {"ok": False, "reason": reason}

        # Known-model fast path: reject on metadata alone, zero spend.
        mismatch_dim = embedding_dim_mismatch(embed_model)
        if mismatch_dim is not None:
            return await _reject_dim(embed_model, mismatch_dim)

        # Fetch + chunk markdown.
        markdown_bytes = asset_store.get(normalized.markdown_uri)
        markdown = markdown_bytes.decode("utf-8")
        chunks = chunk_markdown(markdown)
        if not chunks:
            await _finalize("succeeded", {"chunk_count": 0})
            return {"ok": True, "chunk_count": 0}

        # Unknown-model path: a single-item probe learns the real dim before the
        # full batch, so a wrong-dim unknown embedder wastes one token, not the
        # whole document. (Known models skipped this — their dim is in the
        # static registry.)
        if not is_known_embedding_model(embed_model):
            probe = await embed_texts(
                client=litellm,
                principal=principal,
                project_id=normalized.project_id,
                agent_run_id=claims.agent_run_id,
                profile_bindings=profile.bindings_jsonb,
                texts=["dimension probe"],
                purpose="rks.embed.probe",
            )
            probe_dim = len(probe.embeddings[0]) if probe.embeddings else EMBEDDING_DIM
            if probe_dim != EMBEDDING_DIM:
                return await _reject_dim(embed_model, probe_dim)

        # Embed (this writes a ModelCall + CostLedgerEvent per batch).
        embed_result = await embed_texts(
            client=litellm,
            principal=principal,
            project_id=normalized.project_id,
            agent_run_id=claims.agent_run_id,
            profile_bindings=profile.bindings_jsonb,
            texts=[c.text for c in chunks],
            purpose="rks.embed",
        )

        # Insert chunks. text_tsv is filled by the trigger on document_chunks.
        async with maker() as session:
            ledger = LedgerWriter(session)
            for c, embedding in zip(chunks, embed_result.embeddings, strict=False):
                session.add(
                    DocumentChunk(
                        id=uuid7(),
                        project_id=normalized.project_id,
                        source_id=normalized.source_id,
                        normalized_document_id=normalized.id,
                        ordinal=c.ordinal,
                        text=c.text,
                        text_tsv="",  # trigger fills this
                        embedding=embedding,
                        section_path=c.section_path,
                        char_start=c.char_start,
                        char_end=c.char_end,
                        token_count=c.token_count,
                        embedder_model=embed_result.model,
                    )
                )

            # Upsert RetrievalIndexRecord.
            existing = (
                await session.execute(
                    select(RetrievalIndexRecord).where(
                        RetrievalIndexRecord.source_id == normalized.source_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    RetrievalIndexRecord(
                        id=uuid7(),
                        project_id=normalized.project_id,
                        source_id=normalized.source_id,
                        embedder_model=embed_result.model,
                        chunk_count=len(chunks),
                        indexed_at=utcnow(),
                        created_by=principal.user_id,
                    )
                )
            else:
                existing.embedder_model = embed_result.model
                existing.chunk_count = len(chunks)
                existing.indexed_at = utcnow()

            # Mark Source.status.
            src = (
                await session.execute(select(Source).where(Source.id == normalized.source_id))
            ).scalar_one_or_none()
            if src is not None:
                src.status = "indexed"

            await ledger.append(
                project_id=normalized.project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="embeddings.completed",
                target_id=normalized.id,
                target_kind="normalized_document",
                payload={
                    "source_id": str(normalized.source_id),
                    "chunk_count": len(chunks),
                    "embedder_model": embed_result.model,
                },
                trace_id=current_trace_id(),
            )
            await session.commit()

    await _finalize(
        "succeeded",
        {"chunk_count": len(chunks), "embedder": embed_result.model},
    )
    return {"ok": True, "chunk_count": len(chunks), "embedder": embed_result.model}
