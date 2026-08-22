"""ConnectorCredential — encrypted per-project credentials.

Two cipher schemes:
  * `libsodium-sealed` — local/dev. Sealed-box with a per-project keypair
    derived from a deployment-level master secret + project_id. The
    private key never leaves memory; ciphertexts can be opened only by
    code holding the master + project context.
  * `kms-aes-gcm` — production. DEK wrapped by a cloud KMS key per
    project, ciphertext as AES-GCM. Implementation hook is provided;
    actual KMS calls are operator-configured in production deployment.

A credential is **never returned by any HTTP endpoint**. Only
`decrypt_for_callback` decrypts — called in-process by the research
worker after resolving the project's connector binding.

**Every row records which key generation encrypted it** (`key_version`).
That column is the whole reason a master key can be rotated at all: the cipher
writes at one version and reads at several, so the sequence is install the new
key → re-encrypt → drop the old key, with the credentials readable at every
step. Before WS-P7 there was no version, one secret served three unrelated
purposes, and rotating it was indistinguishable from deleting every stored
third-party API key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from nacl import secret
from sqlalchemy import (
    DateTime,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aleph_connectors.keys import (
    CURRENT_KEY_VERSION,
    LEGACY_KEY_VERSION,
    derive_project_key,
    legacy_v1_master_secret,
    master_key_bytes,
    master_key_from_env,
)
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.base import Base, CommonColumns

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


class ConnectorCredential(CommonColumns, Base):
    __tablename__ = "connector_credentials"
    __table_args__ = (
        UniqueConstraint("project_id", "connector_id", name="uq_cred_project_connector"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    connector_id: Mapped[UUID] = mapped_column(nullable=False)
    cipher_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cipher_scheme: Mapped[str] = mapped_column(String(32), nullable=False)
    kms_key_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Which master-key generation encrypted `cipher_blob`. NOT NULL and with no
    #: default on purpose: there is no sensible default for a new row — it is
    #: whatever key the cipher actually used — and a row whose version is a guess
    #: is a credential nobody can open. The migration backfills every pre-split
    #: row with `v1` because that is factually what encrypted them.
    key_version: Mapped[str] = mapped_column(String(16), nullable=False)


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------


class CredentialCipher(Protocol):
    scheme: str

    #: The version `encrypt` stamps on what it writes. A cipher may READ several
    #: versions; it writes exactly one.
    key_version: str

    def encrypt(self, *, project_id: UUID, plaintext: str) -> bytes: ...

    def decrypt(self, *, project_id: UUID, cipher_blob: bytes, key_version: str) -> str: ...


@dataclass
class LibsodiumSealedBoxCipher:
    """Sealed-box per-project, keyed by `derive_project_key`.

    Writes at `key_version` and reads at any version in `read_secrets` as well.
    Two generations readable at once is not a nicety — it is the only way to
    re-encrypt a live deployment without a window in which the credentials are
    unreadable, which is the same thing as having lost them.
    """

    master_secret: bytes = b""
    key_version: str = CURRENT_KEY_VERSION
    #: version -> master secret, for versions this deployment can still READ.
    #: Populated by `credential_cipher` from the legacy key; empty is a
    #: perfectly good state once nothing is left at the old version.
    read_secrets: Mapping[str, bytes] = field(default_factory=dict)
    scheme: str = "libsodium-sealed"

    def __post_init__(self) -> None:
        if not self.master_secret or len(self.master_secret) < 32:
            msg = "master_secret must be >= 32 bytes"
            raise ValidationFailed(msg)

    def _secret_for(self, key_version: str) -> bytes:
        if key_version == self.key_version:
            return self.master_secret
        found = self.read_secrets.get(key_version, b"")
        if not found:
            # Naming the version and the setting matters: the alternative is a
            # libsodium CryptoError about a corrupt ciphertext, which sends the
            # operator looking for data corruption instead of a missing key.
            msg = (
                f"no master key configured for cipher key_version {key_version!r}; "
                f"this deployment writes {self.key_version!r} and can read "
                f"{sorted({self.key_version, *self.read_secrets})}. "
                f"Set ALEPH_CREDENTIAL_LEGACY_KEY to the pre-split "
                f"ALEPH_AGENT_TOKEN_SECRET to open v1 rows."
            )
            raise ValidationFailed(msg)
        return found

    def encrypt(self, *, project_id: UUID, plaintext: str) -> bytes:
        box = secret.SecretBox(derive_project_key(self.master_secret, project_id))
        return bytes(box.encrypt(plaintext.encode("utf-8")))

    def decrypt(self, *, project_id: UUID, cipher_blob: bytes, key_version: str) -> str:
        box = secret.SecretBox(derive_project_key(self._secret_for(key_version), project_id))
        return box.decrypt(cipher_blob).decode("utf-8")


def credential_cipher(*, master_key: str, legacy_key: str = "") -> LibsodiumSealedBoxCipher:
    """The only supported way to build a credential cipher.

    `master_key` is `ALEPH_CREDENTIAL_MASTER_KEY` and nothing else — passing the
    agent-token signing secret here is the defect this workstream exists to
    remove, and
    `packages/aleph-connectors/tests/test_cipher_construction_sites.py` walks the AST of every
    call site to assert it. `legacy_key` opens v1 rows and is read-only.
    """
    read_secrets: dict[str, bytes] = {}
    v1 = legacy_v1_master_secret(legacy_key)
    if v1:
        read_secrets[LEGACY_KEY_VERSION] = v1
    return LibsodiumSealedBoxCipher(
        master_secret=master_key_bytes(master_key),
        key_version=CURRENT_KEY_VERSION,
        read_secrets=read_secrets,
    )


def credential_cipher_from_env() -> LibsodiumSealedBoxCipher:
    """`credential_cipher` for a call whose signature cannot yet carry settings.

    The worker research-tool binder is reached through
    `aleph_research.research_workflow` and `aleph_workers.jobs.research`, whose
    signatures are owned elsewhere; they thread `agent_token_secret` and nothing
    else. Reading the same env var pydantic-settings reads is the honest
    stopgap — it is the same setting, not a second source of truth — and the
    fix is to thread `credential_master_key` through those two callers.
    """
    master_key, legacy = master_key_from_env()
    return credential_cipher(master_key=master_key, legacy_key=legacy)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class ReencryptReport:
    """What a re-encryption pass actually did.

    `examined` and `reencrypted` are separate numbers because they answer
    different questions, and a pass that reports only "done" cannot tell an
    operator whether the old key is now safe to remove. It is safe exactly when
    a later pass reports `examined == 0`.
    """

    target_version: str
    examined: int = 0
    reencrypted: int = 0
    #: (credential id, reason) for every row that could not be opened. Never
    #: empty-and-ignored: `ok` is False while any of these exist.
    failures: list[tuple[UUID, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


class ConnectorCredentialService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: CredentialCipher,
        dev_default_for: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._defaults = dict(dev_default_for or {})

    async def upsert(
        self,
        *,
        ledger: LedgerWriter,
        principal: Principal,
        project_id: UUID,
        connector_id: UUID,
        connector_kind: str,
        plaintext: str,
    ) -> ConnectorCredential:
        from sqlalchemy import select

        if not plaintext.strip():
            msg = "credential plaintext is empty"
            raise ValidationFailed(msg)
        existing = (
            await self._session.execute(
                select(ConnectorCredential).where(
                    ConnectorCredential.project_id == project_id,
                    ConnectorCredential.connector_id == connector_id,
                )
            )
        ).scalar_one_or_none()
        blob = self._cipher.encrypt(project_id=project_id, plaintext=plaintext)
        action_kind = "connector_credential.update"
        if existing is None:
            existing = ConnectorCredential(
                id=uuid7(),
                project_id=project_id,
                connector_id=connector_id,
                cipher_blob=blob,
                cipher_scheme=self._cipher.scheme,
                key_version=self._cipher.key_version,
                kms_key_arn=None,
                rotated_at=None,
                created_by=principal.user_id,
            )
            self._session.add(existing)
            action_kind = "connector_credential.create"
        else:
            existing.cipher_blob = blob
            existing.cipher_scheme = self._cipher.scheme
            # The blob was just re-encrypted at the WRITE version, so the row's
            # version must move with it. Leaving a stale version here is the
            # exact shape of the bug this column exists to prevent: a row that
            # says v1 and holds v2 bytes decrypts to a CryptoError.
            existing.key_version = self._cipher.key_version
            existing.rotated_at = None
        await self._session.flush()
        from aleph_observability.tracing import current_trace_id

        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind=action_kind,
            target_id=existing.id,
            target_kind="connector_credential",
            payload={"connector_kind": connector_kind},  # NEVER plaintext
            trace_id=current_trace_id(),
        )
        return existing

    async def delete(
        self,
        *,
        ledger: LedgerWriter,
        principal: Principal,
        project_id: UUID,
        connector_id: UUID,
        connector_kind: str,
    ) -> bool:
        from sqlalchemy import select

        existing = (
            await self._session.execute(
                select(ConnectorCredential).where(
                    ConnectorCredential.project_id == project_id,
                    ConnectorCredential.connector_id == connector_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return False
        await self._session.delete(existing)
        await self._session.flush()
        from aleph_observability.tracing import current_trace_id

        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="connector_credential.delete",
            target_id=existing.id,
            target_kind="connector_credential",
            payload={"connector_kind": connector_kind},
            trace_id=current_trace_id(),
        )
        return True

    async def rotate(
        self,
        *,
        ledger: LedgerWriter,
        principal: Principal,
        project_id: UUID,
        connector_id: UUID,
        connector_kind: str,
        new_plaintext: str,
    ) -> ConnectorCredential:
        cred = await self.upsert(
            ledger=ledger,
            principal=principal,
            project_id=project_id,
            connector_id=connector_id,
            connector_kind=connector_kind,
            plaintext=new_plaintext,
        )
        cred.rotated_at = utcnow()
        await self._session.flush()
        return cred

    async def reencrypt(
        self,
        *,
        ledger: LedgerWriter,
        principal: Principal,
        project_id: UUID | None = None,
    ) -> ReencryptReport:
        """Move every row not already at the cipher's write version onto it.

        Step three of a rotation, and it is deliberately a separate call rather
        than something a boot hook does: re-encryption reads plaintext for every
        credential in the deployment, and that is an operator action with a
        ledger entry, not a side effect of a restart.

        Per row, not per batch. A row whose old key is missing or whose blob is
        corrupt is counted and named, and the rest still move — an all-or-nothing
        pass over N credentials fails on the first bad one and leaves the
        deployment split across two versions with no record of which.
        """
        from sqlalchemy import select

        stmt = select(ConnectorCredential).where(
            ConnectorCredential.key_version != self._cipher.key_version
        )
        if project_id is not None:
            stmt = stmt.where(ConnectorCredential.project_id == project_id)
        rows = list((await self._session.execute(stmt)).scalars().all())

        report = ReencryptReport(target_version=self._cipher.key_version, examined=len(rows))
        from aleph_observability.tracing import current_trace_id

        for row in rows:
            try:
                plaintext = self._cipher.decrypt(
                    project_id=row.project_id,
                    cipher_blob=bytes(row.cipher_blob),
                    key_version=row.key_version,
                )
            except Exception as exc:
                report.failures.append((row.id, f"{type(exc).__name__}: {exc}"[:300]))
                continue
            was = row.key_version
            row.cipher_blob = self._cipher.encrypt(project_id=row.project_id, plaintext=plaintext)
            row.cipher_scheme = self._cipher.scheme
            row.key_version = self._cipher.key_version
            await self._session.flush()
            await ledger.append(
                project_id=row.project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="connector_credential.reencrypt",
                target_id=row.id,
                target_kind="connector_credential",
                # Versions only. The payload of a credential event never carries
                # plaintext, and it never carries key material either.
                payload={"from_key_version": was, "to_key_version": row.key_version},
                trace_id=current_trace_id(),
            )
            report.reencrypted += 1
        return report

    async def decrypt_for_callback(
        self,
        *,
        project_id: UUID,
        connector_id: UUID,
        connector_kind: str,
    ) -> str:
        """The only decryption entry point. Called in-process by the
        research worker after the project's connector binding has been
        resolved. Falls back to the deployment env default if no
        project-specific credential exists."""
        from sqlalchemy import select

        existing = (
            await self._session.execute(
                select(ConnectorCredential).where(
                    ConnectorCredential.project_id == project_id,
                    ConnectorCredential.connector_id == connector_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._cipher.decrypt(
                project_id=project_id,
                cipher_blob=existing.cipher_blob,
                key_version=existing.key_version,
            )
        fallback = self._defaults.get(connector_kind, "").strip()
        if not fallback:
            msg = f"no credential available for {connector_kind}"
            raise NotFound(msg)
        return fallback
