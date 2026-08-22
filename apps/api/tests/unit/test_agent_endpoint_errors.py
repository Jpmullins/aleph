"""A failed agent run must say so, in the channel the browser is reading.

Before Aleph owned this route the SSE stream simply stopped when the assistant
broke: the chat showed a half-written message, the browser invented its own
error — that is the confusing "The run has already errored with RUN_ERROR" in
the console — and the actual cause existed only in the API container's stderr.

`ag_ui_langgraph.add_langgraph_fastapi_endpoint` cannot do better; it has no
error handling at all. `LangGraphAgent.run()` cannot either: exactly one `try:`,
one `finally:`, and no `except` between them.

Three properties, all of which were false:

* a graph that raises produces RUN_ERROR as the LAST frame;
* nothing is emitted after a terminal event, in either direction;
* the id the browser is shown appears in the log and in the response header.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from ag_ui.core.events import EventType, RunErrorEvent, RunFinishedEvent, RunStartedEvent
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from aleph_api.agui_endpoint import RUN_ID_HEADER, add_aleph_agui_endpoint

PATH = "/t/agent"

RUN_INPUT: dict[str, Any] = {
    "threadId": "thread-1",
    "runId": "run-1",
    "state": {},
    "messages": [{"id": "m1", "role": "user", "content": "hello"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


class _StubAgent:
    """Stands in for `LangGraphAgent`. `clone()` is the only method the route
    uses besides `run`, and cloning per request is a real upstream requirement
    (per-request state lives on `self.active_run`), so the stub honours it."""

    def __init__(self, events: list[Any], *, raise_after: int | None = None) -> None:
        self._events = events
        self._raise_after = raise_after
        self.name = "stub"

    def clone(self) -> _StubAgent:
        return _StubAgent(self._events, raise_after=self._raise_after)

    async def run(self, _input: Any) -> AsyncIterator[Any]:
        for index, event in enumerate(self._events):
            if self._raise_after is not None and index == self._raise_after:
                msg = "the graph node exploded"
                raise RuntimeError(msg)
            yield event
        if self._raise_after is not None and self._raise_after >= len(self._events):
            msg = "the graph node exploded"
            raise RuntimeError(msg)


def _app(agent: _StubAgent) -> FastAPI:
    """Mount the route over a resolver that always answers with `agent`.

    The route takes a resolver rather than an agent since WS-MEP-6: which agent
    answers depends on the project's model bindings, which change while the
    process is up. These tests are about the envelope, so the resolver is
    constant — `test_agent_profile_switch.py` is where a varying one is driven.
    """
    app = FastAPI()

    async def _resolve(_project_id: UUID | None) -> _StubAgent:
        return agent

    add_aleph_agui_endpoint(app, _resolve, path=PATH)
    return app


def _frames(body: str) -> list[dict[str, Any]]:
    """Parse the `data:` payloads out of an SSE body."""
    out: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                out.append(json.loads(payload))
    return out


async def _post(app: FastAPI) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(PATH, json=RUN_INPUT)


async def test_graph_exception_yields_run_error() -> None:
    """The last frame must be RUN_ERROR, not a truncated body."""
    agent = _StubAgent([RunStartedEvent(threadId="thread-1", runId="run-1")], raise_after=1)
    response = await _post(_app(agent))

    frames = _frames(response.text)
    assert frames, "the stream was empty"
    assert frames[-1]["type"] == EventType.RUN_ERROR.value, (
        f"the run died and the last frame was {frames[-1]['type']}"
    )
    assert "RuntimeError" in frames[-1]["message"]


async def test_a_clean_run_is_untouched() -> None:
    """The guard must not change a run that works — or it is a rewrite, not a
    wrapper, and every future upstream event type becomes Aleph's problem."""
    agent = _StubAgent(
        [
            RunStartedEvent(threadId="thread-1", runId="run-1"),
            RunFinishedEvent(threadId="thread-1", runId="run-1"),
        ]
    )
    frames = _frames((await _post(_app(agent))).text)
    assert [f["type"] for f in frames] == [
        EventType.RUN_STARTED.value,
        EventType.RUN_FINISHED.value,
    ]


async def test_no_events_after_terminal() -> None:
    """Upstream falls through from RUN_ERROR to RUN_FINISHED.

    A client that sees both is entitled to conclude the run recovered, which is
    the worst possible reading of a failure.
    """
    agent = _StubAgent(
        [
            RunStartedEvent(threadId="thread-1", runId="run-1"),
            RunErrorEvent(message="the agent reported its own failure"),
            RunFinishedEvent(threadId="thread-1", runId="run-1"),
        ]
    )
    frames = _frames((await _post(_app(agent))).text)
    terminal = [
        f for f in frames if f["type"] in {EventType.RUN_ERROR.value, EventType.RUN_FINISHED.value}
    ]
    assert len(terminal) == 1, f"a run ended {len(terminal)} times"
    assert frames[-1]["type"] == EventType.RUN_ERROR.value


async def test_a_raise_after_a_reported_error_does_not_double_report() -> None:
    """Both guards at once: the agent reports RUN_ERROR and then raises."""
    agent = _StubAgent(
        [
            RunStartedEvent(threadId="thread-1", runId="run-1"),
            RunErrorEvent(message="reported"),
        ],
        raise_after=2,
    )
    frames = _frames((await _post(_app(agent))).text)
    assert sum(1 for f in frames if f["type"] == EventType.RUN_ERROR.value) == 1


async def test_run_id_links_error_to_log() -> None:
    """The id the user is shown must be findable in the log and on the response.

    "I got an error at 14:22" is not a search. An id is.

    Captured with `structlog.testing.capture_logs` rather than `caplog`:
    structlog renders its key-values at OUTPUT time, so a caplog record's
    `getMessage()` is just the event name and an assertion over it would pass or
    fail for reasons unrelated to whether the id was bound.
    """
    agent = _StubAgent([RunStartedEvent(threadId="thread-1", runId="run-1")], raise_after=1)
    with capture_logs() as captured:
        response = await _post(_app(agent))

    frames = _frames(response.text)
    message = frames[-1]["message"]
    found = re.search(r"[0-9a-f]{32}", message)
    assert found, f"the RUN_ERROR message carries no searchable id: {message}"
    run_id = found.group(0)

    assert response.headers.get(RUN_ID_HEADER) == run_id
    assert any(entry.get("aleph_run_id") == run_id for entry in captured), (
        f"the id shown to the user appears nowhere in the log: {captured}"
    )


async def test_the_health_route_still_answers() -> None:
    agent = _StubAgent([])
    transport = ASGITransport(app=_app(agent))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get(f"{PATH}/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# WS-MEP-6: resolving WHICH agent answers can itself fail.
# ---------------------------------------------------------------------------


def _failing_app(exc: Exception) -> FastAPI:
    app = FastAPI()

    async def _resolve(_project_id: UUID | None) -> Any:
        raise exc

    add_aleph_agui_endpoint(app, _resolve, path=PATH)
    return app


async def test_a_resolution_failure_is_reported_as_run_error() -> None:
    """Building the agent is now part of the turn, so it fails like one.

    Before the resolver, the graph existed before the first request and could
    not fail during one. Now an unreachable database or a profile binding no
    model raises inside the handler — and an unhandled raise there is a 500 with
    an empty body, which is the "the stream just stopped" shape this whole
    module exists to remove, one layer further out.
    """
    frames = _frames((await _post(_failing_app(RuntimeError("no model is bound")))).text)
    assert frames, "a resolution failure produced no frames at all"
    assert frames[-1]["type"] == EventType.RUN_ERROR.value
    assert "no model is bound" in frames[-1]["message"]


async def test_a_resolution_failure_carries_the_same_searchable_id() -> None:
    app = _failing_app(RuntimeError("the profile could not be read"))
    with capture_logs() as captured:
        response = await _post(app)

    message = _frames(response.text)[-1]["message"]
    found = re.search(r"[0-9a-f]{32}", message)
    assert found, f"the RUN_ERROR message carries no searchable id: {message}"
    assert response.headers.get(RUN_ID_HEADER) == found.group(0)
    assert any(entry.get("event") == "agui.resolution_failed" for entry in captured), (
        f"the resolution failure was not logged under its own event: {captured}"
    )


async def test_a_resolution_failure_still_closes_the_recorded_run() -> None:
    """Otherwise the row sits in `running` until the reaper, saying nothing."""

    class _Recorder:
        def __init__(self) -> None:
            self.finished: list[tuple[str, str | None]] = []

        async def begin(self, _thread_id: object) -> Any:
            return SimpleNamespace(run_id=uuid4())

        async def finish(self, _run: Any, *, status: str, error_text: str | None) -> None:
            self.finished.append((status, error_text))

    app = FastAPI()
    recorder = _Recorder()

    async def _resolve(_project_id: UUID | None) -> Any:
        msg = "the gateway endpoint row is gone"
        raise RuntimeError(msg)

    add_aleph_agui_endpoint(app, _resolve, path=PATH, recorder=recorder)
    await _post(app)

    assert recorder.finished, "the run was begun and never finished"
    status, error_text = recorder.finished[-1]
    assert status == "failed"
    assert error_text is not None and "the gateway endpoint row is gone" in error_text


async def test_the_project_named_by_the_thread_id_is_what_gets_resolved() -> None:
    """The resolver is asked about the project, not about the raw thread id.

    Uses `middleware/agent_scope.thread_project_id` — the extractor the
    membership check already ran against — so the profile the turn is built from
    and the project the caller was authorised for cannot be two different
    things.
    """
    project_id = uuid4()
    seen: list[UUID | None] = []
    app = FastAPI()

    async def _resolve(pid: UUID | None) -> _StubAgent:
        seen.append(pid)
        return _StubAgent(
            [
                RunStartedEvent(threadId="t", runId="r"),
                RunFinishedEvent(threadId="t", runId="r"),
            ]
        )

    add_aleph_agui_endpoint(app, _resolve, path=PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        await client.post(PATH, json={**RUN_INPUT, "threadId": f"proj:{project_id}:chat-1"})

    assert seen == [project_id]
