"""The one authenticated asset-byte streaming route (WP-1).

All asset bytes — raw source files, rendered assets, artifact versions —
reach the browser through this route, inside the principal boundary:
AuthMiddleware resolves the principal
and ProjectScopeDep enforces membership (404 across projects). Storage is
never exposed directly; there are no signed direct-to-store URLs.

Ledger posture: none — rule 4 covers mutations; reads stay observable via
request logs and OTEL spans, same as every other GET.

Kinds: `source` is consumed by the Library viewer today. `rendered` and
`artifact-version` are wired now (GOAL rule 2 written reason) because F1
mandates *all* asset bytes flow through this one route and WP-4's catalog
components consume artifacts strictly by URI against it — adding per-kind
routes later would fork the boundary this route exists to unify.

The authorization half is pinned by
`apps/api/tests/unit/test_asset_stream_auth.py`. The positive-path integration
test this docstring used to name was deleted in the harness reset and has not
been restored, so the happy path is currently unpinned — said here rather than
left as a citation of a file that is not there.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_artifacts.models import ArtifactVersion, RenderedAsset
from aleph_core.errors import NotFound
from aleph_rks.asset_store import AssetStoreError
from aleph_rks.models import Source, SourceAsset, SourceVersion

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_rks.asset_store import AssetStore

router = APIRouter(prefix="/v1/projects", tags=["assets"])

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

_RENDER_MIME = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
}

_ARTIFACT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "zip": "application/zip",
    # WP-4c code_runner artifact versions (image / chart / html_frame).
    "png": "image/png",
    "svg": "image/svg+xml",
    "json": "application/json",
    "html": "text/html; charset=utf-8",
}


def _safe_filename(stem: str, ext: str) -> str:
    # Both parts are sanitized: the ext can originate from an uploader-chosen
    # filename, and it lands inside a quoted Content-Disposition value.
    stem = _FILENAME_UNSAFE.sub("-", stem).strip("-") or "asset"
    ext = _FILENAME_UNSAFE.sub("", ext).strip(".")
    return f"{stem}.{ext}" if ext else stem


def _ext_of(storage_uri: str) -> str:
    tail = storage_uri.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1] if "." in tail else ""


async def _resolve_source(
    session: AsyncSession, project_id: UUID, asset_id: UUID
) -> tuple[str, str, str, str]:
    src = (
        await session.execute(
            select(Source).where(Source.id == asset_id, Source.project_id == project_id)
        )
    ).scalar_one_or_none()
    if src is None or src.current_version_id is None:
        msg = f"source asset not available: {asset_id}"
        raise NotFound(msg)
    version = (
        await session.execute(
            select(SourceVersion).where(SourceVersion.id == src.current_version_id)
        )
    ).scalar_one_or_none()
    if version is None:
        msg = f"source version missing: {asset_id}"
        raise NotFound(msg)
    asset = (
        await session.execute(select(SourceAsset).where(SourceAsset.id == version.asset_id))
    ).scalar_one_or_none()
    if asset is None:
        msg = f"source asset row missing: {asset_id}"
        raise NotFound(msg)
    filename = _safe_filename(src.short_id, _ext_of(asset.storage_uri))
    return asset.storage_uri, asset.mime_type, asset.sha256, filename


async def _resolve_rendered(
    session: AsyncSession, project_id: UUID, asset_id: UUID
) -> tuple[str, str, str, str]:
    row = (
        await session.execute(
            select(RenderedAsset).where(
                RenderedAsset.id == asset_id, RenderedAsset.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        msg = f"rendered asset not found: {asset_id}"
        raise NotFound(msg)
    mime = _RENDER_MIME.get(row.output_format, "application/octet-stream")
    filename = _safe_filename(str(row.id), row.output_format)
    return row.storage_uri, mime, row.sha256, filename


async def _resolve_artifact_version(
    session: AsyncSession, project_id: UUID, asset_id: UUID
) -> tuple[str, str, str, str]:
    row = (
        await session.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.id == asset_id, ArtifactVersion.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        msg = f"artifact version not found: {asset_id}"
        raise NotFound(msg)
    ext = _ext_of(row.storage_uri)
    mime = _ARTIFACT_MIME.get(ext, "application/octet-stream")
    filename = _safe_filename(f"artifact-v{row.version_no}", ext)
    return row.storage_uri, mime, row.sha256, filename


@router.get("/{project_id}/assets/{asset_kind}/{asset_id}")
async def stream_asset(
    request: Request,
    project_id: ProjectScopeDep,
    asset_kind: Literal["source", "rendered", "artifact-version"],
    asset_id: UUID,
    session: SessionDep,
) -> Response:
    """Stream stored bytes for a project-scoped asset row."""
    if asset_kind == "source":
        storage_uri, mime, sha256, filename = await _resolve_source(session, project_id, asset_id)
    elif asset_kind == "rendered":
        storage_uri, mime, sha256, filename = await _resolve_rendered(session, project_id, asset_id)
    else:
        storage_uri, mime, sha256, filename = await _resolve_artifact_version(
            session, project_id, asset_id
        )

    etag = f'"{sha256}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": f'inline; filename="{filename}"',
        # Stored mime types are caller-supplied at upload; never let the
        # browser second-guess them into something executable.
        "X-Content-Type-Options": "nosniff",
    }
    base_mime = mime.split(";")[0].strip()
    if base_mime != "application/pdf":
        # Uploaded HTML/SVG must never run script on the API origin — in
        # local auth mode every same-origin request carries ambient auth.
        # CSP `sandbox` applies iframe-sandbox semantics server-side (and,
        # unlike the iframe attribute, also covers direct URL opens). PDFs
        # are exempt: Chromium's PDF viewer refuses to render in a sandboxed
        # document (verified empirically), and it isn't page-context script.
        if asset_kind == "artifact-version" and base_mime == "text/html":
            # WP-4c interactive HtmlFrameCard artifact: scripts run ONLY inside
            # the sandbox's opaque origin (no `allow-same-origin` → no access to
            # the API origin's ambient auth/cookies). This is the amended-rule-8
            # escape hatch; the FE iframe carries the matching `allow-scripts`.
            headers["Content-Security-Policy"] = "sandbox allow-scripts"
        else:
            headers["Content-Security-Policy"] = "sandbox"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    store: AssetStore = request.app.state.asset_store
    try:
        chunks = store.stream(storage_uri)
    except AssetStoreError as exc:
        # The DB row exists but its bytes don't (or belong to the other
        # backend). stream() raises eagerly, so this becomes a clean 404
        # instead of a truncated 200.
        msg = f"asset bytes unavailable for {asset_kind} {asset_id}"
        raise NotFound(msg) from exc
    return StreamingResponse(chunks, media_type=mime, headers=headers)
