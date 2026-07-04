"""Consensus search over MCP streamable-HTTP, with OAuth refresh + quota.

The package stays DB-free: the encrypted credential blob is loaded/saved
through injected async callbacks (the API layer binds them to
`ConnectorCredentialService`). The blob plaintext is JSON:

    {client_id, token_endpoint, refresh_token, access_token,
     access_token_expires_at, status}

Behavior (spec WP-2 §3):

- **Quota**: `INCR scholar:consensus:{project_id}:{YYYY-MM}` (expiry ~62d)
  before anything else; over the cap the client returns a tagged
  quota-exhausted result and performs zero HTTP calls.
- **Refresh**: access token missing/expiring within 60s triggers a
  refresh-token grant at the AS token endpoint (public client + PKCE — no
  secret). A rotated refresh token is persisted via `save_credential`.
  Refresh is serialized per project with a redis lock so parallel tool
  calls never race a one-time-use refresh token.
- **Reconnect-required**: any HTTP 400/401 from the token endpoint is an
  authoritative dead grant (RFC 6749 says `error: invalid_grant`, but the
  live Consensus AS answers `{"detail": ...}`) — the blob is marked
  `status="reconnect_required"` and a tagged reconnect result is returned;
  network failures / 5xx stay transient (`ScholarUpstreamError`), never
  reconnect-required.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from aleph_scholar._json import as_dict, as_list
from aleph_scholar.errors import ConsensusReconnectRequired, ScholarUpstreamError
from aleph_scholar.types import ConsensusHit, ConsensusResult

CONSENSUS_MCP_URL = "https://mcp.consensus.app/mcp"

_QUOTA_TTL_S = 62 * 24 * 3600  # ~62 days — outlives any month it counts
_REFRESH_SKEW_S = 60.0
_LOCK_TTL_S = 30
_LOCK_POLL_S = 0.1

_QUOTA_MESSAGE = (
    "Consensus monthly search quota exhausted (cap {cap}) — "
    "use search_openalex for bulk discovery instead."
)
_RECONNECT_MESSAGE = (
    "Consensus authorization expired — reconnect with scripts/connect-consensus.py."
)

CredentialLoader = Callable[[], Awaitable[dict[str, Any]]]
CredentialSaver = Callable[[dict[str, Any]], Awaitable[None]]
# (access_token, query, filters) -> hits; injectable for tests / transports.
ConsensusSearchFn = Callable[[str, str, dict[str, Any] | None], Awaitable[list[ConsensusHit]]]


class RedisLike(Protocol):
    """The redis.asyncio subset the Consensus client needs."""

    def incr(self, name: str) -> Awaitable[int]: ...

    def expire(self, name: str, time: int) -> Awaitable[bool]: ...

    def set(
        self, name: str, value: str, *, nx: bool = ..., ex: int | None = ...
    ) -> Awaitable[bool | None]: ...

    def get(self, name: str) -> Awaitable[bytes | str | None]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_expiry(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hit_from_mapping(item: dict[str, Any]) -> ConsensusHit | None:
    title = item.get("title") or item.get("paper_title") or item.get("name")
    url = item.get("url") or item.get("consensus_url") or item.get("link")
    if not title and not url:
        return None
    doi = item.get("doi")
    snippet = (
        item.get("snippet") or item.get("abstract") or item.get("display_text") or item.get("text")
    )
    return ConsensusHit(
        title=str(title or url),
        url=str(url or ""),
        doi=str(doi) if doi else None,
        snippet=str(snippet) if snippet else None,
    )


def _hits_from_list(items: list[Any]) -> list[ConsensusHit]:
    out: list[ConsensusHit] = []
    for item in items:
        mapping = as_dict(item)
        if mapping:
            hit = _hit_from_mapping(mapping)
            if hit:
                out.append(hit)
    return out


def _hits_from_value(value: object) -> list[ConsensusHit]:
    mapping = as_dict(value)
    if mapping:
        for key in ("results", "papers", "hits", "items"):
            if key in mapping:
                return _hits_from_list(as_list(mapping.get(key)))
        hit = _hit_from_mapping(mapping)
        return [hit] if hit else []
    return _hits_from_list(as_list(value))


def parse_search_result(result: mcp_types.CallToolResult) -> list[ConsensusHit]:
    """Parse a Consensus `search` tool result into hits, defensively."""
    if result.structuredContent is not None:
        structured_hits = _hits_from_value(result.structuredContent)
        if structured_hits:
            return structured_hits
    hits: list[ConsensusHit] = []
    free_text: list[str] = []
    for block in result.content:
        if not isinstance(block, mcp_types.TextContent):
            continue
        try:
            parsed = json.loads(block.text)
        except ValueError:
            free_text.append(block.text)
            continue
        hits.extend(_hits_from_value(parsed))
    if not hits and free_text:
        hits = _hits_from_markdown("\n".join(free_text))
    return hits


_MD_ENTRY = re.compile(
    r"^\[\d+\]\s+\[(?P<title>.+?)\]\((?P<url>[^)\s]+)\)\s*(?:\((?P<meta>[^)]*)\))?",
    re.MULTILINE,
)


def _hits_from_markdown(text: str) -> list[ConsensusHit]:
    """Parse the live server's format: one text block of
    `[N] [Title](url) (Authors, Year, citations, Journal)` entries, each
    followed by indented snippet lines until the next entry.
    """
    text = text.strip()
    if not text:
        return []
    matches = list(_MD_ENTRY.finditer(text))
    hits: list[ConsensusHit] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        meta = m.group("meta")
        snippet = "\n".join(part for part in (meta, body) if part) or None
        hits.append(
            ConsensusHit(title=m.group("title"), url=m.group("url"), doi=None, snippet=snippet)
        )
    if not hits:
        # Unrecognized shape: keep the old single-blob fallback so the
        # caller still sees *something* rather than silence.
        hits.append(ConsensusHit(title=text.splitlines()[0][:200], url="", doi=None, snippet=text))
    return hits


async def _mcp_search(
    access_token: str, query: str, filters: dict[str, Any] | None, *, mcp_url: str
) -> list[ConsensusHit]:
    """One session per call-burst against the Consensus MCP endpoint."""
    headers = {"Authorization": f"Bearer {access_token}"}
    arguments: dict[str, Any] = {"query": query, **(filters or {})}
    try:
        async with (
            httpx.AsyncClient(
                headers=headers, timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True
            ) as http_client,
            streamable_http_client(mcp_url, http_client=http_client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("search", arguments)
    except Exception as exc:
        msg = f"consensus MCP call failed: {exc!r}"
        raise ScholarUpstreamError(msg) from exc
    if result.isError:
        detail = "; ".join(
            block.text for block in result.content if isinstance(block, mcp_types.TextContent)
        )
        msg = f"consensus search tool errored: {detail or 'unknown error'}"
        raise ScholarUpstreamError(msg)
    return parse_search_result(result)


class ConsensusClient:
    """Quota-aware, self-refreshing Consensus MCP search client."""

    def __init__(
        self,
        *,
        project_id: str,
        redis: RedisLike,
        load_credential: CredentialLoader,
        save_credential: CredentialSaver,
        token_http: httpx.AsyncClient | None = None,
        monthly_cap: int = 200,
        mcp_url: str = CONSENSUS_MCP_URL,
        search_fn: ConsensusSearchFn | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._project_id = project_id
        self._redis = redis
        self._load_credential = load_credential
        self._save_credential = save_credential
        # follow_redirects: the live Consensus AS 308-redirects its token
        # endpoint to a trailing-slash path; 308 preserves method+body.
        self._token_http = token_http or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._monthly_cap = monthly_cap
        self._mcp_url = mcp_url
        self._search_fn = search_fn
        self._now: Callable[[], datetime] = now or _utcnow
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

    # -- quota ------------------------------------------------------------

    def _quota_key(self) -> str:
        return f"scholar:consensus:{self._project_id}:{self._now():%Y-%m}"

    async def _quota_exceeded(self) -> bool:
        key = self._quota_key()
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, _QUOTA_TTL_S)
        return count > self._monthly_cap

    # -- OAuth refresh ----------------------------------------------------

    def _is_stale(self, blob: dict[str, Any]) -> bool:
        if not blob.get("access_token"):
            return True
        expires_at = _parse_expiry(blob.get("access_token_expires_at"))
        if expires_at is None:
            return True
        return expires_at <= self._now() + timedelta(seconds=_REFRESH_SKEW_S)

    async def _refresh(self, blob: dict[str, Any]) -> dict[str, Any]:
        """Refresh-token grant (public client, no secret). Persists the new blob."""
        token_endpoint = str(blob.get("token_endpoint") or "")
        if not token_endpoint:
            raise ConsensusReconnectRequired("credential blob has no token_endpoint")
        try:
            response = await self._token_http.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": str(blob.get("refresh_token") or ""),
                    "client_id": str(blob.get("client_id") or ""),
                },
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            msg = f"consensus token refresh failed (network): {exc!r}"
            raise ScholarUpstreamError(msg) from exc

        if response.status_code == 200:
            payload: dict[str, Any] = response.json()
            access_token = payload.get("access_token")
            if not access_token:
                msg = "consensus token refresh returned 200 without an access_token"
                raise ScholarUpstreamError(msg)
            expires_in = float(payload.get("expires_in") or 3600)
            new_blob = {
                **blob,
                "access_token": str(access_token),
                "access_token_expires_at": (
                    self._now() + timedelta(seconds=expires_in)
                ).isoformat(),
                "status": "active",
            }
            rotated = payload.get("refresh_token")
            if rotated:
                new_blob["refresh_token"] = str(rotated)
            await self._save_credential(new_blob)
            return new_blob

        if response.status_code in (400, 401):
            # Authoritative rejection of the grant. RFC 6749 puts the code in
            # `error` (invalid_grant/invalid_client), but the live Consensus
            # AS is non-conformant and answers `{"detail": "Invalid or
            # expired refresh token"}` — either way a 400/401 from a refresh
            # grant means the grant is dead, not that the network hiccuped.
            reason = ""
            try:
                body = response.json()
                reason = str(body.get("error") or body.get("detail") or "")
            except ValueError:
                reason = ""
            dead_blob = {**blob, "status": "reconnect_required"}
            await self._save_credential(dead_blob)
            msg = f"consensus refresh rejected by AS: {reason or f'HTTP {response.status_code}'}"
            raise ConsensusReconnectRequired(msg)
        msg = f"consensus token refresh failed: HTTP {response.status_code}"
        raise ScholarUpstreamError(msg)

    async def _ensure_access_token(self, blob: dict[str, Any]) -> str:
        if not self._is_stale(blob):
            return str(blob["access_token"])
        lock_key = f"scholar:consensus:refresh:{self._project_id}"
        lock_token = uuid.uuid4().hex
        deadline = self._now() + timedelta(seconds=_LOCK_TTL_S)
        while not await self._redis.set(lock_key, lock_token, nx=True, ex=_LOCK_TTL_S):
            if self._now() >= deadline:
                msg = "consensus refresh lock timed out"
                raise ScholarUpstreamError(msg)
            await self._sleep(_LOCK_POLL_S)
        try:
            # Re-read under the lock — a parallel caller may have refreshed.
            blob = await self._load_credential()
            if self._is_stale(blob):
                blob = await self._refresh(blob)
            return str(blob["access_token"])
        finally:
            holder = await self._redis.get(lock_key)
            held = holder.decode() if isinstance(holder, bytes) else holder
            if held == lock_token:
                await self._redis.delete(lock_key)

    # -- search -----------------------------------------------------------

    async def search(self, query: str, *, filters: dict[str, Any] | None = None) -> ConsensusResult:
        """Quota-gated Consensus search; tagged result, no control-flow exceptions.

        Raises `ScholarUpstreamError` only for transient trouble (the route
        maps it to a retryable upstream failure).
        """
        if await self._quota_exceeded():
            return ConsensusResult(
                status="quota_exhausted",
                message=_QUOTA_MESSAGE.format(cap=self._monthly_cap),
            )
        blob = await self._load_credential()
        if blob.get("status") == "reconnect_required":
            return ConsensusResult(status="reconnect_required", message=_RECONNECT_MESSAGE)
        try:
            access_token = await self._ensure_access_token(blob)
        except ConsensusReconnectRequired:
            return ConsensusResult(status="reconnect_required", message=_RECONNECT_MESSAGE)
        if self._search_fn is not None:
            hits = await self._search_fn(access_token, query, filters)
        else:
            hits = await _mcp_search(access_token, query, filters, mcp_url=self._mcp_url)
        return ConsensusResult(status="ok", hits=hits)
