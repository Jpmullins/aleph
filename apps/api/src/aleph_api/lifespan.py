"""FastAPI lifespan: boot the kernel, mount the app, unwind on shutdown.

The shared services live in `capabilities.py` as kernel capabilities. Order is
computed from their declarations rather than written out here, and teardown is
the exact reverse with every inverse guaranteed to run.

What that replaced: ten singletons constructed in a hand-written sequence, and a
`finally` block of bare awaits where one raising `aclose()` skipped every close
after it — while anything built before the `try` leaked entirely if a later
constructor raised.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aleph_api.a2ui_handlers import build_action_router
from aleph_api.capabilities import (
    BOUND_KEYS,
    DB_SESSIONS,
    LITELLM,
    SETTINGS,
    bind_to_app_state,
)
from aleph_api.settings import Settings, get_settings
from aleph_kernel import Context, EffectScope, Kernel
from aleph_kernel.manifest import load_manifest, mount_manifest

#: Ships beside the app, not in the working directory: the set of core
#: capabilities must not depend on where the process happened to start.
BOOT_MANIFEST = Path(__file__).resolve().parents[2] / "aleph.toml"

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    kernel = Kernel()
    # The manifest is the only source of core capability, and the only place
    # `protected = true` can be set. Nothing mounted from it receives a
    # PluginId, so no argument value an agent can construct names it —
    # deactivating core capability is unexpressible rather than refused.
    mount_manifest(kernel, load_manifest(BOOT_MANIFEST), settings=settings)

    # Every capability sets up and then proves itself against the live system.
    # A failed probe unwinds that capability completely and aborts the boot, so
    # the process does not come up half-working — which is strictly better than
    # discovering at first request that the asset store cannot serve a read.
    await kernel.boot()

    # A reader context declaring everything the app shim needs. It is scoped
    # like any capability, so it cannot reach a service nobody published.
    reader = Context(
        owner="aleph-api",
        requires=BOUND_KEYS,
        store=kernel.store,
        scope=EffectScope("aleph-api"),
    )
    bind_to_app_state(app, reader)
    app.state.kernel = kernel
    app.state.action_router = build_action_router()

    session_maker = reader.get(DB_SESSIONS)

    # Rule 7: resolve the agent's model from the default named ModelProfile
    # rather than a hardcoded id, so the conversational surface uses the
    # configured tier.
    agent_bindings: dict[str, Any] | None = None
    try:
        from aleph_db.repos.model_profile import get_template

        async with session_maker() as session:
            template = await get_template(session, settings.aleph_default_model_profile)
            if template is not None:
                agent_bindings = dict(template.bindings_jsonb)
    except Exception:
        agent_bindings = None

    from aleph_api.copilot_agent import bind_runtime
    from aleph_api.copilotkit_endpoint import setup_copilotkit

    bind_runtime(
        session_maker=session_maker,
        settings=settings,
        litellm=reader.get(LITELLM),
        agent_bindings=agent_bindings,
    )
    # Mounts routes on `app`; not modelled as a capability because adding a
    # route to a running FastAPI app has no meaningful inverse.
    setup_copilotkit(app, settings=settings, store=app.state.agent_store)

    try:
        yield
    finally:
        # Dependents before providers, every inverse runs, failures aggregate.
        await kernel.shutdown()


def app_settings(app: FastAPI) -> Settings:
    return app.state.settings


__all__ = ["SETTINGS", "app_settings", "lifespan"]
