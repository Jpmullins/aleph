"""Upload connector — file ingest. The only connector in Inc 1.

`search` is not supported (uploads are pushed, not searched). `fetch`
reads the previously-uploaded asset bytes from the AssetStore by storage_uri.
Inc 3 re-registers this as a `nat` function inside AIQ's data_source_registry.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aleph_rks.asset_store import AssetStore
from aleph_wiki.connectors.base import (
    ConnectorResult,
    NotSupported,
    ProjectScope,
    RawPayload,
    SearchQuery,
)


class UploadMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(min_length=1, max_length=128)
    uploader_id: UUID
    original_size: int = Field(ge=0)


_EXT_BY_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
    "text/markdown": "md",
    "text/plain": "txt",
    "application/epub+zip": "epub",
}


def extension_for_mime(mime_type: str, fallback_filename: str | None = None) -> str:
    base = _EXT_BY_MIME.get(mime_type)
    if base:
        return base
    if fallback_filename and "." in fallback_filename:
        return fallback_filename.rsplit(".", 1)[-1].lower()
    return "bin"


class UploadConnector:
    kind: ClassVar[str] = "upload"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = UploadMetadata

    def __init__(self, asset_store: AssetStore) -> None:
        self._assets = asset_store

    async def search(self, query: SearchQuery, project: ProjectScope) -> list[ConnectorResult]:
        msg = "upload connector does not support search"
        raise NotSupported(msg)

    async def ingest_bytes(
        self,
        *,
        project: ProjectScope,
        source_id: UUID,
        data: bytes,
        filename: str,
        mime_type: str,
    ) -> RawPayload:
        """Direct upload path. Stores the bytes and returns the canonical payload."""
        hashlib.sha256(data).hexdigest()
        ext = extension_for_mime(mime_type, filename)
        stored = self._assets.put_source_asset(
            project_id=project.project_id,
            source_id=source_id,
            data=data,
            mime_type=mime_type,
            extension=ext,
        )
        return RawPayload(
            data=data,
            mime_type=mime_type,
            sha256=stored.sha256,
            extension=ext,
            declared_metadata={
                "filename": filename,
                "uploader_id": str(project.user_id),
                "original_size": len(data),
                "storage_uri": stored.storage_uri,
            },
        )

    async def fetch(self, result: ConnectorResult, project: ProjectScope) -> RawPayload:
        storage_uri = result.metadata.get("storage_uri")
        expected_sha = result.metadata.get("sha256")
        if not isinstance(storage_uri, str):
            msg = "upload fetch requires storage_uri in result.metadata"
            raise NotSupported(msg)
        data = self._assets.get(storage_uri, expected_sha256=expected_sha)
        mime_type = result.metadata.get("mime_type", "application/octet-stream")
        ext = extension_for_mime(mime_type, result.metadata.get("filename"))
        sha = expected_sha or hashlib.sha256(data).hexdigest()
        return RawPayload(
            data=data,
            mime_type=mime_type,
            sha256=sha,
            extension=ext,
            declared_metadata=dict(result.metadata.items()),
        )
