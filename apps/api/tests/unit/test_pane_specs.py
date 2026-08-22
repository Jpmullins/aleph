"""A pane's parameters arrive under the names the pane declared.

`_parse_pane_specs` read exactly one key, `page_id`, and passed it on as a bare
positional. The grounding pane declares `params=("claim_id",)` and had to
receive its claim id called `page_id` anyway — with an apologetic docstring at
the far end explaining that "the `page_id` pane param carries the CLAIM id
here".

One opaque parameter with the wrong name is survivable with one such pane. At
two it stops being: the Inspector needs `run_id`, and under the old parser it
would have received nothing and rendered "no run selected" for every run,
forever, with no error anywhere.
"""

from __future__ import annotations

import pytest

from aleph_api.routes.surfaces import _parse_pane_specs


def test_declared_params_are_parsed() -> None:
    specs = _parse_pane_specs("inspector:run_id=abc")
    assert specs == [("inspector:run_id=abc", "inspector", {"run_id": "abc"})]


def test_the_grounding_pane_gets_its_claim_id_by_name() -> None:
    """The migration the parser change had to do in the same commit.

    The plan's risk note names this exactly: generalizing the parser touches
    every existing pane, and grounding is the one whose param was mislabelled.
    Doing it in a later change would have broken grounding silently.
    """
    specs = _parse_pane_specs("grounding:claim_id=c1")
    assert specs[0][2] == {"claim_id": "c1"}


def test_an_undeclared_param_is_dropped_not_passed_through() -> None:
    """`PaneKind.params` is the contract, not a suggestion.

    A builder receiving a key it never declared is how a typo becomes a
    silently ignored filter — the reader sees unfiltered results and no error.
    """
    specs = _parse_pane_specs("inspector:run_id=abc&page_id=nope&colour=red")
    assert specs[0][2] == {"run_id": "abc"}


def test_a_param_a_different_pane_declares_is_still_dropped() -> None:
    """`claim_id` is declared — by grounding, not by inspector."""
    assert _parse_pane_specs("inspector:claim_id=c1")[0][2] == {}


@pytest.mark.parametrize(
    "spec",
    ["inspector:run_id=", "inspector:run_id", "inspector:", "inspector"],
)
def test_a_missing_value_yields_no_param_rather_than_an_empty_string(spec: str) -> None:
    """An empty run id must not select "the run whose id is empty"."""
    assert _parse_pane_specs(spec)[0][2] == {}


def test_an_unknown_pane_is_dropped_and_does_not_take_the_others_with_it() -> None:
    """One bad pane in a URL must not take down the whole workspace's stream."""
    specs = _parse_pane_specs("wiki,nonsense:x=1,inspector:run_id=r")
    assert [s[1] for s in specs] == ["wiki", "inspector"]


def test_the_surface_id_is_the_spec_verbatim() -> None:
    """The client mints the pane id from the spec, so a delta stamped with it
    lands in the right pane with no mapping in between."""
    specs = _parse_pane_specs("inspector:run_id=abc")
    assert specs[0][0] == "inspector:run_id=abc"


def test_the_inspector_is_a_pane_the_server_advertises() -> None:
    from aleph_a2ui.pane_registry import PANE_REGISTRY

    kinds = {k.id: k for k in PANE_REGISTRY.all()}
    assert "inspector" in kinds
    assert kinds["inspector"].launchable is True
    assert kinds["inspector"].params == ("run_id",)
