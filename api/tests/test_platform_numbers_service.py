"""platform_numbers service: purchase/release flows with Twilio mocked."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service
from api.services.telephony import platform_numbers
from api.services.telephony.platform_numbers import PlatformNumberError


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(balance_cents: int = 0):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_pn_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
        if balance_cents:
            await billing_service.credit(org.id, balance_cents, "topup")
        return org

    yield make_org
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


@pytest.fixture
def twilio():
    client = MagicMock()
    client.search_available_numbers = AsyncMock(
        return_value=[
            {
                "phone_number": "+14155551234",
                "friendly_name": "",
                "locality": "SF",
                "region": "CA",
            }
        ]
    )
    client.create_subaccount = AsyncMock(
        return_value={"sid": "AC_sub", "auth_token": "sub_tok"}
    )
    client.purchase_number = AsyncMock(
        return_value={"sid": "PN_new", "phone_number": "+14155551234"}
    )
    client.release_number = AsyncMock()
    with (
        patch.object(platform_numbers, "get_platform_twilio", return_value=client),
        patch.object(platform_numbers, "_saas_enabled", return_value=True),
    ):
        yield client


async def test_purchase_full_flow(real_db, twilio):
    org = await real_db(balance_cents=5000)  # rental US = 10 min = 1000
    phone = await platform_numbers.purchase(
        org.id, phone_number="+14155551234", country="US", inbound_workflow_id=None
    )
    assert phone.is_platform is True
    assert phone.provider_sid == "PN_new"
    assert phone.rental_cents == 1000
    assert phone.rental_status == "active"
    assert phone.next_renewal_at > datetime.now(timezone.utc)
    # subaccount config created + reused
    config = await db_client.get_platform_telephony_configuration(org.id)
    assert config.credentials["account_sid"] == "AC_sub"
    twilio.create_subaccount.assert_awaited_once()
    # voice_url wired to the shared inbound dispatcher
    voice_url = twilio.purchase_number.call_args.kwargs["voice_url"]
    assert voice_url.endswith("/api/v1/telephony/inbound/run")
    # first month charged
    assert await billing_service.get_balance_cents(org.id) == 4000
    # second purchase reuses the subaccount
    twilio.purchase_number.return_value = {
        "sid": "PN_2",
        "phone_number": "+14155551235",
    }
    await platform_numbers.purchase(
        org.id, phone_number="+14155551235", country="US", inbound_workflow_id=None
    )
    twilio.create_subaccount.assert_awaited_once()  # still once


async def test_purchase_insufficient_balance(real_db, twilio):
    org = await real_db(balance_cents=100)
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.purchase(
            org.id, phone_number="+14155551236", country="US", inbound_workflow_id=None
        )
    assert err.value.status_code == 402
    twilio.purchase_number.assert_not_awaited()  # never bought at Twilio


async def test_purchase_unsupported_country(real_db, twilio):
    org = await real_db(balance_cents=5000)
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.purchase(
            org.id,
            phone_number="+4915510000000",
            country="DE",
            inbound_workflow_id=None,
        )
    assert err.value.status_code == 400


async def test_release_flow(real_db, twilio):
    org = await real_db(balance_cents=5000)
    phone = await platform_numbers.purchase(
        org.id, phone_number="+14155551237", country="US", inbound_workflow_id=None
    )
    await platform_numbers.release(org.id, phone.id)
    twilio.release_number.assert_awaited_once()
    assert twilio.release_number.call_args.kwargs["number_sid"] == "PN_new"
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "released"
    assert rows[phone.id].is_active is False


async def test_release_cross_org_404(real_db, twilio):
    org_a = await real_db(balance_cents=5000)
    org_b = await real_db()
    phone = await platform_numbers.purchase(
        org_a.id, phone_number="+14155551238", country="US", inbound_workflow_id=None
    )
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.release(org_b.id, phone.id)
    assert err.value.status_code == 404


async def test_disabled_outside_saas(real_db):
    with patch.object(platform_numbers, "_saas_enabled", return_value=False):
        with pytest.raises(PlatformNumberError) as err:
            await platform_numbers.search("US", None, None)
        assert err.value.status_code == 404
