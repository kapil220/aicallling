"""Tests for the billing balance endpoint.

Tests verify that GET /api/v1/billing/balance correctly returns the current
credit balance, independent of BILLING_PAYMENTS_ENABLED.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from api.services.billing import billing_service


@pytest.fixture(autouse=True)
def local_billing(monkeypatch):
    """Ensure BILLING_ENGINE is set to 'local' for these tests."""
    from api.routes import billing_balance

    monkeypatch.setattr(billing_balance, "BILLING_ENGINE", "local")


@pytest.fixture
async def auth_client(db_session):
    """Authenticated HTTP client via local auth signup and Bearer token."""
    from api.app import app

    # Signup via local auth
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": "test_balance@example.com",
                "password": "SecurePassword123!",
                "name": "Test User",
            },
        )
        assert resp.status_code == 200
        auth_data = resp.json()
        token = auth_data["token"]
        org_id = auth_data["user"]["organization_id"]

    # Create a new client with the Bearer token
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as authenticated_client:
        yield authenticated_client, org_id


@pytest.mark.asyncio
async def test_balance_zero_for_fresh_org(auth_client):
    """Fresh organization should have zero balance."""
    client, org_id = auth_client
    resp = await client.get("/api/v1/billing/balance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance_cents"] == 0
    assert data["minutes_equivalent"] == 0.0


@pytest.mark.asyncio
async def test_balance_reflects_ledger(auth_client):
    """Balance endpoint should reflect credits from billing ledger."""
    client, org_id = auth_client

    # Credit the organization
    await billing_service.credit(org_id, 1500, "grant", description="test")

    # Check balance endpoint
    resp = await client.get("/api/v1/billing/balance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance_cents"] == 1500
    assert data["minutes_equivalent"] == 15.0


@pytest.mark.asyncio
async def test_balance_404_when_billing_engine_not_local(auth_client, monkeypatch):
    """Balance endpoint should return 404 when BILLING_ENGINE != 'local'."""
    client, org_id = auth_client

    # Monkeypatch BILLING_ENGINE in the route module
    from api.routes import billing_balance

    monkeypatch.setattr(billing_balance, "BILLING_ENGINE", "mps")

    resp = await client.get("/api/v1/billing/balance")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ledger_reflects_credit(auth_client):
    """Ledger endpoint should list a credit entry after crediting the org."""
    client, org_id = auth_client

    await billing_service.credit(org_id, 500, "grant", description="demo credit")

    resp = await client.get("/api/v1/billing/ledger")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["amount_cents"] == 500
    assert entry["type"] == "grant"
    assert entry["description"] == "demo credit"


@pytest.mark.asyncio
async def test_ledger_404_when_billing_engine_not_local(auth_client, monkeypatch):
    """Ledger endpoint should return 404 when BILLING_ENGINE != 'local'."""
    client, org_id = auth_client

    from api.routes import billing_balance

    monkeypatch.setattr(billing_balance, "BILLING_ENGINE", "mps")

    resp = await client.get("/api/v1/billing/ledger")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ledger_is_org_scoped(auth_client):
    """An org's ledger entries must never leak into another org's listing."""
    from api.app import app

    client, org_id = auth_client

    # Second organization via a fresh signup.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as anon_client:
        resp = await anon_client.post(
            "/api/v1/auth/signup",
            json={
                "email": "test_balance_other_org@example.com",
                "password": "SecurePassword123!",
                "name": "Other Org User",
            },
        )
        assert resp.status_code == 200
        other_org_id = resp.json()["user"]["organization_id"]

    await billing_service.credit(org_id, 700, "grant", description="org 1 credit")
    await billing_service.credit(other_org_id, 900, "grant", description="org 2 credit")

    resp = await client.get("/api/v1/billing/ledger")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["description"] == "org 1 credit"
    assert all(entry["description"] != "org 2 credit" for entry in data)
