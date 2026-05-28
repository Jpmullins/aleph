"""RenderedAsset service.

Inc 7 records a `RenderedAsset` per render and uploads the bytes via
AssetStore. The actual chromium / WeasyPrint invocation lives in the
`render_card_job` worker (so the API isn't blocked while a chart
renders). This service is the persistence + bookkeeping path.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from aleph_artifacts.models import RenderedAsset
from aleph_core.ids import uuid7

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_rks.asset_store import AssetStore
    from aleph_security.principal import Principal


async def record_render(
    session: "AsyncSession",
    *,
    asset_store: "AssetStore",
    principal: "Principal",
    project_id: UUID,
    source_kind: str,
    source_id: UUID,
    source_version_id: UUID | None,
    dataset_version_ids: list[UUID],
    output_format: str,
    data: bytes,
    render_spec: dict[str, Any],
    width_px: int | None = None,
    height_px: int | None = None,
) -> RenderedAsset:
    """Upload bytes to MinIO and write the RenderedAsset row."""
    sha = hashlib.sha256(data).hexdigest()
    ext = output_format.lower()
    key = f"projects/{project_id}/renders/{source_id}/{sha[:12]}.{ext}"
    from io import BytesIO

    asset_store._client.put_object(  # noqa: SLF001
        asset_store._bucket,  # noqa: SLF001
        key,
        data=BytesIO(data),
        length=len(data),
        content_type={
            "png": "image/png",
            "svg": "image/svg+xml",
            "pdf": "application/pdf",
        }.get(ext, "application/octet-stream"),
    )
    storage_uri = f"s3://{asset_store._bucket}/{key}"  # noqa: SLF001
    asset = RenderedAsset(
        id=uuid7(),
        project_id=project_id,
        source_kind=source_kind,
        source_id=source_id,
        source_version_id=source_version_id,
        dataset_version_ids=[str(d) for d in dataset_version_ids],
        output_format=ext,
        storage_uri=storage_uri,
        width_px=width_px,
        height_px=height_px,
        bytes_size=len(data),
        sha256=sha,
        render_spec_jsonb=render_spec,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(asset)
    await session.flush()
    return asset
