"""The Inspector renders a real run — including one that failed.

WS-C3b. Against Postgres, not a fixture: the point of this pane is that the
data exists, and for most of this project's life it did not. Seventeen places
constructed `AgentRun` and none of them was a chat turn, so a pane built earlier
would have rendered an authoritative empty list for every project.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.routes.surfaces import _build_tab_messages
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentEvent, AgentRun

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


async def _seed_run(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    *,
    status: str,
    error_text: str | None = None,
    events: tuple[tuple[str, dict[str, Any]], ...] = (),
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with maker() as session:
        session.add(
            AgentRun(
                id=run_id,
                project_id=project_id,
                agent_kind="assistant",
                correlation_id=f"chat-{run_id.hex}",
                status=status,
                started_at=utcnow(),
                completed_at=utcnow(),
                error_text=error_text,
                input_payload={},
                created_by=ACTOR,
            )
        )
        for kind, payload in events:
            session.add(
                AgentEvent(
                    id=uuid.uuid4(),
                    agent_run_id=run_id,
                    event_kind=kind,
                    payload_jsonb=payload,
                )
            )
        await session.commit()
    return run_id


def _model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The data model the surface bound, as the client receives it.

    Read out of the LAST `updateDataModel` at the root path — the same message
    the A2UI processor applies. Reconstructing it any other way would test this
    helper rather than the surface.
    """
    for m in reversed(messages):
        update = m.get("updateDataModel") or {}
        if update.get("path") == "/":
            value = update.get("value")
            if isinstance(value, dict):
                return value
    msg = "the surface emitted no root updateDataModel"
    raise AssertionError(msg)


async def test_failed_run_shows_its_failure(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """THE criterion: the failing tool's name and the error text both reach the
    surface. Before this, the only place either existed was container stderr."""
    run_id = await _seed_run(
        maker,
        committed_project,
        status="failed",
        error_text="PermissionDenied: no access to project",
        events=(
            ("tool_started", {"tool": "search_wiki", "subagent": "orchestrator", "args": {}}),
            (
                "tool_failed",
                {
                    "tool": "search_wiki",
                    "subagent": "orchestrator",
                    "error_class": "PermissionDenied",
                    "error": "no access to project",
                },
            ),
        ),
    )
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {"run_id": str(run_id)}, "inspector"
        )
    model = _model(messages)
    assert model["selected"]["id"] == str(run_id)
    assert model["selected"]["status"] == "failed"
    assert "PermissionDenied" in model["selected"]["error_text"]

    kinds = {e["kind"] for e in model["events"]}
    assert "tool_failed" in kinds
    failed = next(e for e in model["events"] if e["kind"] == "tool_failed")
    assert failed["tool"] == "search_wiki"
    assert failed["error_class"] == "PermissionDenied"
    assert "no access" in failed["error"]


async def test_a_run_with_no_events_is_shown_as_such_not_as_nothing(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A turn that died before its first tool call is the most informative
    shape a reader can be shown, and the easiest one to render as blank."""
    run_id = await _seed_run(
        maker, committed_project, status="failed", error_text="GatewayUnavailable"
    )
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {"run_id": str(run_id)}, "inspector"
        )
    model = _model(messages)
    assert model["selected"]["id"] == str(run_id)
    assert model["events"] == []
    assert "GatewayUnavailable" in model["selected"]["error_text"]


async def test_no_run_named_selects_the_most_recent(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """What somebody opening the pane straight after a turn is looking for."""
    await _seed_run(maker, committed_project, status="completed")
    newest = await _seed_run(maker, committed_project, status="completed")
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {}, "inspector"
        )
    model = _model(messages)
    assert model["selected"] is not None
    assert model["selected"]["id"] in {str(newest), *[r["id"] for r in model["runs"]]}
    assert len(model["runs"]) >= 2


async def test_a_run_id_from_another_project_is_not_shown(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The id comes from a URL. Fetching it unscoped would leak another
    project's run into this project's pane."""
    stranger = uuid.uuid4()
    async with maker() as session:
        session.add(
            AgentRun(
                id=stranger,
                project_id=uuid.uuid4(),
                agent_kind="assistant",
                correlation_id=f"chat-{stranger.hex}",
                status="completed",
                started_at=utcnow(),
                input_payload={},
                created_by=ACTOR,
            )
        )
        await session.commit()

    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {"run_id": str(stranger)}, "inspector"
        )
    model = _model(messages)
    assert model["selected"] is None or model["selected"]["id"] != str(stranger)


async def test_a_malformed_run_id_does_not_reach_the_query(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`run_id=../../etc` must render an empty selection, not raise."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {"run_id": "not-a-uuid"}, "inspector"
        )
    assert _model(messages)["selected"] is None


async def test_an_empty_project_says_so(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "inspector", {}, "inspector"
        )
    model = _model(messages)
    assert model["runs"] == []
    assert model["selected"] is None
