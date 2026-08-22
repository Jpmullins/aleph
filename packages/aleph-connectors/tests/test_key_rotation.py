"""Rehearse the incident: rotate a secret without losing the credentials.

Two rotations, and they are different problems.

1. **Rotate the agent-token signing secret** — the ordinary response to a
   leaked signing key. Before WS-P7 it also destroyed every stored connector
   credential, permanently, with no warning. It must now be a non-event for
   credentials written after the split.

2. **Rotate the credential master key itself** — a real re-encryption, run
   through the exact entry point `docs/operations.md` names
   (`python -m aleph_connectors.reencrypt`, i.e. `reencrypt_credentials`), so
   the documented procedure is executed rather than described.

Marked `integration`: the write path, the version column and the ledger event
are the behaviour under test, and a mocked session would report all three as
working while the real thing failed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aleph_connectors.credentials import (
    CURRENT_KEY_VERSION,
    LEGACY_KEY_VERSION,
    ConnectorCredential,
    ConnectorCredentialService,
    LibsodiumSealedBoxCipher,
    credential_cipher,
)
from aleph_connectors.keys import legacy_v1_master_secret
from aleph_connectors.reencrypt import reencrypt_credentials
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.agent_token import mint_agent_token, verify_agent_token
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"
)

OLD_TOKEN_SECRET = "old-signing-secret-" + "o" * 45
NEW_TOKEN_SECRET = "new-signing-secret-" + "n" * 45
MASTER = "master-key-" + "m" * 53
NEW_MASTER = "rotated-master-key-" + "r" * 45

PLAINTEXT = "sk-live-connector-credential"


def _principal() -> Principal:
    return Principal(
        user_id=UUID(int=0),
        subject="rotation-test",
        email="rotation@aleph.local",
        actor_kind="system",
    )


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def project_id(
    maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[UUID]:
    pid = uuid4()
    yield pid
    # Only this project's credential rows. `action_ledger_events` is
    # append-only and deliberately left alone — a fixture that switches off a
    # core invariant to tidy up is how the invariant stops being one.
    async with maker() as session:
        await session.execute(
            text("DELETE FROM connector_credentials WHERE project_id = :pid"), {"pid": pid}
        )
        await session.commit()


async def _store(
    maker: async_sessionmaker[AsyncSession],
    project_id: UUID,
    connector_id: UUID,
    *,
    master_key: str,
    legacy_key: str,
    plaintext: str = PLAINTEXT,
) -> None:
    async with maker() as session:
        svc = ConnectorCredentialService(
            session, cipher=credential_cipher(master_key=master_key, legacy_key=legacy_key)
        )
        await svc.upsert(
            ledger=LedgerWriter(session),
            principal=_principal(),
            project_id=project_id,
            connector_id=connector_id,
            connector_kind="tavily",
            plaintext=plaintext,
        )
        await session.commit()


async def _row(maker: async_sessionmaker[AsyncSession], project_id: UUID) -> ConnectorCredential:
    async with maker() as session:
        row = (
            await session.execute(
                select(ConnectorCredential).where(ConnectorCredential.project_id == project_id)
            )
        ).scalar_one()
        return row


async def test_rotating_the_signing_secret_does_not_destroy_credentials(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """Criterion 3. The whole workstream in one test.

    A credential is stored, the agent-token signing secret is replaced with a
    completely different value, and the credential still opens — while a token
    minted under the new secret verifies. Before the split, the first assertion
    failed permanently and the operator had no way to notice until a connector
    stopped working.
    """
    connector_id = uuid4()
    await _store(maker, project_id, connector_id, master_key=MASTER, legacy_key=OLD_TOKEN_SECRET)

    # The rotation: the signing secret changes, the credential master key does not.
    async with maker() as session:
        svc = ConnectorCredentialService(
            session,
            cipher=credential_cipher(master_key=MASTER, legacy_key=NEW_TOKEN_SECRET),
        )
        got = await svc.decrypt_for_callback(
            project_id=project_id, connector_id=connector_id, connector_kind="tavily"
        )
    assert got == PLAINTEXT

    token = mint_agent_token(
        secret=NEW_TOKEN_SECRET,
        user_id=uuid4(),
        project_id=project_id,
        agent_run_id=uuid4(),
        actor_kind="aleph_agent",
        correlation_id="rotation-test",
    )
    assert verify_agent_token(token, secret=NEW_TOKEN_SECRET).project_id == project_id


async def test_a_new_credential_is_written_at_the_current_key_version(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """The column is what makes two generations readable at once, so it has to
    be populated on the real write path — not only in fixtures."""
    await _store(maker, project_id, uuid4(), master_key=MASTER, legacy_key=OLD_TOKEN_SECRET)
    assert (await _row(maker, project_id)).key_version == CURRENT_KEY_VERSION


async def test_a_pre_split_row_is_readable_and_then_re_encrypted(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """The migration path, end to end, through the documented entry point.

    Writes a row exactly as the pre-split code did — keyed by the padded
    agent-token secret and stamped `v1` — then runs `reencrypt_credentials`,
    which is what `python -m aleph_connectors.reencrypt` calls.
    """
    connector_id = uuid4()
    v1 = LibsodiumSealedBoxCipher(
        master_secret=legacy_v1_master_secret(OLD_TOKEN_SECRET),
        key_version=LEGACY_KEY_VERSION,
    )
    async with maker() as session:
        session.add(
            ConnectorCredential(
                id=uuid4(),
                project_id=project_id,
                connector_id=connector_id,
                cipher_blob=v1.encrypt(project_id=project_id, plaintext=PLAINTEXT),
                cipher_scheme=v1.scheme,
                key_version=LEGACY_KEY_VERSION,
                kms_key_arn=None,
                rotated_at=None,
                created_by=UUID(int=0),
            )
        )
        await session.commit()

    # Step 2 of the procedure: a dry run reports what would move and proves
    # every row can actually be opened, BEFORE the old key goes away.
    dry = await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=MASTER,
        legacy_key=OLD_TOKEN_SECRET,
        dry_run=True,
        project_id=project_id,
    )
    assert dry.ok and dry.examined == 1 and dry.reencrypted == 0
    assert (await _row(maker, project_id)).key_version == LEGACY_KEY_VERSION, (
        "a dry run must not write"
    )

    # Step 3.
    done = await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=MASTER,
        legacy_key=OLD_TOKEN_SECRET,
        project_id=project_id,
    )
    assert done.ok and done.reencrypted == 1

    row = await _row(maker, project_id)
    assert row.key_version == CURRENT_KEY_VERSION

    # Step 4: the old key is now removable, and the credential still opens
    # without it.
    async with maker() as session:
        svc = ConnectorCredentialService(session, cipher=credential_cipher(master_key=MASTER))
        assert (
            await svc.decrypt_for_callback(
                project_id=project_id, connector_id=connector_id, connector_kind="tavily"
            )
            == PLAINTEXT
        )

    # And a second pass has nothing left to do — which is exactly the signal
    # that dropping ALEPH_CREDENTIAL_LEGACY_KEY is safe.
    again = await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=MASTER,
        legacy_key=OLD_TOKEN_SECRET,
        dry_run=True,
        project_id=project_id,
    )
    assert again.examined == 0


async def test_re_encryption_ledgers_every_row_it_moves(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """Rotation is a state mutation, so it writes an `ActionLedgerEvent` in the
    same transaction — versions only, never key material and never plaintext.

    Without the event, a key rotation is the one class of change that leaves no
    trace in a ledger built specifically so history is evidence.
    """
    connector_id = uuid4()
    v1 = LibsodiumSealedBoxCipher(
        master_secret=legacy_v1_master_secret(OLD_TOKEN_SECRET),
        key_version=LEGACY_KEY_VERSION,
    )
    async with maker() as session:
        session.add(
            ConnectorCredential(
                id=uuid4(),
                project_id=project_id,
                connector_id=connector_id,
                cipher_blob=v1.encrypt(project_id=project_id, plaintext=PLAINTEXT),
                cipher_scheme=v1.scheme,
                key_version=LEGACY_KEY_VERSION,
                kms_key_arn=None,
                rotated_at=None,
                created_by=UUID(int=0),
            )
        )
        await session.commit()

    report = await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=MASTER,
        legacy_key=OLD_TOKEN_SECRET,
        project_id=project_id,
    )
    assert report.ok and report.reencrypted == 1

    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT payload_jsonb FROM action_ledger_events "
                    "WHERE project_id = :pid AND action_kind = "
                    "'connector_credential.reencrypt'"
                ),
                {"pid": project_id},
            )
        ).all()
    assert len(rows) == 1, "every re-encrypted row writes exactly one ledger event"
    (payload,) = rows[0]
    assert payload["from_key_version"] == LEGACY_KEY_VERSION
    assert payload["to_key_version"] == CURRENT_KEY_VERSION
    # The payload of a credential event carries neither the plaintext nor any
    # key material — the two things that would turn an audit log into the leak.
    rendered = str(payload)
    assert PLAINTEXT not in rendered
    assert MASTER not in rendered
    assert OLD_TOKEN_SECRET not in rendered


async def test_a_dry_run_writes_no_ledger_event(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """A dry run reports; it does not act. If it ledgered, the audit trail would
    claim a rotation that never happened."""
    await _store(maker, project_id, uuid4(), master_key=MASTER, legacy_key=OLD_TOKEN_SECRET)
    await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=NEW_MASTER,
        legacy_key=MASTER,
        dry_run=True,
        project_id=project_id,
    )
    async with maker() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM action_ledger_events WHERE project_id = :pid "
                    "AND action_kind = 'connector_credential.reencrypt'"
                ),
                {"pid": project_id},
            )
        ).scalar_one()
    assert count == 0


async def test_a_row_whose_key_is_gone_is_reported_not_silently_skipped(
    maker: async_sessionmaker[AsyncSession], project_id: UUID
) -> None:
    """The failure an operator must see before removing the old key. A pass that
    counted this row as "done" would let them delete the only thing that could
    have opened it."""
    connector_id = uuid4()
    lost = LibsodiumSealedBoxCipher(
        master_secret=legacy_v1_master_secret("a-secret-nobody-has-any-more" + "z" * 40),
        key_version=LEGACY_KEY_VERSION,
    )
    async with maker() as session:
        session.add(
            ConnectorCredential(
                id=uuid4(),
                project_id=project_id,
                connector_id=connector_id,
                cipher_blob=lost.encrypt(project_id=project_id, plaintext=PLAINTEXT),
                cipher_scheme=lost.scheme,
                key_version=LEGACY_KEY_VERSION,
                kms_key_arn=None,
                rotated_at=None,
                created_by=UUID(int=0),
            )
        )
        await session.commit()

    report = await reencrypt_credentials(
        database_url=DATABASE_URL,
        master_key=MASTER,
        legacy_key=OLD_TOKEN_SECRET,
        project_id=project_id,
    )
    assert not report.ok
    assert len(report.failures) == 1
    assert (await _row(maker, project_id)).key_version == LEGACY_KEY_VERSION
