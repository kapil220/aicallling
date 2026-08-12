"""denormalize telephony account_id off the credentials blob

Phase 4a step 2. Inbound dispatch resolves the owning organization by matching
``credentials->>'<provider account field>'`` in SQL on every call. Once
``credentials`` is encrypted that read returns nothing, so the identifier moves
into its own column first, while the blob is still plaintext.

An account id is an identifier, not a secret — the auth token stored beside it
is — so promoting it to a column is not a weakening.

Revision ID: b6f60d0de0006
Revises: b5f50d0de0005
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6f60d0de0006"
down_revision: Union[str, None] = "b5f50d0de0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors ``ProviderSpec.account_id_credential_field`` for every provider that
# has one. Spelled out here rather than imported: a migration must keep
# describing the data as it was when it ran, even after a provider's spec
# changes or the provider package is removed. Providers with no account concept
# (e.g. ARI) are absent and keep a NULL account_id.
_PROVIDER_ACCOUNT_FIELDS = {
    "twilio": "account_sid",
    "plivo": "auth_id",
    "vobiz": "auth_id",
    "telnyx": "connection_id",
    "vonage": "api_key",
    "cloudonix": "domain_id",
}


def upgrade() -> None:
    op.add_column(
        "telephony_configurations",
        sa.Column("account_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_telephony_configurations_account_id",
        "telephony_configurations",
        ["account_id"],
    )

    # Backfill from the still-plaintext blob. The jsonb_typeof guard skips rows
    # that are already ciphertext (stored as a JSON string, not an object), so
    # re-running this after a partial encryption pass cannot fail.
    for provider, field in _PROVIDER_ACCOUNT_FIELDS.items():
        op.execute(
            sa.text(
                """
                UPDATE telephony_configurations
                   SET account_id = credentials::jsonb ->> :field
                 WHERE provider = :provider
                   AND jsonb_typeof(credentials::jsonb) = 'object'
                   AND credentials::jsonb ->> :field IS NOT NULL
                   AND credentials::jsonb ->> :field <> ''
                """
            ).bindparams(field=field, provider=provider)
        )


def downgrade() -> None:
    op.drop_index(
        "ix_telephony_configurations_account_id",
        table_name="telephony_configurations",
    )
    op.drop_column("telephony_configurations", "account_id")
