"""LedgerWriter — single entry point for ActionLedgerEvent inserts.

Every state-mutating service method calls `LedgerWriter.append(...)`
in the same database transaction as its mutation. The writer:

1. Acquires a SELECT ... FOR UPDATE lock on the per-project chain head row
   (or the null-project row for global events).
2. Computes the new chain hash from the prior head hash + payload.
3. Inserts the event.
4. Updates the head row to point at the new event.

The Postgres triggers installed in the initial migration enforce
immutability of `action_ledger_events` as a defense in depth.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.ledger import ActionLedgerEvent, LedgerChainHead

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def _compute_chain_hash(
    *,
    prev_hash: str,
    action_kind: str,
    target_id: UUID | None,
    payload: dict[str, Any],
    timestamp_iso: str,
) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(b"|")
    h.update(action_kind.encode("utf-8"))
    h.update(b"|")
    h.update(str(target_id).encode("ascii") if target_id else b"")
    h.update(b"|")
    h.update(_canonical_json(payload).encode("utf-8"))
    h.update(b"|")
    h.update(timestamp_iso.encode("ascii"))
    return h.hexdigest()


class LedgerWriter:
    """Per-session ledger writer.

    Construct one per request/job. Reuse across multiple appends in the
    same transaction so the chain-head lock is held only for the
    transaction's lifetime.
    """

    def __init__(self, session: "AsyncSession") -> None:
        self._session = session

    async def append(
        self,
        *,
        project_id: UUID | None,
        actor_id: UUID,
        actor_kind: str,
        action_kind: str,
        target_id: UUID | None,
        target_kind: str | None,
        payload: dict[str, Any],
        trace_id: str | None,
    ) -> ActionLedgerEvent:
        head = await self._lock_or_create_head(project_id=project_id)

        timestamp = utcnow()
        new_id = uuid7()
        chain_hash = _compute_chain_hash(
            prev_hash=head.head_chain_hash,
            action_kind=action_kind,
            target_id=target_id,
            payload=payload,
            timestamp_iso=timestamp.isoformat(),
        )

        event = ActionLedgerEvent(
            id=new_id,
            project_id=project_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            action_kind=action_kind,
            target_id=target_id,
            target_kind=target_kind,
            payload_jsonb=payload,
            trace_id=trace_id,
            timestamp=timestamp,
            prev_event_id=head.head_event_id,
            chain_hash=chain_hash,
        )
        self._session.add(event)

        head.head_event_id = new_id
        head.head_chain_hash = chain_hash
        # `updated_at` is bumped by the onupdate column default

        await self._session.flush()
        return event

    async def _lock_or_create_head(
        self, *, project_id: UUID | None
    ) -> LedgerChainHead:
        if project_id is None:
            stmt = (
                select(LedgerChainHead)
                .where(LedgerChainHead.project_id.is_(None))
                .with_for_update()
            )
        else:
            stmt = (
                select(LedgerChainHead)
                .where(LedgerChainHead.project_id == project_id)
                .with_for_update()
            )
        head = (await self._session.execute(stmt)).scalar_one_or_none()
        if head is not None:
            return head

        head = LedgerChainHead(id=uuid7(), project_id=project_id)
        self._session.add(head)
        await self._session.flush()
        return head
