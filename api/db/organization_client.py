from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import (
    APIKeyModel,
    OrganizationModel,
    organization_users_association,
)
from api.utils.api_key import generate_api_key


class OrganizationClient(BaseDBClient):
    async def get_organization_by_id(
        self, organization_id: int
    ) -> Optional[OrganizationModel]:
        """Get an organization by its ID."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(OrganizationModel.id == organization_id)
            )
            return result.scalars().first()

    async def get_organization_by_provider_id(
        self, org_provider_id: str
    ) -> Optional[OrganizationModel]:
        """Look up an organization by provider_id without creating one."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.provider_id == org_provider_id
                )
            )
            return result.scalars().first()

    async def list_organizations(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[OrganizationModel], int]:
        """Paginated org listing for the superuser backoffice."""
        async with self.async_session() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(OrganizationModel)
                )
            ).scalar_one()
            result = await session.execute(
                select(OrganizationModel)
                .order_by(OrganizationModel.id.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), int(total)

    async def get_or_create_organization_by_provider_id(
        self, org_provider_id: str, user_id: int
    ) -> tuple[OrganizationModel, bool]:
        """Get an existing organization by provider_id or create a new one.

        Returns:
            A tuple of (organization, was_created) where was_created is True if the organization
            was created in this call, False if it already existed.
        """
        async with self.async_session() as session:
            # First try to get existing organization
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.provider_id == org_provider_id
                )
            )
            organization = result.scalars().first()

            if organization is None:
                # Use PostgreSQL's INSERT ... ON CONFLICT DO NOTHING
                # This is atomic and handles race conditions at the database level

                stmt = insert(OrganizationModel.__table__).values(
                    provider_id=org_provider_id, created_at=datetime.now(timezone.utc)
                )
                # ON CONFLICT DO NOTHING - if another request already inserted, this becomes a no-op
                stmt = stmt.on_conflict_do_nothing(index_elements=["provider_id"])

                result = await session.execute(stmt)
                await session.commit()

                # Check if we actually inserted (rowcount > 0) or if there was a conflict (rowcount == 0)
                was_created = result.rowcount > 0

                # Now fetch the organization (either the one we just created or the one that existed)
                result = await session.execute(
                    select(OrganizationModel).where(
                        OrganizationModel.provider_id == org_provider_id
                    )
                )
                organization = result.scalars().first()

                if organization is None:
                    # This should never happen, but handle it just in case
                    error_msg = f"Failed to create or fetch organization with provider_id {org_provider_id}"
                    raise ValueError(error_msg)

                # Only create API key if we actually created the organization
                if was_created:
                    # Create a default API key for the new organization
                    _, key_hash, key_prefix = generate_api_key()

                    api_key = APIKeyModel(
                        organization_id=organization.id,
                        name="Default API Key",
                        key_hash=key_hash,
                        key_prefix=key_prefix,
                        is_active=True,
                        created_by=user_id,
                    )
                    session.add(api_key)
                    await session.commit()

                await session.refresh(organization)
                return organization, was_created
            return organization, False

    async def add_user_to_organization(
        self,
        user_id: int,
        organization_id: int,
        role: str = "member",
        overwrite_role: bool = False,
    ) -> None:
        """Ensure that a user is linked to an organization (many-to-many).

        Idempotent by default: re-adding an existing member is a no-op on the
        role (``ON CONFLICT DO NOTHING``) so lazy re-provisioning on every
        login (auth.py, services/auth/depends.py) can never demote an
        existing admin — e.g. two concurrent first-login requests for the same
        newly-created org must not race each other's role assignment.

        Pass ``overwrite_role=True`` for call sites that intentionally want to
        change an existing member's role on re-add (e.g. re-inviting a member
        with a different role); this keeps the previous "upsert role"
        behavior for those explicit call sites only.
        """
        async with self.async_session() as session:
            stmt = insert(organization_users_association).values(
                user_id=user_id, organization_id=organization_id, role=role
            )
            if overwrite_role:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_id", "organization_id"],
                    set_={"role": stmt.excluded.role},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["user_id", "organization_id"],
                )

            await session.execute(stmt)
            await session.commit()
