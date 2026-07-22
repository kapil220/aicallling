"""Max-agents and max-active-campaigns checks (saas phase 2)."""

from unittest.mock import AsyncMock, patch

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


def _saas(enabled: bool):
    return patch.object(plan_limits, "enforcement_enabled", return_value=enabled)


async def test_agent_limit_blocks_at_cap(real_db):
    org = await real_db("org_enf_agents")  # trial -> max_agents = 3
    with _saas(True), patch.object(
        db_client,
        "get_workflow_counts",
        AsyncMock(return_value={"total": 3, "active": 3, "archived": 0}),
    ):
        err = await plan_limits.check_can_create_agent(org.id)
    assert err is not None
    assert "agent" in err.lower()


async def test_agent_limit_allows_under_cap(real_db):
    org = await real_db("org_enf_agents_ok")
    with _saas(True), patch.object(
        db_client,
        "get_workflow_counts",
        AsyncMock(return_value={"total": 2, "active": 2, "archived": 0}),
    ):
        assert await plan_limits.check_can_create_agent(org.id) is None


async def test_agent_limit_unlimited_for_scale(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_enf_scale", plan_id=scale.id)
    with _saas(True), patch.object(
        db_client,
        "get_workflow_counts",
        AsyncMock(return_value={"total": 999, "active": 999, "archived": 0}),
    ):
        assert await plan_limits.check_can_create_agent(org.id) is None


async def test_campaign_limit_blocks_at_cap(real_db):
    org = await real_db("org_enf_campaigns")  # trial -> max_active_campaigns = 1
    with _saas(True), patch.object(
        db_client, "count_active_campaigns", AsyncMock(return_value=1)
    ):
        err = await plan_limits.check_can_start_campaign(org.id)
    assert err is not None
    assert "campaign" in err.lower()


async def test_campaign_limit_allows_under_cap(real_db):
    org = await real_db("org_enf_campaigns_ok")
    with _saas(True), patch.object(
        db_client, "count_active_campaigns", AsyncMock(return_value=0)
    ):
        assert await plan_limits.check_can_start_campaign(org.id) is None


async def test_count_active_campaigns_real_query(real_db):
    org = await real_db("org_enf_count_sql")
    assert await db_client.count_active_campaigns(org.id) == 0


async def test_oss_mode_never_blocks(real_db):
    org = await real_db("org_enf_oss")
    with _saas(False):
        assert await plan_limits.check_can_create_agent(org.id) is None
        assert await plan_limits.check_can_start_campaign(org.id) is None
