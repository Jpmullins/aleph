"""Connector framework — typed source-kind plugin protocol.

Inc 1 had a Protocol in aleph-wiki/connectors. Inc 3 promotes it into
its own package and extends it with:
  * `ConnectorContext` — carries the agent_token + project scope + the
    credential resolver. Inc 1 didn't need this since the Upload
    connector ran in-process; the research worker constructs it per
    job so identity plumbs through to every fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)
from uuid import UUID

from pydantic import BaseModel


class NotSupported(Exception):
    pass


@dataclass(frozen=True)
class ConnectorContext:
    project_id: UUID
    user_id: UUID
    agent_run_id: UUID | None
    correlation_id: str | None
    credential_value: str | None
    """Decrypted, never logged. Carried only for the lifetime of the call."""


@dataclass(frozen=True)
class SearchQuery:
    text: str
    max_results: int = 10
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConnectorResult:
    external_id: str
    title: str
    url: str | None
    snippet: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class RawPayload:
    data: bytes
    mime_type: str
    sha256: str
    extension: str
    declared_metadata: dict[str, Any]


@runtime_checkable
class ConnectorBase(Protocol):
    kind: ClassVar[str]
    output_kind: ClassVar[Literal["document", "dataset_rows"]]
    requires_auth: ClassVar[bool]
    metadata_schema: ClassVar[type[BaseModel]]

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]: ...

    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload: ...
