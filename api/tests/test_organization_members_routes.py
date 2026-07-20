"""Route tests for member management. Requires the pgvector Postgres
(docker-compose-local) since they create real orgs/users."""

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


async def _make_org_with_members(roles: list[str]):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_members_{id(object())}")
        s.add(org)
        await s.flush()
        users = []
        for i, _ in enumerate(roles):
            u = UserModel(
                provider_id=f"member_user_{id(object())}_{i}",
                email=f"user{i}_{id(object())}@example.com",
            )
            s.add(u)
            users.append(u)
        await s.flush()
        await s.commit()
        for u in users:
            await s.refresh(u)
        await s.refresh(org)

    for u, role in zip(users, roles):
        await db_client.add_user_to_organization(u.id, org.id, role=role)

    return org.id, users


def _override_as(user_id, org_id):
    def _dep():
        return type(
            "U",
            (),
            {"id": user_id, "selected_organization_id": org_id, "is_superuser": False},
        )()

    app.dependency_overrides[get_user_with_selected_organization] = _dep


@pytest.mark.asyncio
async def test_member_cannot_invite():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(member.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/organization/members/invite",
                json={"email": admin.email, "role": "member"},
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_admin_can_change_member_role():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/organization/members/{member.id}",
                json={"role": "admin"},
            )
        assert r.status_code == 200
        assert await db_client.get_member_role(org_id, member.id) == "admin"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_last_admin_demotion_returns_409():
    org_id, (admin,) = await _make_org_with_members(["admin"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/organization/members/{admin.id}",
                json={"role": "member"},
            )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_remove_last_admin"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_remove_member_not_in_org_returns_404():
    org_id, (admin,) = await _make_org_with_members(["admin"])
    _, (other_org_admin,) = await _make_org_with_members(["admin"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete(
                f"/api/v1/organization/members/{other_org_admin.id}"
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_member_can_list_roster():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(member.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/organization/members")
        assert r.status_code == 200
        roles = {row["user_id"]: row["role"] for row in r.json()}
        assert roles[admin.id] == "admin"
        assert roles[member.id] == "member"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
