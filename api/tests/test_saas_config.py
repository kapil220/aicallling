import pytest
from cryptography.fernet import Fernet

from api import constants
from api.services.security import credential_crypto


@pytest.fixture(autouse=True)
def _reset_cipher():
    credential_crypto.reset_cipher_cache()
    yield
    credential_crypto.reset_cipher_cache()


def _set_credential_keys(monkeypatch, keys):
    monkeypatch.setattr(constants, "CREDENTIAL_ENCRYPTION_KEYS", list(keys))
    credential_crypto.reset_cipher_cache()


def _set_valid_saas_env(monkeypatch):
    _set_credential_keys(monkeypatch, [Fernet.generate_key().decode()])
    monkeypatch.setattr("api.services.saas_config.DEPLOYMENT_MODE", "saas")
    monkeypatch.setattr("api.services.saas_config.AUTH_PROVIDER", "clerk")
    monkeypatch.setattr("api.services.saas_config.BILLING_ENGINE", "local")
    monkeypatch.setattr(
        "api.services.saas_config.CLERK_ISSUER", "https://x.clerk.accounts.dev"
    )
    monkeypatch.setattr("api.services.saas_config.CLERK_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setattr("api.services.saas_config.OSS_JWT_SECRET", "a-strong-secret")
    monkeypatch.setattr(
        "api.services.saas_config.CORS_ALLOWED_ORIGINS", ["https://app.voxagent.com"]
    )


def test_oss_mode_is_never_validated(monkeypatch):
    from api.services.saas_config import validate_saas_config

    monkeypatch.setattr("api.services.saas_config.DEPLOYMENT_MODE", "oss")
    validate_saas_config()  # must not raise, regardless of other settings


def test_valid_saas_config_passes(monkeypatch):
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    validate_saas_config()


@pytest.mark.parametrize(
    "attr,bad_value,fragment",
    [
        ("AUTH_PROVIDER", "local", "AUTH_PROVIDER"),
        ("BILLING_ENGINE", "mps", "BILLING_ENGINE"),
        ("CLERK_ISSUER", None, "CLERK_ISSUER"),
        ("CLERK_WEBHOOK_SECRET", None, "CLERK_WEBHOOK_SECRET"),
        ("OSS_JWT_SECRET", "change-me-in-production", "OSS_JWT_SECRET"),
        ("CORS_ALLOWED_ORIGINS", [], "CORS_ALLOWED_ORIGINS"),
    ],
)
def test_invalid_saas_config_fails(monkeypatch, attr, bad_value, fragment):
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    monkeypatch.setattr(f"api.services.saas_config.{attr}", bad_value)
    with pytest.raises(RuntimeError, match=fragment):
        validate_saas_config()


def test_saas_mode_requires_a_credential_encryption_key(monkeypatch):
    """Optional for OSS self-hosters; a launch blocker for a hosted deployment."""
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    _set_credential_keys(monkeypatch, [])

    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEYS"):
        validate_saas_config()


def test_saas_mode_rejects_a_malformed_credential_encryption_key(monkeypatch):
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    _set_credential_keys(monkeypatch, ["not-a-fernet-key"])

    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEYS"):
        validate_saas_config()


def test_oss_mode_does_not_require_a_credential_encryption_key(monkeypatch):
    from api.services.saas_config import validate_saas_config

    monkeypatch.setattr("api.services.saas_config.DEPLOYMENT_MODE", "oss")
    _set_credential_keys(monkeypatch, [])
    validate_saas_config()
