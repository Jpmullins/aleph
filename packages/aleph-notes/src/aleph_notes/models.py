"""Note + NoteSection models."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class Note(CommonColumns, Base):
    __tablename__ = "notes"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)


class NoteSection(CommonColumns, Base):
    __tablename__ = "note_sections"
    __table_args__ = (
        UniqueConstraint("note_id", "ordinal", name="uq_note_sections_note_ord"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    note_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    anchor: Mapped[str | None] = mapped_column(String(255), nullable=True)
