"""Tests for the workspace profile endpoint.

Tests verify that GET/PUT /api/v1/user/workspace-profile correctly stores and
retrieves the company_name/timezone workspace profile via the per-user keyed
JSON configuration store.
"""

import pytest
from httpx import ASGITransport, AsyncClient


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
                "email": "test_workspace_profile@example.com",
                "password": "SecurePassword123!",
                "name": "Test User",
            },
        )
        assert resp.status_code == 200
        auth_data = resp.json()
        token = auth_data["token"]

    # Create a new client with the Bearer token
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as authenticated_client:
        yield authenticated_client


@pytest.mark.asyncio
async def test_get_before_any_put_returns_nulls(auth_client):
    """GET before any PUT should return an all-null profile, not 404."""
    resp = await auth_client.get("/api/v1/user/workspace-profile")
    assert resp.status_code == 200
    assert resp.json() == {"company_name": None, "timezone": None}


@pytest.mark.asyncio
async def test_put_then_get_roundtrips(auth_client):
    """PUT should persist the profile and GET should return the same values."""
    put_resp = await auth_client.put(
        "/api/v1/user/workspace-profile",
        json={"company_name": "Acme", "timezone": "Asia/Kolkata"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == {"company_name": "Acme", "timezone": "Asia/Kolkata"}

    get_resp = await auth_client.get("/api/v1/user/workspace-profile")
    assert get_resp.status_code == 200
    assert get_resp.json() == {"company_name": "Acme", "timezone": "Asia/Kolkata"}


@pytest.mark.asyncio
async def test_put_invalid_timezone_returns_422(auth_client):
    """PUT with an unknown IANA timezone should be rejected."""
    resp = await auth_client.put(
        "/api/v1/user/workspace-profile",
        json={"company_name": "Acme", "timezone": "Not/AZone"},
    )
    assert resp.status_code == 422
