"""`plugin.settings.save` — the action a generated settings screen dispatches.

Three things had to be true at once for a plugin to be able to declare a
settings screen, and none of them were:

* the agent-facing catalog had to contain the input controls (WS-A3a's
  derivation — covered by `scripts/check-agent-catalog-covers-renderer.sh`);
* the save action had to exist in `catalog.json`, or `ActionRouter.register`
  raises before the app finishes booting;
* the handler had to be gated at the same role the equivalent HTTP route is
  gated at, or the surface becomes a way around it.

The third is the one worth a test on its own. Card actions are authorised at
EDITOR in `routes/cards.py`; connector bindings are authorised at OWNER in
`routes/connectors.py`. A handler that writes a binding without re-checking is
an EDITOR-reachable path to an OWNER-gated change, and nothing about the
request looks unusual.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aleph_a2ui.action_router import CardActionRequest
from aleph_a2ui.catalog import CATALOG
from aleph_a2ui.settings_card import SETTINGS_SAVE_ACTION
from aleph_api.a2ui_handlers import _plugin_settings_save, build_action_router
from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole


def _principal(project_id: Any, role: ProjectRole | None) -> Principal:
    p = Principal(
        user_id=uuid4(),
        subject="s",
        email="a@example.com",
        actor_kind="user",
    )
    if role is not None:
        p.cache_role(project_id, role.value)
    return p


def _request(*, plugin_id: str, plugin_kind: str = "connector", enabled: bool = True):
    return CardActionRequest(
        surface_kind="plugins",
        action_kind=SETTINGS_SAVE_ACTION,
        card_id=None,
        target_id=None,
        target_kind=None,
        params={
            "plugin_id": plugin_id,
            "plugin_kind": plugin_kind,
            "field:enabled": enabled,
        },
    )


def test_the_save_action_is_registered() -> None:
    """`register` raises for an action the catalog does not declare, so this
    also proves `plugin.settings.save` reached `catalog.json`."""
    assert SETTINGS_SAVE_ACTION in build_action_router().registered_actions()
    assert SETTINGS_SAVE_ACTION in CATALOG["actions"]


@pytest.mark.asyncio
async def test_an_editor_cannot_save_connector_settings() -> None:
    """The OWNER gate is checked in the handler, not only on the HTTP route.

    `session=None` is deliberate: the role check must happen before anything
    touches the database, so a refused caller cannot even be observed by the
    store. If the gate moves below the first query this raises AttributeError
    instead of PermissionDenied and the test fails.
    """
    project_id = uuid4()
    with pytest.raises(PermissionDenied):
        await _plugin_settings_save(
            session=None,  # type: ignore[arg-type]
            ledger=None,  # type: ignore[arg-type]
            principal=_principal(project_id, ProjectRole.EDITOR),
            project_id=project_id,
            request=_request(plugin_id=str(uuid4())),
        )


@pytest.mark.asyncio
async def test_an_unknown_plugin_kind_is_refused_by_name_not_defaulted() -> None:
    """Defaulting the kind would write one plugin's settings onto another's row."""
    from aleph_core.errors import ValidationFailed

    project_id = uuid4()
    with pytest.raises(ValidationFailed, match="unknown plugin_kind"):
        await _plugin_settings_save(
            session=None,  # type: ignore[arg-type]
            ledger=None,  # type: ignore[arg-type]
            principal=_principal(project_id, ProjectRole.OWNER),
            project_id=project_id,
            request=_request(plugin_id=str(uuid4()), plugin_kind="kernel_capability"),
        )
