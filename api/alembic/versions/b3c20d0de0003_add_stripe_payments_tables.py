"""add stripe payment packs and payments tables

Revision ID: b3c20d0de0003
Revises: b2a10c0de0002
Create Date: 2026-07-21 04:30:00.000000

Adds the Phase 3 payment rail:
- organizations.stripe_customer_id (lazily created Stripe customer)
- payment_packs (prepaid credit-pack catalog)
- payments (Stripe checkout audit trail, joins a Stripe event to a ledger row)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c20d0de0003"
down_revision: Union[str, None] = "b2a10c0de0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_organizations_stripe_customer_id",
        "organizations",
        ["stripe_customer_id"],
        unique=True,
    )

    op.create_table(
        "payment_packs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pack_key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column(
            "currency", sa.String(), nullable=False, server_default=sa.text("'usd'")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pack_key"),
    )
    op.create_index("ix_payment_packs_id", "payment_packs", ["id"], unique=False)
    op.create_index(
        "ix_payment_packs_pack_key", "payment_packs", ["pack_key"], unique=True
    )
    op.create_index(
        "ix_payment_packs_active", "payment_packs", ["is_active"], unique=False
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("payment_pack_id", sa.Integer(), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(), nullable=False),
        sa.Column("stripe_payment_intent_id", sa.String(), nullable=True),
        sa.Column("stripe_customer_id", sa.String(), nullable=False),
        sa.Column("amount_cents_paid", sa.Integer(), nullable=False),
        sa.Column(
            "currency", sa.String(), nullable=False, server_default=sa.text("'usd'")
        ),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("credit_ledger_id", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["payment_pack_id"], ["payment_packs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["credit_ledger_id"], ["credit_ledger.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_checkout_session_id"),
        sa.UniqueConstraint("stripe_payment_intent_id"),
    )
    op.create_index("ix_payments_id", "payments", ["id"], unique=False)
    op.create_index(
        "ix_payments_checkout_session",
        "payments",
        ["stripe_checkout_session_id"],
        unique=True,
    )
    op.create_index(
        "ix_payments_payment_intent",
        "payments",
        ["stripe_payment_intent_id"],
        unique=True,
    )
    op.create_index(
        "ix_payments_customer", "payments", ["stripe_customer_id"], unique=False
    )
    op.create_index(
        "ix_payments_organization_id", "payments", ["organization_id"], unique=False
    )
    op.create_index("ix_payments_status", "payments", ["status"], unique=False)


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_index("ix_payment_packs_active", table_name="payment_packs")
    op.drop_index("ix_payment_packs_pack_key", table_name="payment_packs")
    op.drop_index("ix_payment_packs_id", table_name="payment_packs")
    op.drop_table("payment_packs")
    op.drop_index("ix_organizations_stripe_customer_id", table_name="organizations")
    op.drop_column("organizations", "stripe_customer_id")
