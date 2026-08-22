"""ConsensusClient: quota gate, OAuth refresh + rotation, reconnect, parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mcp import types as mcp_types
from scholar_test_support import FakeRedis

from aleph_scholar.consensus import ConsensusClient, parse_search_result
from aleph_scholar.errors import ScholarUpstreamError
from aleph_scholar.service import ScholarService
from aleph_scholar.types import ConsensusHit

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
TOKEN_ENDPOINT = "https://auth.consensus.example/oauth/token"
PROJECT_ID = "0197a000-0000-7000-8000-000000000001"
QUOTA_KEY = f"scholar:consensus:{PROJECT_ID}:2026-07"


def _now() -> datetime:
    return NOW


def _blob(*, expires_in_s: float, status: str = "active") -> dict[str, Any]:
    return {
        "client_id": "consensus-client",
        "token_endpoint": TOKEN_ENDPOINT,
        "refresh_token": "rt-one",
        "access_token": "at-old",
        "access_token_expires_at": (NOW + timedelta(seconds=expires_in_s)).isoformat(),
        "status": status,
    }


class CredentialStore:
    """load/save callback pair recording every persisted blob."""

    def __init__(self, blob: dict[str, Any]) -> None:
        self.blob = blob
        self.saved: list[dict[str, Any]] = []

    async def load(self) -> dict[str, Any]:
        return dict(self.blob)

    async def save(self, blob: dict[str, Any]) -> None:
        self.blob = dict(blob)
        self.saved.append(dict(blob))


class SearchSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def __call__(
        self, access_token: str, query: str, filters: dict[str, Any] | None
    ) -> list[ConsensusHit]:
        self.calls.append((access_token, query, filters))
        return [ConsensusHit(title="Hit", url="https://consensus.app/x", doi=None, snippet="s")]


def _client(
    store: CredentialStore,
    redis: FakeRedis,
    spy: SearchSpy,
    token_requests: list[httpx.Request],
    *,
    monthly_cap: int = 5,
    token_response: httpx.Response | None = None,
    token_error: Exception | None = None,
) -> ConsensusClient:
    def token_handler(request: httpx.Request) -> httpx.Response:
        token_requests.append(request)
        if token_error is not None:
            raise token_error
        assert token_response is not None, "unexpected token-endpoint call"
        return token_response

    return ConsensusClient(
        project_id=PROJECT_ID,
        redis=redis,
        load_credential=store.load,
        save_credential=store.save,
        token_http=httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
        monthly_cap=monthly_cap,
        search_fn=spy,
        now=_now,
    )


# ------------------------------------------------------------------- quota


async def test_quota_at_cap_returns_quota_result_with_zero_http_calls(
    fake_redis: FakeRedis,
) -> None:
    store = CredentialStore(_blob(expires_in_s=3600))
    spy = SearchSpy()
    token_requests: list[httpx.Request] = []
    client = _client(store, fake_redis, spy, token_requests, monthly_cap=3)
    fake_redis.counters[QUOTA_KEY] = 3  # already at cap

    result = await client.search("does caffeine improve endurance?")

    assert result.status == "quota_exhausted"
    assert result.quota_exhausted is True
    assert result.hits == []
    assert result.message is not None and "search_openalex" in result.message
    assert token_requests == []  # zero HTTP calls
    assert spy.calls == []  # no MCP call either


async def test_quota_under_cap_counts_and_searches(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=3600))
    spy = SearchSpy()
    client = _client(store, fake_redis, spy, [])

    result = await client.search("q", filters={"year_min": 2020})

    assert result.status == "ok"
    assert [h.title for h in result.hits] == ["Hit"]
    assert fake_redis.counters[QUOTA_KEY] == 1
    assert fake_redis.expiries[QUOTA_KEY] == 62 * 24 * 3600
    assert spy.calls == [("at-old", "q", {"year_min": 2020})]


# ----------------------------------------------------------------- refresh


async def test_fresh_token_skips_refresh(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=3600))
    spy = SearchSpy()
    token_requests: list[httpx.Request] = []
    client = _client(store, fake_redis, spy, token_requests)

    result = await client.search("q")

    assert result.status == "ok"
    assert token_requests == []
    assert store.saved == []


async def test_expired_token_triggers_refresh_then_search_succeeds(
    fake_redis: FakeRedis,
) -> None:
    store = CredentialStore(_blob(expires_in_s=-100))  # already expired
    spy = SearchSpy()
    token_requests: list[httpx.Request] = []
    client = _client(
        store,
        fake_redis,
        spy,
        token_requests,
        token_response=httpx.Response(
            200,
            json={"access_token": "at-new", "expires_in": 1800, "refresh_token": "rt-two"},
        ),
    )

    result = await client.search("q")

    assert result.status == "ok"
    assert len(token_requests) == 1
    body = token_requests[0].content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=rt-one" in body
    assert "client_id=consensus-client" in body
    assert "client_secret" not in body  # public client, PKCE — no secret
    assert spy.calls[0][0] == "at-new"  # search used the refreshed token


async def test_refresh_rotation_persists_via_save_credential(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=30))  # inside the 60s skew window
    spy = SearchSpy()
    client = _client(
        store,
        fake_redis,
        spy,
        [],
        token_response=httpx.Response(
            200,
            json={"access_token": "at-new", "expires_in": 1800, "refresh_token": "rt-two"},
        ),
    )

    await client.search("q")

    assert len(store.saved) == 1
    saved = store.saved[0]
    assert saved["access_token"] == "at-new"
    assert saved["refresh_token"] == "rt-two"  # rotated token persisted
    assert saved["status"] == "active"
    expires_at = datetime.fromisoformat(saved["access_token_expires_at"])
    assert expires_at == NOW + timedelta(seconds=1800)
    # the refresh lock was released
    assert f"scholar:consensus:refresh:{PROJECT_ID}" not in fake_redis.store


async def test_refresh_without_rotation_keeps_old_refresh_token(
    fake_redis: FakeRedis,
) -> None:
    store = CredentialStore(_blob(expires_in_s=-1))
    client = _client(
        store,
        fake_redis,
        SearchSpy(),
        [],
        token_response=httpx.Response(200, json={"access_token": "at-new", "expires_in": 900}),
    )

    await client.search("q")

    assert store.blob["refresh_token"] == "rt-one"
    assert store.blob["access_token"] == "at-new"


async def test_invalid_grant_marks_reconnect_required(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=-1))
    spy = SearchSpy()
    client = _client(
        store,
        fake_redis,
        spy,
        [],
        token_response=httpx.Response(400, json={"error": "invalid_grant"}),
    )

    result = await client.search("q")

    assert result.status == "reconnect_required"
    assert result.reconnect_required is True
    assert "connect-consensus" in (result.message or "")
    assert store.blob["status"] == "reconnect_required"  # persisted for the GET route
    assert spy.calls == []


async def test_nonconformant_400_detail_marks_reconnect_required(fake_redis: FakeRedis) -> None:
    """The live Consensus AS answers `{"detail": ...}` with no RFC `error`
    field — a 400 on a refresh grant is still an authoritative dead grant."""
    store = CredentialStore(_blob(expires_in_s=-1))
    spy = SearchSpy()
    client = _client(
        store,
        fake_redis,
        spy,
        [],
        token_response=httpx.Response(400, json={"detail": "Invalid or expired refresh token"}),
    )

    result = await client.search("q")

    assert result.status == "reconnect_required"
    assert store.blob["status"] == "reconnect_required"
    assert spy.calls == []


async def test_reconnect_required_blob_short_circuits(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=-1, status="reconnect_required"))
    spy = SearchSpy()
    token_requests: list[httpx.Request] = []
    client = _client(store, fake_redis, spy, token_requests)

    result = await client.search("q")

    assert result.status == "reconnect_required"
    assert token_requests == []  # no refresh attempt against a dead grant
    assert spy.calls == []


async def test_network_failure_during_refresh_is_transient_not_reconnect(
    fake_redis: FakeRedis,
) -> None:
    store = CredentialStore(_blob(expires_in_s=-1))
    client = _client(
        store,
        fake_redis,
        SearchSpy(),
        [],
        token_error=httpx.ConnectError("as unreachable"),
    )

    with pytest.raises(ScholarUpstreamError):
        await client.search("q")

    assert store.blob["status"] == "active"  # NOT flipped to reconnect_required


# ------------------------------------------------------------ service facade


async def test_scholar_service_wires_consensus(fake_redis: FakeRedis) -> None:
    store = CredentialStore(_blob(expires_in_s=3600))
    spy = SearchSpy()
    service = ScholarService(
        mailto="scholar-tests@aleph-fixture.org",
        redis=fake_redis,
        load_credential=store.load,
        save_credential=store.save,
        consensus_search_fn=spy,
        now=_now,
    )
    result = await service.search_consensus(PROJECT_ID, "screening question")
    assert result.status == "ok"
    assert spy.calls[0][1] == "screening question"


async def test_scholar_service_without_consensus_deps_raises() -> None:
    service = ScholarService(mailto="scholar-tests@aleph-fixture.org")
    with pytest.raises(RuntimeError, match="not configured"):
        await service.search_consensus(PROJECT_ID, "q")


# ------------------------------------------------------------------ parsing


def test_parse_search_result_structured_content() -> None:
    result = mcp_types.CallToolResult(
        content=[],
        structuredContent={
            "results": [
                {
                    "title": "Paper",
                    "url": "https://consensus.app/p",
                    "doi": "10.1234/p",
                    "snippet": "text",
                }
            ]
        },
    )
    hits = parse_search_result(result)
    assert hits == [
        ConsensusHit(title="Paper", url="https://consensus.app/p", doi="10.1234/p", snippet="text")
    ]


def test_parse_search_result_json_text_content() -> None:
    payload = {"papers": [{"title": "P2", "url": "https://consensus.app/p2"}]}
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload))],
    )
    hits = parse_search_result(result)
    assert len(hits) == 1
    assert hits[0].title == "P2"
    assert hits[0].doi is None


def test_parse_search_result_free_text_fallback() -> None:
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="Plain prose answer.\nMore prose.")],
    )
    hits = parse_search_result(result)
    assert len(hits) == 1
    assert hits[0].title == "Plain prose answer."
    assert hits[0].snippet == "Plain prose answer.\nMore prose."


def test_parse_search_result_live_markdown_format():
    """The live server returns ONE text block of markdown entries."""
    from mcp import types as mcp_types

    from aleph_scholar.consensus import parse_search_result

    text = (
        "Found 20 papers, showing top 20.\n\n"
        "[1] [The effects of creatine supplementation on cognitive function]"
        "(https://consensus.app/papers/details/abc123/?utm_source=x) "
        "(Chen Xu et al., 2024, 33 citations, Frontiers in Nutrition)\n"
        "  Background: This study aimed to evaluate creatine monohydrate.\n\n"
        "[2] [Creatine and memory: a meta-analysis]"
        "(https://consensus.app/papers/details/def456/) "
        "(Prokopidis et al., 2023, 120 citations, Nutrition Reviews)\n"
        "  Results: improvements in memory measures.\n"
    )
    result = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text=text)])
    hits = parse_search_result(result)
    assert len(hits) == 2
    assert hits[0].title.startswith("The effects of creatine")
    assert hits[0].url == "https://consensus.app/papers/details/abc123/?utm_source=x"
    assert hits[0].snippet is not None
    assert "Chen Xu" in hits[0].snippet
    assert "Background" in hits[0].snippet
    assert hits[1].title == "Creatine and memory: a meta-analysis"
