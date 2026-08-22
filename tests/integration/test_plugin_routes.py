"""The kernel, reachable over HTTP. WS-A2.

CLAUDE.md's first substantive line is that Aleph is an agent that authors
plugins for itself on a kernel with guardrails, and that "the kernel is the
product". The kernel was built, guarded and covered by 153 tests, and
`grep -rn "AgentPluginAPI" apps/api/src` returned **0** — no route, no tool, no
graph node. The product was a library with one non-test importer, and that
importer was an acceptance probe.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.kernel import Kernel
from aleph_kernel.spec import CapabilitySpec

pytestmark = pytest.mark.integration


def _spec(name: str, *, provides: tuple[str, ...], requires: tuple[str, ...] = ()) -> Any:
    """A minimal working capability.

    `setup` is an async GENERATOR, not a coroutine — the kernel drives it as an
    async iterator so a capability can yield its own inverse. A plain `async
    def` here fails at install with
    `TypeError: 'coroutine' object is not an async iterator`, which is what an
    earlier version of this fixture did.
    """

    async def _setup(ctx: Any) -> Any:
        for key in provides:
            ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover - never taken; makes this a generator
            yield

    async def _probe(_ctx: Any) -> Any:
        from aleph_kernel.spec import ok

        return ok(f"{name} responded")

    return CapabilitySpec(
        name=name,
        provides=frozenset(provides),
        requires=frozenset(requires),
        setup=_setup,
        probe=_probe,
    )


async def test_preview_matches_the_refusal() -> None:
    """THE criterion, and the reason preview exists at all.

    A refusal an operator could not have predicted is indistinguishable from a
    broken button. Preview and refusal read the SAME declaration graph, so this
    asserts they name the same set rather than that each is individually
    plausible.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    a = await api.install(_spec("cap-a", provides=("thing.a",)))
    b = await api.install(_spec("cap-b", provides=("thing.b",), requires=("thing.a",)))
    assert a.installed and b.installed

    previewed = {
        tuple(sorted(getattr(v, "would_also_stop", ()) or ()))
        for v in api.inspect()
        if v.name == "cap-a"
    }
    assert previewed == {("cap-b",)}, previewed

    from aleph_kernel.kernel import PluginId

    outcome = await api.disable(PluginId(uuid.UUID(str(a.plugin_id))))
    # `installed=True` is the REFUSAL: the plugin is still installed. The field
    # answers "is it installed", not "did the call succeed", and reading it the
    # intuitive way inverts the check — which is exactly what the first version
    # of this test, the route and the agent tool all did.
    assert outcome.installed, "the disable was allowed despite a dependent"
    assert "refused" in outcome.detail
    assert "cap-b" in outcome.detail, (
        f"preview said cap-b and the refusal said {outcome.detail!r} — an "
        "operator cannot trust a preview that disagrees with the thing it "
        "previews"
    )


async def test_core_capability_has_no_handle_to_pass_anywhere() -> None:
    """Not "refused" — UNEXPRESSIBLE.

    A capability mounted from the boot manifest has `plugin_id = None`, so
    there is no id to put in a URL or hand to a tool. The route surface carries
    ids rather than names precisely so it inherits that.
    """
    kernel = Kernel()
    kernel.register_core(_spec("core-thing", provides=("core.thing",)))
    api = AgentPluginAPI(kernel)

    core = [v for v in api.inspect() if v.name == "core-thing"]
    assert core, "the core capability vanished from inspect()"
    assert core[0].plugin_id is None
    assert core[0].removable is False


async def test_the_route_module_reaches_the_kernel_through_the_agent_api() -> None:
    """Criterion 1, as a property of the code rather than a grep of it.

    `grep -rn AgentPluginAPI apps/api/src | wc -l >= 1` is satisfied by a
    docstring. This asserts the route module actually constructs one.
    """
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/plugins.py")
    tree = ast.parse(src.read_text())  # noqa: ASYNC240 - a source read, not I/O in a request
    constructed = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "AgentPluginAPI"
    ]
    assert constructed, "the route module names AgentPluginAPI but never builds one"


def test_the_agent_has_the_five_kernel_tools() -> None:
    """And every one is classified for the interpreter loop.

    `test_every_orchestrator_tool_is_classified` enforces the partition; this
    asserts the tools exist to be partitioned, and that the two that CHANGE what
    the system can do are the two withheld from a loop.
    """
    from aleph_api.copilot_agent import _ORCHESTRATOR_TOOLS
    from aleph_api.interpreter import PTC_ALLOWLIST, PTC_WITHHELD

    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in _ORCHESTRATOR_TOOLS}
    assert {
        "list_capabilities",
        "preview_removal",
        "author_plugin",
        "disable_plugin",
        "plugin_health",
    } <= names

    # Reads may loop; the two that alter the system's own abilities may not.
    assert "preview_removal" in PTC_ALLOWLIST
    assert "list_capabilities" in PTC_ALLOWLIST
    assert "author_plugin" in PTC_WITHHELD
    assert "disable_plugin" in PTC_WITHHELD


def test_authoring_requires_owner_not_merely_project_access() -> None:
    """A plugin is code this process executes and an instruction the model
    follows. That is not an editing action."""
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/plugins.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "author_plugin"
    )
    calls = [
        ast.unparse(n)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "require_at_least"
    ]
    assert calls, "author_plugin does not check a role at all"
    assert any("ProjectRole.OWNER" in c for c in calls), calls
