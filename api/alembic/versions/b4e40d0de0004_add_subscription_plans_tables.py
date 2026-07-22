"""add subscription plans tables

Revision ID: b4e40d0de0004
Revises: b3c20d0de0003
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4e40d0de0004"
down_revision: Union[str, None] = "b3c20d0de0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tier_key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="inr"),
        sa.Column("included_minutes", sa.Integer(), nullable=False),
        sa.Column("max_agents", sa.Integer(), nullable=True),
        sa.Column(
            "max_concurrent_calls", sa.Integer(), nullable=False, server_default="2"
        ),
        sa.Column("daily_call_cap", sa.Integer(), nullable=True),
        sa.Column("max_active_campaigns", sa.Integer(), nullable=True),
        sa.Column("razorpay_plan_id", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tier_key", name="_plans_tier_key_uc"),
    )
    op.create_index("ix_plans_active", "plans", ["is_active"])

    op.create_table(
        "subscription_invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(), nullable=False),
        sa.Column("razorpay_subscription_id", sa.String(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="inr"),
        sa.Column("status", sa.String(), nullable=False, server_default="captured"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("razorpay_payment_id", name="_sub_invoice_payment_uc"),
    )
    op.create_index(
        "ix_subscription_invoices_org", "subscription_invoices", ["organization_id"]
    )

    op.add_column("organizations", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_organizations_plan_id",
        "organizations",
        "plans",
        ["plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "organizations", sa.Column("razorpay_subscription_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_organizations_razorpay_subscription_id",
        "organizations",
        ["razorpay_subscription_id"],
        unique=True,
    )
    op.add_column(
        "organizations", sa.Column("subscription_status", sa.String(), nullable=True)
    )
    op.add_column(
        "organizations",
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
    )

    plans = sa.table(
        "plans",
        sa.column("tier_key", sa.String),
        sa.column("display_name", sa.String),
        sa.column("price_cents", sa.Integer),
        sa.column("currency", sa.String),
        sa.column("included_minutes", sa.Integer),
        sa.column("max_agents", sa.Integer),
        sa.column("max_concurrent_calls", sa.Integer),
        sa.column("daily_call_cap", sa.Integer),
        sa.column("max_active_campaigns", sa.Integer),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        plans,
        [
            {
                "tier_key": "starter",
                "display_name": "Starter",
                "price_cents": 149900,
                "currency": "inr",
                "included_minutes": 300,
                "max_agents": 3,
                "max_concurrent_calls": 2,
                "daily_call_cap": 200,
                "max_active_campaigns": 1,
                "sort_order": 0,
            },
            {
                "tier_key": "pro",
                "display_name": "Pro",
                "price_cents": 599900,
                "currency": "inr",
                "included_minutes": 1500,
                "max_agents": 15,
                "max_concurrent_calls": 10,
                "daily_call_cap": 1000,
                "max_active_campaigns": 5,
                "sort_order": 1,
            },
            {
                "tier_key": "scale",
                "display_name": "Scale",
                "price_cents": 1999900,
                "currency": "inr",
                "included_minutes": 6000,
                "max_agents": None,
                "max_concurrent_calls": 25,
                "daily_call_cap": None,
                "max_active_campaigns": None,
                "sort_order": 2,
            },
        ],
    )


def downgrade() -> None:
    op.drop_column("organizations", "current_period_end")
    op.drop_column("organizations", "subscription_status")
    op.drop_index("ix_organizations_razorpay_subscription_id", table_name="organizations")
    op.drop_column("organizations", "razorpay_subscription_id")
    op.drop_constraint("fk_organizations_plan_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "plan_id")
    op.drop_index("ix_subscription_invoices_org", table_name="subscription_invoices")
    op.drop_table("subscription_invoices")
    op.drop_index("ix_plans_active", table_name="plans")
    op.drop_table("plans")
