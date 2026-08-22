"""The surface-bindings sweep must model reality, not just run.

`scripts/check-surface-bindings.sh` is only worth having if it fires on the
defect it was written for and stays quiet otherwise. Its first run reported two
false positives — `GroundingSurface.claim` and `.groundings`, both declared as
`z3.any()` rather than `CommonSchemas.DynamicValue` — which is the shape of
mistake that gets a sweep switched off rather than fixed. These pin both
directions.
"""

from __future__ import annotations

import pathlib

import pytest
from surface_bindings import (
    catalog_components,
    client_props,
    compare,
    producer_props,
    run,
)
from sweep_subject import MissingSubject

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
    root = pathlib.Path(__file__).resolve().parents[2]
    assert run(root).mismatches == []


def test_a_moved_subject_file_raises_instead_of_reporting_clean(
    tmp_path: pathlib.Path,
) -> None:
    """The fail-open that made this sweep worth re-reading.

    `run()` used to return `[]` when either subject was missing. It looked safe
    only because the wrapper re-read the same path a few lines later and died on
    an unhandled `FileNotFoundError` — so the *analyzer* was answering "no
    mismatches" about a file it had never opened, and reordering those two reads
    would have turned a moved producer into a silent pass. Three sweeps in this
    repo have already gone quiet by naming a path that moved.
    """
    with pytest.raises(MissingSubject) as caught:
        run(tmp_path)
    # The message must name the path, or the failure is "the sweep crashed"
    # rather than "your subject moved" — and the usual answer to the first is to
    # delete the sweep.
    assert "surfaces.py" in str(caught.value)


def test_the_coverage_denominator_is_the_canonical_catalog() -> None:
    """`5 components` reads as complete; `5 of N` reads as coverage.

    The denominator comes from `catalog.json` — the one editable copy — not from
    the client zod file, which is one of the two things that can drift.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    catalog = catalog_components(
        (root / "packages/aleph-a2ui/src/aleph_a2ui/catalog.json").read_text()
    )
    assert report.catalog_total == len(catalog)
    # Coverage is genuinely partial today: the card components are bound from
    # `cards.py`, which declares no `{"path": ...}` bindings at all. If this ever
    # becomes 0 the sweep has either grown to cover the cards (good, update this)
    # or lost its denominator (bad).
    assert report.uncompared > 0
    assert set(report.compared) <= catalog
