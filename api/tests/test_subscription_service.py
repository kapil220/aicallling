"""Razorpay webhook handling: lifecycle transitions + idempotent ledger grants."""

from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service, subscription_service


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    # identical committing-fixture shape to test_payment_service.py
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str, balance_cents: int = 0):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=provider_id, credit_balance_cents=balance_cents
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org
    # Undo the razorpay_plan_id link so seeded-plan assertions elsewhere hold.
    starter = await db_client.get_plan_by_tier_key("starter")
    if starter and starter.razorpay_plan_id == "plan_starter_t8":
        await db_client.update_plan(starter.id, razorpay_plan_id=None)
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


def _sub_event(
    event_type,
    *,
    sub_id,
    plan_id,
    org_id,
    current_end=1893456000,
    payment_id="pay_1",
    amount=149900,
):
    return {
        "entity": "event",
        "event": event_type,
        "payload": {
            "subscription": {
                "entity": {
                    "id": sub_id,
                    "plan_id": plan_id,
                    "status": event_type.split(".")[-1],
                    "current_end": current_end,
                    "notes": {"organization_id": str(org_id)},
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            },
        },
    }


async def _linked_starter_plan():
    starter = await db_client.get_plan_by_tier_key("starter")
    if starter.razorpay_plan_id is None:
        starter = await db_client.update_plan(
            starter.id, razorpay_plan_id="plan_starter_t8"
        )
    return starter


async def test_activated_links_org(real_db):
    org = await real_db("org_sub_activated")
    plan = await _linked_starter_plan()
    ev = _sub_event(
        "subscription.activated",
        sub_id="sub_act",
        plan_id=plan.razorpay_plan_id,
        org_id=org.id,
    )
    await subscription_service.handle_event(ev, "evt_act_1")
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_act")
    assert fresh.id == org.id
    assert fresh.subscription_status == "active"
    assert fresh.plan_id == plan.id
    assert fresh.current_period_end == datetime.fromtimestamp(
        1893456000, tz=timezone.utc
    )


async def test_charged_resets_then_grants_idempotently(real_db):
    org = await real_db("org_sub_charged", balance_cents=700)  # leftover trial
    plan = await _linked_starter_plan()
    ev = _sub_event(
        "subscription.charged",
        sub_id="sub_chg",
        plan_id=plan.razorpay_plan_id,
        org_id=org.id,
        payment_id="pay_chg",
    )
    await subscription_service.handle_event(ev, "evt_chg_1")
    # leftover 700 expired, then 300 min * 100 granted
    assert await billing_service.get_balance_cents(org.id) == 30000
    # replay: no double grant
    await subscription_service.handle_event(ev, "evt_chg_1")
    assert await billing_service.get_balance_cents(org.id) == 30000
    invoices = await db_client.list_subscription_invoices(org.id)
    assert len(invoices) == 1
    assert invoices[0].razorpay_payment_id == "pay_chg"
    entries = await db_client.list_ledger_entries(org.id)
    types = [e.type for e in entries]
    assert "plan_renewal" in types
    assert "plan_period_reset" in types


async def test_charged_zero_balance_skips_reset(real_db):
    org = await real_db("org_sub_zero", balance_cents=0)
    plan = await _linked_starter_plan()
    ev = _sub_event(
        "subscription.charged",
        sub_id="sub_zero",
        plan_id=plan.razorpay_plan_id,
        org_id=org.id,
        payment_id="pay_zero",
    )
    await subscription_service.handle_event(ev, "evt_zero_1")
    entries = await db_client.list_ledger_entries(org.id)
    assert [e.type for e in entries if e.type == "plan_period_reset"] == []
    assert await billing_service.get_balance_cents(org.id) == 30000


async def test_halted_and_cancelled_set_status(real_db):
    org = await real_db("org_sub_halted")
    plan = await _linked_starter_plan()
    act = _sub_event(
        "subscription.activated",
        sub_id="sub_hlt",
        plan_id=plan.razorpay_plan_id,
        org_id=org.id,
    )
    await subscription_service.handle_event(act, "evt_h_0")
    await subscription_service.handle_event(
        _sub_event(
            "subscription.halted",
            sub_id="sub_hlt",
            plan_id=plan.razorpay_plan_id,
            org_id=org.id,
        ),
        "evt_h_1",
    )
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_hlt")
    assert fresh.subscription_status == "halted"
    await subscription_service.handle_event(
        _sub_event(
            "subscription.cancelled",
            sub_id="sub_hlt",
            plan_id=plan.razorpay_plan_id,
            org_id=org.id,
        ),
        "evt_h_2",
    )
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_hlt")
    assert fresh.subscription_status == "cancelled"


async def test_payment_failed_records_invoice_without_state_change(real_db):
    org = await real_db("org_sub_pf")
    plan = await _linked_starter_plan()
    act = _sub_event(
        "subscription.activated",
        sub_id="sub_pf",
        plan_id=plan.razorpay_plan_id,
        org_id=org.id,
    )
    await subscription_service.handle_event(act, "evt_pf_0")
    await subscription_service.handle_event(
        _sub_event(
            "payment.failed",
            sub_id="sub_pf",
            plan_id=plan.razorpay_plan_id,
            org_id=org.id,
            payment_id="pay_pf",
        ),
        "evt_pf_1",
    )
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_pf")
    assert fresh.subscription_status == "active"  # grace period, no change
    invoices = await db_client.list_subscription_invoices(org.id)
    assert invoices[0].status == "failed"


async def test_unknown_event_ignored(real_db):
    await subscription_service.handle_event(
        {"event": "invoice.paid", "payload": {}}, "evt_x"
    )
