"""The Claim Spine's write path, against a real Postgres.

`aleph_wiki.belief_service.BeliefService` has **never run in production.** The
deployed database holds 796 `wiki_claims` rows with zero verbatim quotes and
zero `claim_edges`, because every one of them was written by the older
`commit_revision` path, which inserts a fresh claim row per compile. So nothing
here is a regression test in the usual sense: it is the first evidence that the
service does what its docstrings say, executed against the schema as migrated
rather than against a mock session.

The four defects this file exists to keep dead, each of which the previous claim
layer had:

1. **A claim did not survive a page rewrite.** Identity was the row, so a
   recompile discarded the claim's citations, its verdicts and any human
   correction. Identity is now `(project_id, claim_key)` among live rows, held
   by a partial unique index, and re-asserting a proposition updates the belief
   in place.
2. **Confidence was whatever was written into the column.** It is now derived by
   `next_confidence_from_evidence` in the same transaction as the citation
   write, from the citations and edges that exist. `test_support_counts_are_
   recomputed_not_asserted` poisons the columns by hand and proves a recompute
   overwrites the lie.
3. **A citation could assert anything.** Quotes now go through
   `aleph_core.grounding.ground` against source text the *harness* supplies, and
   an ungroundable quote is refused rather than stored with a flag.
4. **Deduplication cost one judge-tier LLM call per page.** `propose_merges` is
   deterministic and calls no model — pinned here by making every outbound HTTP
   request raise for the duration of the call, not by reading the source.

**What this file does NOT prove, stated plainly.**

* `rebuild` is a re-derivation, not a reconstruction. It upserts what the
  extractor emits and **never removes** a claim the current sources no longer
  support, so a claim whose source was deleted stays live with `claims_after`
  counting it. `test_the_belief_graph_rebuilds_from_its_sources` pins what the
  method does; there is no pruning behaviour to pin.
* `rebuild` has no interaction with a user claim of the *same* proposition. If
  an extractor re-derives a proposition a human already owns, `upsert_claim`
  raises `PermissionDenied` and the whole rebuild aborts part-way, leaving the
  claims written so far. That is deliberately not asserted below: the human
  correction is indeed not destroyed, but "aborts the run" is a plausible bug
  rather than a guarantee, and pinning it would make a future fix look like a
  regression.
* `citations_written` in the returned result counts *attempted* writes, not new
  rows. Re-deriving one span twice reports 1 twice while the table holds one
  row. Both tests that touch it assert the table, and say so.
* No LLM extractor is exercised anywhere. `Extractor` is injected, and the
  fixture below is a dict lookup. That is the point of the injection seam, but
  it means nothing here says a real extractor produces groundable quotes.
* Confidence thresholds themselves belong to `aleph_hypotheses.confidence` and
  are unit-tested there. What is pinned here is that the *stored column* tracks
  them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aleph_belief.patch import PatchOperation, PatchStatus
from aleph_belief.reconcile import Candidate, RejectReason
from aleph_belief.trust import TrustTier
from aleph_core.errors import PermissionDenied
from aleph_core.ids import uuid7
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import ActorKind, Principal
from aleph_wiki.belief_service import (
    BeliefService,
    ClaimUpsert,
    EvidenceDraft,
    SourceText,
    claim_key_for,
)
from aleph_wiki.models import Citation, ClaimEdge, WikiClaim, WikiPage

pytestmark = pytest.mark.integration

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

HUMAN = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
AGENT = uuid.UUID("00000000-0000-0000-0000-0000000000a2")

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
    "DELETE FROM wiki_pages WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


# ---------------------------------------------------------------------------
# Source material. Real prose, because `ground` normalises text and a fixture
# of "aaa bbb" would not exercise the punctuation, digits and casing that make
# quote matching interesting.
# ---------------------------------------------------------------------------

SOURCE_A = (
    "Sedimentation rates in the Gulf of Corinth rose sharply after the 8.2 ka event. "
    "The varve record thickens by a factor of three across the same interval. "
    "Radiocarbon dates from core GC-14 bracket the transition to within forty years."
)

SOURCE_B = (
    "Reanalysis of the same cores finds no change in sedimentation rate at 8.2 ka; "
    "the apparent increase is an artefact of the age model used in the original study."
)

QUOTE_A1 = "rose sharply after the 8.2 ka event"
QUOTE_A2 = "The varve record thickens by a factor of three"
QUOTE_A3 = "Radiocarbon dates from core GC-14 bracket the transition"
QUOTE_B1 = "finds no change in sedimentation rate at 8.2 ka"

CLAIM_TEXT = "Sedimentation in the Gulf of Corinth accelerated after the 8.2 ka event"


# ---------------------------------------------------------------------------
# Fixtures. Local to this file on purpose: `tests/integration/conftest.py` is
# not visible from `tests/e2e/`, and this file must not depend on it.
# ---------------------------------------------------------------------------


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
    """A session factory. Tests commit, so rows are really written — the whole
    question about this service is whether its writes land in Postgres."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def project_id(maker: Callable[[], AsyncSession]) -> AsyncIterator[uuid.UUID]:
    """One committed, throwaway project id, deleted afterwards."""
    pid = uuid7()
    try:
        yield pid
    finally:
        async with maker() as s:
            for statement in _TEARDOWN_SQL:
                await s.execute(text(statement), {"pid": pid})
            await s.commit()


@pytest.fixture
async def session(maker: Callable[[], AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as s:
        yield s


@pytest.fixture
async def page_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    """A project and one page for the claims to hang on, committed.

    `wiki_claims.page_id` is NOT NULL with no foreign key, so a bare uuid would
    work — a real page is used anyway so the rows look like production rows and
    the teardown exercises the same delete order.
    """
    session.add(
        Project(
            id=project_id,
            title=f"belief spine {project_id.hex[:8]}",
            model_profile_id=uuid7(),
            created_by=HUMAN,
        )
    )
    page = WikiPage(
        id=uuid7(),
        project_id=project_id,
        title="Gulf of Corinth",
        slug=f"gulf-of-corinth-{uuid.uuid4().hex[:8]}",
        created_by=HUMAN,
    )
    session.add(page)
    await session.commit()
    return page.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal(user_id: uuid.UUID, kind: ActorKind = "user") -> Principal:
    return Principal(
        user_id=user_id,
        subject=f"belief-spine-{kind}",
        email=f"belief-spine-{kind}@example.test",
        actor_kind=kind,
    )


def _human() -> Principal:
    return _principal(HUMAN, "user")


def _agent() -> Principal:
    return _principal(AGENT, "aleph_agent")


def _evidence(
    source_id: uuid.UUID,
    quote: str,
    source_text: str,
    *,
    stance: str = "supports",
    weight: float = 1.0,
    chunk_id: uuid.UUID | None = None,
) -> EvidenceDraft:
    return EvidenceDraft(
        source_id=source_id,
        quote=quote,
        source_text=source_text,
        chunk_id=chunk_id,
        stance=stance,
        weight=weight,
    )


async def _citations(session: AsyncSession, claim_id: uuid.UUID) -> list[Citation]:
    """Citations for one claim, ordered so assertions do not depend on scan order.

    `populate_existing` because the service writes citations with a Core
    `INSERT ... ON CONFLICT DO UPDATE`, which the ORM identity map knows nothing
    about. Without it a re-read hands back the object this session loaded before
    the upsert and the test reports the pre-conflict values — passing or failing
    on SQLAlchemy's cache rather than on what Postgres holds.
    """
    return list(
        (
            await session.execute(
                select(Citation)
                .where(Citation.claim_id == claim_id)
                .order_by(Citation.created_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _claim(session: AsyncSession, claim_id: uuid.UUID) -> WikiClaim:
    """The claim row as Postgres holds it, not as the identity map remembers it."""
    return (
        await session.execute(
            select(WikiClaim)
            .where(WikiClaim.id == claim_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _count(session: AsyncSession, model: Any, project_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(model).where(model.project_id == project_id)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# C1 — a claim survives a page rewrite
# ---------------------------------------------------------------------------


async def test_reasserting_a_claim_keeps_its_identity(
    session: AsyncSession,
    project_id: uuid.UUID,
    page_id: uuid.UUID,
    maker: Callable[[], AsyncSession],
) -> None:
    """Re-asserting a proposition updates the belief instead of forking it.

    The second assertion differs in case and whitespace, which is the shape a
    re-extraction actually takes. `claim_key_for` normalises both away, so the
    two land on the same row through the partial unique index on
    `(project_id, claim_key) WHERE superseded_by IS NULL`.

    The identity is re-read in a SECOND session so this cannot pass on the
    identity map alone — the point is that Postgres holds one row, not that
    SQLAlchemy handed back the object it already had.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    first = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(source_id, QUOTE_A1, SOURCE_A)],
        ),
    )
    await session.commit()

    second = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=f"   {CLAIM_TEXT.upper()}\n\t ",
            page_id=uuid7(),  # a different page: identity is the proposition, not the page
            evidence=[_evidence(source_id, QUOTE_A2, SOURCE_A)],
        ),
    )
    await session.commit()

    assert first.created is True
    assert second.created is False
    assert second.claim_id == first.claim_id

    async with maker() as fresh:
        live = await BeliefService(fresh).live_claims(project_id=project_id)
        assert [c.id for c in live] == [first.claim_id]
        row = live[0]
        assert row.claim_key == claim_key_for(CLAIM_TEXT)
        # An enrich does NOT rewrite the proposition. The stored text is the
        # first writer's, so a re-extraction cannot silently reword a belief
        # that other rows already cite.
        assert row.text == CLAIM_TEXT
        assert row.page_id == page_id

    # Both writes are on the ledger, and the second is an `enrich`, not an
    # `upsert` — the distinction is what tells an auditor a belief accumulated
    # rather than was recreated.
    kinds = (
        (
            await session.execute(
                select(ActionLedgerEvent.action_kind)
                .where(ActionLedgerEvent.project_id == project_id)
                .order_by(ActionLedgerEvent.timestamp)
            )
        )
        .scalars()
        .all()
    )
    assert list(kinds) == ["belief.claim.upsert", "belief.claim.enrich"]


async def test_citations_accumulate_across_reassertions(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """A second assertion ADDS evidence; it does not replace the first batch.

    This is the property the old path could not have: `commit_revision` wrote a
    new claim row per compile, so the previous compile's citations were still in
    the table but pointed at a claim nothing read any more.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    src_a, src_b = uuid7(), uuid7()

    first = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(src_a, QUOTE_A1, SOURCE_A)],
        ),
    )
    await session.commit()
    original_ids = {c.id for c in await _citations(session, first.claim_id)}
    assert len(original_ids) == 1

    await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[
                _evidence(src_a, QUOTE_A2, SOURCE_A),
                _evidence(src_b, QUOTE_B1, SOURCE_B, stance="contradicts"),
            ],
        ),
    )
    await session.commit()

    rows = await _citations(session, first.claim_id)
    assert len(rows) == 3
    # The original row is still there, same id — accumulation, not a rewrite.
    assert original_ids <= {c.id for c in rows}
    assert {c.source_id for c in rows} == {src_a, src_b}

    # And the derived counters moved with the evidence rather than staying at
    # whatever the first write computed.
    claim = await _claim(session, first.claim_id)
    assert claim.support_count == 2
    assert claim.distinct_source_count == 2


async def test_re_deriving_the_same_span_unions_rather_than_duplicates(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """The same span cited twice is one row, keyed by `locator_hash`.

    Re-running extraction over an unchanged corpus must not inflate a claim's
    evidence — support count is read as a strength signal, and a claim cited
    once by one paper would otherwise climb every night.

    The second quote is the same span written in different case. `ground`
    normalises, resolves to the same character offsets, and therefore the same
    locator, which is what makes the union work on re-extraction rather than
    only on byte-identical input.

    NOTE: the *returned* `citations_written` counts attempted writes, not new
    rows — it reports 1 on both calls. The table is what is asserted here.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()
    draft = ClaimUpsert(
        text=CLAIM_TEXT,
        page_id=page_id,
        evidence=[_evidence(source_id, QUOTE_A1, SOURCE_A)],
    )

    first = await svc.upsert_claim(
        principal=_agent(), ledger=ledger, project_id=project_id, draft=draft
    )
    await session.commit()
    (before,) = await _citations(session, first.claim_id)
    before_id, before_locator = before.id, before.locator_hash
    before_start, before_end = before.char_start, before.char_end

    second = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(source_id, QUOTE_A1.upper(), SOURCE_A, weight=2.0)],
        ),
    )
    await session.commit()

    rows = await _citations(session, second.claim_id)
    assert len(rows) == 1
    assert rows[0].id == before_id
    assert rows[0].locator_hash == before_locator
    # The stored quote is the SOURCE's text, both times — a re-derivation in
    # the model's own casing cannot overwrite the document's wording.
    assert rows[0].quote == SOURCE_A[before_start or 0 : before_end or 0]
    assert rows[0].quote == QUOTE_A1
    # The conflict path updates the mutable fields rather than skipping.
    assert rows[0].weight == 2.0
    assert first.citations_written == second.citations_written == 1


async def test_supersession_keeps_the_old_belief_walkable(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """Revision is supersession: the old row stays, linked, never deleted.

    Two links, and both matter. `superseded_by` on the old row is how the
    partial unique index lets the new proposition take the live slot;
    `ClaimEdge(kind='supersedes')` is how a reader walks the history without a
    separate revisions table.

    This is also the only place in the tree that writes a `ClaimEdge` at all —
    which is why the deployed database has zero of them.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    original = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(source_id, QUOTE_A1, SOURCE_A)],
        ),
    )
    await session.commit()

    revised = await svc.supersede(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        claim_id=original.claim_id,
        replacement=ClaimUpsert(
            text="Sedimentation in the Gulf of Corinth accelerated within decades of 8.2 ka",
            page_id=page_id,
            evidence=[_evidence(source_id, QUOTE_A3, SOURCE_A)],
        ),
    )
    await session.commit()

    assert revised.claim_id != original.claim_id

    old = await _claim(session, original.claim_id)
    assert old.superseded_by == revised.claim_id
    assert old.status == "superseded"
    # Not deleted, and its evidence is still attached — the old belief remains
    # answerable to "what did we think, and on what?"
    assert len(await _citations(session, original.claim_id)) == 1

    edge = (
        await session.execute(
            select(ClaimEdge).where(
                ClaimEdge.src_claim_id == revised.claim_id,
                ClaimEdge.dst_claim_id == original.claim_id,
            )
        )
    ).scalar_one()
    assert edge.kind == "supersedes"
    assert edge.project_id == project_id

    live = await svc.live_claims(project_id=project_id)
    assert [c.id for c in live] == [revised.claim_id]


# ---------------------------------------------------------------------------
# C3 — confidence is derived from evidence, not asserted
# ---------------------------------------------------------------------------


async def test_confidence_rises_with_supporting_evidence(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """No evidence → under_investigation; evidence accumulates the state upward.

    The thresholds themselves live in `aleph_hypotheses.confidence` and are
    unit-tested there. What is pinned here is that the value stored on the row
    tracks them, computed in the same transaction as the citation write, so the
    column can never disagree with the evidence it summarises.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    bare = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(text=CLAIM_TEXT, page_id=page_id),
    )
    await session.commit()
    assert bare.confidence == "under_investigation"
    assert (await _claim(session, bare.claim_id)).confidence == "under_investigation"

    one = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(source_id, QUOTE_A1, SOURCE_A, weight=1.0)],
        ),
    )
    await session.commit()
    assert one.confidence == "weakly_supported"

    # net 3.5 with a high-weight supporter — both conditions the state machine
    # requires before it will say well_supported.
    many = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[
                _evidence(source_id, QUOTE_A2, SOURCE_A, weight=1.5),
                _evidence(source_id, QUOTE_A3, SOURCE_A, weight=1.0),
            ],
        ),
    )
    await session.commit()
    assert many.confidence == "well_supported"

    claim = await _claim(session, many.claim_id)
    assert claim.confidence == "well_supported"
    assert claim.support_count == 3
    assert claim.last_surfaced_at is not None


async def test_contradicting_evidence_moves_a_claim_to_contested(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """Contradicting evidence pulls the state DOWN, from the same code path.

    A knowledge layer that can only gain confidence is a collection of press
    releases. `stance='contradicts'` on a citation subtracts its weight, and the
    recompute runs on the same write, so a refuting source registers the moment
    it lands rather than at some later sweep.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    src_a, src_b = uuid7(), uuid7()

    supported = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(src_a, QUOTE_A1, SOURCE_A, weight=1.0)],
        ),
    )
    await session.commit()
    assert supported.confidence == "weakly_supported"

    disputed = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[_evidence(src_b, QUOTE_B1, SOURCE_B, stance="contradicts", weight=2.0)],
        ),
    )
    await session.commit()

    assert disputed.claim_id == supported.claim_id
    assert disputed.confidence == "contested"

    claim = await _claim(session, disputed.claim_id)
    assert claim.confidence == "contested"
    # The contradicting citation is evidence, but it is not SUPPORT. A support
    # count that counted it would report a contested claim as better attested
    # than an uncontested one.
    assert claim.support_count == 1
    assert claim.distinct_source_count == 2


async def test_support_counts_are_recomputed_not_asserted(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """Hand-written counters are overwritten by a recompute, not trusted.

    The columns are poisoned directly — the way a model-written value or a
    stale migration would leave them — and then `recompute_confidence` is asked
    for the truth. Every one of the four derived fields is re-derived from the
    citation rows; none of them is read back and preserved.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    src_a, src_b = uuid7(), uuid7()

    result = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[
                _evidence(src_a, QUOTE_A1, SOURCE_A),
                _evidence(src_a, QUOTE_A2, SOURCE_A),
                _evidence(src_b, QUOTE_B1, SOURCE_B, stance="contradicts"),
            ],
        ),
    )
    await session.commit()

    claim = await _claim(session, result.claim_id)
    claim.support_count = 999
    claim.distinct_source_count = 999
    claim.confidence = "well_supported"
    await session.flush()

    recomputed = await svc.recompute_confidence(project_id=project_id, claim_id=result.claim_id)
    await session.commit()

    # net = 2 supports minus 1 contradiction = 1 → weakly_supported.
    assert recomputed == "weakly_supported"
    claim = await _claim(session, result.claim_id)
    assert claim.confidence == "weakly_supported"
    assert claim.support_count == 2
    assert claim.distinct_source_count == 2


# ---------------------------------------------------------------------------
# C6 — a human's claim is immutable to agents
# ---------------------------------------------------------------------------


async def test_an_agent_cannot_overwrite_a_user_claim(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """`origin='user'` is enforced in the write path, not requested in a prompt.

    Both doors are checked, because they are separate code: `upsert_claim`
    refuses an agent re-assertion of the same proposition, and `supersede`
    refuses an agent replacement of the row. An agent that could reach either
    would be able to edit a human's correction by rephrasing its request.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    owned = await svc.upsert_claim(
        principal=_human(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            origin="user",
            evidence_tier="stated",
            evidence=[_evidence(source_id, QUOTE_A1, SOURCE_A)],
        ),
    )
    await session.commit()

    with pytest.raises(PermissionDenied, match="cannot be overwritten"):
        await svc.upsert_claim(
            principal=_agent(),
            ledger=ledger,
            project_id=project_id,
            draft=ClaimUpsert(
                text=CLAIM_TEXT,
                page_id=page_id,
                origin="agent",
                evidence=[_evidence(source_id, QUOTE_A2, SOURCE_A)],
            ),
        )

    with pytest.raises(PermissionDenied, match="may not supersede"):
        await svc.supersede(
            principal=_agent(),
            ledger=ledger,
            project_id=project_id,
            claim_id=owned.claim_id,
            replacement=ClaimUpsert(
                text="Sedimentation was unchanged at 8.2 ka",
                page_id=page_id,
                origin="agent",
            ),
        )

    await session.rollback()

    # The refusal happens BEFORE any write, so the agent's evidence did not
    # land either — a refused overwrite that still attached the agent's
    # citations would move the claim's confidence without touching its text.
    claim = await _claim(session, owned.claim_id)
    assert claim.origin == "user"
    assert claim.text == CLAIM_TEXT
    assert claim.superseded_by is None
    assert claim.status == "active"
    assert [c.quote for c in await _citations(session, owned.claim_id)] == [QUOTE_A1]
    assert await _count(session, ClaimEdge, project_id) == 0


async def test_a_user_may_revise_their_own_claim(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """The gate is on the WRITER's origin, not a freeze on the row.

    A protection that also blocks the person it protects is an outage. A
    `user`-origin supersession of a `user`-origin claim goes through, and leaves
    the same walkable history any other supersession does.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    owned = await svc.upsert_claim(
        principal=_human(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(text=CLAIM_TEXT, page_id=page_id, origin="user"),
    )
    await session.commit()

    revised = await svc.supersede(
        principal=_human(),
        ledger=ledger,
        project_id=project_id,
        claim_id=owned.claim_id,
        replacement=ClaimUpsert(
            text="Sedimentation in the Gulf of Corinth accelerated within a century of 8.2 ka",
            page_id=page_id,
            origin="user",
            evidence_tier="stated",
            evidence=[_evidence(source_id, QUOTE_A3, SOURCE_A)],
        ),
    )
    await session.commit()

    assert revised.claim_id != owned.claim_id
    old = await _claim(session, owned.claim_id)
    assert old.superseded_by == revised.claim_id

    new = await _claim(session, revised.claim_id)
    assert new.origin == "user"
    live = await svc.live_claims(project_id=project_id)
    assert [c.id for c in live] == [revised.claim_id]


# ---------------------------------------------------------------------------
# C7 — every written citation carries a source id, and its quote is real
# ---------------------------------------------------------------------------


async def test_every_written_citation_carries_a_source_id(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """`Citation.source_id` is populated on the write path. It was not, ever.

    Retraction blast-radius joins on this column. Every production write path
    left it NULL, so the feature returned zero rows for its entire life while
    reporting success. The column is nullable in the schema, which is exactly
    why it needs a test rather than a constraint.

    `source_page_id` is deliberately NULL here — the Claim Spine does not write
    the older bridge key, and asserting that keeps the two apart.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    src_a, src_b = uuid7(), uuid7()
    chunk = uuid7()

    result = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[
                _evidence(src_a, QUOTE_A1, SOURCE_A, chunk_id=chunk),
                _evidence(src_b, QUOTE_B1, SOURCE_B, stance="contradicts"),
            ],
        ),
    )
    await session.commit()

    rows = await _citations(session, result.claim_id)
    assert len(rows) == 2
    assert all(c.source_id is not None for c in rows)
    assert {c.source_id for c in rows} == {src_a, src_b}

    by_source = {c.source_id: c for c in rows}
    anchored = by_source[src_a]
    # The whole chain: source → chunk → exact character span → verbatim text.
    assert anchored.chunk_id == chunk
    assert anchored.chunk_ids == [str(chunk)]
    assert anchored.verbatim is True
    assert anchored.char_start is not None and anchored.char_end is not None
    assert SOURCE_A[anchored.char_start : anchored.char_end] == anchored.quote
    assert anchored.source_page_id is None
    assert anchored.locator_hash is not None


async def test_a_fabricated_quote_is_refused(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """A quote not present in the source is REFUSED, not stored with a flag.

    Storing it would put an unverifiable assertion into the provenance trail,
    and the trail's only value is that everything in it is checkable. Three
    shapes are tried in one batch: a plausible fabrication, an empty quote
    (which would otherwise "match" everywhere and assert nothing), and one real
    quote to prove the batch is not simply failing wholesale.

    The refused evidence must also not reach the confidence engine — a rejected
    citation that still counted would let a model raise a claim's standing by
    inventing quotes.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()
    fabricated = "rates fell sharply after the 8.2 ka event"

    result = await svc.upsert_claim(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text=CLAIM_TEXT,
            page_id=page_id,
            evidence=[
                _evidence(source_id, fabricated, SOURCE_A),
                _evidence(source_id, "   ", SOURCE_A),
                _evidence(source_id, QUOTE_A1, SOURCE_A),
            ],
        ),
    )
    await session.commit()

    assert result.citations_written == 1
    assert result.citations_rejected == [fabricated, "   "]

    rows = await _citations(session, result.claim_id)
    assert [c.quote for c in rows] == [QUOTE_A1]
    # One supporting citation survived, so the state machine sees exactly one
    # unit of support — not three.
    assert result.confidence == "weakly_supported"
    assert (await _claim(session, result.claim_id)).support_count == 1


# ---------------------------------------------------------------------------
# C5 — reconciliation proposes, deterministically, without a model
# ---------------------------------------------------------------------------


async def test_duplicate_beliefs_are_proposed_for_merge_without_a_model(
    session: AsyncSession,
    project_id: uuid.UUID,
    page_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`propose_merges` finds duplicates with zero model spend, and only proposes.

    The "without a model" half is enforced, not asserted by inspection: every
    outbound HTTP request — sync and async — raises for the duration of the
    call. Postgres is reached over asyncpg, so the guard cannot fire
    accidentally, and any LLM client in this repo would go through httpx to the
    gateway. If someone reintroduces `CuratorService.dedup_detect`'s judge call
    here, this test fails loudly rather than quietly costing money.

    The "only proposes" half matters as much: nothing is mutated. The claim rows
    are unchanged and no `claim_edges` merge edge appears — the patch is a
    proposal for a human or an adjudicator to apply.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    texts = [
        "Sedimentation rates rose after the 8.2 ka event",
        "Sedimentation rates rose sharply after the 8.2 ka event",
        "Radiocarbon dates bracket the transition to within a few decades",
    ]
    ids: list[uuid.UUID] = []
    for body in texts:
        r = await svc.upsert_claim(
            principal=_agent(),
            ledger=ledger,
            project_id=project_id,
            draft=ClaimUpsert(text=body, page_id=page_id),
        )
        ids.append(r.claim_id)
    await session.commit()

    def _no_http(*_args: object, **_kwargs: object) -> object:
        msg = "propose_merges made an outbound HTTP request — it must call no model"
        raise AssertionError(msg)

    monkeypatch.setattr(httpx.AsyncClient, "send", _no_http)
    monkeypatch.setattr(httpx.Client, "send", _no_http)

    proposals = await svc.propose_merges(project_id=project_id, profile_hash="test-profile")
    again = await svc.propose_merges(project_id=project_id, profile_hash="test-profile")

    # Every unordered pair of the three live claims is scored.
    assert len(proposals) == 3

    def _key(candidate: Candidate) -> tuple[str, str]:
        return (str(candidate.left.id), str(candidate.right.id))

    # Deterministic: same claims in, same verdicts and same scores out. The LLM
    # curator this replaces could merge a pair on Monday and not on Tuesday.
    assert [(_key(c), c.score, c.verdict) for c, _ in proposals] == [
        (_key(c), c.score, c.verdict) for c, _ in again
    ]

    dup_pair = {ids[0], ids[1]}
    accepted = [(c, p) for c, p in proposals if {c.left.id, c.right.id} == dup_pair]
    assert len(accepted) == 1
    candidate, patch = accepted[0]
    assert candidate.verdict == "accept"
    assert patch is not None
    assert patch.operation is PatchOperation.ACCEPT_MATCH
    assert patch.status is PatchStatus.PROPOSED
    assert patch.trust is TrustTier.EARNED
    assert patch.profile_hash == "test-profile"
    # An accept asserts two claims are one thing, so it carries the terms it
    # matched on. A patch with no evidence would be an unarguable assertion.
    assert patch.evidence_refs
    assert set(patch.target) >= {"source_claim_id", "target_claim_id", "score"}

    # The unrelated claim is rejected with a NAMED reason, not a bare threshold.
    others = [(c, p) for c, p in proposals if ids[2] in {c.left.id, c.right.id}]
    assert len(others) == 2
    for c, p in others:
        assert c.verdict == "reject"
        assert c.reason is RejectReason.DIFFERENT_SUBJECT
        assert p is not None
        assert p.operation is PatchOperation.REJECT_MATCH

    # Proposed, never applied.
    await session.rollback()
    assert await _count(session, ClaimEdge, project_id) == 0
    live = await svc.live_claims(project_id=project_id)
    assert {c.id for c in live} == set(ids)
    assert all(c.superseded_by is None for c in live)


# ---------------------------------------------------------------------------
# C8 — the belief graph rebuilds from its sources, idempotently
# ---------------------------------------------------------------------------


def _fixture_extractor(page: uuid.UUID) -> Callable[[SourceText], Sequence[ClaimUpsert]]:
    """A deterministic extractor. No model, by design.

    `BeliefService.rebuild` takes the extractor as an argument precisely so the
    rebuild machinery can be tested for determinism and idempotence without one.
    The cost is honest: nothing here says a real LLM extractor produces quotes
    that ground.
    """

    def extract(source: SourceText) -> Sequence[ClaimUpsert]:
        drafts: list[ClaimUpsert] = []
        for quote in ("rose sharply", "varve record thickens", "no change in sedimentation rate"):
            if quote not in source.text:
                continue
            drafts.append(
                ClaimUpsert(
                    text=f"{source.title}: {quote}",
                    page_id=page,
                    origin="agent",
                    evidence_tier="stated",
                    evidence=[_evidence(source.source_id, quote, source.text)],
                )
            )
        return drafts

    return extract


def _sources() -> list[SourceText]:
    return [
        SourceText(source_id=uuid7(), text=SOURCE_A, title="Corinth cores"),
        SourceText(source_id=uuid7(), text=SOURCE_B, title="Reanalysis"),
    ]


async def test_the_belief_graph_rebuilds_from_its_sources(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """The graph is a function of (sources x extraction), not primary state.

    The wiki it replaces held the only copy of what the system believed, so a
    bad compile was unrecoverable and a changed extraction prompt could never be
    applied retroactively. Here the whole belief set is re-derivable, and every
    claim it produces lands anchored: verbatim quote, real source id, exact
    character span.

    NOT covered: pruning. `rebuild` upserts what the extractor emits and never
    removes a claim the current sources no longer support, so this is a
    re-derivation rather than a reconstruction. There is no pruning behaviour to
    pin.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    sources = _sources()

    result = await svc.rebuild(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        extract=_fixture_extractor(page_id),
        sources=sources,
    )
    await session.commit()

    # SOURCE_A yields two claims, SOURCE_B one.
    assert result.claims_before == 0
    assert result.claims_after == 3
    assert result.new_claims == 3
    assert result.citations_written == 3
    assert result.citations_rejected == 0

    live = await svc.live_claims(project_id=project_id)
    assert len(live) == 3
    source_ids = {s.source_id: s.text for s in sources}
    for claim in live:
        rows = await _citations(session, claim.id)
        assert len(rows) == 1
        cit = rows[0]
        assert cit.source_id in source_ids
        assert cit.verbatim is True
        assert cit.char_start is not None and cit.char_end is not None
        assert source_ids[cit.source_id][cit.char_start : cit.char_end] == cit.quote
        assert claim.confidence == "weakly_supported"


async def test_rebuilding_twice_is_idempotent(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """A second rebuild over unchanged sources changes nothing in the database.

    Both halves of identity carry this: `claim_key` keeps the claim row, and
    `locator_hash` keeps the citation row. Without either, a nightly rebuild
    would double the graph every night while every step reported success.

    NOTE: `citations_written` is a count of attempted writes, so it reports 3 on
    both runs. The row counts are what is asserted.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    sources = _sources()
    extract = _fixture_extractor(page_id)

    first = await svc.rebuild(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        extract=extract,
        sources=sources,
    )
    await session.commit()
    ids_before = {c.id for c in await svc.live_claims(project_id=project_id)}
    citations_before = await _count(session, Citation, project_id)

    second = await svc.rebuild(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        extract=extract,
        sources=sources,
    )
    await session.commit()

    assert first.new_claims == 3
    assert second.new_claims == 0
    assert second.claims_before == second.claims_after == 3

    assert {c.id for c in await svc.live_claims(project_id=project_id)} == ids_before
    assert await _count(session, Citation, project_id) == citations_before == 3
    assert await _count(session, WikiClaim, project_id) == 3
    assert await _count(session, ClaimEdge, project_id) == 0


async def test_a_rebuild_does_not_destroy_human_corrections(
    session: AsyncSession, project_id: uuid.UUID, page_id: uuid.UUID
) -> None:
    """A `user` claim is untouched by a rebuild, and counted as preserved.

    A user claim is not derived from a source, so re-deriving cannot produce it.
    A rebuild that dropped what it could not re-derive would destroy exactly the
    corrections a human bothered to make — which is the failure mode that makes
    people stop correcting anything.

    NOT asserted, deliberately: what happens when the extractor re-derives the
    SAME proposition a human owns. `upsert_claim` raises `PermissionDenied` and
    the rebuild aborts part-way, leaving the claims written so far. The
    correction survives — but "aborts the run" is a plausible bug rather than a
    guarantee, and pinning it would make a future fix look like a regression.
    """
    svc = BeliefService(session)
    ledger = LedgerWriter(session)
    source_id = uuid7()

    correction = await svc.upsert_claim(
        principal=_human(),
        ledger=ledger,
        project_id=project_id,
        draft=ClaimUpsert(
            text="The 8.2 ka signal at Corinth is tectonic, not climatic",
            page_id=page_id,
            origin="user",
            evidence_tier="stated",
            evidence=[_evidence(source_id, QUOTE_A3, SOURCE_A, weight=1.5)],
        ),
    )
    await session.commit()
    before = await _claim(session, correction.claim_id)
    kept_text, kept_confidence = before.text, before.confidence

    result = await svc.rebuild(
        principal=_agent(),
        ledger=ledger,
        project_id=project_id,
        extract=_fixture_extractor(page_id),
        sources=_sources(),
    )
    await session.commit()

    assert result.claims_before == 1
    assert result.claims_after == 4
    assert result.new_claims == 3
    assert result.user_claims_preserved == 1

    after = await _claim(session, correction.claim_id)
    assert after.superseded_by is None
    assert after.status == "active"
    assert after.origin == "user"
    assert after.text == kept_text
    assert after.confidence == kept_confidence
    assert [c.quote for c in await _citations(session, correction.claim_id)] == [QUOTE_A3]
