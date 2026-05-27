"""Project + ProjectMember + Budget repository functions."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.models.cost import Budget
from aleph_db.models.identity import ProjectMember
from aleph_db.models.project import Project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_project(session: "AsyncSession", project_id: UUID) -> Project | None:
    return (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()


async def get_member(
    session: "AsyncSession", *, project_id: UUID, user_id: UUID
) -> ProjectMember | None:
    return (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def list_member_projects(
    session: "AsyncSession", *, user_id: UUID
) -> list[Project]:
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id, Project.status != "deleted")
        .order_by(Project.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


def new_project(
    *,
    title: str,
    description: str,
    model_profile_id: UUID,
    budget_id: UUID | None,
    created_by: UUID,
) -> Project:
    return Project(
        id=uuid7(),
        title=title,
        description=description,
        model_profile_id=model_profile_id,
        budget_id=budget_id,
        created_by=created_by,
        access_scope="project",
    )


def new_member(
    *,
    project_id: UUID,
    user_id: UUID,
    role: str,
    created_by: UUID,
) -> ProjectMember:
    return ProjectMember(
        id=uuid7(),
        project_id=project_id,
        user_id=user_id,
        role=role,
        created_by=created_by,
        access_scope="project",
    )


def new_budget(
    *,
    project_id: UUID,
    cap_usd: Decimal,
    created_by: UUID,
) -> Budget:
    return Budget(
        id=uuid7(),
        project_id=project_id,
        cap_usd=cap_usd,
        created_by=created_by,
        access_scope="project",
    )
