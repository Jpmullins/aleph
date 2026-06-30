"""FastAPI lifespan: build shared singletons at startup, tear them down at shutdown."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from aleph_api.a2ui_handlers import build_action_router
from aleph_api.realtime import ChangeBroker, NotifyListener, asyncpg_dsn
from aleph_api.settings import Settings, get_settings
from aleph_db.session import async_engine_for, async_sessionmaker_for
from aleph_models.client import LiteLLMClient
from aleph_models.pricing import get_default_pricing
from aleph_observability import (
    configure_logging,
    init_langfuse,
    init_otel,
    instrument_httpx,
    instrument_sqlalchemy,
    shutdown_langfuse,
    shutdown_otel,
)
from aleph_rks.asset_store import AssetStore
from aleph_security.jwt import JWKSCache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    configure_logging(environment=settings.aleph_env)
    init_otel(
        service_name=settings.service_name,
        service_version=settings.service_version,
        environment=settings.aleph_env,
        profile=settings.aleph_default_model_profile,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    init_langfuse(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )

    engine: AsyncEngine = async_engine_for(settings.database_url)
    instrument_sqlalchemy(engine)
    session_maker = async_sessionmaker_for(engine)

    # Shared httpx clients — one for the gateway, one general-purpose.
    instrument_httpx()
    gateway_http = httpx.AsyncClient()
    auth_http = httpx.AsyncClient()

    # JWKS cache is only needed in OIDC mode; in local mode the auth
    # middleware bypasses JWT verification entirely.
    jwks_cache: JWKSCache | None = None
    if settings.aleph_auth_mode == "oidc":
        if not settings.aleph_auth_jwks_url:
            msg = "ALEPH_AUTH_JWKS_URL is required when ALEPH_AUTH_MODE=oidc"
            raise RuntimeError(msg)
        jwks_cache = JWKSCache(
            jwks_url=settings.aleph_auth_jwks_url,
            http_client=auth_http,
        )

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)

    pricing = get_default_pricing()
    litellm = LiteLLMClient(
        base_url=settings.litellm_base_url,
        api_key=settings.insights_litellm_api_key,
        http_client=gateway_http,
        pricing=pricing,
        session_maker=session_maker,
        redis_client=redis_client,
    )

    asset_store: AssetStore | None = None
    if (
        settings.minio_endpoint
        and settings.minio_root_user
        and settings.minio_root_password
        and settings.aleph_s3_bucket
    ):
        try:
            asset_store = AssetStore(
                endpoint=settings.minio_endpoint,
                access_key=settings.minio_root_user,
                secret_key=settings.minio_root_password,
                bucket=settings.aleph_s3_bucket,
                secure=False,
            )
        except Exception:
            asset_store = None

    app.state.settings = settings
    app.state.db_engine = engine
    app.state.session_maker = session_maker
    app.state.jwks_cache = jwks_cache

    # Wave 2 — give the in-process assistant Deep Agent (mounted at
    # /copilotkit/agent/assistant) access to the shared session_maker.
    from aleph_api.copilot_agent import bind_runtime

    # Rule #7: resolve the agent's model from the default named ModelProfile
    # (aleph-dev / aleph-production) rather than a hardcoded id, so the
    # conversational surface uses the configured tier (e.g. Opus in production).
    agent_bindings: dict | None = None
    try:
        from aleph_db.repos.model_profile import get_template

        async with session_maker() as _session:
            _tmpl = await get_template(_session, settings.aleph_default_model_profile)
            if _tmpl is not None:
                agent_bindings = dict(_tmpl.bindings_jsonb)
    except Exception:
        agent_bindings = None

    bind_runtime(
        session_maker=session_maker,
        settings=settings,
        litellm=litellm,
        agent_bindings=agent_bindings,
    )
    app.state.litellm = litellm
    app.state.redis = redis_client
    app.state.gateway_http = gateway_http
    app.state.auth_http = auth_http
    app.state.asset_store = asset_store
    app.state.action_router = build_action_router()

    # Real-time push layer: one supervised LISTEN/NOTIFY connection republishes
    # DB change signals (from the `realtime_notify_triggers` migration) to an
    # in-process per-project broker that the SSE streams subscribe to. Each
    # stream keeps a slow poll fallback so a dropped listener self-heals.
    change_broker = ChangeBroker()
    notify_listener = NotifyListener(dsn=asyncpg_dsn(settings.database_url), broker=change_broker)
    app.state.change_broker = change_broker
    app.state.notify_listener = notify_listener

    # Wave 6 D1 — cross-session memory for the assistant Deep Agent. The
    # Postgres-backed langgraph store (its own `store`/`store_migrations` tables)
    # must be constructed inside the running event loop, so it is built here
    # (not at synchronous app construction). Open the pool, create the tables
    # once via `setup()`, then mount the agent endpoint with this store. The
    # CompositeBackend routes `/memories/` to it for persistence across sessions.
    from aleph_api.copilot_agent import build_agent_store
    from aleph_api.copilotkit_endpoint import setup_copilotkit

    agent_store_pool, agent_store = build_agent_store(database_url=settings.database_url)

    try:
        # Inside the try so a failure in open()/setup()/setup_copilotkit cannot
        # leak the pool's background task + connections — the finally always
        # runs once we have a pool to close.
        await agent_store_pool.open()
        await agent_store.setup()
        app.state.agent_store_pool = agent_store_pool
        app.state.agent_store = agent_store
        setup_copilotkit(app, settings=settings, store=agent_store)
        await notify_listener.start()
        yield
    finally:
        await notify_listener.stop()
        await agent_store_pool.close()
        await gateway_http.aclose()
        await auth_http.aclose()
        await redis_client.aclose()
        await engine.dispose()
        shutdown_langfuse()
        shutdown_otel()


def app_settings(app: FastAPI) -> Settings:
    return app.state.settings
