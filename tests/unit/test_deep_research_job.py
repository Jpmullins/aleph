"""deep_research_job failure semantics (the builder_job model): any exception
marks the AgentRun failed with error_text + completed_at — no strands."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import aleph_workers.jobs.research as research_mod
from aleph_security.agent_token import mint_agent_token
from aleph_workers.jobs.research import deep_research_job

pytestmark = pytest.mark.asyncio

_SECRET = "test-secret"


class _FakeSession:
    def __init__(self, run: Any) -> None:
        self.run = run
        self.added: list[Any] = []
        self.commits = 0

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
        self.commits += 1


def _ctx(session: _FakeSession) -> dict[str, Any]:
    return {
        "agent_token_secret": _SECRET,
        "session_maker": lambda: session,
        "litellm_client": None,
        "scholar": None,
        "asset_store": None,
        "redis_pool": SimpleNamespace(enqueue_job=None),
        "settings": SimpleNamespace(
            research_max_iterations_deep=3,
            research_max_iterations_shallow=1,
            research_max_sources_per_iter=6,
            research_max_total_sources=15,
            aleph_auth_mode="local",
        ),
    }


def _token(project_id: Any, agent_run_id: Any) -> str:
    return mint_agent_token(
        secret=_SECRET,
        user_id=uuid4(),
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id="corr-research",
        ttl_seconds=600,
    )


def _run(project_id: Any) -> Any:
    # Doubles as the ModelProfile row (bindings_jsonb) — the fake session
    # answers every scalar_one_or_none with this namespace.
    return SimpleNamespace(
        status="pending",
        started_at=None,
        completed_at=None,
        error_text=None,
        result_payload=None,
        input_payload={"topic": "Quantum radar", "depth": "deep", "allowed_connectors": None},
        project_id=project_id,
        bindings_jsonb={},
    )


async def test_exception_marks_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("connector registry exploded")

    monkeypatch.setattr(research_mod, "resolve_bound_tools", boom)

    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    session = _FakeSession(run)

    out = await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))

    assert out["ok"] is False
    assert run.status == "failed"
    assert run.error_text is not None and "connector registry exploded" in run.error_text
    assert run.completed_at is not None


async def test_missing_topic_fails_cleanly() -> None:
    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    run.input_payload = {"depth": "deep"}
    session = _FakeSession(run)

    out = await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))

    assert out["ok"] is False
    assert run.status == "failed"
    assert run.error_text is not None and "no topic" in run.error_text


async def test_error_text_truncated_to_4096(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("x" * 10_000)

    monkeypatch.setattr(research_mod, "resolve_bound_tools", boom)

    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    session = _FakeSession(run)

    await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))
    assert run.error_text is not None and len(run.error_text) == 4096


async def test_cancelled_marks_failed_then_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """arq job_timeout / shutdown cancels the task: the run must end `failed`
    (not strand as running) and the CancelledError must still propagate."""
    import asyncio

    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    session = _FakeSession(run)

    async def _boom(*_a: object, **_k: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(research_mod, "resolve_bound_tools", _boom)
    with pytest.raises(asyncio.CancelledError):
        await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))
    assert run.status == "failed"
    assert run.completed_at is not None


async def test_retry_after_interruption_does_not_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    """An arq re-enqueue after a hard kill re-invokes the job while the run is
    already `running`. It must NOT re-run the graph (which would duplicate
    ingested sources) — it converges the run to `failed` and returns."""
    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    run.status = "running"  # a prior attempt started it, then was interrupted
    session = _FakeSession(run)

    def _should_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("graph must not run on a retry of an interrupted run")

    monkeypatch.setattr(research_mod, "resolve_bound_tools", _should_not_run)
    out = await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))
    assert out["ok"] is False
    assert run.status == "failed"
    assert run.completed_at is not None


async def test_already_succeeded_retry_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """arq re-delivering an already-succeeded job must NOT flip it to failed."""
    project_id, run_id = uuid4(), uuid4()
    run = _run(project_id)
    run.status = "succeeded"
    run.result_payload = {"proposal_ids": ["p1"]}
    session = _FakeSession(run)

    def _should_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("terminal run must not re-run")

    monkeypatch.setattr(research_mod, "resolve_bound_tools", _should_not_run)
    out = await deep_research_job(_ctx(session), str(run_id), _token(project_id, run_id))
    assert out["ok"] is True
    assert run.status == "succeeded"  # not flipped
