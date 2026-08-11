"""Schema-level checks for the phase-2 plans tables (migration b4e40d0de0004)."""

from sqlalchemy import select

from api.db import db_client
from api.db.models import OrganizationModel, PlanModel


async def test_seeded_plans_exist(db_session):
    async with db_client.async_session() as s:
        rows = (
            (await s.execute(select(PlanModel).order_by(PlanModel.sort_order)))
            .scalars()
            .all()
        )
    tiers = {p.tier_key: p for p in rows}
    assert {"starter", "pro", "scale"} <= set(tiers)
    assert tiers["starter"].included_minutes == 300
    assert tiers["starter"].max_agents == 3
    assert tiers["pro"].included_minutes == 1500
    assert tiers["pro"].max_concurrent_calls == 10
    assert tiers["scale"].max_agents is None  # NULL = unlimited
    assert tiers["scale"].max_active_campaigns is None
    assert all(p.razorpay_plan_id is None for p in rows)  # linked later via superadmin


async def test_org_subscription_columns_default_null(db_session):
    async with db_client.async_session() as s:
        org = OrganizationModel(provider_id="org_plan_schema_test")
        s.add(org)
        await s.flush()
        assert org.plan_id is None
        assert org.razorpay_subscription_id is None
        assert org.subscription_status is None
        assert org.current_period_end is None
        await s.rollback()
