"""ModelProfile repository functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.models.model_profile import ModelProfile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_template(session: AsyncSession, name: str) -> ModelProfile | None:
    stmt = select(ModelProfile).where(
        ModelProfile.name == name,
        ModelProfile.is_template.is_(True),
        ModelProfile.project_id.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_templates(session: AsyncSession) -> list[ModelProfile]:
    stmt = (
        select(ModelProfile)
        .where(
            ModelProfile.is_template.is_(True),
            ModelProfile.project_id.is_(None),
        )
        .order_by(ModelProfile.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_project_profile(session: AsyncSession, project_id: UUID) -> ModelProfile | None:
    stmt = select(ModelProfile).where(ModelProfile.project_id == project_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_profile_by_id(session: AsyncSession, profile_id: UUID) -> ModelProfile | None:
    return (
        await session.execute(select(ModelProfile).where(ModelProfile.id == profile_id))
    ).scalar_one_or_none()


def new_project_profile(
    *,
    template: ModelProfile,
    project_id: UUID,
    created_by: UUID,
) -> ModelProfile:
    """Copy a template into a project-owned profile."""
    return ModelProfile(
        id=uuid7(),
        name=template.name,
        project_id=project_id,
        is_template=False,
        bindings_jsonb=dict(template.bindings_jsonb),
        created_by=created_by,
        access_scope="project",
    )
