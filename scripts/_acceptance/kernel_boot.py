"""A2 — boot the API's capability set on the kernel and assert every probe passes.

Not a mock. Constructs the real capabilities against the real Postgres, Redis,
asset store and agent store, runs each capability's probe, exercises the
guardrail on the live graph, and asserts shutdown leaves nothing active.

Exit 0 on success; prints the failing probe and exits 1 otherwise.
"""

from __future__ import annotations

import asyncio
import os
import sys

_DEFAULTS = {
    "DATABASE_URL": "postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph",
    "REDIS_URL": "redis://localhost:6379/0",
    "ALEPH_AUTH_MODE": "local",
    "ALEPH_ASSET_BACKEND": "fs",
    "ALEPH_ASSET_ROOT": "/tmp/aleph-acceptance-assets",
    "LITELLM_BASE_URL": "http://localhost:4999",
    "INSIGHTS_LITELLM_API_KEY": "acceptance-probe-not-called",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-acceptance",
    "LANGFUSE_SECRET_KEY": "sk-acceptance",
    # Point the exporter at a closed port so a missing collector does not stall
    # the probe; tracing correctness is asserted in-process, not over the wire.
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:1",
    "ALEPH_AGENT_TOKEN_SECRET": "acceptance-secret-acceptance-secret",
    # Added 2026-08-22. `aleph_credential_master_key` became a required field
    # on BOTH settings models after this list was written, and nothing noticed:
    # the `acceptance` CI job does not set it either, so `kernel_boot.py` and
    # `worker_boot.py` raised `ValidationError` there — A2 and A3 red on a
    # missing env var, under `--strict`, before a single capability mounted.
    # Reproduced by running this file under that job's exact `env:` block.
    #
    # A probe-only value, `setdefault` like every entry here, so a real
    # deployment's key always wins. It decrypts nothing: the same is already
    # true of the signing secret one line above.
    "ALEPH_CREDENTIAL_MASTER_KEY": "acceptance-master-key-acceptance-master-key",
}
for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)


async def main() -> int:
    from aleph_api.lifespan import BOOT_MANIFEST
    from aleph_api.settings import get_settings
    from aleph_kernel import Kernel, ProbeFailed
    from aleph_kernel.manifest import load_manifest, mount_manifest
    from aleph_kernel.support import topological_order

    settings = get_settings()
    kernel = Kernel()
    # Through the manifest, exactly as the app boots — otherwise this checks a
    # code path production does not take.
    entries = load_manifest(BOOT_MANIFEST)
    mount_manifest(kernel, entries, settings=settings)
    specs = [m.spec for m in kernel._mounted.values()]

    order = topological_order({s.name: s for s in specs})

    try:
        await kernel.boot()
    except ProbeFailed as exc:
        print(f"probe failed: {exc}")
        return 1
    except Exception as exc:
        print(f"boot failed: {exc!r}")
        return 1

    active = kernel.active()
    expected = {s.name for s in specs}
    if active != expected:
        print(f"expected {sorted(expected)} active, got {sorted(active)}")
        return 1

    # The guardrail, on the real graph. `database` provides db.sessions, which
    # `models` requires — so removing it must be reported as unsafe.
    radius = kernel.blast_radius("database")
    if radius.is_safe:
        print("blast_radius(database) reported safe; the dependency graph is not being read")
        return 1

    # Addressability: nothing mounted from the manifest may be nameable by an
    # agent, which is the primary guardrail.
    from aleph_kernel.agent_api import AgentPluginAPI

    addressable = [v.name for v in AgentPluginAPI(kernel).inspect() if v.plugin_id is not None]
    if addressable:
        print(f"manifest capabilities are agent-addressable: {addressable}")
        return 1

    # Pins, not `protected` (`docs/decisions.md` D18). The flag was a capability
    # asserting its own importance; a pin is the operator naming a key this
    # deployment exists to serve, and it catches the case the flag could not —
    # retiring the sole provider of a pinned key even when nothing depends on it.
    from aleph_kernel.manifest import load_pins

    pins = load_pins(BOOT_MANIFEST)
    if not pins:
        print("the manifest pins nothing — an agent could retire any key")
        return 1

    provided: set[str] = set()
    for name in kernel.active():
        provided |= kernel.spec_for(name).provides
    unprovided = sorted(pins - provided)
    if unprovided:
        print(f"pinned keys nothing provides: {unprovided}")
        return 1

    await kernel.shutdown()
    if kernel.active():
        print(f"shutdown left {sorted(kernel.active())} active")
        return 1

    print(
        f"{len(order)} capabilities booted from the manifest, all probes passed, "
        f"{len(pins)} pinned key(s) all provided, none agent-addressable, "
        f"blast_radius(database)->{sorted(radius.collateral)}, shutdown clean"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
