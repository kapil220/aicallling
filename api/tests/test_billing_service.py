"""Tests for the local billing engine DB client + service.

Uses a real committing session factory (not the savepoint fixture) so row-locking
and idempotency behave as in production. All created organizations are deleted on
teardown to keep the test DB clean.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel
from api.services.billing import billing_service
from api.services.billing.pricing import RateResult


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

    async def make_org(provider_id: str, balance_cents: int = 0) -> int:
        async with session_factory() as session:
            org = OrganizationModel(
                provider_id=provider_id, credit_balance_cents=balance_cents
            )
            session.add(org)
            await session.commit()
            await session.refresh(org)
            created_org_ids.append(org.id)
            return org.id

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


# --- Pure rounding logic (no DB) -------------------------------------------------


@pytest.mark.parametrize(
    "seconds,rate,expected",
    [
        (0, 6000, 0),
        (1, 6000, 100),
        (30, 6000, 3000),
        (59, 6000, 5900),
        (60, 6000, 6000),
        (61, 6000, 6100),
        (90, 6000, 9000),
        (10, 100, 17),  # 10 * 100/60 = 16.67 -> 17
    ],
)
def test_cost_rounding(seconds, rate, expected):
    assert billing_service._cost_cents(seconds, rate) == expected


# --- DB client + service ---------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_ledger_topup_updates_balance(real_db):
    from api.db import db_client

    org_id = await real_db("org_bill_topup")
    await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=500, type="topup", description="seed"
    )
    assert await db_client.get_credit_balance_cents(org_id) == 500


@pytest.mark.asyncio
async def test_apply_ledger_idempotent(real_db):
    from api.db import db_client

    org_id = await real_db("org_bill_idem")
    await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=100, type="topup"
    )
    key = "debit:999"
    a = await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=-100, type="debit", idempotency_key=key
    )
    b = await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=-100, type="debit", idempotency_key=key
    )
    assert a.id == b.id
    assert await db_client.get_credit_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_debit_clamps_at_zero(real_db):
    from api.db import db_client

    org_id = await real_db("org_bill_clamp", balance_cents=50)
    await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=-100, type="debit"
    )
    assert await db_client.get_credit_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_authorize_requires_one_minute_buffer(real_db):
    org_id = await real_db("org_bill_auth")
    await billing_service.credit(org_id, 50, "topup")
    assert (
        await billing_service.authorize(org_id, RateResult(100, 1, "rule")) is False
    )
    await billing_service.credit(org_id, 60, "topup")  # now 110 >= 100
    assert await billing_service.authorize(org_id, RateResult(100, 1, "rule")) is True


@pytest.mark.asyncio
async def test_authorize_fails_closed_when_no_rate(real_db):
    org_id = await real_db("org_bill_norate", balance_cents=100000)
    assert await billing_service.authorize(org_id, RateResult(0, None, "none")) is False


@pytest.mark.asyncio
async def test_max_affordable_seconds(real_db):
    org_id = await real_db("org_bill_afford")
    await billing_service.credit(org_id, 300, "topup")  # $3.00
    # 6000c/min = 100c/s -> 3 seconds affordable
    secs = await billing_service.max_affordable_seconds(
        org_id, RateResult(6000, 1, "rule")
    )
    assert secs == 3


@pytest.mark.asyncio
async def test_debit_for_run_idempotent_and_deducts(real_db):
    org_id = await real_db("org_bill_debit")
    await billing_service.credit(org_id, 10000, "topup")
    await billing_service.debit_for_run(
        organization_id=org_id,
        workflow_run_id=555,
        duration_seconds=90,
        price_per_minute_cents=6000,
    )
    await billing_service.debit_for_run(
        organization_id=org_id,
        workflow_run_id=555,
        duration_seconds=90,
        price_per_minute_cents=6000,
    )
    assert await billing_service.get_balance_cents(org_id) == 1000  # 10000 - 9000, once


# --- Concurrency: serialized debits ----------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_debits_do_not_lose_updates(real_db):
    import asyncio

    org_id = await real_db("org_bill_concurrent")
    await billing_service.credit(org_id, 10000, "topup")

    # 10 concurrent debits of distinct runs, 60s @ 100c/min = 100c each => 1000c total
    await asyncio.gather(
        *[
            billing_service.debit_for_run(
                organization_id=org_id,
                workflow_run_id=1000 + i,
                duration_seconds=60,
                price_per_minute_cents=100,
            )
            for i in range(10)
        ]
    )
    assert await billing_service.get_balance_cents(org_id) == 9000


# --- End-to-end lifecycle: authorize -> settle -----------------------------------


@pytest.mark.asyncio
async def test_authorize_then_settle_reduces_balance(real_db):
    from types import SimpleNamespace

    from api.db import db_client

    org_id = await real_db("org_bill_lifecycle")
    await billing_service.credit(org_id, 10000, "topup")
    await db_client.create_pricing_rule(
        organization_id=org_id,
        mode="pipeline",
        llm_provider="openai",
        stt_provider="deepgram",
        tts_provider="elevenlabs",
        price_per_minute_cents=6000,
        priority=0,
    )
    cfg = SimpleNamespace(
        is_realtime=False,
        llm=SimpleNamespace(provider="openai"),
        stt=SimpleNamespace(provider="deepgram"),
        tts=SimpleNamespace(provider="elevenlabs"),
    )
    rate = await billing_service.resolve_rate_for(org_id, cfg)
    assert rate.price_per_minute_cents == 6000
    assert await billing_service.authorize(org_id, rate) is True

    await billing_service.debit_for_run(
        organization_id=org_id,
        workflow_run_id=42,
        duration_seconds=30,
        price_per_minute_cents=rate.price_per_minute_cents,
    )
    assert await billing_service.get_balance_cents(org_id) == 7000  # 10000 - 3000
