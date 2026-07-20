import pytest


def _set_valid_saas_env(monkeypatch):
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
