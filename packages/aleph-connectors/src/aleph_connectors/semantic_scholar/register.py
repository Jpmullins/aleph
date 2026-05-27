"""Semantic Scholar Graph API connector.

API: https://api.semanticscholar.org/graph/v1. Optional API key.
Returns paper metadata plus a small citation-graph hint stored on
`Source.source_metadata_jsonb.citations` for downstream Inc 5+ use.
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

_API = "https://api.semanticscholar.org/graph/v1"


class SemanticScholarMetadata(BaseModel):
    paper_id: str
    doi: str | None = None
    year: int | None = None
    citation_count: int | None = None
    references: list[str] = []
    abstract: str | None = None


class SemanticScholarConnector:
    kind: ClassVar[str] = "semantic_scholar"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = SemanticScholarMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(
        self, ctx: ConnectorContext, query: SearchQuery
    ) -> list[ConnectorResult]:
        headers = {}
        if ctx.credential_value:
            headers["x-api-key"] = ctx.credential_value
        resp = await self._http.get(
            f"{_API}/paper/search",
            params={
                "query": query.text,
                "limit": str(query.max_results),
                "fields": "title,externalIds,year,citationCount,openAccessPdf,abstract,references",
            },
            headers=headers,
        )
        if resp.status_code != 200:
            msg = f"semantic scholar search failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body.get("data", []):
            paper_id = r.get("paperId") or ""
            title = r.get("title") or paper_id
            pdf = (r.get("openAccessPdf") or {}).get("url")
            ext_ids = r.get("externalIds") or {}
            doi = ext_ids.get("DOI")
            url = pdf or (f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{paper_id}")
            refs = [
                ref.get("paperId")
                for ref in (r.get("references") or [])
                if ref.get("paperId")
            ][:50]
            out.append(
                ConnectorResult(
                    external_id=paper_id,
                    title=title,
                    url=url,
                    snippet=r.get("abstract"),
                    metadata={
                        "paper_id": paper_id,
                        "doi": doi,
                        "year": r.get("year"),
                        "citation_count": r.get("citationCount"),
                        "references": refs,
                        "abstract": r.get("abstract"),
                    },
                )
            )
        return out

    async def fetch(
        self, ctx: ConnectorContext, result: ConnectorResult
    ) -> RawPayload:
        if not result.url:
            msg = "semantic_scholar fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(result.url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"semantic_scholar fetch failed: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        ct = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
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
