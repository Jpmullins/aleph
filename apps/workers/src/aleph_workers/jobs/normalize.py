"""normalize_job: Source → NormalizedDocument.

  1. Fetch SourceVersion + asset bytes; verify sha256.
  2. Pick normalizer by mime_type.
  3. Produce markdown + structure + quality_flags.
  4. Store markdown to MinIO; insert NormalizedDocument row.
  5. Update Source.status="normalized"; set version.normalized_document_id.
  6. Enqueue chunk_embed_job + wiki_ingest_job.
  7. Ledger normalization.completed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.models import (
    NormalizedDocument,
    Source,
    SourceAsset,
    SourceVersion,
)
from aleph_rks.normalization import NormalizationFailed, normalize_bytes
from aleph_rks.source_service import mark_status
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def normalize_job(
    ctx: dict[str, Any], normalize_input_str: str, agent_token: str
) -> dict[str, Any]:
    """`normalize_input_str` is the SourceVersion UUID as a string."""
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
    version_id = UUID(normalize_input_str)

    maker = ctx["session_maker"]
    asset_store = ctx["asset_store"]

    with start_span(
        "worker.normalize",
        **{
            "aleph.source_version_id": str(version_id),
            "aleph.project_id": str(claims.project_id),
        },
    ):
        async with maker() as session:
            ledger = LedgerWriter(session)
            version = (
                await session.execute(
                    select(SourceVersion).where(SourceVersion.id == version_id)
                )
            ).scalar_one_or_none()
            if version is None:
                msg = f"source version {version_id} not found"
                raise RuntimeError(msg)
            source = (
                await session.execute(
                    select(Source).where(Source.id == version.source_id)
                )
            ).scalar_one_or_none()
            if source is None:
                msg = f"source {version.source_id} not found"
                raise RuntimeError(msg)
            asset = (
                await session.execute(
                    select(SourceAsset).where(SourceAsset.id == version.asset_id)
                )
            ).scalar_one_or_none()
            if asset is None:
                msg = f"source asset {version.asset_id} not found"
                raise RuntimeError(msg)

            try:
                data = asset_store.get(asset.storage_uri, expected_sha256=asset.sha256)
                result = normalize_bytes(data, asset.mime_type)
            except NormalizationFailed as exc:
                await mark_status(
                    session,
                    ledger=ledger,
                    principal=principal,
                    project_id=source.project_id,
                    source_id=source.id,
                    status="failed",
                    failure_reason=str(exc),
                )
                await session.commit()
                return {"ok": False, "error": str(exc)}

            md_uri = asset_store.put_normalized_markdown(
                project_id=source.project_id,
                source_id=source.id,
                version_no=version.version_no,
                markdown=result.markdown,
            )
            normalized = NormalizedDocument(
                id=uuid7(),
                project_id=source.project_id,
                source_id=source.id,
                source_version_id=version.id,
                markdown_uri=md_uri,
                parser=result.parser,
                parser_version=result.parser_version,
                char_count=result.char_count,
                token_count=result.token_count,
                structure_jsonb=result.structure,
                quality_flags_jsonb=result.quality_flags,
                created_by=principal.user_id,
                access_scope="project",
            )
            session.add(normalized)
            await session.flush()

            version.normalized_document_id = normalized.id
            version.parser_version = result.parser_version
            source.status = "normalized"

            await ledger.append(
                project_id=source.project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="normalization.completed",
                target_id=normalized.id,
                target_kind="normalized_document",
                payload={
                    "source_id": str(source.id),
                    "parser": result.parser,
                    "parser_version": result.parser_version,
                    "char_count": result.char_count,
                    "token_count": result.token_count,
                    "quality_flags": result.quality_flags,
                },
                trace_id=current_trace_id(),
            )
            await session.commit()
            normalized_id = normalized.id

    # Enqueue downstream jobs.
    redis_pool = ctx.get("redis_pool")
    if redis_pool is not None:
        await redis_pool.enqueue_job(
            "chunk_embed_job", str(normalized_id), agent_token
        )
        await redis_pool.enqueue_job(
            "wiki_ingest_job", str(normalized_id), agent_token
        )
    return {"ok": True, "normalized_document_id": str(normalized_id)}
