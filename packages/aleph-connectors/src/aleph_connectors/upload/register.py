"""Upload connector adapter — exposes Inc 1's Upload through the Inc 3 Protocol."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

from aleph_connectors.base import (
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)
from aleph_wiki.connectors.upload import UploadMetadata


class UploadConnectorAdapter:
    kind: ClassVar[str] = "upload"
    output_kind: ClassVar[Literal["document", "dataset_rows"]] = "document"
    requires_auth: ClassVar[bool] = False
    metadata_schema: ClassVar[type[BaseModel]] = UploadMetadata

    async def search(
        self, ctx: ConnectorContext, query: SearchQuery
    ) -> list[ConnectorResult]:
        msg = "upload connector does not support search"
        raise NotSupported(msg)

    async def fetch(
        self, ctx: ConnectorContext, result: ConnectorResult
    ) -> RawPayload:
        msg = "upload fetch is handled in-process by POST /sources/upload"
        raise NotSupported(msg)
