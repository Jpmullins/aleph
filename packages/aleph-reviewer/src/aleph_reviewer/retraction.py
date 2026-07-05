"""Source retraction + blast-radius (WP-6 §4).

``retract_source`` is the single path all retraction triggers funnel through —
the manual EDITOR ``POST .../retract`` route, and the WP-2 reviewer
``doi_verification`` node's scholar-detected retractions. It:

- sets ``Source.status="retracted"`` + ``retracted_at`` + ``retraction_reason``
  and writes a ``source.retract`` ledger event;
- walks the **blast-radius join** (the reviewer's ``_registry_sources``
  inverted) — ``Source → SourcePage → Citation → WikiClaim`` — and for every
  dependent claim sets ``confidence="retracted"`` / ``status="contested"`` with a
  per-claim ``wiki_claim.retract_flag`` ledger event;
- emits a ``retracted_source`` ``ReviewFinding`` (severity critical) under a
  minimal ``kind="retraction"`` ``ReviewRun`` so it lands in Briefs — the same
  finding kind the WP-2 reviewer emits.

``dependent_claims`` exposes the queryable blast-radius set. Every mutation is
in the caller's transaction (rule 4); the caller commits.

Layering note: this lives in ``aleph_reviewer`` — the highest of the three
packages it touches (it depends on ``aleph_rks`` for ``Source``, ``aleph_wiki``
for the claim/citation tables, and uses its own ``review_service`` for the
finding). Placing it here keeps the strict higher→lower DAG intact (``aleph_rks``
is a low leaf and must not import ``aleph_wiki``/``aleph_reviewer``). Both
callers — the reviewer's ``doi_verification`` retracted branch (same package)
and the manual ``POST .../retract`` route (``aleph_api``, above everything) —
share this one code path. Every mutation is in the caller's transaction
(rule 4); the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.review_service import add_finding, finalize_run, start_run
from aleph_rks.models import Source
from aleph_wiki.models import Citation, SourcePage, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


@dataclass(frozen=True)
class RetractionResult:
    """Blast-radius summary returned by ``retract_source``."""

    source_id: UUID
    page_ids: set[UUID]
    claim_ids: set[UUID]
    finding_id: UUID | None = None
    already_retracted: bool = False


async def dependent_claims(session: AsyncSession, source_id: UUID) -> list[tuple[UUID, UUID]]:
    """The queryable blast-radius: ``(page_id, claim_id)`` for every wiki claim
    that cites ``source_id``.

    Join (the reviewer's ``_registry_sources`` inverted):
    ``Source → SourcePage(source_id) → Citation(source_page_id=SourcePage.id)
    → WikiClaim(id=Citation.claim_id)``.
    """
    stmt = (
        select(WikiClaim.page_id, WikiClaim.id)
        .join(Citation, Citation.claim_id == WikiClaim.id)
        .join(SourcePage, SourcePage.id == Citation.source_page_id)
        .where(SourcePage.source_id == source_id)
    )
    out: list[tuple[UUID, UUID]] = []
    seen: set[tuple[UUID, UUID]] = set()
    for page_id, claim_id in (await session.execute(stmt)).all():
        key = (page_id, claim_id)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def retract_source(
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    *,
    source_id: UUID,
    reason: str,
) -> RetractionResult:
    """Retract ``source_id`` and flag every dependent wiki claim.

    Idempotent: a source already retracted is not re-ledgered — the current
    blast-radius is returned with ``already_retracted=True``.
    """
    source = (
        await session.execute(select(Source).where(Source.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        msg = f"source not found: {source_id}"
        raise NotFound(msg)

    project_id = source.project_id
    trace = current_trace_id()

    if source.retracted_at is not None:
        deps = await dependent_claims(session, source_id)
        return RetractionResult(
            source_id=source_id,
            page_ids={p for p, _ in deps},
            claim_ids={c for _, c in deps},
            already_retracted=True,
        )

    # 1. Retract the source itself.
    source.status = "retracted"
    source.retracted_at = utcnow()
    source.retraction_reason = reason
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="source.retract",
        target_id=source.id,
        target_kind="source",
        payload={"short_id": source.short_id, "reason": reason},
        trace_id=trace,
    )

    # 2. Walk the blast-radius join and flag every dependent claim.
    deps = await dependent_claims(session, source_id)
    page_ids: set[UUID] = set()
    claim_ids: set[UUID] = set()
    claim_id_list = [c for _, c in deps]
    if claim_id_list:
        claims = list(
            (await session.execute(select(WikiClaim).where(WikiClaim.id.in_(claim_id_list))))
            .scalars()
            .all()
        )
        for claim in claims:
            claim.confidence = "retracted"
            claim.status = "contested"
            page_ids.add(claim.page_id)
            claim_ids.add(claim.id)
            await ledger.append(
                project_id=project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="wiki_claim.retract_flag",
                target_id=claim.id,
                target_kind="wiki_claim",
                payload={"source_id": str(source_id), "page_id": str(claim.page_id)},
                trace_id=trace,
            )

    # 3. Emit a critical `retracted_source` finding under a minimal run so it
    #    lands in Briefs — the same finding kind the WP-2 reviewer emits.
    run = await start_run(
        session,
        project_id=project_id,
        kind="retraction",
        trigger="source_retract",
        target_revision_id=None,
        target_scope="source",
        agent_run_id=principal.agent_run_id or uuid7(),
        created_by=principal.user_id,
    )
    finding = await add_finding(
        session,
        review_run_id=run.id,
        project_id=project_id,
        finding_kind="retracted_source",
        severity="critical",
        title=f"Retracted source: {source.short_id}",
        description=(
            f"Source {source.short_id} ({source.title}) was retracted: {reason}. "
            f"{len(claim_ids)} dependent claim(s) across {len(page_ids)} page(s) "
            "flagged retracted/contested."
        ),
        target_source_id=source.id,
        evidence_refs=[
            {"kind": "source", "source_id": str(source.id), "short_id": source.short_id}
        ],
        auto_resolvable=False,
        created_by=principal.user_id,
    )
    await finalize_run(session, run_id=run.id, status="completed", finding_count=1)

    return RetractionResult(
        source_id=source_id,
        page_ids=page_ids,
        claim_ids=claim_ids,
        finding_id=finding.id,
    )
