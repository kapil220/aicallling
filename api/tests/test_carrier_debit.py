"""Carrier burn debit for platform-number calls (phase 3)."""

import uuid
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services import workflow_run_billing
from api.services.billing import billing_service


class _FakeRun:
    def __init__(self, run_id, config_id):
        self.id = run_id
        self.is_completed = True
        self.cost_info = {"price_per_minute_cents": 100}
        self.usage_info = {"call_duration_seconds": 120.0}
        self.initial_context = {"telephony_configuration_id": config_id}


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []
    workflow_ids: list[int] = []

    async def make_org(balance_cents: int, platform_config: bool):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_carrier_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
        await billing_service.credit(org.id, balance_cents, "topup")
        async with maker() as s:
            from api.db.models import WorkflowModel, WorkflowRunModel

            workflow = WorkflowModel(name="carrier-test", organization_id=org.id)
            s.add(workflow)
            await s.flush()
            run = WorkflowRunModel(
                name="carrier-run", workflow_id=workflow.id, mode="twilio"
            )
            s.add(run)
            await s.commit()
            await s.refresh(run)
            workflow_ids.append(workflow.id)
        if platform_config:
            config = await db_client.create_platform_telephony_configuration(
                org.id, {"account_sid": "AC_c", "auth_token": "t", "from_numbers": []}
            )
        else:
            config = await db_client.create_telephony_configuration(
                org.id,
                name="byo",
                provider="twilio",
                credentials={"account_sid": "AC_byo", "auth_token": "t"},
            )
        return org, config, run.id

    yield make_org
    from api.db.models import WorkflowModel, WorkflowRunModel

    async with maker() as s:
        await s.execute(
            delete(WorkflowRunModel).where(
                WorkflowRunModel.workflow_id.in_(workflow_ids)
            )
        )
        await s.execute(delete(WorkflowModel).where(WorkflowModel.id.in_(workflow_ids)))
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_platform_call_gets_carrier_debit(real_db):
    org, config, run_id = await real_db(10000, platform_config=True)
    run = _FakeRun(run_id, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    # AI: 120s @ 100c/min = 200; carrier: 120s @ 50c/min = 100
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200 - 100
    # replay-safe
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200 - 100


async def test_byo_call_no_carrier_debit(real_db):
    org, config, run_id = await real_db(10000, platform_config=False)
    run = _FakeRun(run_id, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200


async def test_oss_mode_no_carrier_debit(real_db):
    org, config, run_id = await real_db(10000, platform_config=True)
    run = _FakeRun(run_id, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", False):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200


async def test_missing_config_id_no_carrier_debit(real_db):
    org, config, run_id = await real_db(10000, platform_config=True)
    run = _FakeRun(run_id, config.id)
    run.initial_context = {}
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200
