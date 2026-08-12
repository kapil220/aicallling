"""Credentials are ciphertext in the database and plaintext through the ORM.

These are the tests that catch a column somebody forgot to wire: every
assertion reads the raw JSON with SQL, bypassing ``EncryptedJSON`` entirely,
and compares it against what the client layer returns.
"""

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import constants
from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.db.organization_configuration_client import SECRET_CONFIG_KEYS
from api.enums import OrganizationConfigurationKey
from api.services.security import credential_crypto

TWILIO_CREDENTIALS = {
    "account_sid": "AC_test_sid",
    "auth_token": "the-carrier-auth-token",
}


@pytest_asyncio.fixture
async def encrypted_db(setup_test_database, monkeypatch):
    """A DB-backed client with encryption switched on for the duration."""
    monkeypatch.setattr(
        constants, "CREDENTIAL_ENCRYPTION_KEYS", [Fernet.generate_key().decode()]
    )
    credential_crypto.reset_cipher_cache()

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker

    async def raw(table: str, column: str, row_id: int):
        """Read a column as the database actually holds it."""
        async with maker() as s:
            result = await s.execute(
                text(f"SELECT {column} FROM {table} WHERE id = :id"), {"id": row_id}
            )
            return result.scalar_one()

    async def make_org() -> int:
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_crypt_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            return org.id

    async def make_user(org_id: int) -> int:
        async with maker() as s:
            user = UserModel(
                provider_id=f"user_crypt_{uuid.uuid4().hex[:10]}",
                selected_organization_id=org_id,
            )
            s.add(user)
            await s.commit()
            await s.refresh(user)
            return user.id

    harness = SimpleNamespace(
        raw=raw, make_org=make_org, make_user=make_user, session_maker=maker
    )

    try:
        yield harness
    finally:
        db_client.engine, db_client.async_session = orig_engine, orig_maker
        await engine.dispose()
        credential_crypto.reset_cipher_cache()


async def test_telephony_credentials_are_ciphertext_at_rest(encrypted_db):
    org_id = await encrypted_db.make_org()

    config = await db_client.create_telephony_configuration(
        organization_id=org_id,
        name="Twilio prod",
        provider="twilio",
        credentials=TWILIO_CREDENTIALS,
    )

    stored = await encrypted_db.raw(
        "telephony_configurations", "credentials", config.id
    )
    assert credential_crypto.is_encrypted(stored)
    assert "the-carrier-auth-token" not in str(stored)

    fetched = await db_client.get_telephony_configuration(config.id)
    assert fetched.credentials == TWILIO_CREDENTIALS


async def test_account_id_is_denormalized_so_inbound_can_still_match(encrypted_db):
    """The regression that would take phase 3 platform numbers down."""
    org_id = await encrypted_db.make_org()

    config = await db_client.create_telephony_configuration(
        organization_id=org_id,
        name="Twilio prod",
        provider="twilio",
        credentials=TWILIO_CREDENTIALS,
    )
    await db_client.create_phone_number(
        organization_id=org_id,
        telephony_configuration_id=config.id,
        address="+14155550123",
    )

    # account_id is readable in SQL even though credentials is not.
    account_id = await encrypted_db.raw(
        "telephony_configurations", "account_id", config.id
    )
    assert account_id == "AC_test_sid"

    match = await db_client.find_inbound_route_by_account(
        provider="twilio",
        account_id_field="account_sid",
        account_id="AC_test_sid",
        to_number="+14155550123",
    )
    assert match is not None
    matched_config, matched_number = match
    assert matched_config.id == config.id
    assert matched_number.address_normalized == "+14155550123"
    # And the credentials came back decrypted on the way out.
    assert matched_config.credentials == TWILIO_CREDENTIALS


async def test_updating_credentials_refreshes_the_denormalized_account_id(
    encrypted_db,
):
    org_id = await encrypted_db.make_org()
    config = await db_client.create_telephony_configuration(
        organization_id=org_id,
        name="Twilio prod",
        provider="twilio",
        credentials=TWILIO_CREDENTIALS,
    )

    await db_client.update_telephony_configuration(
        config_id=config.id,
        organization_id=org_id,
        credentials={"account_sid": "AC_rotated", "auth_token": "new-token"},
    )

    assert (
        await encrypted_db.raw("telephony_configurations", "account_id", config.id)
        == "AC_rotated"
    )


async def test_webhook_credential_core_update_encrypts(encrypted_db):
    """update_credential writes through Core, which ORM event hooks never see."""
    org_id = await encrypted_db.make_org()
    user_id = await encrypted_db.make_user(org_id)

    credential = await db_client.create_credential(
        organization_id=org_id,
        user_id=user_id,
        name="Webhook key",
        credential_type="api_key",
        credential_data={"header_name": "X-API-Key", "api_key": "initial-secret"},
    )
    assert credential_crypto.is_encrypted(
        await encrypted_db.raw("external_credentials", "credential_data", credential.id)
    )

    updated = await db_client.update_credential(
        credential_uuid=credential.credential_uuid,
        organization_id=org_id,
        credential_data={"header_name": "X-API-Key", "api_key": "rotated-secret"},
    )

    stored = await encrypted_db.raw(
        "external_credentials", "credential_data", credential.id
    )
    assert credential_crypto.is_encrypted(stored)
    assert "rotated-secret" not in str(stored)
    assert updated.credential_data["api_key"] == "rotated-secret"


async def test_secret_org_configuration_keys_are_encrypted(encrypted_db):
    org_id = await encrypted_db.make_org()
    value = {"providers": {"openai": {"api_key": "platform-openai-key"}}}

    config = await db_client.upsert_configuration(
        organization_id=org_id,
        key=OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
        value=value,
    )

    stored = await encrypted_db.raw("organization_configurations", "value", config.id)
    assert credential_crypto.is_encrypted(stored)
    assert "platform-openai-key" not in str(stored)

    # Both the upsert return value and a later read give back plaintext.
    assert config.value == value
    assert (
        await db_client.get_configuration_value(
            org_id, OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
        )
        == value
    )


async def test_non_secret_org_configuration_keys_stay_readable(encrypted_db):
    """Operational config must not become an opaque blob."""
    org_id = await encrypted_db.make_org()
    key = OrganizationConfigurationKey.ORGANIZATION_PREFERENCES.value
    assert key not in SECRET_CONFIG_KEYS

    value = {"timezone": "Asia/Kolkata"}
    config = await db_client.upsert_configuration(
        organization_id=org_id, key=key, value=value
    )

    stored = await encrypted_db.raw("organization_configurations", "value", config.id)
    assert stored == value
    assert not credential_crypto.is_encrypted(stored)


async def test_a_row_written_before_encryption_still_reads(encrypted_db):
    """The mixed-state guarantee the backfill depends on."""
    org_id = await encrypted_db.make_org()
    config = await db_client.create_telephony_configuration(
        organization_id=org_id,
        name="Twilio prod",
        provider="twilio",
        credentials=TWILIO_CREDENTIALS,
    )

    # Overwrite with plaintext, as a pre-phase-4a row would be.
    async with encrypted_db.session_maker() as s:
        await s.execute(
            text(
                "UPDATE telephony_configurations "
                "SET credentials = CAST(:v AS json) WHERE id = :id"
            ),
            {
                "v": '{"account_sid": "AC_legacy", "auth_token": "legacy"}',
                "id": config.id,
            },
        )
        await s.commit()

    fetched = await db_client.get_telephony_configuration(config.id)
    assert fetched.credentials == {
        "account_sid": "AC_legacy",
        "auth_token": "legacy",
    }


async def test_a_key_that_cannot_open_a_row_raises_on_read(encrypted_db, monkeypatch):
    org_id = await encrypted_db.make_org()
    config = await db_client.create_telephony_configuration(
        organization_id=org_id,
        name="Twilio prod",
        provider="twilio",
        credentials=TWILIO_CREDENTIALS,
    )

    monkeypatch.setattr(
        constants, "CREDENTIAL_ENCRYPTION_KEYS", [Fernet.generate_key().decode()]
    )
    credential_crypto.reset_cipher_cache()

    with pytest.raises(credential_crypto.CredentialEncryptionError):
        await db_client.get_telephony_configuration(config.id)
