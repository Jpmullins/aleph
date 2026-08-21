"""chunks before embeddings: a dead embedder degrades instead of blacking out

Three changes, one defect.

The defect: `document_chunks` held zero rows against 75 ingested sources,
because the indexing job wrote chunk rows only *after* the embed call returned,
and the shipped embedding binding named `titan-embed-v2` on a gateway that
serves `titan-embed-text-v2`. One wrong word took down keyword search too — a
capability with no dependency on any model at all.

1. `document_chunks.embedding` and `.embedder_model` become nullable, so a chunk
   can exist before it has a vector. pgvector does not index NULL rows, which is
   exactly right: an un-embedded chunk is absent from the dense leg and present
   in the lexical one.
2. `retrieval_index_records` gains `state` and `degraded_reason`, and its
   `embedder_model` becomes nullable. `lexical_only` is a real, usable, degraded
   index; previously the same situation was represented by the absence of any
   row, which is indistinguishable from "never ingested".
3. The seeded model-profile templates stop naming an embedding model, and every
   project profile still pointing at the unserved `titan-embed-v2` has that
   binding removed. Aleph ships no model list: the binding is chosen by
   `POST /v1/projects/{id}/model-profile/autoconfigure` from what the configured
   gateway reports, and probed before it is bound. Project creation now enqueues
   that automatically.

Revision ID: rs1_chunks_first
Revises: b68c4f52342d
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "rs1_chunks_first"
down_revision = "b68c4f52342d"
branch_labels = None
depends_on = None

#: The name that was shipped and that no configured gateway serves. Removing the
#: binding (rather than rewriting it to a name that happens to work here) is the
#: point: a correct name for this deployment is still a guess about the next one.
UNSERVED_EMBED_MODEL = "titan-embed-v2"


def upgrade() -> None:
    op.alter_column("document_chunks", "embedding", existing_type=Vector(1024), nullable=True)
    op.alter_column(
        "document_chunks",
        "embedder_model",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.alter_column(
        "retrieval_index_records",
        "embedder_model",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.add_column(
        "retrieval_index_records",
        sa.Column("state", sa.String(length=32), nullable=False, server_default="embedded"),
    )
    op.add_column(
        "retrieval_index_records",
        sa.Column("degraded_reason", sa.Text(), nullable=True),
    )

    # Existing rows: an index record whose chunks carry no vectors is
    # `lexical_only`, whatever it claimed before.
    op.execute(
        """
        UPDATE retrieval_index_records r
           SET state = 'lexical_only',
               degraded_reason = 'no chunk of this source carries an embedding'
         WHERE NOT EXISTS (
                 SELECT 1 FROM document_chunks c
                  WHERE c.source_id = r.source_id AND c.embedding IS NOT NULL)
        """
    )

    # Stop shipping a model name.
    op.execute(
        f"""
        UPDATE model_profiles
           SET bindings_jsonb = bindings_jsonb - 'embedding'
         WHERE is_template IS TRUE
            OR bindings_jsonb->'embedding'->>'model' = '{UNSERVED_EMBED_MODEL}'
        """
    )


def downgrade() -> None:
    # The binding cannot be restored: its value was a name that does not resolve
    # anywhere, and re-inserting it would recreate the outage. Downgrade returns
    # the schema, not the defect.
    op.drop_column("retrieval_index_records", "degraded_reason")
    op.drop_column("retrieval_index_records", "state")
    # Rows that were never embedded cannot satisfy a NOT NULL constraint, so the
    # downgrade removes them rather than failing halfway: they are reproducible
    # by re-running the index job, and a half-applied downgrade is not.
    op.execute("DELETE FROM document_chunks WHERE embedding IS NULL OR embedder_model IS NULL")
    op.execute(
        "UPDATE retrieval_index_records SET embedder_model = '' WHERE embedder_model IS NULL"
    )
    op.alter_column(
        "retrieval_index_records",
        "embedder_model",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.alter_column(
        "document_chunks",
        "embedder_model",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.alter_column("document_chunks", "embedding", existing_type=Vector(1024), nullable=False)
