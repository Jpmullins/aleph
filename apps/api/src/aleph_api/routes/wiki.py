"""Wiki API: pages, revisions, search."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.models import ApprovalDecision
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.index_service import IndexService
from aleph_wiki.models import WikiClaim, WikiLink, WikiPage, WikiRevision

router = APIRouter(prefix="/v1/projects", tags=["wiki"])


class WikiPageSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    slug: str
    page_kind: str
    status: str
    is_stub: bool
    current_revision_id: UUID | None
    last_compiled_at: datetime | None


class WikiRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID
    revision_no: int
    body_md: str
    summary: str
    author_kind: str
    author_id: UUID
    parent_revision_id: UUID | None
    body_sha256: str
    commit_message: str
    created_at: datetime


class WikiPageDetailOut(BaseModel):
    page: WikiPageSummaryOut
    revision: WikiRevisionOut | None
    claims: list[dict[str, Any]]
    wikilinks_out: list[dict[str, Any]]


@router.get("/{project_id}/wiki/pages", response_model=list[WikiPageSummaryOut])
async def list_pages(
    project_id: ProjectScopeDep,
    session: SessionDep,
    kind: Annotated[str | None, Query()] = None,
    is_stub: Annotated[bool | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[WikiPageSummaryOut]:
    stmt = (
        select(WikiPage)
        .where(WikiPage.project_id == project_id)
        .order_by(WikiPage.last_compiled_at.desc().nullslast(), WikiPage.title)
    )
    if kind:
        stmt = stmt.where(WikiPage.page_kind == kind)
    if is_stub is not None:
        stmt = stmt.where(WikiPage.is_stub.is_(is_stub))
    if status_filter:
        stmt = stmt.where(WikiPage.status == status_filter)
    rows = list((await session.execute(stmt)).scalars().all())
    return [WikiPageSummaryOut.model_validate(p) for p in rows]


@router.get("/{project_id}/wiki/pages/{page_id}", response_model=WikiPageDetailOut)
async def get_page(
    project_id: ProjectScopeDep,
    page_id: UUID,
    session: SessionDep,
    revision: Annotated[UUID | None, Query()] = None,
) -> WikiPageDetailOut:
    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.id == page_id, WikiPage.project_id == project_id)
        )
    ).scalar_one_or_none()
    if page is None:
        msg = f"wiki page not found: {page_id}"
        raise NotFound(msg)
    rev_id = revision or page.current_revision_id
    rev: WikiRevision | None = None
    claims: list[dict[str, Any]] = []
    if rev_id is not None:
        rev = (
            await session.execute(select(WikiRevision).where(WikiRevision.id == rev_id))
        ).scalar_one_or_none()
        if rev is not None:
            claim_rows = list(
                (await session.execute(select(WikiClaim).where(WikiClaim.revision_id == rev.id)))
                .scalars()
                .all()
            )
            claims = [
                {
                    "id": str(c.id),
                    "text": c.text,
                    "confidence": c.confidence,
                    "section_anchor": c.section_anchor,
                }
                for c in claim_rows
            ]
    links: list[dict[str, Any]] = []
    if page.current_revision_id is not None:
        link_rows = list(
            (
                await session.execute(
                    select(WikiLink).where(WikiLink.src_revision_id == page.current_revision_id)
                )
            )
            .scalars()
            .all()
        )
        # Read-time resolution: wikilinks compiled before their target page
        # existed are stored with a null dst_page_id (an ordering artifact of
        # incremental compile). Resolve those by exact title match so the
        # reader's links are navigable immediately and self-heal as pages
        # appear. Durable stored repair is the `…/aliases/repair-links` route.
        unresolved = {lk.dst_title for lk in link_rows if lk.dst_page_id is None}
        resolved_by_title: dict[str, UUID] = {}
        if unresolved:
            resolved_by_title = {
                title: pid
                for pid, title in (
                    await session.execute(
                        select(WikiPage.id, WikiPage.title).where(
                            WikiPage.project_id == project_id,
                            WikiPage.title.in_(unresolved),
                        )
                    )
                ).all()
            }
        links = [
            {
                "dst_title": lk.dst_title,
                "dst_page_id": (
                    str(lk.dst_page_id)
                    if lk.dst_page_id
                    else (
                        str(resolved_by_title[lk.dst_title])
                        if lk.dst_title in resolved_by_title
                        else None
                    )
                ),
                "occurrences": lk.occurrences,
            }
            for lk in link_rows
        ]
    return WikiPageDetailOut(
        page=WikiPageSummaryOut.model_validate(page),
        revision=WikiRevisionOut.model_validate(rev) if rev else None,
        claims=claims,
        wikilinks_out=links,
    )


@router.get("/{project_id}/wiki/pages/by-slug/{slug}", response_model=WikiPageDetailOut)
async def get_page_by_slug(
    project_id: ProjectScopeDep, slug: str, session: SessionDep
) -> WikiPageDetailOut:
    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.project_id == project_id, WikiPage.slug == slug)
        )
    ).scalar_one_or_none()
    if page is None:
        msg = f"wiki page not found: {slug}"
        raise NotFound(msg)
    return await get_page(project_id, page.id, session, revision=None)


class RejectPageIn(BaseModel):
    reason: str = Field(default="", max_length=2048)


async def _load_draft_page(project_id: UUID, page_id: UUID, session: SessionDep) -> WikiPage:
    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.id == page_id, WikiPage.project_id == project_id)
        )
    ).scalar_one_or_none()
    if page is None:
        msg = f"wiki page not found: {page_id}"
        raise NotFound(msg)
    return page


@router.post("/{project_id}/wiki/pages/{page_id}/approve", response_model=WikiPageSummaryOut)
async def approve_page(
    project_id: ProjectScopeDep,
    page_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> WikiPageSummaryOut:
    """Approve a draft wiki page directly (draft → approved).

    The curation counterpart to the Briefs synthesis-proposal approval: an
    agent-compiled draft has no proposal, so this transitions the page itself,
    records an ApprovalDecision, and writes an Action Ledger event.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    page = await _load_draft_page(project_id, page_id, session)
    if page.status == "approved":
        msg = "page already approved"
        raise ValidationFailed(msg)
    prior = page.status
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="wiki_page",
        target_id=page_id,
        decision="approved",
        reason=None,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(decision)
    page.status = "approved"
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="wiki.page.approve",
        target_id=page_id,
        target_kind="wiki_page",
        payload={"prior_status": prior},
        trace_id=current_trace_id(),
    )
    return WikiPageSummaryOut.model_validate(page)


@router.post("/{project_id}/wiki/pages/{page_id}/reject", response_model=WikiPageSummaryOut)
async def reject_page(
    project_id: ProjectScopeDep,
    page_id: UUID,
    body: Annotated[RejectPageIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> WikiPageSummaryOut:
    """Reject a draft wiki page (→ archived) and record rejection feedback.

    The feedback (reason + the rejected revision) is what the wiki agent reads
    on the next compile to avoid repeating the rejected content.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    page = await _load_draft_page(project_id, page_id, session)
    if page.status == "archived":
        msg = "page already archived"
        raise ValidationFailed(msg)
    prior = page.status
    decision = ApprovalDecision(
        id=uuid7(),
        project_id=project_id,
        target_kind="wiki_page",
        target_id=page_id,
        decision="rejected",
        reason=body.reason or None,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(decision)
    page.status = "archived"
    if body.reason:
        await write_feedback(
            session,
            project_id=project_id,
            page_id=page_id,
            concept_name=page.title,
            rejected_revision_id=page.current_revision_id,
            reason=body.reason,
            rejected_by=principal.user_id,
        )
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="wiki.page.reject",
        target_id=page_id,
        target_kind="wiki_page",
        payload={"prior_status": prior, "reason": body.reason or ""},
        trace_id=current_trace_id(),
    )
    return WikiPageSummaryOut.model_validate(page)


@router.get(
    "/{project_id}/wiki/pages/{page_id}/revisions",
    response_model=list[WikiRevisionOut],
)
async def list_revisions(
    project_id: ProjectScopeDep, page_id: UUID, session: SessionDep
) -> list[WikiRevisionOut]:
    stmt = (
        select(WikiRevision)
        .where(WikiRevision.page_id == page_id, WikiRevision.project_id == project_id)
        .order_by(WikiRevision.revision_no.desc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [WikiRevisionOut.model_validate(r) for r in rows]


class WikiSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    top_k: int = Field(default=8, ge=1, le=50)


class WikiSearchHitOut(BaseModel):
    page_id: UUID
    title: str
    slug: str
    summary: str
    score: float
    page_kind: str
    is_stub: bool
    wikilinks_out: list[dict[str, Any]]


@router.post("/{project_id}/wiki/search", response_model=list[WikiSearchHitOut])
async def wiki_search(
    project_id: ProjectScopeDep,
    body: Annotated[WikiSearchIn, Body()],
    session: SessionDep,
) -> list[WikiSearchHitOut]:
    svc = IndexService(session)
    hits = await svc.select_pages(project_id=project_id, query=body.query, top_k=body.top_k)
    return [
        WikiSearchHitOut(
            page_id=h.page_id,
            title=h.title,
            slug=h.slug,
            summary=h.summary,
            score=h.score,
            page_kind=h.page_kind,
            is_stub=h.is_stub,
            wikilinks_out=h.wikilinks_out,
        )
        for h in hits
    ]
