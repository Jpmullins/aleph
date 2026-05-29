"""Dataset CRUD + version commit.

`commit_version` is atomic: writes the DatasetVersion + either inline
Observations or a parquet pointer. Switches between inline and parquet
based on the project's threshold (default: 1000 rows OR 100 KB).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_datasets.models import Dataset, DatasetVersion, Observation
from aleph_datasets.schema_inference import infer_column_schema
from aleph_observability.tracing import current_trace_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


_INLINE_ROW_LIMIT = 1000
_INLINE_BYTE_LIMIT = 100 * 1024


async def _next_short_id(session: AsyncSession) -> str:
    n = (await session.execute(select(func.count()).select_from(Dataset))).scalar_one()
    return f"D{int(n) + 1:04d}"


async def create_dataset(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    name: str,
    description: str,
    dataset_kind: str,
    column_schema: list[dict[str, Any]] | None,
    source_connector_kind: str | None = None,
) -> Dataset:
    if dataset_kind not in ("tabular", "geo", "graph"):
        msg = f"invalid dataset_kind: {dataset_kind}"
        raise ValidationFailed(msg)
    ds = Dataset(
        id=uuid7(),
        project_id=project_id,
        name=name[:255],
        description=description,
        dataset_kind=dataset_kind,
        source_connector_kind=source_connector_kind,
        short_id=await _next_short_id(session),
        current_version_id=None,
        column_schema_jsonb=column_schema or [],
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(ds)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="dataset.create",
        target_id=ds.id,
        target_kind="dataset",
        payload={"name": name, "short_id": ds.short_id, "kind": dataset_kind},
        trace_id=current_trace_id(),
    )
    return ds


async def get_dataset(
    session: AsyncSession, *, project_id: UUID, dataset_id: UUID
) -> Dataset | None:
    return (
        await session.execute(
            select(Dataset).where(Dataset.id == dataset_id, Dataset.project_id == project_id)
        )
    ).scalar_one_or_none()


async def list_datasets(session: AsyncSession, *, project_id: UUID) -> list[Dataset]:
    stmt = (
        select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_observations(
    session: AsyncSession,
    *,
    project_id: UUID,
    dataset_version_id: UUID,
) -> list[Observation]:
    stmt = (
        select(Observation)
        .where(
            Observation.project_id == project_id,
            Observation.dataset_version_id == dataset_version_id,
        )
        .order_by(Observation.ordinal.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _diff_summary(prior_rows: list[dict] | None, new_rows: list[dict]) -> dict:
    if not prior_rows:
        return {"added": len(new_rows), "removed": 0, "modified": 0}
    prior_keys = [json.dumps(r, sort_keys=True, default=str) for r in prior_rows]
    new_keys = [json.dumps(r, sort_keys=True, default=str) for r in new_rows]
    added = sum(1 for k in new_keys if k not in prior_keys)
    removed = sum(1 for k in prior_keys if k not in new_keys)
    return {"added": added, "removed": removed, "modified": 0}


async def commit_version(
    session: AsyncSession,
    *,
    ledger: LedgerWriter,
    principal: Principal,
    project_id: UUID,
    dataset_id: UUID,
    rows: Iterable[dict[str, Any]],
    commit_message: str = "",
    parquet_uri: str | None = None,
) -> DatasetVersion:
    """Inline path only in Inc 6's first commit. Parquet path is plumbed:
    callers pass parquet_uri when they have a parquet snapshot in MinIO."""
    ds = await get_dataset(session, project_id=project_id, dataset_id=dataset_id)
    if ds is None:
        msg = f"dataset {dataset_id} not found"
        raise NotFound(msg)
    rows_list = list(rows)
    inferred = infer_column_schema(rows_list)
    if not ds.column_schema_jsonb:
        ds.column_schema_jsonb = inferred

    serialized = json.dumps(rows_list, sort_keys=True, default=str).encode("utf-8")
    sha = hashlib.sha256(serialized).hexdigest()

    prior_obs: list[dict] | None = None
    if ds.current_version_id is not None:
        prior_obs_rows = list(
            (
                await session.execute(
                    select(Observation).where(
                        Observation.dataset_version_id == ds.current_version_id
                    )
                )
            )
            .scalars()
            .all()
        )
        prior_obs = [o.payload_jsonb for o in prior_obs_rows]

    max_no = (
        await session.execute(
            select(func.coalesce(func.max(DatasetVersion.version_no), 0)).where(
                DatasetVersion.dataset_id == dataset_id
            )
        )
    ).scalar_one()
    new_no = int(max_no) + 1

    diff = _diff_summary(prior_obs, rows_list)

    event = await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="dataset.version.commit",
        target_id=ds.id,
        target_kind="dataset",
        payload={
            "version_no": new_no,
            "row_count": len(rows_list),
            "data_sha256": sha,
            "commit_message": commit_message[:1024],
            "diff_summary": diff,
        },
        trace_id=current_trace_id(),
    )

    rows_inline = parquet_uri is None and (
        len(rows_list) <= _INLINE_ROW_LIMIT and len(serialized) <= _INLINE_BYTE_LIMIT
    )

    version = DatasetVersion(
        id=uuid7(),
        project_id=project_id,
        dataset_id=dataset_id,
        version_no=new_no,
        row_count=len(rows_list),
        column_schema_jsonb=inferred or ds.column_schema_jsonb,
        parquet_uri=parquet_uri,
        rows_inline=rows_inline,
        data_sha256=sha,
        parent_version_id=ds.current_version_id,
        diff_summary_jsonb=diff,
        author_kind=principal.actor_kind,
        author_id=principal.user_id,
        trace_id=current_trace_id(),
        ledger_event_id=event.id,
    )
    session.add(version)
    await session.flush()

    if rows_inline:
        for ordinal, row in enumerate(rows_list):
            session.add(
                Observation(
                    id=uuid7(),
                    project_id=project_id,
                    dataset_id=dataset_id,
                    dataset_version_id=version.id,
                    ordinal=ordinal,
                    payload_jsonb=row,
                    source_refs_jsonb=row.pop("__source_refs__", [])
                    if isinstance(row, dict)
                    else [],
                )
            )

    ds.current_version_id = version.id
    await session.flush()
    return version
