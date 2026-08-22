"""A catalog is a capability, and a pinned pane holds it up. WS-A3b.

The isolation this workstream buys — one catalog per plugin, so two plugins can
each define `Chart` — is worth nothing if an agent can disable the plugin whose
catalog a pane on screen is painting with. The kernel already refuses a
deactivation whose blast radius is non-empty; what was missing was the
declaration that makes a pane part of that radius.

So a catalog capability `provides` `ui:catalog:<id>`, a pinned pane `requires`
it, and `disable` comes back refused naming the pane — computed by the same
support-set walk that protects the ledger, not by a policy check written beside
`disable`.

**Honest note on the marker.** `test_a_pinned_pane_protects_its_catalog` and
its neighbours need no services: they are a kernel graph and nothing else. They
live here, under the `integration` marker, because the criterion for this
workstream names this path, and because the other half of the file — the
catalogs the route derives from real `plugins` rows — genuinely needs Postgres
and belongs beside them. Read a green run of the kernel tests as "the graph
refuses", not as "the stack was exercised".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.catalog_capability import (
    PinnedPane,
    catalog_capability,
    pane_capability,
    pane_capability_name,
)
from aleph_a2ui.plugin_catalogs import (
    CORE_CATALOG_ID,
    AssembledCatalog,
    CatalogCollisionError,
    PluginCatalog,
    assemble_catalogs,
    catalog_capability_key,
)
from aleph_api.routes.catalogs import assemble_or_reject, enabled_plugin_catalogs
from aleph_db.repos.ledger import LedgerWriter
from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.errors import DependentsWouldBreak, ProbeFailed
from aleph_kernel.kernel import Kernel
from aleph_runtime.plugin_service import PluginDraft, PluginService

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000a3")

INSTRUCTIONS = """\
---
name: atlas
description: Draws things.
---

Draw the thing.
"""


def _catalogs_for(*plugins: PluginCatalog) -> dict[str, AssembledCatalog]:
    return {c.catalog_id: c for c in assemble_catalogs(list(plugins))}


async def _mount(kernel: Kernel, catalog: AssembledCatalog) -> object:
    """Register + activate a catalog capability, returning its plugin id."""
    plugin_id = kernel.register_dynamic(catalog_capability(catalog))
    await kernel.activate(catalog_capability_key(catalog.catalog_id))
    return plugin_id


async def test_a_pinned_pane_protects_its_catalog() -> None:
    """Pin a pane on `aleph://plugin/atlas@1`, then try to disable atlas.

    The refusal must NAME the pane. "cannot disable, something depends on it"
    is a refusal an agent cannot act on: it will try the next id, and a
    guardrail that cannot be predicted is indistinguishable from a broken tool.
    """
    catalogs = _catalogs_for(PluginCatalog(name="atlas", components=("Chart",)))
    atlas = catalogs["aleph://plugin/atlas@1"]

    kernel = Kernel()
    agent = AgentPluginAPI(kernel)
    plugin_id = await _mount(kernel, atlas)

    pane = PinnedPane(
        pane_id="atlas-board",
        catalog_ids=(atlas.catalog_id,),
        components=("Chart", "Column"),
    )
    kernel.register_dynamic(pane_capability(pane))
    await kernel.activate(pane_capability_name("atlas-board"))

    # The kernel's own refusal, raised.
    with pytest.raises(DependentsWouldBreak) as exc:
        await kernel.deactivate(plugin_id)  # type: ignore[arg-type]
    assert pane_capability_name("atlas-board") in str(exc.value)

    # And what the AGENT sees, which is the surface that matters: `disable`
    # converts the exception into a refusal outcome rather than raising, and
    # the pane has to be named there too or the agent learns nothing.
    outcome = await agent.disable(plugin_id)  # type: ignore[arg-type]
    assert outcome.installed is True
    assert "refused" in outcome.detail
    assert pane_capability_name("atlas-board") in outcome.detail
    assert kernel.state_of(catalog_capability_key(atlas.catalog_id)).value == "active"


async def test_an_unpinned_catalog_can_be_disabled() -> None:
    """The control. Without it the test above passes for a refusal that is
    unconditional, which would be a guardrail that is really an outage.
    """
    catalogs = _catalogs_for(PluginCatalog(name="atlas", components=("Chart",)))
    atlas = catalogs["aleph://plugin/atlas@1"]

    kernel = Kernel()
    agent = AgentPluginAPI(kernel)
    plugin_id = await _mount(kernel, atlas)

    outcome = await agent.disable(plugin_id)  # type: ignore[arg-type]
    assert outcome.installed is False
    assert "refused" not in outcome.detail


async def test_disabling_one_plugins_catalog_leaves_the_others_painting() -> None:
    """Isolation, at the lifecycle level. Two plugins, both defining `Chart`;
    disabling one must not disturb the pane pinned to the other.
    """
    catalogs = _catalogs_for(
        PluginCatalog(name="atlas", components=("Chart",)),
        PluginCatalog(name="beta", components=("Chart",)),
    )
    kernel = Kernel()
    agent = AgentPluginAPI(kernel)
    atlas_id = await _mount(kernel, catalogs["aleph://plugin/atlas@1"])
    await _mount(kernel, catalogs["aleph://plugin/beta@1"])

    beta_pane = PinnedPane(
        pane_id="beta-board", catalog_ids=("aleph://plugin/beta@1",), components=("Chart",)
    )
    kernel.register_dynamic(pane_capability(beta_pane))
    await kernel.activate(pane_capability_name("beta-board"))

    outcome = await agent.disable(atlas_id)  # type: ignore[arg-type]
    assert outcome.installed is False, outcome.detail
    assert kernel.state_of(pane_capability_name("beta-board")).value == "active"
    assert kernel.state_of(catalog_capability_key("aleph://plugin/beta@1")).value == "active"


async def test_a_pane_pinned_to_a_catalog_that_cannot_draw_it_never_comes_up() -> None:
    """The probe is a read of the catalog, not a report that setup ran.

    A pane wired to the wrong catalog renders an empty rectangle and nothing
    raises. Here it is refused at activation, naming the component.
    """
    catalogs = _catalogs_for(PluginCatalog(name="atlas", components=("Chart",)))
    kernel = Kernel()
    await _mount(kernel, catalogs["aleph://plugin/atlas@1"])

    pane = PinnedPane(
        pane_id="wrong", catalog_ids=("aleph://plugin/atlas@1",), components=("Sparkline",)
    )
    kernel.register_dynamic(pane_capability(pane))
    with pytest.raises(ProbeFailed) as exc:
        await kernel.activate(pane_capability_name("wrong"))
    assert "Sparkline" in str(exc.value)


async def test_a_pane_cannot_read_a_catalog_it_did_not_declare() -> None:
    """`requires` is enforced by `Context.get`, so it cannot rot into a comment.

    Built by hand rather than through `pane_capability` on purpose: this pins
    the KERNEL's behaviour, which is what makes the declaration in
    `pane_capability` worth writing.
    """
    from aleph_kernel.spec import CapabilitySpec, ok

    catalogs = _catalogs_for(PluginCatalog(name="atlas", components=("Chart",)))
    kernel = Kernel()
    await _mount(kernel, catalogs["aleph://plugin/atlas@1"])

    async def setup(ctx: object):  # type: ignore[no-untyped-def]
        if False:  # pragma: no cover
            yield

    async def probe(ctx):  # type: ignore[no-untyped-def]
        ctx.get(catalog_capability_key("aleph://plugin/atlas@1"))
        return ok()

    kernel.register_dynamic(
        CapabilitySpec(name="ui:pane:sneaky", setup=setup, probe=probe, requires=frozenset())
    )
    with pytest.raises(ProbeFailed) as exc:
        await kernel.activate("ui:pane:sneaky")
    assert "UndeclaredAccess" in str(exc.value)


async def test_a_shadowing_plugin_never_reaches_the_kernel() -> None:
    """Assembly refuses first, so there is no capability to mount and nothing
    to unwind. The graph never sees a plugin that would have overwritten core.
    """
    kernel = Kernel()
    await _mount(kernel, _catalogs_for()[CORE_CATALOG_ID])
    before = set(kernel.active())

    with pytest.raises(CatalogCollisionError):
        _catalogs_for(PluginCatalog(name="shadow", components=("ClaimCard",)))

    assert set(kernel.active()) == before


async def test_the_catalog_array_is_built_from_real_plugin_rows(
    session: AsyncSession,
) -> None:
    """The route's source of truth is the `plugins` table, end to end.

    Postgres is the point here: `provides` is a JSONB column and the catalog a
    plugin gets is read out of it, so a round trip through the database is the
    only way to know the derivation survives storage. Uses the rolled-back
    `session` fixture — nothing here reaches disk.
    """
    project_id = uuid.uuid4()
    service = PluginService(session)
    ledger = LedgerWriter(session)

    await service.install(
        project_id=project_id,
        actor_id=ACTOR,
        draft=PluginDraft(
            name="atlas",
            instructions=INSTRUCTIONS,
            provides=("skill.atlas", "ui:component:Chart"),
        ),
        ledger=ledger,
    )
    await session.flush()

    plugins = await enabled_plugin_catalogs(session, project_id)
    assert [p.catalog_id for p in plugins] == ["aleph://plugin/atlas@1"]
    assert plugins[0].components == ("Chart",)

    catalogs, rejected = assemble_or_reject(plugins)
    assert rejected == []
    assert [c.catalog_id for c in catalogs] == [CORE_CATALOG_ID, "aleph://plugin/atlas@1"]
    assert catalogs[1].owns("Chart")


async def test_a_disabled_plugin_loses_its_catalog(
    session: AsyncSession,
) -> None:
    """Turning a plugin off has to remove its catalog, or its surfaces keep
    painting against a definition nothing is running.
    """
    project_id = uuid.uuid4()
    service = PluginService(session)
    ledger = LedgerWriter(session)

    await service.install(
        project_id=project_id,
        actor_id=ACTOR,
        draft=PluginDraft(
            name="atlas", instructions=INSTRUCTIONS, provides=("ui:component:Chart",)
        ),
        ledger=ledger,
    )
    await session.flush()
    assert await enabled_plugin_catalogs(session, project_id)

    await service.disable(project_id=project_id, actor_id=ACTOR, name="atlas", ledger=ledger)
    await session.flush()
    assert await enabled_plugin_catalogs(session, project_id) == []


async def test_a_shadowing_plugin_row_is_dropped_and_named(
    session: AsyncSession,
) -> None:
    """One bad row must not take the workspace's catalogs down with it.

    A 500 here would mean a single agent-authored plugin makes every pane in
    the project unrenderable; a silent drop would be the overwrite this
    workstream exists to prevent, moved one layer out.

    **The row is inserted directly, not installed.** `PluginService.install`
    refuses a shadowing plugin outright now, so it cannot produce this state —
    and that is the right place for the refusal. This check remains
    load-bearing for the case the install gate structurally cannot cover: a
    plugin installs cleanly today defining `Foo`, and core gains a component
    called `Foo` in a later release. The gate ran when the plugin was
    admissible; nothing re-runs it, and the collision arrives without anybody
    installing anything.
    """
    from aleph_core.ids import uuid7
    from aleph_db.models.plugin import Plugin

    project_id = uuid.uuid4()
    service = PluginService(session)
    ledger = LedgerWriter(session)

    await service.install(
        project_id=project_id,
        actor_id=ACTOR,
        draft=PluginDraft(
            name="atlas",
            instructions=INSTRUCTIONS,
            provides=("ui:component:Chart",),
        ),
        ledger=ledger,
    )
    # The plugin core grew into. Written as a row because that is how it
    # exists: admissible when installed, colliding now.
    session.add(
        Plugin(
            id=uuid7(),
            project_id=project_id,
            name="shadow",
            major_version=1,
            source_kind="skill",
            instructions=INSTRUCTIONS.replace("atlas", "shadow"),
            code="",
            provides=["ui:component:ClaimCard"],
            requires=[],
            state="installed",
            installed_by=ACTOR,
            created_by=ACTOR,
        )
    )
    await session.flush()

    catalogs, rejected = assemble_or_reject(await enabled_plugin_catalogs(session, project_id))
    assert [c.catalog_id for c in catalogs] == [CORE_CATALOG_ID, "aleph://plugin/atlas@1"]
    assert [r["plugin"] for r in rejected] == ["shadow"]
    assert "ClaimCard" in rejected[0]["reason"]
    assert "shadow" in rejected[0]["reason"]
