"""One-time signup trial grant (spec §6): free minutes, no card."""

from loguru import logger

from api.constants import BILLING_ENGINE, TRIAL_MINUTES
from api.services.billing import billing_service

# 1 minute at 1x burn == 100 ledger cents (spec §4).
CENTS_PER_MINUTE = 100


async def grant_signup_trial(organization_id: int, created_by: int | None = None) -> None:
    """Grant a one-time signup trial credit to an organization.

    Grants TRIAL_MINUTES * 100 cents once per org (idempotent via signup_trial key).
    No-op when TRIAL_MINUTES <= 0 or BILLING_ENGINE != "local".

    Args:
        organization_id: The organization to grant trial credits to.
        created_by: Optional user ID who triggered the grant.
    """
    if BILLING_ENGINE != "local" or TRIAL_MINUTES <= 0:
        return
    await billing_service.credit(
        organization_id,
        TRIAL_MINUTES * CENTS_PER_MINUTE,
        "grant",
        description=f"Signup trial: {TRIAL_MINUTES} free minutes",
        created_by=created_by,
        idempotency_key=f"signup_trial:{organization_id}",
    )
    logger.info("Granted {}min signup trial to org {}", TRIAL_MINUTES, organization_id)
