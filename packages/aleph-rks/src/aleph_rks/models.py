"""RKS SQLAlchemy models.

Source → SourceVersion(N) → SourceAsset; each SourceVersion produces a
NormalizedDocument which produces DocumentChunk rows. A
RetrievalIndexRecord tracks per-source embedding state.

Chunks are used for intra-source descent only — see top-level spec §3.3.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns

#: Width of the ``document_chunks.embedding`` pgvector column. This is the
#: single source of truth for the embedding dimension the store can hold; an
#: embedder whose output dimension differs cannot be written and must be
#: rejected *before* any (billed) embed call. See ``aleph_rks.embedding``.
EMBEDDING_DIM = 1024


class Connector(CommonColumns, Base):
    __tablename__ = "connectors"

    kind: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    output_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    metadata_schema_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled_by_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )


class ConnectorBinding(CommonColumns, Base):
    __tablename__ = "connector_bindings"
    __table_args__ = (
        UniqueConstraint("project_id", "connector_id", name="uq_binding_project_connector"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_id: Mapped[UUID] = mapped_column(nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    config_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")


class Source(CommonColumns, Base):
    __tablename__ = "sources"
    __table_args__ = (
        Index("ix_sources_project_status", "project_id", "status"),
        Index("ix_sources_short_id", "short_id"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_metadata_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ingested")
    failure_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # WP-6 trust layer. A retracted source forces every page that cites it
    # unfresh; the retraction service (aleph_reviewer) sets these + status "retracted"
    # and flags dependent claims. Nullable (unset = not retracted).
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retraction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceVersion(CommonColumns, Base):
    __tablename__ = "source_versions"
    __table_args__ = (UniqueConstraint("source_id", "version_no", name="uq_source_version_no"),)

    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_id: Mapped[UUID] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_document_id: Mapped[UUID | None] = mapped_column(nullable=True)


class SourceAsset(CommonColumns, Base):
    __tablename__ = "source_assets"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class NormalizedDocument(CommonColumns, Base):
    __tablename__ = "normalized_documents"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_version_id: Mapped[UUID] = mapped_column(nullable=False)
    markdown_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    parser: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    structure_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    quality_flags_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")


class DocumentChunk(Base):
    """Embedded retrieval unit — the corpus-wide retrieval substrate.

    Was "INTRA-SOURCE DESCENT ONLY": embeddings were forbidden as first-line
    retrieval, so this table could only be searched within one source. That
    restriction, plus a wiki index that never covered page bodies, is why the
    system could not find text by the words it was written in. Both entry points
    now live in `aleph_rks.retrieval` — `search_corpus` across a project, and
    `descend_into_source` pinned to one document.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_chunks_source_ord", "source_id", "ordinal"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_text_fts", "text_tsv", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    normalized_document_id: Mapped[UUID] = mapped_column(nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_tsv: Mapped[TSVECTOR] = mapped_column(TSVECTOR, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedder_model: Mapped[str] = mapped_column(String(128), nullable=False)


class RetrievalIndexRecord(CommonColumns, Base):
    __tablename__ = "retrieval_index_records"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    embedder_model: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
