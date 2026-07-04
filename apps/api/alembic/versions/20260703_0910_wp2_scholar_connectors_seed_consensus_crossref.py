"""wp2 scholar: seed `consensus` and `crossref` connectors.

Data-only migration (no schema change). Inserts the two connector rows
WP-2 (`aleph-scholar`) keys credentials and provenance on:

- ``consensus`` — Consensus over MCP (OAuth credential required).
- ``crossref``  — Crossref REST API (no auth; provenance naming).

Idempotent: ``INSERT ... ON CONFLICT (kind) DO NOTHING``.

Revision ID: wp2_scholar_connectors
Revises: a8b39f52d713
Create Date: 2026-07-03
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "wp2_scholar_connectors"
down_revision: str | None = "a8b39f52d713"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WP2_CONNECTORS = [
    # (kind, name, output_kind, requires_auth, enabled_by_default)
    ("consensus", "Consensus (MCP)", "document", True, True),
    ("crossref", "Crossref", "document", False, True),
]


def upgrade() -> None:
    system_user_id = str(uuid4())
    for kind, name, output_kind, requires_auth, enabled_by_default in _WP2_CONNECTORS:
        op.execute(
            sa.text(
                """
                INSERT INTO connectors (
                  id, created_by, access_scope, kind, name, output_kind,
                  requires_auth, metadata_schema_jsonb, enabled_by_default
                ) VALUES (
                  CAST(:id AS uuid), CAST(:sys AS uuid), 'global', :kind, :name, :output_kind,
                  :req_auth, CAST(:schema AS jsonb), :enabled
                )
                ON CONFLICT (kind) DO NOTHING
                """
            ).bindparams(
                sa.bindparam("id", str(uuid4())),
                sa.bindparam("sys", system_user_id),
                sa.bindparam("kind", kind),
                sa.bindparam("name", name),
                sa.bindparam("output_kind", output_kind),
                sa.bindparam("req_auth", requires_auth),
                sa.bindparam(
                    "schema", json.dumps({"$schema": "http://json-schema.org/draft-07/schema#"})
                ),
                sa.bindparam("enabled", enabled_by_default),
            )
        )


def downgrade() -> None:
    for kind, *_ in _WP2_CONNECTORS:
        op.execute(
            sa.text("DELETE FROM connectors WHERE kind = :k").bindparams(sa.bindparam("k", kind))
        )
