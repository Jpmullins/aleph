"""One catalog per plugin, merged without collisions.

A catalog is a `name -> component` map, and the renderer resolves a component
by looking its name up in the catalog the surface named. That is the whole
mechanism, and it is also the whole hazard: if two plugins both define
something called ``Chart``, the second one written into the map replaces the
first, the browser draws the wrong card against the right data, and nothing
anywhere raises. A plain dict assignment is not a failure mode you can notice.

**The drawing protocol already has the answer.** ``MessageProcessor`` takes a
LIST of catalogs, each with an id, and ``createSurface`` names the one it wants
(`@a2ui/web_core/v0_9/processing/message-processor.js:210` — an unknown id is
`Catalog not found`, an exception, not a shrug). Aleph was already on that code
path and passed a list of exactly one. Two plugins can therefore each define
``Chart`` for free, provided the catalogs they live in have different ids.

**The naming convention.**

* ``aleph://core@1`` — the human-owned set. Every component the browser can
  draw, which is what `render_catalog.generated.json` is extracted from.
* ``aleph://plugin/<name>@<major>`` — one per plugin, holding core plus its own.
* ``aleph://v1`` — the legacy alias of core, kept because the copilot-runtime
  bridge and the surface streamer both still stamp it. It is registered as a
  second catalog over the same components, not as a second definition of one.

Putting the MAJOR in the id is the load-bearing part of the convention and it
is one line's worth of design: ``@1`` and ``@2`` are different strings,
therefore different catalogs, therefore they coexist in the same processor
array. A surface created before an upgrade keeps painting against the catalog
it named, with no migration and no version negotiation. Drop the ``@<major>``
and an upgrade becomes a destructive replace of every live surface — which is
also why `Plugin.__table_args__` puts `major_version` in its unique key.

**The collision check.** :func:`assemble_catalogs` refuses a plugin whose
component or function names intersect core's, naming both sides. Fifteen lines
that turn a silent map overwrite into a rejected install, which is the entire
value of this module. Two plugins colliding with *each other* are fine here —
they are in different catalogs — and are a real problem exactly once, in chat,
where the renderer accepts one catalog and the array has to be flattened. See
:func:`merge_for_chat`, which is where per-plugin isolation would otherwise
hold in panes and quietly fail in chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aleph_a2ui.catalog import _RENDER, _RENDER_FUNCTIONS

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "CATALOG_KEY_PREFIX",
    "CORE_CATALOG_ID",
    "LEGACY_CORE_CATALOG_ID",
    "AssembledCatalog",
    "CatalogCollision",
    "CatalogCollisionError",
    "ChatCatalog",
    "PluginCatalog",
    "assemble_catalogs",
    "catalog_capability_key",
    "core_component_names",
    "core_function_names",
    "merge_for_chat",
    "plugin_catalog_from_provides",
    "plugin_catalog_id",
]

#: The human-owned set. Everything the browser can draw.
CORE_CATALOG_ID: Final[str] = "aleph://core@1"

#: What core was called before the convention existed. Still stamped by
#: `apps/copilot-runtime/src/server.ts` (`defaultCatalogId`) and by
#: `aleph_a2ui.components.surfaces`, so the client registers it as an ALIAS
#: catalog over the same components. Renaming an id in one process and not the
#: other is how every live surface starts answering `Catalog not found`.
LEGACY_CORE_CATALOG_ID: Final[str] = "aleph://v1"

#: How a catalog appears in the kernel's declaration graph. A catalog is a
#: capability: it `provides` this key, and a pane that paints against it
#: `requires` the same key — which is what makes `disable(plugin)` refusable
#: rather than merely regrettable. See `aleph_a2ui.catalog_capability`.
CATALOG_KEY_PREFIX: Final[str] = "ui:catalog:"

#: A plugin declares a component it contributes as `ui:component:<Name>` and a
#: function as `ui:function:<name>` in its `provides` column. Read rather than
#: invented: `Plugin.provides` is stored as written, so the catalog a plugin
#: gets is derived from the row instead of from a second, drifting list.
COMPONENT_PROVIDE_PREFIX: Final[str] = "ui:component:"
FUNCTION_PROVIDE_PREFIX: Final[str] = "ui:function:"

#: A plugin name goes into a URI. Anything outside this set could forge another
#: plugin's id — `a/b` would make `aleph://plugin/a/b@1`, and a name containing
#: `@` could claim a different major. Refused rather than escaped, because an
#: escaped id is one nobody can read in a log line.
_PLUGIN_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")

#: A catalog with none of these cannot paint a surface at all: every surface
#: tree Aleph emits is a `Column` of `Text` and cards. Used by the capability
#: probe, so "this catalog is usable" is checked against something real rather
#: than against "the dict is non-empty".
MINIMUM_RENDERABLE: Final[frozenset[str]] = frozenset({"Column", "Text"})


class CatalogCollisionError(Exception):
    """Two catalogs would claim one name. Refused, naming both sides.

    Both sides, always. "duplicate component" tells an author nothing: the
    question they need answered is *which* of their components hit *whose*, and
    an error that omits either half sends them reading two catalogs by hand.
    """

    def __init__(self, message: str, *, left: str, right: str, names: tuple[str, ...]) -> None:
        super().__init__(message)
        self.left = left
        self.right = right
        self.names = names


@dataclass(frozen=True)
class CatalogCollision:
    """Two plugin catalogs claiming one name in the FLATTENED chat catalog."""

    name: str
    #: Every catalog id that defines it, sorted. Two or more, by construction.
    claimants: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.name} is defined by {' and '.join(self.claimants)}"


@dataclass(frozen=True)
class PluginCatalog:
    """What one plugin contributes, before it is merged with core."""

    name: str
    major: int = 1
    #: Component names the plugin defines. Names only: the browser holds the
    #: implementations, and a schema shipped from the server that no renderer
    #: can draw would be a contract with no consumer.
    components: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _PLUGIN_NAME_RE.match(self.name):
            msg = (
                f"plugin name {self.name!r} cannot go in a catalog id. Use lowercase "
                f"letters, digits, '-' and '_' — a name with '/' or '@' in it could "
                f"forge another plugin's catalog id."
            )
            raise ValueError(msg)
        if self.major < 1:
            msg = f"plugin {self.name!r} has major version {self.major}; majors start at 1"
            raise ValueError(msg)

    @property
    def catalog_id(self) -> str:
        return plugin_catalog_id(self.name, self.major)


@dataclass(frozen=True)
class AssembledCatalog:
    """One entry of the array the renderer is handed."""

    catalog_id: str
    #: Everything resolvable in this catalog, sorted. For a plugin catalog this
    #: is core plus its own, because a plugin's surface still needs `Column`.
    components: tuple[str, ...]
    functions: tuple[str, ...]
    #: "core" or "plugin".
    source: str
    #: None for core.
    plugin: str | None = None
    major: int | None = None

    @property
    def capability_key(self) -> str:
        return catalog_capability_key(self.catalog_id)

    def owns(self, name: str) -> bool:
        return name in self.components


@dataclass(frozen=True)
class ChatCatalog:
    """The one flat catalog the chat renderer accepts, and what it cost."""

    catalog_id: str
    components: tuple[str, ...]
    functions: tuple[str, ...]
    #: Names dropped because more than one plugin claimed them. Empty is the
    #: normal case; non-empty is a thing to show a person, not to swallow.
    collisions: tuple[CatalogCollision, ...] = ()


def plugin_catalog_id(name: str, major: int = 1) -> str:
    return f"aleph://plugin/{name}@{major}"


def catalog_capability_key(catalog_id: str) -> str:
    return f"{CATALOG_KEY_PREFIX}{catalog_id}"


def core_component_names() -> frozenset[str]:
    """Every component the browser can draw.

    Read from the extraction of the live renderer rather than from a list kept
    beside it: `check-agent-catalog-covers-renderer.sh` exists because a
    hand-kept second list is how the agent came to be told about 19 of 39
    components with nothing reporting a problem.
    """
    return frozenset(_RENDER["components"])


def core_function_names() -> frozenset[str]:
    """Every function the browser's catalog can invoke (`formatDate`, ...)."""
    return frozenset(_RENDER_FUNCTIONS)


def plugin_catalog_from_provides(name: str, major: int, provides: Iterable[str]) -> PluginCatalog:
    """Read a plugin's catalog contribution out of its `provides` column."""
    components = sorted(
        {
            p[len(COMPONENT_PROVIDE_PREFIX) :]
            for p in provides
            if p.startswith(COMPONENT_PROVIDE_PREFIX)
        }
    )
    functions = sorted(
        {
            p[len(FUNCTION_PROVIDE_PREFIX) :]
            for p in provides
            if p.startswith(FUNCTION_PROVIDE_PREFIX)
        }
    )
    return PluginCatalog(
        name=name, major=major, components=tuple(components), functions=tuple(functions)
    )


def assemble_catalogs(
    plugins: Sequence[PluginCatalog] = (),
    *,
    core_components: frozenset[str] | None = None,
    core_functions: frozenset[str] | None = None,
) -> list[AssembledCatalog]:
    """Core first, then one catalog per plugin. Refuses rather than overwrites.

    Two refusals, and they are different failures:

    * A plugin whose component or function name is already core's. Core wins by
      definition — it is what every surface in the product paints with — so the
      plugin is refused at assembly, naming both sides. This is the check that
      turns a silent map overwrite into a rejected install.
    * Two plugins producing the same catalog id (same name, same major). One
      would replace the other in the array and `createSurface` would resolve to
      whichever came last, which is the same defect one level up.

    Two plugins each defining ``Chart`` is NOT refused. They are in different
    catalogs, the surface names which one it means, and refusing that would
    make the first plugin to claim a common word own it forever.
    """
    core_names = core_component_names() if core_components is None else core_components
    core_funcs = core_function_names() if core_functions is None else core_functions

    out: list[AssembledCatalog] = [
        AssembledCatalog(
            catalog_id=CORE_CATALOG_ID,
            components=tuple(sorted(core_names)),
            functions=tuple(sorted(core_funcs)),
            source="core",
        )
    ]

    seen: dict[str, str] = {}
    for plugin in plugins:
        shadowed = tuple(sorted(set(plugin.components) & core_names))
        shadowed_funcs = tuple(sorted(set(plugin.functions) & core_funcs))
        if shadowed or shadowed_funcs:
            what = "component" if shadowed else "function"
            names = shadowed or shadowed_funcs
            msg = (
                f"plugin {plugin.name!r} ({plugin.catalog_id}) defines "
                f"{what}(s) {', '.join(names)}, which {CORE_CATALOG_ID} already defines. "
                f"A catalog is a name-to-component map: the second write would replace "
                f"the first and nothing would report it. Rename the plugin's {what}."
            )
            raise CatalogCollisionError(
                msg, left=plugin.catalog_id, right=CORE_CATALOG_ID, names=names
            )
        if plugin.catalog_id in seen:
            msg = (
                f"two plugins claim the catalog id {plugin.catalog_id}: "
                f"{seen[plugin.catalog_id]!r} and {plugin.name!r}. `createSurface` "
                f"resolves an id to exactly one catalog, "
                f"so the second registration would silently win. Bump one plugin's major."
            )
            raise CatalogCollisionError(
                msg,
                left=plugin.catalog_id,
                right=plugin.catalog_id,
                names=(plugin.catalog_id,),
            )
        seen[plugin.catalog_id] = plugin.name
        out.append(
            AssembledCatalog(
                catalog_id=plugin.catalog_id,
                components=tuple(sorted(core_names | set(plugin.components))),
                functions=tuple(sorted(core_funcs | set(plugin.functions))),
                source="plugin",
                plugin=plugin.name,
                major=plugin.major,
            )
        )
    return out


def merge_for_chat(
    plugins: Sequence[PluginCatalog] = (),
    *,
    catalog_id: str = LEGACY_CORE_CATALOG_ID,
    core_components: frozenset[str] | None = None,
    core_functions: frozenset[str] | None = None,
) -> ChatCatalog:
    """Flatten the array into the ONE catalog the chat renderer accepts.

    `createA2UIMessageRenderer({ catalog })` takes a single catalog
    (`@copilotkit/react-core` `A2UIMessageRendererOptions.catalog`), so chat is
    a merge, and a merge is precisely where the silent-overwrite hazard the
    per-plugin ids removed comes back. Isolation that holds in panes and fails
    in chat is not isolation.

    So the merge runs the collision check too, and when two plugins claim one
    name **neither wins**. Dropping both is deliberate: picking a winner by
    order is the original defect wearing a policy, and a chat that renders
    plugin A's Chart against plugin B's data is worse than a chat that cannot
    render Chart and says so. The dropped name and both claimants come back on
    the result so a caller can show it rather than swallow it.

    Core is never a claimant, because :func:`assemble_catalogs` has already
    refused any plugin that shadows it.
    """
    core_names = core_component_names() if core_components is None else core_components
    core_funcs = core_function_names() if core_functions is None else core_functions

    # Runs the core-shadow and duplicate-id refusals before anything is merged.
    assemble_catalogs(plugins, core_components=core_names, core_functions=core_funcs)

    claims: dict[str, list[str]] = {}
    func_claims: dict[str, list[str]] = {}
    for plugin in plugins:
        for name in plugin.components:
            claims.setdefault(name, []).append(plugin.catalog_id)
        for name in plugin.functions:
            func_claims.setdefault(name, []).append(plugin.catalog_id)

    collisions = tuple(
        sorted(
            (
                CatalogCollision(name=name, claimants=tuple(sorted(ids)))
                for name, ids in [*claims.items(), *func_claims.items()]
                if len(ids) > 1
            ),
            key=lambda c: c.name,
        )
    )
    contested = {c.name for c in collisions}

    components = core_names | {n for n in claims if n not in contested}
    functions = core_funcs | {n for n in func_claims if n not in contested}
    return ChatCatalog(
        catalog_id=catalog_id,
        components=tuple(sorted(components)),
        functions=tuple(sorted(functions)),
        collisions=collisions,
    )
