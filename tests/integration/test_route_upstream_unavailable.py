"""An upstream that does not answer is a 502, not a 500. WS-P1 c6 / WS-P3.

## The defect

`GET /v1/gateway/models` calls the LiteLLM gateway. With no gateway reachable —
which is every CI runner, because the `python-integration` job runs no gateway
— `httpx.ConnectError` escaped the handler, reached `ErrorMiddleware`'s bare
`except Exception`, and came back as::

    {"type":"about:blank#internal_error","title":"Internal error","status":500,
     "detail":"An unexpected error occurred."}

Two costs. The browser cannot tell "Aleph is broken" from "the gateway is
down", which are opposite remedies. And it is the single route that makes
`tests/integration/test_route_smoke.py::test_no_route_answers_500` red on a
runner with no gateway, so the whole integration job fails on a fact about the
runner rather than a fact about the code.

## What is asserted here, and why it drives real failures

Every case below produces a **genuine httpx exception from httpx**, never a
fabricated one: a connect to a closed port, a read from a socket that accepts
and never answers, a scheme httpx does not speak. A hand-raised
`httpx.ConnectError(...)` would assert the middleware's own `except` clause
against a constant, which proves nothing about what httpx actually raises.

The negative cases are the load-bearing half. `UnsupportedProtocol` is also an
`httpx.TransportError`, and it means *Aleph built a bad request* — if the
branch caught `TransportError` wholesale, that bug would be reported as the
upstream's fault and disappear. So it must stay a 500.

## No services

Needs no Postgres and no Redis: it mounts the production `ErrorMiddleware` on a
throwaway app. It lives here because the thing it pins is route behaviour over
the wire, and it is marked `integration` so it runs beside the route sweep whose
red it explains.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI

from aleph_api.middleware.errors import ErrorMiddleware

pytestmark = pytest.mark.integration

#: Short enough that the hanging-socket case is a test and not a wait.
_READ_TIMEOUT_S = 0.5


@pytest.fixture(scope="module")
def closed_port() -> int:
    """A port with nothing listening on it. Connecting raises `ConnectError`."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def black_hole_port() -> Iterator[int]:
    """A port that accepts the TCP connection and never sends a byte.

    `listen()` without `accept()` still completes the handshake inside the
    kernel's backlog, so the client connects and then waits — which is
    `ReadTimeout`, the timeout half of the branch, and it is not reachable by
    connecting to a closed port.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def app(closed_port: int, black_hole_port: int) -> FastAPI:
    """A throwaway app carrying the production error middleware."""
    application = FastAPI()
    application.add_middleware(ErrorMiddleware)

    @application.get("/refused")
    async def _refused() -> dict[str, str]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(f"http://127.0.0.1:{closed_port}/models")
        raise AssertionError("the closed port answered")  # pragma: no cover

    @application.get("/hangs")
    async def _hangs() -> dict[str, str]:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT_S) as client:
            await client.get(f"http://127.0.0.1:{black_hole_port}/models")
        raise AssertionError("the black hole answered")  # pragma: no cover

    @application.get("/bad-request-aleph-built")
    async def _bad_scheme() -> dict[str, str]:
        # `UnsupportedProtocol` IS an `httpx.TransportError`, and it is Aleph's
        # own bug. Raised before any socket is opened.
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get("ftp://example.invalid/models")
        raise AssertionError("httpx spoke ftp")  # pragma: no cover

    @application.get("/aleph-bug")
    async def _bug() -> dict[str, str]:
        raise RuntimeError("a defect in Aleph's own code")

    return application


@pytest.fixture
def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://upstream-test", timeout=30.0
    )


async def test_an_unreachable_upstream_is_a_502_naming_the_host(
    client: httpx.AsyncClient, closed_port: int
) -> None:
    """The defect itself: a connect refusal is the upstream's fault, and it is named."""
    async with client:
        response = await client.get("/refused")

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["type"] == "about:blank#upstream_unavailable"
    assert body["status"] == 502
    # The host, because "unavailable" with no host is not actionable — an
    # operator has to know whether it was the gateway, Crossref or the store.
    assert body["details"]["upstream"] == f"http://127.0.0.1:{closed_port}"
    assert f"127.0.0.1:{closed_port}" in body["detail"]
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_an_upstream_that_never_answers_is_a_504(client: httpx.AsyncClient) -> None:
    """A read timeout is a distinct fact from a refusal and gets its own status."""
    async with client:
        response = await client.get("/hangs")

    assert response.status_code == 504, response.text
    body = response.json()
    assert body["type"] == "about:blank#upstream_unavailable"
    assert body["status"] == 504


async def test_a_request_aleph_built_wrong_is_still_a_500(client: httpx.AsyncClient) -> None:
    """The guard on the exemption.

    `UnsupportedProtocol` is an `httpx.TransportError` too. Catching the base
    class would report Aleph's own malformed request as somebody else's outage,
    which is the one thing the 502 branch must not be able to do.
    """
    async with client:
        response = await client.get("/bad-request-aleph-built")

    assert response.status_code == 500, response.text
    assert response.json()["type"] == "about:blank#internal_error"


async def test_an_ordinary_defect_is_still_a_500(client: httpx.AsyncClient) -> None:
    """Control: nothing about this change moved the generic path."""
    async with client:
        response = await client.get("/aleph-bug")

    assert response.status_code == 500, response.text
    assert response.json()["type"] == "about:blank#internal_error"


async def test_the_upstream_response_carries_the_callers_request_id(
    client: httpx.AsyncClient,
) -> None:
    """Correlation is not lost on the new branch — same contract as the 500 path."""
    async with client:
        response = await client.get("/refused", headers={"x-request-id": "req-upstream-1"})

    assert response.headers["x-request-id"] == "req-upstream-1"
    assert response.json()["request_id"] == "req-upstream-1"
