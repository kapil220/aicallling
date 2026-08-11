"""Superadmin plans CRUD — mirrors the payment-packs admin surface.

Requires pgvector Postgres (docker-compose-local).
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.app import app
from api.db.models import PlanModel
from api.services.auth.depends import get_superuser


@pytest.fixture
def superuser_override():
    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    yield
    app.dependency_overrides.pop(get_superuser, None)


@pytest_asyncio.fixture
async def cleanup_plans():
    from api.db import db_client

    async def _purge():
        async with db_client.async_session() as s:
            await s.execute(delete(PlanModel).where(PlanModel.tier_key.like("t3_%")))
            await s.commit()

    await _purge()
    yield
    await _purge()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_list_plans_includes_seeded(superuser_override):
    async with _client() as client:
        resp = await client.get("/api/v1/superuser/plans")
    assert resp.status_code == 200
    tiers = [p["tier_key"] for p in resp.json()]
    assert {"starter", "pro", "scale"} <= set(tiers)


@pytest.mark.asyncio
async def test_create_and_update_plan(superuser_override, cleanup_plans):
    async with _client() as client:
        resp = await client.post(
            "/api/v1/superuser/plans",
            json={
                "tier_key": "t3_biz",
                "display_name": "Business",
                "price_cents": 999900,
                "currency": "inr",
                "included_minutes": 3000,
                "max_agents": 30,
                "max_concurrent_calls": 15,
                "daily_call_cap": 2000,
                "max_active_campaigns": 10,
            },
        )
        assert resp.status_code == 200
        plan_id = resp.json()["id"]

        resp = await client.patch(
            f"/api/v1/superuser/plans/{plan_id}",
            json={"razorpay_plan_id": "plan_live_xyz", "is_active": False},
        )
    assert resp.status_code == 200
    assert resp.json()["razorpay_plan_id"] == "plan_live_xyz"
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_update_missing_plan_404(superuser_override):
    async with _client() as client:
        resp = await client.patch(
            "/api/v1/superuser/plans/999999", json={"is_active": False}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plans_require_superuser():
    async with _client() as client:
        resp = await client.get("/api/v1/superuser/plans")
    assert resp.status_code in (401, 403)
