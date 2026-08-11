"""Boot-time configuration validation for DEPLOYMENT_MODE=saas.

Fails fast with one aggregated error instead of letting a misconfigured
deployment limp into production (spec §1).
"""

from api.constants import (
    AUTH_PROVIDER,
    BILLING_ENGINE,
    BILLING_PAYMENTS_ENABLED,
    CLERK_ISSUER,
    CLERK_WEBHOOK_SECRET,
    CORS_ALLOWED_ORIGINS,
    DEPLOYMENT_MODE,
    DEPLOYMENT_MODE_SAAS,
    OSS_JWT_SECRET,
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET,
)

_DEFAULT_JWT_SECRET = "change-me-in-production"


def validate_saas_config() -> None:
    if DEPLOYMENT_MODE != DEPLOYMENT_MODE_SAAS:
        return

    problems: list[str] = []
    if AUTH_PROVIDER != "clerk":
        problems.append("AUTH_PROVIDER must be 'clerk' in saas mode")
    if BILLING_ENGINE != "local":
        problems.append("BILLING_ENGINE must be 'local' in saas mode")
    if not CLERK_ISSUER:
        problems.append("CLERK_ISSUER is required in saas mode")
    if not CLERK_WEBHOOK_SECRET:
        problems.append("CLERK_WEBHOOK_SECRET is required in saas mode")
    if not OSS_JWT_SECRET or OSS_JWT_SECRET == _DEFAULT_JWT_SECRET:
        problems.append("OSS_JWT_SECRET must be set to a non-default value")
    if not CORS_ALLOWED_ORIGINS:
        problems.append("CORS_ALLOWED_ORIGINS must be an explicit allowlist")
    if BILLING_PAYMENTS_ENABLED:
        for name, value in (
            ("RAZORPAY_KEY_ID", RAZORPAY_KEY_ID),
            ("RAZORPAY_KEY_SECRET", RAZORPAY_KEY_SECRET),
            ("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET),
        ):
            if not value:
                problems.append(
                    f"{name} is required when BILLING_PAYMENTS_ENABLED=true"
                )

    if problems:
        raise RuntimeError(
            "Invalid saas deployment configuration:\n- " + "\n- ".join(problems)
        )
