"""Exa neural-web-search connector. Complementary to Tavily.

API: https://api.exa.ai (Bearer token in `x-api-key`).
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


class ExaMetadata(BaseModel):
    query: str
    score: float = 0.0
    published_date: str | None = None
    author: str | None = None


class ExaConnector:
    kind: ClassVar[str] = "exa"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = True
    metadata_schema: ClassVar[type[BaseModel]] = ExaMetadata

    def __init__(
        self,
        *,
        base_url: str = "https://api.exa.ai",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        if not ctx.credential_value:
            msg = "Exa requires an API key"
            raise NotSupported(msg)
        resp = await self._http.post(
            f"{self._base}/search",
            json={"query": query.text, "numResults": query.max_results},
            headers={"x-api-key": ctx.credential_value},
        )
        if resp.status_code != 200:
            msg = f"exa search failed: {resp.status_code} {resp.text[:200]}"
            raise NotSupported(msg)
        body = resp.json()
        return [
            ConnectorResult(
                external_id=r.get("id") or r.get("url") or "",
                title=r.get("title") or r.get("url") or "",
                url=r.get("url"),
                snippet=r.get("text"),
                metadata={
                    "query": query.text,
                    "score": r.get("score", 0.0),
                    "published_date": r.get("publishedDate"),
                    "author": r.get("author"),
                },
            )
            for r in body.get("results", [])
        ]

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        if not result.url:
            msg = "Exa fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(result.url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"exa fetch failed for {result.url}: {resp.status_code}"
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
