"""add role and created_at to organization_users

Revision ID: b2a10c0de0002
Revises: b1f0c0de0001
Create Date: 2026-07-21 04:00:00.000000

Adds org_role (admin/member) and created_at to organization_users, then backfills:
- exactly one admin per org, preferring the earliest member by user_id ordering
  (best-effort — created_at didn't exist on this table before this migration)
- for orgs with <=2 members where "earliest" is ambiguous/unreliable, ALL current
  members are promoted to admin rather than risk locking a real user out
  (false-positive admin access is cheaper to fix than a false-negative lockout at
  this stage — see docs/specs/managed-saas/phase-4-roles-and-admin.md#rollout)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2a10c0de0002"
down_revision: Union[str, None] = "b1f0c0de0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    org_role = sa.Enum("admin", "member", name="org_role")
    org_role.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "organization_users",
        sa.Column("role", org_role, nullable=False, server_default="member"),
    )
    op.add_column(
        "organization_users",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- Backfill: promote exactly one admin per multi-member org, all members
    # for orgs with <=2 members (see module docstring for rationale). ---
    conn = op.get_bind()
    org_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT DISTINCT organization_id FROM organization_users")
        )
    ]
    for org_id in org_ids:
        member_rows = conn.execute(
            sa.text(
                "SELECT user_id FROM organization_users "
                "WHERE organization_id = :org_id ORDER BY user_id ASC"
            ),
            {"org_id": org_id},
        ).fetchall()
        member_ids = [r[0] for r in member_rows]
        if not member_ids:
            continue
        promote_ids = member_ids if len(member_ids) <= 2 else [member_ids[0]]
        conn.execute(
            sa.text(
                "UPDATE organization_users SET role = 'admin' "
                "WHERE organization_id = :org_id AND user_id = ANY(:user_ids)"
            ),
            {"org_id": org_id, "user_ids": promote_ids},
        )


def downgrade() -> None:
    op.drop_column("organization_users", "created_at")
    op.drop_column("organization_users", "role")
    sa.Enum(name="org_role").drop(op.get_bind(), checkfirst=True)
