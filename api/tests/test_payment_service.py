"""Tests for the Stripe payment rail: PaymentClient + PaymentService.

DB-backed tests use a real committing session factory (matching
api/tests/test_billing_service.py's real_db fixture). Stripe SDK calls are always
mocked — no live network calls. Requires the pgvector Postgres (docker-compose-local).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel, PaymentPackModel
from api.services.billing import billing_service, payment_service


@pytest.fixture(scope="module")
async def real_db(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    org_ids: list[int] = []
    pack_ids: list[int] = []

    async def make_org(provider_id, balance_cents=0):
        async with session_factory() as s:
            org = OrganizationModel(
                provider_id=provider_id, credit_balance_cents=balance_cents
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org.id

    async def make_pack(pack_key, price_cents, credits_granted):
        async with session_factory() as s:
            pack = PaymentPackModel(
                pack_key=pack_key,
                display_name=pack_key,
                price_cents=price_cents,
                credits_granted=credits_granted,
                currency="usd",
            )
            s.add(pack)
            await s.commit()
            await s.refresh(pack)
            pack_ids.append(pack.id)
            return pack.id

    yield make_org, make_pack

    async with session_factory() as s:
        if org_ids:
            await s.execute(
                delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
            )
        if pack_ids:
            await s.execute(
                delete(PaymentPackModel).where(PaymentPackModel.id.in_(pack_ids))
            )
        await s.commit()
    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


def _event(event_id, event_type, obj):
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


@pytest.mark.asyncio
async def test_checkout_completed_credits_ledger_once(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_webhook")
    pack_id = await make_pack("starter_10", 1000, 1000)
    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_webhook",
        stripe_customer_id="cus_wh",
        amount_cents_paid=1000,
        currency="usd",
        credits_granted=1000,
    )
    event = _event(
        "evt_1",
        "checkout.session.completed",
        {
            "id": "cs_test_webhook",
            "payment_intent": "pi_wh",
            "payment_status": "paid",
            "metadata": {"payment_id": str(payment.id), "pack_key": "starter_10"},
        },
    )

    await payment_service.handle_checkout_completed(event)
    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "succeeded"
    assert updated.credit_ledger_id is not None
    assert await billing_service.get_balance_cents(org_id) == 1000

    await payment_service.handle_checkout_completed(event)  # replay
    assert await billing_service.get_balance_cents(org_id) == 1000


@pytest.mark.asyncio
async def test_unpaid_session_is_noop(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_unpaid")
    pack_id = await make_pack("starter_10b", 1000, 1000)
    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_unpaid",
        stripe_customer_id="cus_x",
        amount_cents_paid=1000,
        currency="usd",
        credits_granted=1000,
    )
    event = _event(
        "evt_unpaid",
        "checkout.session.completed",
        {
            "id": "cs_unpaid",
            "payment_intent": None,
            "payment_status": "unpaid",
            "metadata": {"payment_id": str(payment.id)},
        },
    )
    await payment_service.handle_checkout_completed(event)
    assert (await db_client.get_payment_by_id(payment.id)).status == "pending"
    assert await billing_service.get_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_partial_refund_proportional(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_refund")
    pack_id = await make_pack("scale_100", 10000, 10500)
    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_refund",
        stripe_customer_id="cus_w",
        amount_cents_paid=10000,
        currency="usd",
        credits_granted=10500,
    )
    await payment_service.handle_checkout_completed(
        _event(
            "evt_pay",
            "checkout.session.completed",
            {
                "id": "cs_refund",
                "payment_intent": "pi_refund",
                "payment_status": "paid",
                "metadata": {"payment_id": str(payment.id)},
            },
        )
    )
    assert await billing_service.get_balance_cents(org_id) == 10500

    await payment_service.handle_charge_refunded(
        _event(
            "evt_refund",
            "charge.refunded",
            {"payment_intent": "pi_refund", "amount_refunded": 5000},
        )
    )
    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "partially_refunded"
    assert await billing_service.get_balance_cents(org_id) == 10500 - 5250


@pytest.mark.asyncio
async def test_refund_before_success_raises_retryable(real_db):
    from api.db import db_client  # noqa: F401

    refund_event = _event(
        "evt_early",
        "charge.refunded",
        {"payment_intent": "pi_never", "amount_refunded": 1000},
    )
    with pytest.raises(payment_service.RefundTooEarlyError):
        await payment_service.handle_charge_refunded(refund_event)


@pytest.mark.asyncio
async def test_ensure_stripe_customer_creates_and_persists(real_db):
    make_org, _ = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_customer")
    org = SimpleNamespace(id=org_id, stripe_customer_id=None)
    fake = SimpleNamespace(id="cus_new")
    with patch.object(
        payment_service.stripe.Customer, "create_async", AsyncMock(return_value=fake)
    ) as m:
        cid = await payment_service.ensure_stripe_customer(org)
        assert cid == "cus_new"
        assert (await db_client.get_org_stripe_customer_id(org_id)) == "cus_new"
        m.assert_awaited_once()
