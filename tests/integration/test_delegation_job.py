"""Every exit path reaches a terminal status, and the output lands where it is read.

A delegated run left at `running` is what the stale-run reaper exists to clean
up, and needing the reaper is a bug rather than a design. So the tests here are
mostly about the paths that go wrong: a dead token, a raising subagent, a
re-delivered job.

The other half is `values`. `AsyncSubAgentMiddleware._build_check_result` reads a
finished task's output from `thread["values"]["messages"]` — the run's
`result_payload` is Aleph's own record and the thread's `values` is the
contract. Writing one and not the other produces a task the supervisor watches
succeed and can never read, which is the most expensive shape of failure here
because it looks like it worked.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun, AgentThread
from aleph_security.agent_token import mint_agent_token
from aleph_security.principal import Principal
from aleph_workers.jobs import delegation as deleg

pytestmark = pytest.mark.integration

SECRET = "test-only-delegation-secret-not-used-elsewhere"


def test_subagent_of_parses_the_kind() -> None:
    assert deleg.subagent_of("delegation:retriever") == "retriever"
    assert deleg.subagent_of("delegation:") is None
    assert deleg.subagent_of("chunk_embed") is None
    assert deleg.subagent_of("") is None


async def _seed(maker: Any, project_id: uuid.UUID, *, status: str = "pending") -> tuple[Any, Any]:
    thread = AgentThread(
        id=uuid7(),
        project_id=project_id,
        graph_id="retriever",
        parent_agent_run_id=None,
        values_jsonb={},
        created_by=uuid.uuid4(),
    )
    run = AgentRun(
        id=uuid7(),
        project_id=project_id,
        agent_kind="delegation:retriever",
        correlation_id=str(uuid7()),
        status=status,
        input_payload={"messages": [{"role": "user", "content": "go"}]},
        agent_thread_id=thread.id,
        created_by=thread.created_by,
    )
    async with maker() as s:
        s.add(thread)
        s.add(run)
        await s.commit()
    return thread, run


def _ctx(maker: Any) -> dict[str, Any]:
    return {"session_maker": maker, "agent_token_secret": SECRET, "settings": object()}


def _token(project_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return mint_agent_token(
        secret=SECRET,
        user_id=uuid.uuid4(),
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id="t",
    )


async def _status(maker: Any, run_id: uuid.UUID) -> str:
    async with maker() as s:
        return (await s.execute(select(AgentRun.status).where(AgentRun.id == run_id))).scalar_one()


class _Gateways:
    """`gateways(ctx).litellm(project_id)` — the shape, not the client."""

    async def litellm(self, _project_id: Any) -> None:
        return None


async def _none(*_a: Any, **_k: Any) -> None:
    return None


def _principal(project_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        subject="delegation-test",
        email="delegation@test.invalid",
        actor_kind="aleph_agent",
        project_id=project_id,
    )


async def _values(maker: Any, thread_id: uuid.UUID) -> dict[str, Any]:
    async with maker() as s:
        return (
            await s.execute(select(AgentThread.values_jsonb).where(AgentThread.id == thread_id))
        ).scalar_one()


async def test_a_successful_run_writes_the_thread_values_and_converges(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    thread, run = await _seed(maker, committed_project)

    async def fake(_ctx: Any, **_: Any) -> list[dict[str, Any]]:
        return [{"role": "assistant", "content": "the answer"}]

    monkeypatch.setattr(deleg, "_run_subagent", fake)
    out = await deleg.delegated_subagent_job(
        _ctx(maker), str(run.id), _token(committed_project, run.id)
    )
    assert out["ok"] is True
    assert await _status(maker, run.id) == "succeeded"
    assert (await _values(maker, thread.id))["messages"] == [
        {"role": "assistant", "content": "the answer"}
    ]


async def test_a_raising_subagent_still_converges_and_still_writes_values(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """The failure has to be READABLE from the supervisor's side, not just logged.

    A run that fails without writing values leaves `check_async_task` reporting
    an error with nothing to show for it.
    """
    thread, run = await _seed(maker, committed_project)

    async def boom(_ctx: Any, **_: Any) -> list[dict[str, Any]]:
        msg = "gateway said no"
        raise RuntimeError(msg)

    monkeypatch.setattr(deleg, "_run_subagent", boom)
    out = await deleg.delegated_subagent_job(
        _ctx(maker), str(run.id), _token(committed_project, run.id)
    )
    assert out["ok"] is False
    assert await _status(maker, run.id) == "failed"
    values = await _values(maker, thread.id)
    assert "gateway said no" in values["messages"][0]["content"]


async def test_a_dead_token_converges_instead_of_leaving_the_run_pending(
    maker: Any, committed_project: uuid.UUID
) -> None:
    """A delegation that waits in the queue past the token's hour arrives dead.

    Raising here would leave it `pending` forever while arq retried against the
    same dead token, and the only evidence would be a stack trace in a worker
    log. Converging it to a stated failure is the difference between a ticket
    that ends and a ticket that disappears.
    """
    _thread, run = await _seed(maker, committed_project)
    out = await deleg.delegated_subagent_job(_ctx(maker), str(run.id), "not-a-real-token")
    assert out["ok"] is False
    assert await _status(maker, run.id) == "failed"


async def test_a_redelivered_job_does_not_resurrect_a_terminal_run(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """arq can deliver twice. Flipping a cancelled run back to running would
    resume work the supervisor already stopped — and `check_async_task` has
    cached the terminal status, so it would never notice."""
    _thread, run = await _seed(maker, committed_project, status="cancelled")
    called = False

    async def fake(_ctx: Any, **_: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(deleg, "_run_subagent", fake)
    out = await deleg.delegated_subagent_job(
        _ctx(maker), str(run.id), _token(committed_project, run.id)
    )
    assert out.get("skipped") == "cancelled"
    assert called is False, "a terminal run must not be executed again"
    assert await _status(maker, run.id) == "cancelled"


async def test_a_run_that_is_not_a_delegation_is_refused_not_guessed(
    maker: Any, committed_project: uuid.UUID
) -> None:
    """The kind carries the subagent name. Without the prefix there is nothing
    to run, and inventing one would run the wrong agent."""
    _thread, run = await _seed(maker, committed_project)
    async with maker() as s:
        row = (await s.execute(select(AgentRun).where(AgentRun.id == run.id))).scalar_one()
        row.agent_kind = "chunk_embed"
        await s.commit()

    out = await deleg.delegated_subagent_job(
        _ctx(maker), str(run.id), _token(committed_project, run.id)
    )
    assert out["ok"] is False
    assert await _status(maker, run.id) == "failed"


async def test_a_missing_run_is_reported_not_crashed(
    maker: Any, committed_project: uuid.UUID
) -> None:
    ghost = uuid7()
    out = await deleg.delegated_subagent_job(
        _ctx(maker), str(ghost), _token(committed_project, ghost)
    )
    assert out["ok"] is False
    assert out["error"] == "run not found"


async def test_the_job_hands_the_runner_the_run_it_is_executing(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """The caller half of attribution.

    Every other test in this file stubs `_run_subagent` entirely, which is
    exactly how the defect below survived: the function that had to carry the
    run id was the function the tests replaced. This one keeps the stub and
    inspects what it was handed.
    """
    _thread, run = await _seed(maker, committed_project)
    seen: dict[str, Any] = {}

    async def fake(_ctx: Any, **kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return [{"role": "assistant", "content": "ok"}]

    monkeypatch.setattr(deleg, "_run_subagent", fake)
    await deleg.delegated_subagent_job(_ctx(maker), str(run.id), _token(committed_project, run.id))
    assert seen["run_id"] == run.id


async def test_the_delegated_graph_config_carries_the_run_id(monkeypatch: Any) -> None:
    """The callee half, and the one that was actually broken.

    A delegated subagent's tools make model calls of their own — `read_wiki_
    deeply` drives the retrieval router, which makes three. Each reads the turn
    it belongs to from `configurable` via `run_id_from_config`. The config here
    carried `projectId` and nothing else, so all three were written with
    `agent_run_id=NULL` while still declaring `purpose="assistant.*"` — which is
    what `status.sh` number 5 counts, and what it counted: three orphans from
    one delegated wiki read, on a run whose `AgentRun` row existed throughout.

    Asserting on `projectId` too, because the fix adds a key to a dict and the
    cheapest way to get this green is to replace the dict.
    """
    from aleph_api.chat_runs import RUN_ID_KEY

    project_id, run_id = uuid7(), uuid7()
    captured: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, _state: Any, config: Any = None) -> dict[str, Any]:
            captured.update(config or {})
            return {"messages": []}

    monkeypatch.setattr("deepagents.create_deep_agent", lambda **_: _Agent())
    monkeypatch.setattr(deleg, "_api_settings", lambda: object())
    monkeypatch.setattr(deleg, "gateways", lambda _ctx: _Gateways())
    import aleph_api.copilot_agent as ca
    import aleph_api.subagents as sa

    monkeypatch.setattr(ca, "bind_runtime", lambda **_: None)
    monkeypatch.setattr(ca, "bindings_for_project", _none)
    monkeypatch.setattr(ca, "endpoint_for_project", _none)
    monkeypatch.setattr(sa, "build_subagent", lambda _n, **_k: {"model": object(), "tools": []})

    await deleg._run_subagent(
        {"session_maker": None},
        name="retriever",
        messages=[],
        principal=_principal(project_id),
        run_id=run_id,
    )
    configurable = captured.get("configurable") or {}
    assert configurable.get(RUN_ID_KEY) == str(run_id)
    assert configurable.get("projectId") == str(project_id)
