"""An agent tool must not be more permissive than the route it mirrors.

`author_plugin` stores agent-written code that this process will execute;
`disable_plugin` can remove capability other things stand on. Both HTTP routes
require OWNER (`routes/plugins.py`). Both agent tools gated through
`_authorized`, which hardcoded VIEWER — so the same two operations had two
different answers depending on which door they came through, and the door the
model uses was the permissive one.

The existing pinning test (`test_plugin_routes.py`) reads only
`routes/plugins.py`, so the tool path was invisible to it. That is why this test
reads `copilot_agent.py` instead: the gap was not that either check was wrong,
it was that nothing compared them.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from aleph_api import copilot_agent
from aleph_security.roles import ProjectRole

#: Tools whose HTTP equivalent requires OWNER.
MUTATING_PLUGIN_TOOLS = ("author_plugin", "disable_plugin")

AGENT_SRC = Path(inspect.getfile(copilot_agent))


def _owner_gated(func_name: str) -> bool:
    """True when `func_name` calls `_authorized(..., at_least=ProjectRole.OWNER)`."""
    tree = ast.parse(AGENT_SRC.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name != func_name:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            target = call.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if name != "_authorized":
                continue
            for kw in call.keywords:
                if kw.arg == "at_least":
                    return ast.unparse(kw.value).endswith("OWNER")
        return False
    raise AssertionError(f"{func_name} not found in {AGENT_SRC}")


def test_the_default_is_still_viewer() -> None:
    """Most tools read. Tightening the default would break them silently."""
    sig = inspect.signature(copilot_agent._authorized)
    assert sig.parameters["at_least"].default is ProjectRole.VIEWER


def test_mutating_plugin_tools_require_owner() -> None:
    for name in MUTATING_PLUGIN_TOOLS:
        assert _owner_gated(name), (
            f"{name} authorizes at VIEWER while its HTTP route requires OWNER; "
            "the model's door is the permissive one"
        )


def test_a_read_only_plugin_tool_is_not_owner_gated() -> None:
    """The other half — otherwise 'require OWNER everywhere' would pass this
    file while making the assistant unable to answer a question about itself."""
    assert not _owner_gated("list_capabilities")
    assert not _owner_gated("plugin_health")
