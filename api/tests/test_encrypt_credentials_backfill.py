"""The phase 4a backfill script: idempotent, resumable, reversible."""

import uuid
from types import SimpleNamespace

import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import constants
from api.constants import DATABASE_URL
from api.db.models import OrganizationModel
from api.enums import OrganizationConfigurationKey
from api.services.security import credential_crypto
from scripts import encrypt_credentials

LEGACY_CREDENTIALS = '{"account_sid": "AC_legacy", "auth_token": "legacy-token"}'


@pytest_asyncio.fixture
async def backfill_db(setup_test_database, monkeypatch):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(
        constants, "CREDENTIAL_ENCRYPTION_KEYS", [Fernet.generate_key().decode()]
    )
    credential_crypto.reset_cipher_cache()

    # The script converts whole tables, so these tests need the telephony table
    # to contain only their own rows: each case gets a fresh key, and rows left
    # behind by another case were encrypted under a key this one cannot open.
    async with maker() as s:
        await s.execute(text("DELETE FROM telephony_phone_numbers"))
        await s.execute(text("DELETE FROM telephony_configurations"))
        await s.commit()

    async def make_plaintext_config() -> int:
        """A telephony config written the way a pre-phase-4a row was."""
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_bf_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)

            result = await s.execute(
                text(
                    "INSERT INTO telephony_configurations "
                    "(organization_id, name, provider, credentials) "
                    "VALUES (:org, 'legacy', 'twilio', CAST(:creds AS json)) "
                    "RETURNING id"
                ),
                {"org": org.id, "creds": LEGACY_CREDENTIALS},
            )
            row_id = result.scalar_one()
            await s.commit()
            return row_id

    async def raw(table: str, column: str, row_id: int):
        async with maker() as s:
            result = await s.execute(
                text(f"SELECT {column} FROM {table} WHERE id = :id"), {"id": row_id}
            )
            return result.scalar_one()

    async def run(**kwargs):
        return await encrypt_credentials._process_table(
            (engine, maker),
            "telephony_configurations",
            apply_changes=kwargs.get("apply", False),
            decrypt=kwargs.get("decrypt", False),
            batch_size=kwargs.get("batch_size", 500),
        )

    try:
        yield SimpleNamespace(
            make_plaintext_config=make_plaintext_config, raw=raw, run=run, maker=maker
        )
    finally:
        await engine.dispose()
        credential_crypto.reset_cipher_cache()


async def test_dry_run_reports_without_writing(backfill_db):
    row_id = await backfill_db.make_plaintext_config()

    eligible, converted, already = await backfill_db.run()

    assert eligible >= 1
    assert converted >= 1
    assert already == 0
    stored = await backfill_db.raw("telephony_configurations", "credentials", row_id)
    assert not credential_crypto.is_encrypted(stored)


async def test_apply_encrypts_and_is_idempotent(backfill_db):
    row_id = await backfill_db.make_plaintext_config()

    await backfill_db.run(apply=True)

    stored = await backfill_db.raw("telephony_configurations", "credentials", row_id)
    assert credential_crypto.is_encrypted(stored)
    assert "legacy-token" not in stored
    assert credential_crypto.decrypt_json(stored) == {
        "account_sid": "AC_legacy",
        "auth_token": "legacy-token",
    }

    # A second run — the resume-after-interruption case — converts nothing.
    eligible, converted, already = await backfill_db.run(apply=True)
    assert converted == 0
    assert already == eligible


async def test_a_partially_converted_table_finishes_on_the_next_run(backfill_db):
    first = await backfill_db.make_plaintext_config()
    await backfill_db.run(apply=True)
    second = await backfill_db.make_plaintext_config()

    # Mixed state: one ciphertext row, one plaintext row.
    _, converted, _ = await backfill_db.run(apply=True)

    assert converted == 1
    for row_id in (first, second):
        stored = await backfill_db.raw(
            "telephony_configurations", "credentials", row_id
        )
        assert credential_crypto.is_encrypted(stored)


async def test_decrypt_is_a_working_escape_hatch(backfill_db):
    row_id = await backfill_db.make_plaintext_config()
    await backfill_db.run(apply=True)

    await backfill_db.run(apply=True, decrypt=True)

    stored = await backfill_db.raw("telephony_configurations", "credentials", row_id)
    assert not credential_crypto.is_encrypted(stored)
    assert stored == {"account_sid": "AC_legacy", "auth_token": "legacy-token"}


async def test_verify_exits_nonzero_while_plaintext_remains(backfill_db):
    await backfill_db.make_plaintext_config()

    assert await encrypt_credentials._verify((None, backfill_db.maker)) == 1

    await backfill_db.run(apply=True)
    # Other tables may hold plaintext rows from sibling tests; assert on the
    # table this test owns rather than the global exit code.
    async with backfill_db.maker() as s:
        result = await s.execute(
            text(
                "SELECT count(*) FROM telephony_configurations "
                "WHERE json_typeof(credentials) = 'object'"
            )
        )
        assert result.scalar_one() == 0


async def test_non_secret_config_keys_are_left_alone(backfill_db):
    """The backfill must not turn operational config into opaque blobs."""
    async with backfill_db.maker() as s:
        org = OrganizationModel(
            provider_id=f"org_bf_{uuid.uuid4().hex[:10]}", credit_balance_cents=0
        )
        s.add(org)
        await s.commit()
        await s.refresh(org)
        result = await s.execute(
            text(
                "INSERT INTO organization_configurations "
                "(organization_id, key, value) "
                "VALUES (:org, :key, CAST(:v AS json)) RETURNING id"
            ),
            {
                "org": org.id,
                "key": OrganizationConfigurationKey.ORGANIZATION_PREFERENCES.value,
                "v": '{"timezone": "Asia/Kolkata"}',
            },
        )
        row_id = result.scalar_one()
        await s.commit()

    await encrypt_credentials._process_table(
        (None, backfill_db.maker),
        "organization_configurations",
        apply_changes=True,
        decrypt=False,
        batch_size=500,
    )

    stored = await backfill_db.raw("organization_configurations", "value", row_id)
    assert stored == {"timezone": "Asia/Kolkata"}
