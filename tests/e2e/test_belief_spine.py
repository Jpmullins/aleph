"""The Claim Spine's three guarantees.

C1 a claim survives a page rewrite
C3 confidence is derived from evidence, never asserted by a model
C6 a human's claim is immutable to agents

Each of these was false before. Claims were owned by a page revision and
re-inserted on every commit, confidence was a string a model chose, and nothing
distinguished a human correction from an agent's guess.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from aleph_core.errors import PermissionDenied
from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SOURCE_TEXT = (
    "Sedimentation rates rise sharply after 8.2 kiloyears before present, "
    "coincident with the onset of the modern circulation regime. "
    "Two dates were rejected as reworked on stratigraphic grounds."
)
CONTRARY_TEXT = (
    "We find no acceleration in sedimentation across the 8.2 kiloyear interval; "
    "the apparent rise is an artefact of the age model."
)


def principal_for(project_id):
    from aleph_security.principal import Principal

    principal = Principal(user_id=uuid4(), subject="t", email="t@aleph.local", actor_kind="user")
    principal.cache_role(project_id, "editor")
    return principal


async def _service(maker):
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_wiki.belief_service import BeliefService

    session = maker()
    return session, BeliefService(session), LedgerWriter(session)


def draft(text: str, page_id, *, origin="agent", evidence=()):
    from aleph_wiki.belief_service import ClaimUpsert

    return ClaimUpsert(text=text, page_id=page_id, origin=origin, evidence=list(evidence))


def evidence(source_id, quote, *, stance="supports", weight=1.0, text=SOURCE_TEXT):
    from aleph_wiki.belief_service import EvidenceDraft

    return EvidenceDraft(
        source_id=source_id, quote=quote, source_text=text, stance=stance, weight=weight
    )


# -- C1: a claim survives a page rewrite -------------------------------------


async def test_reasserting_a_claim_keeps_its_identity(asgi_app) -> None:
    """THE property. Re-extraction updates the belief; it does not fork it."""
    from aleph_wiki.models import WikiClaim

    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "Sedimentation rates rose sharply after 8.2 ka BP."

    session, svc, ledger = await _service(maker)
    async with session:
        first = await svc.upsert_claim(
            principal=principal, ledger=ledger, project_id=project_id, draft=draft(text, page_id)
        )
        await session.commit()

    # A later compile of a DIFFERENT page re-states the same proposition,
    # spelled with different whitespace and case.
    session, svc, ledger = await _service(maker)
    async with session:
        second = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft("  sedimentation RATES rose sharply after 8.2 ka BP.  ", uuid7()),
        )
        await session.commit()

    assert second.claim_id == first.claim_id, "the same proposition forked into two beliefs"
    assert second.created is False

    async with maker() as session:
        live = (
            (
                await session.execute(
                    select(WikiClaim).where(
                        WikiClaim.project_id == project_id, WikiClaim.superseded_by.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(live) == 1


async def test_citations_accumulate_across_reassertions(asgi_app) -> None:
    """A claim's evidence must survive the next compile, or nothing accumulates."""
    from aleph_wiki.models import Citation

    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "Two dates were rejected as reworked."
    src_a, src_b = uuid7(), uuid7()

    session, svc, ledger = await _service(maker)
    async with session:
        r1 = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, evidence=[evidence(src_a, "rejected as reworked")]),
        )
        await session.commit()

    session, svc, ledger = await _service(maker)
    async with session:
        await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, evidence=[evidence(src_b, "stratigraphic grounds")]),
        )
        await session.commit()

    async with maker() as session:
        cites = (
            (await session.execute(select(Citation).where(Citation.claim_id == r1.claim_id)))
            .scalars()
            .all()
        )
    assert len(cites) == 2, "the second compile replaced the first compile's evidence"
    assert {c.source_id for c in cites} == {src_a, src_b}


async def test_re_deriving_the_same_span_unions_rather_than_duplicates(asgi_app) -> None:
    from aleph_wiki.models import Citation

    maker = asgi_app.state.session_maker
    project_id, page_id, src = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "The onset of the modern circulation regime is dated."

    for _ in range(3):
        session, svc, ledger = await _service(maker)
        async with session:
            result = await svc.upsert_claim(
                principal=principal,
                ledger=ledger,
                project_id=project_id,
                draft=draft(text, page_id, evidence=[evidence(src, "modern circulation regime")]),
            )
            await session.commit()

    async with maker() as session:
        cites = (
            (await session.execute(select(Citation).where(Citation.claim_id == result.claim_id)))
            .scalars()
            .all()
        )
    assert len(cites) == 1, f"the same span produced {len(cites)} citations"


# -- C2/C7: evidence is anchored and verbatim --------------------------------


async def test_a_fabricated_quote_is_refused(asgi_app) -> None:
    """A quote not present in the source must not enter the provenance trail."""
    maker = asgi_app.state.session_maker
    project_id, page_id, src = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)

    session, svc, ledger = await _service(maker)
    async with session:
        result = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(
                "A confident-sounding claim.",
                page_id,
                evidence=[evidence(src, "a sentence the source never contained")],
            ),
        )
        await session.commit()

    assert result.citations_written == 0
    assert result.citations_rejected, "a fabricated quote was accepted"


async def test_every_written_citation_carries_a_source_id(asgi_app) -> None:
    """C7. This column was None on every production write path."""
    from aleph_wiki.models import Citation

    maker = asgi_app.state.session_maker
    project_id, page_id, src = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)

    session, svc, ledger = await _service(maker)
    async with session:
        result = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft("Dates were rejected.", page_id, evidence=[evidence(src, "reworked")]),
        )
        await session.commit()

    async with maker() as session:
        cites = (
            (await session.execute(select(Citation).where(Citation.claim_id == result.claim_id)))
            .scalars()
            .all()
        )
    assert cites
    assert all(c.source_id is not None for c in cites)
    assert all(c.verbatim for c in cites)
    assert all(c.char_start is not None and c.char_end is not None for c in cites)


# -- C3: confidence is derived ------------------------------------------------


async def test_confidence_rises_with_supporting_evidence(asgi_app) -> None:
    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "Sedimentation accelerated after 8.2 ka."

    session, svc, ledger = await _service(maker)
    async with session:
        bare = await svc.upsert_claim(
            principal=principal, ledger=ledger, project_id=project_id, draft=draft(text, page_id)
        )
        await session.commit()
    assert bare.confidence == "under_investigation", "a claim with no evidence looked supported"

    session, svc, ledger = await _service(maker)
    async with session:
        supported = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(
                text,
                page_id,
                evidence=[
                    evidence(uuid7(), "rise sharply", weight=2.0),
                    evidence(uuid7(), "modern circulation regime", weight=2.0),
                ],
            ),
        )
        await session.commit()
    assert supported.confidence == "well_supported", supported.confidence


async def test_contradicting_evidence_moves_a_claim_to_contested(asgi_app) -> None:
    """THE web-of-belief property: adding evidence changes what is believed."""
    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "Sedimentation accelerated after 8.2 ka."

    session, svc, ledger = await _service(maker)
    async with session:
        first = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, evidence=[evidence(uuid7(), "rise sharply")]),
        )
        await session.commit()
    assert first.confidence == "weakly_supported"

    session, svc, ledger = await _service(maker)
    async with session:
        after = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(
                text,
                page_id,
                evidence=[
                    evidence(
                        uuid7(),
                        "no acceleration in sedimentation",
                        stance="contradicts",
                        weight=2.0,
                        text=CONTRARY_TEXT,
                    )
                ],
            ),
        )
        await session.commit()

    assert after.claim_id == first.claim_id
    assert after.confidence == "contested", after.confidence


async def test_support_counts_are_recomputed_not_asserted(asgi_app) -> None:
    from aleph_wiki.models import WikiClaim

    maker = asgi_app.state.session_maker
    project_id, page_id, src = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)

    session, svc, ledger = await _service(maker)
    async with session:
        result = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(
                "Two dates were rejected.",
                page_id,
                evidence=[
                    evidence(src, "rejected as reworked"),
                    evidence(src, "stratigraphic grounds"),
                ],
            ),
        )
        await session.commit()

    async with maker() as session:
        claim = (
            await session.execute(select(WikiClaim).where(WikiClaim.id == result.claim_id))
        ).scalar_one()
    assert claim.support_count == 2
    assert claim.distinct_source_count == 1, "two spans from one source counted as two sources"


# -- C6: a human's claim is immutable to agents ------------------------------


async def test_an_agent_cannot_overwrite_a_user_claim(asgi_app) -> None:
    """Structural, not a prompt instruction."""
    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "The 8.2 ka event is not resolvable at this site."

    session, svc, ledger = await _service(maker)
    async with session:
        await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, origin="user"),
        )
        await session.commit()

    session, svc, ledger = await _service(maker)
    async with session:
        # Not combined into one `async with`: pytest.raises is a sync context
        # manager, and ruff's SIM117 autofix will merge them if given the chance.
        with pytest.raises(PermissionDenied, match="written by a user"):
            await svc.upsert_claim(
                principal=principal,
                ledger=ledger,
                project_id=project_id,
                draft=draft(text, page_id, origin="agent"),
            )


async def test_a_user_may_revise_their_own_claim(asgi_app) -> None:
    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)
    text = "The 8.2 ka event is not resolvable at this site."

    session, svc, ledger = await _service(maker)
    async with session:
        first = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, origin="user"),
        )
        await session.commit()

    session, svc, ledger = await _service(maker)
    async with session:
        again = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft(text, page_id, origin="user"),
        )
        await session.commit()
    assert again.claim_id == first.claim_id


async def test_supersession_keeps_the_old_belief_walkable(asgi_app) -> None:
    from aleph_wiki.models import ClaimEdge, WikiClaim

    maker = asgi_app.state.session_maker
    project_id, page_id = uuid7(), uuid7()
    principal = principal_for(project_id)

    session, svc, ledger = await _service(maker)
    async with session:
        old = await svc.upsert_claim(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            draft=draft("Rates rose after 8.2 ka.", page_id),
        )
        await session.commit()

    session, svc, ledger = await _service(maker)
    async with session:
        new = await svc.supersede(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            claim_id=old.claim_id,
            replacement=draft("Rates rose after 8.4 ka, not 8.2.", page_id),
        )
        await session.commit()

    async with maker() as session:
        previous = (
            await session.execute(select(WikiClaim).where(WikiClaim.id == old.claim_id))
        ).scalar_one()
        edges = (
            (await session.execute(select(ClaimEdge).where(ClaimEdge.dst_claim_id == old.claim_id)))
            .scalars()
            .all()
        )

    assert previous.superseded_by == new.claim_id, "history is not walkable"
    assert previous.status == "superseded"
    assert [e.kind for e in edges] == ["supersedes"]
