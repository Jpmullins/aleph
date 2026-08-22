"""`/readyz` decides what Aleph owns, and reports the gateway with its age.

The defect this pins is a stack that could not start. `docker-compose.yml` wires
`/readyz` as the API's healthcheck, `up -d --wait` blocks on it, and `web` and
`copilot-runtime` both declare `condition: service_healthy` on `api`. While the
route folded `litellm_gateway` into `all_ok`, an unreachable model endpoint —
one wrong character in `LITELLM_BASE_URL` — meant the API never became healthy,
so the web UI never started and `--wait` timed out naming nothing.

The opposite mistake is just as easy and is what Part 4 correction #5 is about:
take the gateway out of the verdict, serve its answer from a cache, and the
stack now reports a healthy gateway for as long as the cache lives. So the leg
carries `checked_age_s` and `last_success_age_s`, and its `ok` is false once the
last SUCCESSFUL probe is older than `max_age_s`.

Unit tests: the route is a plain async function over `app.state`, so a stub app
exercises the real decision logic with no database, no Redis and no gateway.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import Request

from aleph_api.routes import health
from aleph_api.routes.health import GatewayLeg, readyz

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


# --- stubs ------------------------------------------------------------------


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> None:
        return None


class _Maker:
    def __call__(self) -> _Session:
        return _Session()


class _Redis:
    async def ping(self) -> bool:
        return True


class _StoredAsset:
    storage_uri = "file://probe"


class _AssetStore:
    def __init__(self) -> None:
        self.put_thread: int | None = None

    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> _StoredAsset:
        del key, data, mime_type
        self.put_thread = threading.get_ident()
        return _StoredAsset()

    def get(self, storage_uri: str) -> bytes:
        del storage_uri
        return b"ok"


class _Litellm:
    """Counts probes so the caching behaviour is measurable, not asserted."""

    def __init__(self, *, result: bool | BaseException = True) -> None:
        self.result = result
        self.calls = 0

    async def health(self) -> bool:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _State:
    def __init__(self, litellm: Any) -> None:
        self.session_maker = _Maker()
        self.redis = _Redis()
        self.asset_store = _AssetStore()
        self.litellm = litellm


class _App:
    def __init__(self, litellm: Any) -> None:
        self.state = _State(litellm)


class _Request:
    def __init__(self, app: _App) -> None:
        self.app = app


def _request(litellm: Any) -> Request:
    # The route reads only `request.app.state`; casting keeps the stub honest
    # about that rather than dragging a whole ASGI scope in.
    return cast("Request", _Request(_App(litellm)))


async def _body(response: Any) -> dict[str, Any]:
    import json

    return cast("dict[str, Any]", json.loads(bytes(response.body)))


# --- the verdict ------------------------------------------------------------


async def test_a_dead_gateway_still_reports_ready() -> None:
    """The criterion the whole stack's startup depends on.

    Parameterized over BOTH shapes a dead gateway actually takes, because they
    are different code paths and only one of them was covered.
    `LiteLLMClient.health()` catches `httpx.HTTPError` and RETURNS FALSE — it
    does not raise. A double that raises therefore exercises a branch the
    production client never reaches, and an assertion about the `error` key it
    produces is an assertion about the double.
    """
    for result in (RuntimeError("connection refused"), False):
        response = await readyz(_request(_Litellm(result=result)))
        body = await _body(response)

        assert response.status_code == 200, (
            "an unreachable model endpoint blocked the API's healthcheck, so web "
            "and copilot-runtime never started and `up -d --wait` timed out"
        )
        assert body["status"] == "ready"
        leg = body["checks"]["litellm_gateway"]
        assert leg["ok"] is False
        assert leg["in_verdict"] is False
        # `stale` is what carries the diagnosis on the path production takes.
        # An `error` string only exists when the probe RAISED, which the real
        # client never does — so requiring one here would be requiring the
        # double's behaviour.
        assert leg["stale"] is True


async def test_a_raising_probe_still_names_the_reason() -> None:
    """When the probe does raise, the reason is reported rather than swallowed.

    A separate test from the one above, because it is a separate path and
    merging them is how the raising branch came to stand in for both.
    """
    body = await _body(await readyz(_request(_Litellm(result=RuntimeError("boom")))))
    assert "boom" in body["checks"]["litellm_gateway"]["error"]


def test_the_leg_budget_fits_inside_the_healthcheck_client_timeout() -> None:
    """The bound that bites is the CLIENT's, not docker's.

    `LEG_TIMEOUT_S` was sized against the compose step's `timeout: 8s` and
    missed `urlopen(..., timeout=4)` four lines away in the same change. At
    5.0s a hung dependency produced a correct, informative body one second
    AFTER the client had given up — the container went unhealthy with no body,
    which is exactly the failure `/readyz` was restructured to prevent.
    """
    from aleph_api.routes.health import HEALTHCHECK_CLIENT_TIMEOUT_S, LEG_TIMEOUT_S

    assert LEG_TIMEOUT_S < HEALTHCHECK_CLIENT_TIMEOUT_S
    # Real headroom, not a hair. The legs run concurrently, so the endpoint
    # takes about one leg's time plus serialization and the response hop.
    assert HEALTHCHECK_CLIENT_TIMEOUT_S - LEG_TIMEOUT_S >= 1.0


def test_the_compose_healthcheck_still_uses_the_timeout_this_is_derived_from() -> None:
    """The constant is only right while compose agrees with it.

    Two numbers in two files that must match is how they drifted the first
    time. This is the check that notices.
    """
    import pathlib as _pathlib
    import re

    from aleph_api.routes.health import HEALTHCHECK_CLIENT_TIMEOUT_S

    compose = _pathlib.Path("deploy/compose/docker-compose.yml").read_text()
    found = {int(m) for m in re.findall(r"/readyz', timeout=(\d+)\)", compose)}
    assert found, "no /readyz healthcheck found in compose — has it moved?"
    assert found == {int(HEALTHCHECK_CLIENT_TIMEOUT_S)}, (
        f"compose uses timeout={found}, health.py is derived from {HEALTHCHECK_CLIENT_TIMEOUT_S}"
    )


async def test_the_gateway_is_named_as_not_voting() -> None:
    """A 200 next to a false leg has to explain itself in the body."""
    body = await _body(await readyz(_request(_Litellm(result=False))))
    assert "litellm_gateway" not in body["verdict_over"]
    assert body["verdict_over"] == ["postgres", "redis", "asset_store"]
    assert body["checks"]["litellm_gateway"]["in_verdict"] is False


async def test_strict_folds_the_gateway_into_the_verdict() -> None:
    """The operator's question — 'can this stack answer anything' — is separate."""
    request = _request(_Litellm(result=False))
    response = await readyz(request, strict=True)
    body = await _body(response)

    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert "litellm_gateway" in body["verdict_over"]
    assert body["checks"]["litellm_gateway"]["in_verdict"] is True


async def test_an_owned_dependency_failure_is_not_ready() -> None:
    """Decoupling the gateway must not decouple everything."""
    request = _request(_Litellm())

    async def _boom() -> bool:
        raise RuntimeError("the database is gone")

    request.app.state.session_maker = lambda: (_ for _ in ()).throw(RuntimeError("no pool"))
    response = await readyz(request)
    body = await _body(response)

    assert response.status_code == 503
    assert body["checks"]["postgres"]["ok"] is False
    assert "no pool" in body["checks"]["postgres"]["error"]
    del _boom


async def test_a_healthy_stack_is_ready() -> None:
    response = await readyz(_request(_Litellm(result=True)))
    body = await _body(response)
    assert response.status_code == 200
    assert all(leg["ok"] for leg in body["checks"].values())


# --- the gateway leg's age --------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _probe_of(litellm: _Litellm) -> Callable[[], Awaitable[bool]]:
    return litellm.health


async def test_repeated_checks_inside_the_window_make_one_request() -> None:
    """The compose healthcheck runs every 15s; each hit must not be a round trip."""
    clock = _Clock()
    leg = GatewayLeg(max_age_s=30.0, clock=clock)
    gateway = _Litellm(result=True)

    await leg.check(_probe_of(gateway))
    clock.advance(1.0)
    second = await leg.check(_probe_of(gateway))

    assert gateway.calls == 1, f"{gateway.calls} outbound probes for two readiness hits"
    assert second["checked_age_s"] == 1.0


async def test_the_answer_is_reprobed_once_it_reaches_max_age() -> None:
    clock = _Clock()
    leg = GatewayLeg(max_age_s=30.0, clock=clock)
    gateway = _Litellm(result=True)

    await leg.check(_probe_of(gateway))
    clock.advance(31.0)
    fresh = await leg.check(_probe_of(gateway))

    assert gateway.calls == 2, "a cached answer older than max_age_s was served"
    assert fresh["checked_age_s"] == 0.0
    assert fresh["stale"] is False


async def test_checked_age_never_exceeds_max_age() -> None:
    """Correction #5's bound, stated as the assertion it is."""
    clock = _Clock()
    leg = GatewayLeg(max_age_s=30.0, clock=clock)
    gateway = _Litellm(result=True)

    for step in (0.0, 5.0, 29.0, 100.0, 301.0):
        clock.advance(step)
        leg_body = await leg.check(_probe_of(gateway))
        assert leg_body["checked_age_s"] <= leg_body["max_age_s"], (
            f"answer was {leg_body['checked_age_s']}s old against a {leg_body['max_age_s']}s bound"
        )


async def test_a_success_older_than_max_age_is_not_reported_ok() -> None:
    """The belt for correction #5's braces.

    The probe succeeds, then stops answering at all. `checked_at` keeps moving —
    an attempt was made — but `last_success_at` does not, and it is the success
    that readiness is entitled to talk about. This is also what keeps the leg
    honest if the probe is ever swapped for a longer-lived cached view.
    """
    clock = _Clock()
    leg = GatewayLeg(max_age_s=30.0, timeout_s=0.05, clock=clock)
    healthy = _Litellm(result=True)

    first = await leg.check(_probe_of(healthy))
    assert first["ok"] is True
    assert first["last_success_age_s"] == 0.0

    async def _hangs() -> bool:
        await asyncio.sleep(3600)
        return True

    clock.advance(31.0)
    stalled = await leg.check(_hangs)

    assert stalled["ok"] is False
    assert stalled["stale"] is True
    assert stalled["last_success_age_s"] == 31.0
    assert stalled["error"] == "TimeoutError", stalled
    assert stalled["checked_age_s"] == 0.0, "the attempt is fresh even though the success is not"


async def test_a_leg_that_has_never_succeeded_is_stale_not_unknown() -> None:
    leg = GatewayLeg(clock=_Clock())
    body = await leg.check(_probe_of(_Litellm(result=False)))
    assert body["ok"] is False
    assert body["stale"] is True
    assert body["last_success_age_s"] is None


async def test_concurrent_first_hits_make_one_outbound_request() -> None:
    """A restart storm hits /readyz from every direction at once."""
    leg = GatewayLeg(clock=_Clock())
    gateway = _Litellm(result=True)
    await asyncio.gather(*(leg.check(_probe_of(gateway)) for _ in range(8)))
    assert gateway.calls == 1, f"{gateway.calls} probes for one window"


# --- the legs cannot hang the healthcheck -----------------------------------


async def test_a_hanging_dependency_is_reported_rather_than_hung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthcheck that times out has no body, so it names no dependency."""
    monkeypatch.setattr(health, "LEG_TIMEOUT_S", 0.05)
    request = _request(_Litellm(result=True))
    # Released in `finally`: `to_thread.run_sync` cannot cancel a worker thread,
    # so a probe that really slept for an hour would hang the whole test run at
    # teardown rather than the ~50ms this is measuring.
    release = threading.Event()

    class _HangingStore:
        def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> _StoredAsset:
            del key, data, mime_type
            release.wait(30)
            return _StoredAsset()

        def get(self, storage_uri: str) -> bytes:
            del storage_uri
            return b"ok"

    request.app.state.asset_store = _HangingStore()
    try:
        response = await asyncio.wait_for(readyz(request), timeout=5.0)
        body = await _body(response)

        assert response.status_code == 503
        assert body["checks"]["asset_store"]["ok"] is False
        assert body["checks"]["asset_store"]["error"] == "TimeoutError"
    finally:
        release.set()


async def test_the_asset_store_probe_does_not_run_on_the_event_loop() -> None:
    """Compose calls this every 15s; a blocking write here stalls every request."""
    request = _request(_Litellm(result=True))
    store = request.app.state.asset_store
    loop_thread = threading.get_ident()

    await readyz(request)

    assert store.put_thread is not None, "the asset store was never probed"
    assert store.put_thread != loop_thread, (
        "put_bytes ran on the event loop thread — a synchronous filesystem or S3 "
        "write four times a minute, blocking every other request in the process"
    )
