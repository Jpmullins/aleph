"""Prometheus exposition of the API process's meters. NOT public.

## What "not public" can mean here, honestly

`docs/plan.md` WS-P9 asks for "a test asserting GET /metrics without a
credential in oidc mode returns 401". There is no oidc mode: it was removed
(`docs/decisions.md` D6). Aleph runs in `local` mode only, where
`AuthMiddleware` *synthesises* the dev principal for any request that does not
present an agent token — so "unauthenticated" is not a state a request can be
in, and a test asserting 401 in local mode could not be made to pass without
faking it. Writing that test would have been the exact failure this plan's
Part 0 is about: a gate that certifies something untrue.

So this route is defended by two independent things that ARE true, and both can
fail:

  1. It is **not** in `middleware/auth.py::_PUBLIC_PATHS`. It goes through the
     same middleware as every other route, so a malformed or foreign-project
     agent token is refused here exactly as it is everywhere else. Adding
     `/metrics` to that set — the obvious "fix" for a 403 in front of a
     scraper — turns this into an anonymous endpoint, and
     `test_metrics_not_public.py` fails if anyone does.
  2. A check in the handler itself, because (1) is worth little in local mode:
     port 8000 is published on 0.0.0.0 (`docker-compose.yml`), so without (2)
     anything on the network could scrape this.

## The rule (2) enforces

    ALEPH_METRICS_TOKEN set    → `Authorization: Bearer <token>` is required,
                                 compared in constant time. Loopback is not a
                                 bypass.
    ALEPH_METRICS_TOKEN unset  → the peer must be loopback.

The unset case is what makes the default deployment safe with no configuration:
inside the container, `curl localhost:8000/metrics` works, so a sidecar in the
same network namespace or `docker compose exec` can scrape. A request that
arrives from outside the container — including `curl localhost:8000` on the
*host*, which Docker NATs and which therefore arrives from the bridge gateway,
not from loopback — is refused until an operator sets a token. That is
deliberate: "it happens to work from my laptop" is how a metrics endpoint ends
up readable from the LAN.

Caveat worth knowing: uvicorn rewrites `scope["client"]` from `X-Forwarded-For`
when the immediate peer is in `--forwarded-allow-ips` (default `127.0.0.1`).
Under compose the peer is the bridge gateway, so the header is ignored. If you
ever set `FORWARDED_ALLOW_IPS=*`, the loopback rule becomes spoofable and the
token stops being optional.

## Why the gauges are refreshed here

Queue depth and background-run counts are *pull* metrics: the truth lives in
Redis and Postgres, not in this process's memory. Counting them at write time
would be wrong the moment a worker restarted. They are read at scrape time and
handed to the meter just before rendering — see `aleph_observability.metrics`.
Both reads are best-effort: a metrics endpoint that 500s because Redis is down
removes the instrument at the exact moment it is needed.

The same applies to the storage gauge. Bytes used live on the disk and in the
database catalog, not in this process — see `aleph_observability.storage` for
what the two labels mean and why a number that could not be read is *absent*
rather than zero.
"""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from aleph_api.routes.health import measure_storage
from aleph_core.errors import PermissionDenied
from aleph_db.models.agent import AgentRun
from aleph_observability.metrics import (
    render_prometheus,
    replace_agent_run_counts,
    replace_queue_depths,
    replace_storage_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["metrics"])

#: Env var, not a `Settings` field, deliberately. `test_env_settings_reconciled`
#: requires every `ALEPH_*` key in `.env.example` to map to a `Settings` field,
#: and this key is not in `.env.example`: an operator sets it only when they run
#: a scraper that is not on loopback. Promoting it to a settings field is the
#: right move the day it ships in the example env — see plan Part 4 item 2 for
#: the same trap catching `ALEPH_MODEL_HINTS_PATH`.
METRICS_TOKEN_ENV = "ALEPH_METRICS_TOKEN"

#: Peer addresses that count as "inside this container". `::ffff:127.0.0.1` is
#: the IPv4-mapped form uvicorn reports when it binds dual-stack, and leaving it
#: out makes the rule reject a genuinely local scrape depending on how the
#: socket was opened — a difference nobody would think to look for.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

#: arq's queue keys, by the label they are reported under. A fixed list, not a
#: `SCAN arq*`: that pattern also matches `arq:job:<id>`, `arq:result:<id>` and
#: `arq:queue:health-check`, which would put one series per job id into the
#: exposition — the unbounded-label failure this endpoint exists to avoid.
_ARQ_QUEUES: Mapping[str, str] = {
    "default": "arq:queue",
    "code_runner": "arq:queue:code_runner",
}


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Render every meter this process holds, in Prometheus exposition format."""
    _authorize(request)
    await _refresh_pull_gauges(request)
    payload, content_type = render_prometheus()
    return Response(content=payload, media_type=content_type)


def _authorize(request: Request) -> None:
    """Refuse a scrape that is neither token-bearing nor local. See module docs."""
    token = os.environ.get(METRICS_TOKEN_ENV, "").strip()
    if token:
        header = request.headers.get("authorization") or ""
        presented = header[7:].strip() if header[:7].lower() == "bearer " else ""
        # compare_digest, not `==`: the comparison is against a secret and the
        # endpoint is reachable by anything that can open a socket to it.
        if not secrets.compare_digest(presented, token):
            msg = f"/metrics requires the bearer token configured in {METRICS_TOKEN_ENV}"
            raise PermissionDenied(msg)
        return

    host = request.client.host if request.client is not None else ""
    if host not in _LOOPBACK:
        msg = (
            f"/metrics is loopback-only until {METRICS_TOKEN_ENV} is set; "
            f"this request came from {host or 'an unknown peer'}"
        )
        raise PermissionDenied(msg)


async def _refresh_pull_gauges(request: Request) -> None:
    replace_queue_depths(await _queue_depths(request))
    replace_agent_run_counts(await _agent_run_counts(request))
    series, errors = await measure_storage(request)
    if errors:
        # Absent from the exposition rather than zero — see `measure_storage`.
        _log.warning("metrics.storage_bytes_partial", errors=errors)
    replace_storage_bytes(series)


async def _queue_depths(request: Request) -> dict[str, int]:
    """Jobs waiting per arq queue, or `{}` if Redis cannot answer."""
    redis: Any = getattr(request.app.state, "redis", None)
    if redis is None:
        return {}
    depths: dict[str, int] = {}
    try:
        for label, key in _ARQ_QUEUES.items():
            # arq enqueues into a sorted set (the score is the run-at time), but
            # the key does not exist until the first job is queued and older arq
            # used a list. Reading the type first means a missing queue reports
            # 0 instead of raising WRONGTYPE and taking the whole scrape down.
            kind = await redis.type(key)
            kind_str = kind.decode() if isinstance(kind, bytes) else str(kind)
            if kind_str == "zset":
                depths[label] = int(await redis.zcard(key))
            elif kind_str == "list":
                depths[label] = int(await redis.llen(key))
            else:
                depths[label] = 0
    except Exception:
        _log.warning("metrics.queue_depth_unavailable", exc_info=True)
        return depths
    return depths


async def _agent_run_counts(request: Request) -> dict[tuple[str, str], int]:
    """Background runs grouped by `(agent_kind, status)`, or `{}` on failure.

    Read from Postgres rather than counted in worker memory, for two reasons:
    the count then survives a worker restart, and it reflects what
    `reap_stale_runs` did to runs whose owning process died — which is the
    single number that would have surfaced the 45 `chunk_embed` runs stuck in
    `running` with an empty index behind them (WS-RS1).

    Both columns are bounded by code (`agent_kind` is a literal at each enqueue
    site, `status` is a four-value vocabulary), so this cannot grow series.
    """
    maker: Any = getattr(request.app.state, "session_maker", None)
    if maker is None:
        return {}
    try:
        async with maker() as session:
            rows = (
                await session.execute(
                    select(AgentRun.agent_kind, AgentRun.status, func.count()).group_by(
                        AgentRun.agent_kind, AgentRun.status
                    )
                )
            ).all()
    except Exception:
        _log.warning("metrics.agent_run_counts_unavailable", exc_info=True)
        return {}
    return {(str(kind), str(status)): int(count) for kind, status, count in rows}
