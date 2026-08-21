"""Retraction blast-radius: what withdrawing a source does to the beliefs on it.

`aleph_reviewer.retraction` is the single funnel every retraction trigger goes
through — the manual `POST /sources/{id}/retract` route and the mechanical
reviewer's DOI-verification branch both call `retract_source`. This file pins
the four properties that make it mean anything:

1. **The join key is `Citation.source_id`.** The original blast-radius walked
   `Source → SourcePage → Citation.source_page_id`, and no production write path
   ever populated `source_page_id` — so the feature returned zero rows for its
   entire life while every code path reported success. A test that only asserts
   "the cited claim was flagged" does not catch that: it also passes if the join
   silently matches nothing and the test's own fixture happens to be empty. So
   the join is pinned in BOTH directions here — a claim reachable only through
   `source_page_id` must NOT be flagged, and a claim reachable only through
   `source_id` MUST be.
2. **The declined branch.** A claim that still has independent supporting
   evidence is `weakened`, not `unsupported`. Flagging both identically is how a
   flag stops being read.
3. **The mutation and its ledger events are one transaction.** Pinned by
   rolling the transaction back and proving nothing survives, not merely by
   reading rows back through the same open session — which would pass for a
   ledger written on a separate autocommit connection.
4. **Scope.** Retracting one project's source leaves a parallel project alone.

**What this file does NOT prove.** `retraction_impact` contains a recursive CTE
over `claim_edges.kind = 'derived_from'` — the second hop, a claim built on a
claim built on the retracted paper. Nothing in the tree writes such an edge:
`ClaimEdge` is constructed in exactly one place
(`aleph_wiki.belief_service.BeliefService.supersede`) with `kind="supersedes"`,
and `claim_edges` holds 0 rows of every kind on the live stack. The second hop
is therefore pinned below as the one-hop reality it actually is —
`test_the_second_hop_is_unreachable_because_nothing_writes_a_derived_from_edge`
— and not as working capability. See `docs/plan.md` WS-RS9.

Real Postgres, because the properties are transactional and the defect being
guarded was precisely a join that returned nothing against real rows.
"""

from __future__ import annotations

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

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_reviewer.models import ReviewFinding
from aleph_reviewer.retraction import dependent_claims, retract_source, retraction_impact
from aleph_rks.models import Source
from aleph_security.principal import Principal
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
    import os

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
    s: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID, *, body: str
) -> uuid.UUID:
    claim = WikiClaim(
        id=uuid7(),
        project_id=project_id,
        page_id=page_id,
        text=body,
        claim_key=uuid.uuid4().hex,
        origin="agent",
        evidence_tier="stated",
        confidence="cited",
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
) -> uuid.UUID:
    cit = Citation(
        id=uuid7(),
        project_id=project_id,
        claim_id=claim_id,
        source_id=source_id,
        source_page_id=source_page_id,
        stance=stance,
        weight=1.0,
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


async def test_the_declined_branch_is_computed_and_then_discarded_by_the_write_path(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """PINS CURRENT BEHAVIOUR, NOT DESIRED BEHAVIOUR.

    `retraction_impact` separates `weakened` from `unsupported`, and its own
    docstring says flagging both identically would train a reader to ignore the
    flag. `retract_source` then calls `dependent_claims` — which flattens the two
    back into `all_touched` — and writes `status="retracted"` onto every one. Its
    own finding text says "flagged retracted/contested"; it writes exactly one of
    those two words.

    So the corroborated claim, which the declined branch correctly spared, is
    killed anyway on the only path that actually mutates anything.

    When that is fixed, this test SHOULD go red, and the assertion below becomes
    `contested` (or whatever status the fix chooses). Do not delete this test to
    make the fix green — change it, and move the entry out of CLAUDE.md's
    "Known broken" with this file named as the pin.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        retracted = await _add_source(s, project_id, title="The retracted paper")
        independent = await _add_source(s, project_id, title="An unrelated replication")

        corroborated = await _add_claim(s, project_id, page_id, body="Also replicated elsewhere.")
        await _add_citation(s, project_id, corroborated, source_id=retracted, marker="c1")
        await _add_citation(s, project_id, corroborated, source_id=independent, marker="c2")
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, retracted)
        assert impact.weakened == {corroborated}
        # `dependent_claims` is the flattening step: it returns `all_touched`,
        # so the distinction the impact walk just made is gone before the write.
        deps = await dependent_claims(s, retracted)
        assert [c for _, c in deps] == [corroborated]

    async with maker() as s:
        await retract_source(
            s, LedgerWriter(s), _principal(), source_id=retracted, reason="fabricated figures"
        )
        await s.commit()

    async with maker() as s:
        claim = (
            await s.execute(select(WikiClaim).where(WikiClaim.id == corroborated))
        ).scalar_one()
        assert claim.status == "retracted", (
            "behaviour changed: the write path now distinguishes the declined branch. "
            "Good — update this assertion to the new status."
        )
        # Confidence is DERIVED from evidence and is deliberately not touched, so
        # the next recompute cannot silently erase a value its state machine
        # could never produce.
        assert claim.confidence == "cited"


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


async def test_the_second_hop_is_unreachable_because_nothing_writes_a_derived_from_edge(
    maker: Callable[[], AsyncSession], project_ids: tuple[uuid.UUID, ...]
) -> None:
    """The two-hop case, pinned as the one-hop reality it currently is.

    `retraction_impact` contains a depth-capped, cycle-guarded recursive CTE over
    `claim_edges.kind = 'derived_from'`, so a conclusion built on a claim built
    on the retracted paper would be reached. Nothing produces those edges.
    `ClaimEdge` is constructed in exactly one place in the tree —
    `aleph_wiki.belief_service.BeliefService.supersede` — and it writes
    `kind="supersedes"`. `claim_edges` holds 0 rows of every kind on the live
    stack.

    So this seeds the shape a two-hop propagation would need — a conclusion that
    stands on a directly-cited claim — through the only mechanism the system
    actually has, which is nothing, and asserts the conclusion is NOT reached.
    That is not a bug in the CTE; it is a missing writer. This test deliberately
    does NOT hand-insert a `derived_from` row, because passing on a row no
    production code can create would read as proof that two-hop propagation
    works.

    When a writer lands (docs/plan.md WS-RS9), this test is the one to invert:
    assert `conclusion in impact.derived`, and delete the `claim_edges` count
    assertion below.
    """
    project_id = project_ids[0]
    async with maker() as s:
        page_id = await _seed_project(s, project_id)
        source_id = await _add_source(s, project_id, title="The retracted paper")

        cited = await _add_claim(s, project_id, page_id, body="Quokkas photosynthesize.")
        await _add_citation(s, project_id, cited, source_id=source_id)

        # Stands on `cited` in prose and in intent. In the database it stands on
        # nothing, because no code path records the dependency.
        conclusion = await _add_claim(
            s, project_id, page_id, body="Quokka husbandry should therefore be revised."
        )
        await s.commit()

    async with maker() as s:
        impact = await retraction_impact(s, source_id)
        result = await retract_source(
            s, LedgerWriter(s), _principal(), source_id=source_id, reason="fabricated figures"
        )
        await s.commit()

    assert impact.directly_cited == {cited}
    assert impact.derived == set(), "an edge appeared that no writer creates"
    assert conclusion not in result.claim_ids

    async with maker() as s:
        # The real finding: the graph the second hop walks has no edges in it,
        # and the retraction write path did not create one either.
        edges = (
            await s.execute(
                select(func.count())
                .select_from(ClaimEdge)
                .where(ClaimEdge.project_id == project_id)
            )
        ).scalar_one()
        assert edges == 0

        downstream = (
            await s.execute(select(WikiClaim).where(WikiClaim.id == conclusion))
        ).scalar_one()
        assert downstream.status == "active", (
            "the derived claim was flagged, which would mean an edge exists — "
            "if a writer landed, invert this test rather than deleting it"
        )
