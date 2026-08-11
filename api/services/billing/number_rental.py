"""Monthly platform-number rental billing (spec §8, saas phase 3).

Runs from a daily ARQ cron. Charges are idempotent on the renewal date, so
re-running the same day is a no-op. Insufficient balance marks the number
past_due (still routable during grace); past grace the number is released at
Twilio and the row deactivated.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from api.constants import NUMBER_RENTAL_GRACE_DAYS
from api.db import db_client
from api.services.billing import billing_service
from api.services.telephony.platform_twilio import (
    PlatformTwilioError,
    get_platform_twilio,
)

RENTAL_PERIOD_DAYS = 30


async def _release_at_twilio(row) -> bool:
    config = await db_client.get_platform_telephony_configuration(row.organization_id)
    if config is None or not row.provider_sid:
        return False
    try:
        client = get_platform_twilio()
        await client.release_number(
            subaccount_sid=config.credentials["account_sid"],
            subaccount_token=config.credentials["auth_token"],
            number_sid=row.provider_sid,
        )
        return True
    except PlatformTwilioError as e:
        logger.error(f"Failed to release number {row.id} at Twilio: {e.detail}")
        return False


async def charge_due_rentals(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    grace = timedelta(days=NUMBER_RENTAL_GRACE_DAYS)
    stats = {"charged": 0, "past_due": 0, "released": 0}

    for row in await db_client.list_due_platform_numbers(now):
        try:
            rental = int(row.rental_cents or 0)
            balance = await billing_service.get_balance_cents(row.organization_id)
            if rental and balance >= rental:
                await billing_service.credit(
                    row.organization_id,
                    -rental,
                    "number_rental",
                    description=f"Number rental renewal: {row.address}",
                    idempotency_key=(
                        f"number_rental:{row.id}:{row.next_renewal_at.date().isoformat()}"
                    ),
                )
                await db_client.update_platform_number_rental(
                    row.id,
                    rental_status="active",
                    next_renewal_at=row.next_renewal_at
                    + timedelta(days=RENTAL_PERIOD_DAYS),
                )
                stats["charged"] += 1
            elif now >= row.next_renewal_at + grace:
                released = await _release_at_twilio(row)
                await db_client.update_platform_number_rental(
                    row.id, rental_status="released", is_active=False
                )
                stats["released"] += 1
                logger.warning(
                    f"Released platform number {row.address} (org "
                    f"{row.organization_id}) after {NUMBER_RENTAL_GRACE_DAYS}d grace"
                    f"{'' if released else ' (Twilio release failed — orphaned)'}"
                )
            else:
                await db_client.update_platform_number_rental(
                    row.id, rental_status="past_due"
                )
                stats["past_due"] += 1
                logger.warning(
                    f"Platform number {row.address} past due (org "
                    f"{row.organization_id}): balance {balance} < rental {rental}"
                )
        except Exception:
            logger.exception(f"Rental processing failed for number {row.id}")

    logger.info(f"Rental run complete: {stats}")
    return stats
