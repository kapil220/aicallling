"""Crypto core for credential encryption at rest (phase 4a)."""

import pytest
from cryptography.fernet import Fernet

from api import constants
from api.services.security import credential_crypto


@pytest.fixture
def key_a() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def key_b() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def set_keys(monkeypatch):
    """Install a key list and rebuild the cached cipher around one test."""

    def _set(keys):
        monkeypatch.setattr(constants, "CREDENTIAL_ENCRYPTION_KEYS", list(keys))
        credential_crypto.reset_cipher_cache()

    yield _set
    credential_crypto.reset_cipher_cache()


def test_round_trip(set_keys, key_a):
    set_keys([key_a])
    payload = {"account_sid": "AC123", "auth_token": "supersecret"}

    token = credential_crypto.encrypt_json(payload)

    assert token != payload
    assert credential_crypto.is_encrypted(token)
    assert "supersecret" not in token
    assert credential_crypto.decrypt_json(token) == payload


def test_no_key_configured_is_a_pass_through(set_keys):
    set_keys([])
    payload = {"auth_token": "plaintext-is-fine-in-oss"}

    assert credential_crypto.encryption_enabled() is False
    assert credential_crypto.encrypt_json(payload) == payload
    assert credential_crypto.decrypt_json(payload) == payload


def test_plaintext_passes_through_the_read_path(set_keys, key_a):
    """Half-backfilled tables must stay readable — this is what allows it."""
    set_keys([key_a])

    assert credential_crypto.decrypt_json({"still": "plaintext"}) == {
        "still": "plaintext"
    }
    assert credential_crypto.decrypt_json(None) is None
    assert credential_crypto.decrypt_json("an ordinary string") == "an ordinary string"


def test_encrypting_ciphertext_is_a_no_op(set_keys, key_a):
    """What makes the backfill idempotent across interrupted runs."""
    set_keys([key_a])
    token = credential_crypto.encrypt_json({"a": 1})

    assert credential_crypto.encrypt_json(token) == token


def test_rotation_decrypts_under_the_old_key_and_re_wraps_under_the_new(
    set_keys, key_a, key_b
):
    set_keys([key_a])
    token = credential_crypto.encrypt_json({"a": 1})

    # New key prepended: the old one still opens existing rows.
    set_keys([key_b, key_a])
    assert credential_crypto.decrypt_json(token) == {"a": 1}

    rotated = credential_crypto.rotate(token)
    assert rotated != token

    # Once rotated, the old key is no longer needed.
    set_keys([key_b])
    assert credential_crypto.decrypt_json(rotated) == {"a": 1}


def test_wrong_key_raises_rather_than_returning_empty(set_keys, key_a, key_b):
    """Silent failure here would surface as calls not authenticating."""
    set_keys([key_a])
    token = credential_crypto.encrypt_json({"a": 1})

    set_keys([key_b])
    with pytest.raises(credential_crypto.CredentialEncryptionError):
        credential_crypto.decrypt_json(token)


def test_ciphertext_with_no_key_configured_raises(set_keys, key_a):
    set_keys([key_a])
    token = credential_crypto.encrypt_json({"a": 1})

    set_keys([])
    with pytest.raises(credential_crypto.CredentialEncryptionError):
        credential_crypto.decrypt_json(token)


def test_invalid_key_is_rejected_at_cipher_construction(set_keys):
    set_keys(["not-a-valid-fernet-key"])

    with pytest.raises(credential_crypto.CredentialEncryptionError):
        credential_crypto.get_cipher()


def test_is_encrypted_discriminates_on_type_and_prefix(set_keys, key_a):
    set_keys([key_a])

    assert credential_crypto.is_encrypted(credential_crypto.encrypt_json({"a": 1}))
    # A dict is unambiguously legacy plaintext, whatever it contains.
    assert not credential_crypto.is_encrypted({"gAAAAA": "looks like a token"})
    assert not credential_crypto.is_encrypted("gAAA")
    assert not credential_crypto.is_encrypted(None)
    assert not credential_crypto.is_encrypted(42)
