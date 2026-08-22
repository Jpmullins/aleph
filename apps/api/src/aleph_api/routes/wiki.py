"""Wiki API: pages, revisions, search."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from aleph_api.deps import LedgerDep, LiteLLMDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_artifacts.exporters.vault import VaultExport, VaultPage, render_vault
from aleph_artifacts.models import RenderedAsset
from aleph_artifacts.render_service import record_render
from aleph_connectors.models import ApprovalDecision
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models import Project
from aleph_db.repos import model_profile as profile_repo
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.classify import classify_pages, propose_schema
from aleph_wiki.export_evidence import (
    EvidenceCounts,
    PageEvidence,
    attach_evidence,
    count_evidence,
    evidence_files,
)
from aleph_wiki.export_service import load_page_evidence
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.html_compiler import compile_page_html
from aleph_wiki.index_service import IndexService
from aleph_wiki.links import resolve_broken_links
from aleph_wiki.lint import lint_wiki
from aleph_wiki.models import WikiClaim, WikiIndex, WikiLink, WikiPage, WikiRevision
from aleph_wiki.navigation import HubPlan, build_index, plan_hubs, sync_hubs
from aleph_wiki.schema import Category, WikiSchema
from aleph_wiki.schema_service import SchemaService
from aleph_wiki.wiki_service import WikiLinkDraft, WikiService

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
    # WP-6 trust layer.
    volatility: str
    verified_at: datetime | None
    freshness: int | None
    # Schema governance (aleph_wiki.schema). Present on the summary rather than
    # only on the detail read because the surface groups, filters and badges on
    # every one of them — fetching 679 pages to group them by category would be
    # 679 round-trips.
    category: str | None
    page_type: str | None
    tags: list[str]
    confidence: str | None
    contested: bool


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
    """A rejection must say why.

    The reason is not decoration: `feedback_service.pending_for_concept` reads
    it back at compile time so the agent does not reproduce the rejected
    content. This field defaulted to `""` and the handler wrote feedback only
    `if body.reason`, while the only UI that calls this endpoint hardcoded an
    empty string — so the corrective loop existed end to end and had never
    carried a single row. Requiring the reason is what connects it.
    """

    reason: str = Field(min_length=1, max_length=2048)


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
        reason=body.reason,
        decided_by=principal.user_id,
        decided_at=utcnow(),
        created_by=principal.user_id,
    )
    session.add(decision)
    page.status = "archived"
    # Unconditional: the schema now guarantees a non-empty reason, so every
    # rejection reaches the agent instead of only the ones that happened to
    # carry one.
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


async def _compile_and_persist_html(
    *,
    request: Request,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    project_id: UUID,
    page: WikiPage,
) -> tuple[bytes, RenderedAsset]:
    """Deterministically compile the page's HTML and persist it as a
    RenderedAsset (`source_kind="wiki_page_html"`) via `record_render`.

    Idempotent: the compiler is a pure function of (title, body_md, claims,
    infobox), so we key the cached render on the compiled bytes' sha256. A
    matching prior render is reused (no new row, no ledger event); a changed
    page (new revision / edited infobox) materializes a fresh render + a ledger
    event. This keeps repeated reads cheap and never mutates on a cache hit.
    """
    body_md = ""
    claims: list[dict[str, Any]] = []
    if page.current_revision_id is not None:
        rev = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
            )
        ).scalar_one_or_none()
        if rev is not None:
            body_md = rev.body_md
            claim_rows = list(
                (await session.execute(select(WikiClaim).where(WikiClaim.revision_id == rev.id)))
                .scalars()
                .all()
            )
            claims = [{"text": c.text, "confidence": c.confidence} for c in claim_rows]
    html = compile_page_html(
        title=page.title,
        body_md=body_md,
        claims=claims,
        infobox=page.infobox_jsonb,
    )
    data = html.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    existing = (
        await session.execute(
            select(RenderedAsset).where(
                RenderedAsset.project_id == project_id,
                RenderedAsset.source_kind == "wiki_page_html",
                RenderedAsset.source_id == page.id,
                RenderedAsset.sha256 == sha,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return data, existing
    asset = await record_render(
        session,
        asset_store=request.app.state.asset_store,
        principal=principal,
        project_id=project_id,
        source_kind="wiki_page_html",
        source_id=page.id,
        source_version_id=page.current_revision_id,
        dataset_version_ids=[],
        output_format="html",
        data=data,
        render_spec={"kind": "wiki_page_html", "body_sha256": sha},
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="wiki.page.html_compile",
        target_id=asset.id,
        target_kind="rendered_asset",
        payload={"page_id": str(page.id), "sha256": sha},
        trace_id=current_trace_id(),
    )
    return data, asset


@router.get("/{project_id}/wiki/pages/{page_id}/html")
async def get_page_html(
    request: Request,
    project_id: ProjectScopeDep,
    page_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> Response:
    """Deterministic server-compiled HTML for a wiki page (WP-4b).

    Compiles + persists a RenderedAsset (idempotent by sha) and streams the
    self-contained document with `Content-Security-Policy: sandbox` — the
    server-side belt that pairs with `HtmlDocCard`'s `sandbox=""` iframe. No
    scripts, no external refs, so this can never run in the API origin.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    page = (
        await session.execute(
            select(WikiPage).where(WikiPage.id == page_id, WikiPage.project_id == project_id)
        )
    ).scalar_one_or_none()
    if page is None:
        msg = f"wiki page not found: {page_id}"
        raise NotFound(msg)
    data, asset = await _compile_and_persist_html(
        request=request,
        session=session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        page=page,
    )
    return Response(
        content=data,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{asset.sha256}"',
            "Cache-Control": "private, max-age=60",
        },
    )


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


# --- schema governance -------------------------------------------------------
#
# The schema is what the write path validates against and what the agent reads
# before it writes. Exposing it means the taxonomy is editable by the person
# whose domain it describes, rather than being whatever the shipped default
# happened to assume.


class CategoryOut(BaseModel):
    id: str
    title: str
    blurb: str = ""


class WikiSchemaOut(BaseModel):
    domain: str
    categories: list[CategoryOut]
    tags: list[str]
    page_types: list[str]
    min_outbound_links: int
    page_split_lines: int
    stub_promotion_mentions: int
    #: False means this project is still on the shipped default. Worth showing:
    #: a taxonomy nobody has adapted to the domain is a different thing from
    #: one somebody chose.
    customised: bool


class WikiSchemaIn(BaseModel):
    domain: str = Field(min_length=1, max_length=4096)
    categories: list[CategoryOut] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    page_types: list[str] | None = None
    min_outbound_links: int = Field(default=3, ge=0, le=20)
    page_split_lines: int = Field(default=200, ge=20, le=5000)
    stub_promotion_mentions: int = Field(default=5, ge=1, le=100)


def _schema_out(schema: WikiSchema, *, customised: bool) -> WikiSchemaOut:
    return WikiSchemaOut(
        domain=schema.domain,
        categories=[CategoryOut(id=c.id, title=c.title, blurb=c.blurb) for c in schema.categories],
        tags=list(schema.tags),
        page_types=list(schema.page_types),
        min_outbound_links=schema.min_outbound_links,
        page_split_lines=schema.page_split_lines,
        stub_promotion_mentions=schema.stub_promotion_mentions,
        customised=customised,
    )


@router.get("/{project_id}/wiki/schema", response_model=WikiSchemaOut)
async def get_wiki_schema(project_id: ProjectScopeDep, session: SessionDep) -> WikiSchemaOut:
    svc = SchemaService(session)
    return _schema_out(await svc.get(project_id), customised=await svc.is_customised(project_id))


@router.put("/{project_id}/wiki/schema", response_model=WikiSchemaOut)
async def put_wiki_schema(
    project_id: ProjectScopeDep,
    body: Annotated[WikiSchemaIn, Body()],
    session: SessionDep,
    principal: PrincipalDep,
    ledger: LedgerDep,
) -> WikiSchemaOut:
    """Replace the project's schema.

    Editor, not viewer: the taxonomy governs what every future write is allowed
    to say, so widening it is a change to the corpus's rules, not a preference.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    duplicates = {c.id for c in body.categories if [x.id for x in body.categories].count(c.id) > 1}
    if duplicates:
        raise ValidationFailed(
            f"duplicate category ids: {', '.join(sorted(duplicates))} — "
            "[[slug]] resolution is by shortest path, so two categories with "
            "one id makes every link into them ambiguous"
        )

    schema = WikiSchema(
        domain=body.domain,
        categories=tuple(Category(id=c.id, title=c.title, blurb=c.blurb) for c in body.categories),
        tags=tuple(dict.fromkeys(body.tags)),
        page_types=tuple(body.page_types) if body.page_types else WikiSchema.page_types,
        min_outbound_links=body.min_outbound_links,
        page_split_lines=body.page_split_lines,
        stub_promotion_mentions=body.stub_promotion_mentions,
    )
    saved = await SchemaService(session).set(
        project_id=project_id,
        schema=schema,
        principal=principal,
        ledger=ledger,
        trace_id=current_trace_id(),
    )
    await session.commit()
    return _schema_out(saved, customised=True)


# --- lint --------------------------------------------------------------------


class LintFindingOut(BaseModel):
    check: str
    severity: str
    message: str
    fix: str
    page_id: UUID | None
    page_title: str


class LintReportOut(BaseModel):
    pages_scanned: int
    stubs_skipped: int
    checked_at: datetime
    total: int
    by_severity: dict[str, int]
    by_check: dict[str, int]
    findings: list[LintFindingOut]


@router.get("/{project_id}/wiki/lint", response_model=LintReportOut)
async def wiki_lint(
    project_id: ProjectScopeDep,
    session: SessionDep,
    severity: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> LintReportOut:
    """Run every lint check over this project's wiki.

    Read-only. Findings are advice, and acting on one is a write that goes
    through the service so it lands in the ledger — a lint that repaired things
    itself would mutate the corpus with no record of why.
    """
    schema = await SchemaService(session).get(project_id)
    report = await lint_wiki(session, project_id=project_id, schema=schema)
    findings = report.sorted_findings()
    if severity:
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        findings = [f for f in findings if f.severity in wanted]
    return LintReportOut(
        pages_scanned=report.pages_scanned,
        stubs_skipped=report.stubs_skipped,
        checked_at=report.checked_at,
        total=len(findings),
        by_severity=report.by_severity,
        by_check=report.by_check,
        findings=[LintFindingOut(**f.to_dict()) for f in findings[:limit]],
    )


# --- navigation --------------------------------------------------------------


class IndexEntryOut(BaseModel):
    title: str
    slug: str
    summary: str
    status: str
    is_stub: bool


class IndexSectionOut(BaseModel):
    key: str
    title: str
    written: int
    planned: int
    entries: list[IndexEntryOut]


@router.get("/{project_id}/wiki/index", response_model=list[IndexSectionOut])
async def wiki_index(project_id: ProjectScopeDep, session: SessionDep) -> list[IndexSectionOut]:
    """The project index, derived rather than hand-maintained.

    `index.md` in a hermes vault is a file somebody has to remember to update;
    here it is a query, so a page that exists is listed by construction.
    """
    schema = await SchemaService(session).get(project_id)
    sections = await build_index(session, project_id=project_id, schema=schema)
    return [
        IndexSectionOut(
            key=s.key,
            title=s.title,
            written=sum(1 for e in s.entries if not e.is_stub),
            planned=sum(1 for e in s.entries if e.is_stub),
            entries=[
                IndexEntryOut(
                    title=e.title,
                    slug=e.slug,
                    summary=e.summary,
                    status=e.status,
                    is_stub=e.is_stub,
                )
                for e in s.entries
            ],
        )
        for s in sections
    ]


class HubPreviewOut(BaseModel):
    category: str
    title: str
    body_md: str
    written: int
    planned: int


@router.get("/{project_id}/wiki/hubs", response_model=list[HubPreviewOut])
async def wiki_hubs(project_id: ProjectScopeDep, session: SessionDep) -> list[HubPreviewOut]:
    """What each category hub would say if regenerated now."""
    schema = await SchemaService(session).get(project_id)
    return [
        HubPreviewOut(
            category=p.category.id,
            title=f"{p.category.title} Hub",
            body_md=p.body_md,
            written=sum(1 for e in p.entries if not e.is_stub),
            planned=sum(1 for e in p.entries if e.is_stub),
        )
        for p in await plan_hubs(session, project_id=project_id, schema=schema)
    ]


# --- deriving a schema from the corpus ---------------------------------------
#
# The shipped default describes AI/ML research because that is the first plugin
# suite. On any other project it is the wrong taxonomy, and a wrong taxonomy is
# worse than none: it gives every page a plausible-looking home, so nothing ever
# reports a problem while the categories quietly stop meaning anything.


class SchemaProposalOut(BaseModel):
    proposed: WikiSchemaOut | None
    current: WikiSchemaOut
    #: Named so the caller can show what would change before accepting.
    categories_added: list[str]
    categories_removed: list[str]
    tags_added: list[str]
    tags_removed: list[str]


@router.post("/{project_id}/wiki/schema/propose", response_model=SchemaProposalOut)
async def propose_wiki_schema(
    project_id: ProjectScopeDep,
    session: SessionDep,
    litellm: LiteLLMDep,
    principal: PrincipalDep,
) -> SchemaProposalOut:
    """Propose a schema fitting the corpus that actually exists.

    Read-only: this returns a proposal and writes nothing. The taxonomy governs
    every future write, so adopting one is a deliberate act — `PUT /wiki/schema`
    with the proposal is what accepts it.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = f"project {project_id} has no model profile"
        raise NotFound(msg)

    svc = SchemaService(session)
    current = await svc.get(project_id)
    proposed = await propose_schema(
        session,
        project_id=project_id,
        client=litellm,
        principal=principal,
        profile_bindings=profile.bindings_jsonb,
        current=current,
    )
    if proposed is None:
        return SchemaProposalOut(
            proposed=None,
            current=_schema_out(current, customised=await svc.is_customised(project_id)),
            categories_added=[],
            categories_removed=[],
            tags_added=[],
            tags_removed=[],
        )
    return SchemaProposalOut(
        proposed=_schema_out(proposed, customised=True),
        current=_schema_out(current, customised=await svc.is_customised(project_id)),
        categories_added=sorted(proposed.category_ids - current.category_ids),
        categories_removed=sorted(current.category_ids - proposed.category_ids),
        tags_added=sorted(set(proposed.tags) - set(current.tags)),
        tags_removed=sorted(set(current.tags) - set(proposed.tags)),
    )


class ClassifyIn(BaseModel):
    include_stubs: bool = True
    #: Bounded so one request cannot turn into an unbounded model spend. A
    #: caller with a large corpus runs it repeatedly; it is resumable because
    #: only uncategorised pages are ever touched.
    limit: int = Field(default=400, ge=1, le=2000)


class ClassifyOut(BaseModel):
    filed: int
    skipped: int
    unknown_category: int
    remaining_uncategorised: int
    summary: str


@router.post("/{project_id}/wiki/classify", response_model=ClassifyOut)
async def classify_wiki_pages(
    project_id: ProjectScopeDep,
    body: Annotated[ClassifyIn, Body()],
    session: SessionDep,
    litellm: LiteLLMDep,
    principal: PrincipalDep,
) -> ClassifyOut:
    """File uncategorised pages into the schema in force.

    Touches only pages with no category, so it is resumable and never silently
    refiles a page somebody placed by hand.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = f"project {project_id} has no model profile"
        raise NotFound(msg)

    schema = await SchemaService(session).get(project_id)
    result = await classify_pages(
        session,
        project_id=project_id,
        schema=schema,
        client=litellm,
        principal=principal,
        profile_bindings=profile.bindings_jsonb,
        include_stubs=body.include_stubs,
        limit=body.limit,
    )
    await session.commit()

    remaining = (
        await session.execute(
            select(func.count())
            .select_from(WikiPage)
            .where(WikiPage.project_id == project_id, WikiPage.category.is_(None))
        )
    ).scalar_one()
    return ClassifyOut(
        filed=result.filed,
        skipped=result.skipped,
        unknown_category=result.unknown_category,
        remaining_uncategorised=int(remaining or 0),
        summary=result.summary(),
    )


class HubSyncOut(BaseModel):
    created: int
    updated: int
    unchanged: int
    summary: str


@router.post("/{project_id}/wiki/hubs/sync", response_model=HubSyncOut)
async def sync_wiki_hubs(
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
    ledger: LedgerDep,
) -> HubSyncOut:
    """Write every category hub as a real page.

    A hub is derived, but it is persisted rather than rendered on the fly so
    `[[logging-recovery-hub]]` resolves like any other wikilink and an exported
    vault opens in Obsidian with its navigation intact.

    Idempotent: a hub whose body already matches is skipped, so running this on
    a schedule does not append a revision per category per run to an immutable,
    append-only revision table.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    schema = await SchemaService(session).get(project_id)
    svc = WikiService(session)

    async def commit(plan: HubPlan) -> None:
        result = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            page_id=None,
            title=f"{plan.category.title} Hub",
            slug=plan.category.hub_slug,
            page_kind="topic",
            body_md=plan.body_md,
            summary=plan.category.blurb,
            claims=[],
            # `dst_title` must be the text the BODY actually contains. The hub
            # renders `[[slug|Title]]` (Obsidian's shortest-path form, which
            # survives a page being retitled), so recording the title here would
            # give every hub a set of link rows that match nothing in its own
            # prose — the reader resolves a chip by looking its text up in these
            # rows, so every link on every hub would render as broken.
            wikilinks=[
                WikiLinkDraft(dst_title=e.slug, dst_page_id=None)
                for e in plan.entries
                if not e.is_stub
            ],
            commit_message=f"regenerate {plan.category.id} hub",
            respect_hand_edits=False,
            origin="system",
        )
        # A hub is a hub. The commit path knows `page_kind`, which is about how
        # the page was produced; `page_type` is what kind of knowledge it holds,
        # and without setting it the hub would be treated as an ordinary page —
        # listed inside itself, and reported as an orphan by the lint.
        page = (
            await session.execute(select(WikiPage).where(WikiPage.id == result.page_id))
        ).scalar_one()
        page.page_type = "hub"
        page.category = plan.category.id
        page.tags = ["hub"]
        page.confidence = "high"
        page.status = "approved"

    outcome = await sync_hubs(session, project_id=project_id, schema=schema, commit=commit)
    await session.commit()
    return HubSyncOut(
        created=outcome.created,
        updated=outcome.updated,
        unchanged=outcome.unchanged,
        summary=outcome.summary(),
    )


class LinkRepairOut(BaseModel):
    resolved: int
    still_broken: int
    by_title: int
    by_slug: int
    summary: str


@router.post("/{project_id}/wiki/links/resolve", response_model=LinkRepairOut)
async def resolve_wiki_links(
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
    dry_run: Annotated[bool, Query()] = False,
) -> LinkRepairOut:
    """Point every resolvable `[[wikilink]]` at the page it means.

    Matches on exact title, then case-insensitively, then on slug — the last
    because the vault schema makes slugs globally unique so `[[slug]]` resolves
    wherever the page lives, which is how Obsidian's shortest-path linking
    works. The pre-existing repair path went only through the legacy alias
    table, so a link matching a page's title exactly still did not resolve.

    A link still unresolved after this genuinely names a page nobody has
    written, which is what makes the lint's `broken-wikilink` count mean
    something.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    result = await resolve_broken_links(session, project_id=project_id, dry_run=dry_run)
    if not dry_run:
        await session.commit()
    return LinkRepairOut(
        resolved=result.resolved,
        still_broken=result.still_broken,
        by_title=result.by_title,
        by_slug=result.by_slug,
        summary=result.summary(),
    )


# --- vault export ----------------------------------------------------------
#
# Three places in the tree said an Aleph project "opens as an Obsidian vault"
# and nothing anywhere wrote one out. This is the read path for that claim: the
# wiki as markdown files on disk, in a zip, in either of two link dialects.
#
# It matters beyond interoperability. An export is what makes the knowledge
# layer leaveable, and `docs/decisions.md` D1 leaves the wiki's storage
# expected to move; a corpus with no way out is a corpus held hostage to
# whichever schema happens to be current.


class VaultDanglingOut(BaseModel):
    """A link the export could not point at a file in the bundle."""

    from_slug: str
    from_title: str
    target: str
    display: str


class VaultEvidenceOut(BaseModel):
    """How much of the belief layer the bundle carries.

    Reported even when every number is zero, and that is the point: the sidecar
    is absent both when a project has no claims and when `?evidence=false` was
    passed, and those are different facts. A count of 0 next to
    `included: true` says "measured, and the belief layer has never run here" —
    which a missing file cannot say.
    """

    included: bool
    claims: int
    citations: int
    #: Citations carrying the whole chain: a source, a verbatim quote, and the
    #: character span it occupies. On the live corpus this is roughly half of
    #: `citations`, so the two numbers must not be collapsed into one.
    anchored_citations: int
    pages_with_claims: int


class VaultExportOut(BaseModel):
    """What `?dry_run=true` reports instead of bytes."""

    dialect: str
    project_title: str
    page_count: int
    files: list[str]
    dangling: list[VaultDanglingOut]
    evidence: VaultEvidenceOut


async def _vault_pages(
    session: SessionDep,
    project_id: UUID,
    evidence: Mapping[UUID, PageEvidence],
) -> list[VaultPage]:
    """The exportable corpus: every non-stub page, with its current body.

    Stubs are excluded — 779 of the 844 live pages are stubs, and a stub is a
    red link somebody's prose created, not a document. Exporting them would
    produce a vault that is 92% empty files.

    A non-stub page with no current revision IS exported, with the empty body it
    has. It is one page on the live corpus and it is real data: the page exists,
    it has governance fields, and nobody has written it. Dropping it would make
    the file count quietly disagree with `count(*) where not is_stub`, which is
    the number anyone checking this export will run.

    `evidence` maps page id → the live claims on that page. Where there are
    any, the page's body gains a generated `## Evidence` section: the wiki is
    rendered FROM the belief layer, and prose exported without the claims and
    quotes behind it is the conclusion with the reasoning deleted.
    """
    rows = (
        await session.execute(
            select(WikiPage, WikiRevision.body_md, WikiIndex.aliases_jsonb)
            .outerjoin(WikiRevision, WikiRevision.id == WikiPage.current_revision_id)
            .outerjoin(WikiIndex, WikiIndex.page_id == WikiPage.id)
            .where(WikiPage.project_id == project_id, WikiPage.is_stub.is_(False))
            .order_by(WikiPage.slug)
        )
    ).all()
    return [
        VaultPage(
            title=page.title,
            slug=page.slug,
            body_md=(
                attach_evidence(body or "", evidence[page.id])
                if page.id in evidence
                else (body or "")
            ),
            page_type=page.page_type,
            category=page.category,
            tags=tuple(str(t) for t in (page.tags or [])),
            related=tuple(str(r) for r in (page.related or [])),
            aliases=tuple(str(a) for a in (aliases or [])),
            confidence=page.confidence,
            contested=page.contested,
            contradictions=tuple(str(c) for c in (page.contradictions or [])),
            created=page.created_at.date() if page.created_at else None,
            updated=page.updated_at.date() if page.updated_at else None,
        )
        for page, body, aliases in rows
    ]


@router.post("/{project_id}/export/vault")
async def export_vault(
    project_id: ProjectScopeDep,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    dialect: Annotated[str, Query(pattern="^(obsidian|okf)$")] = "obsidian",
    dry_run: Annotated[bool, Query()] = False,
    evidence: Annotated[bool, Query()] = True,
) -> Response:
    """Export the wiki as a markdown vault: one `.md` per non-stub page + `index.md`.

    `dialect=obsidian` emits `[[wikilinks]]`; `dialect=okf` emits
    `[text](./slug.md)` and stamps `okf_version: "0.1"` on the index, which is
    what makes the bundle readable by anything other than Obsidian.

    `?evidence=true` (the default) carries the belief layer out with the prose:
    each page gains a generated `## Evidence` section listing its live claims
    with their verbatim quotes and character spans, and the bundle gains
    `evidence.json`, the same chain in a lossless machine-readable form —
    claim → citation → source → chunk → span. Pass `evidence=false` for the
    prose-only vault. A page is rendered from claims; an export that ships only
    the rendering hands somebody the conclusions and none of the reasons, and
    it cannot be re-imported as knowledge.

    `?dry_run=true` returns the file list, the dangling-link report and the
    evidence counts as JSON instead of the zip. That is the only way either
    report is legible: a link the exporter could not resolve is written out as
    plain text rather than as a link into nowhere, and the sidecar is absent
    from a project with no claims — so a caller who only ever downloads the zip
    would never learn that anything was dropped or that nothing was measured.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        msg = f"project not found: {project_id}"
        raise NotFound(msg)

    evidence_by_page = await load_page_evidence(session, project_id) if evidence else {}
    pages = await _vault_pages(session, project_id, evidence_by_page)
    export = render_vault(pages, dialect=dialect, project_title=project.title)

    # Sorted by slug so two exports of an unchanged corpus produce identical
    # bytes; `dict.values()` order is insertion order, which is the query's
    # order today and nobody's contract.
    page_evidence = sorted(evidence_by_page.values(), key=lambda p: (p.slug, p.title))
    counts = count_evidence(page_evidence)
    extra = evidence_files(page_evidence, project_title=project.title, dialect=export.dialect)

    if dry_run:
        return JSONResponse(
            VaultExportOut(
                dialect=export.dialect,
                project_title=project.title,
                # From the pre-merge export: `page_count` counts every
                # non-reserved file, so a merged bundle would report
                # `evidence.json` as a page.
                page_count=export.page_count,
                files=sorted({**export.files, **extra}),
                evidence=_evidence_out(included=evidence, counts=counts),
                dangling=[
                    VaultDanglingOut(
                        from_slug=d.from_slug,
                        from_title=d.from_title,
                        target=d.target,
                        display=d.display,
                    )
                    for d in export.dangling
                ],
            ).model_dump()
        )

    # One zip writer, reused: `VaultExport.to_bytes` pins entry order and
    # timestamps, and a second writer here would produce a bundle that differs
    # from the vault exporter's on every byte for no reason anybody could see.
    # This merged object exists only to be zipped — read its `page_count` and
    # you get the sidecar counted as a page, which is why every count above and
    # below comes from `export`, not from this.
    payload = VaultExport(
        dialect=export.dialect,
        files={**export.files, **extra},
        dangling=export.dangling,
    ).to_bytes()
    # A full-corpus export is data egress, so it is a ledger event even though
    # it mutates nothing: "who took a copy of the wiki, and when" is exactly the
    # question an append-only action log exists to answer. The evidence counts
    # are in the row because the quotes are somebody else's copyrighted text —
    # "a copy of the wiki left" and "4,082 verbatim source quotes left with it"
    # are different events and the log has to be able to tell them apart.
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="wiki.vault.export",
        target_id=project_id,
        target_kind="project",
        payload={
            "dialect": export.dialect,
            "page_count": export.page_count,
            "dangling_links": len(export.dangling),
            "bytes": len(payload),
            "evidence_included": evidence,
            "claims": counts.claims,
            "citations": counts.citations,
            "anchored_citations": counts.anchored_citations,
        },
        trace_id=current_trace_id(),
    )
    await session.commit()
    slug = _slugify_filename(project.title) or "wiki"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-vault-{export.dialect}.zip"',
            "X-Vault-Dialect": export.dialect,
            "X-Vault-Page-Count": str(export.page_count),
            "X-Vault-Dangling-Links": str(len(export.dangling)),
            "X-Vault-Claims": str(counts.claims),
            "X-Vault-Anchored-Citations": str(counts.anchored_citations),
        },
    )


def _evidence_out(*, included: bool, counts: EvidenceCounts) -> VaultEvidenceOut:
    return VaultEvidenceOut(
        included=included,
        claims=counts.claims,
        citations=counts.citations,
        anchored_citations=counts.anchored_citations,
        pages_with_claims=counts.pages_with_claims,
    )


def _slugify_filename(title: str) -> str:
    """A project title reduced to something safe in a `Content-Disposition`.

    Not cosmetic: a title containing a quote or a newline would break out of the
    header value, and a header injection through a filename is a real one.
    """
    return re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")[:64]
