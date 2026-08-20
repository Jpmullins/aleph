"""Which state keys a LangGraph node may write — the analyzer.

LangGraph silently discards a node's write to a channel its state `TypedDict`
does not declare. No error, no warning: the node returns
`{"resolved_wikilinks": [...]}`, the channel does not exist, the write is
dropped, and the reader's `state.get("resolved_wikilinks", [])` hands back an
empty list. Every step reports success and the feature is inert. Four shipped
defects had exactly that shape.

This module holds the analysis. `scripts/check-graph-state-keys.sh` runs it as
the CI gate and `tests/unit/test_graph_state_keys.py` proves it models
LangGraph's real behaviour, so there is one implementation and two callers.

It used to live in `tests/unit/`, with the CI sweep importing *up* into the test
suite. That inversion broke the build the moment `tests/` was deleted: a gate
that only works when the test suite exists is a gate with a dependency nobody
declared. The analyzer belongs beside the sweep; the tests belong on top of it.

Pure AST — no database, no gateway, no running graph.
"""

from __future__ import annotations

import ast
import pathlib

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
