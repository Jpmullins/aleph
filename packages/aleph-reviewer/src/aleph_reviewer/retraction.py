"""Source retraction + blast-radius (WP-6 §4).

``retract_source`` is the single path all retraction triggers funnel through —
the manual EDITOR ``POST .../retract`` route, and the WP-2 reviewer
``doi_verification`` node's scholar-detected retractions. It:

- sets ``Source.status="retracted"`` + ``retracted_at`` + ``retraction_reason``
  and writes a ``source.retract`` ledger event;
- walks the **blast-radius join** (the reviewer's ``_registry_sources``
  inverted) — ``Source → SourcePage → Citation → WikiClaim`` — and for every
  dependent claim sets ``status="retracted"`` (confidence stays derived) with a
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

from sqlalchemy import select, text

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.review_service import add_finding, finalize_run, start_run
from aleph_rks.models import Source
from aleph_wiki.models import Citation, WikiClaim

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


@dataclass(frozen=True)
class RetractionImpact:
    """What a retraction actually does to the belief graph.

    The distinction is the point. A claim whose only support was the retracted
    source loses its footing; a claim that also rests on independent evidence
    does not, and flagging both identically would train a reader to ignore the
    flag. This is the "declined" branch — the belief survives, annotated that
    one of its supports was withdrawn.
    """

    directly_cited: set[UUID]
    derived: set[UUID]
    unsupported: set[UUID]
    weakened: set[UUID]

    @property
    def all_touched(self) -> set[UUID]:
        return self.directly_cited | self.derived


#: How far a retraction propagates along `derived_from`. Bounded because a
#: cycle or a pathological chain must not turn one retraction into a full-table
#: walk; the CTE also guards cycles explicitly.
MAX_DERIVATION_DEPTH = 4


async def retraction_impact(session: AsyncSession, source_id: UUID) -> RetractionImpact:
    """Walk the belief graph from a retracted source.

    Two hops, not one:

    1. Claims citing the source directly (``citations.source_id``).
    2. Claims transitively ``derived_from`` those, to a bounded depth. A
       conclusion built on a claim built on a retracted paper is also affected,
       and a citation lookup cannot see that.

    Then the declined branch: of everything touched, only claims left with NO
    surviving supporting citation are `unsupported`. The rest are `weakened`.

    Replaces a join through ``Citation.source_page_id``, a column no production
    write path ever populated — so retraction blast-radius returned zero rows
    for the lifetime of the feature.
    """
    direct_rows = (
        (
            await session.execute(
                select(Citation.claim_id).where(Citation.source_id == source_id).distinct()
            )
        )
        .scalars()
        .all()
    )
    directly_cited = set(direct_rows)

    derived: set[UUID] = set()
    if directly_cited:
        # Recursive walk over derived_from, depth-capped and cycle-guarded by
        # excluding anything already visited on the path.
        walk = text(
            """
            WITH RECURSIVE downstream(claim_id, depth, path) AS (
                SELECT e.src_claim_id, 1, ARRAY[e.dst_claim_id, e.src_claim_id]
                FROM claim_edges e
                WHERE e.kind = 'derived_from'
                  AND e.dst_claim_id = ANY(:roots)
                UNION ALL
                SELECT e.src_claim_id, d.depth + 1, d.path || e.src_claim_id
                FROM claim_edges e
                JOIN downstream d ON e.dst_claim_id = d.claim_id
                WHERE e.kind = 'derived_from'
                  AND d.depth < :max_depth
                  AND NOT (e.src_claim_id = ANY(d.path))
            )
            SELECT DISTINCT claim_id FROM downstream
            """
        )
        rows = await session.execute(
            walk,
            {"roots": list(directly_cited), "max_depth": MAX_DERIVATION_DEPTH},
        )
        derived = {row[0] for row in rows.all()} - directly_cited

    touched = directly_cited | derived
    if not touched:
        return RetractionImpact(set(), set(), set(), set())

    # The declined branch: does anything else still support it?
    surviving = (
        (
            await session.execute(
                select(Citation.claim_id)
                .where(
                    Citation.claim_id.in_(touched),
                    Citation.stance == "supports",
                    Citation.source_id.is_not(None),
                    Citation.source_id != source_id,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    still_supported = set(surviving)

    return RetractionImpact(
        directly_cited=directly_cited,
        derived=derived,
        unsupported=touched - still_supported,
        weakened=touched & still_supported,
    )


async def dependent_claims(session: AsyncSession, source_id: UUID) -> list[tuple[UUID, UUID]]:
    """``(page_id, claim_id)`` for every claim a retraction touches.

    Kept for existing callers; the structured answer is `retraction_impact`.
    """
    impact = await retraction_impact(session, source_id)
    if not impact.all_touched:
        return []
    rows = (
        await session.execute(
            select(WikiClaim.page_id, WikiClaim.id).where(WikiClaim.id.in_(impact.all_touched))
        )
    ).all()
    return [(page_id, claim_id) for page_id, claim_id in rows]


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
            # `status` carries the retraction; `confidence` does not. Confidence
            # is DERIVED from the evidence by
            # `aleph_hypotheses.confidence.next_confidence_from_evidence`, and
            # "retracted" is not one of its states — writing it here would put a
            # value in the column that the state machine can never produce, so
            # the next recompute would silently erase it. A claim whose support
            # was withdrawn is `retracted` in status; what it is now worth is
            # whatever its remaining evidence says.
            claim.status = "retracted"
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
