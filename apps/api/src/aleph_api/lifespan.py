"""FastAPI lifespan: boot the kernel, mount the app, unwind on shutdown.

The shared services live in `capabilities.py` as kernel capabilities. Order is
computed from their declarations rather than written out here, and teardown is
the exact reverse with every inverse guaranteed to run.

What that replaced: ten singletons constructed in a hand-written sequence, and a
`finally` block of bare awaits where one raising `aclose()` skipped every close
after it — while anything built before the `try` leaked entirely if a later
constructor raised.

Two things still live here rather than in a capability, because each one binds a
*running FastAPI app* to the booted kernel and has no meaningful inverse:
building the agent's durable checkpointer, and mounting the AG-UI route. Both sit
inside the `try`, so a failure in either still unwinds the kernel completely.

Gateway model discovery is deliberately NOT one of them. The pricing table it
fills belongs to the `models` capability and is read by that capability's own
probe, so discovery has to happen inside it — nothing out here can run early
enough. `app.state.gateway_catalog`, which `GET /v1/gateway/models` reads, comes
through `bind_to_app_state` like every other shared service.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from aleph_api.a2ui_handlers import build_action_router
from aleph_api.settings import Settings, get_settings
from aleph_kernel import Context, EffectScope, Kernel
from aleph_kernel.manifest import load_manifest, mount_manifest
from aleph_runtime.capabilities import (
    AGENT_STORE,
    AGENT_STORE_POOL,
    BOUND_KEYS,
    DB_SESSIONS,
    LITELLM,
    SETTINGS,
    bind_to_app_state,
)

#: Ships beside the app, not in the working directory: the set of core
#: capabilities must not depend on where the process happened to start.
BOOT_MANIFEST = Path(__file__).resolve().parents[2] / "aleph.toml"

_log = structlog.get_logger(__name__)

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

    try:
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

        # Startup reconciliation. A run left in `running` by a process that died
        # is indistinguishable from work still in flight, so nothing reports it
        # and the UI shows a spinner forever. Forty-five stuck `chunk_embed`
        # runs — every one a failed index — is what that looked like in
        # production. Reaping is best-effort: it must never stop the API from
        # coming up, but a failure to reap is itself worth a log line.
        try:
            from aleph_db.repos.agent_runs import reap_stale_runs

            async with session_maker() as session:
                reaped = await reap_stale_runs(session)
                await session.commit()
            if reaped:
                _log.warning("api.boot.reaped_stale_runs", count=reaped)
        except Exception:
            _log.exception("api.boot.reap_failed")

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

        from aleph_api.copilot_agent import bind_runtime, build_agent_checkpointer
        from aleph_api.copilotkit_endpoint import setup_copilotkit

        bind_runtime(
            session_maker=session_maker,
            settings=settings,
            litellm=reader.get(LITELLM),
            agent_bindings=agent_bindings,
        )

        # Durable per-thread conversation state, sharing the agent store's
        # already-open pool. `setup()` creates the saver's own tables; without
        # this the agent falls back to in-memory state and every restart drops
        # its history, its `write_todos` plan, and the summarization archive.
        agent_checkpointer = build_agent_checkpointer(reader.get(AGENT_STORE_POOL))
        await agent_checkpointer.setup()
        app.state.agent_checkpointer = agent_checkpointer

        # Mounts routes on `app`; not modelled as a capability because adding a
        # route to a running FastAPI app has no meaningful inverse.
        setup_copilotkit(
            app,
            settings=settings,
            store=reader.get(AGENT_STORE),
            checkpointer=agent_checkpointer,
            session_maker=session_maker,
        )

        yield
    finally:
        # Dependents before providers, every inverse runs, failures aggregate.
        await kernel.shutdown()


def app_settings(app: FastAPI) -> Settings:
    return app.state.settings


__all__ = ["SETTINGS", "app_settings", "lifespan"]
