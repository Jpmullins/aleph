"""The action ledger is append-only and hash-chained — proven against Postgres.

Both properties live in the database, not in Python:

  * append-only is a trigger that raises on UPDATE and DELETE;
  * the chain is only meaningful if a tampered row is actually detectable.

A mocked session would pass every assertion below while the real table happily
accepted an UPDATE. That is why these are integration tests and why they are
worth the Postgres dependency — `CLAUDE.md` lists the hash-chained ledger as a
design commitment held by review, and this is the part that can be mechanical.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.ids import uuid7
from aleph_db.repos.ledger import LedgerWriter, verify_project_chain

pytestmark = pytest.mark.integration


async def _append(session: AsyncSession, project_id: UUID, n: int) -> list[Any]:
    writer = LedgerWriter(session)
    actor = uuid7()
    events = []
    for i in range(n):
        events.append(
            await writer.append(
                project_id=project_id,
                actor_id=actor,
                actor_kind="user",
                action_kind="test.append",
                target_id=uuid7(),
                target_kind="test",
                payload={"i": i},
                trace_id=None,
            )
        )
    await session.commit()
    return events


async def test_chain_verifies_over_real_rows(session: AsyncSession) -> None:
    project_id = uuid7()
    await _append(session, project_id, 5)

    result = await verify_project_chain(session, project_id)
    assert result.ok, f"fresh chain failed verification at {result.first_divergence}"
    assert result.count == 5


async def test_each_event_links_to_its_predecessor(session: AsyncSession) -> None:
    """A chain that does not actually link is a list with a hash column."""
    project_id = uuid7()
    events = await _append(session, project_id, 4)

    assert events[0].prev_event_id is None, "the first event should start the chain"
    for earlier, later in pairwise(events):
        assert later.prev_event_id == earlier.id
    assert len({e.chain_hash for e in events}) == len(events), "chain hashes repeat"


async def test_update_is_refused_by_the_database(session: AsyncSession) -> None:
    """Append-only must be enforced by Postgres, not by discipline in Python."""
    project_id = uuid7()
    events = await _append(session, project_id, 1)

    with pytest.raises(Exception, match="append-only"):
        await session.execute(
            text("UPDATE action_ledger_events SET action_kind = 'tampered' WHERE id = :i"),
            {"i": events[0].id},
        )
    await session.rollback()


async def test_delete_is_refused_by_the_database(session: AsyncSession) -> None:
    project_id = uuid7()
    events = await _append(session, project_id, 1)

    with pytest.raises(Exception, match="append-only"):
        await session.execute(
            text("DELETE FROM action_ledger_events WHERE id = :i"), {"i": events[0].id}
        )
    await session.rollback()


async def test_tampering_is_detectable(session: AsyncSession) -> None:
    """If a forged row verified, the chain would be decoration.

    The trigger blocks UPDATE, so this drops it for the length of one
    transaction to simulate an attacker with direct database access — which is
    the only threat model under which the hash chain earns its keep — then
    confirms `verify_project_chain` names the tampered event.
    """
    project_id = uuid7()
    events = await _append(session, project_id, 3)
    victim = events[1]

    await session.execute(text("ALTER TABLE action_ledger_events DISABLE TRIGGER USER"))
    await session.execute(
        text("UPDATE action_ledger_events SET chain_hash = :h WHERE id = :i"),
        {"h": "f" * 64, "i": victim.id},
    )
    await session.execute(text("ALTER TABLE action_ledger_events ENABLE TRIGGER USER"))
    await session.commit()

    result = await verify_project_chain(session, project_id)
    assert not result.ok, "a forged chain_hash verified as intact"
    assert result.first_divergence is not None
    assert result.first_divergence.event_id == victim.id
