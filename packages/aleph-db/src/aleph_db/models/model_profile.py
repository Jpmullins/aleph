"""ModelProfile.

Bindings live in a JSONB column rather than a separate table. The schema
is enforced by the Pydantic `ModelBindingIn`/`ModelBindingOut` in
aleph-core; updates flow through the API which validates before write.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class ModelProfile(CommonColumns, Base):
    __tablename__ = "model_profiles"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_profile_project_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    bindings_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
