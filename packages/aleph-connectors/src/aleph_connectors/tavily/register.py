"""Tavily web-search connector implementation.

Used by AIQ DeepResearcher for general web search. `search` returns
top-K results; `fetch` retrieves the raw HTML, which the
normalization worker turns into Markdown via readability.

Auth: API key. The key is fetched via the credential callback when
running inside AIQ; for in-process use (tests / dev tools) the caller
supplies the key in `ConnectorContext.credential_value`.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, Literal

import httpx
from pydantic import BaseModel

from aleph_connectors.base import (
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)


class TavilyMetadata(BaseModel):
    """Per-source metadata recorded for Tavily results."""

    query: str
    score: float = 0.0
    published_date: str | None = None
    domain: str | None = None


class TavilyConnector:
    kind: ClassVar[str] = "tavily"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = True
    metadata_schema: ClassVar[type[BaseModel]] = TavilyMetadata

    def __init__(
        self,
        *,
        base_url: str = "https://api.tavily.com",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        if not ctx.credential_value:
            msg = "Tavily requires an API key"
            raise NotSupported(msg)
        resp = await self._http.post(
            f"{self._base}/search",
            json={
                "api_key": ctx.credential_value,
                "query": query.text,
                "max_results": query.max_results,
                "include_raw_content": False,
                "search_depth": "advanced" if (query.extra or {}).get("advanced") else "basic",
            },
        )
        if resp.status_code != 200:
            msg = f"tavily search failed: {resp.status_code} {resp.text[:200]}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body.get("results", []):
            url = r.get("url") or ""
            out.append(
                ConnectorResult(
                    external_id=url,
                    title=r.get("title") or url,
                    url=url,
                    snippet=r.get("content"),
                    metadata={
                        "query": query.text,
                        "score": r.get("score", 0.0),
                        "published_date": r.get("published_date"),
                        "domain": _domain_of(url),
                    },
                )
            )
        return out

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        if not result.url:
            msg = "Tavily fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(
            result.url,
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": "Aleph/0.1 (+https://github.com/UMD-ARLIS/aleph)"},
        )
        if resp.status_code >= 400:
            msg = f"tavily fetch failed for {result.url}: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        return RawPayload(
            data=data,
            mime_type=resp.headers.get("content-type", "text/html").split(";")[0].strip(),
            sha256=hashlib.sha256(data).hexdigest(),
            extension="html",
            declared_metadata={
                "title": result.title,
                "url": result.url,
                **(result.metadata or {}),
            },
        )


def _domain_of(url: str) -> str | None:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None
