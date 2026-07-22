"""Daily rental renewal: charge, past_due, grace release. Twilio mocked."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, TelephonyPhoneNumberModel
from api.services.billing import billing_service, number_rental


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_setup(balance_cents: int, next_renewal_at):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_rent_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
        if balance_cents:
            await billing_service.credit(org.id, balance_cents, "topup")
        config = await db_client.create_platform_telephony_configuration(
            org.id, {"account_sid": "AC_r", "auth_token": "t", "from_numbers": []}
        )
        phone = await db_client.create_phone_number(
            organization_id=org.id,
            telephony_configuration_id=config.id,
            address=f"+1555{uuid.uuid4().int % 10**7:07d}",
        )
        async with maker() as s:
            row = await s.get(TelephonyPhoneNumberModel, phone.id)
            row.is_platform = True
            row.provider_sid = "PN_r"
            row.rental_cents = 1000
            row.next_renewal_at = next_renewal_at
            row.rental_status = "active"
            await s.commit()
        return org, phone

    yield make_setup
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


def _twilio():
    client = MagicMock()
    client.release_number = AsyncMock()
    return client


async def test_due_number_charged_and_advanced(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(5000, now - timedelta(hours=1))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["charged"] >= 1
    assert await billing_service.get_balance_cents(org.id) == 4000
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].next_renewal_at > now + timedelta(days=29)
    assert rows[phone.id].rental_status == "active"
    # idempotent: same renewal date can't double-charge
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        await number_rental.charge_due_rentals(now)
    assert await billing_service.get_balance_cents(org.id) == 4000


async def test_insufficient_balance_marks_past_due(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(hours=1))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["past_due"] >= 1
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "past_due"
    assert rows[phone.id].is_active is True  # still usable during grace
    assert await billing_service.get_balance_cents(org.id) == 100  # untouched


async def test_past_grace_releases_number(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(days=8))  # grace = 7
    twilio = _twilio()
    with patch.object(number_rental, "get_platform_twilio", return_value=twilio):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["released"] >= 1
    twilio.release_number.assert_awaited_once()
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "released"
    assert rows[phone.id].is_active is False


async def test_topped_up_past_due_recovers(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(days=2))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        await number_rental.charge_due_rentals(now)  # -> past_due
    await billing_service.credit(org.id, 5000, "topup")
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["charged"] >= 1
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "active"


def test_cron_registered():
    from api.tasks.arq import WorkerSettings
    from api.tasks.rental_billing import charge_number_rentals

    assert charge_number_rentals in WorkerSettings.functions
    assert len(WorkerSettings.cron_jobs) >= 1
