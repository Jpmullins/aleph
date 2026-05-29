"""FastAPI lifespan: build shared singletons at startup, tear them down at shutdown."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine

from aleph_db.session import async_engine_for, async_sessionmaker_for
from aleph_models.client import LiteLLMClient
from aleph_models.pricing import get_default_pricing
from aleph_rks.asset_store import AssetStore
from aleph_observability import (
    configure_logging,
    init_langfuse,
    init_otel,
    instrument_httpx,
    instrument_sqlalchemy,
    shutdown_langfuse,
    shutdown_otel,
)
from aleph_security.jwt import JWKSCache

from aleph_api.a2ui_handlers import build_action_router
from aleph_api.settings import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: "FastAPI") -> "AsyncIterator[None]":
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
    if settings.minio_endpoint and settings.minio_root_user and settings.minio_root_password and settings.aleph_s3_bucket:
        try:
            asset_store = AssetStore(
                endpoint=settings.minio_endpoint,
                access_key=settings.minio_root_user,
                secret_key=settings.minio_root_password,
                bucket=settings.aleph_s3_bucket,
                secure=False,
            )
        except Exception:  # noqa: BLE001
            asset_store = None

    app.state.settings = settings
    app.state.db_engine = engine
    app.state.session_maker = session_maker
    app.state.jwks_cache = jwks_cache

    # Wave 2 — give the in-process assistant Deep Agent (mounted at
    # /copilotkit/agent/assistant) access to the shared session_maker.
    from aleph_api.copilot_agent import bind_runtime

    bind_runtime(session_maker=session_maker, settings=settings, litellm=litellm)
    app.state.litellm = litellm
    app.state.redis = redis_client
    app.state.gateway_http = gateway_http
    app.state.auth_http = auth_http
    app.state.asset_store = asset_store
    app.state.action_router = build_action_router()

    try:
        yield
    finally:
        await gateway_http.aclose()
        await auth_http.aclose()
        await redis_client.aclose()
        await engine.dispose()
        shutdown_langfuse()
        shutdown_otel()


def app_settings(app: "FastAPI") -> Settings:
    return app.state.settings
