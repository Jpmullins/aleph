"""Every LangGraph node's state writes must be declared on its state schema.

LangGraph **silently drops** updates to channels absent from the graph's state
schema. The reader then does ``state.get(key) or []``, takes the empty path, and
reports success — a failure mode that is invisible to any test which does not
assert on the *downstream* effect. This repo already documents the hazard
verbatim at ``aleph_reviewer/mechanical/workflow.py`` ("MUST be declared here:
LangGraph silently drops node updates to keys absent from the state schema") and
then violated it in three other modules:

* ``synthesis_workflow.py``  — ``resolved_wikilinks`` → every synthesis-authored
  wiki page committed with **zero** wikilinks, gutting the 1-hop expansion that
  load-bearing rule #1 depends on.
* ``builder/workflow.py``    — ``csl_items``          → every exported artifact
  shipped with an **empty bibliography**.
* ``editorial/workflow.py``  — ``n_c``…``n_f``        → ``finding_count`` always 0.

The two tests below are the standing guard. `test_no_undeclared_state_keys` is a
static sweep over every graph module; `test_langgraph_really_drops_undeclared_keys`
pins the upstream behaviour the sweep exists to defend against, so the sweep can
never be quietly deleted on the theory that "LangGraph probably handles it".
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SEARCH_ROOTS = (REPO_ROOT / "packages", REPO_ROOT / "apps")


def _graph_modules() -> list[pathlib.Path]:
    """Every non-test module that builds a LangGraph ``StateGraph``."""
    out: list[pathlib.Path] = []
    for root in SEARCH_ROOTS:
        for p in root.rglob("*.py"):
            parts = set(p.parts)
            if "tests" in parts or ".venv" in parts or "node_modules" in parts:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if "add_node(" in text and "StateGraph" in text:
                out.append(p)
    return sorted(out)


def _declared_state_keys(tree: ast.Module) -> dict[str, set[str]]:
    """``{StateClassName: {declared keys, including inherited}}``.

    A state class may extend another TypedDict rather than ``TypedDict`` itself
    — ``MechanicalReviewState(_MechanicalReviewInput, total=False)`` does — so
    membership is computed transitively and keys are unioned across the chain.
    """
    own: dict[str, set[str]] = {}
    bases_of: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {b.id for b in node.bases if isinstance(b, ast.Name)}
        own[node.name] = {
            stmt.target.id
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        }
        bases_of[node.name] = base_names

    # Transitive closure: a class is a TypedDict if TypedDict is anywhere above it.
    typed_dicts: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, bases in bases_of.items():
            if name in typed_dicts:
                continue
            if "TypedDict" in bases or (bases & typed_dicts):
                typed_dicts.add(name)
                changed = True

    def _all_keys(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if name in seen:
            return set()
        keys = set(own.get(name, set()))
        for base in bases_of.get(name, set()):
            keys |= _all_keys(base, seen | {name})
        return keys

    return {name: _all_keys(name) for name in typed_dicts}


def _returned_keys(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """String keys of every dict literal this function ``return``s.

    Only top-level ``return {...}`` shapes count — a node's state update is
    always a dict literal returned from the node body. Dict literals *inside*
    the function (payload construction, span attributes) are ignored because
    they are not the node's return value.
    """
    keys: set[str] = set()
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.Return) or not isinstance(stmt.value, ast.Dict):
            continue
        for k in stmt.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


def _registered_nodes(tree: ast.Module) -> tuple[set[str], set[str]]:
    """What ``add_node`` actually registers.

    Returns ``(direct_fn_names, wrapper_slots)``:

    * ``add_node("x", _node_x)``            → ``_node_x``'s returns ARE channel writes.
    * ``add_node("x", _wrap(_node_x, "n"))`` → the *wrapper's* return is the channel
      write, so the slot string ``"n"`` is the written key and ``_node_x``'s own
      ``{"n": ...}`` return is an internal detail that never reaches the graph.

    Restricting the scan to registered nodes is what keeps helper functions that
    merely take the state type from producing false positives.
    """
    direct: set[str] = set()
    slots: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "add_node":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name):
                direct.add(arg.id)
            elif isinstance(arg, ast.Call):
                for inner in arg.args:
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        slots.add(inner.value)
    return direct, slots


def _seeded_keys(tree: ast.Module, state_names: set[str]) -> set[str]:
    """Keys in an initial-state dict literal, e.g. ``state: BuilderState = {...}``.

    LangGraph filters undeclared keys out of the INITIAL state as well as out of
    node writes — verified empirically by
    ``test_langgraph_drops_undeclared_keys_on_input_too`` below. An undeclared
    seed is therefore just as silent as an undeclared write, and it is how the
    ``version_no`` defect hid: the builder computed the next version number
    correctly, seeded it, and the node that built the asset storage key never
    received it, so every rebuild overwrote version 1's bytes.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Dict):
            continue
        ann = node.annotation
        if isinstance(ann, ast.Name) and ann.id in state_names:
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


def _violations(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    states = _declared_state_keys(tree)
    if not states:
        return set()
    declared: set[str] = set()
    for keys in states.values():
        declared |= keys

    direct_names, wrapper_slots = _registered_nodes(tree)
    written: set[str] = set(wrapper_slots)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in direct_names:
            written |= _returned_keys(node)
    written |= _seeded_keys(tree, set(states))
    return written - declared


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
