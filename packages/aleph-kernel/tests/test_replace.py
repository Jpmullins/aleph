"""Hot replacement, and the rollback that makes it safe (A4).

The precondition for anything an agent authors: a plugin it improves must be
replaceable while the system serves requests, or every self-modification costs a
restart and discards all process-local state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.context import Context
from aleph_kernel.errors import DependentsWouldBreak, ProbeFailed
from aleph_kernel.kernel import Kernel
from aleph_kernel.skills import skill_capability, skill_from_source
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

pytestmark = pytest.mark.asyncio

Inv = Callable[[], Awaitable[None]]


def versioned(name: str, version: int, *, healthy: bool = True, provides=("thing",)):
    async def setup(ctx: Context) -> AsyncIterator[Inv]:
        for key in provides:
            ctx.provide(key, f"v{version}")
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        return ok(f"v{version}") if healthy else problem(f"v{version} is broken")

    return CapabilitySpec(name=name, setup=setup, probe=probe, provides=frozenset(provides))


async def test_an_implementation_can_be_swapped_live() -> None:
    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    outcome = await api.install(versioned("svc", 1))
    import uuid

    pid = uuid.UUID(outcome.plugin_id)

    from aleph_kernel.kernel import PluginId

    new_id = await kernel.replace(PluginId(pid), versioned("svc", 2))

    assert "svc" in kernel.active()
    assert str(new_id) != outcome.plugin_id, "the handle must change with the generation"


async def test_a_failed_replacement_restores_the_working_version() -> None:
    """THE property. A hot-reload that can leave a capability absent is worse
    than none: the agent believes it improved something and removed it."""
    import uuid

    from aleph_kernel.kernel import PluginId

    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    outcome = await api.install(versioned("svc", 1))
    pid = PluginId(uuid.UUID(outcome.plugin_id))

    with pytest.raises(ProbeFailed):
        await kernel.replace(pid, versioned("svc", 2, healthy=False))

    assert "svc" in kernel.active(), "a failed swap left the capability absent"
    assert kernel.is_provided("thing"), "the working version's binding was not restored"


async def test_a_replacement_that_drops_a_service_others_need_is_refused() -> None:
    """Dependents are live; taking their footing mid-flight is a break, not a swap."""
    import uuid

    from aleph_kernel.kernel import PluginId

    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    provider = await api.install(versioned("provider", 1, provides=("thing",)))
    await api.install(
        CapabilitySpec(
            name="dependent",
            setup=versioned("x", 1).setup,
            probe=versioned("x", 1).probe,
            requires=frozenset({"thing"}),
        )
    )

    with pytest.raises(DependentsWouldBreak, match="dependent"):
        await kernel.replace(
            PluginId(uuid.UUID(provider.plugin_id)),
            versioned("provider", 2, provides=()),
        )

    assert kernel.is_provided("thing")


async def test_a_replacement_may_add_services() -> None:
    import uuid

    from aleph_kernel.kernel import PluginId

    kernel = Kernel()
    api = AgentPluginAPI(kernel)
    outcome = await api.install(versioned("svc", 1, provides=("thing",)))
    await kernel.replace(
        PluginId(uuid.UUID(outcome.plugin_id)),
        versioned("svc", 2, provides=("thing", "extra")),
    )
    assert kernel.is_provided("extra")


async def test_a_skill_can_be_replaced_with_a_better_version() -> None:
    """The self-improvement case, end to end."""
    import uuid

    from aleph_kernel.kernel import PluginId

    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    v1 = skill_from_source(
        "helper", "---\nname: helper\n---\n\nUse `go()`.", "def go():\n    return 1\n"
    )
    outcome = await api.install(skill_capability(v1))
    assert outcome.installed

    v2 = skill_from_source(
        "helper", "---\nname: helper\n---\n\nUse `go()`.", "def go():\n    return 2\n"
    )
    await kernel.replace(PluginId(uuid.UUID(outcome.plugin_id)), skill_capability(v2))

    assert "skill.helper" in kernel.active()


async def test_a_captured_reference_still_sees_its_own_generation() -> None:
    """Theorem 63: a holder reads the bindings it was loaded against.

    Fresh-namespace loading is what buys this — the old generation's functions
    keep working for whoever already holds them, instead of being rebound
    underneath a caller mid-flight.
    """
    v1 = skill_from_source("g", "---\nname: g\n---\n\nUse `go()`.", "def go():\n    return 1\n")
    captured = v1.helper("go")

    v2 = skill_from_source("g", "---\nname: g\n---\n\nUse `go()`.", "def go():\n    return 2\n")

    assert captured() == 1, "an existing reference was rebound to the new generation"
    assert v2.helper("go")() == 2
