from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import auth as auth_routes
from api.schemas.auth import SignupRequest


@pytest.mark.asyncio
async def test_signup_creator_role_is_admin(monkeypatch):
    fake_user = SimpleNamespace(id=1, provider_id="p1", email="a@b.com")
    fake_org = SimpleNamespace(id=10)

    monkeypatch.setattr(
        auth_routes.db_client, "get_user_by_email", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        auth_routes.db_client,
        "create_user_with_email",
        AsyncMock(return_value=fake_user),
    )
    monkeypatch.setattr(
        auth_routes.db_client,
        "get_or_create_organization_by_provider_id",
        AsyncMock(return_value=(fake_org, True)),
    )
    add_user = AsyncMock()
    monkeypatch.setattr(auth_routes.db_client, "add_user_to_organization", add_user)
    monkeypatch.setattr(
        auth_routes.db_client, "update_user_selected_organization", AsyncMock()
    )
    monkeypatch.setattr(
        auth_routes,
        "create_user_configuration_with_mps_key",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(auth_routes, "capture_event", lambda *a, **k: None)

    await auth_routes.signup(
        SignupRequest(email="a@b.com", password="pw12345678", name="A")
    )

    add_user.assert_awaited_once_with(1, 10, role="admin")
