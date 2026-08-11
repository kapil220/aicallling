"""Daily platform-number rental billing (ARQ cron, saas phase 3)."""

from loguru import logger

from api.constants import IS_SAAS_MODE
from api.services.billing import number_rental


async def charge_number_rentals(_ctx) -> dict:
    if not IS_SAAS_MODE:
        return {"skipped": "not_saas"}
    stats = await number_rental.charge_due_rentals()
    logger.info(f"charge_number_rentals: {stats}")
    return stats
