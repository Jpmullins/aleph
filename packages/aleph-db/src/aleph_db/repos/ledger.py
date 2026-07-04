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
from collections.abc import Sequence
from dataclasses import dataclass
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

    def __init__(self, session: AsyncSession) -> None:
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

    async def _lock_or_create_head(self, *, project_id: UUID | None) -> LedgerChainHead:
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


@dataclass(frozen=True)
class Divergence:
    event_id: UUID
    expected: str
    actual: str


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    count: int
    first_divergence: Divergence | None


def verify_event_chain(
    events: Sequence[Any],
    *,
    genesis_hash: str = "0" * 64,
) -> ChainVerification:
    """Recompute the hash chain over `events` (in chain order) and report the
    first event whose stored `chain_hash` does not match the recomputation.

    Each event must expose: id, action_kind, target_id, payload_jsonb,
    timestamp, chain_hash.
    """
    prev = genesis_hash
    for e in events:
        expected = _compute_chain_hash(
            prev_hash=prev,
            action_kind=e.action_kind,
            target_id=e.target_id,
            payload=e.payload_jsonb,
            timestamp_iso=e.timestamp.isoformat(),
        )
        if expected != e.chain_hash:
            return ChainVerification(
                ok=False,
                count=len(events),
                first_divergence=Divergence(event_id=e.id, expected=expected, actual=e.chain_hash),
            )
        prev = e.chain_hash
    return ChainVerification(ok=True, count=len(events), first_divergence=None)


async def verify_project_chain(session: AsyncSession, project_id: UUID) -> ChainVerification:
    """Verify a project's hash chain by walking `prev_event_id` links from the
    chain head back to genesis, then recomputing the chain in that order.

    The chain order is defined by the `prev_event_id` links, NOT by timestamps:
    events may be inserted with out-of-order timestamps and still form a valid
    chain. A dangling / cyclic `prev_event_id` link (or an ambiguous head) is a
    broken chain and verifies False.
    """
    rows = list(
        (
            await session.execute(
                select(ActionLedgerEvent).where(ActionLedgerEvent.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return verify_event_chain([])

    by_id: dict[UUID, ActionLedgerEvent] = {e.id: e for e in rows}

    # Locate the chain head (tip): prefer the tracked `LedgerChainHead`, else the
    # unique event that no other event references as its predecessor.
    head_row = (
        await session.execute(
            select(LedgerChainHead).where(LedgerChainHead.project_id == project_id)
        )
    ).scalar_one_or_none()
    head_id = head_row.head_event_id if head_row is not None else None
    if head_id is None or head_id not in by_id:
        referenced = {e.prev_event_id for e in rows if e.prev_event_id is not None}
        tips = [e for e in rows if e.id not in referenced]
        if len(tips) != 1:
            # No unambiguous tip — forked or malformed chain.
            return ChainVerification(ok=False, count=len(rows), first_divergence=None)
        head_id = tips[0].id

    # Walk prev_event_id from head → genesis, collecting events in chain order.
    ordered_rev: list[ActionLedgerEvent] = []
    seen: set[UUID] = set()
    cur: ActionLedgerEvent | None = by_id.get(head_id)
    while cur is not None:
        if cur.id in seen:
            # Cycle in the chain — broken.
            return ChainVerification(ok=False, count=len(rows), first_divergence=None)
        seen.add(cur.id)
        ordered_rev.append(cur)
        if cur.prev_event_id is None:
            break
        nxt = by_id.get(cur.prev_event_id)
        if nxt is None:
            # Dangling prev_event_id — the linked predecessor does not exist.
            return ChainVerification(ok=False, count=len(rows), first_divergence=None)
        cur = nxt

    ordered_rev.reverse()
    return verify_event_chain(ordered_rev)
