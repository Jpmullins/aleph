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
}
for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)


async def main() -> int:
    from aleph_api.capabilities import core_capabilities
    from aleph_api.settings import get_settings
    from aleph_kernel import Kernel, ProbeFailed
    from aleph_kernel.support import topological_order

    settings = get_settings()
    specs = core_capabilities(settings)
    kernel = Kernel()
    for spec in specs:
        kernel.register_core(spec)

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

    await kernel.shutdown()
    if kernel.active():
        print(f"shutdown left {sorted(kernel.active())} active")
        return 1

    print(
        f"{len(order)} capabilities booted, all probes passed, "
        f"blast_radius(database)->{sorted(radius.collateral)}, shutdown clean"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
