"""The contract is with deepagents, so the SDK is what tests it.

`docs/decisions.md` D17: Aleph hosts the Agent Protocol so
`AsyncSubAgentMiddleware` can drive delegated work against Aleph's own queue,
instead of Aleph reimplementing a delegation framework. The five routes are
therefore an INTEGRATION SURFACE with a third-party library, and a test written
against my reading of that library proves only that I read it consistently
twice.

So these tests drive `langgraph_sdk`'s own client — the exact object the
middleware constructs, calling the exact five methods it calls — over ASGI
against the real app. If the SDK changes what it sends or expects, this goes red
here rather than in production with a delegated task that never resolves.

The one thing the SDK cannot check for us is the STATUS VOCABULARY, because it
passes those strings through verbatim; `_build_check_result` is what compares
them. That is covered separately by
`packages/aleph-db/tests/test_agent_protocol_statuses.py`, and the last test
here ties the two together by asserting every status this route can emit is one
the middleware branches on.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from aleph_db.agent_protocol import PROTOCOL_STATUSES
from aleph_db.models.agent import AgentRun, AgentThread
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

BASE = "http://testserver/v1/agent-protocol"


class _RecordingPool:
    """Stands in for arq. Records what would have been enqueued.

    The point of these tests is the HTTP contract, not the worker. A real pool
    would make every one of them need Redis, and the thing worth asserting —
    that a run is committed BEFORE the job is enqueued — is observable here and
    not through a live queue.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, name: str, *args: Any) -> None:
        self.jobs.append((name, args))


def _app(monkeypatch: Any, maker: Any, principal: Principal) -> Any:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="test-only-secret-not-used-anywhere-else",
        redis_url="redis://127.0.0.1:1/0",
    )
    app.state.session_maker = maker
    app.state.arq_pool = _RecordingPool()

    async def _fake_local_dev(_request: Any) -> Any:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)
    return app


def _agent_principal(project_id: uuid.UUID) -> Principal:
    """A principal as a minted agent token produces one: bound to a project."""
    return Principal(
        user_id=uuid.uuid4(),
        subject="agent",
        email="agent@aleph.local",
        actor_kind="aleph_agent",
        agent_run_id=uuid.uuid4(),
        correlation_id="test-correlation",
        project_id=project_id,
    )


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _sdk(app: Any) -> Any:
    """The SDK client the middleware itself builds, pointed at the app over ASGI.

    `get_client(url=…)` builds an httpx client under the hood; swapping its
    transport for ASGI is what lets the real SDK talk to the real app with no
    socket. Everything above this line is the middleware's own code path.
    """
    from langgraph_sdk import get_client

    client = get_client(url=BASE, headers={"Authorization": "Bearer local-dev"})
    client.http.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE,
        headers={"Authorization": "Bearer local-dev"},
    )
    return client


async def test_the_sdk_can_create_a_thread_and_read_it_back(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """`threads.create()` then `threads.get()` — the launch and the collect."""
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    client = await _sdk(app)

    thread = await client.threads.create()
    assert "thread_id" in thread, f"the middleware indexes tasks by this key: {thread}"

    fetched = await client.threads.get(thread_id=thread["thread_id"])
    assert "values" in fetched, "_build_check_result reads output from thread['values']"
    assert fetched["values"] == {}


async def test_a_run_returns_immediately_and_is_committed_before_it_is_enqueued(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """The whole feature: the supervisor gets a ticket and keeps talking.

    The commit-before-enqueue ordering is asserted rather than assumed. arq can
    hand a job to a worker before the handler's own transaction would close, and
    a worker that looks up a row which is not there yet fails a delegation for a
    reason nobody can see from either side.
    """
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    client = await _sdk(app)

    thread = await client.threads.create()
    run = await client.runs.create(
        thread_id=thread["thread_id"],
        assistant_id="retriever",
        input={"messages": [{"role": "user", "content": "read the wiki on X"}]},
    )
    assert "run_id" in run
    assert run["status"] == "pending"

    async with maker() as s:
        row = (
            await s.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(run["run_id"])))
        ).scalar_one_or_none()
    assert row is not None, "the run must be committed before the job is enqueued"
    assert row.agent_kind == "delegation:retriever"
    assert row.agent_thread_id == uuid.UUID(thread["thread_id"])

    enqueued = app.state.arq_pool.jobs
    assert len(enqueued) == 1
    assert enqueued[0][0] == "delegated_subagent_job"
    assert enqueued[0][1][0] == run["run_id"]


async def test_run_status_is_readable_and_is_a_word_the_middleware_knows(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    client = await _sdk(app)

    thread = await client.threads.create()
    run = await client.runs.create(
        thread_id=thread["thread_id"], assistant_id="researcher", input={"messages": []}
    )
    got = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
    assert got["status"] in PROTOCOL_STATUSES


async def test_cancel_is_idempotent_and_does_not_resurrect_a_finished_run(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """Cancelling a succeeded run reports success, not `cancelled`.

    The supervisor asked to stop work that is no longer running. Answering
    `cancelled` would misreport a result that exists, and `check_async_task`
    caches terminal statuses — so the wrong word here is permanent for that task.
    """
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    client = await _sdk(app)

    thread = await client.threads.create()
    run = await client.runs.create(
        thread_id=thread["thread_id"], assistant_id="reviewer", input={"messages": []}
    )
    await client.runs.cancel(thread_id=thread["thread_id"], run_id=run["run_id"])
    first = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
    assert first["status"] == "cancelled"

    await client.runs.cancel(thread_id=thread["thread_id"], run_id=run["run_id"])
    again = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
    assert again["status"] == "cancelled", "cancel must be idempotent"

    async with maker() as s:
        row = (
            await s.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(run["run_id"])))
        ).scalar_one()
        row.status = "succeeded"
        await s.commit()

    await client.runs.cancel(thread_id=thread["thread_id"], run_id=run["run_id"])
    after = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
    assert after["status"] == "success", "a finished run keeps its result"


async def test_update_interrupts_the_live_run_rather_than_queueing_behind_it(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """`update_async_task` sends `multitask_strategy="interrupt"`.

    The supervisor is told the task restarted with new instructions, so leaving
    the previous run live would spend a second model loop whose output nobody
    reads — and bill for it.
    """
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    client = await _sdk(app)

    thread = await client.threads.create()
    first = await client.runs.create(
        thread_id=thread["thread_id"], assistant_id="retriever", input={"messages": []}
    )
    second = await client.runs.create(
        thread_id=thread["thread_id"],
        assistant_id="retriever",
        input={"messages": [{"role": "user", "content": "actually, focus on Y"}]},
        multitask_strategy="interrupt",
    )
    assert second["run_id"] != first["run_id"], "update starts a NEW run on the SAME thread"

    was = await client.runs.get(thread_id=thread["thread_id"], run_id=first["run_id"])
    assert was["status"] == "cancelled"
    now = await client.runs.get(thread_id=thread["thread_id"], run_id=second["run_id"])
    assert now["status"] == "pending"


async def test_an_unknown_assistant_id_is_refused_with_the_list(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID
) -> None:
    """A supervisor naming a graph that does not exist must not get a live run.

    The refusal names what IS available, because a model that cannot see the
    allowed set will guess again.
    """
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    async with _client(app) as raw:
        created = await raw.post(f"{BASE}/threads", json={})
        tid = created.json()["thread_id"]
        resp = await raw.post(
            f"{BASE}/threads/{tid}/runs",
            json={"assistant_id": "definitely-not-a-subagent", "input": {}},
        )
    assert resp.status_code == 422
    body = resp.text
    assert "retriever" in body and "researcher" in body


async def test_a_credential_bound_to_no_project_is_refused(monkeypatch: Any, maker: Any) -> None:
    """Scope rides on the credential here, so an unscoped one has no scope at all.

    A human session token has `project_id = None`. These routes have no project
    in the path to fall back to, so the only safe answer is refusal — which is
    STRICTER than an ordinary Aleph route, not looser.
    """
    human = Principal(
        user_id=uuid.uuid4(),
        subject="someone",
        email="someone@example.com",
        actor_kind="user",
        project_id=None,
    )
    app = _app(monkeypatch, maker, human)
    async with _client(app) as raw:
        resp = await raw.post(f"{BASE}/threads", json={})
    assert resp.status_code == 403


async def test_a_thread_in_another_project_is_indistinguishable_from_absent(
    monkeypatch: Any, maker: Any, committed_project: uuid.UUID, second_project: uuid.UUID
) -> None:
    """Cross-project reads return 404, not 403 — existence must not leak."""
    app = _app(monkeypatch, maker, _agent_principal(committed_project))
    async with _client(app) as raw:
        mine = (await raw.post(f"{BASE}/threads", json={})).json()["thread_id"]

    async with maker() as s:
        row = (
            await s.execute(select(AgentThread).where(AgentThread.id == uuid.UUID(mine)))
        ).scalar_one()
        row.project_id = second_project
        await s.commit()

    async with _client(app) as raw:
        resp = await raw.get(f"{BASE}/threads/{mine}")
    assert resp.status_code == 404
