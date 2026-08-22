"""What catalogs this project's renderer should hold. WS-A3b.

`GET /v1/projects/{id}/catalogs` returns the ARRAY the browser hands to
`MessageProcessor` — core first, then one catalog per enabled plugin. The
client already had the mechanism (`new MessageProcessor(catalogs)` takes a
list, and `createSurface` names the one it wants); what it did not have was
anything telling it that more than one catalog exists.

**Driven by the plugin rows, not by a constant.** The set is derived from
`plugins` where `state = 'installed'` — the durable record WS-A1b added — so a
plugin installed by an agent in one process is in the array the next process
serves. A hardcoded list here would make "install a plugin" a deploy.

**Why it can report a refusal instead of just failing.** A plugin whose
component name is already core's is refused (`CatalogCollisionError`), and the
honest thing to do with a refusal on a GET is to serve the catalogs that are
fine and name the one that is not. Returning 500 would take the whole workspace
down over one bad row; dropping it silently would be the overwrite this
workstream exists to prevent, moved one layer out. So it comes back under
`rejected`, with both sides named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter
from sqlalchemy import select

from aleph_a2ui.plugin_catalogs import (
    CORE_CATALOG_ID,
    LEGACY_CORE_CATALOG_ID,
    AssembledCatalog,
    CatalogCollisionError,
    PluginCatalog,
    assemble_catalogs,
    merge_for_chat,
    plugin_catalog_from_provides,
)
from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_db.models.plugin import Plugin

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["catalogs"])


async def enabled_plugin_catalogs(session: AsyncSession, project_id: UUID) -> list[PluginCatalog]:
    """The UI contribution of every plugin currently installed for a project.

    `state == "installed"` is the live set: `disabled` is deliberately off and
    `failed` did not mount, and neither should have a catalog in the array —
    a surface naming a catalog whose plugin is not running must fail to resolve
    rather than paint against a stale definition.
    """
    rows = (
        (
            await session.execute(
                select(Plugin)
                .where(Plugin.project_id == project_id, Plugin.state == "installed")
                .order_by(Plugin.name, Plugin.major_version)
            )
        )
        .scalars()
        .all()
    )
    return [
        plugin_catalog_from_provides(row.name, row.major_version, row.provides or ())
        for row in rows
    ]


def _describe(catalog: AssembledCatalog) -> dict[str, Any]:
    return {
        "catalogId": catalog.catalog_id,
        "source": catalog.source,
        "plugin": catalog.plugin,
        "major": catalog.major,
        "components": list(catalog.components),
        "functions": list(catalog.functions),
        "capabilityKey": catalog.capability_key,
    }


def assemble_or_reject(
    plugins: list[PluginCatalog],
) -> tuple[list[AssembledCatalog], list[dict[str, str]]]:
    """Assemble, dropping any plugin the collision check refuses.

    One at a time, deliberately: `assemble_catalogs` refuses the whole set on
    the first collision, and a single bad plugin must not make the other twelve
    unavailable. Each rejection carries the refusal message, which names both
    sides.
    """
    accepted: list[PluginCatalog] = []
    rejected: list[dict[str, str]] = []
    for plugin in plugins:
        try:
            assemble_catalogs([*accepted, plugin])
        except CatalogCollisionError as exc:
            rejected.append({"plugin": plugin.name, "reason": str(exc)})
            _log.warning(
                "catalog.plugin_rejected",
                plugin=plugin.name,
                catalog_id=plugin.catalog_id,
                reason=str(exc),
            )
            continue
        accepted.append(plugin)
    return assemble_catalogs(accepted), rejected


@router.get("/{project_id}/catalogs")
async def list_catalogs(project_id: ProjectScopeDep, session: SessionDep) -> dict[str, Any]:
    """The catalog array, the chat catalog, and everything that was refused."""
    plugins = await enabled_plugin_catalogs(session, project_id)
    catalogs, rejected = assemble_or_reject(plugins)
    accepted = [p for p in plugins if p.catalog_id in {c.catalog_id for c in catalogs if c.plugin}]
    chat = merge_for_chat(accepted)
    return {
        "catalogs": [_describe(c) for c in catalogs],
        # `aleph://v1` is core under its old name. The copilot-runtime bridge
        # (`defaultCatalogId`) and `aleph_a2ui.components.surfaces` both still
        # stamp it, so the client registers it as a second catalog over the same
        # components rather than as a second definition of one. Renaming an id
        # in one process and not the other is how a live surface starts
        # answering `Catalog not found`.
        "aliases": {LEGACY_CORE_CATALOG_ID: CORE_CATALOG_ID},
        "chat": {
            "catalogId": chat.catalog_id,
            "components": list(chat.components),
            "functions": list(chat.functions),
            # Non-empty means two plugins claimed one name and BOTH were dropped
            # from chat. Shown, not swallowed: chat rendering one plugin's card
            # against another's data is the failure this whole workstream is
            # about.
            "collisions": [
                {"component": c.name, "claimants": list(c.claimants)} for c in chat.collisions
            ],
        },
        "rejected": rejected,
    }
