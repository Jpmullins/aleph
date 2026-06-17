"""aiq_submit_job — the deferred AIQ submission worker job.

Thin wrapper over ``aleph_aiq.dispatch._submit_core`` (tested in
test_dispatch_research.py): verifies the agent token, runs the core, and
translates the outcome into AgentRun status + AgentEvents. Tested with the
same fake-pool pattern plus a minimal fake session/maker for the DB writes.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_aiq.dispatch import SUBMIT_MAX_ATTEMPTS
from aleph_aiq.throttle import DEFAULT_KEY
from aleph_security.agent_token import mint_agent_token
from aleph_workers.jobs.aiq_submit import aiq_submit_job

pytestmark = pytest.mark.asyncio

_SECRET = "test-secret"


class _FakeAIQ:
    def __init__(self, *a: object, **k: object) -> None: ...

    async def health(self) -> bool:
        return True

    async def dispatch_deep(self, **k: object) -> str:
        return "aiq-job-123"


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.zsets: dict[str, dict[str, float]] = {}

    async def enqueue_job(self, *a: object, **k: object) -> None:
        self.calls.append(a)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in zset)
        zset.update(mapping)
        return added

    async def zrank(self, key: str, member: str) -> int | None:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        for i, (m, _) in enumerate(items):
            if m == member:
                return i
        return None

    async def zrem(self, key: str, *members: str) -> int:
        zset = self.zsets.get(key, {})
        return sum(1 for m in members if zset.pop(m, None) is not None)

    async def zremrangebyscore(self, key: str, min: float | str, max: float | str) -> int:
        lo = float("-inf") if min == "-inf" else float(min)
        hi = float("inf") if max == "+inf" else float(max)
        zset = self.zsets.get(key, {})
        doomed = [m for m, s in zset.items() if lo <= s <= hi]
        for m in doomed:
            del zset[m]
        return len(doomed)


class _FakeSession:
    def __init__(self, run: Any) -> None:
        self.run = run
        self.added: list[Any] = []
        self.committed = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def execute(self, stmt: object) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.run)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None: ...

    async def commit(self) -> None:
        self.committed = True


def _ctx(pool: _FakePool, session: _FakeSession, max_concurrent: int = 1) -> dict[str, Any]:
    return {
        "agent_token_secret": _SECRET,
        "session_maker": lambda: session,
        "redis_pool": pool,
        "settings": SimpleNamespace(
            aleph_agent_token_secret=_SECRET,
            aiq_base_url="http://aiq",
            aiq_max_concurrent_jobs=max_concurrent,
        ),
    }


def _token(project_id: object, agent_run_id: object) -> str:
    return mint_agent_token(
        secret=_SECRET,
        user_id=uuid4(),
        project_id=project_id,  # type: ignore[arg-type]
        agent_run_id=agent_run_id,  # type: ignore[arg-type]
        actor_kind="aleph_agent",
        correlation_id="corr-1",
        ttl_seconds=600,
    )


async def test_submit_job_dispatches_and_records_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    run = SimpleNamespace(status="pending", completed_at=None, error_text=None)
    session = _FakeSession(run)
    project_id, run_id = uuid4(), uuid4()
    out = await aiq_submit_job(
        _ctx(pool, session),
        str(run_id),
        str(project_id),
        "Quantum radar",
        "shallow",
        ["arxiv"],
        _token(project_id, run_id),
    )
    assert out["status"] == "dispatched"
    assert pool.calls and pool.calls[0][0] == "aiq_synthesis_poll_job"
    kinds = [getattr(e, "event_kind", None) for e in session.added]
    assert "aiq.job.dispatched" in kinds


async def test_submit_job_gate_full_requeues_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    await pool.zadd(DEFAULT_KEY, {"holder": time.time()})
    run = SimpleNamespace(status="pending", completed_at=None, error_text=None)
    session = _FakeSession(run)
    project_id, run_id = uuid4(), uuid4()
    out = await aiq_submit_job(
        _ctx(pool, session),
        str(run_id),
        str(project_id),
        "Quantum radar",
        "shallow",
        ["arxiv"],
        _token(project_id, run_id),
    )
    assert out["status"] == "queued"
    assert pool.calls and pool.calls[0][0] == "aiq_submit_job"
    assert run.status == "pending"  # untouched while waiting
    assert session.added == []


async def test_submit_job_exhausted_marks_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    await pool.zadd(DEFAULT_KEY, {"holder": time.time()})
    run = SimpleNamespace(status="pending", completed_at=None, error_text=None)
    session = _FakeSession(run)
    project_id, run_id = uuid4(), uuid4()
    out = await aiq_submit_job(
        _ctx(pool, session),
        str(run_id),
        str(project_id),
        "Quantum radar",
        "shallow",
        ["arxiv"],
        _token(project_id, run_id),
        SUBMIT_MAX_ATTEMPTS - 1,
    )
    assert out["status"] == "failed"
    assert run.status == "failed"
    kinds = [getattr(e, "event_kind", None) for e in session.added]
    assert "aiq.dispatch.failed" in kinds
