"""The door has to be mounted, or it is a class nobody constructs.

A limiter that exists in `aleph_models` and is never built at boot is the exact
defect CLAUDE.md names as this codebase's dominant class: written correctly, read
by nothing. The manifest is the only place a capability becomes real — there is
deliberately no directory scan — so these read the manifests as data.

Both processes, not one. The API serves the assistant and the workers run ingest
and the research loop; a ceiling mounted in one of them bounds half the traffic
and the gateway sees the sum.
"""

from __future__ import annotations

import pathlib
import tomllib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from aleph_kernel import CapabilitySpec, Context, Kernel, ProbeFailed, ProbeResult, ok
from aleph_models.limiter import LimiterConfig, current_limits, reset_limiters
from aleph_runtime.capabilities import (
    GATEWAY_LIMITER,
    core_capabilities,
    gateway_limiter,
    models,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFESTS = ("apps/api/aleph.toml", "apps/workers/aleph.toml")
FACTORY = "aleph_runtime.capabilities:gateway_limiter"


def _entries(path: str) -> list[dict[str, Any]]:
    data: Any = tomllib.loads((ROOT / path).read_text(encoding="utf-8"))
    return list(data.get("capability", []))


def test_both_processes_mount_the_limiter() -> None:
    for path in MANIFESTS:
        entry = next((e for e in _entries(path) if e.get("factory") == FACTORY), None)
        assert entry is not None, (
            f"{path} does not mount the gateway limiter; every outbound call from that "
            f"process would be unbounded while the other process is metered"
        )
        # `protected = true` is gone (`docs/decisions.md` D18) and the property
        # it was reaching for is now held twice over, both stronger.
        #
        # Being IN this file is the first: everything the manifest mounts goes
        # through `register_core`, which assigns no `PluginId`, so there is no
        # value an agent could pass to `deactivate`. Unnameable, not refused —
        # which is what the flag restated rather than enforced.
        #
        # The PIN is the second, and it survives a future where the limiter is
        # mounted as a plugin rather than from here: retiring the sole provider
        # of a pinned key is refused, and `force` does not override it.
        import tomllib

        raw = tomllib.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        assert GATEWAY_LIMITER in raw.get("pins", []), (
            f"{path} does not pin {GATEWAY_LIMITER!r} — an agent could retire the "
            f"ceiling that stops it fanning out"
        )


def test_the_models_capability_cannot_boot_without_it() -> None:
    """Declared, so the kernel orders it first and refuses a boot that lacks it.

    The alternative — building the client and hoping a limiter exists — is how a
    process comes up cleanly with an unmetered gateway client.
    """
    assert GATEWAY_LIMITER in models().requires
    assert GATEWAY_LIMITER in gateway_limiter().provides


def test_the_composition_root_lists_it() -> None:
    names = {spec.name for spec in core_capabilities(settings=None)}
    assert "gateway_limiter" in names


# --- the capability itself: setup, probe, and the inverse -------------------


def _settings(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "litellm_base_url": "https://gw.example.com",
        "aleph_gateway_max_concurrency": 5,
        "aleph_gateway_rpm": 0,
        "aleph_gateway_queue_timeout_s": 30.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _boot(settings: Any) -> Kernel:
    """Boot a kernel carrying nothing but settings and the limiter."""

    async def _provide(ctx: Context) -> AsyncIterator[Any]:
        ctx.provide("settings", settings)
        if False:  # pragma: no cover - nothing to undo
            yield

    async def _provided(_ctx: Context) -> ProbeResult:
        return ok("settings provided")

    kernel = Kernel()
    kernel.register_core(
        CapabilitySpec(
            name="settings",
            setup=_provide,
            probe=_provided,
            provides=frozenset({"settings"}),
        )
    )
    kernel.register_core(gateway_limiter())
    await kernel.boot()
    return kernel


async def test_the_capability_boots_and_hands_its_slots_back() -> None:
    kernel = await _boot(_settings())
    try:
        assert "gateway_limiter" in kernel.active()
    finally:
        await kernel.shutdown()
    # The inverse ran: the registry is back to the shipped defaults, so a second
    # boot in the same process does not inherit the first one's ceilings.
    assert current_limits() == LimiterConfig()


async def test_a_ceiling_of_zero_refuses_to_come_up() -> None:
    """It would deadlock the first model call with no error anywhere.

    Asserted through the probe rather than through the semaphore, because the
    semaphore clamps to 1 on purpose — a process that boots and then hangs is
    strictly worse than one that refuses with a reason.
    """
    with pytest.raises(ProbeFailed, match="concurrency ceiling"):
        await _boot(_settings(aleph_gateway_max_concurrency=0))
    reset_limiters()


async def test_an_unset_gateway_url_refuses_to_come_up() -> None:
    """One door keyed on the empty string is one door for every endpoint."""
    with pytest.raises(ProbeFailed, match="empty endpoint"):
        await _boot(_settings(litellm_base_url=""))
    reset_limiters()
