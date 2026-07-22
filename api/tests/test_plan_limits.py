"""plan_limits: plan-driven limits with trial fallbacks."""

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import plan_limits


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str, plan_id: int | None = None):
        async with maker() as s:
            org = OrganizationModel(provider_id=provider_id, plan_id=plan_id)
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
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_trial_org_gets_trial_defaults(real_db):
    org = await real_db("org_limits_trial")
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents == 3
    assert limits.max_concurrent_calls == 2
    assert limits.daily_call_cap == 20
    assert limits.max_active_campaigns == 1


async def test_subscribed_org_gets_plan_limits(real_db):
    pro = await db_client.get_plan_by_tier_key("pro")
    org = await real_db("org_limits_pro", plan_id=pro.id)
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents == 15
    assert limits.max_concurrent_calls == 10
    assert limits.max_active_campaigns == 5


async def test_unlimited_is_none(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_limits_scale", plan_id=scale.id)
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents is None
    assert limits.daily_call_cap is None
