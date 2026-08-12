"""Symmetric encryption for credentials stored in the database (spec §4a).

This module owns all key handling. Nothing else in the codebase imports
``Fernet`` — callers go through :class:`api.db.types.EncryptedJSON` for whole
columns, or through :func:`encrypt_json` / :func:`decrypt_json` for the
conditional org-configuration case.

Encryption is optional. With ``CREDENTIAL_ENCRYPTION_KEYS`` unset, every
function here is a pass-through and storage behaviour is byte-for-byte what it
was before phase 4a. ``validate_saas_config`` is what makes a key mandatory in
saas mode.

Reading is deliberately independent of that switch: :func:`decrypt_json`
decides by inspecting the value, not by asking whether encryption is enabled.
That is what lets a table hold plaintext and ciphertext at the same time, which
every row does during the backfill.
"""

import json
from typing import Any, List, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from api import constants

# Fernet tokens are urlsafe-base64 and start with a 0x80 version byte, which
# always renders as this prefix. Checked together with the JSON type: every
# legacy value in these columns is an object, so a *string* starting with
# ``gAAAAA`` is unambiguously ciphertext.
_FERNET_PREFIX = "gAAAAA"

_cipher: Optional[MultiFernet] = None
_cipher_loaded = False


class CredentialEncryptionError(Exception):
    """Raised when a stored credential cannot be decrypted.

    Deliberately fatal. Returning ``None`` or ``{}`` here would turn "the wrong
    key is deployed" into calls that silently fail to authenticate against the
    carrier — far harder to diagnose than a loud error at read time.
    """


def _configured_keys() -> List[str]:
    return list(constants.CREDENTIAL_ENCRYPTION_KEYS)


def get_cipher() -> Optional[MultiFernet]:
    """The process-wide cipher, or None when no key is configured.

    Built once and cached: ``MultiFernet`` validates and decodes every key on
    construction, and the inbound-call path reads credentials on every call.
    """
    global _cipher, _cipher_loaded
    if _cipher_loaded:
        return _cipher

    keys = _configured_keys()
    if not keys:
        _cipher = None
    else:
        try:
            _cipher = MultiFernet([Fernet(k) for k in keys])
        except (ValueError, TypeError) as e:
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEYS contains an invalid Fernet key. "
                "Each key must be a urlsafe-base64-encoded 32-byte value "
                "(generate with Fernet.generate_key())."
            ) from e
    _cipher_loaded = True
    return _cipher


def reset_cipher_cache() -> None:
    """Drop the cached cipher so the next call re-reads the configured keys.

    Exists for tests, which flip keys between cases. Production builds the
    cipher once per process and never calls this.
    """
    global _cipher, _cipher_loaded
    _cipher = None
    _cipher_loaded = False


def encryption_enabled() -> bool:
    return get_cipher() is not None


def generate_key() -> str:
    """A new Fernet key, for operators bootstrapping or rotating."""
    return Fernet.generate_key().decode()


def is_encrypted(raw: Any) -> bool:
    """True when ``raw`` is a Fernet token produced by :func:`encrypt_json`."""
    return isinstance(raw, str) and raw.startswith(_FERNET_PREFIX)


def encrypt_json(value: Any) -> Any:
    """Encrypt a JSON-serialisable value into a Fernet token.

    Returns the value untouched when no key is configured, or when it is
    already ciphertext — the latter is what makes the backfill idempotent.
    """
    if value is None:
        return None
    if is_encrypted(value):
        return value
    cipher = get_cipher()
    if cipher is None:
        return value

    return cipher.encrypt(json.dumps(value).encode()).decode()


def decrypt_json(raw: Any) -> Any:
    """Decrypt a stored value, passing plaintext through unchanged.

    Raises :class:`CredentialEncryptionError` when the value is a token that no
    configured key can open — see the class docstring for why that is fatal.
    """
    if not is_encrypted(raw):
        return raw

    cipher = get_cipher()
    if cipher is None:
        raise CredentialEncryptionError(
            "Encountered an encrypted credential but CREDENTIAL_ENCRYPTION_KEYS "
            "is not set. Configure the key this data was encrypted with."
        )

    try:
        return json.loads(cipher.decrypt(raw.encode()).decode())
    except InvalidToken as e:
        raise CredentialEncryptionError(
            "Failed to decrypt a stored credential: no configured key opens "
            "this token. Check CREDENTIAL_ENCRYPTION_KEYS still contains the "
            "key this row was encrypted with."
        ) from e


def rotate(raw: Any) -> Any:
    """Re-wrap an existing token under the current primary key."""
    if not is_encrypted(raw):
        return raw
    cipher = get_cipher()
    if cipher is None:
        raise CredentialEncryptionError(
            "Cannot rotate credentials with no CREDENTIAL_ENCRYPTION_KEYS set."
        )
    try:
        return cipher.rotate(raw.encode()).decode()
    except InvalidToken as e:
        raise CredentialEncryptionError(
            "Failed to rotate a stored credential: no configured key opens this token."
        ) from e
