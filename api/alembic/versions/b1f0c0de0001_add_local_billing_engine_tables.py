"""add local billing engine tables

Revision ID: b1f0c0de0001
Revises: 91cc6ba3e1c7
Create Date: 2026-07-21 03:15:00.000000

Adds the local billing engine schema:
- organizations.credit_balance_cents (cached balance)
- credit_ledger (append-only transactions, source of truth)
- pricing_rules (per-architecture per-minute rates)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f0c0de0001"
down_revision: Union[str, None] = "91cc6ba3e1c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "credit_balance_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("balance_after_cents", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"], ["workflow_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="_credit_ledger_idem_uc"
        ),
    )
    op.create_index(
        "ix_credit_ledger_id", "credit_ledger", ["id"], unique=False
    )
    op.create_index(
        "ix_credit_ledger_org", "credit_ledger", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_credit_ledger_org_created",
        "credit_ledger",
        ["organization_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("llm_provider", sa.String(), nullable=True),
        sa.Column("stt_provider", sa.String(), nullable=True),
        sa.Column("tts_provider", sa.String(), nullable=True),
        sa.Column("realtime_provider", sa.String(), nullable=True),
        sa.Column("price_per_minute_cents", sa.Integer(), nullable=False),
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pricing_rules_id", "pricing_rules", ["id"], unique=False)
    op.create_index(
        "ix_pricing_rules_org", "pricing_rules", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_pricing_rules_active", "pricing_rules", ["is_active"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_pricing_rules_active", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_org", table_name="pricing_rules")
    op.drop_index("ix_pricing_rules_id", table_name="pricing_rules")
    op.drop_table("pricing_rules")

    op.drop_index("ix_credit_ledger_org_created", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_org", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")

    op.drop_column("organizations", "credit_balance_cents")
