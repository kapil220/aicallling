"""Custom SQLAlchemy column types.

``EncryptedJSON`` is the phase 4a mechanism for encrypting whole credential
columns at rest. It is a ``TypeDecorator`` rather than an ORM event listener on
purpose: ``webhook_credential_client`` writes ``credential_data`` through a
Core-level ``update().values()``, and ORM ``before_insert`` / ``before_update``
listeners never fire for Core statements — that write would silently store
plaintext. Bind-parameter processing covers ORM and Core alike.

The one thing this type cannot express is a rule that depends on a sibling
column, because ``process_bind_param`` receives only the value. That is why
``organization_configurations.value``, which is only secret for four of its key
types, stays a plain ``JSON`` column and is encrypted explicitly in its client.
"""

from typing import Any, Optional

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

from api.services.security import credential_crypto


class EncryptedJSON(TypeDecorator):
    """A JSON column whose value is stored as a Fernet token.

    Writes encrypt when a key is configured and store as-is when none is —
    so an OSS deployment that never sets a key is unaffected.

    Reads are content-sniffed rather than config-driven: a value that looks
    like a token is decrypted, anything else is returned unchanged. That is
    what keeps a half-backfilled table readable.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        return credential_crypto.encrypt_json(value)

    def process_result_value(self, value: Any, dialect) -> Optional[Any]:
        if value is None:
            return None
        return credential_crypto.decrypt_json(value)
