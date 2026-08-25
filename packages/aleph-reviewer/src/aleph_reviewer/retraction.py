"""Source retraction + blast-radius (WP-6 §4).

``retract_source`` is the single path all retraction triggers funnel through —
the manual EDITOR ``POST .../retract`` route, and the WP-2 reviewer
``doi_verification`` node's scholar-detected retractions. It:

- sets ``Source.status="retracted"`` + ``retracted_at`` + ``retraction_reason``
  and writes a ``source.retract`` ledger event;
- walks the belief graph two hops — ``Citation.source_id`` for the claims that
  cite the source, then ``claim_edges.kind='derived_from'`` for the conclusions
  built on those claims — and marks each one according to what is left of its
  evidence;
- **recomputes the confidence of everything it touched**, because a retraction
  that only writes a status column has not propagated. Evidence from a retracted
  source stops counting, so a claim that rested on it drops out of
  ``well_supported`` on the same transaction, derived from the remaining
  evidence rather than asserted;
- emits a ``retracted_source`` ``ReviewFinding`` (severity critical) under a
  minimal ``kind="retraction"`` ``ReviewRun`` so it lands in Briefs — the same
  finding kind the WP-2 reviewer emits. The finding text **enumerates** the blast
  radius: how many claims were reached directly, how many through a derivation,
  how many were left with no surviving support and how many survive weakened.

`retraction_impact` exposes the blast-radius set without mutating anything, and
is what a caller should ask. It replaced `dependent_claims`, which flattened the
two branches back into one list and had exactly one caller — the write path
below, which then wrote one status onto everything. Deleted rather than kept:
a helper whose only purpose was to discard a distinction, left in the tree "for
existing callers" it no longer has, is how the distinction gets discarded again.

Every mutation is in the caller's transaction (rule 4); the caller commits.

**Two statuses, not one.** A claim whose only support was the retracted source
becomes ``retracted``; a claim that also rests on independent evidence becomes
``weakened``. Writing ``retracted`` onto both — which this did until WS-RS9 —
throws away the distinction the impact walk exists to make, and a flag that
fires on beliefs that are still fine is a flag a reader learns to skip. The
word is ``weakened`` and not ``contested`` because ``contested`` is already
taken twice over: it is a ``Confidence`` state meaning *the evidence points both
ways*, and it is the status ``a2ui_handlers`` writes when a **person** rejects a
page. A withdrawn support is neither of those.

Layering note: this lives in ``aleph_reviewer`` — the highest of the three
packages it touches (it depends on ``aleph_rks`` for ``Source``, ``aleph_wiki``
for the claim/citation tables, and uses its own ``review_service`` for the
finding). Placing it here keeps the strict higher→lower DAG intact (``aleph_rks``
is a low leaf and must not import ``aleph_wiki``/``aleph_reviewer``). Both
callers — the reviewer's ``doi_verification`` retracted branch (same package)
and the manual ``POST .../retract`` route (``aleph_api``, above everything) —
share this one code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.review_service import add_finding, finalize_run, start_run
from aleph_rks.models import Source
from aleph_wiki.belief_service import BeliefService
from aleph_wiki.derivation import DERIVED_FROM
from aleph_wiki.models import Citation, ClaimEdge, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


#: What a claim's status becomes when the retracted source was its only support.
STATUS_UNSUPPORTED = "retracted"

#: What a claim's status becomes when it survives on independent evidence.
STATUS_WEAKENED = "weakened"


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
    #: Hops from the retracted source: 0 for a claim that cites it, 1 for a
    #: claim derived from one of those, and so on. Carried so the ledger and the
    #: finding can say *how* a claim was reached rather than only that it was.
    depth_by_claim: dict[UUID, int] = field(default_factory=dict)
    #: Whether this project has ANY `derived_from` edge for the walk to follow.
    #:
    #: "0 claims were derived from those" and "there is no derivation graph on
    #: this instance" are the same output and are not remotely the same fact,
    #: and today it is always the second: nothing in production writes
    #: `derived_from` (WS-RS9 c4 — `claim_edges` holds two rows and both are
    #: `supersedes`). Reported rather than inferred, because an absence standing
    #: in for a state is the defect class this repository keeps shipping — a
    #: dead embedder wrote no chunks and looked like a project nobody had
    #: ingested into, and a retraction that reaches one hop out of two looks
    #: exactly like a retraction nothing depended on.
    derivation_graph_is_empty: bool = True

    @property
    def all_touched(self) -> set[UUID]:
        return self.directly_cited | self.derived


@dataclass(frozen=True)
class RetractionResult:
    """Blast-radius summary returned by ``retract_source``."""

    source_id: UUID
    page_ids: set[UUID]
    claim_ids: set[UUID]
    finding_id: UUID | None = None
    already_retracted: bool = False
    #: The two hops, kept apart. `claim_ids` is their union and is what the
    #: HTTP route has always returned; these say which is which.
    directly_cited: set[UUID] = field(default_factory=set)
    derived: set[UUID] = field(default_factory=set)
    #: The declined branch, as written. `unsupported` claims are `retracted`;
    #: `weakened` ones survive on independent evidence.
    unsupported: set[UUID] = field(default_factory=set)
    weakened: set[UUID] = field(default_factory=set)
    #: Claims whose derived confidence actually moved, `claim_id -> (before, after)`.
    #: A retraction that reaches ten claims and changes none of their confidence
    #: is a fact worth being able to see.
    confidence_changed: dict[UUID, tuple[str, str]] = field(default_factory=dict)
    #: One sentence naming everything above. This is what lands in the
    #: `retracted_source` finding, and it is the difference between a retraction
    #: that propagated and a retraction that says it propagated.
    summary: str = ""


#: How far a retraction propagates along `derived_from`. Bounded because a
#: cycle or a pathological chain must not turn one retraction into a full-table
#: walk; the CTE also guards cycles explicitly.
MAX_DERIVATION_DEPTH = 4


def describe_impact(impact: RetractionImpact, *, short_id: str, reason: str) -> str:
    """Say what the retraction reached, in one line, in numbers.

    Written as a function so the finding, the ledger payload and any caller that
    wants to log it all say the same thing. A blast radius that is only visible
    by re-running the query is not a report.
    """
    deepest = max(impact.depth_by_claim.values(), default=0)
    # The second hop's zero is qualified, always. Without this the sentence
    # "0 derived from those" is written identically whether the walk found no
    # dependants or whether there was no graph to walk, and only the first of
    # those is a result. On this instance it is always the second.
    second_hop = (
        f"{len(impact.derived)} derived from those (deepest hop {deepest})"
        if not impact.derivation_graph_is_empty
        else (
            "0 derived from those — NOT because nothing depends on them: this "
            "project has no 'derived_from' edge at all, so the second hop had "
            "no graph to walk (WS-RS9 c4)"
        )
    )
    return (
        f"Source {short_id} was retracted: {reason}. "
        f"Reached {len(impact.all_touched)} claim(s): "
        f"{len(impact.directly_cited)} citing it directly, "
        f"{second_hop}. "
        f"{len(impact.unsupported)} left with no surviving support "
        f"(status={STATUS_UNSUPPORTED}); "
        f"{len(impact.weakened)} survive on independent evidence "
        f"(status={STATUS_WEAKENED})."
    )


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
    depth_by_claim: dict[UUID, int] = dict.fromkeys(directly_cited, 0)

    # Asked once, cheaply, and it is the difference between a measurement and
    # an assumption. `LIMIT 1` over the partial-index-able predicate, so the
    # cost is a lookup and not a count.
    has_edges = (
        await session.execute(select(ClaimEdge.id).where(ClaimEdge.kind == DERIVED_FROM).limit(1))
    ).first() is not None

    derived: set[UUID] = set()
    if directly_cited:
        # Recursive walk over derived_from, depth-capped and cycle-guarded by
        # excluding anything already visited on the path.
        walk = text(
            """
            WITH RECURSIVE downstream(claim_id, depth, path) AS (
                SELECT e.src_claim_id, 1, ARRAY[e.dst_claim_id, e.src_claim_id]
                FROM claim_edges e
                WHERE e.kind = :derived_kind
                  AND e.dst_claim_id = ANY(:roots)
                UNION ALL
                SELECT e.src_claim_id, d.depth + 1, d.path || e.src_claim_id
                FROM claim_edges e
                JOIN downstream d ON e.dst_claim_id = d.claim_id
                WHERE e.kind = :derived_kind
                  AND d.depth < :max_depth
                  AND NOT (e.src_claim_id = ANY(d.path))
            )
            SELECT claim_id, MIN(depth) AS depth FROM downstream GROUP BY claim_id
            """
        )
        rows = await session.execute(
            walk,
            {
                "roots": list(directly_cited),
                "max_depth": MAX_DERIVATION_DEPTH,
                # The kind, from the ONE place it is named. It was spelled as a
                # SQL literal here and as `DERIVED_FROM` in the writer, which is
                # the third of the three places `aleph_wiki.derivation` warns
                # cannot import each other — and a typo in either is silent.
                "derived_kind": DERIVED_FROM,
            },
        )
        for claim_id, depth in rows.all():
            if claim_id in directly_cited:
                # A claim that both cites the source and is derived from another
                # claim that does. The direct hop is the shorter and truer story.
                continue
            derived.add(claim_id)
            depth_by_claim[claim_id] = int(depth)

    touched = directly_cited | derived
    if not touched:
        return RetractionImpact(
            set(), set(), set(), set(), {}, derivation_graph_is_empty=not has_edges
        )

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
        depth_by_claim=depth_by_claim,
        derivation_graph_is_empty=not has_edges,
    )


async def retract_source(
    session: AsyncSession,
    ledger: LedgerWriter,
    principal: Principal,
    *,
    source_id: UUID,
    reason: str,
) -> RetractionResult:
    """Retract ``source_id`` and propagate through every dependent claim.

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
        impact = await retraction_impact(session, source_id)
        pages = await _pages_for(session, impact.all_touched)
        return RetractionResult(
            source_id=source_id,
            page_ids=set(pages.values()),
            claim_ids=set(impact.all_touched),
            already_retracted=True,
            directly_cited=impact.directly_cited,
            derived=impact.derived,
            unsupported=impact.unsupported,
            weakened=impact.weakened,
            summary=describe_impact(
                impact, short_id=source.short_id, reason=source.retraction_reason or reason
            ),
        )

    # 1. Walk the graph BEFORE the source is marked retracted.
    #
    #    Order matters and is not cosmetic: `recompute_confidence` excludes
    #    evidence from retracted sources, so the walk has to happen first if the
    #    "does anything else still support this" question is to be asked against
    #    the same graph the reader saw. Marking first, walking second would also
    #    work for this particular query, but it makes the sequence depend on
    #    which predicates happen to filter on `retracted_at` — a dependency
    #    nobody would notice breaking.
    impact = await retraction_impact(session, source_id)

    # 2. Retract the source itself.
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
        payload={
            "short_id": source.short_id,
            "reason": reason,
            "claims_reached": len(impact.all_touched),
            "claims_direct": len(impact.directly_cited),
            "claims_derived": len(impact.derived),
            "claims_unsupported": len(impact.unsupported),
            "claims_weakened": len(impact.weakened),
        },
        trace_id=trace,
    )

    # 3. Mark every dependent claim, and say which branch it fell into.
    beliefs = BeliefService(session)
    page_ids: set[UUID] = set()
    claim_ids: set[UUID] = set()
    confidence_changed: dict[UUID, tuple[str, str]] = {}
    if impact.all_touched:
        claims = list(
            (await session.execute(select(WikiClaim).where(WikiClaim.id.in_(impact.all_touched))))
            .scalars()
            .all()
        )
        for claim in claims:
            unsupported = claim.id in impact.unsupported
            # `status` carries the retraction; `confidence` is never written
            # here. Confidence is DERIVED from the evidence by
            # `aleph_belief.confidence.next_confidence_from_evidence`, and
            # "retracted" is not one of its states — writing it here would put a
            # value in the column that the state machine can never produce, so
            # the next recompute would silently erase it. What the claim is now
            # worth is recomputed below, from whatever evidence survives.
            claim.status = STATUS_UNSUPPORTED if unsupported else STATUS_WEAKENED
            page_ids.add(claim.page_id)
            claim_ids.add(claim.id)

            before = claim.confidence
            after = await beliefs.recompute_confidence(project_id=project_id, claim_id=claim.id)
            if after != before:
                confidence_changed[claim.id] = (before, after)

            await ledger.append(
                project_id=project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="wiki_claim.retract_flag",
                target_id=claim.id,
                target_kind="wiki_claim",
                payload={
                    "source_id": str(source_id),
                    "page_id": str(claim.page_id),
                    # Which hop reached it, which branch it fell into, and what
                    # the withdrawal did to its confidence. A per-claim ledger
                    # row that says only "flagged" cannot answer "why is this
                    # page different today", which is the question a retraction
                    # exists to be able to answer.
                    "hop": "direct" if claim.id in impact.directly_cited else "derived",
                    "depth": impact.depth_by_claim.get(claim.id, 0),
                    "branch": "unsupported" if unsupported else "weakened",
                    "status": claim.status,
                    "confidence_before": before,
                    "confidence_after": after,
                },
                trace_id=trace,
            )

    summary = describe_impact(impact, short_id=source.short_id, reason=reason)

    # 4. Emit a critical `retracted_source` finding under a minimal run so it
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
            f"{summary} Across {len(page_ids)} page(s). "
            f"{len(confidence_changed)} claim(s) changed derived confidence."
        ),
        target_source_id=source.id,
        evidence_refs=[
            {"kind": "source", "source_id": str(source.id), "short_id": source.short_id},
            *[
                {
                    "kind": "wiki_claim",
                    "claim_id": str(cid),
                    "hop": "direct" if cid in impact.directly_cited else "derived",
                    "branch": "unsupported" if cid in impact.unsupported else "weakened",
                }
                for cid in sorted(claim_ids)
            ],
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
        directly_cited=impact.directly_cited,
        derived=impact.derived,
        unsupported=impact.unsupported,
        weakened=impact.weakened,
        confidence_changed=confidence_changed,
        summary=summary,
    )


async def _pages_for(session: AsyncSession, claim_ids: set[UUID]) -> dict[UUID, UUID]:
    if not claim_ids:
        return {}
    rows = (
        await session.execute(
            select(WikiClaim.id, WikiClaim.page_id).where(WikiClaim.id.in_(claim_ids))
        )
    ).all()
    pages: dict[UUID, UUID] = {}
    for claim_id, page_id in rows:
        pages[claim_id] = page_id
    return pages
