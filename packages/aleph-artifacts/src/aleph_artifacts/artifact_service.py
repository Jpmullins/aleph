"""Artifact CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from aleph_artifacts.models import Artifact, ArtifactVersion
from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_security.principal import Principal


async def _next_short_id(session: AsyncSession) -> str:
    n = (await session.execute(select(func.count()).select_from(Artifact))).scalar_one()
    return f"A{int(n) + 1:04d}"


async def create_artifact(
    session: AsyncSession,
    *,
    principal: Principal,
    project_id: UUID,
    title: str,
    artifact_kind: str,
    description: str = "",
) -> Artifact:
    if artifact_kind not in (
        "report_pdf",
        "report_docx",
        "report_markdown_bundle",
        "source_pack",
        "deck_pdf",
    ):
        msg = f"invalid artifact_kind: {artifact_kind}"
        raise ValidationFailed(msg)
    a = Artifact(
        id=uuid7(),
        project_id=project_id,
        short_id=await _next_short_id(session),
        title=title[:512],
        artifact_kind=artifact_kind,
        description=description,
        current_version_id=None,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(a)
    await session.flush()
    return a


async def get_artifact(
    session: AsyncSession, *, project_id: UUID, artifact_id: UUID
) -> Artifact | None:
    return (
        await session.execute(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.project_id == project_id)
        )
    ).scalar_one_or_none()


async def list_artifacts(session: AsyncSession, *, project_id: UUID) -> list[Artifact]:
    stmt = (
        select(Artifact)
        .where(Artifact.project_id == project_id)
        .order_by(Artifact.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_versions(
    session: AsyncSession, *, project_id: UUID, artifact_id: UUID
) -> list[ArtifactVersion]:
    stmt = (
        select(ArtifactVersion)
        .where(
            ArtifactVersion.project_id == project_id,
            ArtifactVersion.artifact_id == artifact_id,
        )
        .order_by(ArtifactVersion.version_no.desc())
    )
    return list((await session.execute(stmt)).scalars().all())
