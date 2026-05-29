"""Serper (Google Scholar) connector.

Serper.dev exposes Google's serps including a `scholar` endpoint.
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


class SerperMetadata(BaseModel):
    query: str
    cited_by: int | None = None
    publication_info: str | None = None
    year: int | None = None


class SerperConnector:
    kind: ClassVar[str] = "serper"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = True
    metadata_schema: ClassVar[type[BaseModel]] = SerperMetadata

    def __init__(
        self,
        *,
        base_url: str = "https://google.serper.dev",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        if not ctx.credential_value:
            msg = "Serper requires an API key"
            raise NotSupported(msg)
        resp = await self._http.post(
            f"{self._base}/scholar",
            json={"q": query.text, "num": query.max_results},
            headers={"X-API-KEY": ctx.credential_value, "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            msg = f"serper scholar failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body.get("organic", []):
            link = r.get("link") or ""
            out.append(
                ConnectorResult(
                    external_id=link or r.get("title") or "",
                    title=r.get("title") or link,
                    url=link,
                    snippet=r.get("snippet"),
                    metadata={
                        "query": query.text,
                        "cited_by": r.get("citedBy"),
                        "publication_info": r.get("publicationInfo"),
                        "year": r.get("year"),
                    },
                )
            )
        return out

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        if not result.url:
            msg = "serper fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(result.url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"serper fetch failed: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        ct = resp.headers.get("content-type", "text/html").split(";")[0].strip()
        ext = "pdf" if "pdf" in ct else "html"
        return RawPayload(
            data=data,
            mime_type=ct,
            sha256=hashlib.sha256(data).hexdigest(),
            extension=ext,
            declared_metadata={
                "title": result.title,
                "url": result.url,
                **(result.metadata or {}),
            },
        )
