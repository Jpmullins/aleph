"""The surface-bindings sweep must model reality, not just run.

`scripts/check-surface-bindings.sh` is only worth having if it fires on the
defect it was written for and stays quiet otherwise. Its first run reported two
false positives — `GroundingSurface.claim` and `.groundings`, both declared as
`z3.any()` rather than `CommonSchemas.DynamicValue` — which is the shape of
mistake that gets a sweep switched off rather than fixed. These pin both
directions.
"""

from __future__ import annotations

from surface_bindings import client_props, compare, producer_props

PRODUCER = """
def wiki_surface_v09(*, pages, open_page=None, categories=None):
    component = {
        "id": "root",
        "component": "WikiSurface",
        "pages": {"path": "/pages"},
        "categories": {"path": "/categories"},
    }
    return full_surface(components=[component],
                        data_model={"pages": pages, "categories": categories})
"""

CLIENT_COMPLETE = """
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
    categories: CommonSchemas.DynamicValue.optional(),
  }),
};
"""

CLIENT_MISSING = """
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
  }),
};
"""


def test_a_bound_prop_with_no_client_declaration_is_reported() -> None:
    """The exact defect: the payload is correct and the view sees undefined."""
    found = compare(producer_props(PRODUCER), client_props(CLIENT_MISSING))
    assert [(m.component, m.prop) for m in found] == [("WikiSurface", "categories")]


def test_a_fully_declared_producer_is_clean() -> None:
    assert compare(producer_props(PRODUCER), client_props(CLIENT_COMPLETE)) == []


def test_the_data_model_keys_are_not_mistaken_for_bindings() -> None:
    """`data_model={"pages": ...}` names the same words as the bindings.

    A regex over the source would count those too, so every producer would look
    like it bound every key it sends. The AST walk only accepts a key whose
    value is a `{"path": ...}` dict inside a dict that also names a component.
    """
    props = producer_props(PRODUCER)
    assert props == {"WikiSurface": {"pages", "categories"}}


def test_a_zod_any_declaration_counts_as_declared() -> None:
    """The false positive from the sweep's first run.

    What matters is whether the binder was told about the prop, not which
    validator the author reached for.
    """
    client = """
export const GroundingSurfaceApi = {
  name: "GroundingSurface",
  schema: z3.object({
    claim: z3.any().optional(),
    groundings: z3.any().optional(),
  }),
};
"""
    producer = """
def grounding_surface(*, claim, groundings):
    component = {
        "id": "root",
        "component": "GroundingSurface",
        "claim": {"path": "/claim"},
        "groundings": {"path": "/groundings"},
    }
"""
    assert compare(producer_props(producer), client_props(client)) == []


def test_a_component_the_client_never_mentions_is_skipped() -> None:
    """Rendered elsewhere. Reporting it would train people to ignore the sweep."""
    assert compare({"SomethingElse": {"x"}}, client_props(CLIENT_COMPLETE)) == []


def test_the_real_tree_is_clean() -> None:
    """The sweep's own subject, so a regression fails here and not only in CI."""
    import pathlib

    from surface_bindings import run

    root = pathlib.Path(__file__).resolve().parents[2]
    assert run(root) == []
