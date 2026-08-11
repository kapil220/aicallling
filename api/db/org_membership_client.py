"""Org membership role operations: lookups, listing, role changes, removal.

The last-admin guard (an org must always keep >=1 admin) is enforced inside a
transaction with a row lock on the org's membership rows (`SELECT ... FOR UPDATE`)
so a concurrent demote/remove can't race two admins down to zero.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationUserModel, UserModel


class LastAdminError(Exception):
    """Raised when an operation would leave an org with zero admins."""


class OrgMembershipClient(BaseDBClient):
    async def get_member_role(self, organization_id: int, user_id: int) -> str | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationUserModel.role).where(
                    OrganizationUserModel.organization_id == organization_id,
                    OrganizationUserModel.user_id == user_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_org_members(self, organization_id: int) -> list[dict]:
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    OrganizationUserModel.user_id,
                    UserModel.email,
                    OrganizationUserModel.role,
                    OrganizationUserModel.created_at,
                )
                .join(UserModel, UserModel.id == OrganizationUserModel.user_id)
                .where(OrganizationUserModel.organization_id == organization_id)
                .order_by(OrganizationUserModel.created_at.asc())
            )
            return [
                {
                    "user_id": r.user_id,
                    "email": r.email,
                    "role": r.role,
                    "created_at": r.created_at,
                }
                for r in result.all()
            ]

    async def count_org_admins(self, organization_id: int) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count()).where(
                    OrganizationUserModel.organization_id == organization_id,
                    OrganizationUserModel.role == "admin",
                )
            )
            return int(result.scalar_one())

    async def upsert_member_role(
        self, organization_id: int, user_id: int, role: str
    ) -> None:
        async with self.async_session() as session:
            stmt = insert(OrganizationUserModel).values(
                organization_id=organization_id, user_id=user_id, role=role
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "organization_id"],
                set_={"role": stmt.excluded.role},
            )
            await session.execute(stmt)
            await session.commit()

    async def set_member_role(
        self, organization_id: int, user_id: int, role: str
    ) -> None:
        """Change a member's role. Rejects demoting the org's last admin."""
        async with self.async_session() as session:
            async with session.begin():
                rows = await session.execute(
                    select(OrganizationUserModel)
                    .where(OrganizationUserModel.organization_id == organization_id)
                    .with_for_update()
                )
                memberships = rows.scalars().all()
                target = next((m for m in memberships if m.user_id == user_id), None)
                if target is None:
                    raise ValueError("not_a_member")

                admin_count = sum(1 for m in memberships if m.role == "admin")
                if target.role == "admin" and role != "admin" and admin_count <= 1:
                    raise LastAdminError(
                        f"cannot demote the only admin of org {organization_id}"
                    )
                target.role = role
                await session.flush()

    async def remove_member(self, organization_id: int, user_id: int) -> None:
        """Remove a membership row. Rejects removing the org's last admin."""
        async with self.async_session() as session:
            async with session.begin():
                rows = await session.execute(
                    select(OrganizationUserModel)
                    .where(OrganizationUserModel.organization_id == organization_id)
                    .with_for_update()
                )
                memberships = rows.scalars().all()
                target = next((m for m in memberships if m.user_id == user_id), None)
                if target is None:
                    raise ValueError("not_a_member")

                admin_count = sum(1 for m in memberships if m.role == "admin")
                if target.role == "admin" and admin_count <= 1:
                    raise LastAdminError(
                        f"cannot remove the only admin of org {organization_id}"
                    )
                await session.delete(target)
