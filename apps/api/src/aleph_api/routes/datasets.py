"""Datasets API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound
from aleph_datasets.dataset_service import (
    commit_version,
    create_dataset,
    get_dataset,
    list_datasets,
    list_observations,
)
from aleph_datasets.models import DatasetVersion
from aleph_datasets.vega_compile import (
    bar_chart_spec,
    line_chart_spec,
    scatter_chart_spec,
)
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["datasets"])


class DatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    short_id: str
    name: str
    description: str
    dataset_kind: str
    source_connector_kind: str | None
    current_version_id: UUID | None
    column_schema_jsonb: list
    created_at: datetime


class DatasetCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4096)
    dataset_kind: str = Field(pattern=r"^(tabular|geo|graph)$")
    column_schema: list[dict[str, Any]] = Field(default_factory=list)
    source_connector_kind: str | None = None


class CommitVersionIn(BaseModel):
    rows: list[dict[str, Any]]
    commit_message: str = ""
    parquet_uri: str | None = None


class DatasetVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    dataset_id: UUID
    version_no: int
    row_count: int
    column_schema_jsonb: list
    parquet_uri: str | None
    rows_inline: bool
    data_sha256: str
    diff_summary_jsonb: dict
    created_at: datetime


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    ordinal: int
    payload_jsonb: dict
    source_refs_jsonb: list


class ChartSpecIn(BaseModel):
    mark: str = Field(default="line", pattern=r"^(line|bar|scatter)$")
    x_field: str
    y_field: str
    color_field: str | None = None
    title: str = ""


@router.get("/{project_id}/datasets", response_model=list[DatasetOut])
async def get_datasets(project_id: ProjectScopeDep, session: SessionDep) -> list[DatasetOut]:
    rows = await list_datasets(session, project_id=project_id)
    return [DatasetOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/datasets",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetOut,
)
async def post_dataset(
    project_id: ProjectScopeDep,
    body: Annotated[DatasetCreateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> DatasetOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    ds = await create_dataset(
        session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        name=body.name,
        description=body.description,
        dataset_kind=body.dataset_kind,
        column_schema=body.column_schema or None,
        source_connector_kind=body.source_connector_kind,
    )
    return DatasetOut.model_validate(ds)


@router.get("/{project_id}/datasets/{dataset_id}", response_model=DatasetOut)
async def get_one_dataset(
    project_id: ProjectScopeDep,
    dataset_id: UUID,
    session: SessionDep,
) -> DatasetOut:
    ds = await get_dataset(session, project_id=project_id, dataset_id=dataset_id)
    if ds is None:
        msg = f"dataset {dataset_id} not found"
        raise NotFound(msg)
    return DatasetOut.model_validate(ds)


@router.post(
    "/{project_id}/datasets/{dataset_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetVersionOut,
)
async def post_version(
    project_id: ProjectScopeDep,
    dataset_id: UUID,
    body: Annotated[CommitVersionIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> DatasetVersionOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    v = await commit_version(
        session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        dataset_id=dataset_id,
        rows=body.rows,
        commit_message=body.commit_message,
        parquet_uri=body.parquet_uri,
    )
    return DatasetVersionOut.model_validate(v)


@router.get(
    "/{project_id}/datasets/{dataset_id}/versions",
    response_model=list[DatasetVersionOut],
)
async def list_versions(
    project_id: ProjectScopeDep,
    dataset_id: UUID,
    session: SessionDep,
) -> list[DatasetVersionOut]:
    stmt = (
        select(DatasetVersion)
        .where(
            DatasetVersion.project_id == project_id,
            DatasetVersion.dataset_id == dataset_id,
        )
        .order_by(DatasetVersion.version_no.desc())
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [DatasetVersionOut.model_validate(r) for r in rows]


@router.get(
    "/{project_id}/dataset-versions/{version_id}/observations",
    response_model=list[ObservationOut],
)
async def get_observations(
    project_id: ProjectScopeDep,
    version_id: UUID,
    session: SessionDep,
) -> list[ObservationOut]:
    rows = await list_observations(session, project_id=project_id, dataset_version_id=version_id)
    return [ObservationOut.model_validate(r) for r in rows]


@router.post("/{project_id}/dataset-versions/{version_id}/chart-spec")
async def chart_spec(
    project_id: ProjectScopeDep,
    version_id: UUID,
    body: Annotated[ChartSpecIn, Body()],
    session: SessionDep,
) -> dict[str, Any]:
    """Compile a Vega-Lite spec from a DatasetVersion + axis hints.

    Inline-row datasets are inlined into the spec; parquet-backed
    datasets get a placeholder data ref the client resolves via a
    follow-on streaming endpoint.
    """
    version = await session.get(DatasetVersion, version_id)
    if version is None or version.project_id != project_id:
        msg = f"dataset version {version_id} not found"
        raise NotFound(msg)
    rows: list[dict[str, Any]] = []
    if version.rows_inline:
        obs = await list_observations(session, project_id=project_id, dataset_version_id=version_id)
        rows = [o.payload_jsonb for o in obs]
    if body.mark == "line":
        spec = line_chart_spec(
            rows=rows,
            x_field=body.x_field,
            y_field=body.y_field,
            color_field=body.color_field,
            title=body.title,
        )
    elif body.mark == "bar":
        spec = bar_chart_spec(
            rows=rows, x_field=body.x_field, y_field=body.y_field, title=body.title
        )
    else:
        spec = scatter_chart_spec(
            rows=rows,
            x_field=body.x_field,
            y_field=body.y_field,
            color_field=body.color_field,
            title=body.title,
        )
    return {"dataset_version_id": str(version.id), "vega_lite_spec": spec}
