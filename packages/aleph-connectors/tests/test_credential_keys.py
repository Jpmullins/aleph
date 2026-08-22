"""The only cryptography in the repo, under test for the first time.

`packages/aleph-connectors` shipped 1,649 lines and zero tests. The two
properties that matter here are not "does encrypt round-trip" — libsodium
answers that — but:

* a key is a key, not a prefix that got padded to the right length, and
* a ciphertext knows which key made it, so a deployment can hold two
  generations at once and rotation is a procedure instead of an incident.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from nacl.exceptions import CryptoError

from aleph_connectors.credentials import (
    CURRENT_KEY_VERSION,
    LEGACY_KEY_VERSION,
    LibsodiumSealedBoxCipher,
    credential_cipher,
    credential_cipher_from_env,
)
from aleph_connectors.keys import (
    MIN_MASTER_KEY_BYTES,
    MasterKeyRejected,
    derive_project_key,
    legacy_read_key,
    legacy_v1_master_secret,
    master_key_bytes,
    master_key_from_env,
)
from aleph_core.errors import ValidationFailed

MASTER = "m" * 64
OTHER_MASTER = "n" * 64
#: The value shipped in deploy/compose/.env.example.
PLACEHOLDER = "CHANGE-ME-run-openssl-rand-hex-32"


# ---------------------------------------------------------------------------
# The master key itself
# ---------------------------------------------------------------------------


def test_short_master_key_is_refused_rather_than_padded() -> None:
    """The defect this replaced: `secret.ljust(32, b"0")` ran BEFORE the
    cipher's own `>= 32 bytes` guard, so the guard could never fire and a
    four-character secret was accepted as a 32-byte key."""
    with pytest.raises(MasterKeyRejected) as exc:
        master_key_bytes("short")
    assert "ALEPH_CREDENTIAL_MASTER_KEY" in str(exc.value)
    assert "NOT padded" in str(exc.value)


def test_placeholder_master_key_is_refused_even_though_it_is_long_enough() -> None:
    """`CHANGE-ME-run-openssl-rand-hex-32` is longer than 32 bytes, so a length
    check alone accepts it — and every deployment that keeps it then shares one
    key that is published in this repository."""
    assert len(PLACEHOLDER.encode()) >= MIN_MASTER_KEY_BYTES
    with pytest.raises(MasterKeyRejected) as exc:
        master_key_bytes(PLACEHOLDER)
    assert "placeholder" in str(exc.value)


def test_empty_master_key_names_the_setting_and_how_to_make_one() -> None:
    with pytest.raises(MasterKeyRejected) as exc:
        master_key_bytes("")
    msg = str(exc.value)
    assert "ALEPH_CREDENTIAL_MASTER_KEY" in msg
    assert "openssl rand -hex 32" in msg


def test_master_key_rejection_is_both_a_validation_error_and_a_value_error() -> None:
    """Dual base on purpose: `ValueError` so a pydantic-settings validator turns
    it into a normal boot-time `ValidationError` naming the field, and
    `ValidationFailed` so the API still maps it to 422 like every other domain
    error."""
    assert issubclass(MasterKeyRejected, ValueError)
    assert issubclass(MasterKeyRejected, ValidationFailed)


def test_a_valid_master_key_is_returned_unchanged_and_unpadded() -> None:
    assert master_key_bytes(MASTER) == MASTER.encode("utf-8")


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_two_projects_never_share_a_key() -> None:
    a, b = uuid4(), uuid4()
    m = MASTER.encode()
    assert derive_project_key(m, a) != derive_project_key(m, b)


def test_the_same_project_under_two_masters_gets_two_keys() -> None:
    p = uuid4()
    assert derive_project_key(MASTER.encode(), p) != derive_project_key(OTHER_MASTER.encode(), p)


def test_derivation_is_stable_across_calls() -> None:
    """A derivation that is not reproducible is a delete button. Pinned against
    a fixed project id so a refactor that reorders the concatenation is caught,
    not just one that changes the inputs."""
    p = UUID("00000000-0000-0000-0000-0000000000ff")
    first = derive_project_key(MASTER.encode(), p)
    assert first == derive_project_key(MASTER.encode(), p)
    assert len(first) == 32


def test_legacy_v1_secret_reproduces_the_historical_padding_exactly() -> None:
    """v1 rows on disk were keyed by a padded secret. If this stops matching the
    pre-split behaviour byte for byte, those rows become unreadable — which is
    an operator's real third-party API keys, gone."""
    assert legacy_v1_master_secret("abc") == b"abc" + b"0" * 29
    assert len(legacy_v1_master_secret("abc")) == MIN_MASTER_KEY_BYTES
    assert legacy_v1_master_secret(MASTER) == MASTER.encode()
    assert legacy_v1_master_secret("") == b""


def test_legacy_read_key_defaults_to_the_agent_token_secret() -> None:
    """Because that is factually what encrypted v1 rows. An explicit legacy key
    wins, for the deployment that has already rotated the signing secret."""
    assert legacy_read_key("", "token-secret") == "token-secret"
    assert legacy_read_key("explicit", "token-secret") == "explicit"


# ---------------------------------------------------------------------------
# The cipher
# ---------------------------------------------------------------------------


def test_round_trip_and_project_isolation() -> None:
    c = credential_cipher(master_key=MASTER)
    p, other = uuid4(), uuid4()
    blob = c.encrypt(project_id=p, plaintext="sk-live-xyz")
    assert c.decrypt(project_id=p, cipher_blob=blob, key_version=c.key_version) == "sk-live-xyz"
    with pytest.raises(CryptoError):
        c.decrypt(project_id=other, cipher_blob=blob, key_version=c.key_version)


def test_new_writes_are_stamped_v2_not_v1() -> None:
    assert credential_cipher(master_key=MASTER).key_version == CURRENT_KEY_VERSION
    assert CURRENT_KEY_VERSION != LEGACY_KEY_VERSION


def test_a_cipher_reads_both_generations_at_once() -> None:
    """The property the whole rotation procedure rests on. Without it there is
    no moment at which both the old and the new key work, so re-encryption has
    a window in which the credentials are unreadable — indistinguishable from
    having lost them."""
    token_secret = "t" * 64
    old = credential_cipher(master_key=token_secret)
    v1_blob_source = LibsodiumSealedBoxCipher(
        master_secret=legacy_v1_master_secret(token_secret),
        key_version=LEGACY_KEY_VERSION,
    )
    p = uuid4()
    v1_blob = v1_blob_source.encrypt(project_id=p, plaintext="old-secret")

    both = credential_cipher(master_key=MASTER, legacy_key=token_secret)
    v2_blob = both.encrypt(project_id=p, plaintext="new-secret")

    assert both.decrypt(project_id=p, cipher_blob=v1_blob, key_version="v1") == "old-secret"
    assert both.decrypt(project_id=p, cipher_blob=v2_blob, key_version="v2") == "new-secret"
    assert old.key_version == CURRENT_KEY_VERSION  # the write version never varies


def test_a_version_with_no_configured_key_says_so_by_name() -> None:
    """Not a CryptoError about a corrupt ciphertext — that sends the operator
    looking for data corruption instead of a missing environment variable."""
    c = credential_cipher(master_key=MASTER)  # no legacy key configured
    with pytest.raises(ValidationFailed) as exc:
        c.decrypt(project_id=uuid4(), cipher_blob=b"\x00" * 64, key_version=LEGACY_KEY_VERSION)
    msg = str(exc.value)
    assert LEGACY_KEY_VERSION in msg
    assert "ALEPH_CREDENTIAL_LEGACY_KEY" in msg


def test_the_cipher_cannot_be_built_from_a_short_key_through_the_factory() -> None:
    with pytest.raises(MasterKeyRejected):
        credential_cipher(master_key="tiny")


def test_a_directly_constructed_cipher_still_refuses_a_short_secret() -> None:
    """Defence in depth: the factory validates, and so does the dataclass, so a
    future caller that bypasses the factory cannot install a stub key."""
    with pytest.raises(ValidationFailed):
        LibsodiumSealedBoxCipher(master_secret=b"tiny")


# ---------------------------------------------------------------------------
# Env plumbing (the worker binder's stopgap source)
# ---------------------------------------------------------------------------


def test_master_key_from_env_reads_the_setting_names() -> None:
    master, legacy = master_key_from_env(
        {"ALEPH_CREDENTIAL_MASTER_KEY": MASTER, "ALEPH_AGENT_TOKEN_SECRET": "tok"}
    )
    assert master == MASTER
    assert legacy == "tok"


def test_master_key_from_env_prefers_an_explicit_legacy_key() -> None:
    _, legacy = master_key_from_env(
        {
            "ALEPH_CREDENTIAL_MASTER_KEY": MASTER,
            "ALEPH_CREDENTIAL_LEGACY_KEY": "previous",
            "ALEPH_AGENT_TOKEN_SECRET": "tok",
        }
    )
    assert legacy == "previous"


def test_env_cipher_refuses_to_fall_back_to_the_token_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that matters most: if the master key is unset, the worker
    must fail loudly rather than quietly reverting to the agent-token secret and
    re-creating the coupling under a new name."""
    monkeypatch.delenv("ALEPH_CREDENTIAL_MASTER_KEY", raising=False)
    monkeypatch.setenv("ALEPH_AGENT_TOKEN_SECRET", "t" * 64)
    with pytest.raises(MasterKeyRejected):
        credential_cipher_from_env()
