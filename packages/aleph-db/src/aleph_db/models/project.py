"""Project."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class Project(CommonColumns, Base):
    __tablename__ = "projects"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(4096), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    model_profile_id: Mapped[UUID] = mapped_column(nullable=False)
