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
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.routes.surfaces import _build_tab_messages
from aleph_db.models.plugin_settings import PluginSettings
from aleph_db.repos.ledger import LedgerWriter
from aleph_kernel.kernel import Kernel
from aleph_runtime.plugin_service import PluginDraft, PluginService
from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS, UIContribution

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

#: `apps/api/src/aleph_api`, resolved from this file rather than from the
#: working directory, so the AST check below reads the same source whatever
#: pytest was invoked from.
_API_SRC = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "aleph_api"

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
) -> dict[str, Any]:
    """Dispatch one `plugin.settings.save` and hand back the handler's result.

    The result is returned rather than discarded because what a save ANSWERS is
    half of `WS-A4` c6 — see `test_a_save_does_not_branch_on_the_declared_trust`.
    """
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
        outcome = await router.dispatch(
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
    return {"ok": outcome.ok, "result": outcome.result}


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


# ---------------------------------------------------------------------------
# WS-A4 c6, the behavioural half — withdrawn, and this is what withdrawal costs
# ---------------------------------------------------------------------------
#
# The criterion asks for an `authored`-tier save to answer `requires_approval:
# true` while a `core` save does not. It was not built, deliberately, and the
# reasoning is written down in `aleph_runtime.ui_contributions` — a numbered
# decision recording it is PROPOSED, not yet in `docs/decisions.md`, so do not
# read a citation of one here. The two tests below are what stop that reasoning
# from being only prose.
#
# The decision rests on one fact about who can reach this handler at all —
# `require_at_least(..., ProjectRole.OWNER)`, so the only actor who can save a
# plugin's settings is a human project owner, and asking an owner to approve
# their own change gates nothing. That fact is pinned by
# `test_an_editor_cannot_change_settings_through_a_card_action` above and by
# `test_the_agent_cannot_reach_the_settings_save_action` below. If EITHER stops
# holding, the decision has lost its premise and the approver has to be built.


@pytest.mark.parametrize("trust", ["core", "verified", "authored"])
async def test_a_save_does_not_branch_on_the_declared_trust(
    trust: str, maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Every tier saves the same way, and no tier answers with a pending flag.

    Not a restatement of the decision — a bound on how it can be undone. The
    shape `ui_contributions` refuses is a save that RETURNS
    `requires_approval: true` and stores the value anyway: a flag with no
    consumer, which reads as a gate and is not one. That version of the feature
    is green under every other test in this file, because every other test
    ignores what the handler returned.

    So: assert against the whole serialised response, not against a key anybody
    has to remember to look for, and assert the value really landed at each
    tier. Half-building it turns this red; building the approver properly —
    a pending store, a route, a screen — also turns it red, which is correct,
    because that is a change of behaviour and it should have to be declared
    here.
    """
    import json as _json

    from aleph_runtime.ui_contributions import TrustTier

    UI_CONTRIBUTIONS.remove("probe-plugin")
    UI_CONTRIBUTIONS.register(
        UIContribution(
            plugin_id="probe-plugin",
            title="Probe plugin",
            config_schema=SCHEMA,
            trust=cast("TrustTier", trust),
        )
    )
    try:
        answer = await _save(maker, committed_project, {"field:mode": "thorough"})
    finally:
        UI_CONTRIBUTIONS.remove("probe-plugin")

    serialised = _json.dumps(answer, default=str)
    for word in ("requires_approval", "requiresApproval", "pending_approval"):
        assert word not in serialised, (
            f"a {trust!r} save answered with {word!r}. Either it is a flag with "
            "no consumer — the value is stored regardless, so the flag reads as "
            "a gate and is not one — or an approver now exists, in which case "
            "the decision recorded in aleph_runtime.ui_contributions is out of "
            "date and this test should be replaced by one that drives the "
            "approval."
        )

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
    assert [r.values.get("mode") for r in rows] == ["thorough"], (
        f"a {trust!r} save did not land. Trust is a DISPLAY attribute: if a "
        "tier now changes what saving DOES, that is the behavioural half of "
        "WS-A4 c6 arriving, and it needs an approver, a route and a screen "
        "rather than a branch here."
    )


def test_the_agent_cannot_reach_the_settings_save_action() -> None:
    """The premise under that decision: no agent tool dispatches a card action
    it was not written to dispatch.

    `plugin.settings.save` is gated at OWNER, so approval would only ever ask a
    human owner to approve their own change — UNLESS something non-human can
    reach it. The one seam by which the agent reaches card actions at all is
    `copilot_agent._dispatch_card_action_impl`, which self-calls
    `POST /v1/projects/{id}/cards/actions`.

    Read as an AST rather than as a grep, because the property is not "the
    string `plugin.settings.save` is absent" — that would still be true of a
    tool taking `action_kind` as a parameter, which is the version that puts
    every registered action within the agent's reach at once. What is checked
    is that every dispatch CALLED BY NAME passes a literal, so the reachable
    set is decidable, and then that the set excludes this one.

    "Called by name" is the honest limit, and it is stated rather than implied:
    the walk matches `ast.Name` funcs, so rebinding the function object evades
    it. Both `_alias = _dispatch_card_action_impl; await _alias("…")` and
    `await globals()["_dispatch_card_action_impl"]("…")` survive — measured. A
    wrapper taking `kind` as a parameter and a registry-dict lookup, which are
    the shapes this would plausibly grow into, are both caught. The first
    version of this docstring claimed decidability outright; it does not
    deliver that, and a pinned premise overstating itself is the failure this
    file exists to prevent.

    Scanned over all of `apps/api/src`, not just `copilot_agent.py`: the seam
    lives there today, but a tool in a new module would otherwise be invisible
    to the check that a decision rests on.
    """
    import ast

    kinds: list[str] = []
    dynamic: list[str] = []
    for path in sorted(_API_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "_dispatch_card_action_impl"):
                continue
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                kinds.append(first.value)
            else:
                dynamic.append(f"{path.relative_to(_API_SRC)}:{node.lineno}")

    # Anti-vacuity. A rename moves the seam and the walk finds nothing, at
    # which point every assertion below is trivially satisfied and this test
    # certifies a file it no longer reads.
    assert kinds or dynamic, (
        "no call to `_dispatch_card_action_impl` was found anywhere in apps/api/src. "
        "The seam has been renamed or removed; re-point this check at the new "
        "one rather than deleting it — it is the premise the decision in "
        "aleph_runtime.ui_contributions rests on."
    )
    assert not dynamic, (
        f"`_dispatch_card_action_impl` is called with a non-literal action kind "
        f"at line(s) {dynamic}. The set of card actions the agent can reach is "
        "no longer decidable, so `plugin.settings.save` may be among them and "
        "the trust-is-display decision has lost its premise."
    )
    assert "plugin.settings.save" not in kinds, (
        f"an agent tool dispatches plugin.settings.save (reachable kinds: "
        f"{sorted(set(kinds))}). Saving a plugin's settings is no longer a "
        "human-owner-only act, and the approval flow this project declined to "
        "build is now the thing that would gate it."
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


# ---------------------------------------------------------------------------
# The registry had no writer
# ---------------------------------------------------------------------------
#
# `UIContributionRegistry` had three read sites and zero writes: nothing outside
# tests ever called `register`. So `GET .../surfaces/settings` reported
# `plugins: 0` on every project that has ever existed, the "Open" button never
# rendered, and `_plugin_settings_messages` — the entire generated-settings path
# WS-A4 and WS-B1 exist to provide — had no reachable entry point. The producer
# was complete and the consumer unreachable, which is this repository's dominant
# defect class, in the workstream whose subject is plugin settings.


async def test_installing_a_plugin_with_a_config_schema_gives_it_a_settings_screen(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    UI_CONTRIBUTIONS.remove("configurable-thing")
    try:
        async with maker() as session:
            await PluginService(session).install(
                project_id=committed_project,
                actor_id=ACTOR,
                draft=_draft(
                    name="configurable-thing",
                    config_schema={
                        "type": "object",
                        "properties": {"depth": {"type": "integer", "default": 3}},
                    },
                ),
                ledger=LedgerWriter(session),
                kernel=Kernel(),
            )
            await session.commit()

        contribution = UI_CONTRIBUTIONS.get("configurable-thing")
        assert contribution is not None, (
            "the plugin installed and contributed no settings screen, so the "
            "settings pane will report `plugins: 0` and its Open button will "
            "never render"
        )
        assert contribution.config_schema["properties"]["depth"]["default"] == 3
        assert "configurable-thing" in {c.plugin_id for c in UI_CONTRIBUTIONS.all()}
    finally:
        UI_CONTRIBUTIONS.remove("configurable-thing")


async def test_a_plugin_with_no_schema_contributes_nothing(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """An empty settings screen is worse than none.

    It invites somebody to look for the setting that is not there. Without this
    test, registering unconditionally would pass the one above.
    """
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    UI_CONTRIBUTIONS.remove("plain-thing")
    async with maker() as session:
        await PluginService(session).install(
            project_id=committed_project,
            actor_id=ACTOR,
            draft=_draft(name="plain-thing"),
            ledger=LedgerWriter(session),
            kernel=Kernel(),
        )
        await session.commit()
    assert UI_CONTRIBUTIONS.get("plain-thing") is None


async def test_the_settings_screen_comes_back_after_a_restart(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Reconstitution has to re-contribute, or a restart mounts the capability
    and silently loses its configuration UI — which looks exactly like the
    plugin working and its settings having been taken away."""
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    UI_CONTRIBUTIONS.remove("durable-settings")
    try:
        async with maker() as session:
            await PluginService(session).install(
                project_id=committed_project,
                actor_id=ACTOR,
                draft=_draft(name="durable-settings", config_schema=schema),
                ledger=LedgerWriter(session),
                kernel=Kernel(),
            )
            await session.commit()

        # A new process: the registry is process-local, so drop it the way a
        # restart would.
        UI_CONTRIBUTIONS.remove("durable-settings")
        assert UI_CONTRIBUTIONS.get("durable-settings") is None

        async with maker() as session:
            mounted, failed = await PluginService(session).reconstitute(
                project_id=committed_project, kernel=Kernel()
            )
        assert "durable-settings" in mounted, failed
        assert UI_CONTRIBUTIONS.get("durable-settings") is not None, (
            "the plugin came back and its settings screen did not"
        )
    finally:
        UI_CONTRIBUTIONS.remove("durable-settings")


async def test_disabling_a_plugin_withdraws_its_settings_screen(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A settings form for a plugin that is not running writes into a capability
    nobody will read."""
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    kernel = Kernel()
    UI_CONTRIBUTIONS.remove("goes-away")
    try:
        async with maker() as session:
            await PluginService(session).install(
                project_id=committed_project,
                actor_id=ACTOR,
                draft=_draft(
                    name="goes-away",
                    config_schema={"type": "object", "properties": {"a": {"type": "string"}}},
                ),
                ledger=LedgerWriter(session),
                kernel=kernel,
            )
            await session.commit()
        await kernel.boot()
        assert UI_CONTRIBUTIONS.get("goes-away") is not None

        async with maker() as session:
            await PluginService(session).disable(
                project_id=committed_project,
                actor_id=ACTOR,
                name="goes-away",
                ledger=LedgerWriter(session),
                kernel=kernel,
            )
            await session.commit()
        assert UI_CONTRIBUTIONS.get("goes-away") is None
    finally:
        UI_CONTRIBUTIONS.remove("goes-away")


def _instructions(name: str) -> str:
    return f"---\nname: {name}\ndescription: A skill called {name}.\n---\n\nDo the thing.\n"


def _draft(**over: object) -> PluginDraft:
    name = str(over.pop("name", "settings-plugin"))
    base: dict[str, object] = {"name": name, "instructions": _instructions(name), "code": ""}
    base.update(over)
    return PluginDraft(**base)  # ty: ignore[invalid-argument-type]


# ---------------------------------------------------------------------------
# The declared schema is enforced, not decorative
# ---------------------------------------------------------------------------
#
# `_generic_plugin_settings_save` fetched `contribution.config_schema` and never
# used it. A plugin declaring `{"depth": {"type": "integer"}}` accepted the
# string "banana", stored it, and ledgered the change — and the plugin found out
# when it read its own configuration and crashed, a process boundary and some
# hours away from the person who typed it.


_TYPED_SCHEMA = {
    "type": "object",
    "properties": {"depth": {"type": "integer", "minimum": 1, "maximum": 10}},
    "required": ["depth"],
}


async def _save_settings(
    session: AsyncSession, project_id: uuid.UUID, plugin_id: str, **values: Any
) -> Any:
    """Drive the real save handler, the way the rest of this file does.

    Named apart from this file's existing `_save`: defining a second one
    later in the module silently rebound the name for every test above,
    which failed as `no plugin has declared a settings screen for "{...}"`
    — a message about a plugin id that was actually a params dict.
    """
    from aleph_a2ui.action_router import CardActionRequest
    from aleph_api.a2ui_handlers import _generic_plugin_settings_save

    return await _generic_plugin_settings_save(
        session=session,
        ledger=LedgerWriter(session),
        principal=_principal(),
        project_id=project_id,
        request=CardActionRequest(
            action_kind="plugin.settings.save",
            surface_kind="settings",
            card_id=None,
            target_id=None,
            target_kind=None,
            params={
                "plugin_id": plugin_id,
                "plugin_kind": "plugin",
                # `submitted_values` reads only `field:`-prefixed keys — that
                # prefix IS the convention, and a test that skipped it would
                # submit an empty settings object and validate nothing.
                **{f"field:{k}": v for k, v in values.items()},
            },
        ),
    )


async def test_a_value_the_schema_forbids_is_refused_and_stored_nowhere(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    from aleph_core.errors import ValidationFailed

    UI_CONTRIBUTIONS.remove("typed-plugin")
    UI_CONTRIBUTIONS.register(
        UIContribution(plugin_id="typed-plugin", title="Typed", config_schema=_TYPED_SCHEMA)
    )
    try:
        async with maker() as session:
            with pytest.raises(ValidationFailed) as caught:
                await _save_settings(session, committed_project, "typed-plugin", depth="banana")
            await session.rollback()
        assert "depth" in str(caught.value), str(caught.value)

        # And nothing was written. A refusal that leaves the row behind is worse
        # than no refusal: the operator is told it failed and the plugin reads
        # the bad value anyway.
        async with maker() as session:
            rows = list(
                (
                    await session.execute(
                        select(PluginSettings).where(
                            PluginSettings.project_id == committed_project,
                            PluginSettings.plugin_id == "typed-plugin",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == [], "a refused save still wrote a settings row"
    finally:
        UI_CONTRIBUTIONS.remove("typed-plugin")


async def test_a_value_the_schema_allows_is_stored(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The refusal must not have closed the feature it guards."""
    UI_CONTRIBUTIONS.remove("typed-plugin")
    UI_CONTRIBUTIONS.register(
        UIContribution(plugin_id="typed-plugin", title="Typed", config_schema=_TYPED_SCHEMA)
    )
    try:
        async with maker() as session:
            await _save_settings(session, committed_project, "typed-plugin", depth=4)
            await session.commit()
        async with maker() as session:
            row = (
                await session.execute(
                    select(PluginSettings).where(
                        PluginSettings.project_id == committed_project,
                        PluginSettings.plugin_id == "typed-plugin",
                    )
                )
            ).scalar_one()
        assert row.values["depth"] == 4
    finally:
        UI_CONTRIBUTIONS.remove("typed-plugin")


async def test_a_plugin_with_an_invalid_schema_is_reported_as_the_defect_it_is(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A broken schema must not degrade to "no validation at all".

    Skipping validation when the schema will not compile is the state this
    whole function exists to end, arrived at by a different door.
    """
    from aleph_core.errors import ValidationFailed

    UI_CONTRIBUTIONS.remove("broken-schema")
    UI_CONTRIBUTIONS.register(
        UIContribution(
            plugin_id="broken-schema",
            title="Broken",
            config_schema={"type": "object", "properties": {"n": {"type": "not-a-json-type"}}},
        )
    )
    try:
        async with maker() as session:
            with pytest.raises(ValidationFailed) as caught:
                await _save_settings(session, committed_project, "broken-schema", n=1)
            await session.rollback()
        assert "invalid settings schema" in str(caught.value)
    finally:
        UI_CONTRIBUTIONS.remove("broken-schema")


def _principal() -> Any:
    from aleph_security.principal import Principal

    return Principal(user_id=ACTOR, subject="local-dev", email="dev@example.com", actor_kind="user")
