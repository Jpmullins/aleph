"""Sessions, threads, messages — the chat surface API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_assistant.models import AssistantMessage, AssistantSession, AssistantThread
from aleph_assistant.thread_service import (
    append_message,
    create_session,
    fork_thread,
    get_message,
    get_thread,
    list_messages,
    list_sessions,
    list_threads,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_db.repos import model_profile as profile_repo
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

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


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    thread_id: UUID
    ordinal: int
    role: str
    body_md: str
    status: str
    retrieval_jsonb: dict
    attached_cards_jsonb: list
    agent_run_id: UUID | None
    cost_usd: Decimal
    latency_ms: int | None
    error_text: str | None
    created_at: datetime


class PostMessageIn(BaseModel):
    body_md: str = Field(min_length=1, max_length=64_000)


class PostMessageOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut


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
async def get_sessions(
    project_id: ProjectScopeDep, session: SessionDep
) -> list[SessionOut]:
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


@router.get(
    "/{project_id}/sessions/{session_id}/threads", response_model=list[ThreadOut]
)
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
# Messages
# ---------------------------------------------------------------------------


@router.get(
    "/{project_id}/threads/{thread_id}/messages", response_model=list[MessageOut]
)
async def get_thread_messages(
    project_id: ProjectScopeDep, thread_id: UUID, session: SessionDep
) -> list[MessageOut]:
    t = await get_thread(session, project_id=project_id, thread_id=thread_id)
    if t is None:
        msg = f"thread {thread_id} not found"
        raise NotFound(msg)
    rows = await list_messages(session, thread_id=thread_id)
    return [MessageOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/threads/{thread_id}/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=PostMessageOut,
)
async def post_message(
    request: Request,
    project_id: ProjectScopeDep,
    thread_id: UUID,
    body: Annotated[PostMessageIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> PostMessageOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    t = await get_thread(session, project_id=project_id, thread_id=thread_id)
    if t is None:
        msg = f"thread {thread_id} not found"
        raise NotFound(msg)

    user_msg = await append_message(
        session,
        project_id=project_id,
        thread_id=thread_id,
        role="user",
        body_md=body.body_md,
        created_by=principal.user_id,
        status="complete",
    )

    # Create assistant message placeholder (status=streaming) + AgentRun.
    agent_run_id = uuid7()
    correlation_id = f"chat-{user_msg.id.hex[:8]}"
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind="assistant",
        correlation_id=correlation_id,
        status="pending",
        input_payload={
            "thread_id": str(thread_id),
            "user_message_id": str(user_msg.id),
        },
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(run)
    await session.flush()

    assistant_msg = await append_message(
        session,
        project_id=project_id,
        thread_id=thread_id,
        role="assistant",
        body_md="",
        created_by=principal.user_id,
        status="streaming",
        agent_run_id=agent_run_id,
    )

    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="assistant.message.user_posted",
        target_id=user_msg.id,
        target_kind="assistant_message",
        payload={"thread_id": str(thread_id), "ordinal": user_msg.ordinal},
        trace_id=current_trace_id(),
    )

    # Mint agent token; enqueue assistant_turn_job.
    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(
            RedisSettings.from_dsn(request.app.state.settings.redis_url)
        )
        try:
            await pool.enqueue_job(
                "assistant_turn_job",
                str(project_id),
                str(thread_id),
                str(user_msg.id),
                str(assistant_msg.id),
                token,
            )
        finally:
            await pool.aclose()
    except Exception as exc:  # noqa: BLE001
        # Mark assistant message failed so the UI sees consistent state.
        assistant_msg.status = "failed"
        assistant_msg.error_text = f"failed to enqueue assistant turn: {exc}"[:4096]

    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(assistant_msg),
    )


@router.get(
    "/{project_id}/messages/{message_id}", response_model=MessageOut
)
async def get_one_message(
    project_id: ProjectScopeDep, message_id: UUID, session: SessionDep
) -> MessageOut:
    m = await get_message(session, project_id=project_id, message_id=message_id)
    if m is None:
        msg = f"message {message_id} not found"
        raise NotFound(msg)
    return MessageOut.model_validate(m)


@router.get("/{project_id}/messages/{message_id}/stream")
async def stream_message(
    request: Request,
    project_id: ProjectScopeDep,
    message_id: UUID,
) -> StreamingResponse:
    """SSE stream that polls the in-progress assistant message and emits
    incremental updates as Server-Sent Events. The composer's actual
    token-streaming is approximated by polling `body_md` length — this
    keeps the streaming wire format stable; Inc 4 swaps the in-process
    composer to true token streaming."""

    async def events() -> Any:
        last_len = 0
        maker = request.app.state.session_maker
        for _ in range(600):  # ~5 min cap
            async with maker() as s:
                msg = await s.get(AssistantMessage, message_id)
                if msg is None or msg.project_id != project_id:
                    yield 'data: {"event":"error","reason":"not_found"}\n\n'
                    return
                if len(msg.body_md) > last_len:
                    delta = msg.body_md[last_len:]
                    last_len = len(msg.body_md)
                    import json as _json

                    yield (
                        "data: "
                        + _json.dumps({"event": "token", "delta": delta})
                        + "\n\n"
                    )
                if msg.status in ("complete", "failed", "budget_blocked"):
                    yield (
                        'data: {"event":"done","status":"' + msg.status + '"}\n\n'
                    )
                    return
            await asyncio.sleep(0.5)
        yield 'data: {"event":"timeout"}\n\n'

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter

    router_obj = WikiFirstRetrievalRouter(
        session_maker=request.app.state.session_maker,
        litellm=request.app.state.litellm,
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
            {"concept": s.concept, "missing": s.missing}
            for s in result.synthesis_requests
        ],
    }
