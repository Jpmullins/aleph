"""A plugin declares a schema and gets a working settings screen. WS-A4.

`settings_card.py` is 279 lines of working, unit-tested generator that turns a
JSON Schema into a settings screen, and it had ZERO importers outside its own
tests. WS-A3a gave it one — and that caller was the SAVE handler, so the screen
could only be seen by first writing to it. There was no way to open it.

A plugin nobody can configure is one you trust blindly or edit `.env` for, which
is the opposite of what "an agent authors plugins for itself" is supposed to
feel like from the outside.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.routes.surfaces import _build_tab_messages
from aleph_db.models.plugin_settings import PluginSettings
from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS, UIContribution

pytestmark = pytest.mark.integration

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "endpoint": {"type": "string", "title": "Endpoint"},
        "enabled": {"type": "boolean", "default": True},
        "mode": {"type": "string", "enum": ["fast", "thorough"], "default": "fast"},
        "depth": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
    },
}


@pytest.fixture
def contribution() -> Any:
    c = UIContribution(
        plugin_id="probe-plugin",
        title="Probe plugin",
        description="A plugin that declares only a schema.",
        config_schema=SCHEMA,
        trust="authored",
    )
    UI_CONTRIBUTIONS.register(c)
    yield c
    UI_CONTRIBUTIONS.remove("probe-plugin")


def _components(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        out.extend(m.get("updateComponents", {}).get("components", []) or [])
    return out


async def test_a_schema_alone_produces_every_control_with_no_ui_code(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 1. A string, a boolean, an enum and a number, each rendered."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session,
            committed_project,
            "settings",
            {"plugin": "probe-plugin"},
            "settings:probe-plugin",
        )
    kinds = {c.get("component") for c in _components(messages)}
    assert {"TextField", "CheckBox", "ChoicePicker", "Slider"} <= kinds, sorted(kinds)


async def test_an_unopened_screen_shows_the_schema_defaults(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A plugin with no stored row renders defaults, which is what a settings
    screen should do the first time it is opened — not blank fields."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session,
            committed_project,
            "settings",
            {"plugin": "probe-plugin"},
            "settings:probe-plugin",
        )
    model = next(
        m["updateDataModel"]["value"]
        for m in reversed(messages)
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert model["enabled"] is True
    assert model["mode"] == "fast"
    assert model["depth"] == 3


async def test_saving_persists_and_is_auditable_in_one_transaction(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 2: exactly one row AND exactly one ledger event."""
    from aleph_a2ui.action_router import ActionRouter, CardActionRequest
    from aleph_api.a2ui_handlers import build_action_router
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

    principal = Principal(
        user_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        subject="a4",
        email="a4@example.test",
        actor_kind="user",
    )
    # OWNER, cached the way the middleware caches it. The handler gates at
    # OWNER because a settings save can change what a plugin does, and a card
    # action must not be a way for an EDITOR to make a change they cannot make
    # over HTTP — the gate would be bypassed by the surface rather than by the
    # route. Granting it here tests the handler; removing the gate would test
    # nothing.
    principal.cache_role(committed_project, "owner")
    router: ActionRouter = build_action_router()

    async with maker() as session:
        await router.dispatch(
            session=session,
            ledger=LedgerWriter(session),
            principal=principal,
            project_id=committed_project,
            request=CardActionRequest(
                action_kind="plugin.settings.save",
                surface_kind="settings",
                card_id=None,
                target_id=None,
                target_kind=None,
                params={
                    "plugin_id": "probe-plugin",
                    "plugin_kind": "plugin",
                    "field:endpoint": "http://example.test",
                    "field:enabled": False,
                    "field:mode": "thorough",
                    "field:depth": 7,
                },
            ),
        )
        await session.commit()

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(PluginSettings).where(
                        PluginSettings.project_id == committed_project,
                        PluginSettings.plugin_id == "probe-plugin",
                    )
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == "plugin_settings.update",
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].values["endpoint"] == "http://example.test"
    assert rows[0].values["mode"] == "thorough"
    assert len(events) == 1
    assert events[0].target_id == rows[0].id
    # The KEYS, not the values. A settings payload is arbitrary plugin data and
    # the ledger is append-only.
    assert events[0].payload_jsonb["keys"] == sorted(rows[0].values)


async def test_the_screen_reads_back_what_was_saved(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The read path and the write path meeting — which is the whole workstream.

    Before this the generator's only caller was the save handler, so the screen
    could not be opened without first writing to it.
    """
    from aleph_core.ids import uuid7

    async with maker() as session:
        session.add(
            PluginSettings(
                id=uuid7(),
                project_id=committed_project,
                plugin_id="probe-plugin",
                values={"endpoint": "http://stored", "enabled": False, "mode": "thorough"},
                created_by=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
            )
        )
        await session.commit()

    async with maker() as session:
        messages = await _build_tab_messages(
            session,
            committed_project,
            "settings",
            {"plugin": "probe-plugin"},
            "settings:probe-plugin",
        )
    model = next(
        m["updateDataModel"]["value"]
        for m in reversed(messages)
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert model["endpoint"] == "http://stored"
    assert model["enabled"] is False


async def test_a_plugin_that_declared_nothing_is_named_not_blanked(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """An empty message list renders as a blank block, which is
    indistinguishable from a pane that failed to load."""
    async with maker() as session:
        messages = await _build_tab_messages(
            session, committed_project, "settings", {"plugin": "nobody"}, "settings:nobody"
        )
    model = next(
        m["updateDataModel"]["value"]
        for m in reversed(messages)
        if m.get("updateDataModel", {}).get("path") == "/"
    )
    assert "nobody" in model["message"]


def test_a_second_plugin_cannot_take_over_an_existing_settings_screen() -> None:
    """Same rule as `PANE_REGISTRY.extend()`, and for the same reason: a silent
    overwrite is a plugin quietly configuring something that is not its own."""
    c = UIContribution(plugin_id="dup-probe", title="One")
    UI_CONTRIBUTIONS.register(c)
    try:
        with pytest.raises(ValueError, match="already registered"):
            UI_CONTRIBUTIONS.register(UIContribution(plugin_id="dup-probe", title="Two"))
    finally:
        UI_CONTRIBUTIONS.remove("dup-probe")


async def _save(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    params: dict[str, Any],
    *,
    role: str | None = "owner",
) -> None:
    from aleph_a2ui.action_router import ActionRouter, CardActionRequest
    from aleph_api.a2ui_handlers import build_action_router
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal

    principal = Principal(
        user_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        subject="a4",
        email="a4@example.test",
        actor_kind="user",
    )
    if role is not None:
        principal.cache_role(project_id, role)

    router: ActionRouter = build_action_router()
    async with maker() as session:
        await router.dispatch(
            session=session,
            ledger=LedgerWriter(session),
            principal=principal,
            project_id=project_id,
            request=CardActionRequest(
                action_kind="plugin.settings.save",
                surface_kind="settings",
                card_id=None,
                target_id=None,
                target_kind=None,
                params={"plugin_id": "probe-plugin", "plugin_kind": "plugin", **params},
            ),
        )
        await session.commit()


async def test_a_cleared_field_stays_cleared(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The save REPLACES; it does not merge.

    A settings screen submits every field it renders, so merging keeps a value
    the operator just cleared — the field goes blank, the save reports success,
    and the old value is still in effect. Nothing on screen would say so.
    """
    await _save(maker, committed_project, {"field:endpoint": "http://first", "field:mode": "fast"})
    await _save(maker, committed_project, {"field:mode": "thorough"})

    async with maker() as session:
        row = (
            await session.execute(
                select(PluginSettings).where(
                    PluginSettings.project_id == committed_project,
                    PluginSettings.plugin_id == "probe-plugin",
                )
            )
        ).scalar_one()
    assert row.values == {"mode": "thorough"}, (
        f"the cleared endpoint survived the save: {row.values}"
    )


async def test_an_editor_cannot_change_settings_through_a_card_action(
    contribution: Any, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Card actions are gated at EDITOR; this handler gates at OWNER.

    Without that line the surface becomes a way to make a change the route
    refuses — the gate bypassed by the UI rather than by the endpoint. The
    connector branch has the same guard and the same comment; this is the test
    neither had.
    """
    from aleph_core.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        await _save(maker, committed_project, {"field:mode": "fast"}, role="editor")

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(PluginSettings).where(
                        PluginSettings.project_id == committed_project,
                        PluginSettings.plugin_id == "probe-plugin",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "the refused save wrote a row anyway"
