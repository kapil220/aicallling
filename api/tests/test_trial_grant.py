"""Tests for the one-time signup trial grant service.

Uses a real committing session factory (not the savepoint fixture) so idempotency
behaves as in production. All created organizations are deleted on teardown to keep
the test DB clean.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel
from api.services.billing import billing_service


@pytest.fixture(scope="module")
async def real_db(setup_test_database):
    """Patch db_client to a real committing session factory for this module."""
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    created_org_ids: list[int] = []

    async def make_org(provider_id: str, balance_cents: int = 0) -> OrganizationModel:
        async with session_factory() as session:
            org = OrganizationModel(
                provider_id=provider_id, credit_balance_cents=balance_cents
            )
            session.add(org)
            await session.commit()
            await session.refresh(org)
            created_org_ids.append(org.id)
            return org

    yield make_org

    # Cleanup: cascade deletes ledger + pricing rows via FK ondelete=CASCADE.
    async with session_factory() as session:
        if created_org_ids:
            await session.execute(
                delete(OrganizationModel).where(
                    OrganizationModel.id.in_(created_org_ids)
                )
            )
            await session.commit()

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@pytest.fixture
def local_billing(monkeypatch):
    monkeypatch.setattr("api.services.billing.trial.BILLING_ENGINE", "local")
    monkeypatch.setattr("api.services.billing.trial.TRIAL_MINUTES", 15)


@pytest.mark.asyncio
async def test_trial_grant_credits_once(real_db, local_billing):
    from api.services.billing.trial import grant_signup_trial

    org = await real_db("org_trial_test")
    await grant_signup_trial(org.id)
    await grant_signup_trial(org.id)  # idempotent — second call is a no-op

    assert await billing_service.get_balance_cents(org.id) == 1500


@pytest.mark.asyncio
async def test_trial_grant_disabled_when_zero(real_db, local_billing, monkeypatch):
    from api.services.billing.trial import grant_signup_trial

    monkeypatch.setattr("api.services.billing.trial.TRIAL_MINUTES", 0)
    org = await real_db("org_trial_zero")
    await grant_signup_trial(org.id)
    assert await billing_service.get_balance_cents(org.id) == 0


@pytest.mark.asyncio
async def test_trial_grant_noop_on_mps_engine(real_db, local_billing, monkeypatch):
    from api.services.billing.trial import grant_signup_trial

    monkeypatch.setattr("api.services.billing.trial.BILLING_ENGINE", "mps")
    org = await real_db("org_trial_mps")
    await grant_signup_trial(org.id)
    assert await billing_service.get_balance_cents(org.id) == 0
