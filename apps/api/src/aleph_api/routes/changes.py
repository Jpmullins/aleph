"""SSE stream of compact change signals — the live-wiki consumer of the push layer.

`GET /v1/projects/{id}/changes/stream` subscribes to the ChangeBroker and, on each
wake (push or fallback), runs two scoped cursor queries and emits compact signals:

  * `committed`    — a `wiki.revision.commit` ledger event (the page was saved) →
                     drives the "updated just now" pulse + a targeted refetch.
  * `compiling`    — a page-scoped `compile_page` phase_started agent event →
                     "✦ agent is editing this page…".
  * `compile_done` — its phase_completed/failed → clears the indicator.

The stream is intentionally general (a ledger allowlist + the compile_page phase),
so Notes/Briefs can subscribe later by widening the allowlist. The serializers are
pure (ORM rows → list[dict]) and unit-tested without a DB.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from sqlalchemy import select
from starlette.responses import StreamingResponse

from aleph_api.deps import PrincipalDep
from aleph_api.middleware.project_scope import assert_stream_access
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_db.models.ledger import ActionLedgerEvent

router = APIRouter(prefix="/v1/projects", tags=["changes"])

# Poll-fallback window (push is the real path; this self-heals a dropped listener).
_FALLBACK_SEC = 5.0
_BATCH_LIMIT = 200
# On connect, replay this much recent compile activity so a page opened mid-compile
# shows presence immediately (the frontend reconstructs in-flight state from the
# compiling/compile_done pairs).
_REPLAY_WINDOW_SEC = 60.0

# Ledger action kinds the live surfaces care about.
WIKI_LEDGER_ALLOWLIST = frozenset({"wiki.revision.commit"})
# Other mutations → which workspace domain (tab data) they invalidate. The
# frontend's live-signals hook turns these `changed` signals into targeted
# react-query invalidations, so every tab refreshes the instant something
# writes instead of waiting for its poll interval.
DOMAIN_LEDGER_KINDS: dict[str, str] = {
    "note.create": "notes",
    "note.update": "notes",
    "note.section.create": "notes",
    "note.section.update": "notes",
    "hypothesis.create": "hypotheses",
    "hypothesis.evidence.add": "hypotheses",
    "hypothesis.version.create": "hypotheses",
    "artifact.create": "artifacts",
    "artifact.version.create": "artifacts",
    "source.create": "wiki",
    "source.status_change": "wiki",
    "note.promote": "briefs",
    "synthesis.proposal.create": "briefs",
    "synthesis.proposal.approve": "briefs",
    "synthesis.proposal.reject": "briefs",
    "review_finding.resolve": "briefs",
    "review_finding.dismiss": "briefs",
    "card.pin": "briefs",
    "card.unpin": "briefs",
    "approval_request.create": "briefs",
    "approval_request.approved": "briefs",
    "approval_request.rejected": "briefs",
}
LEDGER_ALLOWLIST = WIKI_LEDGER_ALLOWLIST | frozenset(DOMAIN_LEDGER_KINDS)
_PHASE_KINDS = ("phase_started", "phase_completed", "phase_failed")
_COMPILE_PHASE = "compile_page"


def ledger_rows_to_signals(rows: list[ActionLedgerEvent]) -> list[dict[str, Any]]:
    """Map allowlisted ledger rows to `committed` / `changed` signals (pure)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.action_kind in WIKI_LEDGER_ALLOWLIST:
            payload = r.payload_jsonb or {}
            out.append(
                {
                    "kind": "committed",
                    "action_kind": r.action_kind,
                    "page_id": str(r.target_id) if r.target_id else payload.get("page_id"),
                    "page_title": payload.get("page_title"),
                    "actor_kind": r.actor_kind,
                    "ts": r.timestamp.isoformat(),
                }
            )
        elif r.action_kind in DOMAIN_LEDGER_KINDS:
            out.append(
                {
                    "kind": "changed",
                    "domain": DOMAIN_LEDGER_KINDS[r.action_kind],
                    "action_kind": r.action_kind,
                    "target_id": str(r.target_id) if r.target_id else None,
                    "actor_kind": r.actor_kind,
                    "ts": r.timestamp.isoformat(),
                }
            )
    return out


def phase_rows_to_signals(rows: list[AgentEvent]) -> list[dict[str, Any]]:
    """Map page-scoped `compile_page` phase rows to compiling/compile_done (pure).

    A row needs the `compile_page` phase and at least one of page_id/page_title
    (the started event is keyed by title before the page row exists; the done
    event carries the real page_id).
    """
    out: list[dict[str, Any]] = []
    for e in rows:
        payload = e.payload_jsonb or {}
        if payload.get("phase") != _COMPILE_PHASE:
            continue
        if not payload.get("page_id") and not payload.get("page_title"):
            continue
        compiling = e.event_kind == "phase_started"
        sig: dict[str, Any] = {
            "kind": "compiling" if compiling else "compile_done",
            "page_id": payload.get("page_id"),
            "page_title": payload.get("page_title"),
            "ts": e.timestamp.isoformat(),
        }
        if compiling:
            sig["page_kind"] = payload.get("page_kind")
        out.append(sig)
    return out


@router.get("/{project_id}/changes/stream", response_model=None)
async def stream_changes(
    project_id: Annotated[UUID, Path(...)],
    request: Request,
    principal: PrincipalDep,
    since: Annotated[datetime | None, Query()] = None,
) -> StreamingResponse:
    # Membership check WITHOUT pinning a pool connection for the stream's life.
    await assert_stream_access(request, project_id, principal)
    maker = request.app.state.session_maker
    broker = request.app.state.change_broker
    now = datetime.now(tz=UTC)
    ledger_cursor: datetime = since or now
    # Phase cursor starts in the past so the first pass replays recent compile
    # activity (in-flight presence); the ledger cursor does not replay old commits.
    phase_cursor: datetime = (since or now) - timedelta(seconds=_REPLAY_WINDOW_SEC)

    async def _gen() -> AsyncIterator[bytes]:
        nonlocal ledger_cursor, phase_cursor
        async with broker.subscribe(project_id) as sub:
            while True:
                if await request.is_disconnected():
                    return

                async with maker() as session:
                    ledger_rows = list(
                        (
                            await session.execute(
                                select(ActionLedgerEvent)
                                .where(
                                    ActionLedgerEvent.project_id == project_id,
                                    ActionLedgerEvent.timestamp > ledger_cursor,
                                    ActionLedgerEvent.action_kind.in_(LEDGER_ALLOWLIST),
                                )
                                .order_by(ActionLedgerEvent.timestamp.asc())
                                .limit(_BATCH_LIMIT)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    phase_rows = list(
                        (
                            await session.execute(
                                select(AgentEvent)
                                .join(AgentRun, AgentRun.id == AgentEvent.agent_run_id)
                                .where(
                                    AgentRun.project_id == project_id,
                                    AgentEvent.timestamp > phase_cursor,
                                    AgentEvent.event_kind.in_(_PHASE_KINDS),
                                )
                                .order_by(AgentEvent.timestamp.asc())
                                .limit(_BATCH_LIMIT)
                            )
                        )
                        .scalars()
                        .all()
                    )

                signals = ledger_rows_to_signals(ledger_rows) + phase_rows_to_signals(phase_rows)
                signals.sort(key=lambda s: s["ts"])
                for sig in signals:
                    yield f"event: change\ndata: {json.dumps(sig)}\n\n".encode()

                if ledger_rows:
                    ledger_cursor = max(r.timestamp for r in ledger_rows)
                if phase_rows:
                    phase_cursor = max(e.timestamp for e in phase_rows)

                yield b": heartbeat\n\n"
                await sub.wait(timeout=_FALLBACK_SEC)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
