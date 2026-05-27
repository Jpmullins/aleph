"""Lens.org connector.

Disabled by default — `Connector.enabled_by_default=False` in the
inc3 migration seed. Enable per-project once the operator provides
`LENS_API_KEY`.
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

_API = "https://api.lens.org"


class LensMetadata(BaseModel):
    lens_id: str
    doi: str | None = None
    patent_number: str | None = None
    publication_type: str | None = None
    year: int | None = None


class LensConnector:
    kind: ClassVar[str] = "lens"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = True
    metadata_schema: ClassVar[type[BaseModel]] = LensMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(
        self, ctx: ConnectorContext, query: SearchQuery
    ) -> list[ConnectorResult]:
        if not ctx.credential_value:
            msg = "Lens.org connector is disabled until a credential is provided"
            raise NotSupported(msg)
        # Scholarly works endpoint. Patents API is similar but separate.
        resp = await self._http.post(
            f"{_API}/scholarly/search",
            json={"query": {"match": {"full_text": query.text}}, "size": query.max_results},
            headers={"Authorization": f"Bearer {ctx.credential_value}"},
        )
        if resp.status_code != 200:
            msg = f"lens search failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body.get("data", []):
            lens_id = r.get("lens_id") or r.get("id") or ""
            title = r.get("title") or lens_id
            doi = (r.get("external_ids") or {}).get("doi") if isinstance(r.get("external_ids"), dict) else None
            year = r.get("year_published")
            out.append(
                ConnectorResult(
                    external_id=lens_id,
                    title=title,
                    url=f"https://www.lens.org/lens/scholar/article/{lens_id}",
                    snippet=r.get("abstract"),
                    metadata={
                        "lens_id": lens_id,
                        "doi": doi,
                        "patent_number": None,
                        "publication_type": r.get("publication_type"),
                        "year": year,
                    },
                )
            )
        return out

    async def fetch(
        self, ctx: ConnectorContext, result: ConnectorResult
    ) -> RawPayload:
        if not result.url:
            msg = "lens fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(result.url, follow_redirects=True)
        if resp.status_code >= 400:
            msg = f"lens fetch failed: {resp.status_code}"
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
