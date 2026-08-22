"""Every request is counted, and the route label is the TEMPLATE.

Two things are pinned here that nothing else can pin.

The first is cardinality. `/v1/projects/{project_id}/sources` must produce one
series no matter how many projects exist. The test drives two different ids
through the same route and asserts they land on the same series — an assertion
that goes red the moment somebody "helpfully" labels with `scope["path"]`.

The second is that the wrapper is installed at all. It is deliberately not an
`app.add_middleware` entry (see `http_metrics`), which means reading
`app.user_middleware` cannot tell you whether it is there. So the test drives a
real request and checks the counter moved. Delete the `install_http_metrics`
call in `tracing.instrument_fastapi` and this file goes red.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from aleph_observability import metrics as m
from aleph_observability.http_metrics import is_http_metrics_installed
from aleph_observability.tracing import instrument_fastapi


def _count(**labels: str) -> float:
    value = m.sample_value(m.HTTP_REQUESTS, **labels)
    return 0.0 if value is None else value


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()

    @application.get("/things/{thing_id}")
    async def _thing(thing_id: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"id": thing_id}

    @application.get("/boom")
    async def _boom() -> None:  # pyright: ignore[reportUnusedFunction]
        msg = "deliberate"
        raise RuntimeError(msg)

    instrument_fastapi(application)
    return application


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.get(path)


async def test_two_ids_on_one_route_share_one_series(app: FastAPI) -> None:
    labels = {"route": "/things/{thing_id}", "method": "GET", "status": "200"}
    before = _count(**labels)

    await _get(app, "/things/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    await _get(app, "/things/11111111-2222-4333-8444-555555555555")

    assert _count(**labels) == before + 2, (
        "two requests to the same route did not land on the same series — the "
        "label is not the route template, and this endpoint now grows one "
        "series per id"
    )
    text = m.render_prometheus()[0].decode()
    assert "aaaaaaaa-bbbb" not in text, "a request id leaked into a metric label"


async def test_latency_is_recorded_for_the_same_template(app: FastAPI) -> None:
    labels = {"route": "/things/{thing_id}", "method": "GET"}
    before = m.sample_value(f"{m.HTTP_DURATION}_count", **labels) or 0.0
    await _get(app, "/things/42")
    after = m.sample_value(f"{m.HTTP_DURATION}_count", **labels) or 0.0
    assert after == before + 1


async def test_a_handler_that_raises_is_counted_as_a_500(app: FastAPI) -> None:
    """The exception path has no response object, and is where a naive
    middleware silently stops counting — so the outage is the moment the graph
    goes flat rather than red."""
    labels = {"route": "/boom", "method": "GET", "status": "500"}
    before = _count(**labels)
    await _get(app, "/boom")
    assert _count(**labels) == before + 1


async def test_an_unmatched_path_collapses_to_one_series(app: FastAPI) -> None:
    """A scanner probing a thousand URLs must not mint a thousand series."""
    before = _count(route=m.UNMATCHED_ROUTE, method="GET", status="404")
    await _get(app, "/no-such-thing-1")
    await _get(app, "/no-such-thing-2")
    assert _count(route=m.UNMATCHED_ROUTE, method="GET", status="404") == before + 2


def test_the_wrapper_is_installed_by_instrument_fastapi(app: FastAPI) -> None:
    """Invisible to `app.user_middleware`, so it needs its own assertion."""
    assert is_http_metrics_installed(app)
    assert not any("HttpMetrics" in mw.cls.__name__ for mw in app.user_middleware), (
        "the metrics wrapper appeared in user_middleware; that breaks the "
        "four-entry tuple pinned by test_request_correlation and is not how "
        "this is meant to be installed"
    )
