"""inc1: RKS + wiki skeleton.

Revision ID: inc1_rks_wiki
Revises: inc0_initial
Create Date: 2026-05-27
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "inc1_rks_wiki"
down_revision: str | None = "inc0_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common(*extra: sa.Column) -> list[sa.Column]:
    """Build the CommonColumns set + caller-supplied extras."""
    base = [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "access_scope",
            sa.String(64),
            nullable=False,
            server_default="project",
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=True),
    ]
    return base + list(extra)


def upgrade() -> None:
    # ---- connectors / connector_bindings ------------------------------------
    op.create_table(
        "connectors",
        *_common(
            sa.Column("kind", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("output_kind", sa.String(16), nullable=False),
            sa.Column(
                "requires_auth",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "metadata_schema_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "enabled_by_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        ),
    )
    op.create_table(
        "connector_bindings",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "config_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
        ),
        sa.UniqueConstraint(
            "project_id", "connector_id", name="uq_binding_project_connector"
        ),
    )

    # ---- sources / versions / assets ---------------------------------------
    op.create_table(
        "sources",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("connector_kind", sa.String(64), nullable=False),
            sa.Column("external_id", sa.String(512), nullable=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("url", sa.String(2048), nullable=True),
            sa.Column(
                "source_metadata_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("short_id", sa.String(16), nullable=False, unique=True),
            sa.Column(
                "status",
                sa.String(32),
                nullable=False,
                server_default="ingested",
            ),
            sa.Column("failure_reason", sa.String(2048), nullable=True),
            sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )
    op.create_index("ix_sources_project_status", "sources", ["project_id", "status"])
    op.create_index("ix_sources_short_id", "sources", ["short_id"])

    op.create_table(
        "source_versions",
        *_common(
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("parser_version", sa.String(64), nullable=True),
            sa.Column("normalized_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        ),
        sa.UniqueConstraint("source_id", "version_no", name="uq_source_version_no"),
    )

    op.create_table(
        "source_assets",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("storage_uri", sa.String(1024), nullable=False),
            sa.Column("mime_type", sa.String(128), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False, index=True),
        ),
    )

    op.create_table(
        "normalized_documents",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("source_version_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("markdown_uri", sa.String(1024), nullable=False),
            sa.Column("parser", sa.String(64), nullable=False),
            sa.Column("parser_version", sa.String(64), nullable=False),
            sa.Column("char_count", sa.Integer(), nullable=False),
            sa.Column("token_count", sa.Integer(), nullable=False),
            sa.Column(
                "structure_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "quality_flags_jsonb",
                postgresql.JSONB(),
                nullable=False,
                server_default="[]",
            ),
        ),
    )

    # ---- document_chunks (pgvector + FTS) ----------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgvector")
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("normalized_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # TSVECTOR is created as a generated column from text below.
        sa.Column("text_tsv", postgresql.TSVECTOR(), nullable=False),
        sa.Column(
            "embedding",
            sa.dialects.postgresql.ARRAY(sa.Float(), dimensions=1).with_variant(
                # Fallback typing; the real column type is `vector(1024)`.
                sa.dialects.postgresql.ARRAY(sa.Float()),
                "postgresql",
            ),
            nullable=False,
        ),
        sa.Column("section_path", sa.String(512), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedder_model", sa.String(128), nullable=False),
    )
    # Replace the placeholder embedding column with a real pgvector column
    # (Alembic doesn't have a built-in for `vector(N)` until pgvector-sqlalchemy 0.4+
    # is loaded into env.py; we patch the type here at the SQL level).
    op.execute("ALTER TABLE document_chunks DROP COLUMN embedding")
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1024) NOT NULL")
    op.create_index("ix_chunks_source_ord", "document_chunks", ["source_id", "ordinal"])
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )
    op.create_index(
        "ix_chunks_text_fts",
        "document_chunks",
        ["text_tsv"],
        postgresql_using="gin",
    )
    # Maintain text_tsv via trigger so callers don't need to set it.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION document_chunks_tsvector_update() RETURNS TRIGGER AS $$
        BEGIN
          NEW.text_tsv := to_tsvector('english', coalesce(NEW.text, ''));
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER document_chunks_tsv_iu
        BEFORE INSERT OR UPDATE ON document_chunks
        FOR EACH ROW EXECUTE FUNCTION document_chunks_tsvector_update();
        """
    )

    op.create_table(
        "retrieval_index_records",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column(
                "source_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                unique=True,
            ),
            sa.Column("embedder_model", sa.String(128), nullable=False),
            sa.Column("chunk_count", sa.Integer(), nullable=False),
            sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        ),
    )

    # ---- wiki: pages, revisions, sections, links, claims, citations -------
    op.create_table(
        "wiki_pages",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("slug", sa.String(512), nullable=False),
            sa.Column(
                "page_kind",
                sa.String(16),
                nullable=False,
                server_default="topic",
            ),
            sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "is_stub",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "status",
                sa.String(32),
                nullable=False,
                server_default="draft",
            ),
            sa.Column("last_compiled_at", sa.DateTime(timezone=True), nullable=True),
        ),
        sa.UniqueConstraint("project_id", "slug", name="uq_wiki_pages_project_slug"),
    )
    op.create_index(
        "ix_wiki_pages_project_title", "wiki_pages", ["project_id", "title"]
    )

    op.create_table(
        "wiki_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("summary", sa.String(2048), nullable=False, server_default=""),
        sa.Column("author_kind", sa.String(16), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column(
            "commit_message", sa.String(2048), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("ledger_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("page_id", "revision_no", name="uq_wiki_rev_page_no"),
    )
    op.create_index("ix_wiki_revisions_created_at", "wiki_revisions", ["created_at"])
    # Immutability — same pattern as action_ledger_events.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wiki_revisions_immutable() RETURNS TRIGGER AS $$
        BEGIN
          RAISE EXCEPTION 'wiki_revisions is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_revisions_no_update BEFORE UPDATE ON wiki_revisions
          FOR EACH ROW EXECUTE FUNCTION wiki_revisions_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_revisions_no_delete BEFORE DELETE ON wiki_revisions
          FOR EACH ROW EXECUTE FUNCTION wiki_revisions_immutable();
        """
    )

    op.create_table(
        "wiki_sections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("anchor", sa.String(512), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "revision_id", "anchor", name="uq_wiki_sections_rev_anchor"
        ),
    )
    op.create_index(
        "ix_sections_page_rev", "wiki_sections", ["page_id", "revision_id"]
    )

    op.create_table(
        "wiki_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("src_page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("src_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_page_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("dst_title", sa.String(512), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_links_src_dst", "wiki_links", ["src_page_id", "dst_title"])

    op.create_table(
        "wiki_claims",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("section_anchor", sa.String(512), nullable=True),
            sa.Column("text", sa.String(2048), nullable=False),
            sa.Column(
                "confidence",
                sa.String(16),
                nullable=False,
                server_default="cited",
            ),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="active"
            ),
        ),
    )

    op.create_table(
        "citations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "chunk_ids", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("source_page_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("citation_marker", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "source_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "source_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True
        ),
        sa.Column(
            "page_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True
        ),
        sa.Column(
            "extracted_claims_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "aliases",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("surface_form", sa.String(512), nullable=False),
            sa.Column("canonical_name", sa.String(512), nullable=False),
            sa.Column("canonical_page_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "confidence", sa.Float(), nullable=False, server_default="1.0"
            ),
        ),
        sa.UniqueConstraint(
            "project_id", "surface_form", name="uq_aliases_project_surface"
        ),
    )
    op.create_index(
        "ix_aliases_project_canonical", "aliases", ["project_id", "canonical_name"]
    )

    op.create_table(
        "hand_edit_marks",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("section_anchor", sa.String(512), nullable=True),
            sa.Column("body_sha256_at_edit", sa.String(64), nullable=False),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("applied_by", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cleared_by", postgresql.UUID(as_uuid=True), nullable=True),
        ),
    )
    op.execute(
        "CREATE INDEX ix_handedits_active ON hand_edit_marks (page_id) "
        "WHERE cleared_at IS NULL"
    )

    op.create_table(
        "rejection_feedbacks",
        *_common(
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
            sa.Column(
                "concept_name", sa.String(512), nullable=False, index=True
            ),
            sa.Column("rejected_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("reason", sa.String(4096), nullable=False),
            sa.Column("rejected_by", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "addressed_in_revision_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        ),
    )

    # ---- wiki_index (FTS) --------------------------------------------------
    op.create_table(
        "wiki_index",
        sa.Column("page_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("slug", sa.String(512), nullable=False),
        sa.Column(
            "aliases_jsonb", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "summary", sa.String(2048), nullable=False, server_default=""
        ),
        sa.Column(
            "wikilinks_out_jsonb",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("page_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("is_stub", sa.Boolean(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("index_tsv", postgresql.TSVECTOR(), nullable=False),
    )
    op.create_index(
        "ix_wiki_index_tsv", "wiki_index", ["index_tsv"], postgresql_using="gin"
    )
    op.create_index(
        "ix_wiki_index_project_title", "wiki_index", ["project_id", "title"]
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wiki_index_tsv_update() RETURNS TRIGGER AS $$
        BEGIN
          NEW.index_tsv := to_tsvector('english',
              coalesce(NEW.title, '') || ' ' ||
              coalesce(NEW.summary, '') || ' ' ||
              coalesce((
                SELECT string_agg(elem, ' ')
                FROM jsonb_array_elements_text(NEW.aliases_jsonb) AS t(elem)
              ), '')
          );
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_index_tsv_iu
        BEFORE INSERT OR UPDATE ON wiki_index
        FOR EACH ROW EXECUTE FUNCTION wiki_index_tsv_update();
        """
    )

    # ---- seed the Upload connector ----------------------------------------
    upload_id = str(uuid4())
    upload_metadata_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "filename": {"type": "string", "maxLength": 1024},
            "mime_type": {"type": "string", "maxLength": 128},
            "uploader_id": {"type": "string", "format": "uuid"},
            "original_size": {"type": "integer", "minimum": 0},
        },
        "required": ["filename", "mime_type", "uploader_id", "original_size"],
    }
    op.execute(
        sa.text(
            """
            INSERT INTO connectors (
              id, created_by, access_scope, kind, name, output_kind,
              requires_auth, metadata_schema_jsonb, enabled_by_default
            ) VALUES (
              :id, :sys, 'global', 'upload', 'Upload', 'document',
              false, :schema, true
            )
            """
        ).bindparams(
            sa.bindparam("id", upload_id),
            sa.bindparam("sys", str(uuid4())),
            sa.bindparam("schema", json.dumps(upload_metadata_schema)),
        )
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS wiki_index_tsv_iu ON wiki_index")
    op.execute("DROP FUNCTION IF EXISTS wiki_index_tsv_update")
    op.execute("DROP TRIGGER IF EXISTS document_chunks_tsv_iu ON document_chunks")
    op.execute("DROP FUNCTION IF EXISTS document_chunks_tsvector_update")
    op.execute("DROP TRIGGER IF EXISTS wiki_revisions_no_update ON wiki_revisions")
    op.execute("DROP TRIGGER IF EXISTS wiki_revisions_no_delete ON wiki_revisions")
    op.execute("DROP FUNCTION IF EXISTS wiki_revisions_immutable")

    op.drop_index("ix_wiki_index_project_title", "wiki_index")
    op.drop_index("ix_wiki_index_tsv", "wiki_index")
    op.drop_table("wiki_index")
    op.drop_table("rejection_feedbacks")
    op.execute("DROP INDEX IF EXISTS ix_handedits_active")
    op.drop_table("hand_edit_marks")
    op.drop_index("ix_aliases_project_canonical", "aliases")
    op.drop_table("aliases")
    op.drop_table("source_pages")
    op.drop_table("citations")
    op.drop_table("wiki_claims")
    op.drop_index("ix_links_src_dst", "wiki_links")
    op.drop_table("wiki_links")
    op.drop_index("ix_sections_page_rev", "wiki_sections")
    op.drop_table("wiki_sections")
    op.drop_index("ix_wiki_revisions_created_at", "wiki_revisions")
    op.drop_table("wiki_revisions")
    op.drop_index("ix_wiki_pages_project_title", "wiki_pages")
    op.drop_table("wiki_pages")
    op.drop_table("retrieval_index_records")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_fts")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.drop_index("ix_chunks_source_ord", "document_chunks")
    op.drop_table("document_chunks")
    op.drop_table("normalized_documents")
    op.drop_table("source_assets")
    op.drop_table("source_versions")
    op.drop_index("ix_sources_short_id", "sources")
    op.drop_index("ix_sources_project_status", "sources")
    op.drop_table("sources")
    op.drop_table("connector_bindings")
    op.drop_table("connectors")
