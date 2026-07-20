"""Requires pgvector Postgres (docker-compose-local)."""
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_superuser
from api.services.billing import billing_service


@pytest.fixture
def superuser_override():
    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    yield
    app.dependency_overrides.pop(get_superuser, None)


async def _org_with_admin(balance_cents=0):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(
            provider_id=f"org_su_{id(object())}", credit_balance_cents=0
        )
        s.add(org)
        await s.flush()
        admin = UserModel(provider_id=f"su_admin_{id(object())}")
        s.add(admin)
        await s.flush()
        await s.commit()
        await s.refresh(admin)
        await s.refresh(org)

    await db_client.add_user_to_organization(admin.id, org.id, role="admin")
    if balance_cents:
        await billing_service.credit(org.id, balance_cents, "topup")
    return org.id, admin.id


@pytest.mark.asyncio
async def test_list_orgs_shows_balance_and_admin_count(superuser_override):
    org_id, _admin_id = await _org_with_admin(balance_cents=1500)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/superuser/orgs?limit=200")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["organizations"]}
    assert rows[org_id]["credit_balance_cents"] == 1500
    assert rows[org_id]["admin_count"] == 1
    assert rows[org_id]["member_count"] == 1


@pytest.mark.asyncio
async def test_org_detail_lists_members(superuser_override):
    org_id, admin_id = await _org_with_admin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get(f"/api/v1/superuser/orgs/{org_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == org_id
    assert body["members"][0]["user_id"] == admin_id
    assert body["members"][0]["role"] == "admin"


@pytest.mark.asyncio
async def test_role_override_repairs_zero_admin_org(superuser_override):
    org_id, admin_id = await _org_with_admin()
    from api.db.database import async_session

    async with async_session() as s:
        u2 = UserModel(provider_id=f"su_member_{id(object())}")
        s.add(u2)
        await s.commit()
        await s.refresh(u2)
    await db_client.add_user_to_organization(u2.id, org_id, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            f"/api/v1/superuser/orgs/{org_id}/members/{u2.id}/role",
            json={"role": "admin"},
        )
    assert r.status_code == 200
    assert await db_client.get_member_role(org_id, u2.id) == "admin"
    assert await db_client.count_org_admins(org_id) == 2
