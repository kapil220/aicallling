"""Platform telephony config + number client methods (phase 3).

Requires pgvector Postgres (docker-compose-local).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org():
        async with maker() as s:
            org = OrganizationModel(provider_id=f"org_p3_{uuid.uuid4().hex[:10]}")
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()  # CASCADE removes configs + numbers
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def _platform_number(org, config, address, **kw):
    phone = await db_client.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=config.id,
        address=address,
        country_code="US",
    )
    async with db_client.async_session() as s:
        from api.db.models import TelephonyPhoneNumberModel

        row = await s.get(TelephonyPhoneNumberModel, phone.id)
        row.is_platform = True
        row.provider_sid = kw.get("provider_sid", "PN_test")
        row.rental_cents = kw.get("rental_cents", 1000)
        row.next_renewal_at = kw.get("next_renewal_at")
        row.rental_status = kw.get("rental_status", "active")
        await s.commit()
    return phone


async def test_platform_config_create_and_get(real_db):
    org = await real_db()
    assert await db_client.get_platform_telephony_configuration(org.id) is None
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_sub", "auth_token": "tok", "from_numbers": []}
    )
    assert config.is_platform is True
    found = await db_client.get_platform_telephony_configuration(org.id)
    assert found.id == config.id
    # Scoped: another org sees nothing
    other = await real_db()
    assert await db_client.get_platform_telephony_configuration(other.id) is None


async def test_due_numbers_and_rental_update(real_db):
    org = await real_db()
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_sub2", "auth_token": "tok", "from_numbers": []}
    )
    now = datetime.now(timezone.utc)
    due = await _platform_number(
        org, config, "+15550001111", next_renewal_at=now - timedelta(days=1)
    )
    not_due = await _platform_number(
        org, config, "+15550002222", next_renewal_at=now + timedelta(days=10)
    )
    due_rows = await db_client.list_due_platform_numbers(now)
    due_ids = {r.id for r in due_rows}
    assert due.id in due_ids
    assert not_due.id not in due_ids

    await db_client.update_platform_number_rental(due.id, rental_status="past_due")
    listed = await db_client.list_platform_numbers_for_org(org.id)
    by_id = {r.id: r for r in listed}
    assert by_id[due.id].rental_status == "past_due"
    assert by_id[due.id].next_renewal_at is not None  # untouched by partial update

    await db_client.update_platform_number_rental(
        due.id, rental_status="released", is_active=False
    )
    released = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert released[due.id].is_active is False
    # released numbers no longer show up as due
    assert due.id not in {r.id for r in await db_client.list_due_platform_numbers(now)}


async def test_list_all_platform_numbers_spans_orgs(real_db):
    org_a, org_b = await real_db(), await real_db()
    for org, addr in ((org_a, "+15550003333"), (org_b, "+15550004444")):
        config = await db_client.create_platform_telephony_configuration(
            org.id,
            {"account_sid": f"AC_{org.id}", "auth_token": "t", "from_numbers": []},
        )
        await _platform_number(org, config, addr)
    rows = await db_client.list_all_platform_numbers()
    org_ids = {org_id for _, org_id in rows}
    assert {org_a.id, org_b.id} <= org_ids
