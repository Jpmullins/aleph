"""normalize_job: Source → NormalizedDocument.

1. Fetch SourceVersion + asset bytes; verify sha256.
2. Pick normalizer by mime_type.
3. Produce markdown + structure + quality_flags.
4. Store markdown via the AssetStore; insert NormalizedDocument row.
5. Update Source.status="normalized"; set version.normalized_document_id.
6. Enqueue chunk_embed_job + wiki_ingest_job.
7. Ledger normalization.completed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
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


async def _set_run_status(
    maker: Any,
    agent_run_id: UUID | None,
    *,
    status: str,
    result_payload: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    """Update the AgentRun row's lifecycle state in its own short transaction.

    Kept out of the main session so a normalization-success commit can't be
    rolled back by a later AgentRun-update failure (and vice versa).
    """
    if agent_run_id is None:
        return
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        now = utcnow()
        if status == "running":
            run.status = "running"
            run.started_at = now
        elif status == "succeeded":
            run.status = "succeeded"
            run.completed_at = now
            if result_payload is not None:
                run.result_payload = result_payload
        elif status == "failed":
            run.status = "failed"
            run.completed_at = now
            if error_text is not None:
                run.error_text = error_text[:4096]
        await session.commit()


#: How many times a missing row is treated as "not visible yet" before it is
#: treated as "genuinely absent".
_MISSING_ROW_RETRIES = 3


def _missing_row(ctx: dict[str, Any], what: str) -> Exception:
    """A row the job cannot find: retry a few times, then fail for real.

    The API now commits before enqueueing, so this should not fire. It is kept
    because the failure it guards is silent and expensive: a plain exception
    here is TERMINAL in arq — no retry — so a source that lost the race sat in
    `ingested` forever with nothing recorded to say why. Deferring a few times
    lets a transient visibility gap heal itself, while a row that truly does not
    exist still fails loudly rather than retrying forever.
    """
    from arq.worker import Retry

    attempt = int(ctx.get("job_try", 1) or 1)
    if attempt <= _MISSING_ROW_RETRIES:
        return Retry(defer=2**attempt)
    return RuntimeError(f"{what} (after {attempt} attempts)")


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

    await _set_run_status(maker, claims.agent_run_id, status="running")

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
                await session.execute(select(SourceVersion).where(SourceVersion.id == version_id))
            ).scalar_one_or_none()
            if version is None:
                raise _missing_row(ctx, f"source version {version_id} not found")
            source = (
                await session.execute(select(Source).where(Source.id == version.source_id))
            ).scalar_one_or_none()
            if source is None:
                raise _missing_row(ctx, f"source {version.source_id} not found")
            asset = (
                await session.execute(select(SourceAsset).where(SourceAsset.id == version.asset_id))
            ).scalar_one_or_none()
            if asset is None:
                raise _missing_row(ctx, f"source asset {version.asset_id} not found")

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
                await _set_run_status(
                    maker, claims.agent_run_id, status="failed", error_text=str(exc)
                )
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
        await redis_pool.enqueue_job("chunk_embed_job", str(normalized_id), agent_token)
        await redis_pool.enqueue_job("wiki_ingest_job", str(normalized_id), agent_token)

    result_payload = {"normalized_document_id": str(normalized_id)}
    await _set_run_status(
        maker, claims.agent_run_id, status="succeeded", result_payload=result_payload
    )
    return {"ok": True, "normalized_document_id": str(normalized_id)}
