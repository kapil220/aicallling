"""Local email/password auth routes (signup/login/me) are an OSS-mode-only
flow. In saas mode (AUTH_PROVIDER=clerk) they must 404 instead of being
reachable alongside Clerk auth."""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def async_client(db_session):
    from api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_signup_404_when_auth_provider_not_local(async_client, monkeypatch):
    from api.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "AUTH_PROVIDER", "clerk")

    resp = await async_client.post(
        "/api/v1/auth/signup",
        json={
            "email": "gated@example.com",
            "password": "SecurePassword123!",
            "name": "Gated User",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_login_404_when_auth_provider_not_local(async_client, monkeypatch):
    from api.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "AUTH_PROVIDER", "clerk")

    resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": "gated@example.com", "password": "SecurePassword123!"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_me_404_when_auth_provider_not_local(async_client, monkeypatch):
    from api.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "AUTH_PROVIDER", "clerk")

    # No Authorization header at all — if the gate didn't run first, `get_user`
    # would raise 401 (missing Clerk token) instead of the intended 404.
    resp = await async_client.get("/api/v1/auth/me")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_signup_still_works_in_oss_mode(async_client, monkeypatch):
    from api.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "AUTH_PROVIDER", "local")

    resp = await async_client.post(
        "/api/v1/auth/signup",
        json={
            "email": "ungated@example.com",
            "password": "SecurePassword123!",
            "name": "Ungated User",
        },
    )
    assert resp.status_code == 200
