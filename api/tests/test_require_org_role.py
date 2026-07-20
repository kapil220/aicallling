from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.enums import Role
from api.services.auth import depends as auth_depends


def _user(is_superuser=False, org_id=1):
    return SimpleNamespace(
        id=1, is_superuser=is_superuser, selected_organization_id=org_id
    )


@pytest.mark.asyncio
async def test_superuser_bypasses_role_check(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value=None)
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    result_user, role = await dep(user=_user(is_superuser=True))
    assert result_user.is_superuser is True
    assert role is None


@pytest.mark.asyncio
async def test_admin_passes_admin_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="admin")
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    _, role = await dep(user=_user())
    assert role == Role.ADMIN


@pytest.mark.asyncio
async def test_member_fails_admin_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await dep(user=_user())
    assert exc.value.status_code == 403
    assert exc.value.detail == "org_admin_required"


@pytest.mark.asyncio
async def test_member_passes_member_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    dep = auth_depends.require_org_role(Role.MEMBER)
    _, role = await dep(user=_user())
    assert role == Role.MEMBER


@pytest.mark.asyncio
async def test_has_org_role_helper(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    assert await auth_depends.has_org_role(_user(), 1, Role.MEMBER) is True
    assert await auth_depends.has_org_role(_user(), 1, Role.ADMIN) is False
