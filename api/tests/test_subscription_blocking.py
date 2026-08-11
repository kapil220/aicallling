"""halted/cancelled orgs are blocked from starting calls in saas mode."""

from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services import quota_service
from api.services.billing import plan_limits


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str, subscription_status: str | None = None):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=provider_id, subscription_status=subscription_status
            )
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


async def test_halted_org_blocked(real_db):
    org = await real_db("org_blk_halted", subscription_status="halted")
    with _saas(True):
        result = await quota_service._check_subscription_state(org.id)
    assert result is not None
    assert result.error_code == "subscription_inactive"
    assert result.has_quota is False


async def test_cancelled_org_blocked(real_db):
    org = await real_db("org_blk_cancelled", subscription_status="cancelled")
    with _saas(True):
        result = await quota_service._check_subscription_state(org.id)
    assert result is not None


async def test_trial_org_not_blocked(real_db):
    org = await real_db("org_blk_trial", subscription_status=None)
    with _saas(True):
        assert await quota_service._check_subscription_state(org.id) is None


async def test_active_org_not_blocked(real_db):
    org = await real_db("org_blk_active", subscription_status="active")
    with _saas(True):
        assert await quota_service._check_subscription_state(org.id) is None


async def test_oss_mode_not_blocked(real_db):
    org = await real_db("org_blk_oss", subscription_status="halted")
    with _saas(False):
        assert await quota_service._check_subscription_state(org.id) is None
