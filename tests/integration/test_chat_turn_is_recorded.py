"""One chat turn, one recorded run, with the tool timeline attached.

Nothing about a conversation with the assistant was written down. No record that
a turn happened, which tools it called, how long they took, which subagent did
what, or how it ended — while `agent_runs`, `agent_events` and the
`/agent-events` SSE route all already existed and were used only by the worker
jobs. `grep -rn 'AgentRun(' apps packages` found seventeen producers and not one
on the chat path.

The trap these tests exist to catch is narrow and it has bitten this codebase
once already: the run id travels in `config["configurable"]`, NOT in
`metadata`. `model_calls.agent_run_id` was NULL for the whole life of that
column because its reader looked in `metadata` while nothing ever put it there.
`test_the_run_id_travels_in_configurable_not_metadata` is the pin for that
specific mistake.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.agent_middleware import AlephAgentMiddleware
from aleph_api.chat_runs import (
    RUN_ID_KEY,
    SUBAGENT_MARKER,
    SUBAGENT_MARKER_KEY,
    SUBAGENT_NAME_KEY,
    TOOL_FAILED,
    TOOL_FINISHED,
    TOOL_STARTED,
    ChatRunRecorder,
    run_id_from_config,
    subagent_from_config,
)
from aleph_core.errors import PermissionDenied
from aleph_db.models.agent import AgentEvent, AgentRun
from aleph_db.models.ledger import ActionLedgerEvent

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _recorder(maker: Callable[[], AsyncSession], project_id: uuid.UUID) -> ChatRunRecorder:
    return ChatRunRecorder(
        session_maker=maker,
        project_resolver=lambda _thread: project_id,
        actor_id=ACTOR,
    )


class _Runtime:
    """Stands in for LangGraph's ToolRuntime — the route puts the run id here."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config


def _request(name: str, run_id: uuid.UUID, *, subagent: str | None = None) -> Any:
    """A tool request shaped the way deepagents really shapes one.

    `subagent` goes into `metadata["lc_agent_name"]` and sets the
    `configurable` marker, because that is what a delegated run actually
    carries — `create_sub_agent` passes `name=` to `create_agent`, langchain
    stamps it into metadata, and deepagents adds only
    `ls_agent_type: "subagent"` to configurable.

    It used to write `configurable["subagent"] = subagent`, a key NOTHING in
    production sets. So the test supplied the value it then asserted, and
    `test_subagent_attribution` passed for years over a field that was
    "orchestrator" on all 302 real runs. A fixture that injects the answer
    cannot detect its absence.
    """
    from langchain.agents.middleware.types import ToolCallRequest

    configurable: dict[str, Any] = {RUN_ID_KEY: str(run_id)}
    config: dict[str, Any] = {"configurable": configurable}
    if subagent is not None:
        configurable[SUBAGENT_MARKER_KEY] = SUBAGENT_MARKER
        config["metadata"] = {SUBAGENT_NAME_KEY: subagent}
    return ToolCallRequest(
        tool_call={"name": name, "args": {"query": "rag"}, "id": f"c-{name}", "type": "tool_call"},
        tool=None,
        state={},
        runtime=_Runtime(config),
    )


async def _events(maker: Callable[[], AsyncSession], run_id: uuid.UUID) -> list[AgentEvent]:
    async with maker() as s:
        rows = await s.execute(
            select(AgentEvent)
            .where(AgentEvent.agent_run_id == run_id)
            .order_by(AgentEvent.timestamp)
        )
        return list(rows.scalars().all())


async def test_one_run_per_turn(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    recorder = _recorder(maker, committed_project)
    run = await recorder.begin("proj:x:thread-1")
    assert run is not None
    await recorder.finish(run, status="completed")

    async with maker() as s:
        rows = list(
            (
                await s.execute(
                    select(AgentRun).where(
                        AgentRun.project_id == committed_project,
                        AgentRun.agent_kind == "assistant",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "completed"
    assert rows[0].started_at is not None
    assert rows[0].completed_at is not None


async def test_the_turn_is_ledgered_in_the_same_transaction(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    run = await _recorder(maker, committed_project).begin("proj:x:thread-1")
    assert run is not None
    async with maker() as s:
        events = list(
            (
                await s.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == "assistant.turn",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].target_id == run.run_id


async def test_a_turn_with_no_resolvable_project_is_not_recorded(
    maker: Callable[[], AsyncSession],
) -> None:
    """A run row naming the wrong project is worse than no row: it is evidence,
    and it is false."""
    recorder = ChatRunRecorder(
        session_maker=maker, project_resolver=lambda _t: None, actor_id=ACTOR
    )
    assert await recorder.begin("not-project-prefixed") is None


async def test_tool_events_recorded(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    run = await _recorder(maker, committed_project).begin("proj:x:thread-1")
    assert run is not None
    middleware = AlephAgentMiddleware(session_maker=maker)

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(content="ok", tool_call_id="c-search_wiki", name="search_wiki")

    await middleware.awrap_tool_call(_request("search_wiki", run.run_id), handler)

    events = await _events(maker, run.run_id)
    kinds = [e.event_kind for e in events]
    assert TOOL_STARTED in kinds
    assert TOOL_FINISHED in kinds

    started = next(e for e in events if e.event_kind == TOOL_STARTED)
    finished = next(e for e in events if e.event_kind == TOOL_FINISHED)
    assert started.payload_jsonb["tool"] == "search_wiki"
    assert started.payload_jsonb["tool_call_id"] == finished.payload_jsonb["tool_call_id"]
    assert isinstance(finished.payload_jsonb["duration_ms"], int)
    assert finished.payload_jsonb["duration_ms"] >= 0
    # The arguments, so a timeline shows what was asked rather than only that
    # something was.
    assert started.payload_jsonb["args"]["query"] == "rag"


async def test_tool_failure_recorded(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A failing tool is recorded as failed AND the run still completes — the
    two halves of WS-E1b and WS-C3a have to hold together or the timeline shows
    a dead run for a turn the user saw finish."""
    recorder = _recorder(maker, committed_project)
    run = await recorder.begin("proj:x:thread-1")
    assert run is not None
    middleware = AlephAgentMiddleware(session_maker=maker)

    async def handler(_req: Any) -> Any:
        raise PermissionDenied("no access")

    result = await middleware.awrap_tool_call(_request("search_wiki", run.run_id), handler)
    assert result.status == "error"
    await recorder.finish(run, status="completed")

    events = await _events(maker, run.run_id)
    failed = [e for e in events if e.event_kind == TOOL_FAILED]
    assert len(failed) == 1
    assert failed[0].payload_jsonb["error_class"] == "PermissionDenied"

    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == run.run_id))).scalar_one()
    assert row.status == "completed"


async def test_subagent_attribution(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The Inspector must be able to say WHO did a thing, not only that it happened."""
    run = await _recorder(maker, committed_project).begin("proj:x:thread-1")
    assert run is not None
    middleware = AlephAgentMiddleware(session_maker=maker)

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(content="ok", tool_call_id="x", name="t")

    await middleware.awrap_tool_call(_request("search_wiki", run.run_id), handler)
    await middleware.awrap_tool_call(
        _request("deep_read", run.run_id, subagent="retriever"), handler
    )

    events = await _events(maker, run.run_id)
    subagents = {e.payload_jsonb.get("subagent") for e in events}
    assert len(subagents) >= 2, f"everything was attributed to {subagents}"
    assert "retriever" in subagents
    assert "orchestrator" in subagents


def test_the_run_id_travels_in_configurable_not_metadata() -> None:
    """The specific mistake that made `model_calls.agent_run_id` NULL forever.

    `metadata` is not the channel deepagents forwards to subagents, so a reader
    looking there gets None for every subagent call as well as for the
    orchestrator — and None on every row looks exactly like a feature nobody
    uses.
    """
    run_id = uuid.uuid4()
    assert run_id_from_config({"configurable": {RUN_ID_KEY: str(run_id)}}) == run_id
    assert run_id_from_config({"metadata": {RUN_ID_KEY: str(run_id)}}) is None
    assert run_id_from_config({"configurable": {RUN_ID_KEY: "not-a-uuid"}}) is None
    assert run_id_from_config(None) is None


def test_an_unattributed_call_is_the_orchestrator_not_empty() -> None:
    """A distinct-count assertion over an empty string counts nulls, not agents."""
    assert subagent_from_config({}) == "orchestrator"
    assert subagent_from_config({"tags": ["subagent:reviewer"]}) == "reviewer"
    assert subagent_from_config({"configurable": {"subagent": "analyst"}}) == "analyst"


async def test_the_existing_read_path_serves_the_new_rows(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`GET /v1/projects/{id}/agent-events?agent_run_id=` needs no change.

    Asserted at the query the route runs rather than over HTTP: the route's own
    filter is the contract, and this proves the rows satisfy it.
    """
    run = await _recorder(maker, committed_project).begin("proj:x:thread-1")
    assert run is not None
    middleware = AlephAgentMiddleware(session_maker=maker)

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(content="ok", tool_call_id="x", name="t")

    await middleware.awrap_tool_call(_request("search_wiki", run.run_id), handler)

    async with maker() as s:
        rows = list(
            (
                await s.execute(
                    select(AgentEvent)
                    .where(AgentEvent.agent_run_id == run.run_id)
                    .order_by(AgentEvent.timestamp.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    assert rows, "the read path returns nothing for a chat run"


async def test_a_turn_with_no_recorder_records_nothing_and_does_not_raise(
    maker: Callable[[], AsyncSession],
) -> None:
    """The middleware is used in unit tests with no database, so a missing
    recorder must be inert rather than an error."""
    middleware = AlephAgentMiddleware()

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(content="ok", tool_call_id="x", name="t")

    result = await middleware.awrap_tool_call(_request("search_wiki", uuid.uuid4()), handler)
    assert str(result.content) == "ok"
