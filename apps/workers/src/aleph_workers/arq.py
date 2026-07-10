"""Arq WorkerSettings — startup, shutdown, function registration."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings

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
from aleph_rks.asset_store import create_asset_store
from aleph_scholar import ScholarService
from aleph_workers.jobs import (
    bootstrap_project_job,
    builder_job,
    chunk_embed_job,
    curate_page_job,
    deep_research_job,
    editorial_review_job,
    mechanical_review_job,
    normalize_job,
    reembed_job,
    refresh_stale_pages_job,
    render_code_artifact_job,
    smoke_llm_job,
    wiki_ingest_job,
    wiki_refresh_job,
)
from aleph_workers.settings import get_worker_settings


async def _startup(ctx: dict[str, Any]) -> None:
    s = get_worker_settings()
    configure_logging(environment=s.aleph_env)
    init_otel(
        service_name=s.service_name,
        service_version=s.service_version,
        environment=s.aleph_env,
        profile=s.aleph_default_model_profile,
        otlp_endpoint=s.otel_exporter_otlp_endpoint,
    )
    init_langfuse(
        host=s.langfuse_host,
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
    )
    engine = async_engine_for(s.database_url)
    instrument_sqlalchemy(engine)
    maker = async_sessionmaker_for(engine)
    instrument_httpx()
    gateway_http = httpx.AsyncClient()
    redis = aioredis.from_url(s.redis_url, decode_responses=False)
    # The plain Redis client above is stashed on the job ctx (`ctx["redis"]`) for
    # jobs that need direct key access. Job-to-job enqueue (normalize → chunk →
    # wiki) needs an ArqRedis pool, which only `arq.create_pool` returns.
    redis_pool = await create_pool(RedisSettings.from_dsn(s.redis_url))
    # Dedicated pool for dispatching code_runner jobs on the isolated code-job
    # Redis (the sandbox shares that bus; it must never reach the platform
    # Redis above, which carries tokens + privileged queues). WP-4c §6.
    code_runner_pool = await create_pool(RedisSettings.from_dsn(s.code_runner_redis_url))
    litellm = LiteLLMClient(
        base_url=s.litellm_base_url,
        api_key=s.insights_litellm_api_key,
        http_client=gateway_http,
        pricing=get_default_pricing(),
        session_maker=maker,
    )
    asset_store = create_asset_store(
        backend=s.aleph_asset_backend,
        root=s.aleph_asset_root,
        endpoint=s.aleph_s3_endpoint,
        access_key=s.aleph_s3_access_key,
        secret_key=s.aleph_s3_secret_key,
        bucket=s.aleph_s3_bucket,
        secure=s.aleph_s3_secure,
    )
    # Scholar (WP-2): DOI verification for the MechanicalReviewer. The
    # settings key is being added by a parallel work package — fall back to
    # the spec default until it lands.
    scholar_mailto: str = getattr(s, "aleph_scholar_mailto", "dev@aleph.local")
    scholar = ScholarService(mailto=scholar_mailto)
    ctx["settings"] = s
    ctx["scholar"] = scholar
    ctx["db_engine"] = engine
    ctx["session_maker"] = maker
    ctx["litellm_client"] = litellm
    ctx["gateway_http"] = gateway_http
    ctx["redis"] = redis
    ctx["redis_pool"] = redis_pool
    ctx["code_runner_pool"] = code_runner_pool
    ctx["asset_store"] = asset_store
    ctx["agent_token_secret"] = s.aleph_agent_token_secret


async def _shutdown(ctx: dict[str, Any]) -> None:
    await ctx["scholar"].http.aclose()
    await ctx["gateway_http"].aclose()
    await ctx["redis"].aclose()
    await ctx["redis_pool"].aclose()
    await ctx["code_runner_pool"].aclose()
    await ctx["db_engine"].dispose()
    shutdown_langfuse()
    shutdown_otel()


def _redis_from_url(url: str) -> RedisSettings:
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    functions: ClassVar = [
        smoke_llm_job,
        normalize_job,
        chunk_embed_job,
        wiki_ingest_job,
        mechanical_review_job,
        editorial_review_job,
        builder_job,
        deep_research_job,
        bootstrap_project_job,
        curate_page_job,
        reembed_job,
        render_code_artifact_job,
        wiki_refresh_job,
        refresh_stale_pages_job,
    ]
    on_startup = _startup
    on_shutdown = _shutdown
    redis_settings = _redis_from_url(get_worker_settings().redis_url)
    max_jobs = get_worker_settings().arq_max_jobs
    job_timeout = 600
    keep_result = 3600
