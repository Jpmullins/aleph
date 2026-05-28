"""RenderedAsset / Artifact / ArtifactVersion models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class RenderedAsset(CommonColumns, Base):
    __tablename__ = "rendered_assets"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    source_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    dataset_version_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    output_format: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    render_spec_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Artifact(CommonColumns, Base):
    __tablename__ = "artifacts"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "version_no", name="uq_artifact_version_no"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    artifact_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    bytes_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    lineage_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    template_name: Mapped[str] = mapped_column(String(64), nullable=False)
    csl_style: Mapped[str] = mapped_column(String(64), nullable=False)
    builder_agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)
