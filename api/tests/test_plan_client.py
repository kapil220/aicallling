"""PlanClient CRUD + subscription-field updates, real-DB pattern from test_payment_service.py."""

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, PlanModel


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str) -> OrganizationModel:
        async with maker() as s:
            org = OrganizationModel(provider_id=provider_id)
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
        await s.execute(delete(PlanModel).where(PlanModel.tier_key.like("t2_%")))
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_plan_crud_roundtrip(real_db):
    plan = await db_client.create_plan(
        tier_key="t2_custom",
        display_name="Custom",
        price_cents=100000,
        currency="inr",
        included_minutes=100,
        max_agents=5,
        max_concurrent_calls=3,
        daily_call_cap=50,
        max_active_campaigns=2,
        sort_order=99,
    )
    assert (await db_client.get_plan_by_tier_key("t2_custom")).id == plan.id
    updated = await db_client.update_plan(plan.id, razorpay_plan_id="plan_rzp_123")
    assert updated.razorpay_plan_id == "plan_rzp_123"
    assert (await db_client.get_plan_by_razorpay_plan_id("plan_rzp_123")).id == plan.id
    seeded = await db_client.list_active_plans()
    assert [p.tier_key for p in seeded[:3]] == ["starter", "pro", "scale"]


async def test_update_org_subscription_partial(real_db):
    org = await real_db("org_plan_client_1")
    starter = await db_client.get_plan_by_tier_key("starter")
    await db_client.update_org_subscription(
        org.id,
        plan_id=starter.id,
        razorpay_subscription_id="sub_abc",
        subscription_status="active",
    )
    found = await db_client.get_org_by_razorpay_subscription_id("sub_abc")
    assert found.id == org.id
    assert found.subscription_status == "active"
    # Partial update must not clobber the other fields
    await db_client.update_org_subscription(org.id, subscription_status="halted")
    found = await db_client.get_org_by_razorpay_subscription_id("sub_abc")
    assert found.subscription_status == "halted"
    assert found.plan_id == starter.id


async def test_record_subscription_invoice_idempotent(real_db):
    org = await real_db("org_plan_client_2")
    first = await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id="pay_dup",
        razorpay_subscription_id="sub_abc2",
        amount_cents=149900,
        currency="inr",
        status="captured",
    )
    second = await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id="pay_dup",
        razorpay_subscription_id="sub_abc2",
        amount_cents=149900,
        currency="inr",
        status="captured",
    )
    assert first.id == second.id
    invoices = await db_client.list_subscription_invoices(org.id)
    assert len(invoices) == 1
