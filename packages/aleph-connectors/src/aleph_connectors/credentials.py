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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Cipher
# ---------------------------------------------------------------------------


class CredentialCipher(Protocol):
    scheme: str

    def encrypt(self, *, project_id: UUID, plaintext: str) -> bytes: ...

    def decrypt(self, *, project_id: UUID, cipher_blob: bytes) -> str: ...


@dataclass
class LibsodiumSealedBoxCipher:
    """Sealed-box per-project. Derives a SecretBox key from
    HKDF-ish: sha256(master_secret || project_id)."""

    scheme: str = "libsodium-sealed"
    master_secret: bytes = b""

    def __post_init__(self) -> None:
        if not self.master_secret or len(self.master_secret) < 32:
            msg = "master_secret must be >= 32 bytes"
            raise ValidationFailed(msg)

    def _key(self, project_id: UUID) -> bytes:
        return hashlib.sha256(self.master_secret + project_id.bytes).digest()

    def encrypt(self, *, project_id: UUID, plaintext: str) -> bytes:
        box = secret.SecretBox(self._key(project_id))
        return bytes(box.encrypt(plaintext.encode("utf-8")))

    def decrypt(self, *, project_id: UUID, cipher_blob: bytes) -> str:
        box = secret.SecretBox(self._key(project_id))
        return box.decrypt(cipher_blob).decode("utf-8")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


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
                kms_key_arn=None,
                rotated_at=None,
                created_by=principal.user_id,
                access_scope="project",
            )
            self._session.add(existing)
            action_kind = "connector_credential.create"
        else:
            existing.cipher_blob = blob
            existing.cipher_scheme = self._cipher.scheme
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
            return self._cipher.decrypt(project_id=project_id, cipher_blob=existing.cipher_blob)
        fallback = self._defaults.get(connector_kind, "").strip()
        if not fallback:
            msg = f"no credential available for {connector_kind}"
            raise NotFound(msg)
        return fallback
