"""Writing ``derived_from`` — the edge that makes retraction a graph walk.

``aleph_reviewer.retraction.retraction_impact`` has always contained a
depth-capped, cycle-guarded recursive CTE over
``claim_edges.kind = 'derived_from'``: the second hop, a conclusion built on a
claim built on the retracted paper. It has never returned a row, because nothing
in the tree wrote such an edge. ``ClaimEdge`` was constructed in exactly one
place — ``BeliefService.supersede`` — with ``kind="supersedes"``, and
``claim_edges`` held two rows of that kind and zero of every other. The walk was
correct and blind: retraction reported success while reaching one hop out of two.

This is the writer. It is deliberately a small module rather than a method on
``BeliefService``: recording a derivation is not part of deriving confidence,
the two are called from different places, and a caller that only wants to say
"claim B stands on claim A" should not have to construct the belief write path
to do it.

**What it refuses, and why each refusal is load-bearing.**

*Self-loops.* ``claim_edges`` has a CHECK constraint against them; hitting it
aborts the caller's whole transaction, so an extractor that emits a claim as its
own parent would cost a page its commit rather than one edge.

*Cross-project parents.* An edge from a claim in project A to a claim in project
B makes ``retraction_impact`` walk out of the project it was called for, and the
retraction path has no project predicate on the CTE — it trusts the edges. A
leaked edge is a leaked blast radius. ``tests/e2e/test_retraction_walk.py``
pins that retraction does not reach across projects; this is the write-side half
of the same guarantee.

*Duplicates.* Re-deriving the same page must union rather than accumulate, so
the insert is ``ON CONFLICT DO NOTHING`` against the ``(src, dst, kind)`` unique
index. Idempotence is what lets a rebuild run twice.

Every accepted edge is ledgered in the caller's transaction (rule 4); the caller
commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id, start_span
from aleph_wiki.models import ClaimEdge, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

__all__ = ["DERIVED_FROM", "record_derivations"]

#: The edge kind. Named because it is spelled in three places that cannot import
#: each other: this writer, the recursive CTE in `aleph_reviewer.retraction`, and
#: the acceptance query. A typo in any one of them is silent.
DERIVED_FROM = "derived_from"


async def record_derivations(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    claim_id: UUID,
    derived_from: list[UUID],
    weight: float = 1.0,
    rationale: str = "",
) -> list[UUID]:
    """Record that ``claim_id`` stands on each of ``derived_from``.

    Returns the parent ids for which an edge now exists — including ones that
    already existed, because the caller's question is "is the dependency
    recorded", not "did I win the race to record it". Ids that were refused
    (self, missing, another project's) are absent from the result, so a caller
    that wants to know can compare lengths.
    """
    wanted = [p for p in dict.fromkeys(derived_from) if p != claim_id]
    if not wanted:
        return []

    with start_span(
        "belief.record_derivations",
        **{"aleph.project_id": str(project_id), "aleph.claim_id": str(claim_id)},
    ) as span:
        # Both endpoints must be live claims of THIS project. Checked in one
        # query rather than trusted: `claim_edges` has foreign keys to
        # `wiki_claims` but no project predicate, so the database would happily
        # accept an edge that walks out of the project.
        valid = set(
            (
                await session.execute(
                    select(WikiClaim.id).where(
                        WikiClaim.id.in_([*wanted, claim_id]),
                        WikiClaim.project_id == project_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if claim_id not in valid:
            span.set_attribute("aleph.derivations.written", 0)
            return []
        parents = [p for p in wanted if p in valid]
        if not parents:
            span.set_attribute("aleph.derivations.written", 0)
            return []

        stmt = (
            insert(ClaimEdge)
            .values(
                [
                    {
                        "id": uuid7(),
                        "project_id": project_id,
                        "src_claim_id": claim_id,
                        "dst_claim_id": parent,
                        "kind": DERIVED_FROM,
                        "weight": weight,
                        "rationale": rationale,
                        "created_by": principal.user_id,
                    }
                    for parent in parents
                ]
            )
            .on_conflict_do_nothing(index_elements=["src_claim_id", "dst_claim_id", "kind"])
        )
        await session.execute(stmt)
        await session.flush()

        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="belief.claim.derive",
            target_id=claim_id,
            target_kind="wiki_claim",
            payload={
                "kind": DERIVED_FROM,
                "derived_from": [str(p) for p in parents],
                "refused": [str(p) for p in wanted if p not in valid],
            },
            trace_id=current_trace_id(),
        )
        span.set_attribute("aleph.derivations.written", len(parents))
        return parents
