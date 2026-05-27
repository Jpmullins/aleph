"""HuggingFace Hub connector. Optional API key.

`search` queries the Hub for models / datasets / papers. `fetch`
retrieves the README of the matching repo as a markdown document.
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

_API = "https://huggingface.co/api"


class HuggingFaceHubMetadata(BaseModel):
    repo_id: str
    repo_type: str  # model | dataset | paper
    downloads: int | None = None
    likes: int | None = None
    tags: list[str] = []


class HuggingFaceHubConnector:
    kind: ClassVar[str] = "huggingface_hub"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = HuggingFaceHubMetadata

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client or httpx.AsyncClient(timeout=20.0)

    async def search(
        self, ctx: ConnectorContext, query: SearchQuery
    ) -> list[ConnectorResult]:
        headers = {}
        if ctx.credential_value:
            headers["Authorization"] = f"Bearer {ctx.credential_value}"
        repo_type = (query.extra or {}).get("repo_type", "models")
        if repo_type not in ("models", "datasets"):
            repo_type = "models"
        endpoint = f"{_API}/{repo_type}"
        resp = await self._http.get(
            endpoint,
            params={"search": query.text, "limit": str(query.max_results)},
            headers=headers,
        )
        if resp.status_code != 200:
            msg = f"hf hub search failed: {resp.status_code}"
            raise NotSupported(msg)
        body = resp.json()
        out: list[ConnectorResult] = []
        for r in body[: query.max_results]:
            repo_id = r.get("id") or r.get("modelId") or ""
            title = repo_id
            out.append(
                ConnectorResult(
                    external_id=f"{repo_type[:-1]}:{repo_id}",
                    title=title,
                    url=f"https://huggingface.co/{repo_id}/raw/main/README.md",
                    snippet=None,
                    metadata={
                        "repo_id": repo_id,
                        "repo_type": repo_type[:-1],
                        "downloads": r.get("downloads"),
                        "likes": r.get("likes"),
                        "tags": r.get("tags", [])[:25],
                    },
                )
            )
        return out

    async def fetch(
        self, ctx: ConnectorContext, result: ConnectorResult
    ) -> RawPayload:
        if not result.url:
            msg = "hf hub fetch requires a url"
            raise NotSupported(msg)
        resp = await self._http.get(result.url, follow_redirects=True)
        if resp.status_code >= 400:
            # README may not exist; persist a placeholder marker so the
            # caller knows the search succeeded but content was empty.
            data = (f"# {result.title}\n\n_No README at {result.url}._\n").encode("utf-8")
            return RawPayload(
                data=data,
                mime_type="text/markdown",
                sha256=hashlib.sha256(data).hexdigest(),
                extension="md",
                declared_metadata={
                    "title": result.title,
                    "url": result.url,
                    "empty": True,
                    **(result.metadata or {}),
                },
            )
        data = resp.content
        return RawPayload(
            data=data,
            mime_type="text/markdown",
            sha256=hashlib.sha256(data).hexdigest(),
            extension="md",
            declared_metadata={
                "title": result.title,
                "url": result.url,
                **(result.metadata or {}),
            },
        )
