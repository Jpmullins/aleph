"""wiki_refresh_job + refresh_stale_pages_job (WP-6 §3).

``wiki_refresh_job`` re-checks a wiki page's trust without ever recompiling it:

1. Resolve the page's *contributing sources* via the blast-radius join
   (current-revision ``WikiClaim → Citation → SourcePage → Source``).
2. For each source, re-fetch (``_refetch_source_markdown``), read the prior
   ``NormalizedDocument`` markdown, and **fact-diff** the two via
   ``LiteLLMClient.chat(capability=CLASSIFICATION, purpose="wiki.refresh.factdiff")``
   → a verdict ``unchanged | updated | contradicted``. A fetch failure is
   ``unreachable`` with **no** LLM call.
3. Emit exactly **one** ``ApprovalCard`` (``target_kind="refresh_result"``,
   ``target_id=page_id``), pinned to Briefs via ``card_service.pin_card`` so it
   survives a surface rebuild. Approve/skip → ``WikiPage.verified_at = now``;
   flag (reject) → the page's cited claims drop to ``confidence="contested"``.
   Handled in ``aleph_api.a2ui_handlers``. The job **never** recompiles.

``refresh_stale_pages_job`` is the bounded scheduler pass: it picks the stalest
pages (low freshness + ``verified_at`` past the volatility half-life), caps the
fan-out, and enqueues one ``wiki_refresh_job`` per page with a minted agent token.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import or_, select

from aleph_core.ids import uuid7
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import ChatMessage
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.models import NormalizedDocument, Source, SourceAsset, SourceVersion
from aleph_rks.normalization import normalize_bytes
from aleph_security.agent_token import mint_agent_token, verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.models import Citation, SourcePage, WikiClaim, WikiPage
from aleph_workers.gateway import gateways

_log = structlog.get_logger(__name__)

#: Freshness half-life (days) per volatility class — mirrors
#: ``aleph_wiki.freshness`` (hot 30d / warm 90d / cold 365d). Used only by the
#: scheduler's staleness gate.
_HALFLIFE_DAYS: dict[str, float] = {"hot": 30.0, "warm": 90.0, "cold": 365.0}

#: Scheduler staleness gate + fan-out cap.
_STALE_FRESHNESS_MAX = 60
_MAX_FANOUT = 10

#: Markdown budget (chars) per side of the fact-diff prompt.
_DIFF_CHAR_BUDGET = 6000

_VALID_VERDICTS = {"unchanged", "updated", "contradicted"}

_FACTDIFF_SYSTEM = (
    "You compare an OLD and a NEW version of a source document that backs wiki "
    "claims. Judge whether the NEW version changes the facts the OLD asserted. "
    'Reply ONLY with JSON: {"verdict": "<v>"} where <v> is exactly one of: '
    '"unchanged" (substantively the same facts), "updated" (new or changed facts '
    'that do not contradict the old), or "contradicted" (the new version '
    "contradicts facts the old asserted)."
)


async def _refetch_source_markdown(
    source: Source, asset_store: Any, *, asset: SourceAsset | None
) -> str | None:
    """Re-fetch a source and return freshly-normalized markdown, or ``None`` if
    unreachable.

    URL-backed sources are re-fetched over HTTP; upload/asset-backed sources are
    re-read from the asset store (their upstream cannot change, so this yields
    the stored bytes). Any fetch/normalize failure → ``None`` (``unreachable``).
    Isolated as a module function so tests can substitute a deterministic fetch.
    """
    url = source.url
    try:
        if url and (url.startswith("http://") or url.startswith("https://")):
            import httpx

            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
            resp.raise_for_status()
            mime = (resp.headers.get("content-type") or "text/html").split(";")[0].strip()
            return normalize_bytes(resp.content, mime).markdown
        if asset is not None:
            data = asset_store.get(asset.storage_uri, expected_sha256=asset.sha256)
            return normalize_bytes(data, asset.mime_type).markdown
    except Exception:
        return None
    return None


async def _classify_factdiff(
    *,
    litellm: Any,
    principal: Principal,
    project_id: UUID,
    agent_run_id: UUID | None,
    profile_bindings: dict[str, Any],
    prior_md: str,
    new_md: str,
) -> str:
    """LLM fact-diff → ``unchanged | updated | contradicted`` (rule 5)."""
    resp = await litellm.chat(
        principal=principal,
        project_id=project_id,
        agent_run_id=agent_run_id,
        capability=Capability.CLASSIFICATION,
        profile_bindings=profile_bindings,
        messages=[
            ChatMessage(role="system", content=_FACTDIFF_SYSTEM),
            ChatMessage(
                role="user",
                content=(
                    f"OLD:\n{prior_md[:_DIFF_CHAR_BUDGET]}\n\nNEW:\n{new_md[:_DIFF_CHAR_BUDGET]}"
                ),
            ),
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=256,
        purpose="wiki.refresh.factdiff",
    )
    if not resp.choices:
        return "updated"
    try:
        verdict = str(json.loads(resp.choices[0].message.content or "{}").get("verdict") or "")
    except json.JSONDecodeError:
        return "updated"
    return verdict if verdict in _VALID_VERDICTS else "updated"


async def _contributing_sources(session: Any, page: WikiPage) -> dict[UUID, list[UUID]]:
    """``source_id -> [claim_id, ...]`` for the page's current-revision claims.

    The freshness/retraction blast-radius join, forward:
    ``WikiClaim(current rev) → Citation → SourcePage → Source``."""
    out: dict[UUID, list[UUID]] = {}
    if page.current_revision_id is None:
        return out
    claims = list(
        (
            await session.execute(
                select(WikiClaim).where(WikiClaim.revision_id == page.current_revision_id)
            )
        )
        .scalars()
        .all()
    )
    for claim in claims:
        cites = list(
            (await session.execute(select(Citation).where(Citation.claim_id == claim.id)))
            .scalars()
            .all()
        )
        for cite in cites:
            if cite.source_page_id is None:
                continue
            sp = await session.get(SourcePage, cite.source_page_id)
            if sp is None:
                continue
            out.setdefault(sp.source_id, [])
            if claim.id not in out[sp.source_id]:
                out[sp.source_id].append(claim.id)
    return out


_SEVERITY_BY_WORST = {
    "contradicted": "high",
    "updated": "medium",
    "unreachable": "low",
    "unchanged": "info",
}


async def wiki_refresh_job(
    ctx: dict[str, Any], project_id: str, page_id: str, agent_token: str
) -> dict[str, Any]:
    """Re-check one wiki page's sources and emit a `refresh_result` ApprovalCard.

    Never recompiles the page (markdown/claims unchanged); the analyst decides
    via approve-skip (re-affirm) or flag (reject) on the card.
    """
    from aleph_a2ui.card_service import pin_card
    from aleph_a2ui.components.cards import ApprovalCardProps, approval_card

    secret: str = ctx["agent_token_secret"]
    claims_tok = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims_tok.user_id,
        subject="agent",
        email="",
        actor_kind=claims_tok.actor_kind,
        agent_run_id=claims_tok.agent_run_id,
        correlation_id=claims_tok.correlation_id,
    )
    pid = UUID(project_id)
    page_uuid = UUID(page_id)
    maker = ctx["session_maker"]
    litellm = await gateways(ctx).litellm(pid)
    asset_store = ctx["asset_store"]

    with start_span(
        "worker.wiki_refresh", **{"aleph.project_id": project_id, "aleph.page_id": page_id}
    ):
        # Resolve page, contributing sources, and the model profile.
        async with maker() as session:
            page = (
                await session.execute(
                    select(WikiPage).where(WikiPage.id == page_uuid, WikiPage.project_id == pid)
                )
            ).scalar_one_or_none()
            if page is None:
                return {"ok": False, "error": "page not found"}
            page_title = page.title
            contributing = await _contributing_sources(session, page)
            profile = (
                await session.execute(select(ModelProfile).where(ModelProfile.project_id == pid))
            ).scalar_one_or_none()
            profile_bindings: dict[str, Any] = profile.bindings_jsonb if profile is not None else {}

            # Ensure the AgentRun the token names exists + is running.
            run_id = claims_tok.agent_run_id or uuid7()
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one_or_none()
            if run is None:
                session.add(
                    AgentRun(
                        id=run_id,
                        project_id=pid,
                        agent_kind="wiki_refresh",
                        correlation_id=claims_tok.correlation_id or f"wiki-refresh-{run_id.hex}",
                        status="running",
                        input_payload={"page_id": page_id},
                        created_by=principal.user_id,
                    )
                )
            else:
                run.status = "running"
                run.started_at = utcnow()
            await session.commit()

            # Snapshot each source's current version markdown + asset (prior).
            source_ctx: list[tuple[Source, str | None, SourceAsset | None]] = []
            for source_id in contributing:
                src = await session.get(Source, source_id)
                if src is None:
                    continue
                prior_md: str | None = None
                asset: SourceAsset | None = None
                if src.current_version_id is not None:
                    ver = await session.get(SourceVersion, src.current_version_id)
                    if ver is not None:
                        if ver.asset_id is not None:
                            asset = await session.get(SourceAsset, ver.asset_id)
                        if ver.normalized_document_id is not None:
                            nd = await session.get(NormalizedDocument, ver.normalized_document_id)
                            if nd is not None:
                                try:
                                    prior_md = asset_store.get(nd.markdown_uri).decode(
                                        "utf-8", "replace"
                                    )
                                except Exception:
                                    prior_md = None
                source_ctx.append((src, prior_md, asset))

        # Re-fetch + classify per source (LLM calls outside the DB session).
        verdicts: list[dict[str, Any]] = []
        for src, prior_md, asset in source_ctx:
            new_md = await _refetch_source_markdown(src, asset_store, asset=asset)
            if new_md is None or prior_md is None:
                verdict = "unreachable"
            else:
                verdict = await _classify_factdiff(
                    litellm=litellm,
                    principal=principal,
                    project_id=pid,
                    agent_run_id=run_id,
                    profile_bindings=profile_bindings,
                    prior_md=prior_md,
                    new_md=new_md,
                )
            verdicts.append(
                {
                    "source_id": str(src.id),
                    "short_id": src.short_id,
                    "title": src.title,
                    "verdict": verdict,
                    "claim_ids": [str(c) for c in contributing.get(src.id, [])],
                }
            )

        worst = "unchanged"
        for order in ("contradicted", "updated", "unreachable", "unchanged"):
            if any(v["verdict"] == order for v in verdicts):
                worst = order
                break
        severity = _SEVERITY_BY_WORST.get(worst, "info")
        changed = [v for v in verdicts if v["verdict"] in ("updated", "contradicted")]
        summary_parts = [f"{v['short_id']}: {v['verdict']}" for v in verdicts] or [
            "no contributing sources to re-check"
        ]
        summary = (
            f"Refresh of “{page_title}”: "
            + "; ".join(summary_parts)
            + (
                ". Approve to re-affirm; flag (reject) to mark the page's claims contested."
                if verdicts
                else "."
            )
        )[:1000]

        # Emit exactly one refresh_result ApprovalCard, pinned to Briefs.
        card_id = uuid7()
        payload = approval_card(
            ApprovalCardProps(
                target_id=page_uuid,
                target_kind="refresh_result",
                title=f"Refresh: {page_title}"[:200],
                summary=summary,
                severity=severity,
                evidence_refs=[
                    {"kind": "source", "id": v["source_id"], "label": v["short_id"]}
                    for v in verdicts
                ],
            ),
            card_id=f"refresh-{card_id}",
        )
        # Extra (additionalProperties) trust context for the reader + auditors.
        payload["props"]["page_id"] = str(page_uuid)
        payload["props"]["verdicts"] = verdicts
        payload["props"]["changed"] = bool(changed)

        async with maker() as session:
            ledger = LedgerWriter(session)
            event = await ledger.append(
                project_id=pid,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="wiki.refresh.completed",
                target_id=page_uuid,
                target_kind="wiki_page",
                payload={"verdicts": verdicts, "worst": worst},
                trace_id=current_trace_id(),
            )
            await pin_card(
                session,
                project_id=pid,
                card_id=card_id,
                card_kind="ApprovalCard",
                title=f"Refresh: {page_title}",
                payload=payload,
                author_id=principal.user_id,
                author_kind=principal.actor_kind,
                ledger_event_id=event.id,
                trace_id=current_trace_id(),
            )
            await session.commit()

        async with maker() as session:
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one_or_none()
            if run is not None:
                run.status = "succeeded"
                run.completed_at = utcnow()
                run.result_payload = {"card_id": str(card_id), "worst": worst}
                await session.commit()

    _log.info("wiki.refresh.done", project_id=project_id, page_id=page_id, worst=worst)
    return {"ok": True, "card_id": str(card_id), "worst": worst, "verdicts": verdicts}


async def refresh_stale_pages_job(ctx: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Bounded scheduler pass: enqueue ``wiki_refresh_job`` for the stalest pages.

    Picks pages whose ``freshness`` is below ``_STALE_FRESHNESS_MAX`` and whose
    ``verified_at`` is older than the volatility half-life (or never verified),
    caps the fan-out at ``_MAX_FANOUT`` (stalest first), and enqueues one refresh
    job per page with a freshly-minted short-lived agent token.
    """
    pid = UUID(project_id)
    maker = ctx["session_maker"]
    redis_pool = ctx.get("redis_pool")
    secret: str = ctx["agent_token_secret"]
    now = utcnow()

    async with maker() as session:
        project = (
            await session.execute(select(Project).where(Project.id == pid))
        ).scalar_one_or_none()
        if project is None:
            return {"enqueued": 0}
        owner = project.created_by
        candidates = list(
            (
                await session.execute(
                    select(WikiPage)
                    .where(
                        WikiPage.project_id == pid,
                        WikiPage.is_stub.is_(False),
                        or_(
                            WikiPage.freshness.is_(None),
                            WikiPage.freshness < _STALE_FRESHNESS_MAX,
                        ),
                    )
                    .order_by(WikiPage.freshness.asc().nullsfirst())
                )
            )
            .scalars()
            .all()
        )

    stale: list[WikiPage] = []
    for page in candidates:
        halflife = _HALFLIFE_DAYS.get(page.volatility, _HALFLIFE_DAYS["warm"])
        if page.verified_at is None or (now - page.verified_at) > timedelta(days=halflife):
            stale.append(page)
        if len(stale) >= _MAX_FANOUT:
            break

    enqueued = 0
    if redis_pool is not None:
        for page in stale:
            run_id = uuid7()
            token = mint_agent_token(
                secret=secret,
                user_id=owner,
                project_id=pid,
                agent_run_id=run_id,
                actor_kind="aleph_agent",
                correlation_id=f"wiki-refresh-{run_id.hex}",
                ttl_seconds=3600,
            )
            await redis_pool.enqueue_job("wiki_refresh_job", str(pid), str(page.id), token)
            enqueued += 1

    _log.info("wiki.refresh.scheduled", project_id=project_id, enqueued=enqueued)
    return {"enqueued": enqueued}
