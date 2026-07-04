"""ActionRouter handler registrations.

Each handler executes the requested action inside the dispatching
session + ledger. Handlers receive the same kwargs `ActionRouter`
collects (session, ledger, principal, project_id, request) and return
a dict that ends up in `CardAction.result_jsonb`.

This module is imported once at app lifespan startup; the registrations
populate `app.state.action_router`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_a2ui.action_router import ActionRouter, CardActionRequest
from aleph_a2ui.card_service import pin_card, unpin_card
from aleph_a2ui.catalog import CatalogValidationError, validate_component
from aleph_a2ui.models import InteractiveCard
from aleph_connectors.models import (
    ApprovalDecision,
    SynthesisProposal,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_notes.note_service import (
    update_note,
)
from aleph_notes.note_service import (
    update_section as update_note_section,
)
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.models import ApprovalRequest, ReviewFinding
from aleph_wiki.curator_service import CuratorService
from aleph_wiki.feedback_service import write_feedback
from aleph_wiki.handedit_service import clear_section, mark_section
from aleph_wiki.models import PageMergeProposal, WikiPage

# Agent actions an approval may execute on approve. The approve handler
# dispatches via this fixed map (route + how to shape the request body), never
# via arbitrary agent input — args were persisted server-side at request time.
_AGENT_ACTION_ROUTES: dict[str, str] = {
    "build_artifact": "/v1/projects/{project_id}/artifacts/build",
    "set_connector_enabled": "/v1/projects/{project_id}/connectors/bindings",
}

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _self_post(
    route: str,
    *,
    json: dict[str, Any],
    what: str,
    principal: Principal,
    project_id: UUID,
) -> dict[str, Any]:
    """POST to one of this API's own routes with a short-lived agent token (A4).

    Shared by the self-calling handlers (agent-action execution, note promote).
    Mints an HS256 agent token scoped to the acting analyst (`principal`), the
    project, and a fresh agent_run_id — the auth middleware verifies it in BOTH
    local and oidc mode (the old local-dev bearer sentinel authenticated only in
    local mode). The real route writes its own ledger + state in its own
    transaction.
    """
    import httpx

    from aleph_api.settings import get_settings
    from aleph_security.agent_token import mint_agent_token

    settings = get_settings()
    agent_run_id = uuid7()
    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=f"cards-selfcall-{agent_run_id.hex}",
        ttl_seconds=300,
    )
    base = settings.aleph_self_url
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}{route}", json=json, headers={"Authorization": f"Bearer {token}"}
        )
    if resp.status_code >= 400:
        msg = f"{what} failed ({resp.status_code}): {resp.text[:200]}"
        raise ValidationFailed(msg)
    return resp.json()


async def _execute_agent_action(
    *, project_id: UUID, tool: str, args: dict[str, Any], principal: Principal
) -> dict[str, Any]:
    """Execute an approved agent action by self-calling its underlying route.

    Dispatch is keyed by `_AGENT_ACTION_ROUTES` (a fixed allowlist); `tool` is
    validated by the caller. `args` were persisted server-side when the approval
    was requested, so this is not agent-emitted-executable.
    """
    route = _AGENT_ACTION_ROUTES[tool].format(project_id=project_id)
    return await _self_post(
        route, json=args, what=f"executing {tool}", principal=principal, project_id=project_id
    )


async def _approve(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    target_id = UUID(request.params["target_id"])
    target_kind = request.params["target_kind"]
    if target_kind == "synthesis_proposal":
        p = (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.id == target_id,
                    SynthesisProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if p is None:
            msg = f"proposal {target_id} not found"
            raise NotFound(msg)
        if p.status != "pending":
            msg = f"proposal already {p.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="synthesis_proposal",
            target_id=target_id,
            decision="approved",
            reason=None,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        p.status = "approved"
        p.approval_decision_id = decision.id
        page = await session.get(WikiPage, p.page_id)
        if page is not None:
            page.status = "approved"
        await session.flush()
        return {"target": str(target_id), "new_status": "approved"}
    if target_kind == "page_merge_proposal":
        mp = (
            await session.execute(
                select(PageMergeProposal).where(
                    PageMergeProposal.id == target_id,
                    PageMergeProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if mp is None:
            msg = f"merge proposal {target_id} not found"
            raise NotFound(msg)
        if mp.status != "pending":
            msg = f"merge proposal already {mp.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="page_merge_proposal",
            target_id=target_id,
            decision="approved",
            reason=None,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        # apply_merge redirects links, rewrites inbound bodies, aliases the
        # source, soft-deletes it, and appends its own wiki.page.merge event.
        await CuratorService(session, ledger=ledger).apply_merge(
            proposal=mp, principal=principal, ledger=ledger
        )
        mp.status = "approved"
        mp.approval_decision_id = decision.id
        await session.flush()
        return {"target": str(target_id), "new_status": "approved"}
    if target_kind == "agent_action":
        # Row-level lock: two concurrent approves of the same agent action must
        # serialize at the DB level. With FOR UPDATE the second txn blocks until
        # the first commits, then sees status != pending and no-ops below — so the
        # multi-second effect (build / connector toggle) executes exactly once.
        req = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.target_id == target_id,
                    ApprovalRequest.target_kind == "agent_action",
                    ApprovalRequest.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if req is None:
            msg = f"agent-action approval {target_id} not found"
            raise NotFound(msg)
        if req.status != "pending":
            # Already decided (e.g. a concurrent approve committed first).
            # No-op — do NOT re-execute the effect.
            return {"target": str(target_id), "new_status": req.status, "noop": True}
        patch: dict[str, Any] = req.proposed_patch_jsonb or {}
        tool = str(patch.get("tool") or "")
        args: dict[str, Any] = patch.get("args") or {}
        if tool not in _AGENT_ACTION_ROUTES:
            msg = f"agent action tool {tool!r} is not in the execution allowlist"
            raise ValidationFailed(msg)
        # Execute the real effect (self-call the underlying route) BEFORE marking
        # approved, so a failed execution leaves the request pending.
        executed = await _execute_agent_action(
            project_id=project_id, tool=tool, args=args, principal=principal
        )
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="agent_action",
            target_id=target_id,
            decision="approved",
            reason=None,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        req.status = "approved"
        req.decided_at = utcnow()
        req.decision_id = decision.id
        await session.flush()
        # Mirror approval_service.decide(): write the specific decision ledger
        # event (rule #4), in addition to the generic a2ui.action.approve event
        # the ActionRouter appends.
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="approval_request.approved",
            target_id=req.id,
            target_kind="approval_request",
            payload={"tool": tool, "target_kind": "agent_action"},
            trace_id=current_trace_id(),
        )
        return {
            "target": str(target_id),
            "new_status": "approved",
            "executed": executed,
        }
    if target_kind == "review_finding":
        finding = (
            await session.execute(
                select(ReviewFinding).where(
                    ReviewFinding.id == target_id,
                    ReviewFinding.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if finding is None:
            msg = f"finding {target_id} not found"
            raise NotFound(msg)
        if finding.status != "open":
            msg = f"finding already {finding.status}"
            raise ValidationFailed(msg)
        finding.status = "resolved"
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="review_finding.resolve",
            target_id=finding.id,
            target_kind="review_finding",
            payload={"title": finding.title, "finding_kind": finding.finding_kind},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "resolved"}
    if target_kind == "wiki_page":
        # Curation counterpart to the synthesis-proposal approval: an
        # agent-compiled draft has no proposal, so approve transitions the page
        # itself (mirrors routes/wiki.py::approve_page).
        page = (
            await session.execute(
                select(WikiPage).where(WikiPage.id == target_id, WikiPage.project_id == project_id)
            )
        ).scalar_one_or_none()
        if page is None:
            msg = f"wiki page not found: {target_id}"
            raise NotFound(msg)
        if page.status == "approved":
            msg = "page already approved"
            raise ValidationFailed(msg)
        prior = page.status
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="wiki_page",
            target_id=target_id,
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
            target_id=target_id,
            target_kind="wiki_page",
            payload={"prior_status": prior},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "approved"}
    msg = f"approve handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


async def _reject(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    target_id = UUID(request.params["target_id"])
    target_kind = request.params["target_kind"]
    reason = request.params["reason"]
    if target_kind == "synthesis_proposal":
        p = (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.id == target_id,
                    SynthesisProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if p is None:
            msg = f"proposal {target_id} not found"
            raise NotFound(msg)
        if p.status != "pending":
            msg = f"proposal already {p.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="synthesis_proposal",
            target_id=target_id,
            decision="rejected",
            reason=reason,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        p.status = "rejected"
        p.approval_decision_id = decision.id
        page = await session.get(WikiPage, p.page_id)
        if page is not None:
            page.status = "archived"
        await session.flush()
        await write_feedback(
            session,
            project_id=project_id,
            page_id=p.page_id,
            concept_name=p.topic,
            rejected_revision_id=p.revision_id,
            reason=reason,
            rejected_by=principal.user_id,
            ledger=ledger,
            actor_kind=principal.actor_kind,
        )
        return {"target": str(target_id), "new_status": "rejected"}
    if target_kind == "page_merge_proposal":
        mp = (
            await session.execute(
                select(PageMergeProposal).where(
                    PageMergeProposal.id == target_id,
                    PageMergeProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if mp is None:
            msg = f"merge proposal {target_id} not found"
            raise NotFound(msg)
        if mp.status != "pending":
            msg = f"merge proposal already {mp.status}"
            raise ValidationFailed(msg)
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="page_merge_proposal",
            target_id=target_id,
            decision="rejected",
            reason=reason,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        mp.status = "rejected"
        mp.approval_decision_id = decision.id
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="wiki.page.merge.reject",
            target_id=target_id,
            target_kind="page_merge_proposal",
            payload={"proposal_id": str(target_id), "reason": reason},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "rejected"}
    if target_kind == "agent_action":
        # Row-level lock for consistency with _approve — serialize concurrent
        # decisions on the same agent action at the DB level.
        req = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.target_id == target_id,
                    ApprovalRequest.target_kind == "agent_action",
                    ApprovalRequest.project_id == project_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if req is None:
            msg = f"agent-action approval {target_id} not found"
            raise NotFound(msg)
        if req.status != "pending":
            # Already decided by a concurrent decision — no-op.
            return {"target": str(target_id), "new_status": req.status, "noop": True}
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="agent_action",
            target_id=target_id,
            decision="rejected",
            reason=reason,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        req.status = "rejected"
        req.decided_at = utcnow()
        req.decision_id = decision.id
        # No execution on reject.
        await session.flush()
        # Mirror approval_service.decide(): write the specific decision ledger
        # event (rule #4), in addition to the generic a2ui.action.reject event.
        patch: dict[str, Any] = req.proposed_patch_jsonb or {}
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="approval_request.rejected",
            target_id=req.id,
            target_kind="approval_request",
            payload={"tool": str(patch.get("tool") or ""), "target_kind": "agent_action"},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "rejected"}
    if target_kind == "review_finding":
        finding = (
            await session.execute(
                select(ReviewFinding).where(
                    ReviewFinding.id == target_id,
                    ReviewFinding.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if finding is None:
            msg = f"finding {target_id} not found"
            raise NotFound(msg)
        if finding.status != "open":
            msg = f"finding already {finding.status}"
            raise ValidationFailed(msg)
        finding.status = "dismissed"
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="review_finding.dismiss",
            target_id=finding.id,
            target_kind="review_finding",
            payload={
                "title": finding.title,
                "finding_kind": finding.finding_kind,
                "reason": reason,
            },
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "dismissed"}
    if target_kind == "wiki_page":
        # Mirrors routes/wiki.py::reject_page — draft → archived, plus rejection
        # feedback the wiki agent reads on the next compile.
        page = (
            await session.execute(
                select(WikiPage).where(WikiPage.id == target_id, WikiPage.project_id == project_id)
            )
        ).scalar_one_or_none()
        if page is None:
            msg = f"wiki page not found: {target_id}"
            raise NotFound(msg)
        if page.status == "archived":
            msg = "page already archived"
            raise ValidationFailed(msg)
        prior = page.status
        decision = ApprovalDecision(
            id=uuid7(),
            project_id=project_id,
            target_kind="wiki_page",
            target_id=target_id,
            decision="rejected",
            reason=reason or None,
            decided_by=principal.user_id,
            decided_at=utcnow(),
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(decision)
        page.status = "archived"
        if reason:
            await write_feedback(
                session,
                project_id=project_id,
                page_id=target_id,
                concept_name=page.title,
                rejected_revision_id=page.current_revision_id,
                reason=reason,
                rejected_by=principal.user_id,
            )
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="wiki.page.reject",
            target_id=target_id,
            target_kind="wiki_page",
            payload={"prior_status": prior, "reason": reason or ""},
            trace_id=current_trace_id(),
        )
        return {"target": str(target_id), "new_status": "archived"}
    msg = f"reject handler not wired for target_kind={target_kind}"
    raise ValidationFailed(msg)


async def _unpin(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
) -> dict[str, Any]:
    card_id = UUID(request.params["card_id"])
    card = await unpin_card(session, project_id=project_id, card_id=card_id)
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="card.unpin",
        target_id=card.id,
        target_kind="interactive_card",
        payload={"card_kind": card.card_kind, "title": card.title or ""},
        trace_id=current_trace_id(),
    )
    return {"card_id": str(card_id), "pinned_to": None}


async def _open(
    *,
    session: AsyncSession,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Resolve an `open` target to a navigable workspace location.

    Returns `{navigate: {tab, page_id?, target_id, target_kind}}` — the
    frontend `adapt()` switches the right panel accordingly. Unknown kinds
    keep the legacy echo shape (no `tab`), which the frontend ignores.
    """
    target_id = UUID(request.params["target_id"])
    target_kind = str(request.params["target_kind"])
    nav: dict[str, Any] = {
        "target_id": str(target_id),
        "target_kind": target_kind,
    }
    if target_kind == "synthesis_proposal":
        p = (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.id == target_id,
                    SynthesisProposal.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if p is not None:
            nav["tab"] = "Wiki"
            nav["page_id"] = str(p.page_id)
    elif target_kind == "review_finding":
        finding = (
            await session.execute(
                select(ReviewFinding).where(
                    ReviewFinding.id == target_id,
                    ReviewFinding.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if finding is not None:
            nav["tab"] = "Wiki"
            if finding.target_page_id is not None:
                nav["page_id"] = str(finding.target_page_id)
    elif target_kind in ("wiki_page", "source_page"):
        nav["tab"] = "Wiki"
        nav["page_id"] = str(target_id)
    elif target_kind == "hypothesis":
        nav["tab"] = "Hypotheses"
    elif target_kind in ("artifact", "artifact_version"):
        nav["tab"] = "Library"
    elif target_kind == "note":
        nav["tab"] = "Notes"
    return {"navigate": nav}


async def _navigate_wiki(
    *,
    session: AsyncSession,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    # Resolve to a navigable location so the frontend `adapt()` opens the Wiki
    # tab on the target page (a wikilink click / agent `open_page` routes through
    # the ledger-audited router, not a client-only navigation). The agent may
    # pass a `slug` instead of a `page_id` (WP-4d); resolve it here.
    page_id = request.params.get("page_id")
    slug = request.params.get("slug")
    if not page_id and slug:
        resolved = (
            await session.execute(
                select(WikiPage.id).where(
                    WikiPage.project_id == project_id,
                    WikiPage.slug == str(slug),
                )
            )
        ).scalar_one_or_none()
        if resolved is None:
            msg = f"no wiki page with slug {slug!r} in this project"
            raise NotFound(msg)
        page_id = resolved
    if not page_id:
        msg = "navigate_wiki requires a page_id or a resolvable slug"
        raise ValidationFailed(msg)
    page_id = str(page_id)
    return {"page_id": page_id, "navigate": {"tab": "Wiki", "page_id": page_id}}


async def _focus_tab(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    """Switch the analyst's right-panel tab (WP-4d, folds the old `open_surface`).

    A navigation-only action — no domain mutation — but routed through the
    ledger-audited router like every other card action so the agent's UI moves
    are auditable. The frontend tool applies `navigate.tab` to `useWorkspaceUI`.
    """
    tab = str(request.params["tab"])
    return {"tab": tab, "navigate": {"tab": tab}}


async def _highlight_claim(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    """Highlight a claim in the open wiki reader (WP-4d).

    The frontend tool applies `highlight.claim_id` to `useWorkspaceUI`; the
    WikiPageCard rings the matching claim. Ledger-audited via the router.
    """
    claim_id = str(request.params["claim_id"])
    return {"claim_id": claim_id, "highlight": {"claim_id": claim_id}}


async def _compose_dossier(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Compose a derived, read-only Briefs card grouping referenced pages/cards.

    Persists a single catalog-validated `WikiPageCard` payload (marked
    `derived: true, read_only: true`) via `card_service.pin_card`, so it appears
    in Briefs and survives a surface rebuild. The dossier body links the grouped
    pages as `[[wikilinks]]` and lists the grouped cards. Ledger-audited (its own
    `card.compose_dossier` event feeds `pin_card`; the router appends the generic
    `a2ui.action.compose_dossier` event + CardAction row).
    """
    title = str(request.params["title"]).strip() or "Dossier"
    raw_page_ids: list[str] = request.params.get("page_ids") or []
    raw_card_ids: list[str] = request.params.get("card_ids") or []
    page_ids = [UUID(pid) for pid in raw_page_ids]
    card_ids = [UUID(cid) for cid in raw_card_ids]

    page_titles: dict[UUID, str] = {}
    if page_ids:
        pages = (
            (
                await session.execute(
                    select(WikiPage).where(
                        WikiPage.project_id == project_id,
                        WikiPage.id.in_(page_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        page_titles = {pg.id: pg.title for pg in pages}
    card_titles: dict[UUID, str] = {}
    if card_ids:
        grouped_cards = (
            (
                await session.execute(
                    select(InteractiveCard).where(
                        InteractiveCard.project_id == project_id,
                        InteractiveCard.id.in_(card_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        card_titles = {c.id: (c.title or "Untitled card") for c in grouped_cards}

    body_lines = [f"# {title}", ""]
    if page_ids:
        body_lines.append("## Pages")
        for pid in page_ids:
            body_lines.append(f"- [[{page_titles.get(pid, str(pid))}]]")
        body_lines.append("")
    if card_ids:
        body_lines.append("## Cards")
        for cid in card_ids:
            body_lines.append(f"- {card_titles.get(cid, str(cid))}")
        body_lines.append("")
    body_md = "\n".join(body_lines).strip() or f"# {title}"

    wikilinks_out = [
        {"dst_title": page_titles.get(pid, str(pid)), "dst_page_id": str(pid), "occurrences": 1}
        for pid in page_ids
    ]

    dossier_id = uuid7()
    payload: dict[str, Any] = {
        "type": "WikiPageCard",
        "id": f"dossier-{dossier_id}",
        "props": {
            "body_md": body_md,
            "wikilinks_out": wikilinks_out,
            "claims": [],
            "citations": [],
            "page_meta": {"title": title},
            "derived": True,
            "read_only": True,
            "dossier_refs": {
                "page_ids": [str(p) for p in page_ids],
                "card_ids": [str(c) for c in card_ids],
            },
        },
    }
    try:
        validate_component(payload)
    except CatalogValidationError as exc:
        msg = f"dossier payload rejected by catalog: {exc}"
        raise ValidationFailed(msg) from exc

    event = await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="card.compose_dossier",
        target_id=dossier_id,
        target_kind="interactive_card",
        payload={
            "title": title,
            "page_count": len(page_ids),
            "card_count": len(card_ids),
            "pinned_to": "briefs",
        },
        trace_id=current_trace_id(),
    )
    await pin_card(
        session,
        project_id=project_id,
        card_id=dossier_id,
        card_kind="WikiPageCard",
        title=title,
        payload=payload,
        author_id=principal.user_id,
        author_kind=principal.actor_kind,
        ledger_event_id=event.id,
        trace_id=current_trace_id(),
    )
    return {
        "card_id": str(dossier_id),
        "derived": True,
        "read_only": True,
        "page_count": len(page_ids),
        "card_count": len(card_ids),
    }


async def _spotlight(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Mark one Briefs card spotlighted (WP-4d) — a persisted bit the surface
    builder reads to order it first. Flips `InteractiveCard.spotlighted`,
    ledger-audited."""
    card_id = UUID(str(request.params["card_id"]))
    card = (
        await session.execute(
            select(InteractiveCard).where(
                InteractiveCard.id == card_id,
                InteractiveCard.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if card is None:
        msg = f"card {card_id} not found"
        raise NotFound(msg)
    card.spotlighted = True
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="card.spotlight",
        target_id=card_id,
        target_kind="interactive_card",
        payload={"card_kind": card.card_kind, "title": card.title or ""},
        trace_id=current_trace_id(),
    )
    return {"card_id": str(card_id), "spotlighted": True}


async def _repair_links(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    **_: Any,
) -> dict[str, Any]:
    """Resolve broken wikilinks via aliases (WP-4b reader affordance). Mirrors
    routes/aliases.py::repair_links; the AliasService writes its own ledger
    event when ≥1 link is repaired."""
    from aleph_wiki.alias_service import AliasService

    n = await AliasService(session, ledger).repair_broken_links(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
    )
    return {"repaired": n}


async def _rename_note(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Rename a note through the note service (NoteEditorCard title edit)."""
    note_id = UUID(request.params["note_id"])
    title = str(request.params["title"]).strip() or "Untitled note"
    n = await update_note(session, project_id=project_id, note_id=note_id, title=title)
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="note.rename",
        target_id=n.id,
        target_kind="note",
        payload={"title": n.title},
        trace_id=current_trace_id(),
    )
    return {"note_id": str(n.id), "title": n.title}


async def _promote_note(
    *,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Promote a note to a draft wiki page + Briefs proposal (NoteEditorCard).

    Self-calls the existing `…/notes/{id}/promote` route (the established
    `_execute_agent_action` pattern) so the promotion writes its own ledger +
    creates the SynthesisProposal in its own transaction — no logic duplication.
    The self-call carries a minted short-lived agent token (works in both auth
    modes), consistent with the other self-call handlers.
    """
    note_id = UUID(request.params["note_id"])
    route = f"/v1/projects/{project_id}/notes/{note_id}/promote"
    return await _self_post(
        route, json={}, what="promoting note", principal=principal, project_id=project_id
    )


async def _edit_note(
    *,
    session: AsyncSession,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    section_id = UUID(request.params["section_id"])
    body_md = request.params["body_md"]
    s = await update_note_section(
        session,
        project_id=project_id,
        section_id=section_id,
        body_md=body_md,
    )
    return {"section_id": str(s.id), "ordinal": s.ordinal}


async def _mark_handedit(
    *,
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    page_id = UUID(request.params["page_id"])
    anchor = request.params["section_anchor"]
    m = await mark_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        applied_by=principal.user_id,
    )
    return {"mark_id": str(m.id)}


async def _clear_handedit(
    *,
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    page_id = UUID(request.params["page_id"])
    anchor = request.params["section_anchor"]
    n = await clear_section(
        session,
        project_id=project_id,
        page_id=page_id,
        section_anchor=anchor,
        cleared_by=principal.user_id,
    )
    return {"cleared": n}


async def _submit_form(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    return {
        "form_id": request.params["form_id"],
        "values": request.params["values"],
    }


async def _create_hypothesis(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Route the Hypotheses tab's "+ New" through the existing hypothesis
    service (WP-4: surface mutations flow through the ledger-audited router
    instead of a component `useMutation`)."""
    from aleph_hypotheses.hypothesis_service import create_hypothesis

    title = str(request.params["title"]).strip()
    statement = str(request.params["statement"]).strip()
    if not title or not statement:
        msg = "create_hypothesis requires non-empty title and statement"
        raise ValidationFailed(msg)
    h = await create_hypothesis(
        session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        title=title,
        statement=statement,
    )
    return {"hypothesis_id": str(h.id), "short_id": h.short_id}


async def _create_note(
    *,
    session: AsyncSession,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Route the Notes tab's "+ New" through the existing note service, seeding
    one empty section so the bound editor has a `section_id` to `edit_note`."""
    from aleph_notes.note_service import create_note, create_section

    title = str(request.params.get("title") or "Untitled note").strip() or "Untitled note"
    note = await create_note(
        session,
        project_id=project_id,
        title=title,
        created_by=principal.user_id,
    )
    section = await create_section(
        session,
        project_id=project_id,
        note_id=note.id,
        body_md="",
        anchor=None,
        created_by=principal.user_id,
    )
    return {"note_id": str(note.id), "section_id": str(section.id), "title": note.title}


async def _feedback(
    *,
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    request: CardActionRequest,
    **_: Any,
) -> dict[str, Any]:
    """Route surface-card feedback through the shared feedback writer (WP-4:
    the FeedbackButton's `useMutation` became an `onAction("feedback", …)`)."""
    from aleph_api.feedback_writer import record_feedback

    fb = await record_feedback(
        session,
        ledger,
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        target_kind=str(request.params["target_kind"]),
        target_id=UUID(str(request.params["target_id"])),
        signal=str(request.params["signal"]),
        note=str(request.params.get("note") or ""),
        severity=request.params.get("severity"),
        context=request.params.get("context") or {},
    )
    return {"feedback_id": str(fb.id), "signal": fb.signal}


async def _clarify(*, request: CardActionRequest, **_: Any) -> dict[str, Any]:
    # Inc 3 wired the research clarifier loop; this handler is the surface end.
    return {
        "agent_run_id": request.params["agent_run_id"],
        "answer_length": len(request.params["answer"]),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build_action_router() -> ActionRouter:
    r = ActionRouter()
    r.register("approve", _approve)
    r.register("reject", _reject)
    r.register("open", _open)
    r.register("unpin", _unpin)
    r.register("navigate_wiki", _navigate_wiki)
    r.register("submit_form", _submit_form)
    r.register("create_hypothesis", _create_hypothesis)
    r.register("create_note", _create_note)
    r.register("feedback", _feedback)
    r.register("edit_note", _edit_note)
    r.register("clarify", _clarify)
    r.register("mark_handedit", _mark_handedit)
    r.register("clear_handedit", _clear_handedit)
    r.register("repair_links", _repair_links)
    r.register("rename_note", _rename_note)
    r.register("promote_note", _promote_note)
    # WP-4d agent eyes+hands: navigation + composition verbs.
    r.register("focus_tab", _focus_tab)
    r.register("highlight_claim", _highlight_claim)
    r.register("compose_dossier", _compose_dossier)
    r.register("spotlight", _spotlight)
    return r
