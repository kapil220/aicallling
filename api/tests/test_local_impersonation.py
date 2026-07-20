from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.routes import superuser as superuser_routes
from api.services.auth.depends import get_superuser
from api.utils.auth import decode_jwt_token


@pytest.mark.asyncio
async def test_local_mode_impersonate_mints_jwt(monkeypatch):
    monkeypatch.setattr(superuser_routes, "AUTH_PROVIDER", "local")

    target_user = type(
        "U", (), {"id": 42, "provider_id": "prov-42", "email": "target@example.com"}
    )()
    monkeypatch.setattr(
        superuser_routes.db_client,
        "get_user_by_id",
        AsyncMock(return_value=target_user),
    )

    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/superuser/impersonate", json={"user_id": 42}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"] == body["refresh_token"]
        payload = decode_jwt_token(body["access_token"])
        assert payload["sub"] == "42"
        assert payload["impersonated_by"] == "1"
    finally:
        app.dependency_overrides.pop(get_superuser, None)
