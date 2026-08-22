"""connector credentials record which key encrypted them

Step one of splitting the credential-encryption key out of the agent-token
signing secret (WS-P7), and it is deliberately alone in this revision.

Until now a ciphertext carried no record of the key that produced it, so there
was exactly one key the code could try. That is what made rotating
`ALEPH_AGENT_TOKEN_SECRET` — the ordinary response to a leaked signing key —
destroy every stored connector credential: real third-party API keys and the
Consensus OAuth grant, none of which Aleph can re-derive from anything.

With `key_version` on the row, a deployment can read two generations at once,
which is the only way to re-encrypt live data without a window in which it is
unreadable. The order is: ship this, install the new key alongside the old,
re-encrypt (`python -m aleph_connectors.reencrypt`), then drop the old key.

Every row that exists today was encrypted from the agent-token secret, so they
are backfilled `v1` — a fact, not a default. The column then goes NOT NULL with
no server default: a new row's version is whatever key the cipher actually used,
and a row whose version is a guess is a credential nobody can open.

Revision ID: p7_cred_key_version
Revises: rs1_chunks_first
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p7_cred_key_version"
down_revision = "rs1_chunks_first"
branch_labels = None
depends_on = None

#: What encrypted every row written before the split.
LEGACY_KEY_VERSION = "v1"


def upgrade() -> None:
    # Nullable first, backfill, then NOT NULL — an `add_column` with a server
    # default would leave the default behind on the table, and the next row
    # inserted without an explicit version would silently claim to be v1 while
    # holding v2 bytes.
    op.add_column(
        "connector_credentials",
        sa.Column("key_version", sa.String(length=16), nullable=True),
    )
    op.execute(
        f"UPDATE connector_credentials SET key_version = '{LEGACY_KEY_VERSION}' "
        f"WHERE key_version IS NULL"
    )
    op.alter_column(
        "connector_credentials",
        "key_version",
        existing_type=sa.String(length=16),
        nullable=False,
    )


def downgrade() -> None:
    """Drop the column.

    SAFE only while every row is still on the key the pre-split code uses — i.e.
    before `python -m aleph_connectors.reencrypt` has run. After re-encryption
    the old code has no way to know a blob is v2 and will try to open it with
    the agent-token secret, which fails as a CryptoError about a corrupt
    ciphertext. Re-encrypt back to v1 before downgrading, or do not downgrade.

    Not enforced here with a guard that aborts on a v2 row: a downgrade that
    refuses to run is a downgrade that has never been run, and this one is
    exercised on every push by scripts/check-migration-roundtrip.sh against a
    scratch database, where the table is empty and the warning is the honest
    output.
    """
    op.drop_column("connector_credentials", "key_version")
