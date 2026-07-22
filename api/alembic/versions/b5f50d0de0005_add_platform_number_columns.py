"""add platform number columns

Revision ID: b5f50d0de0005
Revises: b4e40d0de0004
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5f50d0de0005"
down_revision: Union[str, None] = "b4e40d0de0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telephony_configurations",
        sa.Column(
            "is_platform", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "telephony_phone_numbers",
        sa.Column(
            "is_platform", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("provider_sid", sa.String(), nullable=True)
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("rental_cents", sa.Integer(), nullable=True)
    )
    op.add_column(
        "telephony_phone_numbers",
        sa.Column("next_renewal_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("rental_status", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_telephony_phone_numbers_platform_renewal",
        "telephony_phone_numbers",
        ["is_platform", "next_renewal_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telephony_phone_numbers_platform_renewal",
        table_name="telephony_phone_numbers",
    )
    op.drop_column("telephony_phone_numbers", "rental_status")
    op.drop_column("telephony_phone_numbers", "next_renewal_at")
    op.drop_column("telephony_phone_numbers", "rental_cents")
    op.drop_column("telephony_phone_numbers", "provider_sid")
    op.drop_column("telephony_phone_numbers", "is_platform")
    op.drop_column("telephony_configurations", "is_platform")
