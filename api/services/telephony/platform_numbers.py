"""Platform-provisioned Twilio numbers (spec §8, saas phase 3).

Purchase creates (lazily) a per-org Twilio subaccount, buys the number into
it with the shared inbound dispatcher as VoiceUrl, records a flagged
telephony_phone_numbers row, and charges the first month's rental from the
credit ledger. Release relinquishes the number at Twilio and deactivates the
row (kept for history).
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from api.constants import (
    IS_SAAS_MODE,
    PLATFORM_NUMBER_COUNTRIES,
    PLATFORM_NUMBER_RENTAL_MINUTES,
    PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT,
)
from api.db import db_client
from api.services.billing import billing_service
from api.services.billing.trial import CENTS_PER_MINUTE
from api.services.telephony.platform_twilio import (
    PlatformTwilioError,
    get_platform_twilio,
)

RENTAL_PERIOD_DAYS = 30


class PlatformNumberError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _saas_enabled() -> bool:
    return IS_SAAS_MODE


def rental_cents_for_country(country: str) -> int:
    minutes = PLATFORM_NUMBER_RENTAL_MINUTES.get(
        country.upper(), PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT
    )
    return minutes * CENTS_PER_MINUTE


def _require_available(country: str) -> None:
    if not _saas_enabled():
        raise PlatformNumberError(404, "platform_numbers_unavailable")
    if country.upper() not in PLATFORM_NUMBER_COUNTRIES:
        raise PlatformNumberError(400, "country_not_supported")


async def _inbound_voice_url() -> str:
    # Same URL _sync_inbound_for_phone_number wires for BYO numbers — the
    # shared dispatcher resolves org + workflow from (subaccount sid, number).
    from api.utils.common import get_backend_endpoints

    backend_endpoint, _ = await get_backend_endpoints()
    return f"{backend_endpoint}/api/v1/telephony/inbound/run"


async def search(country: str, area_code, contains) -> list[dict]:
    _require_available(country)
    try:
        client = get_platform_twilio()
        return await client.search_available_numbers(
            country=country.upper(), area_code=area_code, contains=contains
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)


async def _ensure_subaccount(organization_id: int) -> dict:
    config = await db_client.get_platform_telephony_configuration(organization_id)
    if config is not None:
        return config.credentials
    client = get_platform_twilio()
    sub = await client.create_subaccount(f"voxagent-org-{organization_id}")
    credentials = {
        "account_sid": sub["sid"],
        "auth_token": sub["auth_token"],
        "from_numbers": [],
    }
    await db_client.create_platform_telephony_configuration(organization_id, credentials)
    return credentials


async def purchase(
    organization_id: int,
    *,
    phone_number: str,
    country: str,
    inbound_workflow_id,
):
    _require_available(country)
    rental = rental_cents_for_country(country)
    balance = await billing_service.get_balance_cents(organization_id)
    if balance < rental:
        raise PlatformNumberError(402, "insufficient_balance_for_rental")

    try:
        credentials = await _ensure_subaccount(organization_id)
        client = get_platform_twilio()
        bought = await client.purchase_number(
            subaccount_sid=credentials["account_sid"],
            subaccount_token=credentials["auth_token"],
            phone_number=phone_number,
            voice_url=await _inbound_voice_url(),
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)

    config = await db_client.get_platform_telephony_configuration(organization_id)
    phone = await db_client.create_phone_number(
        organization_id=organization_id,
        telephony_configuration_id=config.id,
        address=bought["phone_number"],
        country_code=country.upper(),
        label="Platform number",
        inbound_workflow_id=inbound_workflow_id,
    )
    next_renewal = datetime.now(timezone.utc) + timedelta(days=RENTAL_PERIOD_DAYS)
    async with db_client.async_session() as session:
        from api.db.models import TelephonyPhoneNumberModel

        row = await session.get(TelephonyPhoneNumberModel, phone.id)
        row.is_platform = True
        row.provider_sid = bought["sid"]
        row.rental_cents = rental
        row.next_renewal_at = next_renewal
        row.rental_status = "active"
        await session.commit()
        await session.refresh(row)

    await billing_service.credit(
        organization_id,
        -rental,
        "number_rental",
        description=f"Number rental (first month): {bought['phone_number']}",
        idempotency_key=f"number_rental:{phone.id}:initial",
    )
    logger.info(
        f"Platform number purchased org={organization_id} number={bought['phone_number']}"
    )
    return row


async def release(organization_id: int, phone_number_id: int) -> None:
    if not _saas_enabled():
        raise PlatformNumberError(404, "platform_numbers_unavailable")
    rows = await db_client.list_platform_numbers_for_org(organization_id)
    row = next((r for r in rows if r.id == phone_number_id), None)
    if row is None or row.rental_status == "released":
        raise PlatformNumberError(404, "number_not_found")
    config = await db_client.get_platform_telephony_configuration(organization_id)
    try:
        client = get_platform_twilio()
        await client.release_number(
            subaccount_sid=config.credentials["account_sid"],
            subaccount_token=config.credentials["auth_token"],
            number_sid=row.provider_sid,
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)
    await db_client.update_platform_number_rental(
        phone_number_id, rental_status="released", is_active=False
    )
