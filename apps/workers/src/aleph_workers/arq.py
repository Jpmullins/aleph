"""Arq WorkerSettings — startup, shutdown, function registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from arq.connections import RedisSettings

from aleph_kernel import Context, EffectScope, Kernel
from aleph_kernel.manifest import load_manifest, mount_manifest
from aleph_runtime.capabilities import (
    ARQ_POOL,
    ASSET_STORE,
    CODE_RUNNER_POOL,
    DB_ENGINE,
    DB_SESSIONS,
    HTTP_GATEWAY,
    PRICING,
    REDIS,
    SCHOLAR,
    SETTINGS,
)
from aleph_workers.gateway import GATEWAYS_KEY, WorkerGateways
from aleph_workers.jobs import (
    autoconfigure_profile_job,
    backfill_index_job,
    background_task_job,
    bootstrap_project_job,
    builder_job,
    chunk_embed_job,
    curate_page_job,
    deep_research_job,
    delegated_subagent_job,
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

#: Ships beside the worker, not in the working directory: what a process boots
#: must not depend on where it was started from.
BOOT_MANIFEST = Path(__file__).resolve().parents[2] / "aleph.toml"


async def _startup(ctx: dict[str, Any]) -> None:
    """Boot the worker's capabilities from its manifest.

    Previously this hand-constructed nine singletons and `_shutdown` closed them
    in a bare await chain — so one raising `aclose()` stranded every close after
    it, leaking the engine pool and both job buses. The API had the identical
    bug in its own copy of this code, which is what made it worth moving the
    factories into `aleph-runtime`: fixed twice, or not at all.

    The worker mounts a different set than the API. It has no HTTP surface and
    no realtime listener; it has the two job buses, which the API does not.
    """
    settings = get_worker_settings()
    kernel = Kernel()
    mount_manifest(kernel, load_manifest(BOOT_MANIFEST), settings=settings)
    await kernel.boot()

    reader = Context(
        owner="aleph-workers",
        requires=frozenset(
            {
                SETTINGS,
                DB_ENGINE,
                DB_SESSIONS,
                HTTP_GATEWAY,
                REDIS,
                PRICING,
                ASSET_STORE,
                SCHOLAR,
                ARQ_POOL,
                CODE_RUNNER_POOL,
            }
        ),
        store=kernel.store,
        scope=EffectScope("aleph-workers"),
    )

    ctx["kernel"] = kernel
    ctx["settings"] = settings
    ctx["scholar"] = reader.get(SCHOLAR)
    ctx["db_engine"] = reader.get(DB_ENGINE)
    ctx["session_maker"] = reader.get(DB_SESSIONS)
    ctx["gateway_http"] = reader.get(HTTP_GATEWAY)
    ctx["redis"] = reader.get(REDIS)
    # WS-MEP-4. There is deliberately no `ctx["litellm_client"]` and no
    # `ctx["gateway_catalog"]` any more: those were ONE client and ONE catalog
    # built at boot from `LITELLM_BASE_URL`, handed to every job for every
    # project, so a project's `gateway_endpoints` row reached the settings
    # screen and none of its background traffic. A job asks this object which
    # gateway a given project talks to, and gets a client cached per endpoint.
    #
    # Workers still price calls from the same discovered rates the API uses:
    # the `models` capability runs discovery and refreshes this table in place,
    # so a worker-side ModelCall is priced rather than recording
    # pricing_source="unknown" — the research loop is the heaviest spender.
    ctx["pricing"] = reader.get(PRICING)
    ctx[GATEWAYS_KEY] = WorkerGateways(
        settings=settings,
        session_maker=reader.get(DB_SESSIONS),
        pricing=reader.get(PRICING),
        http_client=reader.get(HTTP_GATEWAY),
        redis_client=reader.get(REDIS),
    )
    ctx["redis_pool"] = reader.get(ARQ_POOL)
    ctx["code_runner_pool"] = reader.get(CODE_RUNNER_POOL)
    ctx["asset_store"] = reader.get(ASSET_STORE)
    ctx["agent_token_secret"] = settings.aleph_agent_token_secret


async def _shutdown(ctx: dict[str, Any]) -> None:
    """Dependents before providers, every inverse runs, failures aggregate.

    `ctx` may have no kernel: arq runs `on_shutdown` even when `on_startup`
    raised, and the kernel is stored only after `boot()` returns. Indexing
    blindly replaced the real diagnostic with `KeyError: 'kernel'` — a boot that
    failed on a named `ProbeFailed` reported a missing dict key instead, exactly
    when the operator needed the reason. Nothing to tear down is a normal
    outcome here, not an error.
    """
    resolver = ctx.get(GATEWAYS_KEY)
    if resolver is not None:
        # Before the kernel, because the clients it holds were built on the
        # `http` capability's pool: releasing them after that pool is closed is
        # the LIFO order this file was rewritten to stop getting wrong.
        await resolver.aclose()
    kernel = ctx.get("kernel")
    if kernel is None:
        return
    await kernel.shutdown()


def _redis_from_url(url: str) -> RedisSettings:
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    functions: ClassVar = [
        smoke_llm_job,
        autoconfigure_profile_job,
        backfill_index_job,
        background_task_job,
        delegated_subagent_job,
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
