"""The purpose prefix has to name the CALLER, or status number 5 measures noise.

`scripts/_acceptance/status_numbers.py` counts a model call as an
attribution defect when `agent_run_id IS NULL AND purpose LIKE 'assistant%'`,
and says why in a comment: "the prefix is the honest discriminator — `purpose`
is what the call site declares itself to be."

It was not the call site. `WikiFirstRetrievalRouter` wrote the purpose itself,
so a real agent turn and the debug-only `/assistant/retrieve` route emitted the
same four strings. Measured on this instance: 10 rows of
`assistant.corpus_search.query_embed` and `assistant.page_selection` with a null
run id, every one of them from the debug route, counted as an agent-path defect
that did not exist — while the agent path was in fact 13/13 attributed.

A number that reports a defect which is not there gets ignored, and then it
cannot report the one that is.

The debug route has no turn to belong to and never will; requiring one would
mean minting a fake run so a number could go green. So it declares itself
`diagnostic`, and an unattributed `assistant.*` row means what number 5 always
claimed it meant.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter


def _prefix_kwarg(source_file: Path) -> list[str | None]:
    """Every `WikiFirstRetrievalRouter(...)` in a module, as its prefix kwarg.

    Returns `None` for a construction that passes no `purpose_prefix`, which is
    how the default is distinguished from an explicit value.
    """
    tree = ast.parse(source_file.read_text())
    found: list[str | None] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "WikiFirstRetrievalRouter":
            continue
        explicit: str | None = None
        for kw in node.keywords:
            if kw.arg == "purpose_prefix" and isinstance(kw.value, ast.Constant):
                explicit = str(kw.value.value)
        found.append(explicit)
    return found


def test_the_default_prefix_is_assistant() -> None:
    """The agent path passes nothing, so the default is what a real turn emits."""
    sig = inspect.signature(WikiFirstRetrievalRouter.__init__)
    assert sig.parameters["purpose_prefix"].default == "assistant"


def test_the_debug_route_does_not_claim_the_assistant_prefix() -> None:
    """The route that passes `agent_run_id=None` must not look like a turn."""
    route = Path(__file__).resolve().parents[2] / "src" / "aleph_api" / "routes" / "assistant.py"
    prefixes = _prefix_kwarg(route)
    assert prefixes, f"no WikiFirstRetrievalRouter construction found in {route}"
    for prefix in prefixes:
        assert prefix is not None, (
            "the debug retrieval route constructs the router without "
            "purpose_prefix, so it inherits 'assistant' and its unattributed "
            "calls are counted as agent-path attribution defects"
        )
        assert not prefix.startswith("assistant"), (
            f"debug route declares purpose_prefix={prefix!r}, which status "
            "number 5 reads as the agent path"
        )


def test_the_agent_path_still_uses_the_assistant_prefix() -> None:
    """The other half. Exempting the debug route is only safe if the real one
    still declares itself — otherwise this change would have made number 5
    unable to report the defect it exists for."""
    agent = Path(__file__).resolve().parents[2] / "src" / "aleph_api" / "copilot_agent.py"
    prefixes = _prefix_kwarg(agent)
    assert prefixes, f"no WikiFirstRetrievalRouter construction found in {agent}"
    for prefix in prefixes:
        assert prefix is None or prefix.startswith("assistant"), (
            f"the agent path declares purpose_prefix={prefix!r}; its calls "
            "would no longer be counted by status number 5"
        )
