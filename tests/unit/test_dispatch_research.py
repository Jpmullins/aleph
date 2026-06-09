"""The shared dispatch_research helper's pure dispatch core.

`_dispatch_core` is the part that, given resolved connectors + a created run,
mints tokens, dispatches to AIQ, and enqueues the poll job. It needs no DB, so
we test it directly with a fake AIQ client + fake arq pool.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio


class _FakeAIQ:
    def __init__(self, *a: object, **k: object) -> None: ...

    async def health(self) -> bool:
        return True

    async def dispatch_deep(self, **k: object) -> str:
        return "aiq-job-123"


class _FakeAIQDown(_FakeAIQ):
    async def health(self) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def enqueue_job(self, *a: object, **k: object) -> None:
        self.calls.append(a)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(aleph_agent_token_secret="test-secret", aiq_base_url="http://aiq")


async def test_dispatch_core_dispatches_and_enqueues(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    pool = _FakePool()
    result = await d._dispatch_core(
        settings=_settings(),
        redis_pool=pool,
        project_id=uuid4(),
        principal_user_id=uuid4(),
        agent_run_id=uuid4(),
        correlation_id="boot-1",
        topic="Quantum radar",
        depth="shallow",
        enabled_connectors=["arxiv"],
    )
    assert result.dispatched is True
    assert result.aiq_job_id == "aiq-job-123"
    assert pool.calls and pool.calls[0][0] == "aiq_synthesis_poll_job"


async def test_dispatch_core_aiq_down_no_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQDown)
    pool = _FakePool()
    result = await d._dispatch_core(
        settings=_settings(),
        redis_pool=pool,
        project_id=uuid4(),
        principal_user_id=uuid4(),
        agent_run_id=uuid4(),
        correlation_id="boot-2",
        topic="Quantum radar",
        depth="shallow",
        enabled_connectors=["arxiv"],
    )
    assert result.dispatched is False
    assert result.aiq_job_id is None
    assert pool.calls == []
