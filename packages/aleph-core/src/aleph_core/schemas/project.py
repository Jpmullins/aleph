"""Project + ProjectMember schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ProjectRoleStr = Literal["owner", "editor", "viewer"]
ProjectStatusStr = Literal["active", "archived", "deleted"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4096)
    model_profile_name: str = Field(
        default="aleph-dev",
        pattern=r"^aleph-(dev|production)$",
        description="Template name to copy. Must match a seeded template.",
    )
    budget_usd: Decimal = Field(default=Decimal("100.00"), ge=Decimal("0.01"))


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    status: ProjectStatusStr | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str
    status: ProjectStatusStr
    model_profile_id: UUID
    budget_id: UUID | None
    created_at: datetime
    updated_at: datetime
    created_by: UUID


class ProjectMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    user_id: UUID
    role: ProjectRoleStr
    created_at: datetime
