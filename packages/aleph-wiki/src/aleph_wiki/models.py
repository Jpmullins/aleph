"""Wiki SQLAlchemy models.

WikiRevision is immutable — guarded by triggers from the Inc 1 migration.
WikiPage carries the current revision pointer. WikiSection enables
sub-page granularity for hand-edit + rejection feedback. Citations bridge
claims to chunks or to source pages.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class WikiPage(CommonColumns, Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_wiki_pages_project_slug"),
        Index("ix_wiki_pages_project_title", "project_id", "title"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="topic")
    current_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_stub: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="draft")
    last_compiled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WikiRevision(Base):
    """Immutable wiki revision. Triggers in the Inc 1 migration reject UPDATE/DELETE."""

    __tablename__ = "wiki_revisions"
    __table_args__ = (UniqueConstraint("page_id", "revision_no", name="uq_wiki_rev_page_no"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_message: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)


class WikiSection(Base):
    __tablename__ = "wiki_sections"
    __table_args__ = (
        UniqueConstraint("revision_id", "anchor", name="uq_wiki_sections_rev_anchor"),
        Index("ix_sections_page_rev", "page_id", "revision_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    anchor: Mapped[str] = mapped_column(String(512), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class WikiLink(Base):
    __tablename__ = "wiki_links"
    __table_args__ = (Index("ix_links_src_dst", "src_page_id", "dst_title"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    src_revision_id: Mapped[UUID] = mapped_column(nullable=False)
    dst_page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    dst_title: Mapped[str] = mapped_column(String(512), nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class WikiClaim(CommonColumns, Base):
    __tablename__ = "wiki_claims"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(String(2048), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, server_default="cited")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    claim_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    chunk_ids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    source_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    citation_marker: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SourcePage(Base):
    __tablename__ = "source_pages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    extracted_claims_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alias(CommonColumns, Base):
    __tablename__ = "aliases"
    __table_args__ = (
        UniqueConstraint("project_id", "surface_form", name="uq_aliases_project_surface"),
        Index("ix_aliases_project_canonical", "project_id", "canonical_name"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    surface_form: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_page_id: Mapped[UUID | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")


class HandEditMark(CommonColumns, Base):
    __tablename__ = "hand_edit_marks"
    __table_args__ = (
        Index(
            "ix_handedits_active",
            "page_id",
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_sha256_at_edit: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_by: Mapped[UUID] = mapped_column(nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[UUID | None] = mapped_column(nullable=True)


class RejectionFeedback(CommonColumns, Base):
    __tablename__ = "rejection_feedbacks"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    concept_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    rejected_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(4096), nullable=False)
    rejected_by: Mapped[UUID] = mapped_column(nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    addressed_in_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class WikiIndex(Base):
    __tablename__ = "wiki_index"
    __table_args__ = (
        Index("ix_wiki_index_tsv", "index_tsv", postgresql_using="gin"),
        Index("ix_wiki_index_project_title", "project_id", "title"),
    )

    page_id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    wikilinks_out_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    page_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_stub: Mapped[bool] = mapped_column(Boolean, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    index_tsv: Mapped[TSVECTOR] = mapped_column(TSVECTOR, nullable=False)


class PageMergeProposal(CommonColumns, Base):
    """A curator-detected near-duplicate: ``source`` should merge into ``target``.

    Human-gated (ApprovalCard) — the curator never merges automatically. On
    approval, ``CuratorService.apply_merge`` redirects inbound links
    source→target, aliases the source title to the target, and soft-deletes the
    source page. Reject leaves both pages. Approval is recorded as a generic
    ``ApprovalDecision`` (``target_kind="page_merge_proposal"``).
    """

    __tablename__ = "page_merge_proposals"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    rationale: Mapped[str] = mapped_column(String(2048), nullable=False, server_default="")
    similarity: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    approval_decision_id: Mapped[UUID | None] = mapped_column(nullable=True)
