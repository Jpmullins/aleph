"""The composition root's probes, run against real services. WS-P1 criterion 4.

`packages/aleph-runtime` is 783 lines and had **zero tests**. It is the file
that decides what a running Aleph consists of: every shared service is a kernel
capability declared there, each with an inverse and a probe that exercises its
real read path. The rule it exists to enforce is that *a capability which cannot
answer a live query must not come up* — and nothing checked that the probes
could tell.

`scripts/_acceptance/kernel_boot.py` does boot the manifest, but CI never called
it until recently and a script is not a test: it cannot be run per-change, it
cannot be collected, and a failure in it names a process rather than a
capability.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

pytestmark = pytest.mark.integration

MANIFEST = pathlib.Path("apps/api/aleph.toml")


def _declared_names() -> set[str]:
    """The capabilities the boot manifest names.

    Parsed from the manifest rather than listed here. A test carrying its own
    copy of the list passes forever after somebody adds a capability and forgets
    to mount it — which is exactly the drift this asserts against.
    """
    data = tomllib.loads(MANIFEST.read_text())
    return {c["name"] for c in data.get("capability", [])}


def test_the_manifest_and_the_composition_root_declare_the_same_set() -> None:
    """Two lists that must match, in two files, with nothing checking them.

    A capability in the manifest with no spec fails at boot with a name and no
    context. A spec no manifest names is dead code that looks live.
    """
    from aleph_api.settings import Settings
    from aleph_runtime.capabilities import core_capabilities

    def _placeholder(name: str, field: object) -> str:
        return "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"

    settings = Settings(
        **{
            name: _placeholder(name, field)
            for name, field in Settings.model_fields.items()
            if field.is_required()
        }
    )
    specs = {spec.name for spec in core_capabilities(settings)}
    declared = _declared_names()
    assert specs == declared, (
        f"manifest-only: {sorted(declared - specs)}; spec-only: {sorted(specs - declared)}"
    )


def test_every_capability_has_a_probe() -> None:
    """A capability with no probe reports healthy by never being asked.

    The composition root's own rule is that a probe must exercise the REAL read
    path. This is the weaker check that one exists at all — the strong version
    is `test_a_probe_notices_a_dead_dependency` below, which breaks a dependency
    and requires the probe to notice.
    """
    from aleph_api.settings import Settings
    from aleph_runtime.capabilities import core_capabilities

    settings = Settings(
        **{
            name: "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"
            for name, field in Settings.model_fields.items()
            if field.is_required()
        }
    )
    missing = [s.name for s in core_capabilities(settings) if s.probe is None]
    assert missing == [], f"capabilities with no probe: {missing}"


def test_every_capability_has_an_inverse_or_declares_it_has_none() -> None:
    """LIFO unwind is only meaningful if the things being unwound undo something.

    `setup` is an async generator: what it yields after the `yield` is the
    inverse. A capability that yields nothing is claiming it acquires nothing
    that needs releasing, which is a real answer — but it should be a decision,
    and this counts how many make it so the number is visible.
    """
    import inspect

    from aleph_api.settings import Settings
    from aleph_runtime.capabilities import core_capabilities

    settings = Settings(
        **{
            name: "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"
            for name, field in Settings.model_fields.items()
            if field.is_required()
        }
    )
    specs = core_capabilities(settings)
    generators = [s.name for s in specs if inspect.isasyncgenfunction(s.setup)]
    assert generators == [s.name for s in specs], (
        "a capability whose setup is not an async generator cannot yield an "
        f"inverse: {sorted({s.name for s in specs} - set(generators))}"
    )


async def test_a_probe_notices_a_dead_dependency() -> None:
    """The strong version, and the rule the composition root exists to enforce:
    a capability that cannot answer a live query must not come up.

    The database capability is pointed at a closed port and the kernel is
    booted. A probe that merely checked "did setup return an object" would pass
    — the engine constructs fine against an unreachable host, because asyncpg
    connects lazily. Only a probe that issues a real query fails, which is why
    the rule is "exercises its real read path" and not "has a probe".
    """
    from aleph_api.settings import Settings
    from aleph_kernel.kernel import Kernel
    from aleph_runtime.capabilities import core_capabilities

    required = {
        name: "placeholder-not-a-real-secret-placeholder-not-a-real-secret-xxxxx"
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }
    # Port 1 is reserved and nothing listens on it, so this is refused
    # immediately rather than hanging — a hang would test the probe TIMEOUT
    # instead, which is a different property.
    broken = Settings(**{**required, "database_url": "postgresql+asyncpg://x:x@127.0.0.1:1/x"})

    kernel = Kernel(probe_timeout_s=10.0)
    for spec in core_capabilities(broken):
        if spec.name in {"observability", "database"}:
            kernel.register_core(spec)

    with pytest.raises(Exception) as caught:
        await kernel.boot()
    # The failure names the capability. A boot that fails with a bare asyncpg
    # error tells an operator which library broke, not which dependency.
    assert "database" in str(caught.value).lower(), caught.value
