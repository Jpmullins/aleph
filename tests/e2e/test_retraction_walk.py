"""Retraction propagates through the belief graph — with a declined branch.

The previous implementation joined through `Citation.source_page_id`, a column
no production write path ever populated, so retraction blast-radius returned
zero rows for the lifetime of the feature. Retracting a paper flagged nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SOURCE_TEXT = "Sedimentation rates rise sharply after 8.2 kiloyears before present."
OTHER_TEXT = "An independent survey confirms the same acceleration at 8.2 ka."


def principal_for(project_id):
    from aleph_security.principal import Principal

    p = Principal(user_id=uuid4(), subject="t", email="t@aleph.local", actor_kind="user")
    p.cache_role(project_id, "editor")
    return p


async def _claim(maker, project_id, principal, text, evidence):
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_wiki.belief_service import BeliefService, ClaimUpsert

    async with maker() as session:
        svc = BeliefService(session)
        result = await svc.upsert_claim(
            principal=principal,
            ledger=LedgerWriter(session),
            project_id=project_id,
            draft=ClaimUpsert(text=text, page_id=uuid7(), evidence=list(evidence)),
        )
        await session.commit()
    return result.claim_id


def ev(source_id, quote, text=SOURCE_TEXT):
    from aleph_wiki.belief_service import EvidenceDraft

    return EvidenceDraft(source_id=source_id, quote=quote, source_text=text)


async def _derive(maker, project_id, principal, child, parent):
    from aleph_wiki.models import ClaimEdge

    async with maker() as session:
        session.add(
            ClaimEdge(
                id=uuid7(),
                project_id=project_id,
                src_claim_id=child,
                dst_claim_id=parent,
                kind="derived_from",
                created_by=principal.user_id,
                access_scope="project",
            )
        )
        await session.commit()


async def test_a_claim_resting_only_on_the_retracted_source_is_unsupported(asgi_app) -> None:
    """THE thing the system has never done: retraction flags what depended on it."""
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    project_id, bad_source = uuid7(), uuid7()
    principal = principal_for(project_id)
    claim = await _claim(
        maker, project_id, principal, "Rates rose after 8.2 ka.", [ev(bad_source, "rise sharply")]
    )

    async with maker() as session:
        impact = await retraction_impact(session, bad_source)

    assert claim in impact.directly_cited
    assert claim in impact.unsupported, "a claim with no surviving support was not flagged"
    assert claim not in impact.weakened


async def test_a_claim_with_independent_support_is_weakened_not_unsupported(asgi_app) -> None:
    """The declined branch. Flagging both identically trains a reader to ignore the flag."""
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    project_id, bad_source, good_source = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)
    claim = await _claim(
        maker,
        project_id,
        principal,
        "Rates rose after 8.2 ka.",
        [ev(bad_source, "rise sharply"), ev(good_source, "same acceleration", OTHER_TEXT)],
    )

    async with maker() as session:
        impact = await retraction_impact(session, bad_source)

    assert claim in impact.directly_cited
    assert claim in impact.weakened, "a claim with surviving evidence should stay believed"
    assert claim not in impact.unsupported


async def test_retraction_propagates_to_derived_claims(asgi_app) -> None:
    """A conclusion built on a claim built on a retracted paper is also affected."""
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    project_id, bad_source = uuid7(), uuid7()
    principal = principal_for(project_id)

    base = await _claim(
        maker, project_id, principal, "Rates rose after 8.2 ka.", [ev(bad_source, "rise sharply")]
    )
    middle = await _claim(maker, project_id, principal, "The shelf aggraded rapidly.", [])
    top = await _claim(maker, project_id, principal, "Accommodation outpaced supply.", [])
    await _derive(maker, project_id, principal, middle, base)
    await _derive(maker, project_id, principal, top, middle)

    async with maker() as session:
        impact = await retraction_impact(session, bad_source)

    assert base in impact.directly_cited
    assert {middle, top} <= impact.derived, "the derivation chain was not walked"
    assert {base, middle, top} <= impact.unsupported


async def test_a_derivation_cycle_terminates(asgi_app) -> None:
    """A cycle must not turn one retraction into a full-table walk."""
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    project_id, bad_source = uuid7(), uuid7()
    principal = principal_for(project_id)

    a = await _claim(maker, project_id, principal, "Claim A.", [ev(bad_source, "rise sharply")])
    b = await _claim(maker, project_id, principal, "Claim B.", [])
    await _derive(maker, project_id, principal, b, a)
    await _derive(maker, project_id, principal, a, b)  # cycle

    async with maker() as session:
        impact = await retraction_impact(session, bad_source)

    assert {a, b} <= impact.all_touched


async def test_unrelated_claims_are_untouched(asgi_app) -> None:
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    project_id, bad_source, other_source = uuid7(), uuid7(), uuid7()
    principal = principal_for(project_id)

    await _claim(
        maker, project_id, principal, "Rates rose after 8.2 ka.", [ev(bad_source, "rise sharply")]
    )
    unrelated = await _claim(
        maker,
        project_id,
        principal,
        "The survey used a vibracorer.",
        [ev(other_source, "same acceleration", OTHER_TEXT)],
    )

    async with maker() as session:
        impact = await retraction_impact(session, bad_source)

    assert unrelated not in impact.all_touched


async def test_retracting_a_source_nothing_cites_touches_nothing(asgi_app) -> None:
    from aleph_reviewer.retraction import retraction_impact

    maker = asgi_app.state.session_maker
    async with maker() as session:
        impact = await retraction_impact(session, uuid7())
    assert impact.all_touched == set()
