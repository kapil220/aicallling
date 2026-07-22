"""Daily call cap resolution + real count query (saas phase 2)."""

from datetime import datetime, timezone
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


async def test_daily_cap_blocks_at_cap(real_db):
    org = await real_db("org_cap_hit")  # trial -> daily_call_cap = 20
    with _saas(True), patch.object(
        db_client, "count_org_runs_since", AsyncMock(return_value=20)
    ):
        err = await plan_limits.check_daily_call_cap(org.id)
    assert err is not None
    assert "daily" in err.lower() or "per day" in err.lower()


async def test_daily_cap_allows_under(real_db):
    org = await real_db("org_cap_ok")
    with _saas(True), patch.object(
        db_client, "count_org_runs_since", AsyncMock(return_value=5)
    ):
        assert await plan_limits.check_daily_call_cap(org.id) is None


async def test_daily_cap_unlimited_scale(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_cap_scale", plan_id=scale.id)
    with _saas(True), patch.object(
        db_client, "count_org_runs_since", AsyncMock(return_value=10_000)
    ):
        assert await plan_limits.check_daily_call_cap(org.id) is None


async def test_count_org_runs_since_real_query(real_db):
    org = await real_db("org_cap_sql")
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert await db_client.count_org_runs_since(org.id, midnight) == 0


async def test_oss_mode_never_caps(real_db):
    org = await real_db("org_cap_oss")
    with _saas(False):
        assert await plan_limits.check_daily_call_cap(org.id) is None
