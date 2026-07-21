"""Tests for the Clerk auth path in `get_user` (AUTH_PROVIDER=clerk).

Uses the suite's `db_session` savepoint fixture (see conftest.py) so
`get_or_create_user_by_provider_id` / `get_or_create_organization_by_provider_id`
/ etc. run against real (rolled-back) rows, matching the invariants asserted by
`test_signup_creator_is_admin.py` for local signup. Only the three external
seams named in the brief (`verify_clerk_token`, `grant_signup_trial`,
`seed_platform_model_configuration`) are mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

CLAIMS = {"sub": "user_clerk_1", "email": "clerk@example.com"}


@pytest.fixture
def clerk_mode(monkeypatch):
    monkeypatch.setattr("api.services.auth.depends.AUTH_PROVIDER", "clerk")


async def _call_get_user():
    from api.services.auth.depends import get_user

    return await get_user(authorization="Bearer fake", x_api_key=None)


@pytest.mark.asyncio
async def test_first_login_provisions_user_org_trial_and_config(
    clerk_mode, db_session
):
    with (
        patch(
            "api.services.auth.depends.verify_clerk_token",
            AsyncMock(return_value=CLAIMS),
        ),
        patch(
            "api.services.auth.depends.grant_signup_trial", AsyncMock()
        ) as trial,
        patch(
            "api.services.auth.depends.seed_platform_model_configuration",
            AsyncMock(),
        ) as seed,
    ):
        user = await _call_get_user()

    assert user.provider_id == "user_clerk_1"
    assert user.email == "clerk@example.com"
    assert user.selected_organization_id is not None
    trial.assert_awaited_once_with(user.selected_organization_id, created_by=user.id)
    seed.assert_awaited_once_with(user.selected_organization_id)

    # Creator must be org admin (same invariant as local signup).
    role = await db_session.get_member_role(user.selected_organization_id, user.id)
    assert role == "admin"


@pytest.mark.asyncio
async def test_second_login_is_idempotent(clerk_mode, db_session):
    with (
        patch(
            "api.services.auth.depends.verify_clerk_token",
            AsyncMock(return_value=CLAIMS),
        ),
        patch("api.services.auth.depends.grant_signup_trial", AsyncMock()) as trial,
        patch(
            "api.services.auth.depends.seed_platform_model_configuration",
            AsyncMock(),
        ) as seed,
    ):
        first = await _call_get_user()
        second = await _call_get_user()

    assert first.id == second.id
    # Provisioning side-effects fire only on org creation.
    assert trial.await_count <= 1
    assert seed.await_count <= 1


@pytest.mark.asyncio
async def test_invalid_token_401(clerk_mode, db_session):
    with patch(
        "api.services.auth.depends.verify_clerk_token",
        AsyncMock(side_effect=HTTPException(status_code=401, detail="bad")),
    ):
        with pytest.raises(HTTPException) as exc:
            await _call_get_user()
    assert exc.value.status_code == 401
