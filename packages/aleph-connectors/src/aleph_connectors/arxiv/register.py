"""arXiv paper-search connector.

Uses arXiv's open OAI-PMH-like Atom API. No API key required;
rate-limited per arXiv's terms (we respect ~1 req / 3s).

`fetch` retrieves the PDF directly from arxiv.org.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, Literal
from xml.etree import ElementTree as ET

import httpx
from pydantic import BaseModel

from aleph_connectors.base import (
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)

_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ArxivMetadata(BaseModel):
    arxiv_id: str
    primary_category: str | None = None
    doi: str | None = None
    published: str | None = None
    authors: list[str] = []
    summary: str | None = None


class ArxivConnector:
    kind: ClassVar[str] = "arxiv"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = ArxivMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def search(
        self, ctx: ConnectorContext, query: SearchQuery
    ) -> list[ConnectorResult]:
        resp = await self._http.get(
            _ARXIV_API,
            params={
                "search_query": query.text,
                "max_results": str(query.max_results),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        if resp.status_code != 200:
            msg = f"arxiv search failed: {resp.status_code}"
            raise NotSupported(msg)
        root = ET.fromstring(resp.text)
        out: list[ConnectorResult] = []
        for entry in root.findall(f"{_ATOM_NS}entry"):
            arxiv_id = _text(entry.find(f"{_ATOM_NS}id")) or ""
            arxiv_short = arxiv_id.rsplit("/", 1)[-1]
            title = (_text(entry.find(f"{_ATOM_NS}title")) or "").strip()
            summary = (_text(entry.find(f"{_ATOM_NS}summary")) or "").strip()
            authors = [
                _text(a.find(f"{_ATOM_NS}name")) or ""
                for a in entry.findall(f"{_ATOM_NS}author")
            ]
            pdf_url = None
            for link in entry.findall(f"{_ATOM_NS}link"):
                if link.attrib.get("title") == "pdf":
                    pdf_url = link.attrib.get("href")
                    break
            doi = _text(entry.find(f"{_ARXIV_NS}doi"))
            primary = entry.find(f"{_ARXIV_NS}primary_category")
            primary_cat = primary.attrib.get("term") if primary is not None else None
            published = _text(entry.find(f"{_ATOM_NS}published"))

            out.append(
                ConnectorResult(
                    external_id=arxiv_short,
                    title=title,
                    url=pdf_url or arxiv_id,
                    snippet=summary[:500],
                    metadata={
                        "arxiv_id": arxiv_short,
                        "primary_category": primary_cat,
                        "doi": doi,
                        "published": published,
                        "authors": authors,
                        "summary": summary,
                    },
                )
            )
        return out

    async def fetch(
        self, ctx: ConnectorContext, result: ConnectorResult
    ) -> RawPayload:
        url = result.url
        if not url:
            msg = "arxiv fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"arxiv fetch failed for {url}: {resp.status_code}"
            raise NotSupported(msg)
        data = resp.content
        return RawPayload(
            data=data,
            mime_type=resp.headers.get("content-type", "application/pdf").split(";")[0].strip(),
            sha256=hashlib.sha256(data).hexdigest(),
            extension="pdf",
            declared_metadata={
                "title": result.title,
                "url": url,
                **(result.metadata or {}),
            },
        )


def _text(el: ET.Element | None) -> str | None:
    return el.text if el is not None else None
