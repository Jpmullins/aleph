"""`task()` from the REPL is on, and the reachable set is pinned rather than gated.

`CodeInterpreterMiddleware` takes `subagents` as a BOOL. There is no per-subagent
allowlist parameter, so the set `task()` can dispatch is exactly the
orchestrator's roster and nothing narrows it. Pretending otherwise would be the
kind of claim this repository keeps finding and deleting.

What CAN be asserted is that the set does not grow by accident. A dispatch from
inside an `eval` carries no per-dispatch HITL approval — upstream says so in a
warning block — so the next subagent somebody adds may be one that spends or
writes far more freely than these five, and it would become REPL-dispatchable the
moment it joined the roster. This test makes that a decision instead.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from aleph_api import interpreter
from aleph_api.interpreter import SUBAGENT_DISPATCHABLE


def test_the_interpreter_exposes_task() -> None:
    """The feature itself. `subagents=False` forbids the fan-out the interpreter
    exists to enable, while still permitting the loop-over-tools case that is
    only marginally cheaper."""
    src = Path(inspect.getfile(interpreter)).read_text()
    tree = ast.parse(src)
    found: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "CodeInterpreterMiddleware":
            continue
        for kw in node.keywords:
            if kw.arg == "subagents" and isinstance(kw.value, ast.Constant):
                found.append(bool(kw.value.value))
    assert found, "no CodeInterpreterMiddleware construction found"
    assert all(found), "subagents must be True for task() to exist in the REPL"


def test_the_fan_out_is_bounded_even_without_per_dispatch_approval() -> None:
    """The honest safety argument, asserted rather than asserted-about.

    There is no HITL gate per `task()` call. What bounds the fan-out is the
    eval's own ceiling — and a subagent loop costs seconds, so the wall clock is
    the operative limit, not the tool-call cap beside it.
    """
    assert interpreter.INTERPRETER_TIMEOUT_S <= 120, (
        "the wall clock is the operative bound on subagent fan-out; raising it "
        "raises how many model loops one approved eval can start"
    )
    assert interpreter.INTERPRETER_MAX_PTC_CALLS <= 256
    assert interpreter.INTERPRETER_MEMORY_LIMIT_BYTES <= 64 * 1024 * 1024


def test_repl_dispatchable_subagents_are_pinned() -> None:
    """The inventory must equal the roster, in both directions.

    A subagent in the roster but not here became REPL-dispatchable without
    anyone deciding. One here but not in the roster is a name `task()` would
    reject — a documented capability that does not exist.
    """
    from aleph_api.subagents import DELEGATABLE_SUBAGENTS

    assert set(SUBAGENT_DISPATCHABLE) == set(DELEGATABLE_SUBAGENTS), (
        "the pinned REPL-dispatchable set and the subagent roster disagree; "
        "adding a subagent makes it dispatchable from an approved eval with no "
        "further approval, so update this deliberately"
    )


def test_the_inventory_is_not_described_as_a_gate() -> None:
    """It cannot narrow anything, and the docstring must not imply it can.

    `subagents` is a bool. A future reader who believes this list is enforced
    would add a dangerous subagent to the roster and leave it off this list,
    expecting to be protected.
    """
    doc = inspect.getdoc(interpreter) or ""
    assert "NOT A GATE" in Path(inspect.getfile(interpreter)).read_text()
    assert "bool" in doc.lower() or "BOOL" in Path(inspect.getfile(interpreter)).read_text()
