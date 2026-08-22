"""AgentRun reconciliation — a run that stopped is a run that reports.

An ``AgentRun`` row is set to ``running`` by the process doing the work and
moved to a terminal status by that same process. Nothing else ever touches it.
So when the process dies — a worker restart, an OOM kill, an exception on a path
with no ``finally`` — the row stays ``running`` forever, and the UI shows work in
progress that no longer exists.

That is not hypothetical: the deployed stack carried 45 ``chunk_embed`` runs
stuck in ``running``, every one of them a failed index nobody was told about.
Forty-five identical silent failures is what an unreconciled status column looks
like.

This module is the reconciliation: on boot, any run still ``running`` past a
deadline is moved to ``failed`` with a stated reason. It converts a class of
silent hangs into visible state, not only the one that produced it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.repos.background_tasks import heartbeat_at
from aleph_db.repos.ledger import LedgerWriter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

#: How long a run may sit in ``running`` before it is presumed dead. Deliberately
#: generous: a deep-research run legitimately takes many minutes, and reaping a
#: live run is a worse failure than reporting a dead one late.
DEFAULT_STALE_AFTER = timedelta(hours=1)

#: Reconciliation is nobody's user action, so it is attributed to the nil
#: actor rather than to whoever happened to start the run — the same
#: convention `aleph_wiki.curator_service` already uses for system writes.
SYSTEM_ACTOR = UUID(int=0)

REAP_REASON = (
    "reaped by startup reconciliation: the process that owned this run exited "
    "without recording a terminal status"
)


async def stale_running_runs(
    session: AsyncSession, *, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> list[AgentRun]:
    """Runs still ``running`` whose ``started_at`` is older than the deadline.

    A run with no ``started_at`` is left alone: it is ``pending`` work that was
    marked running without a timestamp, and guessing its age would reap runs
    that never began rather than runs that never ended.

    **A run that is demonstrably alive is left alone too.** ``started_at`` alone
    answers "how long has this been going", which is the wrong question for a
    background task: a corpus reindex or a review sweep can legitimately run for
    hours, and reaping it mid-flight would mark a working job ``failed``, tell
    the analyst it died, and leave the real process writing to a row that now
    says it is over. So a run that has written a heartbeat inside the same
    window is excluded. Runs that never heartbeat — every worker job predating
    ``background_tasks`` — are unaffected, which is why the filter is applied
    after the query and not folded into it: the SQL keeps stating the original
    rule, and this states the exemption.
    """
    cutoff = utcnow() - stale_after
    rows = await session.execute(
        select(AgentRun).where(
            AgentRun.status == "running",
            AgentRun.started_at.is_not(None),
            AgentRun.started_at < cutoff,
        )
    )
    candidates = list(rows.scalars().all())
    return [r for r in candidates if not _alive_since(r, cutoff)]


def _alive_since(run: AgentRun, cutoff: datetime) -> bool:
    """Has this run proved it is alive more recently than the deadline?"""
    beat = heartbeat_at(run)
    return beat is not None and beat >= cutoff


async def reap_stale_runs(
    session: AsyncSession,
    *,
    actor_id: UUID = SYSTEM_ACTOR,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    reason: str = REAP_REASON,
) -> int:
    """Fail every stale ``running`` run, ledgering each. Returns the count.

    The ledger write is in the same transaction as the status change, per the
    standing rule — a state mutation nobody can audit is how the original
    silence happened.
    """
    stale = await stale_running_runs(session, stale_after=stale_after)
    if not stale:
        return 0
    ledger = LedgerWriter(session)
    now = utcnow()
    for run in stale:
        run.status = "failed"
        run.completed_at = now
        run.error_text = reason[:4096]
        await ledger.append(
            project_id=run.project_id,
            actor_id=actor_id,
            actor_kind="system",
            action_kind="agent_run.reaped",
            target_id=run.id,
            target_kind="agent_run",
            payload={
                "agent_kind": run.agent_kind,
                "correlation_id": run.correlation_id,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "reason": reason,
            },
            trace_id=None,
        )
    _log.warning("agent_runs.reaped", count=len(stale))
    return len(stale)
