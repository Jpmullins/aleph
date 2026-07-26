"""Sources API: upload, ingest-url, list, detail, normalized text.

Raw asset bytes are served by the streaming route in `routes/assets.py`.

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
from sqlalchemy import func, select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.retraction import retract_source
from aleph_rks.models import Connector, NormalizedDocument, Source
from aleph_rks.source_service import register_uploaded_source
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

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
    except Exception as exc:
        # Job enqueue failed — mark source as failed so the UI surfaces it.
        created.source.status = "failed"
        created.source.failure_reason = f"failed to enqueue normalize job: {exc}"[:2048]


class IngestUrlIn(BaseModel):
    url: HttpUrl
    title: str = Field("", max_length=512)
    # WP-2 §5 — scholarly provenance passthrough. `connector_kind` must name a
    # seeded connector (e.g. openalex | crossref | consensus | arxiv); unknown
    # kinds are a 422 so provenance names stay real. `source_metadata` is merged
    # onto the Source row's source_metadata_jsonb (doi, openalex_id, doi_verdict).
    connector_kind: str | None = Field(None, max_length=64)
    source_metadata: dict[str, Any] | None = None


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

    # Validate the declared provenance BEFORE fetching anything: an unknown
    # connector_kind is a caller bug (422), not a reason to hit the network.
    if body.connector_kind is not None:
        known = (
            await session.execute(select(Connector).where(Connector.kind == body.connector_kind))
        ).scalar_one_or_none()
        if known is None:
            msg = f"unknown connector_kind: {body.connector_kind}"
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
        connector_kind=body.connector_kind or "upload",
        source_metadata=body.source_metadata,
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


class RetractIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2048)


class RetractOut(BaseModel):
    source_id: UUID
    status: str
    already_retracted: bool
    page_ids: list[UUID]
    claim_ids: list[UUID]
    finding_id: UUID | None


@router.post("/{project_id}/sources/{source_id}/retract", response_model=RetractOut)
async def retract_source_route(
    request: Request,
    project_id: ProjectScopeDep,
    source_id: UUID,
    body: Annotated[RetractIn, Body()],
    principal: PrincipalDep,
    session: SessionDep,
    ledger: LedgerDep,
) -> RetractOut:
    """Retract a source and flag every dependent wiki claim (WP-6 §4, EDITOR).

    Funnels through the shared ``retract_source`` service: sets the source
    ``retracted`` + ledger event, walks the blast-radius join flagging dependent
    claims, and emits a critical ``retracted_source`` finding into Briefs. Then
    enqueues a best-effort curator freshness recompute for each affected page.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    src = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)

    result = await retract_source(
        session, ledger, principal, source_id=source_id, reason=body.reason
    )

    # Best-effort: enqueue a curator freshness recompute for each affected page.
    if result.page_ids:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
            try:
                for page_id in result.page_ids:
                    await pool.enqueue_job("curate_page_job", str(project_id), str(page_id))
            finally:
                await pool.aclose()
        except Exception:
            # Recompute is best-effort; a queue hiccup never fails the retract.
            pass

    return RetractOut(
        source_id=result.source_id,
        status="retracted",
        already_retracted=result.already_retracted,
        page_ids=sorted(result.page_ids),
        claim_ids=sorted(result.claim_ids),
        finding_id=result.finding_id,
    )


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
    md = request.app.state.asset_store.get(nd.markdown_uri).decode("utf-8")
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


#: The corpus pipeline, in the order a source actually moves through it. Each
#: stage is *cumulative*: a source that reached `indexed` has necessarily been
#: normalized, so a stage's count includes everything downstream of it. Showing
#: non-cumulative counts made the strip read as though work had been lost —
#: sources "disappeared" from `normalized` as they advanced.
_PIPELINE_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ingested", "Ingested", ("ingested", "normalized", "indexed", "wiki_done")),
    ("normalized", "Normalized", ("normalized", "indexed", "wiki_done")),
    ("indexed", "Chunked + embedded", ("indexed", "wiki_done")),
    ("wiki_done", "On the wiki", ("wiki_done",)),
)

#: Terminal failures. Counted separately and never folded into a stage: a
#: failed source that silently vanished from the strip is the corpus-level
#: version of the empty-path failure this codebase keeps finding.
_PIPELINE_FAILED: tuple[str, ...] = ("failed", "wiki_failed")


class PipelineStageOut(BaseModel):
    key: str
    label: str
    count: int


class PipelineOut(BaseModel):
    """Corpus-level progress: how far the whole source set has actually got."""

    stages: list[PipelineStageOut]
    failed: int
    total: int


@router.get("/{project_id}/pipeline", response_model=PipelineOut)
async def get_pipeline(project_id: ProjectScopeDep, session: SessionDep) -> PipelineOut:
    """Counts per ingest stage for the project's whole corpus.

    The gap this closes: a source's journey (fetch → normalize → chunk+embed →
    wiki) ran entirely in workers with no corpus-level view, so "is my library
    ready to ask questions of?" was unanswerable without reading logs. A run
    that stalled after normalization looked exactly like one that had finished.
    """
    rows = (
        await session.execute(
            select(Source.status, func.count())
            .where(Source.project_id == project_id)
            .group_by(Source.status)
        )
    ).all()
    counts = {str(status): int(n) for status, n in rows}
    return PipelineOut(
        stages=[
            PipelineStageOut(
                key=key,
                label=label,
                count=sum(counts.get(s, 0) for s in members),
            )
            for key, label, members in _PIPELINE_STAGES
        ],
        failed=sum(counts.get(s, 0) for s in _PIPELINE_FAILED),
        total=sum(counts.values()),
    )
