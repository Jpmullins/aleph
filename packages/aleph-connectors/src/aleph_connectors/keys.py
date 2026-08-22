"""The one place a credential-encryption key is derived, and the only place a
master key is checked.

Before WS-P7 this derivation existed in four copies — the cipher itself plus
three call sites that each re-implemented the master-secret handling — and all
four fed on ``ALEPH_AGENT_TOKEN_SECRET``, the HS256 secret that signs the
short-lived tokens workers use to call back into the API. One secret did three
unrelated jobs, so the standard response to a leaked signing key (rotate it)
silently and permanently destroyed every stored connector credential: real
third-party API keys and OAuth grants that Aleph cannot re-derive from anything.

Two things changed.

1. The encryption key is its own setting, ``ALEPH_CREDENTIAL_MASTER_KEY``.
2. Every ciphertext records the *version* of the key that produced it, so a
   deployment can read two generations at once. That is what turns rotation
   into a procedure rather than an incident: install the new key as the write
   key while the old one stays readable, re-encrypt every row, then drop the
   old key. Never in one step — a single-step swap is indistinguishable from
   losing the credentials.

Nothing here pads. The padding this replaced (``secret.ljust(32, b"0")``) was
applied *before* the cipher's own ``>= 32 bytes`` guard, so the guard could
never fire and a four-character secret was accepted as a 32-byte key.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

from aleph_core.errors import ValidationFailed

if TYPE_CHECKING:
    from uuid import UUID

#: The version stamped on everything written today: keyed by
#: ``ALEPH_CREDENTIAL_MASTER_KEY``.
CURRENT_KEY_VERSION = "v2"

#: Rows written before the split, keyed by the agent-token signing secret with
#: the historical padding. Readable, never written.
LEGACY_KEY_VERSION = "v1"

#: A master key shorter than the SecretBox key it feeds is not a key, it is a
#: prefix. `openssl rand -hex 32` gives 64 hex characters = 64 bytes.
MIN_MASTER_KEY_BYTES = 32

#: The filler byte the pre-split code padded short secrets with. Reproduced for
#: the v1 read path only — see `legacy_v1_master_secret`.
_V1_PAD_BYTE = b"0"

#: `.env.example` ships `CHANGE-ME-run-openssl-rand-hex-32`, which is exactly 32
#: characters — so a length check alone accepts the placeholder verbatim and the
#: whole deployment shares a key that is published in this repository. The
#: prefix check is the part that catches that, not the length.
_PLACEHOLDER_PREFIX = "CHANGE-ME"


class MasterKeyRejected(ValidationFailed, ValueError):
    """A master key that must not be used.

    Subclasses ``ValueError`` as well as ``ValidationFailed`` on purpose: raised
    from a pydantic-settings field validator it becomes a normal boot-time
    ``ValidationError`` naming the offending setting, and raised from the cipher
    it still maps to the API's 422 like every other ``ValidationFailed``.
    """


def derive_project_key(master_secret: bytes, project_id: UUID) -> bytes:
    """Derive one project's SecretBox key from the deployment master secret.

    THE key derivation. There is exactly one, and
    ``packages/aleph-connectors/tests/test_key_derivation_is_single.py`` walks
    the AST of every module under ``apps/`` and ``packages/`` to assert that —
    four copies is how three call sites came to disagree about padding while
    every one of them looked correct on its own.

    Deliberately *not* keyed by cipher version: v1 and v2 differ only in which
    master secret goes in. Re-deriving per version would be a second derivation
    to keep in step with this one.
    """
    return hashlib.sha256(master_secret + project_id.bytes).digest()


def master_key_bytes(value: str, *, setting: str = "ALEPH_CREDENTIAL_MASTER_KEY") -> bytes:
    """Validate the write-side master key and return its bytes, unpadded.

    Fails loudly here — at settings construction, i.e. at boot — rather than at
    the first decrypt, which is a background job at 3am whose only symptom is a
    connector quietly dropping out of the research loop.
    """
    text = value.strip()
    if not text:
        msg = (
            f"{setting} is not set. It encrypts every stored connector credential and "
            f"is deliberately NOT the agent-token signing secret. "
            f"Generate one with: openssl rand -hex 32"
        )
        raise MasterKeyRejected(msg)
    if text.upper().startswith(_PLACEHOLDER_PREFIX):
        msg = (
            f"{setting} is still the .env.example placeholder. That value is published in "
            f"this repository, so every deployment that keeps it shares one key. "
            f"Generate one with: openssl rand -hex 32"
        )
        raise MasterKeyRejected(msg)
    raw = text.encode("utf-8")
    if len(raw) < MIN_MASTER_KEY_BYTES:
        msg = (
            f"{setting} must be at least {MIN_MASTER_KEY_BYTES} bytes; got {len(raw)}. "
            f"It is NOT padded to length — padding is what let a short secret through the "
            f"cipher's own guard before WS-P7. Generate one with: openssl rand -hex 32"
        )
        raise MasterKeyRejected(msg)
    return raw


def legacy_v1_master_secret(legacy_key: str) -> bytes:
    """Reproduce the PRE-SPLIT master secret, byte for byte.

    Before WS-P7 the master secret was the agent-token signing secret encoded as
    UTF-8 and, when shorter than 32 bytes, right-padded with ASCII ``0``. That
    padding was the defect. It is reproduced here for exactly one reason: rows
    written before the split are still on disk, and a v1 row that cannot be
    decrypted is a third-party API key or an OAuth grant the operator has lost
    with no way back.

    Returns ``b""`` for an empty key — a deployment with no v1 key configured
    can still read and write v2, it simply cannot open v1 rows, and
    ``LibsodiumSealedBoxCipher`` says so by name rather than raising a
    ``CryptoError`` about a bad ciphertext.

    Delete this, and the v1 read path, once ``python -m
    aleph_connectors.reencrypt`` reports zero v1 rows on every deployment.
    """
    raw = legacy_key.strip().encode("utf-8")
    if not raw:
        return b""
    if len(raw) >= MIN_MASTER_KEY_BYTES:
        return raw
    return raw + _V1_PAD_BYTE * (MIN_MASTER_KEY_BYTES - len(raw))


def legacy_read_key(legacy_key: str, agent_token_secret: str) -> str:
    """Which secret opens v1 rows.

    v1 ciphertexts were keyed by the agent-token signing secret, so that is the
    default. ``ALEPH_CREDENTIAL_LEGACY_KEY`` overrides it, and exists for the
    deployment that has already rotated the signing secret: the old value is
    then the only thing that can open those rows, and it lives nowhere else.

    One function rather than an ``or`` at each call site, because the two are
    not interchangeable and picking the wrong one is silent.
    """
    return legacy_key.strip() or agent_token_secret


def master_key_from_env(
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """``(master_key, legacy_read_key)`` from the process environment.

    For the worker tool binder, which builds a cipher inside a call whose
    signature is owned by callers in another workstream and therefore cannot
    yet be threaded a settings object. The variable names are the settings
    names: ``ALEPH_CREDENTIAL_MASTER_KEY`` is the same value
    ``Settings.aleph_credential_master_key`` reads.
    """
    src = dict(os.environ) if env is None else env
    return (
        src.get("ALEPH_CREDENTIAL_MASTER_KEY", ""),
        legacy_read_key(
            src.get("ALEPH_CREDENTIAL_LEGACY_KEY", ""),
            src.get("ALEPH_AGENT_TOKEN_SECRET", ""),
        ),
    )
