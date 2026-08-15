"""A3 — the worker boots its capabilities from its own manifest.

Asserts the two processes SHARE the factories and DIFFER in what they mount:
duplicated wiring is what let the same teardown bug exist in both, fixed in one.
"""

from __future__ import annotations

import asyncio
import os
import sys

_DEFAULTS = {
    "DATABASE_URL": "postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph",
    "REDIS_URL": "redis://localhost:6379/0",
    "ALEPH_CODE_RUNNER_REDIS_URL": "redis://localhost:6379/1",
    "ALEPH_ASSET_BACKEND": "fs",
    "ALEPH_ASSET_ROOT": "/tmp/aleph-acceptance-assets",
    "LITELLM_BASE_URL": "http://localhost:4999",
    "INSIGHTS_LITELLM_API_KEY": "acceptance-probe-not-called",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-acceptance",
    "LANGFUSE_SECRET_KEY": "sk-acceptance",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:1",
    "ALEPH_AGENT_TOKEN_SECRET": "acceptance-secret-acceptance-secret",
    # Workers re-enter the API over HTTP for every mutation, so this is
    # required config rather than a default.
    "ALEPH_API_INTERNAL_URL": "http://localhost:8000",
    # The sandbox bus must differ from the platform bus; the probe checks it.
    "CODE_RUNNER_REDIS_URL": "redis://localhost:6379/1",
}
for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)


async def main() -> int:
    from aleph_api.lifespan import BOOT_MANIFEST as API_MANIFEST
    from aleph_kernel import Kernel, ProbeFailed
    from aleph_kernel.manifest import load_manifest, mount_manifest
    from aleph_workers.arq import BOOT_MANIFEST as WORKER_MANIFEST
    from aleph_workers.settings import get_worker_settings

    api_entries = load_manifest(API_MANIFEST)
    worker_entries = load_manifest(WORKER_MANIFEST)

    api_names = {e.name for e in api_entries}
    worker_names = {e.name for e in worker_entries}
    if api_names == worker_names:
        print("both manifests mount the same set; the split buys nothing")
        return 1

    # Shared factories, not copied ones.
    api_factories = {e.factory for e in api_entries}
    worker_factories = {e.factory for e in worker_entries}
    if not (api_factories & worker_factories):
        print("the two processes share no factories — the wiring is still duplicated")
        return 1
    for factory in api_factories | worker_factories:
        if not factory.startswith("aleph_runtime."):
            print(f"factory {factory} is not in the shared package")
            return 1

    kernel = Kernel()
    mount_manifest(kernel, worker_entries, settings=get_worker_settings())
    try:
        await kernel.boot()
    except ProbeFailed as exc:
        print(f"worker probe failed: {exc}")
        return 1
    except Exception as exc:
        print(f"worker boot failed: {exc!r}")
        return 1

    if kernel.active() != worker_names:
        print(f"expected {sorted(worker_names)}, got {sorted(kernel.active())}")
        return 1

    await kernel.shutdown()
    if kernel.active():
        print(f"shutdown left {sorted(kernel.active())} active")
        return 1

    only_worker = sorted(worker_names - api_names)
    only_api = sorted(api_names - worker_names)
    print(
        f"worker booted {len(worker_names)} capabilities from its manifest, all probes "
        f"passed, shutdown clean. worker-only: {only_worker}; api-only: {only_api}; "
        f"shared factories: {len(api_factories & worker_factories)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
