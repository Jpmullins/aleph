"""Retraction blast-radius: what withdrawing a source does to the beliefs on it.

`aleph_reviewer.retraction` is the single funnel every retraction trigger goes
through — the manual `POST /sources/{id}/retract` route and the mechanical
reviewer's DOI-verification branch both call `retract_source`. This file pins
the properties that make it mean anything:

1. **The join key is `Citation.source_id`.** The original blast-radius walked
   `Source → SourcePage → Citation.source_page_id`, and no production write path
   ever populated `source_page_id` — so the feature returned zero rows for its
   entire life while every code path reported success. A test that only asserts
   "the cited claim was flagged" does not catch that: it also passes if the join
   silently matches nothing and the test's own fixture happens to be empty. So
   the join is pinned in BOTH directions here — a claim reachable only through
   `source_page_id` must NOT be flagged, and a claim reachable only through
   `source_id` MUST be.
2. **The declined branch reaches the database.** A claim that still has
   independent supporting evidence is `weakened`, not `retracted`. The walk
   always made that distinction and the write path always threw it away one line
   later; both halves are pinned now.
3. **It says what it reached.** Per-claim ledger rows carrying hop, depth and
   branch, a `source.retract` payload carrying the counts, and a finding whose
   text and `evidence_refs` enumerate the radius. "It reached 12 claims" is not
   a report.
4. **Confidence moves.** A claim held at `well_supported` by three verbatim
   quotes to one paper is not still well supported once that paper is withdrawn.
   `recompute_confidence` excludes evidence from a retracted source and
   `retract_source` re-derives everything it touched in the same transaction —
   otherwise "propagation" is one status column and nothing else.
5. **Two hops.** `retraction_impact` walks `claim_edges.kind='derived_from'`
   transitively, depth-capped and cycle-guarded. Until WS-RS9 nothing in the
   tree wrote such an edge (`ClaimEdge` was constructed in one place, with
   `kind="supersedes"`, and `claim_edges` held zero rows of every other kind),
   so the CTE was correct and blind and this file pinned the second hop as
   unreachable. The writer is `aleph_wiki.derivation.record_derivations`, and
   the two-hop tests below use it rather than hand-inserting a row: a test that
   passes on a row no production code can create reads as proof that the
   feature works.
6. **The mutation and its ledger events are one transaction.** Pinned by
   rolling the transaction back and proving nothing survives, not merely by
   reading rows back through the same open session — which would pass for a
   ledger written on a separate autocommit connection.
7. **Scope.** Retracting one project's source leaves a parallel project alone,
   and no derivation edge can be written across the boundary in the first place
   — the recursive CTE has no project predicate, so one leaked edge is one
   leaked blast radius.

Plus idempotency, because the mechanical reviewer re-checks DOIs on a schedule
and a second pass over an already-retracted source must not file a second
critical finding against the same person's queue.

Real Postgres, because the properties are transactional and the defect being
guarded was precisely a join that returned nothing against real rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aleph_belief.trust import TrustTier
from aleph_core.confidence import Confidence
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_hypotheses.confidence import weight_for_tier
from aleph_reviewer.models import ReviewFinding
from aleph_reviewer.retraction import (
    STATUS_UNSUPPORTED,
    STATUS_WEAKENED,
    retract_source,
    retraction_impact,
)
from aleph_rks.models import Source
from aleph_security.principal import Principal
from aleph_wiki.derivation import record_derivations
from aleph_wiki.models import Citation, ClaimEdge, SourcePage, WikiClaim, WikiPage

pytestmark = pytest.mark.integration

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

#: Project-scoped teardown, in delete order. Named explicitly rather than
#: reflected: DATABASE_URL normally points at the running compose Postgres,
#: which holds a real corpus, so a truncate-everything teardown is a data-loss
#: bug waiting for its first run.
#:
#: `action_ledger_events` is deliberately absent. It carries an append-only
#: DELETE trigger, and `aleph` is a superuser here — so a teardown *could*
#: bypass it with `session_replication_role`. It must not: a fixture that
#: switches off a core invariant to tidy up is how the invariant stops being
#: one. Those rows are scoped to a throwaway project id and interfere with
#: nothing.
_TEARDOWN_SQL = (
    "DELETE FROM citations WHERE project_id = :pid",
    "DELETE FROM claim_edges WHERE project_id = :pid",
    "DELETE FROM wiki_claims WHERE project_id = :pid",
    "DELETE FROM source_pages WHERE project_id = :pid",
    "DELETE FROM wiki_pages WHERE project_id = :pid",
    "DELETE FROM review_findings WHERE project_id = :pid",
    "DELETE FROM review_runs WHERE project_id = :pid",
    "DELETE FROM sources WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


# --------------------------------------------------------------------------
# Fixtures. Local to this file on purpose: `tests/integration/conftest.py` is
# not visible from `tests/e2e/`, and this file must not depend on it.
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Per-test rather than session-scoped: a session-scoped async engine binds its
    asyncpg pool to the first test's event loop, and every later test then fails
    with "attached to a different loop".
    """
    eng = create_async_engine(database_url, poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    """A session factory. The tests here open and close their own transactions,
    because transaction boundaries are part of what is being asserted."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def project_ids(maker: Callable[[], AsyncSession]) -> AsyncIterator[tuple[uuid.UUID, ...]]:
    """Two committed, throwaway project ids, deleted afterwards.

    Two rather than one because scoping is a property under test and it cannot
    be asserted against a single project.
    """
    ids = (uuid7(), uuid7())
    try:
        yield ids
    finally:
        async with maker() as s:
            for pid in ids:
                for statement in _TEARDOWN_SQL:
                    await s.execute(text(statement), {"pid": pid})
            await s.commit()


# --------------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------------


def _principal() -> Principal:
    return Principal(
        user_id=ACTOR,
        subject="retraction-walk",
        email="retraction-walk@example.test",
        actor_kind="user",
    )


async def _seed_project(s: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    """A project plus one page for its claims to hang on. Returns the page id."""
    s.add(
        Project(
            id=project_id,
            title=f"retraction walk {project_id.hex[:8]}",
            model_profile_id=uuid7(),
            created_by=ACTOR,
        )
    )
    page_id = await _add_page(s, project_id, title="Topic")
    await s.flush()
    return page_id


async def _add_page(s: AsyncSession, project_id: uuid.UUID, *, title: str) -> uuid.UUID:
    page = WikiPage(
        id=uuid7(),
        project_id=project_id,
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
        created_by=ACTOR,
    )
    s.add(page)
    await s.flush()
    return page.id


async def _add_source(s: AsyncSession, project_id: uuid.UUID, *, title: str) -> uuid.UUID:
    src = Source(
        id=uuid7(),
        project_id=project_id,
        connector_kind="upload",
        title=title,
        short_id=f"s{uuid.uuid4().hex[:8]}",
        status="ingested",
        source_metadata_jsonb={},
        created_by=ACTOR,
    )
    s.add(src)
    await s.flush()
    return src.id


async def _add_claim(
    s: AsyncSession,
    project_id: uuid.UUID,
    page_id: uuid.UUID,
    *,
    body: str,
    confidence: str = Confidence.WEAKLY_SUPPORTED.value,
) -> uuid.UUID:
    claim = WikiClaim(
        id=uuid7(),
        project_id=project_id,
        page_id=page_id,
        text=body,
        claim_key=uuid.uuid4().hex,
        origin="agent",
        evidence_tier="stated",
        confidence=confidence,
        status="active",
        created_by=ACTOR,
    )
    s.add(claim)
    await s.flush()
    return claim.id


async def _add_citation(
    s: AsyncSession,
    project_id: uuid.UUID,
    claim_id: uuid.UUID,
    *,
    source_id: uuid.UUID | None = None,
    source_page_id: uuid.UUID | None = None,
    stance: str = "supports",
    marker: str = "c1",
    weight: float = 1.0,
) -> uuid.UUID:
    cit = Citation(
        id=uuid7(),
        project_id=project_id,
        claim_id=claim_id,
        source_id=source_id,
        source_page_id=source_page_id,
        stance=stance,
        weight=weight,
        citation_marker=marker,
        quote="the quoted span",
        verbatim=False,
    )
    s.add(cit)
    await s.flush()
    return cit.id


async def _ledger_kinds(
    s: AsyncSession, project_id: uuid.UUID, action_kind: str
) -> list[ActionLedgerEvent]:
    return list(
        (
            await s.execute(
                select(ActionLedgerEvent).where(
                    ActionLedgerEvent.project_id == project_id,
                    ActionLedgerEvent.action_kind == action_kind,
                )
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# 1. It propagates at all
# --------------------------------------------------------------------------


async def test_retracting_a_source_flags_the_claim_that_rests_on_it(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The whole feature in one line: withdraw the paper, the belief is marked."""
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        claim_id = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, claim_id, source_id=source_id)
        await s.commit()

    async with maker() as s:
        result = await retract_source(
            s,
            LedgerWriter(s),
            _principal(),
            source_id=source_id,
            reason="fabricated figures",
        )
        await s.commit()

    assert result.claim_ids == {claim_id}, (
        "the blast radius did not reach the one claim that cites the source — "
        "this is the zero-rows failure mode the join key was changed to fix"
    )
    assert result.page_ids == {page_id}
    assert result.already_retracted is False
    assert result.finding_id is not None

    async with maker() as s:
        source = (await s.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert source.status == "retracted"
        assert source.retracted_at is not None
        assert source.retraction_reason == "fabricated figures"

        claim = (await s.execute(select(WikiClaim).where(WikiClaim.id == claim_id))).scalar_one()
        assert claim.status == "retracted"

        finding = (
            await s.execute(select(ReviewFinding).where(ReviewFinding.id == result.finding_id))
        ).scalar_one()
        # The finding is how a retraction reaches a person: same kind the
        # mechanical reviewer emits, so it lands in the existing Briefs surface
        # rather than in a second inbox nobody opens.
        assert finding.finding_kind == "retracted_source"
        assert finding.severity == "critical"
        assert finding.target_source_id == source_id


# --------------------------------------------------------------------------
# 2. The join key
# --------------------------------------------------------------------------


async def test_the_blast_radius_joins_on_source_id_not_on_source_page_id(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Pinned in both directions, because one direction alone proves nothing.

    `source_page_id` was the old join key and no writer ever populated it, so the
    walk matched nothing. The seed here builds the exact counterexample: claim
    `via_source_id` is reachable ONLY through `Citation.source_id`, and claim
    `via_source_page_id` is reachable ONLY through the legacy
    `Source → SourcePage → Citation.source_page_id` chain, which is fully wired
    up here (the `SourcePage` row exists) so that reverting the join would
    genuinely find it.

    Revert `retraction_impact` to the old join and BOTH assertions flip.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")

        # The source's own wiki page, and the SourcePage row that bridges them —
        # what the legacy join walked.
        source_page_id = await _add_page(s, project_id, title="Source page")
        s.add(
            SourcePage(
                id=uuid7(),
                project_id=project_id,
                source_id=source_id,
                page_id=source_page_id,
                extracted_claims_jsonb=[],
                extracted_at=utcnow(),
            )
        )

        via_source_id = await _add_claim(s, project_id, page_id, body="Reachable by source_id.")
        await _add_citation(s, project_id, via_source_id, source_id=source_id)

        via_page_id = await _add_claim(s, project_id, page_id, body="Reachable by source_page_id.")
        await _add_citation(
            s, project_id, via_page_id, source_id=None, source_page_id=source_page_id
        )
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, source_id)

    assert via_source_id in impact.directly_cited, (
        "a citation carrying source_id was not found — the live join key is broken"
    )
    assert via_page_id not in impact.all_touched, (
        "a citation carrying only source_page_id was found, so the walk is back on "
        "the column no production writer populates"
    )


# --------------------------------------------------------------------------
# 3. The declined branch
# --------------------------------------------------------------------------


async def test_a_claim_with_independent_surviving_evidence_is_declined_not_unsupported(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The declined branch: two supports minus one is not zero supports.

    `unsupported` is the claim whose footing is gone. `weakened` is the claim
    that also rests on something the retraction did not touch — it stays
    believed, annotated that one of its supports was withdrawn.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        retracted = await _add_source(s, project_id, title="The retracted paper")
        independent = await _add_source(s, project_id, title="An unrelated replication")

        sole = await _add_claim(s, project_id, page_id, body="Rests only on the retracted paper.")
        await _add_citation(s, project_id, sole, source_id=retracted)

        corroborated = await _add_claim(s, project_id, page_id, body="Also replicated elsewhere.")
        await _add_citation(s, project_id, corroborated, source_id=retracted, marker="c1")
        await _add_citation(
            s, project_id, corroborated, source_id=independent, marker="c2", stance="supports"
        )
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, retracted)

    assert impact.all_touched == {sole, corroborated}
    assert impact.unsupported == {sole}, (
        "a claim whose only support was withdrawn must be unsupported"
    )
    assert impact.weakened == {corroborated}, (
        "a claim with independent surviving evidence was killed rather than "
        "declined — flagging both identically trains a reader to ignore the flag"
    )


async def test_the_declined_branch_survives_the_write_path(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The distinction the impact walk makes must reach the database.

    THIS TEST WAS INVERTED BY WS-RS9, and the previous version is worth knowing
    about: `retraction_impact` separated `weakened` from `unsupported`,
    `retract_source` then called `dependent_claims` — which flattens the two back
    into `all_touched` — and wrote `status="retracted"` onto every one. The
    declined branch was computed and thrown away one line later, so a claim with
    independent surviving evidence was killed anyway on the only path that
    mutates anything, and the finding's own text said "flagged
    retracted/contested" while writing exactly one of those two words.

    Now the two branches get two statuses. `weakened` is not `contested`:
    `contested` is a *confidence* state meaning the evidence points both ways,
    and it is also the status a person's page rejection writes. A withdrawn
    support is neither.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        retracted = await _add_source(s, project_id, title="The retracted paper")
        independent = await _add_source(s, project_id, title="An unrelated replication")

        corroborated = await _add_claim(s, project_id, page_id, body="Also replicated elsewhere.")
        await _add_citation(s, project_id, corroborated, source_id=retracted, marker="c1")
        await _add_citation(s, project_id, corroborated, source_id=independent, marker="c2")

        orphaned = await _add_claim(s, project_id, page_id, body="Rests on the bad paper alone.")
        await _add_citation(s, project_id, orphaned, source_id=retracted, marker="c1")
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, retracted)
        assert impact.weakened == {corroborated}
        assert impact.unsupported == {orphaned}

    async with maker() as s:
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=retracted, reason="fabricated figures"
        )
        await s.commit()

    assert result.weakened == {corroborated}
    assert result.unsupported == {orphaned}

    async with maker() as s:
        rows = {
            c.id: c
            for c in (
                await s.execute(select(WikiClaim).where(WikiClaim.id.in_([corroborated, orphaned])))
            )
            .scalars()
            .all()
        }
        assert rows[orphaned].status == STATUS_UNSUPPORTED
        assert rows[corroborated].status == STATUS_WEAKENED, (
            "the claim with independent surviving evidence was killed rather than "
            "declined — flagging both identically trains a reader to ignore the flag"
        )


async def test_the_ledger_says_which_branch_each_claim_fell_into(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """ "It reached 12 claims" is not a report; which 12, and how, is.

    A per-claim ledger row saying only "flagged" cannot answer "why does this
    page read differently today", which is the one question a retraction exists
    to be answerable.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        retracted = await _add_source(s, project_id, title="The retracted paper")
        independent = await _add_source(s, project_id, title="A replication")

        orphaned = await _add_claim(s, project_id, page_id, body="Rests on the bad paper alone.")
        await _add_citation(s, project_id, orphaned, source_id=retracted)
        corroborated = await _add_claim(s, project_id, page_id, body="Also replicated elsewhere.")
        await _add_citation(s, project_id, corroborated, source_id=retracted, marker="c1")
        await _add_citation(s, project_id, corroborated, source_id=independent, marker="c2")
        await s.commit()

    async with maker() as s:
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=retracted, reason="fabricated figures"
        )
        await s.commit()

    async with maker() as s:
        events = await _ledger_kinds(s, project_id, "wiki_claim.retract_flag")
        by_claim = {e.target_id: e.payload_jsonb for e in events}
        assert set(by_claim) == {orphaned, corroborated}
        assert by_claim[orphaned]["branch"] == "unsupported"
        assert by_claim[orphaned]["status"] == STATUS_UNSUPPORTED
        assert by_claim[corroborated]["branch"] == "weakened"
        assert by_claim[corroborated]["hop"] == "direct"
        assert by_claim[corroborated]["depth"] == 0

        source_events = await _ledger_kinds(s, project_id, "source.retract")
        assert len(source_events) == 1
        assert source_events[0].payload_jsonb["claims_reached"] == 2
        assert source_events[0].payload_jsonb["claims_unsupported"] == 1
        assert source_events[0].payload_jsonb["claims_weakened"] == 1

    # And the same numbers reach a person, in the finding, in words.
    assert "2 claim(s)" in result.summary
    assert "1 left with no surviving support" in result.summary
    async with maker() as s:
        finding = (
            await s.execute(select(ReviewFinding).where(ReviewFinding.id == result.finding_id))
        ).scalar_one()
        assert "1 left with no surviving support" in finding.description
        kinds = {ref.get("kind") for ref in finding.evidence_refs_jsonb}
        assert kinds == {"source", "wiki_claim"}, (
            "the finding names the source but not the claims it reached, so the "
            "blast radius is only recoverable by re-running the query"
        )


async def test_a_retraction_moves_the_confidence_it_bought(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Propagation is not a status column.

    A claim held at `well_supported` by three verbatim citations to one paper is
    not still well supported when that paper is withdrawn. `recompute_confidence`
    excludes evidence from a retracted source, and `retract_source` re-derives
    every claim it touched in the same transaction — so the belief layer, not
    just a flag, reflects the withdrawal.
    """
    project_id = project_ids[0]
    earned = weight_for_tier(TrustTier.EARNED)
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        claim_id = await _add_claim(
            s,
            project_id,
            page_id,
            body="Three earned quotes say so.",
            confidence=Confidence.WELL_SUPPORTED.value,
        )
        for i in range(3):
            await _add_citation(
                s, project_id, claim_id, source_id=source_id, marker=f"c{i}", weight=earned
            )
        await s.commit()

    async with maker() as s:
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    assert claim_id in result.confidence_changed
    before, after = result.confidence_changed[claim_id]
    assert before == Confidence.WELL_SUPPORTED.value
    assert after == Confidence.UNDER_INVESTIGATION.value, (
        "the claim kept the confidence the withdrawn paper bought it — the "
        "retraction wrote a status and nothing else"
    )

    async with maker() as s:
        claim = (await s.execute(select(WikiClaim).where(WikiClaim.id == claim_id))).scalar_one()
        assert claim.confidence == Confidence.UNDER_INVESTIGATION.value
        assert claim.status == STATUS_UNSUPPORTED


# --------------------------------------------------------------------------
# 4. One transaction
# --------------------------------------------------------------------------


async def test_the_retraction_and_its_ledger_events_commit_together(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Every mutation gets an `ActionLedgerEvent`: one for the source, one per claim."""
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        claim_id = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, claim_id, source_id=source_id)
        await s.commit()

    async with maker() as s:
        await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    async with maker() as s:
        retracts = await _ledger_kinds(s, project_id, "source.retract")
        assert len(retracts) == 1
        assert retracts[0].target_id == source_id
        assert retracts[0].target_kind == "source"
        assert retracts[0].payload_jsonb["reason"] == "fabricated figures"

        flags = await _ledger_kinds(s, project_id, "wiki_claim.retract_flag")
        assert len(flags) == 1
        assert flags[0].target_id == claim_id
        assert flags[0].payload_jsonb["source_id"] == str(source_id)


async def test_a_rolled_back_retraction_leaves_neither_the_flag_nor_the_ledger_event(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """ "Same transaction" means the ledger dies with the mutation.

    Reading the events back through the still-open session proves only that they
    were written somewhere. A ledger writer that opened its own connection would
    pass that and still leave an event describing a retraction that never
    happened. Rolling back is the assertion that catches it.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        claim_id = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, claim_id, source_id=source_id)
        await s.commit()

    async with maker() as s:
        await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        # Visible inside the transaction that made them...
        assert len(await _ledger_kinds(s, project_id, "source.retract")) == 1
        await s.rollback()

    # ...and gone with it.
    async with maker() as s:
        source = (await s.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert source.status == "ingested"
        assert source.retracted_at is None

        claim = (await s.execute(select(WikiClaim).where(WikiClaim.id == claim_id))).scalar_one()
        assert claim.status == "active"

        assert await _ledger_kinds(s, project_id, "source.retract") == [], (
            "a ledger event survived a rolled-back retraction, so the ledger is "
            "not writing in the caller's transaction"
        )
        assert await _ledger_kinds(s, project_id, "wiki_claim.retract_flag") == []


async def test_retracting_twice_neither_re_ledgers_nor_re_flags(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Idempotent, because the mechanical reviewer re-checks DOIs on a schedule.

    Without this, every reviewer pass over an already-retracted source would add
    another critical finding to the same person's queue.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        claim_id = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, claim_id, source_id=source_id)
        await s.commit()

    async with maker() as s:
        await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    async with maker() as s:
        second = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    assert second.already_retracted is True
    assert second.claim_ids == {claim_id}, "the second call still reports the blast radius"
    assert second.finding_id is None

    async with maker() as s:
        assert len(await _ledger_kinds(s, project_id, "source.retract")) == 1
        assert len(await _ledger_kinds(s, project_id, "wiki_claim.retract_flag")) == 1
        findings = (
            await s.execute(select(ReviewFinding).where(ReviewFinding.project_id == project_id))
        ).scalars()
        assert len(list(findings)) == 1, "a re-check queued a duplicate critical finding"


# --------------------------------------------------------------------------
# 5. Scope
# --------------------------------------------------------------------------


async def test_retraction_does_not_reach_across_projects(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Two projects that ingested the SAME paper. Retracting it in one is not
    retracting it in the other.

    This is the realistic shape, not a contrived one: the same DOI ingested into
    two projects produces two `Source` rows with the same title, the same URL and
    (often) the same extracted claim text. Everything about them matches except
    the id and the project. So a blast radius that widens even slightly — joining
    on title, on URL, on claim text — reaches across the boundary here.

    Note what this does and does not cover. `retraction_impact` filters on
    `Citation.source_id` alone, with no `project_id` predicate; scoping holds
    because a source id is globally unique and a citation never names another
    project's source. This asserts the observable consequence (the sibling
    project's rows, findings and ledger are untouched); it does not assert that
    the query itself carries a project filter, because it does not.
    """
    kept, other = project_ids[0], project_ids[1]
    async with maker() as s:
        kept_page = await _seed_project(s, kept)
        kept_source = await _add_source(s, kept, title="The retracted paper")
        kept_claim = await _add_claim(s, kept, kept_page, body="Quokkas photosynthesize.")
        await _add_citation(s, kept, kept_claim, source_id=kept_source)

        other_page = await _seed_project(s, other)
        # Same title as the retracted one — the same paper, ingested twice.
        other_source = await _add_source(s, other, title="The retracted paper")
        other_claim = await _add_claim(s, other, other_page, body="Quokkas photosynthesize.")
        await _add_citation(s, other, other_claim, source_id=other_source)
        await s.commit()

    async with maker() as s:
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=kept_source, reason="fabricated figures"
        )
        await s.commit()

    assert result.claim_ids == {kept_claim}
    assert other_claim not in result.claim_ids

    async with maker() as s:
        neighbour = (
            await s.execute(select(WikiClaim).where(WikiClaim.id == other_claim))
        ).scalar_one()
        assert neighbour.status == "active", "a retraction crossed into another project"

        neighbour_source = (
            await s.execute(select(Source).where(Source.id == other_source))
        ).scalar_one()
        assert neighbour_source.retracted_at is None

        assert await _ledger_kinds(s, other, "source.retract") == []
        assert await _ledger_kinds(s, other, "wiki_claim.retract_flag") == []

        findings = (
            await s.execute(select(ReviewFinding).where(ReviewFinding.project_id == other))
        ).scalars()
        assert list(findings) == [], "the finding was filed under the wrong project"


# --------------------------------------------------------------------------
# 6. The second hop, honestly
# --------------------------------------------------------------------------


async def test_a_retraction_reaches_a_claim_derived_from_a_claim(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The second hop, now that something writes the edge.

    THIS TEST WAS INVERTED BY WS-RS9. The previous version was
    `test_the_second_hop_is_unreachable_because_nothing_writes_a_derived_from_edge`
    and it asserted the conclusion was NOT reached — honestly, because
    `retraction_impact` walked a `derived_from` edge that no code in the tree
    created. `ClaimEdge` was constructed in exactly one place
    (`BeliefService.supersede`, `kind="supersedes"`) and `claim_edges` held zero
    rows of every other kind. The CTE was correct and blind.

    The writer is `aleph_wiki.derivation.record_derivations`, and it is used
    here rather than a hand-inserted row on purpose: a two-hop test that passes
    on a row no production code can create reads as proof that two-hop
    propagation works, which is exactly the claim the old test refused to make.

    Shape: source S ← cited by claim A ← derived_from by conclusion B ←
    derived_from by C. Retract S and all three are reached, at depths 0, 1, 2.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")

        cited = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, cited, source_id=source_id)
        conclusion = await _add_claim(
            s, project_id, page_id, body="Quokka husbandry should therefore be revised."
        )
        downstream = await _add_claim(
            s, project_id, page_id, body="The 2019 husbandry guidance needs rewriting."
        )
        await s.commit()

    async with maker() as s:
        written = await record_derivations(
            s,
            ledger=LedgerWriter(s),
            principal=_principal(),
            project_id=project_id,
            claim_id=conclusion,
            derived_from=[cited],
        )
        assert written == [cited]
        await record_derivations(
            s,
            ledger=LedgerWriter(s),
            principal=_principal(),
            project_id=project_id,
            claim_id=downstream,
            derived_from=[conclusion],
        )
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, source_id)
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    assert impact.directly_cited == {cited}
    assert impact.derived == {conclusion, downstream}, (
        "the walk stopped at the citation join — a conclusion built on a claim "
        "built on a retracted paper is affected, and a citation lookup cannot see it"
    )
    assert impact.depth_by_claim == {cited: 0, conclusion: 1, downstream: 2}
    assert result.claim_ids == {cited, conclusion, downstream}
    assert "2 derived from those (deepest hop 2)" in result.summary

    async with maker() as s:
        rows = {
            c.id: c
            for c in (await s.execute(select(WikiClaim).where(WikiClaim.project_id == project_id)))
            .scalars()
            .all()
        }
        # None of the three has independent evidence, so all three are unsupported.
        assert {rows[c].status for c in (cited, conclusion, downstream)} == {STATUS_UNSUPPORTED}

        events = await _ledger_kinds(s, project_id, "wiki_claim.retract_flag")
        by_claim = {e.target_id: e.payload_jsonb for e in events}
        assert by_claim[cited]["hop"] == "direct"
        assert by_claim[downstream]["hop"] == "derived"
        assert by_claim[downstream]["depth"] == 2


async def test_the_derivation_walk_is_depth_capped_and_cycle_safe(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """A chain longer than the cap stops at the cap; a cycle terminates.

    Both guards are in the CTE and neither was ever exercised, because no edge
    existed to walk. An unbounded walk over a cyclic graph is a hung request in
    the one code path a person triggers by hand and waits on.
    """
    project_id = project_ids[0]
    depth_cap = 4
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")
        chain = [
            await _add_claim(s, project_id, page_id, body=f"Link {i} in the chain.")
            for i in range(depth_cap + 3)
        ]
        await _add_citation(s, project_id, chain[0], source_id=source_id)
        await s.commit()

    async with maker() as s:
        ledger = LedgerWriter(s)
        for child, parent in zip(chain[1:], chain, strict=False):
            await record_derivations(
                s,
                ledger=ledger,
                principal=_principal(),
                project_id=project_id,
                claim_id=child,
                derived_from=[parent],
            )
        # Close the loop: the last link is also a parent of the first.
        await record_derivations(
            s,
            ledger=ledger,
            principal=_principal(),
            project_id=project_id,
            claim_id=chain[0],
            derived_from=[chain[-1]],
        )
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, source_id)

    assert max(impact.depth_by_claim.values()) <= depth_cap
    assert len(impact.derived) == depth_cap, (
        "the walk did not stop at MAX_DERIVATION_DEPTH; a pathological chain "
        "turns one retraction into a full-table walk"
    )


async def test_a_derivation_edge_cannot_be_written_across_projects(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The write-side half of "retraction does not reach across projects".

    The recursive CTE has no project predicate — it trusts the edges. One edge
    pointing into another project is therefore one leaked blast radius, and the
    read-side scope test below would still pass because it seeds no such edge.
    """
    a, b = project_ids
    async with maker() as s:
        page_a = await _seed_project(s, a)
        page_b = await _seed_project(s, b)
        here = await _add_claim(s, a, page_a, body="A claim in project A.")
        elsewhere = await _add_claim(s, b, page_b, body="A claim in project B.")
        await s.commit()

    async with maker() as s:
        written = await record_derivations(
            s,
            ledger=LedgerWriter(s),
            principal=_principal(),
            project_id=a,
            claim_id=here,
            derived_from=[elsewhere, here],
        )
        await s.commit()

    assert written == [], "an edge was written to another project's claim, or to itself"
    async with maker() as s:
        edges = (
            await s.execute(
                select(func.count()).select_from(ClaimEdge).where(ClaimEdge.project_id == a)
            )
        ).scalar_one()
        assert edges == 0


async def test_recording_the_same_derivation_twice_writes_one_edge(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """Idempotence, because a rebuild re-derives the same graph from the same sources."""
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        parent = await _add_claim(s, project_id, page_id, body="The premise.")
        child = await _add_claim(s, project_id, page_id, body="The conclusion.")
        await s.commit()

    for _ in range(2):
        async with maker() as s:
            written = await record_derivations(
                s,
                ledger=LedgerWriter(s),
                principal=_principal(),
                project_id=project_id,
                claim_id=child,
                derived_from=[parent, parent],
            )
            await s.commit()
        assert written == [parent]

    async with maker() as s:
        edges = (
            await s.execute(
                select(func.count())
                .select_from(ClaimEdge)
                .where(ClaimEdge.project_id == project_id, ClaimEdge.kind == "derived_from")
            )
        ).scalar_one()
        assert edges == 1
