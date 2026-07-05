"""LangfuseReader: read-only Langfuse REST access + diagnostic aggregation.

Uses an httpx MockTransport so no live Langfuse is required — asserts the
reader hits the right public endpoints with the right params/auth and folds
the responses into a DiagnosticSnapshot."""

from __future__ import annotations

import base64

import httpx
import pytest

from aleph_observability import DiagnosticSnapshot, LangfuseReader


def _reader(handler: object) -> LangfuseReader:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport)
    return LangfuseReader(
        host="http://langfuse:3000/",  # trailing slash must be normalized away
        public_key="pk-test",
        secret_key="sk-test",
        client=client,
    )


async def test_endpoints_params_and_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/public/traces":
            return httpx.Response(200, json={"data": [], "meta": {"totalItems": 42}})
        if request.url.path == "/api/public/observations":
            return httpx.Response(200, json={"data": [], "meta": {"totalItems": 3}})
        return httpx.Response(404, json={})

    reader = _reader(handler)
    traces = await reader.list_traces(limit=5, from_timestamp="2026-07-05T00:00:00Z")
    obs = await reader.list_observations(level="ERROR", limit=7)
    await reader.aclose()

    assert traces["meta"]["totalItems"] == 42
    assert obs["meta"]["totalItems"] == 3
    # host trailing slash normalized; no double slash
    assert str(seen[0].url).startswith("http://langfuse:3000/api/public/traces")
    assert seen[0].url.params["limit"] == "5"
    assert seen[0].url.params["fromTimestamp"] == "2026-07-05T00:00:00Z"
    assert seen[1].url.params["level"] == "ERROR"
    # Basic auth header from pk:sk
    expected = "Basic " + base64.b64encode(b"pk-test:sk-test").decode()
    assert seen[0].headers["authorization"] == expected


async def test_none_params_are_dropped() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [], "meta": {"totalItems": 0}})

    reader = _reader(handler)
    await reader.list_observations(limit=10)  # level/type/from all None
    await reader.aclose()
    q = seen[0].url.params
    assert "level" not in q and "type" not in q and "fromStartTime" not in q
    assert q["limit"] == "10"


async def test_diagnostic_snapshot_aggregates_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        level = request.url.params.get("level")
        if path == "/api/public/traces":
            return httpx.Response(200, json={"data": [], "meta": {"totalItems": 100}})
        if path == "/api/public/observations" and level == "ERROR":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "obs-1",
                            "name": "litellm.chat",
                            "traceId": "tr-1",
                            "startTime": "2026-07-05T11:00:00Z",
                            "statusMessage": "429 rate limited",
                        },
                        {"id": "obs-2", "traceId": "tr-2"},  # sparse row → defaults
                    ],
                    "meta": {"totalItems": 2},
                },
            )
        # non-error observation total
        return httpx.Response(200, json={"data": [], "meta": {"totalItems": 500}})

    reader = _reader(handler)
    snap = await reader.diagnostic_snapshot(window_hours=6, error_limit=10)
    await reader.aclose()

    assert isinstance(snap, DiagnosticSnapshot)
    assert snap.window_hours == 6
    assert snap.total_traces == 100
    assert snap.total_observations == 500
    assert snap.error_count == 2
    assert [e.id for e in snap.recent_errors] == ["obs-1", "obs-2"]
    assert snap.recent_errors[0].status_message == "429 rate limited"
    # sparse row defaults
    assert snap.recent_errors[1].name == "(unnamed)"
    assert snap.recent_errors[1].status_message == ""


async def test_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    reader = _reader(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await reader.list_traces()
    await reader.aclose()
