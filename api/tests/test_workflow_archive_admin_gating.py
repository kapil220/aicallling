"""Requires pgvector Postgres (docker-compose-local)."""
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


@pytest.mark.asyncio
async def test_member_cannot_archive_workflow():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_wf_{id(object())}")
        s.add(org)
        await s.flush()
        member = UserModel(provider_id=f"wf_member_{id(object())}")
        s.add(member)
        await s.flush()
        await s.commit()
        await s.refresh(member)
        await s.refresh(org)

    await db_client.add_user_to_organization(member.id, org.id, role="member")

    app.dependency_overrides[get_user_with_selected_organization] = lambda: type(
        "U", (), {"id": member.id, "selected_organization_id": org.id, "is_superuser": False}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.put(
                "/api/v1/workflow/999999/status", json={"status": "archived"}
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
