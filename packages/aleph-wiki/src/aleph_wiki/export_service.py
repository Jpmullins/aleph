"""Read the evidence chain out of the database, into the export view model.

The split is deliberate and it is the mitigation WS-H8's risk section names.
`export_evidence` is the *format* — pure, ORM-free, and unaffected if the wiki's
storage moves to the Claim Spine as `docs/decisions.md` D1 anticipates. This
module is the *query*, and it is the only part that has to be rewritten when
that happens. `aleph_artifacts.exporters.vault` sits on the same side of the
line and imports nothing from here, which keeps the dependency direction
(artifacts → wiki) intact.

One query, one pass. Not for speed: a per-page N+1 over a corpus with 8,056
claims turns an export into hundreds of round trips, and — the reason that
matters here — a per-page query that returns rows in whatever order Postgres
chooses makes the exported bytes non-deterministic, which silently defeats the
byte-identical round-trip criterion the whole format is checked by.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Select, select

from aleph_rks.models import Source
from aleph_wiki.export_evidence import (
    ClaimEvidence,
    EvidenceCitation,
    PageEvidence,
    normalize_marker,
)
from aleph_wiki.models import Citation, WikiClaim, WikiPage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["EVIDENCE_ORDER_BY", "evidence_query", "load_page_evidence"]

#: The ordering the exported bytes rest on, named so it can be checked.
#:
#: Six of these seven are pinned behaviourally by
#: `tests/integration/test_vault_evidence_export.py`, which stores a corpus
#: back to front so the physical order is the *wrong* answer. `WikiClaim.id` is
#: not, and cannot be from a test: the planner reaches `wiki_claims` through
#: `ix_claims_live`, so two claims tying on `text` already arrive in id order
#: and dropping the tiebreaker changes nothing observable. Pinning it needs the
#: statement, which is what `packages/aleph-wiki/tests/test_export_query.py`
#: reads. See that file for what a behavioural pin would require.
EVIDENCE_ORDER_BY: tuple[str, ...] = (
    "wiki_pages.slug",
    "wiki_claims.text",
    "wiki_claims.id",
    "citations.citation_marker",
    "citations.source_id",
    "citations.char_start",
    "citations.id",
)


def evidence_query(
    project_id: UUID,
) -> Select[tuple[UUID, str, str, WikiClaim, Citation, str, str, str | None]]:
    """The statement `load_page_evidence` runs, built where a test can read it.

    Separated from the execution for one reason: the ORDER BY below is the only
    thing standing between this export and bytes that differ run to run, and
    two consecutive reads in one process agree with each other whether or not
    anything asked them to. Deleting the whole clause left thirteen integration
    tests green. A statement a test can compile is a clause a test can count.
    """
    return (
        select(
            WikiPage.id,
            WikiPage.slug,
            WikiPage.title,
            WikiClaim,
            Citation,
            Source.short_id,
            Source.title.label("source_title"),
            Source.url,
        )
        .join(WikiPage, WikiPage.id == WikiClaim.page_id)
        .outerjoin(Citation, Citation.claim_id == WikiClaim.id)
        # LEFT JOIN, not INNER: a citation whose source row was deleted
        # still has to reach the export. An inner join would drop the
        # evidence along with the source and report a claim as uncited,
        # which is a stronger statement than "the source is gone".
        .outerjoin(Source, Source.id == Citation.source_id)
        .where(
            WikiClaim.project_id == project_id,
            WikiClaim.superseded_by.is_(None),
            WikiPage.is_stub.is_(False),
        )
        .order_by(
            WikiPage.slug,
            WikiClaim.text,
            WikiClaim.id,
            Citation.citation_marker,
            Citation.source_id,
            Citation.char_start,
            Citation.id,
        )
    )


async def load_page_evidence(session: AsyncSession, project_id: UUID) -> dict[UUID, PageEvidence]:
    """Live claims and their citations, keyed by page id.

    **Live** means `superseded_by IS NULL`. Supersession is how a belief is
    revised (see `WikiClaim`), so exporting superseded rows would ship every
    draft of every claim as though all of them were currently held.

    A *retracted* claim IS exported, with its status. It is a claim the project
    made and withdrew, the withdrawal is part of the record, and the renderer
    prints the status so it can never read as support. Dropping it would make
    the export quietly disagree with the belief layer it claims to carry.

    Stub pages are excluded to match `_vault_pages` in the export route: a
    claim attached to a page that is not in the bundle would appear in
    `evidence.json` under a slug with no file, which is precisely the
    dangling-reference shape `scripts/check-okf.py` exists to catch.

    Ordering is total and derived only from stored values — page slug, then
    claim text, then claim id, then citation marker, source, span and citation
    id. Two exports of an unchanged corpus therefore produce identical bytes.
    Leaving any tiebreaker out makes the bundle differ run to run whenever two
    rows collide on the keys that are there, which reads as a format bug and is
    a missing ORDER BY.
    """
    rows = (await session.execute(evidence_query(project_id))).all()

    pages: dict[UUID, tuple[str, str]] = {}
    citations: dict[UUID, list[EvidenceCitation]] = {}
    #: page id → claim ids in the order the ORDER BY produced them. A dict of
    #: sets would lose that order and the bundle would stop being reproducible.
    order: dict[UUID, list[UUID]] = {}
    by_claim_id: dict[UUID, WikiClaim] = {}

    for page_id, slug, title, claim, citation, short_id, source_title, url in rows:
        pages.setdefault(page_id, (slug, title))
        if claim.id not in citations:
            citations[claim.id] = []
            by_claim_id[claim.id] = claim
            order.setdefault(page_id, []).append(claim.id)
        if citation is not None:
            citations[claim.id].append(
                EvidenceCitation(
                    marker=normalize_marker(citation.citation_marker),
                    stance=citation.stance,
                    weight=float(citation.weight),
                    verbatim=bool(citation.verbatim),
                    source_id=str(citation.source_id) if citation.source_id else None,
                    source_short_id=short_id,
                    source_title=source_title,
                    source_url=url,
                    chunk_id=str(citation.chunk_id) if citation.chunk_id else None,
                    quote=citation.quote,
                    char_start=citation.char_start,
                    char_end=citation.char_end,
                )
            )

    out: dict[UUID, PageEvidence] = {}
    for page_id, (slug, title) in pages.items():
        built: list[ClaimEvidence] = []
        for claim_id in order[page_id]:
            claim = by_claim_id[claim_id]
            built.append(
                ClaimEvidence(
                    claim_id=str(claim.id),
                    text=claim.text,
                    confidence=claim.confidence,
                    evidence_tier=claim.evidence_tier,
                    origin=claim.origin,
                    status=claim.status,
                    section_anchor=claim.section_anchor,
                    citations=tuple(citations[claim_id]),
                )
            )
        out[page_id] = PageEvidence(slug=slug, title=title, claims=tuple(built))
    return out
