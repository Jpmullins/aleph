"""AgentRun + AgentEvent + AgentThread."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class AgentRun(CommonColumns, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("correlation_id", name="uq_agent_runs_correlation_id"),)

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    #: The Agent Protocol thread this run belongs to, when it is a delegation.
    #:
    #: Nullable because most runs are not delegations. Present because the
    #: protocol's `update` verb starts a NEW run on the SAME thread — so runs
    #: have to be groupable by thread, and the thread is what carries the
    #: accumulated `values` the supervisor reads a result out of.
    agent_thread_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class AgentThread(CommonColumns, Base):
    """One Agent Protocol thread: a delegated subagent's own conversation.

    Aleph hosts the Agent Protocol so `deepagents`' `AsyncSubAgentMiddleware`
    can drive delegated work against Aleph's queue — `docs/decisions.md` D17.
    A thread is the unit that verb set addresses: `start` creates one, `check`
    reads the run on it, `update` starts another run on the SAME one, and the
    result is read from this row's `values`.

    **Not `assistant_threads`.** That table's `session_id` is NOT NULL, and a
    delegated subagent run is not a user chat session — reusing it would mean
    minting a fake session per delegation to satisfy a foreign concern.
    """

    __tablename__ = "agent_threads"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    #: Which subagent this thread runs. The protocol calls it `assistant_id`;
    #: `AsyncSubAgent` calls it `graph_id`. Aleph resolves it to a subagent name.
    #:
    #: NULLABLE, and that is the protocol's shape rather than a looseness here:
    #: `threads.create()` takes no arguments, and `assistant_id` only arrives on
    #: the first `runs.create`. A thread therefore exists briefly with no graph,
    #: and a NOT NULL column would have to be filled with a lie to model it.
    graph_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The supervisor run that delegated this, when there is one.
    parent_agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    #: The thread's accumulated state. `threads.get()["values"]["messages"]` is
    #: where `_build_check_result` reads a finished task's output from, so the
    #: shape here is the contract and not an implementation detail.
    values_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
