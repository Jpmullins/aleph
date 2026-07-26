"""Project + ProjectMember repository functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.models.identity import ProjectMember
from aleph_db.models.project import Project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_project(session: AsyncSession, project_id: UUID) -> Project | None:
    return (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()


async def get_member(
    session: AsyncSession, *, project_id: UUID, user_id: UUID
) -> ProjectMember | None:
    return (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def get_member_role_and_status(
    session: AsyncSession, *, project_id: UUID, user_id: UUID
) -> tuple[str, str] | None:
    """`(member role, project status)`, or `None` if either is missing.

    One round trip instead of two. `project_scope_dep` runs on every
    project-scoped handler and every SSE connect, so resolving membership and
    status separately doubled the query count on the hottest path in the API.

    The INNER JOIN also closes a gap the separate queries left open: a
    membership row whose project has been hard-deleted used to satisfy the
    membership check and then fail — or not — depending on what the caller did
    next. Here it simply resolves to `None`, and the caller reports the project
    as absent, which is what it is.
    """
    stmt = (
        select(ProjectMember.role, Project.status)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    return (str(row[0]), str(row[1])) if row is not None else None


async def list_member_projects(
    session: AsyncSession, *, user_id: UUID, include_deleted: bool = False
) -> list[Project]:
    """The caller's projects, newest first.

    `include_deleted` exists so the restore path is reachable. Refusing writes
    to a deleted project is only half a fix: the other half is being able to
    *find* the project the error tells you to restore. Without this a user whose
    project was deleted had no route back short of knowing its UUID — which is
    exactly how a real research corpus became unreachable.
    """
    stmt = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    if not include_deleted:
        stmt = stmt.where(Project.status != "deleted")
    return list((await session.execute(stmt)).scalars().all())


def new_project(
    *,
    title: str,
    description: str,
    model_profile_id: UUID,
    created_by: UUID,
) -> Project:
    return Project(
        id=uuid7(),
        title=title,
        description=description,
        model_profile_id=model_profile_id,
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
