"""The vocabulary the API accepts and the handlers the worker binds must agree.

Two processes hold two halves of one fact. The API validates a requested kind
against ``aleph_db.repos.background_tasks.BACKGROUND_TASK_KINDS`` and refuses
anything else with a 422; the worker looks the kind up in
``BACKGROUND_TASK_HANDLERS`` and can only fail the ticket if it is missing. They
cannot import each other — apps never depend on apps — so nothing but this test
stops them drifting.

Both directions fail, because each drift is a different defect:

* a kind the route accepts with no handler is a ticket that can only ever fail,
  and it fails minutes later in a worker log rather than in the assistant's
  hand;
* a handler nobody can request is dead code that reads as capability.

The last test pins the arq registration, because a bound handler and an
unregistered job function produce the identical symptom — a ticket that sits at
``pending`` forever with nothing anywhere saying why. It stubs the worker's
environment because importing ``aleph_workers.arq`` constructs real settings at
module scope; the values are obvious non-secrets long enough to clear the
boot-time length guards.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from aleph_db.repos.background_tasks import BACKGROUND_TASK_KINDS
from aleph_workers.jobs import background_kinds
from aleph_workers.jobs.background import background_task_job
from aleph_workers.jobs.background_kinds import (
    BACKGROUND_TASK_HANDLERS,
    MAX_UNITS_PER_TASK,
    reindex_corpus,
    review_sweep,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

#: Every worker setting with no default, plus a plausible value. Not secrets:
#: nothing here opens a cipher or reaches a gateway, and both key fields refuse
#: the published .env.example placeholder, so they have to be something else.
_WORKER_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph",
    "REDIS_URL": "redis://localhost:6379/0",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-test-not-a-real-key",
    "LANGFUSE_SECRET_KEY": "sk-test-not-a-real-key",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318",
    "LITELLM_BASE_URL": "http://localhost:4001",
    "INSIGHTS_LITELLM_API_KEY": "sk-test-not-a-real-key",
    "ALEPH_API_INTERNAL_URL": "http://localhost:8000",
    "ALEPH_AGENT_TOKEN_SECRET": "ws-h6-worker-test-secret-0123456789abcdef",
    "ALEPH_CREDENTIAL_MASTER_KEY": "ws-h6-worker-test-master-0123456789abcdef",
}


def test_every_kind_has_a_handler() -> None:
    assert set(BACKGROUND_TASK_HANDLERS) == set(BACKGROUND_TASK_KINDS)


def test_every_handler_takes_the_task_and_returns_a_result() -> None:
    """One argument, awaited, returning the payload the ticket reports.

    A handler with a different shape fails at dispatch time inside a worker,
    where the only evidence is a ticket that went straight to `failed`.
    """
    for kind, handler in BACKGROUND_TASK_HANDLERS.items():
        assert inspect.iscoroutinefunction(handler), kind
        params = list(inspect.signature(handler).parameters)
        assert params == ["task"], f"{kind} takes {params}"


def test_the_supervisor_is_registered_with_arq(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name the route enqueues is a function this worker answers to."""
    for key, value in _WORKER_ENV.items():
        monkeypatch.setenv(key, value)

    from aleph_workers.arq import WorkerSettings

    registered = {fn.__name__ for fn in WorkerSettings.functions}
    assert "background_task_job" in registered, (
        "a ticket dispatched to an unregistered job sits at `pending` forever"
    )
    assert background_task_job.__name__ == "background_task_job"


# ---------------------------------------------------------------------------
# The cancellation checkpoint, in the handlers a user can actually request
# ---------------------------------------------------------------------------
#
# The mid-flight cancel test in tests/integration ran a FIXTURE handler that
# supplied its own `await task.cancelled()` call. Deleting the checkpoint from
# both shipping handlers — `reindex_corpus` and `review_sweep` — left every
# integration and worker test green. That is the plan's own risk note come
# true: "a flag nobody checks looks identical to a flag that works until you
# test it". Without the checkpoint a cancelled `review_sweep` fans out all 200
# pages and is then relabelled `cancelled`, which is the difference between
# stopping a sweep and letting it finish and calling it stopped.


class _StubTask:
    """The two verbs a handler is allowed to use, and nothing else.

    Modelled on `BackgroundTask`'s surface rather than on a mock library, so a
    handler reaching for anything the real object does not expose fails here.
    """

    def __init__(self, *, rows: list[Any], cancel_after: int | None) -> None:
        self._rows = rows
        self._cancel_after = cancel_after
        self.checkpoints = 0
        self.enqueued: list[tuple[str, tuple[Any, ...]]] = []
        self.steps: list[str] = []
        self.project_id = uuid.UUID("00000000-0000-0000-0000-0000000000b6")
        self.params: dict[str, Any] = {}
        self.cancel_seen = False

    async def cancelled(self) -> bool:
        self.checkpoints += 1
        if self._cancel_after is None:
            return False
        stop = self.checkpoints > self._cancel_after
        self.cancel_seen = self.cancel_seen or stop
        return stop

    @asynccontextmanager
    async def step(self, name: str, **payload: Any) -> AsyncIterator[dict[str, Any]]:
        self.steps.append(name)
        yield dict(payload)

    def agent_token(self, *, ttl_seconds: int = 3600) -> str:
        return "stub-token"

    async def enqueue(self, function: str, *args: Any) -> None:
        self.enqueued.append((function, args))

    def maker(self) -> Any:
        return _StubSession(self._rows)


class _StubSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _statement: Any) -> Any:
        rows = self._rows

        class _Result:
            def all(self) -> list[Any]:
                return rows

            def scalars(self) -> Any:
                return SimpleNamespace(all=lambda: rows)

        return _Result()


async def test_review_sweep_stops_at_the_checkpoint_when_cancelled() -> None:
    """40 of 200 pages, not 200 then a relabel."""
    pages = [(uuid.uuid4(), uuid.uuid4()) for _ in range(50)]
    task = _StubTask(rows=pages, cancel_after=3)

    result = await review_sweep(cast("Any", task))

    assert len(task.enqueued) == 3, (
        f"the sweep enqueued {len(task.enqueued)} of {len(pages)} pages after a "
        "cancel — the checkpoint is not being consulted"
    )
    assert result["pages_enqueued"] == 3
    assert task.cancel_seen is True


async def test_review_sweep_does_all_the_work_when_not_cancelled() -> None:
    """The stopping test alone would pass on a handler that enqueues nothing."""
    pages = [(uuid.uuid4(), uuid.uuid4()) for _ in range(7)]
    task = _StubTask(rows=pages, cancel_after=None)

    result = await review_sweep(cast("Any", task))

    assert len(task.enqueued) == 7
    assert result["pages_enqueued"] == 7
    assert task.cancel_seen is False


async def test_reindex_corpus_stops_at_the_checkpoint_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_ids = [uuid.uuid4() for _ in range(50)]

    async def _fake_unindexed(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        return doc_ids

    monkeypatch.setattr(background_kinds, "unindexed_document_ids", _fake_unindexed)
    task = _StubTask(rows=[], cancel_after=4)

    result = await reindex_corpus(cast("Any", task))

    assert len(task.enqueued) == 4, (
        f"reindex enqueued {len(task.enqueued)} of {len(doc_ids)} documents after "
        "a cancel — the checkpoint is not being consulted"
    )
    assert result["documents_enqueued"] == 4


async def test_reindex_corpus_does_all_the_work_when_not_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_ids = [uuid.uuid4() for _ in range(6)]

    async def _fake_unindexed(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        return doc_ids

    monkeypatch.setattr(background_kinds, "unindexed_document_ids", _fake_unindexed)
    task = _StubTask(rows=[], cancel_after=None)

    result = await reindex_corpus(cast("Any", task))

    assert len(task.enqueued) == 6
    assert result["documents_enqueued"] == 6


#: The cap, written out. Deriving the test's input from MAX_UNITS_PER_TASK
#: makes the test scale with the constant, so raising the constant to ten
#: million still passes — which is what the first version of this test did.
#: The cap is a decision about how much work one confused request may cause;
#: changing it means changing this line in the same commit.
CAP = 200


async def test_the_cap_is_the_number_the_design_chose() -> None:
    assert MAX_UNITS_PER_TASK == CAP, (
        "the per-task cap moved. That is allowed, but it is a decision about "
        "blast radius, not a tuning constant — say why in the same change."
    )


async def test_both_handlers_cap_the_work_they_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MAX_UNITS_PER_TASK is the answer to "a confused agent does a great deal
    at once". Raising it 200 → 10_000_000 left all fifteen tests green."""
    over = CAP + 25

    pages = [(uuid.uuid4(), uuid.uuid4()) for _ in range(over)]
    sweep = _StubTask(rows=pages, cancel_after=None)
    sweep_result = await review_sweep(cast("Any", sweep))
    assert sweep_result["pages_found"] == over
    assert len(sweep.enqueued) == CAP
    assert sweep_result["capped"] is True

    doc_ids = [uuid.uuid4() for _ in range(over)]

    async def _fake_unindexed(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        return doc_ids

    monkeypatch.setattr(background_kinds, "unindexed_document_ids", _fake_unindexed)
    reindex = _StubTask(rows=[], cancel_after=None)
    reindex_result = await reindex_corpus(cast("Any", reindex))
    assert reindex_result["documents_found"] == over
    assert len(reindex.enqueued) == CAP
    assert reindex_result["capped"] is True
