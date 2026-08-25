"""Sessions, threads, messages — the chat surface API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_assistant.models import AssistantSession
from aleph_assistant.thread_service import (
    create_session,
    fork_thread,
    get_thread,
    list_sessions,
    list_threads,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_db.repos import model_profile as profile_repo
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["assistant"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    last_activity_at: datetime
    created_at: datetime


class SessionCreateIn(BaseModel):
    title: str = Field(default="New session", min_length=1, max_length=255)


class SessionRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    parent_thread_id: UUID | None
    title: str | None
    created_at: datetime


class ForkIn(BaseModel):
    parent_thread_id: UUID
    from_ordinal: int = Field(ge=0)


class RetrievalDebugIn(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    top_k: int = Field(default=8, ge=1, le=20)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=dict,
)
async def post_session(
    project_id: ProjectScopeDep,
    body: Annotated[SessionCreateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> dict[str, str]:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    s, t = await create_session(
        session,
        project_id=project_id,
        title=body.title,
        created_by=principal.user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="assistant.session.create",
        target_id=s.id,
        target_kind="assistant_session",
        payload={"title": body.title, "thread_id": str(t.id)},
        trace_id=current_trace_id(),
    )
    return {"session_id": str(s.id), "thread_id": str(t.id)}


@router.get("/{project_id}/sessions", response_model=list[SessionOut])
async def get_sessions(project_id: ProjectScopeDep, session: SessionDep) -> list[SessionOut]:
    rows = await list_sessions(session, project_id=project_id)
    return [SessionOut.model_validate(r) for r in rows]


@router.patch("/{project_id}/sessions/{session_id}", response_model=SessionOut)
async def rename_session(
    project_id: ProjectScopeDep,
    session_id: UUID,
    body: Annotated[SessionRenameIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> SessionOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    s = await session.get(AssistantSession, session_id)
    if s is None or s.project_id != project_id:
        msg = f"session not found: {session_id}"
        raise NotFound(msg)
    s.title = body.title
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="assistant.session.rename",
        target_id=s.id,
        target_kind="assistant_session",
        payload={"new_title": body.title},
        trace_id=current_trace_id(),
    )
    return SessionOut.model_validate(s)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@router.get("/{project_id}/sessions/{session_id}/threads", response_model=list[ThreadOut])
async def get_threads(
    project_id: ProjectScopeDep, session_id: UUID, session: SessionDep
) -> list[ThreadOut]:
    # Verify session belongs to project (404 on cross-project access).
    s = await session.get(AssistantSession, session_id)
    if s is None or s.project_id != project_id:
        msg = f"session not found: {session_id}"
        raise NotFound(msg)
    rows = await list_threads(session, session_id=session_id)
    return [ThreadOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/sessions/{session_id}/threads/fork",
    status_code=status.HTTP_201_CREATED,
    response_model=ThreadOut,
)
async def post_fork(
    project_id: ProjectScopeDep,
    session_id: UUID,
    body: Annotated[ForkIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ThreadOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    s = await session.get(AssistantSession, session_id)
    if s is None or s.project_id != project_id:
        msg = f"session not found: {session_id}"
        raise NotFound(msg)
    parent = await get_thread(session, project_id=project_id, thread_id=body.parent_thread_id)
    if parent is None:
        msg = f"parent thread {body.parent_thread_id} not found"
        raise NotFound(msg)
    new_thread = await fork_thread(
        session,
        project_id=project_id,
        parent_thread_id=parent.id,
        from_ordinal=body.from_ordinal,
        created_by=principal.user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="assistant.thread.fork",
        target_id=new_thread.id,
        target_kind="assistant_thread",
        payload={
            "parent_thread_id": str(parent.id),
            "from_ordinal": body.from_ordinal,
        },
        trace_id=current_trace_id(),
    )
    return ThreadOut.model_validate(new_thread)


# ---------------------------------------------------------------------------
# Retrieval debug
# ---------------------------------------------------------------------------


@router.post("/{project_id}/retrieval/debug")
async def retrieval_debug(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[RetrievalDebugIn, Body()],
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = "project has no model profile"
        raise ValidationFailed(msg)
    # This project's client, not the deployment's. `app.state.litellm` is built
    # once at boot from `LITELLM_BASE_URL`, so a project with its own
    # `gateway_endpoints` row got a setting that read back correctly and routed
    # its traffic somewhere else — which is the defect MEP-4 exists to close.
    from aleph_api.routes.gateway_endpoints import litellm_for_project
    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter

    router_obj = WikiFirstRetrievalRouter(
        session_maker=request.app.state.session_maker,
        litellm=await litellm_for_project(request, session, project_id),
        # Debug-only route: an HTTP request, not a turn, so there is no agent
        # run for these calls to belong to and `agent_run_id=None` below is
        # correct rather than a gap. Saying so in the PURPOSE is what keeps
        # status number 5 meaningful — see the note in the router's __init__.
        purpose_prefix="diagnostic",
    )
    result = await router_obj.retrieve(
        principal=principal,
        project_id=project_id,
        thread_id=uuid7(),  # debug-only, no thread persistence
        query=body.query,
        prior_messages=[],
        profile=profile,
        agent_run_id=None,
        top_k_pages=body.top_k,
    )
    return {
        "composed_body_md": result.composed_body_md,
        "coverage_judgment": result.coverage_judgment,
        "page_selection_reason": result.page_selection_reason,
        "selected_pages": [
            {
                "page_id": str(p.page_id),
                "title": p.title,
                "relevance": p.relevance_label,
                "score": p.score,
            }
            for p in result.selected_pages
        ],
        "expanded_pages": [
            {"page_id": str(p.page_id), "title": p.title} for p in result.expanded_pages
        ],
        "descent_chunks": [
            {
                "chunk_id": str(c.chunk_id),
                "source_short_id": c.source_short_id,
                "section_path": c.section_path,
                "score": c.score,
            }
            for c in result.descent_chunks
        ],
        "synthesis_requests": [
            {"concept": s.concept, "missing": s.missing} for s in result.synthesis_requests
        ],
    }
