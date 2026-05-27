"""Ledger immutability test — Postgres trigger must raise on UPDATE/DELETE."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_update_raises(asgi_app):
    from aleph_db.repos.ledger import LedgerWriter

    maker = asgi_app.state.session_maker
    async with maker() as session:
        ledger = LedgerWriter(session)
        evt = await ledger.append(
            project_id=None,
            actor_id=uuid4(),
            actor_kind="system",
            action_kind="system.test_marker",
            target_id=None,
            target_kind=None,
            payload={"k": "v"},
            trace_id=None,
        )
        await session.commit()

    async with maker() as session:
        with pytest.raises(Exception) as exc:
            await session.execute(
                text(
                    "UPDATE action_ledger_events SET payload_jsonb = '{}'::jsonb WHERE id = :id"
                ),
                {"id": str(evt.id)},
            )
            await session.commit()
        assert "append-only" in str(exc.value).lower()


async def test_delete_raises(asgi_app):
    from aleph_db.repos.ledger import LedgerWriter

    maker = asgi_app.state.session_maker
    async with maker() as session:
        ledger = LedgerWriter(session)
        await ledger.append(
            project_id=None,
            actor_id=uuid4(),
            actor_kind="system",
            action_kind="system.test_delete_marker",
            target_id=None,
            target_kind=None,
            payload={},
            trace_id=None,
        )
        await session.commit()

    async with maker() as session:
        with pytest.raises(Exception) as exc:
            await session.execute(
                text(
                    "DELETE FROM action_ledger_events WHERE action_kind = 'system.test_delete_marker'"
                )
            )
            await session.commit()
        assert "append-only" in str(exc.value).lower()
