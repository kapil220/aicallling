"""Requires pgvector Postgres (docker-compose-local)."""
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


async def _org_with_member_and_admin():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_cred_{id(object())}")
        s.add(org)
        await s.flush()
        member = UserModel(provider_id=f"cred_member_{id(object())}")
        admin = UserModel(provider_id=f"cred_admin_{id(object())}")
        s.add_all([member, admin])
        await s.flush()
        await s.commit()
        for u in (member, admin):
            await s.refresh(u)
        await s.refresh(org)

    await db_client.add_user_to_organization(member.id, org.id, role="member")
    await db_client.add_user_to_organization(admin.id, org.id, role="admin")
    return org.id, member, admin


@pytest.mark.asyncio
async def test_member_cannot_delete_credential():
    org_id, member, _admin = await _org_with_member_and_admin()
    app.dependency_overrides[get_user_with_selected_organization] = lambda: type(
        "U", (), {"id": member.id, "selected_organization_id": org_id, "is_superuser": False}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete("/api/v1/credentials/nonexistent-uuid")
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
