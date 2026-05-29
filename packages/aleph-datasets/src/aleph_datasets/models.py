"""Dataset / DatasetVersion / Observation."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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


class Dataset(CommonColumns, Base):
    __tablename__ = "datasets"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_connector_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    column_schema_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version_no", name="uq_dataset_version_no"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    column_schema_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False)
    parquet_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    rows_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    data_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    diff_summary_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)


class Observation(Base):
    __tablename__ = "observations"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_version_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
