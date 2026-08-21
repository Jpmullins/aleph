"""Source-side business logic: registration, status transitions, asset writes.

`SourceService` is the only path that creates `Source` + `SourceVersion` +
`SourceAsset` triplets. The Upload connector calls this; future connectors
(Inc 3+) call it too. All mutations write a ledger event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_observability.tracing import current_trace_id
from aleph_rks.asset_store import AssetStore
from aleph_rks.models import Source, SourceAsset, SourceVersion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


@dataclass(frozen=True)
class SourceCreated:
    source: Source
    version: SourceVersion
    asset: SourceAsset


#: Allocated by Postgres, not by Python. See the docstring below.
_SHORT_ID_SEQUENCE = "sources_short_id_seq"


async def _next_short_id(session: AsyncSession) -> str:
    """Allocate the next `S0001`-style id from a sequence.

    This used to be `COUNT(*) + 1`, which is a lost-update race against a column
    carrying a **global** unique constraint. Every concurrent caller read the
    same count and returned the same id; the first insert won and the rest died
    with `UniqueViolationError`, surfacing as `ingest-url failed (500)`.

    It only failed under concurrency — which is exactly what a research run is.
    Eight papers ingested in the same second meant one success and seven
    failures, while ingesting the same papers one at a time worked perfectly.

    Counting was also non-monotonic: deleting a source lowered the count, so the
    next allocation reused an id already cited as `[[Source:S0042]]` in
    committed wiki prose, silently re-pointing those citations at a different
    paper. A sequence never goes backwards, so an id is issued at most once.
    """
    n = (await session.execute(select(func.nextval(_SHORT_ID_SEQUENCE)))).scalar_one()
    return f"S{int(n):04d}"


async def register_uploaded_source(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    principal: Principal,
    asset_store: AssetStore,
    project_id: UUID,
    title: str,
    data: bytes,
    filename: str,
    mime_type: str,
    connector_kind: str = "upload",
    source_metadata: dict[str, Any] | None = None,
) -> SourceCreated:
    """Store the bytes, create Source + SourceVersion + SourceAsset rows,
    set Source.status="normalizing", emit ledger events.

    `connector_kind` records the real origin (WP-2 §5: openalex | crossref |
    consensus | arxiv | ... — callers validate it against the connectors
    table); `source_metadata` is merged onto `source_metadata_jsonb`
    (scholarly identity: doi, openalex_id, doi_verdict). Defaults preserve
    the historical upload behavior.
    """
    source_id = uuid7()
    short_id = await _next_short_id(session)

    # Store bytes. Upload connector stores by (project_id, source_id, sha256, ext).
    extension = (filename.rsplit(".", 1)[-1] if "." in filename else "bin").lower()[:16]
    stored = asset_store.put_source_asset(
        project_id=project_id,
        source_id=source_id,
        data=data,
        mime_type=mime_type,
        extension=extension,
    )

    sha = stored.sha256

    asset = SourceAsset(
        id=uuid7(),
        project_id=project_id,
        storage_uri=stored.storage_uri,
        mime_type=mime_type,
        size_bytes=stored.size_bytes,
        sha256=sha,
        created_by=principal.user_id,
    )
    session.add(asset)
    await session.flush()

    source = Source(
        id=source_id,
        project_id=project_id,
        connector_kind=connector_kind,
        external_id=f"{principal.user_id}::{filename}",
        title=title or filename,
        url=None,
        source_metadata_jsonb={
            "filename": filename,
            "uploader_id": str(principal.user_id),
            "original_size": len(data),
            "storage_uri": stored.storage_uri,
            **(source_metadata or {}),
        },
        short_id=short_id,
        status="normalizing",
        current_version_id=None,
        created_by=principal.user_id,
    )
    session.add(source)
    await session.flush()

    version = SourceVersion(
        id=uuid7(),
        source_id=source_id,
        version_no=1,
        asset_id=asset.id,
        sha256=sha,
        fetched_at=utcnow(),
        parser_version=None,
        normalized_document_id=None,
        created_by=principal.user_id,
    )
    session.add(version)
    source.current_version_id = version.id
    await session.flush()

    trace_id = current_trace_id()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="source.create",
        target_id=source_id,
        target_kind="source",
        payload={
            "short_id": short_id,
            "title": source.title,
            "connector_kind": connector_kind,
            "mime_type": mime_type,
            "sha256": sha,
            "size_bytes": len(data),
        },
        trace_id=trace_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="source_version.create",
        target_id=version.id,
        target_kind="source_version",
        payload={"source_id": str(source_id), "version_no": 1, "sha256": sha},
        trace_id=trace_id,
    )

    return SourceCreated(source=source, version=version, asset=asset)


async def mark_status(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    source_id: UUID,
    status: str,
    failure_reason: str | None = None,
) -> None:
    src = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one_or_none()
    if src is None:
        msg = f"source {source_id} not found"
        raise ValueError(msg)
    prior = src.status
    src.status = status
    if failure_reason is not None:
        src.failure_reason = failure_reason[:2048]
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="source.status_change",
        target_id=source_id,
        target_kind="source",
        payload={"from": prior, "to": status, "failure_reason": failure_reason},
        trace_id=current_trace_id(),
    )
