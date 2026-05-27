"""User + ProjectMember."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class User(CommonColumns, Base):
    __tablename__ = "users"

    subject: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    """OIDC `sub` claim. Identity boundary."""
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )


class ProjectMember(CommonColumns, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_proj_user"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
