"""Sources API: upload, list, detail, signed asset URL, reingest, delete.

Upload kicks off the normalize_job in the Arq queue. A short-lived agent
token is minted in-process (matching the /v1/agent-tokens path) and used
by the worker to call back into the API via the agent's authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, File, Form, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import current_trace_id
from aleph_rks.models import (
    NormalizedDocument,
    Source,
    SourceAsset,
    SourceVersion,
)
from aleph_rks.source_service import register_uploaded_source
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_rks.source_service import SourceCreated
    from aleph_security.principal import Principal

router = APIRouter(prefix="/v1/projects", tags=["sources"])


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    title: str
    short_id: str
    connector_kind: str
    url: str | None
    status: str
    failure_reason: str | None
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


@router.post(
    "/{project_id}/sources/upload", status_code=status.HTTP_201_CREATED, response_model=SourceOut
)
async def upload_source(
    request: Request,
    project_id: ProjectScopeDep,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    principal: PrincipalDep = None,  # type: ignore[assignment]
    session: SessionDep = None,  # type: ignore[assignment]
    ledger: LedgerDep = None,  # type: ignore[assignment]
) -> SourceOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    if request.app.state.asset_store is None:
        msg = "asset store is not configured"
        raise ValidationFailed(msg)

    data = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"

    created = await register_uploaded_source(
        session,
        ledger=ledger,
        principal=principal,
        asset_store=request.app.state.asset_store,
        project_id=project_id,
        title=title or filename,
        data=data,
        filename=filename,
        mime_type=mime_type,
    )

    await _kick_off_normalize(
        request,
        session=session,
        project_id=project_id,
        principal=principal,
        ledger=ledger,
        created=created,
    )

    # Refresh so Pydantic doesn't trigger a lazy reload outside the
    # async greenlet context (server_default-bumped updated_at).
    await session.refresh(created.source)
    return SourceOut.model_validate(created.source)


async def _kick_off_normalize(
    request: Request,
    *,
    session: AsyncSession,
    project_id: UUID,
    principal: Principal,
    ledger: LedgerWriter,
    created: SourceCreated,
) -> None:
    """Mint an agent token, ledger an AgentRun, and enqueue `normalize_job`.

    Shared by the upload and ingest-url routes so both kick the normalize
    pipeline off the same way. On enqueue failure the source is marked
    `failed` so the UI surfaces it (the row is committed by the caller).
    """
    # Mint an agent token + create an AgentRun so the worker can authenticate.
    agent_run_id = uuid7()
    # Use the full agent_run_id hex (not a truncated source-id prefix): two
    # uuid7s minted within the same ~17s window share their first 8 hex digits
    # (the high bits of the 48-bit ms timestamp), which collides on the unique
    # `uq_agent_runs_correlation_id` constraint and 500s. correlation_id is an
    # opaque label minted into the agent token and echoed back by the worker —
    # nothing parses it from the source id, so the full id is a safe label.
    correlation_id = f"normalize-{agent_run_id.hex}"
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind="normalizer",
        correlation_id=correlation_id,
        status="pending",
        input_payload={
            "source_id": str(created.source.id),
            "source_version_id": str(created.version.id),
        },
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(run)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="agent_run.create",
        target_id=agent_run_id,
        target_kind="agent_run",
        payload={
            "agent_kind": "normalizer",
            "correlation_id": correlation_id,
            "source_id": str(created.source.id),
        },
        trace_id=current_trace_id(),
    )
    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )

    # Enqueue normalize job via the shared Redis (arq.create_pool API works
    # through the same redis URL the app uses).
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
        try:
            await pool.enqueue_job("normalize_job", str(created.version.id), token)
        finally:
            await pool.aclose()
    except Exception as exc:  # noqa: BLE001
        # Job enqueue failed — mark source as failed so the UI surfaces it.
        created.source.status = "failed"
        created.source.failure_reason = f"failed to enqueue normalize job: {exc}"[:2048]


class IngestUrlIn(BaseModel):
    url: HttpUrl
    title: str = Field("", max_length=512)


class IngestUrlOut(BaseModel):
    source_id: str
    status: str


@router.post(
    "/{project_id}/sources/ingest-url",
    status_code=status.HTTP_201_CREATED,
    response_model=IngestUrlOut,
)
async def ingest_url(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[IngestUrlIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    """Fetch a remote URL server-side and register it as a Source.

    Thin counterpart to the file-upload route: it pulls the bytes, then runs
    the identical `register_uploaded_source` + normalize-enqueue path. The
    `ingest_source` agent tool self-calls this so the agent never touches the
    DB or asset store directly (architecture rule #3).

    SSRF note: this fetches an arbitrary, caller-supplied URL from the server.
    That is intentional and consistent with the connectors, which already
    fetch remote URLs; this is a local research tool, not a public service.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    if request.app.state.asset_store is None:
        msg = "asset store is not configured"
        raise ValidationFailed(msg)

    url = str(body.url)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            resp = await c.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        msg = f"could not fetch {url}: {exc}"
        raise ValidationFailed(msg) from exc

    mime_type = resp.headers.get("content-type", "text/html").split(";")[0].strip()
    _path = urlparse(url).path
    filename = _path.rstrip("/").rsplit("/", 1)[-1] or "page.html"

    created = await register_uploaded_source(
        session,
        ledger=ledger,
        principal=principal,
        asset_store=request.app.state.asset_store,
        project_id=project_id,
        title=body.title or filename,
        data=resp.content,
        filename=filename,
        mime_type=mime_type,
    )

    await _kick_off_normalize(
        request,
        session=session,
        project_id=project_id,
        principal=principal,
        ledger=ledger,
        created=created,
    )

    await session.refresh(created.source)
    return {"source_id": str(created.source.id), "status": created.source.status}


@router.get("/{project_id}/sources", response_model=list[SourceOut])
async def list_sources(
    project_id: ProjectScopeDep,
    session: SessionDep,
    status_filter: Annotated[str | None, None] = None,
) -> list[SourceOut]:
    stmt = select(Source).where(Source.project_id == project_id).order_by(Source.created_at.desc())
    if status_filter:
        stmt = stmt.where(Source.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [SourceOut.model_validate(r) for r in rows]


@router.get("/{project_id}/sources/{source_id}", response_model=SourceOut)
async def get_source(
    project_id: ProjectScopeDep, source_id: UUID, session: SessionDep
) -> SourceOut:
    s = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if s is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)
    return SourceOut.model_validate(s)


class AssetURLOut(BaseModel):
    url: str
    ttl_seconds: int


@router.get("/{project_id}/sources/{source_id}/asset", response_model=AssetURLOut)
async def get_source_asset(
    request: Request,
    project_id: ProjectScopeDep,
    source_id: UUID,
    session: SessionDep,
) -> AssetURLOut:
    src = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None or src.current_version_id is None:
        msg = "source asset not available"
        raise NotFound(msg)
    version = (
        await session.execute(
            select(SourceVersion).where(SourceVersion.id == src.current_version_id)
        )
    ).scalar_one_or_none()
    if version is None:
        msg = "source version missing"
        raise NotFound(msg)
    asset = (
        await session.execute(select(SourceAsset).where(SourceAsset.id == version.asset_id))
    ).scalar_one_or_none()
    if asset is None:
        msg = "source asset row missing"
        raise NotFound(msg)
    store = request.app.state.asset_store
    if store is None:
        msg = "asset store not configured"
        raise ValidationFailed(msg)
    url = store.presigned_get_url(asset.storage_uri)
    return AssetURLOut(url=url, ttl_seconds=600)


class NormalizedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    parser: str
    parser_version: str
    char_count: int
    token_count: int
    quality_flags: list[Any]
    markdown: str


@router.get("/{project_id}/sources/{source_id}/normalized", response_model=NormalizedOut)
async def get_normalized(
    request: Request,
    project_id: ProjectScopeDep,
    source_id: UUID,
    session: SessionDep,
) -> NormalizedOut:
    src = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)
    stmt = (
        select(NormalizedDocument)
        .where(NormalizedDocument.source_id == source_id)
        .order_by(NormalizedDocument.created_at.desc())
        .limit(1)
    )
    nd = (await session.execute(stmt)).scalar_one_or_none()
    if nd is None:
        msg = "normalized document not yet available"
        raise NotFound(msg)
    store = request.app.state.asset_store
    if store is None:
        msg = "asset store not configured"
        raise ValidationFailed(msg)
    md = store.get(nd.markdown_uri).decode("utf-8")
    return NormalizedOut(
        id=nd.id,
        source_id=nd.source_id,
        parser=nd.parser,
        parser_version=nd.parser_version,
        char_count=nd.char_count,
        token_count=nd.token_count,
        quality_flags=nd.quality_flags_jsonb,
        markdown=md,
    )
