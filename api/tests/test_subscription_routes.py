"""Webhook + customer subscription routes. Provider is always mocked.

Requires pgvector Postgres (docker-compose-local).
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.app import app
from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, PlanModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing.providers.base import SubscriptionCheckout


def _mock_provider(verify=True):
    provider = MagicMock()
    provider.verify_webhook_signature.return_value = verify
    provider.create_subscription = AsyncMock(
        return_value=SubscriptionCheckout(
            provider_subscription_id="sub_new", checkout_url="https://rzp.io/i/new"
        )
    )
    provider.cancel_subscription = AsyncMock()
    return provider


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str):
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
        await s.execute(delete(PlanModel).where(PlanModel.tier_key.like("t9_%")))
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


@pytest_asyncio.fixture
async def org(real_db):
    return await real_db(f"org_sub_routes_{uuid.uuid4().hex[:10]}")


@pytest.fixture
def client(org, monkeypatch):
    from api.routes import billing as billing_routes
    from api.routes import webhooks as webhook_routes

    monkeypatch.setattr(billing_routes, "BILLING_PAYMENTS_ENABLED", True)
    monkeypatch.setattr(webhook_routes, "BILLING_PAYMENTS_ENABLED", True)
    fake_user = type(
        "U", (), {"id": 1, "selected_organization_id": org.id, "is_superuser": False}
    )()
    app.dependency_overrides[get_user_with_selected_organization] = lambda: fake_user
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest_asyncio.fixture
async def linked_plan():
    plan = await db_client.create_plan(
        tier_key=f"t9_linked_{uuid.uuid4().hex[:10]}",
        display_name="Linked",
        price_cents=100000,
        currency="inr",
        included_minutes=100,
        max_concurrent_calls=2,
        razorpay_plan_id="plan_t9",
        sort_order=98,
    )
    yield plan


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(client):
    with patch(
        "api.routes.webhooks.get_provider", return_value=_mock_provider(verify=False)
    ):
        async with client as c:
            resp = await c.post(
                "/api/v1/webhooks/razorpay",
                content=json.dumps({"event": "subscription.charged"}),
                headers={"X-Razorpay-Signature": "bad"},
            )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_dispatches_verified_event(client):
    handled = AsyncMock()
    with (
        patch("api.routes.webhooks.get_provider", return_value=_mock_provider()),
        patch("api.routes.webhooks.subscription_service") as svc,
    ):
        svc.handle_event = handled
        async with client as c:
            resp = await c.post(
                "/api/v1/webhooks/razorpay",
                content=json.dumps({"event": "subscription.charged", "payload": {}}),
                headers={
                    "X-Razorpay-Signature": "good",
                    "X-Razorpay-Event-Id": "evt_9",
                },
            )
    assert resp.status_code == 200
    handled.assert_awaited_once()
    assert handled.await_args.args[1] == "evt_9"


@pytest.mark.asyncio
async def test_list_plans_public(client):
    async with client as c:
        resp = await c.get("/api/v1/billing/plans")
    assert resp.status_code == 200
    tiers = [p["tier_key"] for p in resp.json()]
    assert "starter" in tiers


@pytest.mark.asyncio
async def test_subscription_empty_for_trial_org(client):
    async with client as c:
        resp = await c.get("/api/v1/billing/subscription")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_tier"] is None
    assert body["subscription_status"] is None


@pytest.mark.asyncio
async def test_subscribe_requires_linked_plan(client):
    # seeded plans have razorpay_plan_id = NULL -> not purchasable yet
    async with client as c:
        resp = await c.post("/api/v1/billing/subscribe", json={"tier_key": "starter"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "plan_not_purchasable"


@pytest.mark.asyncio
async def test_subscribe_returns_checkout_url(client, linked_plan, org):
    with patch("api.routes.billing.get_provider", return_value=_mock_provider()):
        async with client as c:
            resp = await c.post(
                "/api/v1/billing/subscribe", json={"tier_key": linked_plan.tier_key}
            )
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://rzp.io/i/new"
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_new")
    assert fresh.id == org.id


@pytest.mark.asyncio
async def test_cancel_without_subscription_409(client):
    async with client as c:
        resp = await c.post("/api/v1/billing/cancel")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_invoices_empty(client):
    async with client as c:
        resp = await c.get("/api/v1/billing/invoices")
    assert resp.status_code == 200
    assert resp.json() == []
