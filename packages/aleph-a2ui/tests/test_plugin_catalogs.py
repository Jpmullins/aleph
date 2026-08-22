"""One catalog per plugin, merged without collisions. WS-A3b.

The defect these exist for is a plain dict assignment. A catalog is a
`name -> component` map; two plugins both defining `Chart` means the second one
merged replaces the first, the browser draws the wrong card against the right
data, and nothing raises anywhere. It is invisible in review, invisible in the
type checker, and invisible at run time.

Every assertion here is against `aleph_a2ui.plugin_catalogs` — the function the
route and the capability layer both call — never against a fixture of its
shape.
"""

from __future__ import annotations

import pytest

from aleph_a2ui.catalog import CATALOG_ID
from aleph_a2ui.components.surfaces import ALEPH_V09_CATALOG_ID
from aleph_a2ui.plugin_catalogs import (
    CORE_CATALOG_ID,
    LEGACY_CORE_CATALOG_ID,
    CatalogCollisionError,
    PluginCatalog,
    assemble_catalogs,
    catalog_capability_key,
    core_component_names,
    core_function_names,
    merge_for_chat,
    plugin_catalog_from_provides,
    plugin_catalog_id,
)


def test_two_plugins_may_share_a_component_name() -> None:
    """The criterion. Chart and Chart coexist, and each catalog keeps its own.

    Not "no exception was raised" — that would pass if the second plugin's
    catalog were silently dropped. The assertion is that BOTH catalogs are in
    the array, both resolve `Chart`, and their ids differ, which is the only
    reason the renderer can tell them apart.
    """
    atlas = PluginCatalog(name="atlas", components=("Chart", "Timeline"))
    beta = PluginCatalog(name="beta", components=("Chart",))

    catalogs = assemble_catalogs([atlas, beta])
    by_id = {c.catalog_id: c for c in catalogs}

    assert set(by_id) == {
        CORE_CATALOG_ID,
        "aleph://plugin/atlas@1",
        "aleph://plugin/beta@1",
    }
    assert by_id["aleph://plugin/atlas@1"].owns("Chart")
    assert by_id["aleph://plugin/beta@1"].owns("Chart")
    # Isolation, stated as the thing that would break: atlas's Timeline must
    # NOT leak into beta's catalog. A merge that ignored ids would put it there.
    assert by_id["aleph://plugin/atlas@1"].owns("Timeline")
    assert not by_id["aleph://plugin/beta@1"].owns("Timeline")
    # Core is untouched by either.
    assert not by_id[CORE_CATALOG_ID].owns("Chart")


def test_a_plugin_cannot_shadow_a_core_component() -> None:
    """The refusal names BOTH sides: which plugin, and which core component.

    "duplicate component name" would be a true message and a useless one — the
    author would have to diff two catalogs by hand to find out what they hit.
    """
    with pytest.raises(CatalogCollisionError) as exc:
        assemble_catalogs([PluginCatalog(name="atlas", components=("ClaimCard",))])

    message = str(exc.value)
    assert "atlas" in message
    assert "ClaimCard" in message
    assert CORE_CATALOG_ID in message
    assert exc.value.left == "aleph://plugin/atlas@1"
    assert exc.value.right == CORE_CATALOG_ID
    assert exc.value.names == ("ClaimCard",)


def test_a_plugin_cannot_shadow_a_core_function() -> None:
    """A shadowed FUNCTION is the same defect and a quieter one.

    `formatDate` resolving to a plugin's implementation throws
    `Function not found in catalog` in one renderer and works in another — the
    exact split that `scripts/check-single-catalog.sh` was written after.
    """
    with pytest.raises(CatalogCollisionError) as exc:
        assemble_catalogs([PluginCatalog(name="atlas", functions=("formatDate",))])
    assert "atlas" in str(exc.value)
    assert "formatDate" in str(exc.value)


def test_core_is_first_and_carries_everything_the_browser_can_draw() -> None:
    core = assemble_catalogs()[0]
    assert core.catalog_id == CORE_CATALOG_ID
    assert core.source == "core"
    assert set(core.components) == core_component_names()
    assert set(core.functions) == core_function_names()
    # The floor the capability probe checks. If these ever leave the renderer,
    # a catalog that cannot paint anything would still come up ACTIVE.
    assert {"Column", "Text"} <= set(core.components)
    # Named, not counted, and not "== core_function_names()" — that comparison
    # is a tautology when the extraction is empty, and an empty extraction is
    # exactly what would turn the function-shadow refusal off without a word.
    assert {"formatDate", "equals", "openUrl"} <= set(core.functions)


def test_a_plugin_catalog_carries_core_as_well_as_its_own() -> None:
    """A plugin catalog holds core plus its own.

    A plugin's surface still needs `Column` and `Text` to lay itself out; a
    catalog holding only the plugin's own components would render nothing and
    the failure would arrive as `Component not found` at paint time.
    """
    catalogs = assemble_catalogs([PluginCatalog(name="atlas", components=("Chart",))])
    plugin = catalogs[1]
    assert core_component_names() <= set(plugin.components)
    assert plugin.owns("Chart")
    assert plugin.plugin == "atlas"
    assert plugin.major == 1


def test_two_majors_of_one_plugin_coexist() -> None:
    """`@1` and `@2` are different strings, therefore different catalogs.

    This is the whole reason the major is in the id: a surface created before
    an upgrade keeps painting against the catalog it named. Without it, an
    upgrade is a destructive replace of every live surface.
    """
    catalogs = assemble_catalogs(
        [
            PluginCatalog(name="atlas", major=1, components=("Chart",)),
            PluginCatalog(name="atlas", major=2, components=("Chart", "Chart3D")),
        ]
    )
    ids = [c.catalog_id for c in catalogs]
    assert ids == [CORE_CATALOG_ID, "aleph://plugin/atlas@1", "aleph://plugin/atlas@2"]
    assert not catalogs[1].owns("Chart3D")
    assert catalogs[2].owns("Chart3D")


def test_two_plugins_claiming_one_catalog_id_are_refused() -> None:
    """Same name, same major. One would silently win the array lookup."""
    with pytest.raises(CatalogCollisionError) as exc:
        assemble_catalogs(
            [
                PluginCatalog(name="atlas", major=1, components=("Chart",)),
                PluginCatalog(name="atlas", major=1, components=("Table",)),
            ]
        )
    assert "aleph://plugin/atlas@1" in str(exc.value)


@pytest.mark.parametrize("bad", ["at/las", "atlas@2", "Atlas", "", "at las"])
def test_a_plugin_name_that_could_forge_an_id_is_refused(bad: str) -> None:
    """`a/b` would produce `aleph://plugin/a/b@1`, which parses as someone else."""
    with pytest.raises(ValueError, match="catalog id"):
        PluginCatalog(name=bad)


def test_the_chat_merge_drops_a_contested_name_and_names_both_claimants() -> None:
    """Chat takes ONE catalog, so the array has to be flattened — and a flatten
    is exactly where the silent overwrite the per-plugin ids removed comes back.

    Neither claimant wins. Picking one by order is the original defect wearing a
    policy: a chat that renders atlas's Chart against beta's data is worse than
    a chat that cannot render Chart and reports why.
    """
    merged = merge_for_chat(
        [
            PluginCatalog(name="atlas", components=("Chart", "Timeline")),
            PluginCatalog(name="beta", components=("Chart",)),
        ]
    )
    assert "Chart" not in merged.components
    assert "Timeline" in merged.components  # uncontested, still available
    assert core_component_names() <= set(merged.components)

    assert [c.name for c in merged.collisions] == ["Chart"]
    assert merged.collisions[0].claimants == (
        "aleph://plugin/atlas@1",
        "aleph://plugin/beta@1",
    )


def test_the_chat_merge_runs_the_core_shadow_check_too() -> None:
    """Isolation that holds in panes and fails in chat is not isolation."""
    with pytest.raises(CatalogCollisionError):
        merge_for_chat([PluginCatalog(name="atlas", components=("ClaimCard",))])


def test_the_chat_merge_of_nothing_is_exactly_core() -> None:
    merged = merge_for_chat([])
    assert merged.catalog_id == LEGACY_CORE_CATALOG_ID
    assert set(merged.components) == core_component_names()
    assert merged.collisions == ()


def test_a_plugins_catalog_is_read_from_its_provides_column() -> None:
    """Derived from the stored row, not from a second list beside it.

    `Plugin.provides` is stored as written precisely so a plugin whose
    declaration changed underneath it fails loudly instead of mounting against
    a different graph.
    """
    catalog = plugin_catalog_from_provides(
        "atlas",
        2,
        ["skill.atlas", "ui:component:Chart", "ui:function:sparkline", "ui:component:Chart"],
    )
    assert catalog.catalog_id == "aleph://plugin/atlas@2"
    assert catalog.components == ("Chart",)
    assert catalog.functions == ("sparkline",)


def test_the_capability_key_is_derived_from_the_id() -> None:
    assert catalog_capability_key(plugin_catalog_id("atlas", 3)) == (
        "ui:catalog:aleph://plugin/atlas@3"
    )


def test_every_catalog_id_python_stamps_is_one_a_client_can_resolve() -> None:
    """The producers and the registered set must name the same catalogs.

    This is not hypothetical. `catalog.json` carried `catalogId: "aleph-v1"`,
    `a2ui_handlers.connector_settings_surface` stamped it onto every generated
    settings screen, and no client has ever declared a catalog by that name —
    `MessageProcessor` raises `Catalog not found` and the pane dies. The
    mismatch sat in `docs/research/a2ui.md:477` as an observation and in the
    code as a live defect.
    """
    registered = {c.catalog_id for c in assemble_catalogs()} | {LEGACY_CORE_CATALOG_ID}
    assert CATALOG_ID in registered
    assert ALEPH_V09_CATALOG_ID in registered
