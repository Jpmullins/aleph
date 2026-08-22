"""The page-compile path writes BELIEFS, not rows — WS-RS8/RS10, against Postgres.

`commit_revision` used to construct `WikiClaim` by hand: no `claim_key`, no
`embedding`, a confidence the producer asserted about its own evidence, and a
fresh row on every compile. Measured on the live database on 2026-08-22:
**7,195 of 17,241 claims written that day had no `claim_key`**, and **18,038 of
18,038 had no embedding** — an HNSW index over a column nothing had ever
filled. Two of the three claim writers in the tree were this one and
`curator_service`'s merge fold; only `BeliefService` filled the columns the
knowledge layer is built on.

Every test here drives `WikiService.commit_revision` — the real write path, the
one four packages call — and reads the rows back out of Postgres. Nothing is
asserted about a fixture this file built.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.confidence import Confidence
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal
from aleph_wiki.belief_service import claim_key_for
from aleph_wiki.curator_service import CuratorService
from aleph_wiki.models import Citation, PageMergeProposal, WikiClaim
from aleph_wiki.wiki_service import CitationDraft, ClaimDraft, CommitResult, WikiService

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000bb")

CLAIM_TEXT = "Sedimentation rates rose sharply after the 8.2 ka event across the basin."
OTHER_CLAIM = "Radiocarbon dates bracket the transition to within a few decades."


def _agent() -> Principal:
    return Principal(
        user_id=ACTOR,
        subject="page-compiler",
        email="page-compiler@example.test",
        actor_kind="aleph_agent",
    )


def _citation(marker: str = "[c1]") -> CitationDraft:
    """A citation shaped exactly like the research composer's: it carries the
    span that path already computed, and no `source_id` is invented here."""
    return CitationDraft(
        chunk_ids=[],
        source_page_id=None,
        citation_marker=marker,
        quote="rose sharply after the 8.2 ka event",
        char_start=17,
        char_end=52,
    )


async def _commit(
    maker: Callable[[], AsyncSession],
    *,
    project_id: uuid.UUID,
    title: str,
    body_md: str,
    claims: list[ClaimDraft],
    page_id: uuid.UUID | None = None,
    embed: object | None = None,
) -> CommitResult:
    async with maker() as session:
        result = await WikiService(session).commit_revision(
            principal=_agent(),
            ledger=LedgerWriter(session),
            project_id=project_id,
            page_id=page_id,
            title=title,
            slug=None,
            page_kind="topic",
            body_md=body_md,
            summary=body_md[:120],
            claims=claims,
            wikilinks=[],
            commit_message="belief write path probe",
            embed=embed,  # type: ignore[arg-type]
        )
        await session.commit()
        return result


async def _live_claims(maker: Callable[[], AsyncSession], project_id: uuid.UUID) -> list[WikiClaim]:
    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(WikiClaim).where(
                        WikiClaim.project_id == project_id,
                        WikiClaim.superseded_by.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_a_compiled_claim_carries_a_claim_key(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The column that makes a claim identifiable, and the one this path never
    wrote. `claim_key` NULL is not merely missing data: the unique index that
    enforces one belief per proposition is partial on it, so every NULL row is
    exempt from de-duplication for ever."""
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    claims = await _live_claims(maker, committed_project)
    assert len(claims) == 1
    assert claims[0].claim_key == claim_key_for(CLAIM_TEXT)


async def test_recompiling_a_page_keeps_the_claim_and_moves_it_to_the_new_revision(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Identity is the point of the Claim Spine and this path had none.

    Two things have to hold at once and they pull against each other: the claim
    must keep its ROW (so its citations, edges and any human correction
    survive the recompile) and it must be reachable from the page's CURRENT
    revision (which is how `a2ui_handlers`, `routes/surfaces`, `routes/wiki`,
    `wiki_refresh` and the mechanical reviewer all list a page's claims). A
    naive upsert satisfies the first and breaks every one of those readers.
    """
    first = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    before = await _live_claims(maker, committed_project)
    assert [c.revision_id for c in before] == [first.revision_id]

    second = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        page_id=first.page_id,
        body_md="# Basin\n\nSecond compile, different prose.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    assert second.revision_id != first.revision_id

    after = await _live_claims(maker, committed_project)
    assert len(after) == 1, (
        "the recompile inserted a second row for the same proposition; the "
        "claim's citations and edges stay on the row nobody reads any more"
    )
    assert after[0].id == before[0].id, "the claim lost its identity across a recompile"
    assert after[0].revision_id == second.revision_id, (
        "the claim kept the revision it was born on, so every reader that "
        "lists a page's claims by current_revision_id now shows none"
    )


async def test_recompiling_a_page_does_not_double_its_evidence(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """With durable claim identity the citations land on the SAME claim every
    time, so an insert-per-compile inflates the evidence that confidence is
    derived from. `page_citation_locator` is what makes the second compile a
    union."""
    first = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        page_id=first.page_id,
        body_md="# Basin\n\nSecond compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(Citation).where(Citation.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"the evidence doubled on recompile: {len(rows)} rows"
    assert rows[0].locator_hash is not None, (
        "a citation with no locator is exempt from the union index for ever"
    )


async def test_two_markers_on_one_claim_stay_two_pieces_of_evidence(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The other half of the locator: de-duplication must not become collapse.

    Both citations here carry no source, no chunk and no span — the shape the
    stub and cross-link writers produce — so anything that hashed only the
    anchor would fold them into one row and halve the claim's support count.
    """
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[
            ClaimDraft(
                text=CLAIM_TEXT,
                citations=[
                    CitationDraft(chunk_ids=[], source_page_id=None, citation_marker="[c1]"),
                    CitationDraft(chunk_ids=[], source_page_id=None, citation_marker="[c2]"),
                ],
            )
        ],
    )
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(Citation).where(Citation.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert {r.citation_marker for r in rows} == {"[c1]", "[c2]"}


async def test_a_compiled_claim_is_embedded_when_the_caller_supplies_an_embedder(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`wiki_claims.embedding` was NULL on all 18,038 rows because this path
    had no way to fill it at all — not because the embedder was unreachable."""
    vector = [0.05] * 1024
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
        embed={CLAIM_TEXT: vector}.get,
    )
    claims = await _live_claims(maker, committed_project)
    assert claims[0].embedding is not None, "the claim is invisible to vector search"
    assert len(list(claims[0].embedding)) == 1024


async def test_a_RE_ASSERTED_claim_gets_its_vector_too(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The UPDATE branch, which is the one production actually takes.

    `embedding` was assigned in the insert branch only. The single wired
    production caller — `curator_service.recurate_overview` through
    `_carry_claims` — selects claims that ALREADY EXIST, so every draft it
    produces lands on the update branch. The vector was computed, paid for at
    the gateway, and discarded 100% of the time.

    That is why 18,038 of 18,038 claims on the live stack carried a NULL
    embedding while a test asserting "a compiled claim is embedded" passed: it
    only ever compiled the FIRST time.
    """
    vector = [0.05] * 1024
    first = {"title": "Basin sedimentation", "body_md": "# Basin\n\nFirst compile.\n"}

    # First compile with NO embedder — the row that exists on the live stack.
    await _commit(
        maker,
        project_id=committed_project,
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
        **first,
    )
    claims = await _live_claims(maker, committed_project)
    assert claims[0].embedding is None, "the premise of this test is gone"

    # Recompile with one. Same claim text, so `claim_key` matches and this is
    # an update, not an insert.
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nSecond compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
        embed={CLAIM_TEXT: vector}.get,
    )
    claims = await _live_claims(maker, committed_project)
    assert len(claims) == 1, f"the re-assertion inserted a second row: {len(claims)}"
    assert claims[0].embedding is not None, (
        "a re-asserted claim never gets a vector, so the only path production "
        "takes leaves the HNSW index with nothing to index"
    )
    assert len(list(claims[0].embedding)) == 1024


async def test_a_claim_that_already_has_a_vector_is_not_re_embedded(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Backfill, not re-embed on every recompile.

    A claim's text IS its identity (`claim_key`), so a row that already has a
    vector has the right one. Re-embedding every re-assertion would pay the
    gateway again on every recompile of every page — and without this test the
    fix above would most naturally be written that way.
    """
    original = [0.05] * 1024
    replacement = [0.99] * 1024
    calls: list[str] = []

    def counting_embed(text: str) -> list[float] | None:
        # A DIFFERENT vector each call. Returning the same one both times makes
        # an overwrite indistinguishable from a skip, and the first version of
        # this test did exactly that — mutating the guard to `if True:` left it
        # green.
        calls.append(text)
        return original if len(calls) == 1 else replacement

    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
        embed=counting_embed,
    )
    assert calls == [CLAIM_TEXT], calls

    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nSecond.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
        embed=counting_embed,
    )
    # The embedder may be CALLED (the caller computes eagerly); what must not
    # happen is the stored vector being replaced on a row that already had one.
    claims = await _live_claims(maker, committed_project)
    stored = list(claims[0].embedding)
    assert stored == original, (
        "the vector was replaced on re-assertion, so every recompile of every "
        "page pays the gateway again for a claim whose text — its identity — "
        "did not change"
    )
    assert stored != replacement


async def test_a_compiled_claim_without_an_embedder_is_still_written(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The degradation, stated: no gateway costs the vector, never the belief."""
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    claims = await _live_claims(maker, committed_project)
    assert len(claims) == 1
    assert claims[0].embedding is None


async def test_confidence_is_derived_from_the_evidence_not_from_the_draft(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A producer's opinion of its own evidence is not evidence.

    The draft says `well_supported` and attaches nothing. The column must say
    what the citations say, which is that nothing has been assessed.
    """
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[
            ClaimDraft(
                text=CLAIM_TEXT,
                confidence=Confidence.WELL_SUPPORTED.value,
                citations=[],
            )
        ],
    )
    claims = await _live_claims(maker, committed_project)
    assert claims[0].confidence == Confidence.UNDER_INVESTIGATION.value


async def test_a_draft_confidence_in_no_vocabulary_is_still_refused(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The check survives the column becoming derived.

    806 rows of "cited" is what a page-compile path without this looks like,
    and a value nobody can render is a producer bug whether or not the value
    reaches the column.
    """
    with pytest.raises(ValueError, match="is not a confidence"):
        await _commit(
            maker,
            project_id=committed_project,
            title="Basin sedimentation",
            body_md="# Basin\n\nFirst compile.\n",
            claims=[ClaimDraft(text=CLAIM_TEXT, confidence="excellent")],
        )


async def test_each_compiled_claim_is_ledgered(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Rule 4: every state mutation writes an ActionLedgerEvent in the same
    transaction. The hand-built writer wrote the revision event and nothing for
    the claims, so a belief could appear with no record of who asserted it."""
    await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nFirst compile.\n",
        claims=[
            ClaimDraft(text=CLAIM_TEXT, citations=[_citation()]),
            ClaimDraft(text=OTHER_CLAIM, citations=[_citation("[c2]")]),
        ],
    )
    async with maker() as session:
        kinds = list(
            (
                await session.execute(
                    select(ActionLedgerEvent.action_kind).where(
                        ActionLedgerEvent.project_id == committed_project
                    )
                )
            )
            .scalars()
            .all()
        )
    assert kinds.count("belief.claim.upsert") == 2, kinds


async def _apply_merge(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    source_page_id: uuid.UUID,
    target_page_id: uuid.UUID,
) -> int:
    """Run the real `CuratorService.apply_merge` and return `claims_folded`."""
    async with maker() as session:
        proposal = PageMergeProposal(
            id=uuid.uuid4(),
            project_id=project_id,
            source_page_id=source_page_id,
            target_page_id=target_page_id,
            rationale="near duplicate",
            similarity=0.95,
            status="approved",
            created_by=ACTOR,
        )
        session.add(proposal)
        await session.flush()
        ledger = LedgerWriter(session)
        await CuratorService(session, ledger=ledger, actor_id=ACTOR).apply_merge(
            proposal=proposal, principal=_agent(), ledger=ledger
        )
        event = (
            await session.execute(
                select(ActionLedgerEvent)
                .where(
                    ActionLedgerEvent.project_id == project_id,
                    ActionLedgerEvent.action_kind == "wiki.page.merge",
                )
                .order_by(ActionLedgerEvent.timestamp.desc())
                .limit(1)
            )
        ).scalar_one()
        folded = int(event.payload_jsonb["claims_folded"])
        await session.commit()
        return folded


# --- the curator's merge fold: the second hand-built writer -----------------


async def test_a_merge_folds_the_source_page_claims_as_beliefs(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`curator_service` built `WikiClaim` by hand too, with the same two holes.

    A folded claim was born with no `claim_key` and no `embedding`, so a merge
    — the operation whose entire purpose is to stop the wiki holding the same
    thing twice — created a belief that de-duplication could never see again.
    """
    target = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nThe surviving page.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    source = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation (draft)",
        body_md="# Basin draft\n\nSuperseded prose.\n",
        claims=[ClaimDraft(text=OTHER_CLAIM, citations=[_citation("[c2]")])],
    )
    folded = await _apply_merge(maker, committed_project, source.page_id, target.page_id)
    assert folded == 1, f"the ledger should record the claim the target gained; got {folded}"

    claims = await _live_claims(maker, committed_project)
    by_text = {c.text: c for c in claims}
    assert set(by_text) == {CLAIM_TEXT, OTHER_CLAIM}
    assert by_text[OTHER_CLAIM].page_id == target.page_id, "the merge dropped the claim"
    assert by_text[CLAIM_TEXT].page_id == target.page_id
    assert by_text[OTHER_CLAIM].claim_key == claim_key_for(OTHER_CLAIM)


async def test_a_folded_claim_keeps_the_evidence_that_anchors_it(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The fold used to rebuild each citation from three columns.

    The old writer copied `chunk_ids`, `source_page_id` and the marker and
    dropped the quote, the chunk and the span — so a merge turned a citation
    that quoted a sentence into one that quoted nothing, and the claim →
    chunk → char-span chain the project advertises ended at the merge. Now that
    citations are keyed by their anchor it would be worse than lossy: the
    anchorless copy hashes differently from the row it came from, so the fold
    would ADD an evidence-free citation to the same claim and inflate the
    confidence derived from it.
    """
    source = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation (draft)",
        body_md="# Basin draft\n\nSuperseded prose.\n",
        claims=[ClaimDraft(text=OTHER_CLAIM, citations=[_citation("[c2]")])],
    )
    target = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nThe surviving page.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    await _apply_merge(maker, committed_project, source.page_id, target.page_id)

    async with maker() as session:
        claim = (
            await session.execute(
                select(WikiClaim).where(
                    WikiClaim.project_id == committed_project,
                    WikiClaim.text == OTHER_CLAIM,
                    WikiClaim.superseded_by.is_(None),
                )
            )
        ).scalar_one()
        cites = list(
            (await session.execute(select(Citation).where(Citation.claim_id == claim.id)))
            .scalars()
            .all()
        )
    assert len(cites) == 1, f"the fold added a second citation to the same claim: {len(cites)}"
    assert cites[0].quote == _citation().quote, "the fold dropped the quote"
    assert cites[0].char_start == _citation().char_start
    assert cites[0].char_end == _citation().char_end


async def test_a_merge_folds_a_claim_whose_revision_id_has_gone_stale(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`_carry_claims` selects a page's claims, and a claim's revision moves.

    A claim is durable and a page is not: recompile the page without listing
    the claim again and the row keeps the revision it was last asserted on
    while the page moves ahead. Selecting `revision_id == current_revision_id`
    — which is what the carry did — makes that claim invisible to the merge,
    so the fold silently drops a belief the page still holds. The wiki ingest
    path produces exactly this shape: `_node_claim_extraction` writes claims
    against the revision that has just been committed, and the next curator
    commit moves the page past it.
    """
    target = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation",
        body_md="# Basin\n\nThe surviving page.\n",
        claims=[ClaimDraft(text=CLAIM_TEXT, citations=[_citation()])],
    )
    source = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation (draft)",
        body_md="# Basin draft\n\nFirst.\n",
        claims=[ClaimDraft(text=OTHER_CLAIM, citations=[_citation("[c2]")])],
    )
    # A recompile that does not re-list the claim. The page advances; the
    # belief stays where it was last asserted.
    stale = await _commit(
        maker,
        project_id=committed_project,
        title="Basin sedimentation (draft)",
        page_id=source.page_id,
        body_md="# Basin draft\n\nSecond, and it lists no claims.\n",
        claims=[],
    )
    assert stale.revision_id != source.revision_id

    folded = await _apply_merge(maker, committed_project, source.page_id, target.page_id)
    assert folded == 1, (
        "the merge did not fold a claim whose revision_id had gone stale; the "
        "source page is about to be soft-deleted and the belief goes with it"
    )
