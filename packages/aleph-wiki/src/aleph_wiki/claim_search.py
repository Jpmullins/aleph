"""Retrieval over CLAIMS rather than over passages.

WS-RS10. The question this exists to answer is a real one with a real chance of
a negative answer: does searching what a project concluded beat searching what
it collected? A claim is short, deduplicated and evidence-anchored, which should
help; it is also a lossy restatement, which should hurt. Nobody knows, because
`wiki_claims.embedding` has been NULL on all 1,325 rows and the HNSW index at
`models.py:204` has never had anything to index.

**Two corrections to the plan, both structural.**

The plan says to implement this in `aleph-belief`. That would be a dependency
cycle: `aleph-wiki` imports `aleph_belief.patch` and `aleph_belief.reconcile`,
so `aleph_belief` cannot import `WikiClaim`. It lives with the model it queries.

And the plan frames this as "making the wiki-deletion gate runnable".
`docs/decisions.md` D1 reversed that decision — the wiki and the RAG are two
knowledge plugins and both stay. So the comparison is no longer a gate on
anything; it is a measurement, and it is worth having on its own terms. Wiring
it to exit non-zero when claims lose to chunks would encode a decision the
project has already made differently.

**The graph hop is the part passage search cannot do.** A claim reached only
through `claim_edges` from a matched claim is a result no chunk index can
produce. If it does not help, that is the finding — reported, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select

from aleph_core.rrf import fuse
from aleph_core.tsquery import or_tsquery
from aleph_wiki.models import ClaimEdge, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Over-fetch per leg, so fusion has two rankings to disagree about. A fused
#: list built from two `top_k`-length rankings is mostly just the first one.
_FETCH_MULTIPLIER = 4

#: How far the graph walk goes. One hop, and stated rather than tuned: two hops
#: over a dense belief graph reaches most of it, at which point "related" stops
#: meaning anything and the hop is just a slower way to return everything.
GRAPH_HOPS = 1

#: Edge kinds worth walking for RETRIEVAL. `supersedes` is deliberately absent —
#: it points at a belief that is no longer live, and surfacing it as a search
#: result would answer a question with a statement the project has withdrawn.
TRAVERSABLE_EDGES = ("supports", "contradicts", "refines", "relates_to")


@dataclass(frozen=True)
class ClaimHit:
    claim_id: UUID
    text: str
    confidence: str
    page_id: UUID | None
    score: float
    #: True when this claim was reached ONLY by walking `claim_edges` from
    #: something that matched. Reported so the graph hop's contribution can be
    #: measured rather than assumed — it is the whole reason to prefer a claim
    #: layer over a passage index, and it might contribute nothing.
    via_graph: bool = False
    cosine_distance: float | None = None
    lexical_rank: float | None = None


async def search_claims(
    session: AsyncSession,
    *,
    project_id: UUID,
    query_text: str,
    query_embedding: list[float] | None,
    top_k: int = 8,
    walk_graph: bool = True,
) -> list[ClaimHit]:
    """Dense + lexical over live claims, fused by RRF, then one graph hop.

    `query_embedding=None` runs the lexical leg only — the same contract
    `search_corpus` has, and for the same reason: a zero vector is not
    equivalent, it makes cosine distance degenerate and the dense leg returns an
    arbitrary page of rows that RRF then fuses as though it were a ranking.

    Only LIVE claims. A superseded belief is what the project used to think, and
    returning it as an answer is worse than returning nothing.
    """
    fetch = max(top_k * _FETCH_MULTIPLIER, top_k)
    scope = (WikiClaim.project_id == project_id, WikiClaim.superseded_by.is_(None))
    columns = (
        WikiClaim.id,
        WikiClaim.text,
        WikiClaim.confidence,
        WikiClaim.page_id,
    )

    dense_rows: list[Any] = []
    if query_embedding is not None:
        distance = WikiClaim.embedding.cosine_distance(query_embedding)  # type: ignore[attr-defined]
        dense_rows = list(
            (
                await session.execute(
                    select(*columns, distance.label("distance"))
                    .where(*scope, WikiClaim.embedding.isnot(None))
                    .order_by(distance)
                    .limit(fetch)
                )
            ).all()
        )

    tsquery = or_tsquery(query_text)
    document = func.to_tsvector("english", WikiClaim.text)
    rank = func.ts_rank(document, tsquery)
    lexical_rows = list(
        (
            await session.execute(
                select(*columns, rank.label("rank"))
                .where(*scope, document.op("@@")(tsquery))
                .order_by(rank.desc())
                .limit(fetch)
            )
        ).all()
    )

    by_id = {row.id: row for row in [*dense_rows, *lexical_rows]}
    distance_by_id = {row.id: float(row.distance) for row in dense_rows}
    rank_by_id = {row.id: float(row.rank) for row in lexical_rows}
    fused = fuse([[r.id for r in dense_rows], [r.id for r in lexical_rows]])

    hits: list[ClaimHit] = []
    for claim_id, score in fused[:top_k]:
        row = by_id[claim_id]
        hits.append(
            ClaimHit(
                claim_id=claim_id,
                text=row.text,
                confidence=row.confidence,
                page_id=row.page_id,
                score=score,
                cosine_distance=distance_by_id.get(claim_id),
                lexical_rank=rank_by_id.get(claim_id),
            )
        )

    if walk_graph and hits and len(hits) < top_k:
        hits.extend(
            await _neighbours(
                session,
                project_id=project_id,
                seeds=[h.claim_id for h in hits],
                exclude={h.claim_id for h in hits},
                limit=top_k - len(hits),
            )
        )
    return hits


async def _neighbours(
    session: AsyncSession,
    *,
    project_id: UUID,
    seeds: list[UUID],
    exclude: set[UUID],
    limit: int,
) -> list[ClaimHit]:
    """Claims one edge away from something that matched.

    Appended AFTER the fused hits and never interleaved: a neighbour is weaker
    evidence than a direct match by construction, and mixing them by a
    synthesized score would invent a comparison between two things that were
    never scored on the same scale.
    """
    if not seeds or limit <= 0:
        return []
    edges = list(
        (
            await session.execute(
                select(ClaimEdge.src_claim_id, ClaimEdge.dst_claim_id)
                .where(
                    ClaimEdge.project_id == project_id,
                    ClaimEdge.kind.in_(TRAVERSABLE_EDGES),
                    ClaimEdge.src_claim_id.in_(seeds) | ClaimEdge.dst_claim_id.in_(seeds),
                )
                .limit(limit * 8)
            )
        ).all()
    )
    reachable: list[UUID] = []
    seen = set(exclude)
    for src, dst in edges:
        for candidate in (src, dst):
            if candidate not in seen:
                seen.add(candidate)
                reachable.append(candidate)
    if not reachable:
        return []

    rows = list(
        (
            await session.execute(
                select(WikiClaim.id, WikiClaim.text, WikiClaim.confidence, WikiClaim.page_id)
                .where(
                    WikiClaim.project_id == project_id,
                    WikiClaim.superseded_by.is_(None),
                    WikiClaim.id.in_(reachable[: limit * 4]),
                )
                .limit(limit)
            )
        ).all()
    )
    return [
        ClaimHit(
            claim_id=r.id,
            text=r.text,
            confidence=r.confidence,
            page_id=r.page_id,
            # Below every fused hit, by construction rather than by a score
            # that would imply they were ranked together.
            score=0.0,
            via_graph=True,
        )
        for r in rows
    ]
