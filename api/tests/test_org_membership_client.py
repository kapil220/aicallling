"""DB-backed tests for OrgMembershipClient (role CRUD + last-admin guard).

Uses a real committing session factory so the row-locked guard behaves as in
production. Created orgs/users are removed on teardown. Requires the project's
pgvector-enabled Postgres (docker-compose-local).
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel, OrganizationUserModel, UserModel
from api.db.org_membership_client import LastAdminError


@pytest.fixture(scope="module")
async def real_db(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    created_org_ids: list[int] = []
    created_user_ids: list[int] = []

    async def make_org_with_members(n=1):
        async with session_factory() as s:
            org = OrganizationModel(provider_id=f"org_mem_{id(object())}")
            s.add(org)
            await s.flush()
            users = []
            for i in range(n):
                u = UserModel(provider_id=f"user_mem_{id(object())}_{i}")
                s.add(u)
                users.append(u)
            await s.flush()
            await s.commit()
            for u in users:
                await s.refresh(u)
            await s.refresh(org)
            created_org_ids.append(org.id)
            created_user_ids.extend(u.id for u in users)
            return org.id, [u.id for u in users]

    yield make_org_with_members

    async with session_factory() as s:
        if created_org_ids:
            await s.execute(
                delete(OrganizationUserModel).where(
                    OrganizationUserModel.organization_id.in_(created_org_ids)
                )
            )
            await s.execute(
                delete(OrganizationModel).where(
                    OrganizationModel.id.in_(created_org_ids)
                )
            )
        if created_user_ids:
            await s.execute(
                delete(UserModel).where(UserModel.id.in_(created_user_ids))
            )
        await s.commit()

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_member_defaults_to_member_role(real_db):
    from api.db import db_client

    org_id, (user_id,) = await real_db(1)
    await db_client.add_user_to_organization(user_id, org_id)
    assert await db_client.get_member_role(org_id, user_id) == "member"


@pytest.mark.asyncio
async def test_add_user_to_organization_with_explicit_admin_role(real_db):
    from api.db import db_client

    org_id, (user_id,) = await real_db(1)
    await db_client.add_user_to_organization(user_id, org_id, role="admin")
    assert await db_client.get_member_role(org_id, user_id) == "admin"
    assert await db_client.count_org_admins(org_id) == 1


@pytest.mark.asyncio
async def test_upsert_member_role_idempotent(real_db):
    from api.db import db_client

    org_id, (user_id,) = await real_db(1)
    await db_client.add_user_to_organization(user_id, org_id, role="member")
    await db_client.upsert_member_role(org_id, user_id, "admin")
    await db_client.upsert_member_role(org_id, user_id, "admin")
    assert await db_client.get_member_role(org_id, user_id) == "admin"


@pytest.mark.asyncio
async def test_add_user_to_organization_does_not_demote_existing_admin(real_db):
    """Re-adding an existing member (e.g. a concurrent lazy-provision call
    racing against itself on first login) must never downgrade their role —
    this is the default (non-overwrite) upsert semantics used by auth call
    sites, as opposed to the explicit re-invite path."""
    from api.db import db_client

    org_id, (admin_id,) = await real_db(1)
    await db_client.add_user_to_organization(admin_id, org_id, role="admin")
    # Simulate a second concurrent request re-provisioning the same user with
    # a "member" role (mirrors the org_was_created=False branch racing the
    # org_was_created=True branch for the same creator).
    await db_client.add_user_to_organization(admin_id, org_id, role="member")
    assert await db_client.get_member_role(org_id, admin_id) == "admin"


@pytest.mark.asyncio
async def test_add_user_to_organization_overwrite_role_changes_existing_member(
    real_db,
):
    """The explicit overwrite path (re-invite) should still be able to change
    an existing member's role."""
    from api.db import db_client

    org_id, (user_id,) = await real_db(1)
    await db_client.add_user_to_organization(user_id, org_id, role="member")
    await db_client.add_user_to_organization(
        user_id, org_id, role="admin", overwrite_role=True
    )
    assert await db_client.get_member_role(org_id, user_id) == "admin"


@pytest.mark.asyncio
async def test_last_admin_cannot_be_demoted(real_db):
    from api.db import db_client

    org_id, (admin_id,) = await real_db(1)
    await db_client.add_user_to_organization(admin_id, org_id, role="admin")
    with pytest.raises(LastAdminError):
        await db_client.set_member_role(org_id, admin_id, "member")


@pytest.mark.asyncio
async def test_demote_allowed_when_another_admin_remains(real_db):
    from api.db import db_client

    org_id, (admin_1, admin_2) = await real_db(2)
    await db_client.add_user_to_organization(admin_1, org_id, role="admin")
    await db_client.add_user_to_organization(admin_2, org_id, role="admin")
    await db_client.set_member_role(org_id, admin_1, "member")
    assert await db_client.get_member_role(org_id, admin_1) == "member"
    assert await db_client.count_org_admins(org_id) == 1


@pytest.mark.asyncio
async def test_last_admin_cannot_be_removed(real_db):
    from api.db import db_client

    org_id, (admin_id,) = await real_db(1)
    await db_client.add_user_to_organization(admin_id, org_id, role="admin")
    with pytest.raises(LastAdminError):
        await db_client.remove_member(org_id, admin_id)


@pytest.mark.asyncio
async def test_list_org_members_returns_role_and_email(real_db):
    from api.db import db_client

    org_id, (user_id,) = await real_db(1)
    await db_client.add_user_to_organization(user_id, org_id, role="admin")
    members = await db_client.list_org_members(org_id)
    assert len(members) == 1
    assert members[0]["user_id"] == user_id
    assert members[0]["role"] == "admin"
    assert "created_at" in members[0]
