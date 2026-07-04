"""Connector framework — typed source-kind plugin protocol.

Inc 1 only registers the Upload connector. The typed research
connector set lives in `aleph_connectors` and feeds the native
research loop.
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
    """Raised when a connector is asked to do something it does not support
    (e.g. `search` on the Upload connector)."""


@dataclass(frozen=True)
class ProjectScope:
    project_id: UUID
    user_id: UUID


@dataclass(frozen=True)
class SearchQuery:
    text: str
    max_results: int = 25
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConnectorResult:
    """A single discovered candidate from `search` — not yet fetched."""

    external_id: str
    title: str
    url: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RawPayload:
    """The bytes pulled by `fetch`, with declared mime type and a sha256.
    Stored as a `SourceAsset` upstream."""

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

    async def search(self, query: SearchQuery, project: ProjectScope) -> list[ConnectorResult]: ...

    async def fetch(self, result: ConnectorResult, project: ProjectScope) -> RawPayload: ...
