"""OpenAlex connector — open scholarly graph.

No API key required; OpenAlex asks for a `mailto` tag for polite use.
"""

from __future__ import annotations

import hashlib
import os
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

_API = "https://api.openalex.org"


class OpenAlexMetadata(BaseModel):
    work_id: str
    doi: str | None = None
    publication_year: int | None = None
    cited_by_count: int | None = None
    referenced_works: list[str] = []
    open_access_pdf_url: str | None = None


class OpenAlexConnector:
    kind: ClassVar[str] = "openalex"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = OpenAlexMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]:
        mailto = os.environ.get("ALEPH_OPENALEX_MAILTO", "")
        params = {
            "search": query.text,
            "per_page": str(min(query.max_results, 25)),
        }
        if mailto:
            params["mailto"] = mailto
        resp = await self._http.get(f"{_API}/works", params=params)
        if resp.status_code != 200:
            msg = f"openalex search failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body.get("results", []):
            work_id = r.get("id") or ""
            short_id = work_id.rsplit("/", 1)[-1] if work_id else ""
            title = r.get("title") or short_id
            doi = r.get("doi")
            oa_pdf = (r.get("open_access") or {}).get("oa_url") or (
                r.get("primary_location") or {}
            ).get("pdf_url")
            out.append(
                ConnectorResult(
                    external_id=short_id,
                    title=title,
                    url=oa_pdf or doi or work_id,
                    snippet=r.get("abstract_inverted_index_text"),
                    metadata={
                        "work_id": short_id,
                        "doi": doi,
                        "publication_year": r.get("publication_year"),
                        "cited_by_count": r.get("cited_by_count"),
                        "referenced_works": r.get("referenced_works", [])[:50],
                        "open_access_pdf_url": oa_pdf,
                    },
                )
            )
        return out

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload:
        url = result.url
        if not url:
            msg = "openalex fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"openalex fetch failed for {url}: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        ct = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
        ext = "pdf" if "pdf" in ct else "html" if "html" in ct else "bin"
        return RawPayload(
            data=data,
            mime_type=ct,
            sha256=hashlib.sha256(data).hexdigest(),
            extension=ext,
            declared_metadata={
                "title": result.title,
                "url": url,
                **(result.metadata or {}),
            },
        )
