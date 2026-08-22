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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aleph_belief.patch import BeliefPatch
from aleph_belief.reconcile import Candidate, ClaimRef, propose_with_stats
from aleph_belief.trust import TrustTier
from aleph_core.confidence import Confidence
from aleph_core.errors import PermissionDenied
from aleph_core.grounding import ground
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_hypotheses.confidence import (
    EvidenceRow,
    next_confidence_from_evidence,
    weight_for_tier,
)
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.models import Source
from aleph_wiki.models import Citation, ClaimEdge, WikiClaim

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

__all__ = [
    "BeliefService",
    "ClaimUpsert",
    "EvidenceDraft",
    "claim_embedder_for",
    "claim_key_for",
    "locator_hash_for",
    "page_citation_locator",
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


def page_citation_locator(
    *,
    source_id: UUID | None,
    source_page_id: UUID | None,
    chunk_id: UUID | None,
    char_start: int | None,
    char_end: int | None,
    citation_marker: str,
) -> str:
    """Identity of a citation written by the PAGE-COMPILE path.

    Separate from :func:`locator_hash_for` for two reasons, and neither is
    cosmetic.

    *It has to tolerate no anchor at all.* A compile-path citation may carry
    only a marker and a source page — the stub and cross-link writers cite a
    page, not a passage — where `locator_hash_for` requires a `source_id`.

    *It has to keep two markers on one claim apart.* Two citations with no span
    and different markers are two pieces of evidence, and hashing only the
    (absent) anchor would collapse them into one row and halve the claim's
    support count.

    A DIFFERENT function rather than more arguments on the existing one,
    because changing what `locator_hash_for` hashes would change the identity
    of every span already in the database and turn the next re-derivation into
    a duplicate of itself.
    """
    parts = "|".join(
        [
            str(source_id or ""),
            str(source_page_id or ""),
            str(chunk_id or ""),
            "" if char_start is None else str(char_start),
            "" if char_end is None else str(char_end),
            citation_marker,
        ]
    )
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
    #: WS-RS9. Every citation this service writes has had its quote located
    #: verbatim in the source by `ground()` — `_attach_evidence` refuses the
    #: rest — which is exactly `TrustTier.EARNED`. The old default of a flat
    #: 1.0 (`TrustTier.ASSERTED`, an agent's say-so) is why `WELL_SUPPORTED` was
    #: structurally unreachable: the state machine requires one piece of
    #: evidence weighing >= 1.5 and no writer in the tree produced one.
    weight: float = weight_for_tier(TrustTier.EARNED)
    citation_marker: str = "[c1]"
    #: Where ``source_text`` begins inside the whole document.
    #:
    #: Grounding has to be scoped to ONE chunk — a quote hunted across a whole
    #: document anchors to its first occurrence, which for a repeated sentence
    #: is the wrong place, and a citation pointing at the wrong sentence is
    #: worse than one pointing nowhere. But the stored span must be
    #: document-relative, because that is what every reader assumes and what
    #: `test_belief_spine` asserts (`source[char_start:char_end] == quote`).
    #:
    #: So: ground inside the chunk, store `chunk.char_start + span`. The
    #: arithmetic is exact — `test_chunk_offsets.py` pins
    #: `markdown[chunk.char_start:chunk.char_end] == chunk.text` over real
    #: documents, so an offset into the chunk plus its start is an offset into
    #: the document.
    char_offset: int = 0


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
    #: Move an existing belief onto ``page_id``, rather than leaving it where
    #: it was first asserted.
    #:
    #: False by default and that is a decision, pinned by
    #: `test_reasserting_a_claim_keeps_its_identity`: identity is the
    #: proposition, not the page, so page B re-asserting page A's claim must
    #: not quietly move it — the belief would vanish from A's claim list
    #: because A did nothing wrong.
    #:
    #: The curator's merge fold passes True, because moving the claim onto the
    #: target IS the merge: the source page is soft-deleted a few lines later
    #: and a belief left pointing at it is orphaned.
    reassign_page: bool = False


@dataclass(frozen=True)
class SourceText:
    """One source, as the extractor sees it."""

    source_id: UUID
    text: str
    title: str = ""


#: Turns a source into claim drafts. Deliberately a plain callable: the rebuild
#: machinery does not care whether it is an LLM, a rule set, or a fixture, and
#: keeping it injectable is what makes the machinery testable without one.
Extractor = Callable[[SourceText], Sequence["ClaimUpsert"]]


@dataclass(frozen=True)
class RebuildResult:
    claims_before: int
    claims_after: int
    new_claims: int
    citations_written: int
    citations_rejected: int
    user_claims_preserved: int


@dataclass
class UpsertResult:
    claim_id: UUID
    created: bool
    confidence: str
    citations_written: int
    citations_rejected: list[str]


_log = structlog.get_logger(__name__)


#: Turns a claim's proposition into a vector. Injected, never imported — see
#: `BeliefService.upsert_claim`.
ClaimEmbedder = Callable[[str], "list[float] | None"]


async def claim_embedder_for(
    *,
    client: Any,
    principal: Principal,
    project_id: UUID,
    agent_run_id: UUID | None,
    profile_bindings: dict[str, Any],
    texts: Sequence[str],
    purpose: str = "wiki.claim_embed",
) -> ClaimEmbedder:
    """One gateway round trip for a batch of claims, as an injectable embedder.

    Returns ``dict.get`` over a `text -> vector` map, so a claim whose text is
    absent gets ``None`` and is written without a vector — the honest outcome,
    and the lexical leg still finds it.

    Batched deliberately. Embedding inside ``upsert_claim`` would be one call
    per claim, and a page compile writes tens; the write path also has to stay
    runnable with no gateway at all, which is why the embedder is passed IN
    rather than constructed there.

    A failure costs the vectors and not the claims. Losing a belief because a
    model was unreachable would be trading the thing for the index of it, and a
    partial batch is refused outright — zipping a short result would attach
    vectors to the wrong claims, which is worse than none.

    No early return for an empty list: `embed_texts` already makes no call for
    one, so a guard here would be a branch no test could tell from its absence.
    """
    from aleph_rks.embedding import embed_texts

    wanted = list(dict.fromkeys(texts))
    try:
        result = await embed_texts(
            client=client,
            principal=principal,
            project_id=project_id,
            agent_run_id=agent_run_id,
            profile_bindings=profile_bindings,
            texts=wanted,
            purpose=purpose,
        )
    except Exception:
        _log.warning("belief.claim_embed_batch_failed", claims=len(wanted))
        return {}.get
    if len(result.embeddings) != len(wanted):
        _log.warning(
            "belief.claim_embed_count_mismatch", asked=len(wanted), got=len(result.embeddings)
        )
        return {}.get
    return dict(zip(wanted, result.embeddings, strict=True)).get


def _embed_or_none(embed: ClaimEmbedder | None, text: str) -> list[float] | None:
    """The claim's vector, or None if there is no embedder or it failed.

    Swallowing the failure is deliberate and narrow. A claim with no vector is
    still a claim: the lexical leg finds it, `claim_key` still identifies it,
    and `BeliefService.rebuild` can fill the column later. Refusing to record a
    belief because a model was unreachable would lose the thing to protect the
    index of it.
    """
    if embed is None:
        return None
    try:
        return embed(text)
    except Exception:
        _log.warning("belief.claim_embed_failed", chars=len(text))
        return None


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
        embed: ClaimEmbedder | None = None,
    ) -> UpsertResult:
        """Create or update a belief, attach its evidence, recompute confidence.

        ``embed`` fills `wiki_claims.embedding`, and it is INJECTED rather than
        imported for the same reason the extractor's model call is: the write
        path must stay runnable with no gateway, and the caller owns capability
        routing and cost attribution. Passing nothing leaves the column NULL,
        which is what it has been for all 1,325 existing rows — the HNSW index
        at `models.py:204` has never had anything to index.

        An embedding failure does NOT fail the write. A claim without a vector
        is still a claim, findable by the lexical leg; refusing to record it
        because a model was unreachable would lose the belief to protect an
        index.
        """
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
                    confidence=Confidence.UNDER_INVESTIGATION.value,
                    status="active",
                    embedding=_embed_or_none(embed, proposition),
                    created_by=principal.user_id,
                )
                self._session.add(claim)
                await self._session.flush()
                created = True
            else:
                claim = existing
                # An update enriches; it never silently downgrades provenance.
                if rationale and not claim.rationale:
                    claim.rationale = rationale
                # Re-point the claim at the revision that has just re-asserted
                # it. NOT bookkeeping: five readers list a page's claims with
                # `WikiClaim.revision_id == page.current_revision_id`
                # (`a2ui_handlers`, `routes/surfaces`, `routes/wiki`,
                # `wiki_refresh`, the mechanical reviewer). Identity is
                # `claim_key`, so a recompiled page finds its existing claim
                # instead of inserting a new row — and if that row kept the
                # revision it was born on, every one of those readers would
                # show the page as having no claims from the second compile
                # onwards.
                #
                # Only for the page the claim already sits on, unless the
                # caller says otherwise. A DIFFERENT page re-asserting the same
                # proposition must not take the belief off the page that holds
                # it — see `ClaimUpsert.reassign_page`.
                #
                # `revision_id` only when the caller supplies one: a writer
                # that does not know the revision must not blank the one that
                # is there.
                if draft.reassign_page or claim.page_id == draft.page_id:
                    claim.page_id = draft.page_id
                    if draft.revision_id is not None:
                        claim.revision_id = draft.revision_id
                    if draft.section_anchor is not None:
                        claim.section_anchor = draft.section_anchor

                # The vector, on the UPDATE branch too.
                #
                # `embedding` was assigned in the insert branch only, and the
                # one wired production caller — `curator_service.
                # recurate_overview` via `_carry_claims` — selects claims that
                # ALREADY EXIST, so every draft it produces lands here. The
                # vector was computed, paid for at the gateway, and discarded
                # 100% of the time; 18,038 of 18,038 claims on the live stack
                # have a NULL embedding and an HNSW index with nothing to index.
                #
                # Backfill-only, not unconditional. A claim's text is its
                # identity (`claim_key`), so a row that already has a vector has
                # the right one, and re-embedding every re-assertion would pay
                # the gateway again on every recompile of every page. This
                # fills the hole and does not re-dig it.
                if claim.embedding is None:
                    vector = _embed_or_none(embed, proposition)
                    if vector is not None:
                        claim.embedding = vector
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
            start = span.char_start + draft.char_offset
            end = span.char_end + draft.char_offset
            locator = locator_hash_for(draft.source_id, draft.chunk_id, start, end)
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
                char_start=start,
                char_end=end,
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

        WS-RS9: evidence from a **retracted** source does not count. Without
        this predicate a retraction only wrote a status column — every claim
        resting on the withdrawn paper kept the confidence that paper bought it,
        for as long as nobody re-derived it, which is never. The outer join is
        deliberate: a citation with no `source_id` is unanchored, not retracted,
        and dropping those would quietly delete the evidence the legacy wiki
        path wrote.
        """
        rows = (
            await self._session.execute(
                select(Citation.stance, Citation.weight, Citation.source_id)
                .outerjoin(Source, Source.id == Citation.source_id)
                .where(
                    Citation.claim_id == claim_id,
                    Source.retracted_at.is_(None),
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
        embed: ClaimEmbedder | None = None,
    ) -> UpsertResult:
        """Replace a belief with a revised one, keeping the old row walkable.

        Refuses to supersede a user-authored claim from an agent origin. The
        refusal is structural — an agent cannot phrase its way past it.

        ``embed`` is forwarded, because a superseding claim has DIFFERENT text
        and therefore a different `claim_key`: it is a brand-new row, not an
        update to the one being replaced, and it inherits nothing — including
        the old row's vector. Without this the revision of an embedded belief
        is silently less findable than the belief it corrects.
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
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=replacement,
            embed=embed,
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
                )
            )
            await self._session.flush()
        return result

    async def propose_merges(
        self, *, project_id: UUID, profile_hash: str = "", graph_hash: str = ""
    ) -> list[tuple[Candidate, BeliefPatch | None]]:
        """Deterministically find duplicate beliefs. No model is called.

        Replaces `CuratorService.dedup_detect`, which spent one judge-tier LLM
        call per new page deciding whether two claims were the same thing —
        expensive, non-reproducible, and untestable.

        Returns proposals, never mutations. A pair in the ambiguous band comes
        back with ``patch=None``: that is what the LLM adjudicator is handed,
        and it is the only place model spend belongs.

        The hashes stamp what the proposals were computed against, so a decision
        made against an older graph is detected at apply time instead of
        applying blindly.
        """
        claims = await self.live_claims(project_id=project_id)
        refs = [ClaimRef(id=c.id, text=c.text, origin=c.origin) for c in claims]
        proposals, stats = propose_with_stats(
            refs,
            project_id=project_id,
            profile_hash=profile_hash or "unset",
            graph_hash=graph_hash or _graph_hash_for(refs),
        )
        if stats.truncated:
            # A dedupe pass that quietly stopped early is indistinguishable from
            # one that found nothing to merge — both return a short list. Say it
            # out loud, because the caller cannot tell and will otherwise treat
            # a sample as a sweep.
            _log.warning(
                "belief.propose_truncated",
                project_id=str(project_id),
                claims=stats.claims,
                pairs_scored=stats.pairs_scored,
                pairs_total=stats.pairs_total,
                limit=stats.limit,
            )
        return proposals

    async def rebuild(
        self,
        *,
        principal: Principal,
        ledger: LedgerWriter,
        project_id: UUID,
        extract: Extractor,
        sources: Sequence[SourceText],
        embed: ClaimEmbedder | None = None,
    ) -> RebuildResult:
        """Re-derive the belief graph from its sources.

        The property this exists to make true: **the belief graph is a function
        of (sources x extraction x decisions), not primary state.** The wiki it
        replaces could not be rebuilt from anything — it was the only copy of
        what the system believed, so a bad compile was unrecoverable and a
        changed extraction prompt could never be applied retroactively.

        ``extract`` is injected rather than hard-wired. The rebuild machinery
        and the extractor are separate concerns: this can be tested for
        determinism and idempotence with a fixture extractor, and swapping in a
        better model later does not change any of the guarantees below.

        Claims with ``origin='user'`` are NOT touched. They are not derived from
        a source, so re-deriving cannot produce them — a rebuild that dropped
        them would destroy exactly the corrections a human bothered to make.

        Idempotent: running twice over the same sources leaves the same graph,
        because identity is `claim_key` and evidence is deduped by locator.

        ``embed`` matters more here than anywhere else, and it was missing.
        Two comments on the live write path — in `upsert_claim` and in
        `_node_claim_extraction` — justify leaving `embedding` NULL by saying
        that "`BeliefService.rebuild` re-derives later". It could not: rebuild
        called `upsert_claim` with no embedder, so the designated repair for a
        NULL vector produced NULL vectors. Passing nothing is still allowed —
        the rebuild machinery has to stay runnable with no gateway, which is
        the same reason `extract` is injected — but it is now a choice the
        caller makes rather than one the method makes for it.
        """
        with start_span("belief.rebuild", **{"aleph.project_id": str(project_id)}) as span:
            before = {c.claim_key for c in await self.live_claims(project_id=project_id)}
            written = 0
            rejected = 0

            for source in sources:
                for draft in extract(source):
                    result = await self.upsert_claim(
                        principal=principal,
                        ledger=ledger,
                        project_id=project_id,
                        draft=draft,
                        embed=embed,
                    )
                    written += result.citations_written
                    rejected += len(result.citations_rejected)

            after_claims = await self.live_claims(project_id=project_id)
            after = {c.claim_key for c in after_claims}
            preserved = sum(1 for c in after_claims if c.origin in _PROTECTED_ORIGINS)

            span.set_attribute("aleph.belief.rebuilt", len(after))
            return RebuildResult(
                claims_before=len(before),
                claims_after=len(after),
                new_claims=len(after - before),
                citations_written=written,
                citations_rejected=rejected,
                user_claims_preserved=preserved,
            )

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


def _graph_hash_for(refs: list[ClaimRef]) -> str:
    """A digest of the belief set the proposals were computed against.

    Cheap and order-independent, so the same set of claims always produces the
    same hash and a patch can tell whether the graph has moved under it.
    """
    joined = "|".join(sorted(f"{r.id}:{claim_key_for(r.text)}" for r in refs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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
