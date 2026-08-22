"""Health and readiness endpoints. Public (no auth).

`/healthz` is liveness: the process is answering. `/readyz` is readiness, and
the two verdicts it can return are deliberately different questions.

**What `/readyz` decides.** Only the dependencies Aleph itself owns and can do
something about: Postgres, Redis, the asset store. The remote model gateway is
probed and reported with its own boolean, and is NOT in the verdict.

That is the fix for a real outage shape, not a preference. `docker-compose.yml`
wires `/readyz` as the API's healthcheck, `up -d --wait` blocks on it, and both
`web` and `copilot-runtime` declare `condition: service_healthy` on `api`. While
this endpoint folded `litellm_gateway` into `all_ok`, one wrong character in
`LITELLM_BASE_URL` stopped the *entire stack* from ever coming up — no web UI,
no API, and a `--wait` timeout as the only symptom, naming nothing. An
unreachable model endpoint is a degraded Aleph (retrieval degrades to
lexical-only, the workbench still loads, the wiki still reads) and not a dead
one. Restarting the API does not make somebody else's gateway reachable, so
putting it in the container's restart gate buys nothing and costs the stack.

**What `/readyz?strict=1` decides.** Everything above PLUS the gateway. That is
the endpoint for an operator asking "can this stack answer a question right
now", and nothing that restarts a container is wired to it.

**Why the gateway leg carries an age.** Reporting a leg outside the verdict is
one step from reporting it from a cache and letting it go stale — the gateway
catalog already holds a 300s view, and serving readiness from that would make
the stack report a healthy gateway for five minutes after the endpoint died.
So the leg publishes `checked_age_s`, `last_success_age_s` and `stale`, and its
`ok` is false whenever the last SUCCESSFUL probe is older than
`GATEWAY_MAX_AGE_S` — even if some future cached source still says true. A
boolean with no age attached cannot be distinguished from a stale one, and this
repo has already shipped one number that meant "nobody measured".

**Why the legs are timed out and run concurrently.** Four sequential legs, any
one of which can block on a network round trip, can outlast the healthcheck —
and a healthcheck that times out is reported as "unhealthy" with no body at
all, so the operator learns that something is wrong and not which thing. Each
leg gets its own budget and they run together, so `/readyz` answers within the
window and names the failing dependency even when the failure is a hang.

**The budget is the CLIENT's timeout, not docker's.** The first version of this
sized `LEG_TIMEOUT_S` against the compose `timeout: 8s` and missed the tighter
bound four lines away in the same change: the healthcheck command is
`urlopen(..., timeout=4)`. At 5.0s per leg a hung dependency produced a
correct, informative body 5 seconds in — one second after the client had given
up. Measured: `/readyz` returned 200 in 5.00s naming the timed-out leg, and the
exact compose command exited 1 after 4.08s having seen none of it.

That was worst for an OWNED leg. `_asset_store` against S3 is a real network
round trip with no cache, so every probe would have exceeded the client budget,
`retries: 10` would have accumulated, and the container would have gone
unhealthy with no body naming the dependency — verbatim the failure this design
exists to prevent.

`LEG_TIMEOUT_S` is therefore derived from `HEALTHCHECK_CLIENT_TIMEOUT_S` rather
than written next to it, so the two cannot drift apart again.

**Why the asset-store probe runs in a thread.** It is a synchronous filesystem
(or S3) write plus a read. Compose calls this every 15 seconds; on the event
loop that is four blocking round trips a minute stalling every other request in
the process, and against S3 it is a blocking socket.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from anyio import to_thread
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

router = APIRouter(tags=["health"])

#: How stale the gateway leg's answer is allowed to be, in seconds.
#:
#: Two constraints pin this. Below the compose healthcheck interval (15s) the
#: cache never hits and every probe is an outbound request; far above it, a dead
#: gateway keeps reporting `ok: true` long enough for an operator to trust it.
#: 30s means at most one wasted round trip per two healthchecks, and at most 30
#: seconds of lag between the endpoint dying and `/readyz` saying so.
GATEWAY_MAX_AGE_S = 30.0

#: What the compose healthcheck's own client allows before it gives up.
#:
#: `docker-compose.yml` runs `urlopen('http://localhost:8000/readyz', timeout=4)`
#: inside a step with `timeout: 8s`. The DOCKER timeout is the outer bound and
#: the CLIENT timeout is the one that bites first — sizing against 8s produced a
#: body nobody ever saw.
HEALTHCHECK_CLIENT_TIMEOUT_S = 4.0

#: Per-leg budget. Derived, not chosen: the legs run concurrently, so the whole
#: endpoint takes about one leg's time, and it has to finish and serialize
#: inside the client's window. The margin covers JSON encoding and the response
#: hop. Keep them derived — the previous pair of hand-picked constants drifted
#: apart within one change.
LEG_TIMEOUT_S = HEALTHCHECK_CLIENT_TIMEOUT_S - 1.5

#: The legs whose failure means *Aleph* is not ready. Everything else in
#: `checks` is reported and not voted on. See the module docstring.
OWNED_LEGS = ("postgres", "redis", "asset_store")


def _describe(exc: BaseException) -> str:
    """`str(TimeoutError())` is the empty string, which reads as 'no error'."""
    detail = str(exc)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@dataclass
class GatewayLeg:
    """The gateway's readiness answer, with the age of the answer attached.

    Holds the last probe result so repeated healthchecks do not each become an
    outbound request, and refuses to report a success older than `max_age_s`.
    `clock` is injectable so a test can advance time without sleeping.
    """

    max_age_s: float = GATEWAY_MAX_AGE_S
    timeout_s: float = LEG_TIMEOUT_S
    clock: Callable[[], float] = time.monotonic
    _ok: bool = False
    _error: str | None = None
    _checked_at: float | None = None
    _last_success_at: float | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _expired(self) -> bool:
        return self._checked_at is None or (self.clock() - self._checked_at) >= self.max_age_s

    async def check(self, probe: Callable[[], Awaitable[bool]]) -> dict[str, Any]:
        if self._expired():
            # Double-checked under the lock: a burst of concurrent healthchecks
            # must produce one outbound request, not one per caller.
            async with self._lock:
                if self._expired():
                    await self._probe(probe)

        now = self.clock()
        checked_age = 0.0 if self._checked_at is None else max(0.0, now - self._checked_at)
        success_age = (
            None if self._last_success_at is None else max(0.0, now - self._last_success_at)
        )
        # `stale` is computed from the last SUCCESS, not the last attempt: a leg
        # that is probed every 15s and fails every time is stale, which is the
        # honest reading of "nobody has confirmed this in N seconds".
        #
        # `ok` is deliberately NOT `self._ok and not stale`. It was, and that
        # conjunct was unreachable: `_expired()` re-probes at `max_age_s`, so a
        # recorded success can never be older than the bound while `_ok` is
        # still true, and no test could tell the two versions apart. Dead
        # defensive code that no test can fail is exactly the thing this repo
        # keeps finding, so it is gone.
        #
        # What actually enforces correction #5 is `_expired()` plus
        # `test_checked_age_never_exceeds_max_age`, which asserts the published
        # age against the published bound. Serve this leg from a longer-lived
        # cache — the gateway catalog's 300s view is the obvious temptation —
        # and that test goes red rather than the endpoint quietly reporting a
        # five-minute-old success.
        stale = success_age is None or success_age > self.max_age_s
        leg: dict[str, Any] = {
            "ok": self._ok,
            "checked_age_s": round(checked_age, 3),
            "max_age_s": self.max_age_s,
            "last_success_age_s": None if success_age is None else round(success_age, 3),
            "stale": stale,
        }
        if self._error is not None:
            leg["error"] = self._error
        return leg

    async def _probe(self, probe: Callable[[], Awaitable[bool]]) -> None:
        try:
            ok = bool(await asyncio.wait_for(probe(), timeout=self.timeout_s))
            self._error = None
        except Exception as exc:  # any failure to answer is "not reachable"
            ok = False
            self._error = _describe(exc)
        self._checked_at = self.clock()
        if ok:
            self._last_success_at = self._checked_at
        self._ok = ok


def _gateway_leg(request: Request) -> GatewayLeg:
    """The per-app leg, created on first use.

    Two concurrent first requests can each build one; the loser is discarded and
    the cost is one extra probe, so this is deliberately not locked.
    """
    leg = getattr(request.app.state, "readyz_gateway_leg", None)
    if not isinstance(leg, GatewayLeg):
        leg = GatewayLeg()
        request.app.state.readyz_gateway_leg = leg
    return leg


async def _leg(probe: Callable[[], Awaitable[bool]]) -> dict[str, Any]:
    """Run one dependency probe under its own budget, never raising."""
    try:
        return {"ok": bool(await asyncio.wait_for(probe(), timeout=LEG_TIMEOUT_S))}
    except Exception as exc:  # any failure to answer is "not ready"
        return {"ok": False, "error": _describe(exc)}


async def _postgres(request: Request) -> bool:
    maker = request.app.state.session_maker
    async with maker() as session:
        await session.execute(text("SELECT 1"))
    return True


async def _redis(request: Request) -> bool:
    await request.app.state.redis.ping()
    return True


async def _asset_store(request: Request) -> bool:
    store = request.app.state.asset_store

    def _round_trip() -> bool:
        probe = store.put_bytes(key=".readyz/probe", data=b"ok", mime_type="text/plain")
        return bool(store.get(probe.storage_uri) == b"ok")

    return await to_thread.run_sync(_round_trip)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, strict: bool = False) -> JSONResponse:
    """Readiness. `strict=1` folds the model gateway into the verdict.

    The default answer is the container's gate and covers only what Aleph owns.
    """
    postgres, redis, asset_store, gateway = await asyncio.gather(
        _leg(lambda: _postgres(request)),
        _leg(lambda: _redis(request)),
        _leg(lambda: _asset_store(request)),
        _gateway_leg(request).check(lambda: request.app.state.litellm.health()),
    )
    gateway["in_verdict"] = strict
    checks: dict[str, dict[str, Any]] = {
        "postgres": postgres,
        "redis": redis,
        "asset_store": asset_store,
        "litellm_gateway": gateway,
    }

    voting = (*OWNED_LEGS, "litellm_gateway") if strict else OWNED_LEGS
    all_ok = all(bool(checks[name]["ok"]) for name in voting)
    code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    body = {
        "status": "ready" if all_ok else "not_ready",
        # Named in the body so an operator reading a 200 with a false leg can
        # see, without reading this source, that the leg was not voting.
        "verdict_over": list(voting),
        "checks": checks,
    }
    return JSONResponse(body, status_code=code)
