"""aiq_synthesis_poll_job must release the AIQ in-flight gate slot.

The slot acquired at submission (see aleph_aiq.throttle) is held while the
research job runs inside aiq-server and must be freed exactly when the job
leaves AIQ: on any terminal status, and on the poller's give-up timeout. A
still-running poll must NOT release it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_aiq.client import AIQJobStatus
from aleph_aiq.throttle import DEFAULT_KEY
from aleph_security.agent_token import mint_agent_token
from aleph_workers.jobs.aiq_synthesis import _MAX_ATTEMPTS, aiq_synthesis_poll_job

pytestmark = pytest.mark.asyncio

_SECRET = "test-secret"


def _fake_client_returning(status: AIQJobStatus) -> type:
    class _FakeAIQ:
        def __init__(self, *a: object, **k: object) -> None: ...

        async def get_job(self, job_id: str) -> Any:
            return SimpleNamespace(status=status)

    return _FakeAIQ


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
        return 0


class _FakeSession:
    def __init__(self, run: Any) -> None:
        self.run = run

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def execute(self, stmt: object) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.run)

    async def commit(self) -> None: ...


async def _run_poll(
    pool: _FakePool, status: AIQJobStatus, attempt: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Any, str]:
    import aleph_workers.jobs.aiq_synthesis as mod

    monkeypatch.setattr(mod, "AIQClient", _fake_client_returning(status))
    project_id, run_id = uuid4(), uuid4()
    run = SimpleNamespace(status="running", completed_at=None, error_text=None)
    token = mint_agent_token(
        secret=_SECRET,
        user_id=uuid4(),
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id="corr-1",
        ttl_seconds=600,
    )
    await pool.zadd(DEFAULT_KEY, {str(run_id): time.time()})  # slot held by this run
    ctx: dict[str, Any] = {
        "agent_token_secret": _SECRET,
        "session_maker": lambda: _FakeSession(run),
        "litellm_client": None,
        "redis_pool": pool,
        "settings": SimpleNamespace(
            aleph_agent_token_secret=_SECRET,
            aiq_base_url="http://aiq",
            aiq_max_concurrent_jobs=3,
        ),
    }
    out = await aiq_synthesis_poll_job(
        ctx, str(run_id), "aiq-job-123", str(project_id), "Quantum radar", token, attempt
    )
    return out, run, str(run_id)


async def test_still_running_keeps_slot_and_requeues(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _FakePool()
    out, _, run_id = await _run_poll(pool, AIQJobStatus.RUNNING, attempt=1, monkeypatch=monkeypatch)
    assert out["status"] == "polling"
    assert pool.calls and pool.calls[0][0] == "aiq_synthesis_poll_job"
    assert await pool.zrank(DEFAULT_KEY, run_id) is not None  # slot still held


async def test_terminal_failure_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _FakePool()
    out, run, run_id = await _run_poll(
        pool, AIQJobStatus.FAILED, attempt=1, monkeypatch=monkeypatch
    )
    assert out["status"] == "failed"
    assert run.status == "failed"
    assert await pool.zrank(DEFAULT_KEY, run_id) is None  # slot freed


async def test_poll_timeout_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _FakePool()
    out, run, run_id = await _run_poll(
        pool, AIQJobStatus.RUNNING, attempt=_MAX_ATTEMPTS - 1, monkeypatch=monkeypatch
    )
    assert out["status"] == "failed"
    assert run.status == "failed"
    assert await pool.zrank(DEFAULT_KEY, run_id) is None  # slot freed
