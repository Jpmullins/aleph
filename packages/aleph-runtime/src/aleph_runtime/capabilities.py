"""The API's shared services, as kernel capabilities.

Replaces a hand-wired startup sequence and a hand-ordered teardown chain. Two
concrete bugs went with it:

* **A failing teardown stranded every teardown after it.** The old `finally` was
  a bare sequence of awaits, so one raising `aclose()` skipped the rest —
  leaking the Redis connection, the engine pool and the HTTP clients.
* **A failing constructor leaked everything built before it.** Half the
  singletons were created before the `try`, so a misconfigured asset backend
  (which raises by design) left the engine, three HTTP clients and the Redis
  connection open with nothing to close them.

Both are structural now: an inverse is authored next to the thing it undoes, and
`EffectScope` unwinds LIFO, runs every inverse even when one raises, and unwinds
whatever a partially-completed setup had built.

Every capability carries a probe that exercises its READ path against the live
system. A capability that cannot answer a real query does not come up.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import text

from aleph_core.ids import uuid7
from aleph_db.session import async_engine_for, async_sessionmaker_for
from aleph_kernel import CapabilitySpec, Context, ProbeResult, ok, problem
from aleph_models.client import LiteLLMClient
from aleph_models.discovery import GatewayCatalog
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
from aleph_rks.asset_store import AssetStore, create_asset_store
from aleph_scholar import ScholarService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

#: Structurally typed rather than imported: the API and the workers each have
#: their own Settings class, and a package may not import from an app. Every
#: capability below reads attributes off whichever one it is handed.
Settings = Any

Inv = "Callable[[], Awaitable[None]]"

# Service keys. Dotted so a capability's provisions read as a namespace, and
# stable because `requires` strings are the coupling between capabilities.
SETTINGS = "settings"
DB_ENGINE = "db.engine"
DB_SESSIONS = "db.sessions"
HTTP_GATEWAY = "http.gateway"
HTTP_AUTH = "http.auth"
HTTP_CONSENSUS = "http.consensus_token"
REDIS = "redis"
LITELLM = "litellm"
PRICING = "pricing"
GATEWAY_CATALOG = "models.gateway_catalog"
ASSET_STORE = "asset_store"
SCHOLAR = "scholar"
CHANGE_BROKER = "realtime.broker"
NOTIFY_LISTENER = "realtime.listener"
AGENT_STORE = "agent.store"
AGENT_STORE_POOL = "agent.store_pool"


def observability(settings: Settings) -> CapabilitySpec:
    """Logging, tracing and Langfuse. Provides settings to everything else."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        configure_logging(environment=settings.aleph_env)
        init_otel(
            service_name=settings.service_name,
            service_version=settings.service_version,
            environment=settings.aleph_env,
            profile=settings.aleph_default_model_profile,
            otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        )

        async def _otel_down() -> None:
            shutdown_otel()

        yield _otel_down

        # Same opt-in rule as the OTLP exporter above: Langfuse lives behind
        # `--profile tracing`, and initialising a client against a host that is
        # not running makes every process retry a DNS name that does not
        # resolve, forever. Keys are what gate it — a deployment that wants
        # tracing has them, and one that does not has nothing to send.
        # `CHANGE-ME` counts as unset. `.env.example` ships placeholder keys and
        # the bootstrap copies it verbatim, so a truthiness check alone would
        # call every default install "configured" and then spend its life
        # retrying a host that is not running.
        def _configured(value: str) -> bool:
            v = value.strip()
            return bool(v) and "CHANGE-ME" not in v

        langfuse_enabled = (
            _configured(settings.langfuse_host)
            and _configured(settings.langfuse_public_key)
            and _configured(settings.langfuse_secret_key)
        )
        if langfuse_enabled:
            init_langfuse(
                host=settings.langfuse_host,
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
            )

        async def _langfuse_down() -> None:
            if langfuse_enabled:
                shutdown_langfuse()

        yield _langfuse_down

        instrument_httpx()
        ctx.provide(SETTINGS, settings)

    async def probe(ctx: Context) -> ProbeResult:
        # A real read: emit a span and confirm the tracer produced a live
        # context. A configured-but-inert tracer is the failure this catches.
        from aleph_observability.tracing import current_trace_id, start_span

        with start_span("kernel.probe.observability"):
            trace_id = current_trace_id()
        if not trace_id:
            return problem("tracing is initialised but produced no trace id inside a span")
        return ok(f"tracing live (trace {trace_id[:8]})")

    return CapabilitySpec(
        name="observability", setup=setup, probe=probe, provides=frozenset({SETTINGS})
    )


def database() -> CapabilitySpec:
    """The engine and session factory."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        settings: Settings = ctx.get(SETTINGS)
        engine = async_engine_for(settings.database_url)
        instrument_sqlalchemy(engine)

        async def _dispose() -> None:
            await engine.dispose()

        yield _dispose

        ctx.provide(DB_ENGINE, engine)
        ctx.provide(DB_SESSIONS, async_sessionmaker_for(engine))

    async def probe(ctx: Context) -> ProbeResult:
        # Round-trip a query. "The engine object exists" is not evidence that a
        # connection can be opened, which is the thing every other capability
        # is about to assume.
        maker = ctx.get(DB_SESSIONS)
        async with maker() as session:
            value = (await session.execute(text("SELECT 1"))).scalar_one()
        if value != 1:
            return problem(f"SELECT 1 returned {value!r}")
        return ok("connection round-tripped")

    return CapabilitySpec(
        name="database",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({DB_ENGINE, DB_SESSIONS}),
    )


def http_clients() -> CapabilitySpec:
    """The three shared httpx clients, each closed by its own inverse."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        gateway = httpx.AsyncClient()
        yield gateway.aclose
        auth = httpx.AsyncClient()
        yield auth.aclose
        # The live Consensus AS 308-redirects its token endpoint to a
        # trailing-slash path; 308 preserves method and body.
        consensus = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        yield consensus.aclose

        ctx.provide(HTTP_GATEWAY, gateway)
        ctx.provide(HTTP_AUTH, auth)
        ctx.provide(HTTP_CONSENSUS, consensus)

    async def probe(ctx: Context) -> ProbeResult:
        for key in (HTTP_GATEWAY, HTTP_AUTH, HTTP_CONSENSUS):
            client: httpx.AsyncClient = ctx.get(key)
            if client.is_closed:
                return problem(f"{key} was already closed at probe time")
        return ok("three clients open")

    return CapabilitySpec(
        name="http",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({HTTP_GATEWAY, HTTP_AUTH, HTTP_CONSENSUS}),
    )


def redis_client() -> CapabilitySpec:
    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        settings: Settings = ctx.get(SETTINGS)
        client = aioredis.from_url(settings.redis_url, decode_responses=False)
        yield client.aclose
        ctx.provide(REDIS, client)

    async def probe(ctx: Context) -> ProbeResult:
        client = ctx.get(REDIS)
        if not await client.ping():
            return problem("redis did not answer PING")
        return ok("PING answered")

    return CapabilitySpec(
        name="redis",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({REDIS}),
    )


def models() -> CapabilitySpec:
    """The LLM gateway client, its pricing table, and the catalog behind both."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        settings: Settings = ctx.get(SETTINGS)
        # Rates are learned from the gateway, not shipped: `get_default_pricing`
        # returns an EMPTY table. Discovery has to run here, inside the
        # capability that owns the table, because the probe below reads it and
        # nothing outside can populate it before that probe runs.
        #
        # The table is refreshed **in place**, so the client built from it a few
        # lines down needs no rebuilding when rates arrive. `refresh_pricing`
        # logs rather than raises: an unreachable gateway must not stop a
        # process booting, and calls made meanwhile record
        # `pricing_source="unknown"` instead of a fabricated $0.
        pricing = get_default_pricing()
        catalog = GatewayCatalog(
            base_url=settings.litellm_base_url,
            api_key=settings.insights_litellm_api_key,
            client=ctx.get(HTTP_GATEWAY),
        )
        await catalog.refresh_pricing(pricing)
        client = LiteLLMClient(
            base_url=settings.litellm_base_url,
            api_key=settings.insights_litellm_api_key,
            http_client=ctx.get(HTTP_GATEWAY),
            pricing=pricing,
            session_maker=ctx.get(DB_SESSIONS),
            redis_client=ctx.get(REDIS),
        )
        ctx.provide(PRICING, pricing)
        ctx.provide(LITELLM, client)
        # The same catalog the Settings model picker reads
        # (`GET /v1/gateway/models`) — Aleph ships no committed model list, so
        # one cached view of the gateway serves discovery, pricing and the UI.
        ctx.provide(GATEWAY_CATALOG, catalog)
        if False:  # pragma: no cover - the shared http client owns the socket
            yield

    async def probe(ctx: Context) -> ProbeResult:
        # The read path every LLM call starts from: resolve the default profile
        # out of the database and confirm it names models at all. A profile that
        # is missing or empty cannot resolve a capability, so the first call
        # would fail — that is what this refuses to come up with.
        from aleph_db.repos.model_profile import get_template

        settings: Settings = ctx.get(SETTINGS)
        pricing = ctx.get(PRICING)
        maker = ctx.get(DB_SESSIONS)

        async with maker() as session:
            template = await get_template(session, settings.aleph_default_model_profile)
        if template is None:
            return problem(
                f"default model profile {settings.aleph_default_model_profile!r} is not seeded; "
                f"capability resolution would fail on the first LLM call"
            )

        # bindings_jsonb is {capability: {"model": ..., "fallback": ..., ...}};
        # the fallback counts too, since a call that falls back still bills.
        bound: set[str] = set()
        for binding in template.bindings_jsonb.values():
            if not isinstance(binding, dict):
                continue
            for field_name in ("model", "fallback"):
                value = binding.get(field_name)
                if isinstance(value, str) and value.strip():
                    bound.add(value.strip())
        if not bound:
            return problem(f"profile {settings.aleph_default_model_profile!r} binds no models")

        # Whether every bound model carries a rate is reported, not enforced.
        #
        # It used to be enforced, when the pricing table was a static list
        # shipped in the repo and an unknown model silently cost $0. Neither
        # premise holds now: rates come from whichever gateway this deployment
        # points at, so an empty table means only that discovery has not
        # succeeded yet (unreachable gateway, or the ids-only `/v1/models`
        # fallback — `refresh_pricing` swallows both by design), and an unpriced
        # call is no longer silent — `PricingTable.breakdown` returns
        # `priced=False, source="unknown"`, which `LiteLLMClient` logs and
        # persists on every `ModelCall`.
        #
        # Enforcing it would also deadlock the fix: a seeded profile naming
        # models this gateway does not serve is exactly what
        # `POST /v1/projects/{id}/model-profile/autoconfigure` repairs, and that
        # endpoint lives behind the process that would be refusing to boot.
        priced_count = len(pricing.models())
        unpriced = sorted(m for m in bound if not pricing.has(m))
        if unpriced:
            return ok(
                f"{len(bound)} bound models, {priced_count} priced by the gateway; "
                f"unpriced (will record pricing_source=unknown): {', '.join(unpriced)}"
            )
        return ok(f"{len(bound)} bound models all priced from {priced_count} gateway rates")

    return CapabilitySpec(
        name="models",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS, HTTP_GATEWAY, DB_SESSIONS, REDIS}),
        provides=frozenset({LITELLM, PRICING, GATEWAY_CATALOG}),
    )


def assets() -> CapabilitySpec:
    """The asset store. Fails fast on a misconfigured backend, by design."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        settings: Settings = ctx.get(SETTINGS)
        store: AssetStore = create_asset_store(
            backend=settings.aleph_asset_backend,
            root=settings.aleph_asset_root,
            endpoint=settings.aleph_s3_endpoint,
            access_key=settings.aleph_s3_access_key,
            secret_key=settings.aleph_s3_secret_key,
            bucket=settings.aleph_s3_bucket,
            secure=settings.aleph_s3_secure,
        )
        ctx.provide(ASSET_STORE, store)
        if False:  # pragma: no cover
            yield

    async def probe(ctx: Context) -> ProbeResult:
        # A genuine round-trip through the real backend. Every asset byte in the
        # product takes this path, and a store that constructs but cannot serve
        # is exactly the failure a constructor check would miss.
        #
        # `get(..., expected_sha256=...)` makes this stronger than a byte
        # compare: it exercises the store's own integrity verification, so a
        # backend that silently truncates fails here rather than at read time.
        # The store has no delete, so the probe writes one stable key that each
        # boot overwrites instead of accumulating.
        store: AssetStore = ctx.get(ASSET_STORE)
        payload = b"aleph-kernel-probe"
        try:
            stored = store.put_bytes(
                key="kernel-probe/boot", data=payload, mime_type="application/octet-stream"
            )
            got = store.get(stored.storage_uri, expected_sha256=stored.sha256)
        except Exception as exc:  # a store that raises here cannot serve assets
            return problem(f"asset round-trip raised {exc!r}")
        if got != payload:
            return problem(f"asset round-trip returned {len(got)} bytes, expected {len(payload)}")
        return ok(f"round-tripped {stored.size_bytes} bytes with sha256 verification")

    return CapabilitySpec(
        name="assets",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({ASSET_STORE}),
    )


def scholar() -> CapabilitySpec:
    """One shared ScholarService, so the polite throttle lives for the app's lifetime."""

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        settings: Settings = ctx.get(SETTINGS)
        service = ScholarService(
            mailto=settings.aleph_scholar_mailto,
            redis=ctx.get(REDIS),
            consensus_monthly_cap=settings.aleph_consensus_monthly_search_cap,
            consensus_token_http=ctx.get(HTTP_CONSENSUS),
        )
        yield service.http.aclose
        ctx.provide(SCHOLAR, service)

    async def probe(ctx: Context) -> ProbeResult:
        # No network — a boot probe must not depend on Crossref being up. Probe
        # the local invariant that actually breaks silently: the DOI extractor.
        # If this regexes wrong, every citation in the product goes unverified
        # and nothing raises.
        from aleph_scholar.dois import extract_dois

        found = extract_dois("see 10.1234/abc-1 and https://doi.org/10.5678/xyz for detail")
        if sorted(found) != ["10.1234/abc-1", "10.5678/xyz"]:
            return problem(f"DOI extraction returned {found!r}")
        return ok("DOI extraction correct")

    return CapabilitySpec(
        name="scholar",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS, REDIS, HTTP_CONSENSUS}),
        provides=frozenset({SCHOLAR}),
    )


def realtime() -> CapabilitySpec:
    """The LISTEN/NOTIFY listener and the per-project change broker.

    Its inverse was the first line of the old teardown chain, so anything that
    raised here stranded every close after it.
    """

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        from aleph_api.realtime import ChangeBroker, NotifyListener, asyncpg_dsn

        settings: Settings = ctx.get(SETTINGS)
        broker = ChangeBroker()
        listener = NotifyListener(dsn=asyncpg_dsn(settings.database_url), broker=broker)
        await listener.start()
        yield listener.stop

        ctx.provide(CHANGE_BROKER, broker)
        ctx.provide(NOTIFY_LISTENER, listener)

    async def probe(ctx: Context) -> ProbeResult:
        # Publish through the broker and require a real subscriber to receive
        # it. "The listener object exists" says nothing about whether a change
        # signal reaches an SSE stream, which is the entire job.
        broker = ctx.get(CHANGE_BROKER)
        project_id = uuid7()
        async with broker.subscribe(project_id) as subscription:
            broker.publish(project_id, {"kind": "kernel.probe"})
            delivered = subscription.drain()
        if [s.get("kind") for s in delivered] != ["kernel.probe"]:
            return problem(f"a published change did not reach a live subscriber: {delivered!r}")
        if broker.subscriber_count(project_id) != 0:
            return problem("the probe's subscription outlived its context")
        return ok("broker delivered to a subscriber and cleaned up")

    return CapabilitySpec(
        name="realtime",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({CHANGE_BROKER, NOTIFY_LISTENER}),
    )


def agent_store() -> CapabilitySpec:
    """The Postgres-backed cross-session store for the assistant agent.

    Must be built inside the running loop, which is why it was never a
    module-level singleton.
    """

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        from aleph_api.copilot_agent import build_agent_store

        settings: Settings = ctx.get(SETTINGS)
        pool, store = build_agent_store(database_url=settings.database_url, settings=settings)
        await pool.open()
        # Registered immediately after open() so a failure in setup() below
        # cannot leak the pool's background task and connections — the old code
        # relied on a `try` placed just so.
        yield pool.close
        await store.setup()

        ctx.provide(AGENT_STORE_POOL, pool)
        ctx.provide(AGENT_STORE, store)

    async def probe(ctx: Context) -> ProbeResult:
        # Round-trip a namespaced item. `setup()` creating tables is not
        # evidence the store can serve a get, which is what every agent turn
        # depends on.
        store = ctx.get(AGENT_STORE)
        namespace = ("kernel-probe",)
        try:
            await store.aput(namespace, "boot", {"ok": True})
            got = await store.aget(namespace, "boot")
        except Exception as exc:
            return problem(f"agent-store round-trip raised {exc!r}")
        finally:
            with suppress(Exception):
                await store.adelete(namespace, "boot")
        if got is None or got.value.get("ok") is not True:
            return problem(f"agent-store returned {got!r}")
        return ok("put/get/delete round-tripped")

    return CapabilitySpec(
        name="agent_store",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({AGENT_STORE, AGENT_STORE_POOL}),
    )


def core_capabilities(settings: Settings) -> tuple[CapabilitySpec, ...]:
    """Everything the API mounts at boot, in no particular order.

    Order is computed from the declarations, not from this tuple — which is the
    point. Adding a capability here does not require knowing where in a sequence
    it belongs.
    """
    return (
        observability(settings),
        database(),
        http_clients(),
        redis_client(),
        models(),
        assets(),
        scholar(),
        realtime(),
        agent_store(),
    )


def bind_to_app_state(app: Any, ctx: Context) -> None:
    """Publish resolved services onto `app.state` for existing call sites.

    A shim, deliberately. Routes read `request.app.state.litellm` today; moving
    ~200 call sites onto context resolution is a separate change from proving
    the kernel boots the app. This keeps that diff honest and small.
    """
    app.state.settings = ctx.get(SETTINGS)
    app.state.db_engine = ctx.get(DB_ENGINE)
    app.state.session_maker = ctx.get(DB_SESSIONS)
    app.state.litellm = ctx.get(LITELLM)
    app.state.pricing = ctx.get(PRICING)
    app.state.gateway_catalog = ctx.get(GATEWAY_CATALOG)
    app.state.redis = ctx.get(REDIS)
    app.state.gateway_http = ctx.get(HTTP_GATEWAY)
    app.state.auth_http = ctx.get(HTTP_AUTH)
    app.state.consensus_token_http = ctx.get(HTTP_CONSENSUS)
    app.state.asset_store = ctx.get(ASSET_STORE)
    app.state.scholar = ctx.get(SCHOLAR)
    app.state.change_broker = ctx.get(CHANGE_BROKER)
    app.state.notify_listener = ctx.get(NOTIFY_LISTENER)
    app.state.agent_store = ctx.get(AGENT_STORE)
    app.state.agent_store_pool = ctx.get(AGENT_STORE_POOL)


#: Everything `bind_to_app_state` reads, so the observer context can declare it.
BOUND_KEYS = frozenset(
    {
        SETTINGS,
        DB_ENGINE,
        DB_SESSIONS,
        LITELLM,
        PRICING,
        GATEWAY_CATALOG,
        REDIS,
        HTTP_GATEWAY,
        HTTP_AUTH,
        HTTP_CONSENSUS,
        ASSET_STORE,
        SCHOLAR,
        CHANGE_BROKER,
        NOTIFY_LISTENER,
        AGENT_STORE,
        AGENT_STORE_POOL,
    }
)

__all__ = [
    "AGENT_STORE",
    "AGENT_STORE_POOL",
    "ASSET_STORE",
    "BOUND_KEYS",
    "CHANGE_BROKER",
    "DB_ENGINE",
    "DB_SESSIONS",
    "GATEWAY_CATALOG",
    "HTTP_AUTH",
    "HTTP_CONSENSUS",
    "HTTP_GATEWAY",
    "LITELLM",
    "NOTIFY_LISTENER",
    "PRICING",
    "REDIS",
    "SCHOLAR",
    "SETTINGS",
    "bind_to_app_state",
    "core_capabilities",
]


# ---------------------------------------------------------------------------
# Worker-only capabilities. The API does not enqueue jobs, so it does not mount
# these — which is the point of a per-process manifest: two processes share the
# factories and differ in what they boot.
# ---------------------------------------------------------------------------

ARQ_POOL = "arq.pool"
CODE_RUNNER_POOL = "arq.code_runner_pool"


def arq_pool() -> CapabilitySpec:
    """The platform job bus.

    Distinct from the plain Redis client: job-to-job enqueue needs the ArqRedis
    pool that only `arq.create_pool` returns, while `LiteLLMClient` wants an
    ordinary client for idempotency caching. Both exist for real reasons and
    conflating them breaks one of the two.
    """

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        from arq import create_pool
        from arq.connections import RedisSettings

        settings: Settings = ctx.get(SETTINGS)
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        yield pool.aclose
        ctx.provide(ARQ_POOL, pool)

    async def probe(ctx: Context) -> ProbeResult:
        pool = ctx.get(ARQ_POOL)
        if not await pool.ping():
            return problem("the job bus did not answer PING")
        return ok("job bus reachable")

    return CapabilitySpec(
        name="arq_pool",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({ARQ_POOL}),
    )


def code_runner_pool() -> CapabilitySpec:
    """The isolated bus the sandbox shares.

    Deliberately a separate Redis from the platform one, which carries agent
    tokens and privileged queues. The sandbox must never reach that, so the
    separation is a security boundary rather than a scaling choice — and a probe
    that accidentally proved connectivity to the WRONG bus would hide exactly
    the misconfiguration that matters, so it checks the configured URL differs.
    """

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        from arq import create_pool
        from arq.connections import RedisSettings

        settings: Settings = ctx.get(SETTINGS)
        pool = await create_pool(RedisSettings.from_dsn(settings.code_runner_redis_url))
        yield pool.aclose
        ctx.provide(CODE_RUNNER_POOL, pool)

    async def probe(ctx: Context) -> ProbeResult:
        settings: Settings = ctx.get(SETTINGS)
        if settings.code_runner_redis_url == settings.redis_url:
            return problem(
                "the code-runner bus is the same Redis as the platform bus; the "
                "sandbox would share a channel with agent tokens and privileged queues"
            )
        pool = ctx.get(CODE_RUNNER_POOL)
        if not await pool.ping():
            return problem("the code-runner bus did not answer PING")
        return ok("code-runner bus reachable and separate from the platform bus")

    return CapabilitySpec(
        name="code_runner_pool",
        setup=setup,
        probe=probe,
        requires=frozenset({SETTINGS}),
        provides=frozenset({CODE_RUNNER_POOL}),
    )
