"""claims become durable beliefs

The Claim Spine. See docs/belief-engine.md.

Claims were owned by a page revision: `wiki_claims.revision_id` was NOT NULL and
`commit_revision` inserted a fresh row on every commit. So every page rewrite
threw away the claim's identity along with its citations, its verdicts and any
human correction — the knowledge layer could not accumulate, which is the one
thing a knowledge layer is for.

Three changes:

1. `wiki_claims` gains a stable identity — `claim_key = sha256(normalize(text))`
   unique per project among live rows — plus supersession, provenance and
   derived counts. `revision_id` becomes nullable and degrades to first-seen
   provenance; the column keeps its name so six call sites stay untouched.

2. `citations` gains a real anchor. `source_id` is the join key retraction
   needs and was `None` on every production write path, which silently voided
   retraction blast-radius, two freshness dimensions, the reviewer's source
   registry and the citation popover. `stance` and `weight` are named to match
   `aleph_hypotheses.confidence.EvidenceRow` exactly, so the existing pure,
   tested confidence engine consumes citations with zero adaptation.

3. `claim_edges` — the only new table. Typed, weighted, revision-free relations
   between claims. This is what `WikiLink(src_page_id, dst_title)` should have
   been, and it is what turns retraction from a citation lookup into a graph walk.

Nothing is dropped here. The wiki tables stay until the replacement wins on the
retrieval eval — see docs/acceptance.md part E.

Revision ID: claim_spine
Revises: wiki_index_body
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "claim_spine"
down_revision = "wiki_index_body"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    # ---- 1. claims become durable -----------------------------------------
    # `confidence` was varchar(16), which cannot hold "under_investigation" (19)
    # — the state a claim with no evidence is in, and therefore the state every
    # claim starts in. It fit before only because the column held whatever
    # adjective a model produced rather than a value from the state machine.
    op.alter_column(
        "wiki_claims",
        "confidence",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.alter_column("wiki_claims", "revision_id", existing_type=postgresql.UUID(), nullable=True)
    op.add_column("wiki_claims", sa.Column("claim_key", sa.String(64), nullable=True))
    op.add_column("wiki_claims", sa.Column("superseded_by", postgresql.UUID(), nullable=True))
    op.add_column(
        "wiki_claims",
        sa.Column("origin", sa.String(16), nullable=False, server_default="agent"),
    )
    op.add_column(
        "wiki_claims",
        sa.Column("evidence_tier", sa.String(16), nullable=False, server_default="inferred"),
    )
    op.add_column(
        "wiki_claims", sa.Column("rationale", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("wiki_claims", sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column(
        "wiki_claims",
        sa.Column("support_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wiki_claims",
        sa.Column("distinct_source_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "wiki_claims", sa.Column("last_surfaced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_claims_superseded_by", "wiki_claims", "wiki_claims", ["superseded_by"], ["id"]
    )

    # Backfill identity for existing rows so the unique index can be created.
    # sha256 of the lowercased, whitespace-collapsed text — the same shape the
    # service computes, expressed in SQL so the backfill needs no Python pass.
    op.execute(
        """
        UPDATE wiki_claims
        SET claim_key = encode(
            sha256(convert_to(lower(regexp_replace(text, '\\s+', ' ', 'g')), 'UTF8')),
            'hex'
        )
        WHERE claim_key IS NULL
        """
    )

    # Older duplicates are superseded by the newest row sharing a key, so the
    # partial unique index below can be created without data loss.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, project_id, claim_key,
                   row_number() OVER (
                       PARTITION BY project_id, claim_key ORDER BY created_at DESC, id DESC
                   ) AS rn,
                   first_value(id) OVER (
                       PARTITION BY project_id, claim_key ORDER BY created_at DESC, id DESC
                   ) AS winner
            FROM wiki_claims
        )
        UPDATE wiki_claims AS c
        SET superseded_by = ranked.winner
        FROM ranked
        WHERE c.id = ranked.id AND ranked.rn > 1
        """
    )

    op.create_index(
        "uq_claims_project_key",
        "wiki_claims",
        ["project_id", "claim_key"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_index(
        "ix_claims_live",
        "wiki_claims",
        ["project_id"],
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_index(
        "ix_claims_embedding_hnsw",
        "wiki_claims",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    # Expression index; no extra tsvector column to keep in sync.
    op.execute(
        "CREATE INDEX ix_claims_text_fts ON wiki_claims USING gin (to_tsvector('english', text))"
    )

    # ---- 2. evidence gets a real anchor ------------------------------------
    op.add_column("citations", sa.Column("source_id", postgresql.UUID(), nullable=True))
    op.add_column("citations", sa.Column("chunk_id", postgresql.UUID(), nullable=True))
    op.add_column("citations", sa.Column("quote", sa.Text(), nullable=True))
    op.add_column(
        "citations",
        sa.Column("verbatim", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "citations",
        sa.Column("stance", sa.String(16), nullable=False, server_default="supports"),
    )
    op.add_column(
        "citations", sa.Column("weight", sa.Float(), nullable=False, server_default="1.0")
    )
    op.add_column("citations", sa.Column("locator_hash", sa.String(64), nullable=True))
    op.add_column("citations", sa.Column("char_start", sa.Integer(), nullable=True))
    op.add_column("citations", sa.Column("char_end", sa.Integer(), nullable=True))
    op.create_index("ix_citations_source", "citations", ["source_id"])
    # Union-not-clobber: re-deriving the same span twice updates one row instead
    # of duplicating it, so merging two extractions never inflates the evidence.
    op.create_index(
        "uq_citations_claim_locator",
        "citations",
        ["claim_id", "locator_hash"],
        unique=True,
        postgresql_where=sa.text("locator_hash IS NOT NULL"),
    )

    # ---- 3. the belief graph ------------------------------------------------
    op.create_table(
        "claim_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("src_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        # supports | contradicts | derived_from | specializes | supersedes
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("access_scope", sa.String(16), nullable=False, server_default="project"),
        sa.ForeignKeyConstraint(["src_claim_id"], ["wiki_claims.id"]),
        sa.ForeignKeyConstraint(["dst_claim_id"], ["wiki_claims.id"]),
        sa.CheckConstraint("src_claim_id <> dst_claim_id", name="ck_claim_edges_no_self_loop"),
    )
    op.create_index("ix_claim_edges_src", "claim_edges", ["src_claim_id", "kind"])
    op.create_index("ix_claim_edges_dst", "claim_edges", ["dst_claim_id", "kind"])
    op.create_index(
        "uq_claim_edges_triple",
        "claim_edges",
        ["src_claim_id", "dst_claim_id", "kind"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("claim_edges")

    op.drop_index("uq_citations_claim_locator", "citations")
    op.drop_index("ix_citations_source", "citations")
    for column in (
        "char_end",
        "char_start",
        "locator_hash",
        "weight",
        "stance",
        "verbatim",
        "quote",
        "chunk_id",
        "source_id",
    ):
        op.drop_column("citations", column)

    op.execute("DROP INDEX IF EXISTS ix_claims_text_fts")
    op.drop_index("ix_claims_embedding_hnsw", "wiki_claims")
    op.drop_index("ix_claims_live", "wiki_claims")
    op.drop_index("uq_claims_project_key", "wiki_claims")
    op.drop_constraint("fk_claims_superseded_by", "wiki_claims", type_="foreignkey")
    for column in (
        "last_surfaced_at",
        "distinct_source_count",
        "support_count",
        "embedding",
        "rationale",
        "evidence_tier",
        "origin",
        "superseded_by",
        "claim_key",
    ):
        op.drop_column("wiki_claims", column)
    # Rows superseded by the backfill cannot be un-superseded meaningfully, so
    # NOT NULL is restored only if every remaining row has a revision.
    op.execute("DELETE FROM wiki_claims WHERE revision_id IS NULL")
    op.alter_column("wiki_claims", "revision_id", existing_type=postgresql.UUID(), nullable=False)
    op.execute("UPDATE wiki_claims SET confidence = left(confidence, 16)")
    op.alter_column(
        "wiki_claims",
        "confidence",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
