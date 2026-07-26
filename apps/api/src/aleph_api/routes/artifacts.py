"""Artifacts + RenderedAssets API.

`POST /artifacts/build` is the entry point: it creates an `Artifact`
(or reuses one), enqueues the Builder worker job, returns the
agent_run_id so the client can subscribe to progress.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_artifacts.artifact_service import (
    create_artifact,
    get_artifact,
    list_artifacts,
    list_versions,
)
from aleph_artifacts.models import RenderedAsset
from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["artifacts"])


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    short_id: str
    title: str
    artifact_kind: str
    description: str
    current_version_id: UUID | None
    created_at: datetime


class ArtifactVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    artifact_id: UUID
    version_no: int
    storage_uri: str
    bytes_size: int
    sha256: str
    lineage_jsonb: dict[str, Any]
    template_name: str
    csl_style: str
    created_at: datetime


class RenderedAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_kind: str
    source_id: UUID
    output_format: str
    storage_uri: str
    bytes_size: int
    sha256: str
    created_at: datetime


class BuildIn(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    artifact_kind: str = Field(
        # Only kinds the Builder actually produces (implement-or-400): no
        # `report_docx` / `deck_pdf` exporter exists, so they are rejected here
        # rather than silently emitting a mislabeled markdown bundle.
        pattern=r"^(report_pdf|report_markdown_bundle|source_pack)$"
    )
    description: str = Field(default="", max_length=4096)
    template_name: str = Field(default="report_pdf", max_length=64)
    csl_style: str = Field(default="apa-7", max_length=64)
    wiki_page_ids: list[UUID] = Field(default_factory=list)
    dataset_version_ids: list[UUID] = Field(default_factory=list)


class BuildOut(BaseModel):
    artifact_id: str
    agent_run_id: str
    dispatched: bool


@router.get("/{project_id}/artifacts", response_model=list[ArtifactOut])
async def get_artifacts(project_id: ProjectScopeDep, session: SessionDep) -> list[ArtifactOut]:
    rows = await list_artifacts(session, project_id=project_id)
    return [ArtifactOut.model_validate(r) for r in rows]


@router.get("/{project_id}/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_one_artifact(
    project_id: ProjectScopeDep,
    artifact_id: UUID,
    session: SessionDep,
) -> ArtifactOut:
    a = await get_artifact(session, project_id=project_id, artifact_id=artifact_id)
    if a is None:
        msg = f"artifact {artifact_id} not found"
        raise NotFound(msg)
    return ArtifactOut.model_validate(a)


@router.get(
    "/{project_id}/artifacts/{artifact_id}/versions",
    response_model=list[ArtifactVersionOut],
)
async def get_artifact_versions(
    project_id: ProjectScopeDep,
    artifact_id: UUID,
    session: SessionDep,
) -> list[ArtifactVersionOut]:
    rows = await list_versions(session, project_id=project_id, artifact_id=artifact_id)
    return [ArtifactVersionOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/artifacts/build",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BuildOut,
)
async def post_build(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[BuildIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> BuildOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    artifact = await create_artifact(
        session,
        principal=principal,
        project_id=project_id,
        title=body.title,
        artifact_kind=body.artifact_kind,
        description=body.description,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="artifact.create",
        target_id=artifact.id,
        target_kind="artifact",
        payload={"title": body.title, "artifact_kind": body.artifact_kind},
        trace_id=current_trace_id(),
    )

    agent_run_id = uuid7()
    correlation_id = f"builder-{agent_run_id.hex}"  # full hex — hex[:8] collides within ~65s
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind="builder",
        correlation_id=correlation_id,
        status="pending",
        input_payload={
            "artifact_id": str(artifact.id),
            "artifact_kind": body.artifact_kind,
        },
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(run)
    await session.flush()

    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    # Durable before dispatched: the worker resolves these ids the moment it
    # dequeues, and an arq worker beats an uncommitted transaction. A job that
    # loses that race fails on a row that does not exist yet.
    await session.commit()

    dispatched = False
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
        try:
            await pool.enqueue_job(
                "builder_job",
                str(project_id),
                str(artifact.id),
                body.artifact_kind,
                body.template_name,
                body.csl_style,
                [str(w) for w in body.wiki_page_ids],
                [str(d) for d in body.dataset_version_ids],
                token,
            )
            dispatched = True
        finally:
            await pool.aclose()
    except Exception as exc:
        dispatched = False
        # A swallowed dispatch failure must not strand the run in `pending`
        # forever — the Activity card treats pending as live work.
        run.status = "failed"
        run.completed_at = utcnow()
        run.error_text = f"failed to enqueue builder_job: {exc}"[:4096]
        await session.flush()
    return BuildOut(
        artifact_id=str(artifact.id),
        agent_run_id=str(agent_run_id),
        dispatched=dispatched,
    )


@router.get("/{project_id}/rendered-assets", response_model=list[RenderedAssetOut])
async def get_renders(project_id: ProjectScopeDep, session: SessionDep) -> list[RenderedAssetOut]:
    stmt = (
        select(RenderedAsset)
        .where(RenderedAsset.project_id == project_id)
        .order_by(RenderedAsset.created_at.desc())
        .limit(200)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [RenderedAssetOut.model_validate(r) for r in rows]
