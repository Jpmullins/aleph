"""The shared dispatch_research helper's pure dispatch core.

`_dispatch_core` is the part that, given resolved connectors + a created run,
mints tokens, dispatches to AIQ, and enqueues the poll job. It needs no DB, so
we test it directly with a fake AIQ client + fake arq pool.

Submission is gated by AIQThrottle (bounding research jobs in flight inside
aiq-server): with a free slot the dispatch happens inline exactly as before;
with the gate full the job is handed to the deferred ``aiq_submit_job``
instead — never lost, never stampeding.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


class _FakeAIQ:
    dispatch_calls: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *a: object, **k: object) -> None: ...

    async def health(self) -> bool:
        return True

    async def dispatch_deep(self, **k: object) -> str:
        type(self).dispatch_calls.append(k)
        return "aiq-job-123"


class _FakeAIQDown(_FakeAIQ):
    async def health(self) -> bool:
        return False


class _FakePool:
    """Fake ArqRedis: enqueue_job + the zset commands AIQThrottle uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.zsets: dict[str, dict[str, float]] = {}

    async def enqueue_job(self, *a: object, **k: object) -> None:
        self.calls.append(a)
        self.call_kwargs.append(k)

    def _sorted(self, key: str) -> list[tuple[str, float]]:
        return sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in zset)
        zset.update(mapping)
        return added

    async def zrank(self, key: str, member: str) -> int | None:
        for i, (m, _) in enumerate(self._sorted(key)):
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

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))


def _settings(max_concurrent: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        aleph_agent_token_secret="test-secret",
        aiq_base_url="http://aiq",
        aiq_max_concurrent_jobs=max_concurrent,
    )


async def _run_dispatch_core(
    d: Any, pool: _FakePool, settings: SimpleNamespace, agent_run_id: object
) -> Any:
    return await d._dispatch_core(
        settings=settings,
        redis_pool=pool,
        project_id=uuid4(),
        principal_user_id=uuid4(),
        agent_run_id=agent_run_id,
        correlation_id="boot-1",
        topic="Quantum radar",
        depth="shallow",
        enabled_connectors=["arxiv"],
    )


async def test_dispatch_core_dispatches_and_enqueues(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    result = await _run_dispatch_core(d, pool, _settings(), uuid4())
    assert result.dispatched is True
    assert result.aiq_job_id == "aiq-job-123"
    assert pool.calls and pool.calls[0][0] == "aiq_synthesis_poll_job"


async def test_dispatch_core_aiq_down_requeues_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """AIQ unreachable at dispatch must NOT strand the run — it defers to the
    submit job (queued, retried when AIQ recovers), like the throttle-full path."""
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQDown)
    pool = _FakePool()
    result = await _run_dispatch_core(d, pool, _settings(), uuid4())
    # Same semantics as the throttle-full path: in flight (dispatched) but queued
    # via the deferred submit job — nothing reached aiq-server, NOT stranded.
    assert result.dispatched is True
    assert result.queued is True
    assert result.aiq_job_id is None
    assert pool.calls and pool.calls[0][0] == "aiq_submit_job"


async def test_dispatch_core_holds_a_throttle_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inline dispatch must register itself in the shared gate."""
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    run_id = uuid4()
    result = await _run_dispatch_core(d, pool, _settings(), run_id)
    assert result.dispatched is True
    assert await pool.zrank(DEFAULT_KEY, str(run_id)) is not None


async def test_dispatch_core_gate_full_defers_to_submit_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate full → no AIQ submission; the deferred submit job carries it."""
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    _FakeAIQ.dispatch_calls = []
    pool = _FakePool()
    import time as _time

    await pool.zadd(DEFAULT_KEY, {"holder": _time.time()})
    run_id = uuid4()
    result = await _run_dispatch_core(d, pool, _settings(max_concurrent=1), run_id)

    assert _FakeAIQ.dispatch_calls == []  # nothing reached aiq-server
    assert result.dispatched is True  # the run is still in flight, just queued
    assert result.queued is True
    assert result.aiq_job_id is None
    assert pool.calls and pool.calls[0][0] == "aiq_submit_job"
    assert pool.calls[0][1] == str(run_id)
    assert pool.call_kwargs[0].get("_defer_by")  # retried later, not dropped
    # The queued run must NOT hold a slot while waiting.
    assert await pool.zrank(DEFAULT_KEY, str(run_id)) is None


async def _run_submit_core(
    d: Any, pool: _FakePool, settings: SimpleNamespace, agent_run_id: object, attempt: int = 0
) -> Any:
    return await d.submit_core(
        settings=settings,
        redis_pool=pool,
        project_id=uuid4(),
        principal_user_id=uuid4(),
        agent_run_id=agent_run_id,
        topic="Quantum radar",
        depth="shallow",
        enabled_connectors=["arxiv"],
        poll_agent_token="poll-token",
        attempt=attempt,
    )


async def test_submit_core_dispatches_when_slot_free(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    run_id = uuid4()
    outcome = await _run_submit_core(d, pool, _settings(), run_id)
    assert outcome.status == "dispatched"
    assert outcome.aiq_job_id == "aiq-job-123"
    assert pool.calls and pool.calls[0][0] == "aiq_synthesis_poll_job"
    assert pool.calls[0][2] == "aiq-job-123"
    assert await pool.zrank(DEFAULT_KEY, str(run_id)) is not None  # slot held


async def test_submit_core_gate_full_requeues_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    _FakeAIQ.dispatch_calls = []
    pool = _FakePool()
    import time as _time

    await pool.zadd(DEFAULT_KEY, {"holder": _time.time()})
    run_id = uuid4()
    outcome = await _run_submit_core(d, pool, _settings(max_concurrent=1), run_id, attempt=2)
    assert outcome.status == "requeued"
    assert _FakeAIQ.dispatch_calls == []
    assert pool.calls and pool.calls[0][0] == "aiq_submit_job"
    assert pool.calls[0][-1] == 3  # attempt incremented
    assert pool.call_kwargs[0].get("_defer_by")


async def test_submit_core_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    import time as _time

    await pool.zadd(DEFAULT_KEY, {"holder": _time.time()})
    outcome = await _run_submit_core(
        d, pool, _settings(max_concurrent=1), uuid4(), attempt=d.SUBMIT_MAX_ATTEMPTS - 1
    )
    assert outcome.status == "exhausted"
    assert pool.calls == []  # no further requeue


async def test_submit_core_aiq_down_releases_slot_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A health blip after acquiring must not hold the slot or kill the run."""
    import aleph_aiq.dispatch as d
    from aleph_aiq.throttle import DEFAULT_KEY

    monkeypatch.setattr(d, "AIQClient", _FakeAIQDown)
    pool = _FakePool()
    run_id = uuid4()
    outcome = await _run_submit_core(d, pool, _settings(), run_id)
    assert outcome.status == "requeued"
    assert pool.calls and pool.calls[0][0] == "aiq_submit_job"
    assert await pool.zrank(DEFAULT_KEY, str(run_id)) is None  # slot released
