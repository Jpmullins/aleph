"""The Claim Spine's write path.

Three guarantees, each of which the previous claim layer lacked:

**A claim survives a page rewrite.** Identity is ``(project_id, claim_key)``
among live rows, so re-asserting the same proposition updates the existing
belief rather than creating a new one. `commit_revision` used to insert a fresh
row per commit, discarding the claim's citations, its verdicts and any human
correction — so the knowledge layer could not accumulate.

**Confidence is derived, never asserted.** It is recomputed from the evidence in
the same transaction as any citation write, by
``aleph_hypotheses.confidence.next_confidence_from_evidence`` — a pure, tested
function with no LLM call. A model is never asked how confident it is.

**A human's claim is immutable to agents.** ``origin='user'`` is enforced in the
write path, not requested in a prompt. An agent that tries to supersede one is
refused; that is the whole point of having the column.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aleph_core.errors import PermissionDenied
from aleph_core.grounding import ground
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_hypotheses.confidence import EvidenceRow, next_confidence_from_evidence
from aleph_observability.tracing import current_trace_id, start_span
from aleph_wiki.models import Citation, ClaimEdge, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

__all__ = [
    "BeliefService",
    "ClaimUpsert",
    "EvidenceDraft",
    "claim_key_for",
    "locator_hash_for",
]

_WS = re.compile(r"\s+")

#: A proposition longer than this is a paragraph. Paragraphs cannot be
#: contradicted, retracted or scored, so the excess goes to `rationale`.
MAX_PROPOSITION_CHARS = 300

#: Writers whose claims an agent may never overwrite.
_PROTECTED_ORIGINS = frozenset({"user"})


def claim_key_for(text: str) -> str:
    """Stable identity of a proposition: sha256 of its normalized form.

    Case and whitespace are normalised so a re-extraction that differs only in
    spacing updates the belief instead of forking it. Deliberately NOT the full
    grounding normalisation — accent and ligature folding would merge
    propositions that a reader would consider different.
    """
    normalized = _WS.sub(" ", text.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def locator_hash_for(
    source_id: UUID, chunk_id: UUID | None, char_start: int | None, char_end: int | None
) -> str:
    """Identity of an evidence span, so re-derivation unions instead of duplicating."""
    start = "" if char_start is None else char_start
    end = "" if char_end is None else char_end
    parts = f"{source_id}|{chunk_id or ''}|{start}|{end}"
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceDraft:
    """A proposed citation. ``quote`` is checked against the source before it lands."""

    source_id: UUID
    quote: str
    #: The text the quote is checked against. The HARNESS supplies this from its
    #: own registry — the model never provides both a claim and the evidence for
    #: it, which would make the check circular.
    source_text: str
    chunk_id: UUID | None = None
    stance: str = "supports"
    weight: float = 1.0
    citation_marker: str = "[c1]"


@dataclass(frozen=True)
class ClaimUpsert:
    text: str
    page_id: UUID
    origin: str = "agent"
    evidence_tier: str = "inferred"
    rationale: str = ""
    section_anchor: str | None = None
    revision_id: UUID | None = None
    evidence: list[EvidenceDraft] = field(default_factory=list)


@dataclass
class UpsertResult:
    claim_id: UUID
    created: bool
    confidence: str
    citations_written: int
    citations_rejected: list[str]


class BeliefService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_claim(
        self,
        *,
        principal: Principal,
        ledger: LedgerWriter,
        project_id: UUID,
        draft: ClaimUpsert,
    ) -> UpsertResult:
        """Create or update a belief, attach its evidence, recompute confidence."""
        with start_span(
            "belief.upsert_claim",
            **{"aleph.project_id": str(project_id), "aleph.origin": draft.origin},
        ) as span:
            proposition, rationale = _split_proposition(draft.text, draft.rationale)
            key = claim_key_for(proposition)

            existing = (
                await self._session.execute(
                    select(WikiClaim).where(
                        WikiClaim.project_id == project_id,
                        WikiClaim.claim_key == key,
                        WikiClaim.superseded_by.is_(None),
                    )
                )
            ).scalar_one_or_none()

            # A user's claim is immutable to agents. Structural, not a prompt
            # instruction — an agent cannot phrase its way past it.
            if (
                existing is not None
                and existing.origin in _PROTECTED_ORIGINS
                and draft.origin not in _PROTECTED_ORIGINS
            ):
                msg = (
                    f"claim {existing.id} was written by a user and cannot be "
                    f"overwritten by origin={draft.origin!r}"
                )
                raise PermissionDenied(msg)

            if existing is None:
                claim = WikiClaim(
                    id=uuid7(),
                    project_id=project_id,
                    page_id=draft.page_id,
                    revision_id=draft.revision_id,
                    section_anchor=draft.section_anchor,
                    text=proposition,
                    claim_key=key,
                    origin=draft.origin,
                    evidence_tier=draft.evidence_tier,
                    rationale=rationale,
                    confidence="under_investigation",
                    status="active",
                    created_by=principal.user_id,
                    access_scope="project",
                )
                self._session.add(claim)
                await self._session.flush()
                created = True
            else:
                claim = existing
                # An update enriches; it never silently downgrades provenance.
                if rationale and not claim.rationale:
                    claim.rationale = rationale
                created = False

            written, rejected = await self._attach_evidence(
                project_id=project_id, claim_id=claim.id, drafts=draft.evidence
            )
            confidence = await self.recompute_confidence(project_id=project_id, claim_id=claim.id)

            await ledger.append(
                project_id=project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="belief.claim.upsert" if created else "belief.claim.enrich",
                target_id=claim.id,
                target_kind="wiki_claim",
                payload={
                    "claim_key": key,
                    "origin": draft.origin,
                    "citations_written": written,
                    "citations_rejected": len(rejected),
                    "confidence": confidence,
                },
                trace_id=current_trace_id(),
            )
            span.set_attribute("aleph.belief.created", created)
            span.set_attribute("aleph.belief.confidence", confidence)
            return UpsertResult(
                claim_id=claim.id,
                created=created,
                confidence=confidence,
                citations_written=written,
                citations_rejected=rejected,
            )

    async def _attach_evidence(
        self, *, project_id: UUID, claim_id: UUID, drafts: list[EvidenceDraft]
    ) -> tuple[int, list[str]]:
        """Write citations whose quotes are verbatim-present in their source.

        A quote that is not in the source is REFUSED, not stored with a flag.
        Storing it would put an unverifiable assertion into the provenance
        trail, and the trail's only value is that everything in it is checkable.
        """
        written = 0
        rejected: list[str] = []
        for draft in drafts:
            span = ground(draft.quote, draft.source_text)
            if span is None:
                rejected.append(draft.quote[:80])
                continue
            locator = locator_hash_for(
                draft.source_id, draft.chunk_id, span.char_start, span.char_end
            )
            stmt = insert(Citation).values(
                id=uuid7(),
                project_id=project_id,
                claim_id=claim_id,
                source_id=draft.source_id,
                chunk_id=draft.chunk_id,
                quote=span.matched_text,
                verbatim=True,
                stance=draft.stance,
                weight=draft.weight,
                locator_hash=locator,
                char_start=span.char_start,
                char_end=span.char_end,
                chunk_ids=[str(draft.chunk_id)] if draft.chunk_id else [],
                source_page_id=None,
                citation_marker=draft.citation_marker[:16],
            )
            # Union, not clobber: re-deriving the same span updates the row.
            await self._session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["claim_id", "locator_hash"],
                    index_where=Citation.locator_hash.is_not(None),
                    set_={
                        "quote": stmt.excluded.quote,
                        "stance": stmt.excluded.stance,
                        "weight": stmt.excluded.weight,
                        "verbatim": stmt.excluded.verbatim,
                    },
                )
            )
            written += 1
        await self._session.flush()
        return written, rejected

    async def recompute_confidence(self, *, project_id: UUID, claim_id: UUID) -> str:
        """Derive confidence from the evidence. No model is consulted.

        Called in the same transaction as any citation or edge write, so the
        stored value can never disagree with the evidence it summarises.
        """
        rows = (
            await self._session.execute(
                select(Citation.stance, Citation.weight, Citation.source_id).where(
                    Citation.claim_id == claim_id
                )
            )
        ).all()
        edges = (
            await self._session.execute(
                select(ClaimEdge.kind, ClaimEdge.weight).where(
                    ClaimEdge.dst_claim_id == claim_id,
                    ClaimEdge.kind.in_(("supports", "contradicts")),
                )
            )
        ).all()

        evidence = [EvidenceRow(stance=r.stance, weight=float(r.weight)) for r in rows]
        evidence += [EvidenceRow(stance=e.kind, weight=float(e.weight)) for e in edges]
        confidence = next_confidence_from_evidence(evidence)

        claim = (
            await self._session.execute(select(WikiClaim).where(WikiClaim.id == claim_id))
        ).scalar_one()
        claim.confidence = confidence.value
        claim.support_count = sum(1 for r in rows if r.stance == "supports")
        claim.distinct_source_count = len({r.source_id for r in rows if r.source_id})
        claim.last_surfaced_at = utcnow()
        await self._session.flush()
        return confidence.value

    async def supersede(
        self,
        *,
        principal: Principal,
        ledger: LedgerWriter,
        project_id: UUID,
        claim_id: UUID,
        replacement: ClaimUpsert,
    ) -> UpsertResult:
        """Replace a belief with a revised one, keeping the old row walkable.

        Refuses to supersede a user-authored claim from an agent origin. The
        refusal is structural — an agent cannot phrase its way past it.
        """
        old = (
            await self._session.execute(select(WikiClaim).where(WikiClaim.id == claim_id))
        ).scalar_one()
        if old.origin in _PROTECTED_ORIGINS and replacement.origin not in _PROTECTED_ORIGINS:
            msg = (
                f"claim {claim_id} was written by a user; origin={replacement.origin!r} "
                f"may not supersede it"
            )
            raise PermissionDenied(msg)

        result = await self.upsert_claim(
            principal=principal, ledger=ledger, project_id=project_id, draft=replacement
        )
        if result.claim_id != claim_id:
            old.superseded_by = result.claim_id
            old.status = "superseded"
            await self._session.flush()
            self._session.add(
                ClaimEdge(
                    id=uuid7(),
                    project_id=project_id,
                    src_claim_id=result.claim_id,
                    dst_claim_id=claim_id,
                    kind="supersedes",
                    weight=1.0,
                    created_by=principal.user_id,
                    access_scope="project",
                )
            )
            await self._session.flush()
        return result

    async def live_claims(self, *, project_id: UUID) -> list[WikiClaim]:
        return list(
            (
                await self._session.execute(
                    select(WikiClaim).where(
                        WikiClaim.project_id == project_id,
                        WikiClaim.superseded_by.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


def _split_proposition(text: str, rationale: str) -> tuple[str, str]:
    """Keep the proposition short; the overflow becomes rationale.

    Truncating at a sentence boundary rather than mid-word, so the stored
    proposition still reads as a claim.
    """
    cleaned = _WS.sub(" ", text.strip())
    if len(cleaned) <= MAX_PROPOSITION_CHARS:
        return cleaned, rationale
    cut = cleaned.rfind(". ", 0, MAX_PROPOSITION_CHARS)
    if cut < MAX_PROPOSITION_CHARS // 2:
        cut = cleaned.rfind(" ", 0, MAX_PROPOSITION_CHARS)
    if cut <= 0:
        cut = MAX_PROPOSITION_CHARS
    head, tail = cleaned[: cut + 1].strip(), cleaned[cut + 1 :].strip()
    return head, f"{rationale}\n\n{tail}".strip() if rationale else tail
