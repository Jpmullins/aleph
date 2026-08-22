"""The generated settings screen, end to end against a real store.

`aleph_a2ui.settings_card` was finished and unreachable: a complete generator
with no caller outside its own tests. A generator nothing calls is the same
shape of defect as a column nothing reads — it is asserted to work rather than
observed to.

This is the round trip that makes it real: render a connector's settings from
what the database holds, dispatch the save the rendered button emits, and
re-render. What comes back must reflect the STORED value, because the handler
re-renders from storage rather than echoing the submission — a value the server
coerced or has no column for then shows up unchanged instead of appearing saved.

`connector_bindings` has no teardown entry in `tests/integration/conftest.py`,
so this test deliberately uses the rolled-back `session` fixture rather than
`committed_project`: nothing here reaches disk.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.action_router import CardActionRequest
from aleph_a2ui.settings_card import SETTINGS_SAVE_ACTION, SETTINGS_VALUE_PREFIX
from aleph_api.a2ui_handlers import _plugin_settings_save, connector_settings_surface
from aleph_core.ids import uuid7
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole

pytestmark = pytest.mark.integration


def _owner(project_id: uuid.UUID) -> Principal:
    p = Principal(user_id=uuid7(), subject="s", email="o@example.com", actor_kind="user")
    p.cache_role(project_id, ProjectRole.OWNER.value)
    return p


async def _a_connector(session: AsyncSession, *, enabled_by_default: bool) -> Connector:
    connector = Connector(
        id=uuid7(),
        kind=f"probe-{uuid7().hex[:8]}",
        name="Probe connector",
        output_kind="document",
        requires_auth=True,
        metadata_schema_jsonb={},
        enabled_by_default=enabled_by_default,
        created_by=uuid7(),
    )
    session.add(connector)
    await session.flush()
    return connector


def _components(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    update = next(m for m in messages if "updateComponents" in m)
    return {c["id"]: c for c in update["updateComponents"]["components"]}


def _data_model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    update = next(m for m in messages if "updateDataModel" in m)
    return update["updateDataModel"]["value"]


async def test_an_unbound_connector_renders_its_default_not_false(session: AsyncSession) -> None:
    """Seeding from `False` would show a running connector as switched off, and
    the first save would then turn it off for real."""
    project_id = uuid7()
    connector = await _a_connector(session, enabled_by_default=True)

    messages = await connector_settings_surface(
        session, project_id=project_id, connector_id=connector.id
    )
    assert _data_model(messages)["enabled"] is True
    assert messages[0]["createSurface"]["surfaceId"] == f"settings:{connector.id}"

    comps = _components(messages)
    assert comps["f0-enabled"]["component"] == "CheckBox"
    # The connector needs a key and the screen has no field for one; saying so
    # is the difference between "no key required" and "not editable here".
    assert "API key" in comps["subtitle"]["text"]


async def test_saving_writes_the_binding_and_re_renders_from_storage(
    session: AsyncSession,
) -> None:
    project_id = uuid7()
    connector = await _a_connector(session, enabled_by_default=True)
    ledger = LedgerWriter(session)

    result = await _plugin_settings_save(
        session=session,
        ledger=ledger,
        principal=_owner(project_id),
        project_id=project_id,
        request=CardActionRequest(
            surface_kind="plugins",
            action_kind=SETTINGS_SAVE_ACTION,
            card_id=None,
            target_id=None,
            target_kind=None,
            params={
                "plugin_id": str(connector.id),
                "plugin_kind": "connector",
                f"{SETTINGS_VALUE_PREFIX}enabled": False,
            },
        ),
    )

    binding = (
        await session.execute(
            select(ConnectorBinding).where(
                ConnectorBinding.project_id == project_id,
                ConnectorBinding.connector_id == connector.id,
            )
        )
    ).scalar_one()
    assert binding.enabled is False

    # The response carries the re-rendered screen, and it shows what the row
    # holds — not what was submitted.
    assert _data_model(result["surface"])["enabled"] is False

    rows = list(
        (
            await session.execute(
                select(ActionLedgerEvent).where(
                    ActionLedgerEvent.project_id == project_id,
                    ActionLedgerEvent.action_kind == "connector_binding.create",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "a settings save must leave exactly one ledger row"
    assert rows[0].target_id == binding.id
    assert rows[0].payload_jsonb["enabled"] is False


async def test_a_second_save_updates_rather_than_duplicating(session: AsyncSession) -> None:
    """The unique constraint on (project, connector) makes a second insert an
    error rather than a second row — the handler must find and update."""
    project_id = uuid7()
    connector = await _a_connector(session, enabled_by_default=False)
    ledger = LedgerWriter(session)
    principal = _owner(project_id)

    for value in (True, False, True):
        await _plugin_settings_save(
            session=session,
            ledger=ledger,
            principal=principal,
            project_id=project_id,
            request=CardActionRequest(
                surface_kind="plugins",
                action_kind=SETTINGS_SAVE_ACTION,
                card_id=None,
                target_id=None,
                target_kind=None,
                params={
                    "plugin_id": str(connector.id),
                    "plugin_kind": "connector",
                    f"{SETTINGS_VALUE_PREFIX}enabled": value,
                },
            ),
        )

    bindings = list(
        (
            await session.execute(
                select(ConnectorBinding).where(
                    ConnectorBinding.project_id == project_id,
                    ConnectorBinding.connector_id == connector.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(bindings) == 1
    assert bindings[0].enabled is True

    kinds = [
        r.action_kind
        for r in (
            await session.execute(
                select(ActionLedgerEvent)
                .where(ActionLedgerEvent.project_id == project_id)
                .order_by(ActionLedgerEvent.timestamp)
            )
        )
        .scalars()
        .all()
    ]
    assert kinds == [
        "connector_binding.create",
        "connector_binding.update",
        "connector_binding.update",
    ]
