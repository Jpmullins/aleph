"""LangGraph drops undeclared state writes — these are the standing guard.

`test_no_undeclared_state_keys` sweeps every graph module statically.
`test_langgraph_really_drops_undeclared_keys` pins the upstream behaviour the
sweep exists to defend against, so the sweep can never be quietly deleted on the
theory that "LangGraph probably handles it".

`REGRESSION_CHANNELS` is the valuable part: four channels whose loss was a live
production defect, each driven through a real `StateGraph` built on the real
production state class, each asserting the consequence in words. Deleting a
declaration fails here with the effect spelled out rather than with a diff.

The analyzer itself lives in `scripts/_lib/graph_state_keys.py` so the CI sweep
does not have to import up into the test suite — that inversion is what broke
the build when `tests/` was deleted.
"""

from __future__ import annotations

import pathlib

import pytest
from graph_state_keys import (
    REPO_ROOT,
    _graph_modules,
    _violations,
)


@pytest.mark.parametrize("path", _graph_modules(), ids=lambda p: p.name)
def test_no_undeclared_state_keys(path: pathlib.Path) -> None:
    """A node may not write a channel its state schema does not declare."""
    missing = _violations(path)
    assert not missing, (
        f"{path.relative_to(REPO_ROOT)} writes state keys absent from its "
        f"TypedDict: {sorted(missing)}. LangGraph SILENTLY DROPS these writes — "
        f"the downstream reader will get None/[] and the feature will do nothing "
        f"while reporting success. Declare them on the state class."
    )


def test_graph_modules_were_actually_found() -> None:
    """Guard the guard: an empty parametrize would make the sweep vacuously green."""
    mods = _graph_modules()
    assert len(mods) >= 4, f"expected to find the graph modules, found {mods}"


#: The channels whose loss was a live production defect, with the effect each
#: silently produced. Driven through a real ``StateGraph`` built on the REAL
#: production state class below — so deleting a declaration fails here, loudly,
#: with the consequence spelled out.
REGRESSION_CHANNELS = [
    pytest.param(
        "aleph_wiki.synthesis_workflow",
        "SynthesisState",
        "resolved_wikilinks",
        [{"dst_title": "Topic A"}],
        "every synthesis-authored wiki page commits with ZERO wikilinks, "
        "gutting the 1-hop expansion rule #1 depends on",
        id="synthesis:resolved_wikilinks",
    ),
    pytest.param(
        "aleph_artifacts.builder.workflow",
        "BuilderState",
        "csl_items",
        [{"id": "S1", "title": "A Paper"}],
        "every exported artifact ships with an EMPTY bibliography",
        id="builder:csl_items",
    ),
    pytest.param(
        "aleph_artifacts.builder.workflow",
        "BuilderState",
        "version_no",
        7,
        "every rebuild overwrites version 1's bytes at the same asset key while "
        "the DB records an incrementing version_no",
        id="builder:version_no",
    ),
    pytest.param(
        "aleph_reviewer.editorial.workflow",
        "EditorialReviewState",
        "n_c",
        3,
        "editorial finding_count is always 0",
        id="editorial:n_c",
    ),
]


@pytest.mark.parametrize(("module", "cls_name", "key", "value", "consequence"), REGRESSION_CHANNELS)
@pytest.mark.asyncio
async def test_production_state_carries_channel(
    module: str, cls_name: str, key: str, value: object, consequence: str
) -> None:
    """A real graph over the REAL state class must carry the real channel.

    This is the behavioural half of the guard. The sweep above proves the key is
    *declared*; this proves LangGraph actually *carries* it end to end through a
    write→read hop, using the production TypedDict rather than a stand-in.
    """
    import importlib

    from langgraph.graph import END, START, StateGraph

    state_cls = getattr(importlib.import_module(module), cls_name)
    seen: dict[str, object] = {}

    async def _writer(_state: object) -> dict:
        return {key: value}

    async def _reader(state: dict) -> dict:
        seen[key] = state.get(key)
        return {}

    g = StateGraph(state_cls)
    g.add_node("writer", _writer)
    g.add_node("reader", _reader)
    g.add_edge(START, "writer")
    g.add_edge("writer", "reader")
    g.add_edge("reader", END)
    await g.compile().ainvoke({})

    assert seen[key] == value, (
        f"{module}.{cls_name} dropped the '{key}' channel. Effect in production: {consequence}."
    )


@pytest.mark.asyncio
async def test_langgraph_really_drops_undeclared_keys() -> None:
    """Pin the upstream behaviour this whole module defends against.

    If LangGraph ever starts preserving undeclared channels, this test fails and
    tells the next reader the sweep may be relaxed. Until then it is proof that
    the sweep guards a real, load-bearing semantic rather than a style rule.
    """
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict, total=False):
        declared: str

    seen: dict[str, object] = {}

    async def _writer(_state: _S) -> dict:
        return {"declared": "kept", "undeclared": "dropped"}

    async def _reader(state: _S) -> dict:
        seen["declared"] = state.get("declared")
        seen["undeclared"] = state.get("undeclared")  # type: ignore[typeddict-item]
        return {}

    g = StateGraph(_S)
    g.add_node("writer", _writer)
    g.add_node("reader", _reader)
    g.add_edge(START, "writer")
    g.add_edge("writer", "reader")
    g.add_edge("reader", END)
    await g.compile().ainvoke({})

    assert seen["declared"] == "kept"
    assert seen["undeclared"] is None, (
        "LangGraph now preserves undeclared state channels — the "
        "test_no_undeclared_state_keys sweep may be relaxed."
    )


@pytest.mark.asyncio
async def test_langgraph_drops_undeclared_keys_on_input_too() -> None:
    """The same filtering applies to the INITIAL state, not just node writes.

    This is the half of the semantic that hid the `version_no` defect: the
    builder seeded a correctly-computed value into the invoke payload and the
    node that consumed it silently received the fallback.
    """
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class _S(TypedDict, total=False):
        declared: str

    seen: dict[str, object] = {}

    async def _reader(state: _S) -> dict:
        seen["declared"] = state.get("declared")
        seen["undeclared"] = state.get("undeclared")  # type: ignore[typeddict-item]
        return {}

    g = StateGraph(_S)
    g.add_node("reader", _reader)
    g.add_edge(START, "reader")
    g.add_edge("reader", END)
    await g.compile().ainvoke({"declared": "kept", "undeclared": "seeded"})  # type: ignore[typeddict-unknown-key]

    assert seen["declared"] == "kept"
    assert seen["undeclared"] is None, (
        "LangGraph now preserves undeclared keys seeded into the initial state — "
        "the _seeded_keys half of the sweep may be relaxed."
    )
