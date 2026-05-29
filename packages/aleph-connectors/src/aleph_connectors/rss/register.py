"""RSS feed connector.

`search` parses a single feed URL (passed via SearchQuery.extra["feed_url"])
and returns each item as a result. `fetch` retrieves the article HTML.
No auth required.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, Literal

import feedparser
import httpx
from pydantic import BaseModel

from aleph_connectors.base import (
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)


class RSSMetadata(BaseModel):
    feed_url: str
    published: str | None = None
    author: str | None = None
    tags: list[str] = []


class RSSConnector:
    kind: ClassVar[str] = "rss"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = RSSMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        feed_url = (query.extra or {}).get("feed_url")
        if not feed_url:
            msg = "RSS connector requires extra['feed_url']"
            raise NotSupported(msg)
        resp = await self._http.get(feed_url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"rss fetch of {feed_url} failed: {resp.status_code}"
            raise NotSupported(msg)
        feed = feedparser.parse(resp.text)
        out: list[ConnectorResult] = []
        q_lower = query.text.lower().strip() if query.text else ""
        for entry in feed.entries[: query.max_results]:
            link = entry.get("link") or ""
            title = entry.get("title") or link
            summary = entry.get("summary") or entry.get("description") or ""
            if q_lower and q_lower not in (title + " " + summary).lower():
                continue
            tags = [t.term for t in (entry.get("tags") or []) if getattr(t, "term", None)]
            out.append(
                ConnectorResult(
                    external_id=link or title,
                    title=title,
                    url=link,
                    snippet=summary[:500],
                    metadata={
                        "feed_url": feed_url,
                        "published": entry.get("published"),
                        "author": entry.get("author"),
                        "tags": tags,
                    },
                )
            )
        return out

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        url = result.url
        if not url:
            msg = "rss fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"rss item fetch failed for {url}: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        return RawPayload(
            data=data,
            mime_type=resp.headers.get("content-type", "text/html").split(";")[0].strip(),
            sha256=hashlib.sha256(data).hexdigest(),
            extension="html",
            declared_metadata={
                "title": result.title,
                "url": url,
                **(result.metadata or {}),
            },
        )
