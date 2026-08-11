# SaaS Phase 2 — Plans, Razorpay Subscriptions, Limit Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subscription plans with included monthly minutes on the existing credit ledger, sold via Razorpay Subscriptions behind a `PaymentProvider` abstraction, with tier-scaled limit enforcement (max agents, concurrency, daily call cap, max active campaigns) and a reworked billing UI.

**Architecture:** A new `plans` catalog table + subscription fields on `OrganizationModel`. Razorpay webhooks drive the subscription lifecycle; on every successful charge the previous period's ledger balance is expired (`plan_period_reset`) and the new allowance granted (`plan_renewal`), both idempotent on the Razorpay event id via the existing `_credit_ledger_idem_uc` unique constraint. Limits are read from the org's plan (trial defaults when unsubscribed) by a new `plan_limits` service and enforced at existing choke points (workflow create, campaign start, quota authorize, campaign dispatcher). All enforcement is gated on `IS_SAAS_MODE` — OSS behavior untouched.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, `razorpay` Python SDK, pytest (existing `real_db` committing-fixture pattern), Next.js 15 + shadcn/ui + `@hey-api/openapi-ts` generated client.

**Spec:** `docs/superpowers/specs/2026-07-21-saas-platform-design.md` §4, §5, §6, §9 (phase 2 of §13 build order).

## Global Constraints

- `DEPLOYMENT_MODE=saas` behavior only; **OSS mode must remain byte-for-byte unchanged in behavior** (every new enforcement path is gated on `IS_SAAS_MODE` from `api/constants.py`).
- 1 minute = 100 ledger units (cents) at 1× burn (`CENTS_PER_MINUTE = 100` in `api/services/billing/trial.py:9`).
- Ledger writes go through `billing_service.credit(...)` / `db_client.apply_ledger_entry(...)` only — never touch `credit_ledger` rows directly.
- New ledger `type` values: `plan_period_reset`, `plan_renewal`. Idempotency keys: `razorpay:{event_id}:reset` and `razorpay:{event_id}:renewal`.
- Launch placeholder tiers (superadmin-tunable): Starter 300 min / 3 agents / 2 concurrent / 1 campaign; Pro 1,500 min / 15 agents / 10 concurrent / 5 campaigns; Scale 6,000 min / unlimited agents / 25 concurrent / unlimited campaigns. Prices INR placeholders: ₹1,499 / ₹5,999 / ₹19,999 (`price_cents` 149900 / 599900 / 1999900, currency `inr`). `NULL` limit = unlimited.
- No rollover: on renewal, remaining balance is zeroed before the new grant.
- Unpriced/unresolvable states fail closed (calls blocked), matching the existing `billing_service.authorize` posture.
- Razorpay webhook signature verification is mandatory; unverified requests → 400.
- Tests run with: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/<file> -v` from the repo root.
- Commit after every task (message prefix `feat(saas-p2):`, `fix:`, `test:` as appropriate). Never `--no-verify`.
- After backend route changes that should reach the UI, regenerate the client: `cd ui && npm run generate-client` (requires the API running locally serving `/api/v1/openapi.json`).

**Decisions locked during planning** (owner can veto before execution):
1. Plan-change v1 = `POST /billing/change-plan` cancels the current Razorpay subscription immediately and returns a new checkout URL; remaining minutes expire on the new plan's first charge (via the normal reset+grant). True in-place `subscription.update` is deferred.
2. Payment history for subscriptions = new `subscription_invoices` table written from `subscription.charged` webhooks (the Stripe `payments` table is Stripe-shaped and stays dormant).
3. "Active campaign" for the max-active-campaigns limit = state in (`syncing`, `running`). Paused campaigns don't hold a slot (resume re-checks).
4. Daily call cap counts workflow runs created since UTC midnight (org-timezone day windows deferred).
5. Trial-org defaults (env-tunable): `TRIAL_MAX_AGENTS=3`, `TRIAL_MAX_CONCURRENT_CALLS=2`, `TRIAL_DAILY_CALL_CAP=20`, `TRIAL_MAX_ACTIVE_CAMPAIGNS=1`.
6. Email upgrade-prompt on exhaustion is deferred to Phase 4 (no email infra exists); in-app prompts ship now.

---

### Task 1: Plans + subscription schema (models + migration + seed)

**Files:**
- Modify: `api/db/models.py` (add `PlanModel`, `SubscriptionInvoiceModel`; extend `OrganizationModel` around line 177)
- Create: `api/alembic/versions/b4e40d0de0004_add_subscription_plans_tables.py`
- Test: `api/tests/test_plan_models.py`

**Interfaces:**
- Consumes: existing `OrganizationModel` (`api/db/models.py:119`), migration chain head `b3c20d0de0003`.
- Produces: `PlanModel` (tablename `plans`: `id, tier_key, display_name, price_cents, currency, included_minutes, max_agents, max_concurrent_calls, daily_call_cap, max_active_campaigns, razorpay_plan_id, is_active, sort_order, created_at, updated_at`), `SubscriptionInvoiceModel` (tablename `subscription_invoices`: `id, organization_id, razorpay_payment_id, razorpay_subscription_id, amount_cents, currency, status, created_at`), and new `OrganizationModel` columns `plan_id`, `razorpay_subscription_id`, `subscription_status`, `current_period_end`. Seeded rows for tiers `starter`, `pro`, `scale`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_plan_models.py
"""Schema-level checks for the phase-2 plans tables (migration b4e40d0de0004)."""

from sqlalchemy import select

from api.db import db_client
from api.db.models import OrganizationModel, PlanModel


async def test_seeded_plans_exist(db_session):
    async with db_client.async_session() as s:
        rows = (await s.execute(select(PlanModel).order_by(PlanModel.sort_order))).scalars().all()
    tiers = {p.tier_key: p for p in rows}
    assert {"starter", "pro", "scale"} <= set(tiers)
    assert tiers["starter"].included_minutes == 300
    assert tiers["starter"].max_agents == 3
    assert tiers["pro"].included_minutes == 1500
    assert tiers["pro"].max_concurrent_calls == 10
    assert tiers["scale"].max_agents is None  # NULL = unlimited
    assert tiers["scale"].max_active_campaigns is None
    assert all(p.razorpay_plan_id is None for p in rows)  # linked later via superadmin


async def test_org_subscription_columns_default_null(db_session):
    async with db_client.async_session() as s:
        org = OrganizationModel(provider_id="org_plan_schema_test")
        s.add(org)
        await s.flush()
        assert org.plan_id is None
        assert org.razorpay_subscription_id is None
        assert org.subscription_status is None
        assert org.current_period_end is None
        await s.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlanModel'`.

- [ ] **Step 3: Add models to `api/db/models.py`**

Add after `PricingRuleModel` (ends ~line 295), following its style:

```python
class PlanModel(Base):
    """Subscription plan catalog (saas mode). NULL limit columns mean unlimited."""

    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    tier_key = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    price_cents = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="inr", server_default="inr")
    included_minutes = Column(Integer, nullable=False)
    max_agents = Column(Integer, nullable=True)
    max_concurrent_calls = Column(Integer, nullable=False, default=2, server_default="2")
    daily_call_cap = Column(Integer, nullable=True)
    max_active_campaigns = Column(Integer, nullable=True)
    razorpay_plan_id = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tier_key", name="_plans_tier_key_uc"),
        Index("ix_plans_active", "is_active"),
    )


class SubscriptionInvoiceModel(Base):
    """One row per successful (or failed) Razorpay subscription charge."""

    __tablename__ = "subscription_invoices"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    razorpay_payment_id = Column(String, nullable=False)
    razorpay_subscription_id = Column(String, nullable=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="inr", server_default="inr")
    status = Column(String, nullable=False, default="captured", server_default="captured")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("razorpay_payment_id", name="_sub_invoice_payment_uc"),
        Index("ix_subscription_invoices_org", "organization_id"),
    )
```

Extend `OrganizationModel` right after `stripe_customer_id` (line ~177):

```python
    # Phase 2 (saas): subscription plan linkage. Lifecycle states mirror
    # Razorpay: None (never subscribed / trial) | active | halted | cancelled.
    plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    razorpay_subscription_id = Column(String, unique=True, nullable=True, index=True)
    subscription_status = Column(String, nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Write the migration**

Create `api/alembic/versions/b4e40d0de0004_add_subscription_plans_tables.py` (hand-picked revision id continuing the `b#…de000#` billing series; `down_revision = "b3c20d0de0003"`):

```python
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
        sa.Column("max_concurrent_calls", sa.Integer(), nullable=False, server_default="2"),
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_models.py -v`
Expected: PASS (the session-scoped `setup_test_database` fixture in `api/conftest.py:114` recreates the test DB and runs `alembic upgrade head`, picking up the new migration + seed).

- [ ] **Step 6: Verify the full suite still migrates cleanly**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_service.py api/tests/test_payment_service.py -v`
Expected: PASS (no regressions from the org columns).

- [ ] **Step 7: Commit**

```bash
git add api/db/models.py api/alembic/versions/b4e40d0de0004_add_subscription_plans_tables.py api/tests/test_plan_models.py
git commit -m "feat(saas-p2): plans + subscription schema with seeded launch tiers"
```

---

### Task 2: PlanClient DB methods

**Files:**
- Create: `api/db/plan_client.py`
- Modify: `api/db/db_client.py` (add `PlanClient` to the `DBClient` mixin list at lines 41–42)
- Test: `api/tests/test_plan_client.py`

**Interfaces:**
- Consumes: `PlanModel`, `SubscriptionInvoiceModel`, `OrganizationModel` from Task 1; `BaseDBClient` (`api/db/base_client.py`).
- Produces (all on `db_client`):
  - `list_active_plans() -> list[PlanModel]` (ordered by `sort_order`)
  - `list_all_plans() -> list[PlanModel]`
  - `get_plan_by_tier_key(tier_key: str) -> PlanModel | None`
  - `get_plan_by_razorpay_plan_id(razorpay_plan_id: str) -> PlanModel | None`
  - `get_plan_by_id(plan_id: int) -> PlanModel | None`
  - `create_plan(**fields) -> PlanModel`
  - `update_plan(plan_id: int, **fields) -> PlanModel | None`
  - `get_org_by_razorpay_subscription_id(subscription_id: str) -> OrganizationModel | None`
  - `update_org_subscription(organization_id: int, *, plan_id=..., razorpay_subscription_id=..., subscription_status=..., current_period_end=...) -> None` (only provided kwargs are updated; use a `_UNSET` sentinel)
  - `record_subscription_invoice(*, organization_id, razorpay_payment_id, razorpay_subscription_id, amount_cents, currency, status) -> SubscriptionInvoiceModel | None` (idempotent: returns existing row on duplicate `razorpay_payment_id`)
  - `list_subscription_invoices(organization_id: int, *, limit: int = 50) -> list[SubscriptionInvoiceModel]`

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_plan_client.py
"""PlanClient CRUD + subscription-field updates, real-DB pattern from test_payment_service.py."""

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, PlanModel, SubscriptionInvoiceModel


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str) -> OrganizationModel:
        async with maker() as s:
            org = OrganizationModel(provider_id=provider_id)
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org

    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.execute(delete(PlanModel).where(PlanModel.tier_key.like("t2_%")))
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_plan_crud_roundtrip(real_db):
    plan = await db_client.create_plan(
        tier_key="t2_custom",
        display_name="Custom",
        price_cents=100000,
        currency="inr",
        included_minutes=100,
        max_agents=5,
        max_concurrent_calls=3,
        daily_call_cap=50,
        max_active_campaigns=2,
    )
    assert (await db_client.get_plan_by_tier_key("t2_custom")).id == plan.id
    updated = await db_client.update_plan(plan.id, razorpay_plan_id="plan_rzp_123")
    assert updated.razorpay_plan_id == "plan_rzp_123"
    assert (await db_client.get_plan_by_razorpay_plan_id("plan_rzp_123")).id == plan.id
    seeded = await db_client.list_active_plans()
    assert [p.tier_key for p in seeded[:3]] == ["starter", "pro", "scale"]


async def test_update_org_subscription_partial(real_db):
    org = await real_db("org_plan_client_1")
    starter = await db_client.get_plan_by_tier_key("starter")
    await db_client.update_org_subscription(
        org.id,
        plan_id=starter.id,
        razorpay_subscription_id="sub_abc",
        subscription_status="active",
    )
    found = await db_client.get_org_by_razorpay_subscription_id("sub_abc")
    assert found.id == org.id
    assert found.subscription_status == "active"
    # Partial update must not clobber the other fields
    await db_client.update_org_subscription(org.id, subscription_status="halted")
    found = await db_client.get_org_by_razorpay_subscription_id("sub_abc")
    assert found.subscription_status == "halted"
    assert found.plan_id == starter.id


async def test_record_subscription_invoice_idempotent(real_db):
    org = await real_db("org_plan_client_2")
    first = await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id="pay_dup",
        razorpay_subscription_id="sub_abc2",
        amount_cents=149900,
        currency="inr",
        status="captured",
    )
    second = await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id="pay_dup",
        razorpay_subscription_id="sub_abc2",
        amount_cents=149900,
        currency="inr",
        status="captured",
    )
    assert first.id == second.id
    invoices = await db_client.list_subscription_invoices(org.id)
    assert len(invoices) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_client.py -v`
Expected: FAIL — `AttributeError: 'DBClient' object has no attribute 'create_plan'`.

- [ ] **Step 3: Implement `api/db/plan_client.py`**

```python
"""DB access for subscription plans + org subscription state (saas phase 2)."""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, PlanModel, SubscriptionInvoiceModel

_UNSET = object()


class PlanClient(BaseDBClient):
    async def list_active_plans(self) -> list[PlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PlanModel)
                .where(PlanModel.is_active.is_(True))
                .order_by(PlanModel.sort_order, PlanModel.id)
            )
            return list(result.scalars().all())

    async def list_all_plans(self) -> list[PlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PlanModel).order_by(PlanModel.sort_order, PlanModel.id)
            )
            return list(result.scalars().all())

    async def get_plan_by_tier_key(self, tier_key: str) -> Optional[PlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PlanModel).where(PlanModel.tier_key == tier_key)
            )
            return result.scalar_one_or_none()

    async def get_plan_by_id(self, plan_id: int) -> Optional[PlanModel]:
        async with self.async_session() as session:
            return await session.get(PlanModel, plan_id)

    async def get_plan_by_razorpay_plan_id(
        self, razorpay_plan_id: str
    ) -> Optional[PlanModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PlanModel).where(PlanModel.razorpay_plan_id == razorpay_plan_id)
            )
            return result.scalar_one_or_none()

    async def create_plan(self, **fields) -> PlanModel:
        async with self.async_session() as session:
            plan = PlanModel(**fields)
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return plan

    async def update_plan(self, plan_id: int, **fields) -> Optional[PlanModel]:
        async with self.async_session() as session:
            plan = await session.get(PlanModel, plan_id)
            if plan is None:
                return None
            for key, value in fields.items():
                setattr(plan, key, value)
            await session.commit()
            await session.refresh(plan)
            return plan

    async def get_org_by_razorpay_subscription_id(
        self, subscription_id: str
    ) -> Optional[OrganizationModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.razorpay_subscription_id == subscription_id
                )
            )
            return result.scalar_one_or_none()

    async def update_org_subscription(
        self,
        organization_id: int,
        *,
        plan_id=_UNSET,
        razorpay_subscription_id=_UNSET,
        subscription_status=_UNSET,
        current_period_end=_UNSET,
    ) -> None:
        async with self.async_session() as session:
            org = await session.get(OrganizationModel, organization_id)
            if org is None:
                return
            if plan_id is not _UNSET:
                org.plan_id = plan_id
            if razorpay_subscription_id is not _UNSET:
                org.razorpay_subscription_id = razorpay_subscription_id
            if subscription_status is not _UNSET:
                org.subscription_status = subscription_status
            if current_period_end is not _UNSET:
                org.current_period_end = current_period_end
            await session.commit()

    async def record_subscription_invoice(
        self,
        *,
        organization_id: int,
        razorpay_payment_id: str,
        razorpay_subscription_id: Optional[str],
        amount_cents: int,
        currency: str,
        status: str,
    ) -> Optional[SubscriptionInvoiceModel]:
        async with self.async_session() as session:
            invoice = SubscriptionInvoiceModel(
                organization_id=organization_id,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_subscription_id=razorpay_subscription_id,
                amount_cents=amount_cents,
                currency=currency,
                status=status,
            )
            session.add(invoice)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                result = await session.execute(
                    select(SubscriptionInvoiceModel).where(
                        SubscriptionInvoiceModel.razorpay_payment_id
                        == razorpay_payment_id
                    )
                )
                return result.scalar_one_or_none()
            await session.refresh(invoice)
            return invoice

    async def list_subscription_invoices(
        self, organization_id: int, *, limit: int = 50
    ) -> list[SubscriptionInvoiceModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(SubscriptionInvoiceModel)
                .where(SubscriptionInvoiceModel.organization_id == organization_id)
                .order_by(SubscriptionInvoiceModel.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
```

In `api/db/db_client.py`, import `PlanClient` and add it to the `DBClient(...)` mixin bases next to `BillingClient`/`PaymentClient` (lines 41–42).

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/db/plan_client.py api/db/db_client.py api/tests/test_plan_client.py
git commit -m "feat(saas-p2): PlanClient db methods for plans + org subscription state"
```

---

### Task 3: Superadmin plans CRUD routes

**Files:**
- Modify: `api/routes/billing_admin.py` (append plan routes; follow the `payment-packs` pattern at lines 104–121)
- Test: `api/tests/test_billing_admin_plans.py`

**Interfaces:**
- Consumes: `db_client.list_all_plans / create_plan / update_plan` (Task 2), `get_superuser` dependency (already imported in `billing_admin.py`).
- Produces: `GET /api/v1/superuser/plans`, `POST /api/v1/superuser/plans`, `PATCH /api/v1/superuser/plans/{plan_id}` with `PlanRequest` / `PlanUpdateRequest` / `PlanAdminResponse` Pydantic models.

- [ ] **Step 1: Write the failing test**

Follow `api/tests/test_superuser_orgs_routes.py` for the superuser route-test pattern (it shows how a test client + superuser auth override is built — reuse its fixtures/helpers verbatim; `test_client_factory` lives at `api/conftest.py:328`).

```python
# api/tests/test_billing_admin_plans.py
"""Superadmin plans CRUD — mirrors the payment-packs admin surface."""
# Reuse the client/superuser fixtures exactly as test_superuser_orgs_routes.py does.


async def test_list_plans_includes_seeded(superuser_client):
    resp = await superuser_client.get("/api/v1/superuser/plans")
    assert resp.status_code == 200
    tiers = [p["tier_key"] for p in resp.json()]
    assert {"starter", "pro", "scale"} <= set(tiers)


async def test_create_and_update_plan(superuser_client):
    resp = await superuser_client.post(
        "/api/v1/superuser/plans",
        json={
            "tier_key": "t3_biz",
            "display_name": "Business",
            "price_cents": 999900,
            "currency": "inr",
            "included_minutes": 3000,
            "max_agents": 30,
            "max_concurrent_calls": 15,
            "daily_call_cap": 2000,
            "max_active_campaigns": 10,
        },
    )
    assert resp.status_code == 200
    plan_id = resp.json()["id"]

    resp = await superuser_client.patch(
        f"/api/v1/superuser/plans/{plan_id}",
        json={"razorpay_plan_id": "plan_live_xyz", "is_active": False},
    )
    assert resp.status_code == 200
    assert resp.json()["razorpay_plan_id"] == "plan_live_xyz"
    assert resp.json()["is_active"] is False


async def test_plans_require_superuser(regular_user_client):
    resp = await regular_user_client.get("/api/v1/superuser/plans")
    assert resp.status_code in (401, 403)
```

(If `test_superuser_orgs_routes.py` names its fixtures differently, use its names — the assertion bodies above are the contract.)

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_plans.py -v`
Expected: FAIL — 404 on `/api/v1/superuser/plans`.

- [ ] **Step 3: Implement routes in `api/routes/billing_admin.py`**

```python
class PlanRequest(BaseModel):
    tier_key: str
    display_name: str
    price_cents: int
    currency: str = "inr"
    included_minutes: int
    max_agents: int | None = None
    max_concurrent_calls: int = 2
    daily_call_cap: int | None = None
    max_active_campaigns: int | None = None
    razorpay_plan_id: str | None = None
    is_active: bool = True
    sort_order: int = 0


class PlanUpdateRequest(BaseModel):
    display_name: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    included_minutes: int | None = None
    max_agents: int | None = None
    max_concurrent_calls: int | None = None
    daily_call_cap: int | None = None
    max_active_campaigns: int | None = None
    razorpay_plan_id: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class PlanAdminResponse(BaseModel):
    id: int
    tier_key: str
    display_name: str
    price_cents: int
    currency: str
    included_minutes: int
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None
    razorpay_plan_id: str | None
    is_active: bool
    sort_order: int


def _plan_admin_response(plan) -> PlanAdminResponse:
    return PlanAdminResponse(
        id=plan.id,
        tier_key=plan.tier_key,
        display_name=plan.display_name,
        price_cents=plan.price_cents,
        currency=plan.currency,
        included_minutes=plan.included_minutes,
        max_agents=plan.max_agents,
        max_concurrent_calls=plan.max_concurrent_calls,
        daily_call_cap=plan.daily_call_cap,
        max_active_campaigns=plan.max_active_campaigns,
        razorpay_plan_id=plan.razorpay_plan_id,
        is_active=plan.is_active,
        sort_order=plan.sort_order,
    )


@router.get("/plans", response_model=list[PlanAdminResponse])
async def list_plans_admin(user=Depends(get_superuser)):
    return [_plan_admin_response(p) for p in await db_client.list_all_plans()]


@router.post("/plans", response_model=PlanAdminResponse)
async def create_plan_admin(body: PlanRequest, user=Depends(get_superuser)):
    plan = await db_client.create_plan(**body.model_dump())
    return _plan_admin_response(plan)


@router.patch("/plans/{plan_id}", response_model=PlanAdminResponse)
async def update_plan_admin(
    plan_id: int, body: PlanUpdateRequest, user=Depends(get_superuser)
):
    fields = body.model_dump(exclude_unset=True)
    plan = await db_client.update_plan(plan_id, **fields)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")
    return _plan_admin_response(plan)
```

(Match the existing imports in `billing_admin.py`; add `HTTPException` to the fastapi import if missing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_plans.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/routes/billing_admin.py api/tests/test_billing_admin_plans.py
git commit -m "feat(saas-p2): superadmin plans CRUD"
```

---

### Task 4: `plan_limits` service + trial-default constants

**Files:**
- Modify: `api/constants.py` (append near `TRIAL_MINUTES`, line 46)
- Create: `api/services/billing/plan_limits.py`
- Test: `api/tests/test_plan_limits.py`

**Interfaces:**
- Consumes: `db_client.get_plan_by_id`, `OrganizationModel.plan_id` (Tasks 1–2); `IS_SAAS_MODE` (`api/constants.py:35`).
- Produces:
  - Constants: `TRIAL_MAX_AGENTS` (default 3), `TRIAL_MAX_CONCURRENT_CALLS` (default 2), `TRIAL_DAILY_CALL_CAP` (default 20), `TRIAL_MAX_ACTIVE_CAMPAIGNS` (default 1).
  - `@dataclass(frozen=True) PlanLimits: max_agents: int | None; max_concurrent_calls: int; daily_call_cap: int | None; max_active_campaigns: int | None` (None = unlimited).
  - `async get_org_limits(organization_id: int) -> PlanLimits` — org's plan limits, or trial defaults when `plan_id is None`.
  - `def enforcement_enabled() -> bool` — `IS_SAAS_MODE` (single gate every enforcement site uses).
  - `UPGRADE_PROMPT = "Upgrade your plan at /billing to raise this limit."` (appended to all limit errors).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_plan_limits.py
"""plan_limits: plan-driven limits with trial fallbacks."""

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import plan_limits


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str, plan_id: int | None = None):
        async with maker() as s:
            org = OrganizationModel(provider_id=provider_id, plan_id=plan_id)
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org
    async with maker() as s:
        await s.execute(delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids)))
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_trial_org_gets_trial_defaults(real_db):
    org = await real_db("org_limits_trial")
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents == 3
    assert limits.max_concurrent_calls == 2
    assert limits.daily_call_cap == 20
    assert limits.max_active_campaigns == 1


async def test_subscribed_org_gets_plan_limits(real_db):
    pro = await db_client.get_plan_by_tier_key("pro")
    org = await real_db("org_limits_pro", plan_id=pro.id)
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents == 15
    assert limits.max_concurrent_calls == 10
    assert limits.max_active_campaigns == 5


async def test_unlimited_is_none(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_limits_scale", plan_id=scale.id)
    limits = await plan_limits.get_org_limits(org.id)
    assert limits.max_agents is None
    assert limits.daily_call_cap is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_limits.py -v`
Expected: FAIL — `ImportError` on `plan_limits`.

- [ ] **Step 3: Implement**

Append to `api/constants.py` directly under `TRIAL_MINUTES` (line 46):

```python
# Phase 2 (saas): limits applied to orgs with no active plan (trial). NULL/unset
# plan columns mean unlimited; these trial values are intentionally small.
TRIAL_MAX_AGENTS = int(os.getenv("TRIAL_MAX_AGENTS", "3"))
TRIAL_MAX_CONCURRENT_CALLS = int(os.getenv("TRIAL_MAX_CONCURRENT_CALLS", "2"))
TRIAL_DAILY_CALL_CAP = int(os.getenv("TRIAL_DAILY_CALL_CAP", "20"))
TRIAL_MAX_ACTIVE_CAMPAIGNS = int(os.getenv("TRIAL_MAX_ACTIVE_CAMPAIGNS", "1"))
```

Create `api/services/billing/plan_limits.py`:

```python
"""Plan-tier limit resolution (saas phase 2).

Every enforcement site gates on enforcement_enabled() so OSS deployments
are untouched. A limit of None means unlimited.
"""

from dataclasses import dataclass

from api.constants import (
    IS_SAAS_MODE,
    TRIAL_DAILY_CALL_CAP,
    TRIAL_MAX_ACTIVE_CAMPAIGNS,
    TRIAL_MAX_AGENTS,
    TRIAL_MAX_CONCURRENT_CALLS,
)
from api.db import db_client

UPGRADE_PROMPT = "Upgrade your plan at /billing to raise this limit."


@dataclass(frozen=True)
class PlanLimits:
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None


TRIAL_LIMITS = PlanLimits(
    max_agents=TRIAL_MAX_AGENTS,
    max_concurrent_calls=TRIAL_MAX_CONCURRENT_CALLS,
    daily_call_cap=TRIAL_DAILY_CALL_CAP,
    max_active_campaigns=TRIAL_MAX_ACTIVE_CAMPAIGNS,
)


def enforcement_enabled() -> bool:
    return IS_SAAS_MODE


async def get_org_limits(organization_id: int) -> PlanLimits:
    org = await db_client.get_organization_by_id(organization_id)
    if org is None or org.plan_id is None:
        return TRIAL_LIMITS
    plan = await db_client.get_plan_by_id(org.plan_id)
    if plan is None:
        return TRIAL_LIMITS
    return PlanLimits(
        max_agents=plan.max_agents,
        max_concurrent_calls=plan.max_concurrent_calls,
        daily_call_cap=plan.daily_call_cap,
        max_active_campaigns=plan.max_active_campaigns,
    )
```

Note: if `db_client.get_organization_by_id` doesn't exist under that name, grep `api/db/organization_client.py` for the actual single-org getter and use it (there is one — the superuser org-detail route at `api/routes/superuser.py:241` uses it).

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_limits.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/constants.py api/services/billing/plan_limits.py api/tests/test_plan_limits.py
git commit -m "feat(saas-p2): plan_limits service with trial-default fallbacks"
```

---

### Task 5: Enforce max agents + max active campaigns

**Files:**
- Modify: `api/routes/workflow.py` (`create_workflow` at line 371 — insert before `db_client.create_workflow` call at line ~401; `create_workflow_from_template` at line 442 — same insertion)
- Modify: `api/routes/campaign.py` (`start_campaign` at line 530 and `resume_campaign` at line 855 — insert before their existing `authorize_workflow_run_start` calls at lines 553/878)
- Modify: `api/db/campaign_client.py` (add `count_active_campaigns`; find the class by grepping `class CampaignClient`)
- Test: `api/tests/test_plan_limit_enforcement.py`

**Interfaces:**
- Consumes: `plan_limits.get_org_limits / enforcement_enabled / UPGRADE_PROMPT` (Task 4); `db_client.get_workflow_counts(organization_id)` (`api/db/workflow_client.py:389`, returns `{"total","active","archived"}`); `CampaignModel.state` (`api/db/models.py:872`).
- Produces:
  - `db_client.count_active_campaigns(organization_id: int) -> int` — count of campaigns whose `state` is in `("syncing", "running")` for the org.
  - HTTP 402 `{"detail": "agent_limit_reached: ..."}` from workflow create; HTTP 402 `{"detail": "campaign_limit_reached: ..."}` from campaign start/resume.

- [ ] **Step 1: Write the failing test**

Test the check helpers directly (route-level auth plumbing is already covered by existing route tests; keep these unit-level against the db + service):

```python
# api/tests/test_plan_limit_enforcement.py
"""Max-agents and max-active-campaigns checks (saas phase 2)."""

from unittest.mock import patch

# reuse the real_db fixture pattern from test_plan_limits.py (copy it here,
# module-scoped, with make_org)

from api.db import db_client
from api.services.billing import plan_limits


async def test_agent_limit_blocks_at_cap(real_db):
    org = await real_db("org_enf_agents")  # trial → max_agents = 3
    with patch.object(
        db_client, "get_workflow_counts", return_value={"total": 3, "active": 3, "archived": 0}
    ):
        err = await plan_limits.check_can_create_agent(org.id)
    assert err is not None
    assert "agent" in err.lower()


async def test_agent_limit_allows_under_cap(real_db):
    org = await real_db("org_enf_agents_ok")
    with patch.object(
        db_client, "get_workflow_counts", return_value={"total": 2, "active": 2, "archived": 0}
    ):
        assert await plan_limits.check_can_create_agent(org.id) is None


async def test_agent_limit_unlimited_for_scale(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_enf_scale", plan_id=scale.id)
    with patch.object(
        db_client, "get_workflow_counts", return_value={"total": 999, "active": 999, "archived": 0}
    ):
        assert await plan_limits.check_can_create_agent(org.id) is None


async def test_campaign_limit_blocks_at_cap(real_db):
    org = await real_db("org_enf_campaigns")  # trial → max_active_campaigns = 1
    with patch.object(db_client, "count_active_campaigns", return_value=1):
        err = await plan_limits.check_can_start_campaign(org.id)
    assert err is not None


async def test_oss_mode_never_blocks(real_db):
    org = await real_db("org_enf_oss")
    with patch.object(plan_limits, "enforcement_enabled", return_value=False):
        assert await plan_limits.check_can_create_agent(org.id) is None
        assert await plan_limits.check_can_start_campaign(org.id) is None
```

(The `patch.object(db_client, ...)` mocks return plain values; since the helpers `await` them, wrap with `AsyncMock(return_value=...)` — `from unittest.mock import AsyncMock; patch.object(db_client, "get_workflow_counts", AsyncMock(return_value=...))`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_limit_enforcement.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'check_can_create_agent'`.

- [ ] **Step 3: Implement checks + wire routes**

Append to `api/services/billing/plan_limits.py`:

```python
async def check_can_create_agent(organization_id: int) -> str | None:
    """Returns an error message when the org is at its agent cap, else None."""
    if not enforcement_enabled():
        return None
    limits = await get_org_limits(organization_id)
    if limits.max_agents is None:
        return None
    counts = await db_client.get_workflow_counts(organization_id=organization_id)
    if counts.get("active", 0) >= limits.max_agents:
        return (
            f"agent_limit_reached: your plan allows {limits.max_agents} active "
            f"agents. {UPGRADE_PROMPT}"
        )
    return None


async def check_can_start_campaign(organization_id: int) -> str | None:
    """Returns an error message when the org is at its active-campaign cap."""
    if not enforcement_enabled():
        return None
    limits = await get_org_limits(organization_id)
    if limits.max_active_campaigns is None:
        return None
    active = await db_client.count_active_campaigns(organization_id)
    if active >= limits.max_active_campaigns:
        return (
            f"campaign_limit_reached: your plan allows {limits.max_active_campaigns} "
            f"active campaigns. {UPGRADE_PROMPT}"
        )
    return None
```

Add to the campaign DB client (same file that owns campaign queries — grep `def create_campaign` under `api/db/`):

```python
async def count_active_campaigns(self, organization_id: int) -> int:
    async with self.async_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(CampaignModel)
            .where(
                CampaignModel.organization_id == organization_id,
                CampaignModel.state.in_(["syncing", "running"]),
            )
        )
        return int(result.scalar_one())
```

(Confirm `CampaignModel` has `organization_id`; if campaigns link to orgs via workflow, join `WorkflowModel` on `CampaignModel.workflow_id` and filter `WorkflowModel.organization_id` instead — check `api/db/models.py:860+`.)

Wire `api/routes/workflow.py` — in `create_workflow` (before the `db_client.create_workflow` call ~line 401) and in `create_workflow_from_template` (same position):

```python
from api.services.billing import plan_limits  # top of file

limit_error = await plan_limits.check_can_create_agent(user.selected_organization_id)
if limit_error:
    raise HTTPException(status_code=402, detail=limit_error)
```

Wire `api/routes/campaign.py` — in `start_campaign` (line 530, before the quota call at 553) and `resume_campaign` (line 855, before 878):

```python
from api.services.billing import plan_limits  # top of file

limit_error = await plan_limits.check_can_start_campaign(user.selected_organization_id)
if limit_error:
    raise HTTPException(status_code=402, detail=limit_error)
```

(Use whatever variable holds the acting user/org in those handlers — both already resolve the org for the quota check; mirror it.)

- [ ] **Step 4: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_limit_enforcement.py api/tests/test_quota_service.py -v`
Expected: PASS (quota tests unchanged — enforcement is off outside saas mode; `.env.test` runs OSS unless it sets saas).

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/plan_limits.py api/routes/workflow.py api/routes/campaign.py api/db/*.py api/tests/test_plan_limit_enforcement.py
git commit -m "feat(saas-p2): enforce max agents and max active campaigns from plan tier"
```

---

### Task 6: Plan-sourced concurrency + daily call cap + campaign auto-pause

**Files:**
- Modify: `api/routes/campaign.py:28` (`_get_org_concurrent_limit`) and `api/services/campaign/campaign_call_dispatcher.py:54` (`get_org_concurrent_limit`) — plan caps the org-config value in saas mode
- Modify: `api/services/quota_service.py` (`_authorize_local_billing` at line 324) — daily-cap check
- Modify: `api/services/campaign/campaign_call_dispatcher.py` (`process_batch` at line 69) — batch pre-check: daily cap + balance → auto-pause
- Modify: `api/db/workflow_run_client.py` (add `count_org_runs_since`)
- Test: `api/tests/test_plan_daily_cap.py`

**Interfaces:**
- Consumes: `plan_limits.get_org_limits / enforcement_enabled`; `campaign_runner_service.pause_campaign` (`api/services/campaign/runner.py:66`); `billing_service.get_balance_cents` (`api/services/billing/billing_service.py:78`); `MINIMUM_CREDIT_CENTS` (`api/constants.py:61`); `QuotaCheckResult` (`api/services/quota_service.py:42`).
- Produces:
  - `db_client.count_org_runs_since(organization_id: int, since: datetime) -> int` — workflow runs joined via `WorkflowModel.organization_id`, `WorkflowRunModel.created_at >= since`.
  - `plan_limits.check_daily_call_cap(organization_id: int) -> str | None`.
  - Quota result `error_code="daily_call_cap_reached"` when capped.
  - Dispatcher pauses (not fails) campaigns on cap/balance exhaustion, logging via the existing `append_campaign_log` used at `api/tasks/campaign_tasks.py:99+`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_plan_daily_cap.py
"""Daily call cap resolution + quota integration (saas phase 2)."""

from unittest.mock import AsyncMock, patch

# copy the module-scoped real_db/make_org fixture from test_plan_limits.py

from api.db import db_client
from api.services.billing import plan_limits


async def test_daily_cap_blocks_at_cap(real_db):
    org = await real_db("org_cap_hit")  # trial → daily_call_cap = 20
    with patch.object(db_client, "count_org_runs_since", AsyncMock(return_value=20)):
        err = await plan_limits.check_daily_call_cap(org.id)
    assert err is not None
    assert "daily" in err.lower()


async def test_daily_cap_allows_under(real_db):
    org = await real_db("org_cap_ok")
    with patch.object(db_client, "count_org_runs_since", AsyncMock(return_value=5)):
        assert await plan_limits.check_daily_call_cap(org.id) is None


async def test_daily_cap_unlimited_scale(real_db):
    scale = await db_client.get_plan_by_tier_key("scale")
    org = await real_db("org_cap_scale", plan_id=scale.id)
    with patch.object(db_client, "count_org_runs_since", AsyncMock(return_value=10_000)):
        assert await plan_limits.check_daily_call_cap(org.id) is None


async def test_count_org_runs_since_real_query(real_db):
    # smoke the SQL: zero runs for a fresh org
    from datetime import datetime, timezone
    org = await real_db("org_cap_sql")
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert await db_client.count_org_runs_since(org.id, midnight) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_daily_cap.py -v`
Expected: FAIL — missing `check_daily_call_cap` / `count_org_runs_since`.

- [ ] **Step 3: Implement**

`api/db/workflow_run_client.py` (inside the client class owning workflow-run queries):

```python
async def count_org_runs_since(self, organization_id: int, since: datetime) -> int:
    async with self.async_session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(WorkflowRunModel)
            .join(WorkflowModel, WorkflowRunModel.workflow_id == WorkflowModel.id)
            .where(
                WorkflowModel.organization_id == organization_id,
                WorkflowRunModel.created_at >= since,
            )
        )
        return int(result.scalar_one())
```

(If `WorkflowRunModel` carries `organization_id` directly, drop the join.)

`api/services/billing/plan_limits.py` — append:

```python
from datetime import datetime, timezone


def _utc_midnight() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def check_daily_call_cap(organization_id: int) -> str | None:
    """Returns an error message when today's (UTC) runs are at the plan cap."""
    if not enforcement_enabled():
        return None
    limits = await get_org_limits(organization_id)
    if limits.daily_call_cap is None:
        return None
    used = await db_client.count_org_runs_since(organization_id, _utc_midnight())
    if used >= limits.daily_call_cap:
        return (
            f"daily_call_cap_reached: your plan allows {limits.daily_call_cap} calls "
            f"per day. {UPGRADE_PROMPT}"
        )
    return None
```

`api/services/quota_service.py` — at the top of `_authorize_local_billing` (line 324), before rate resolution:

```python
from api.services.billing import plan_limits  # top of file

cap_error = await plan_limits.check_daily_call_cap(organization_id)
if cap_error:
    return QuotaCheckResult(
        has_quota=False,
        error_message=cap_error,
        error_code="daily_call_cap_reached",
    )
```

(Match the actual `QuotaCheckResult` constructor — see its dataclass at line 42 and how `_insufficient_billing_v2_quota_result()` builds one.)

`api/services/campaign/campaign_call_dispatcher.py` — at the top of `process_batch` (line 69), before claiming runs:

```python
from api.services.billing import billing_service, plan_limits  # top of file
from api.constants import BILLING_ENGINE, BILLING_LOCAL, MINIMUM_CREDIT_CENTS

# Saas plan guards: pause (not fail) the campaign when the org can't dispatch.
if plan_limits.enforcement_enabled():
    organization_id = ...  # resolve as the existing code in this method does
    pause_reason = await plan_limits.check_daily_call_cap(organization_id)
    if pause_reason is None and BILLING_ENGINE == BILLING_LOCAL:
        balance = await billing_service.get_balance_cents(organization_id)
        if balance < MINIMUM_CREDIT_CENTS:
            pause_reason = (
                "minutes_exhausted: your plan minutes are used up. "
                + plan_limits.UPGRADE_PROMPT
            )
    if pause_reason:
        await campaign_runner_service.pause_campaign(campaign_id)
        await db_client.append_campaign_log(campaign_id, pause_reason)
        return
```

(`process_batch` already loads the campaign/org — reuse its resolution; `campaign_runner_service` import from `api.services.campaign.runner`; match `append_campaign_log`'s real signature as used in `api/tasks/campaign_tasks.py`. `pause_campaign` requires state `running`/`syncing` — that guard is exactly what we want; catch/ignore its already-paused error if it raises.)

Concurrency from plan — `api/routes/campaign.py:28` and `api/services/campaign/campaign_call_dispatcher.py:54` both read the `CONCURRENT_CALL_LIMIT` org-config. In each helper, after computing the config value:

```python
if plan_limits.enforcement_enabled():
    limits = await plan_limits.get_org_limits(organization_id)
    return min(configured_limit, limits.max_concurrent_calls)
return configured_limit
```

- [ ] **Step 4: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_plan_daily_cap.py api/tests/test_quota_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/plan_limits.py api/services/quota_service.py api/services/campaign/campaign_call_dispatcher.py api/routes/campaign.py api/db/workflow_run_client.py api/tests/test_plan_daily_cap.py
git commit -m "feat(saas-p2): plan-capped concurrency, daily call cap, campaign auto-pause"
```

---

### Task 7: Razorpay provider behind `PaymentProvider`

**Files:**
- Modify: `api/requirements.txt` (add `razorpay` next to `stripe`, line 23)
- Modify: `api/constants.py` (Razorpay keys next to Stripe block, lines 65–68)
- Modify: `api/services/saas_config.py` (boot validation, extend the checks around line 28)
- Create: `api/services/billing/providers/__init__.py`, `api/services/billing/providers/base.py`, `api/services/billing/providers/razorpay_provider.py`
- Test: `api/tests/test_razorpay_provider.py`

**Interfaces:**
- Consumes: `razorpay` SDK (`razorpay.Client(auth=(key_id, key_secret))`, `client.subscription.create`, `client.subscription.cancel`, `client.utility.verify_webhook_signature`).
- Produces:
  - Constants: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (all `os.getenv(...)`, default None).
  - `base.py`: `@dataclass(frozen=True) SubscriptionCheckout: provider_subscription_id: str; checkout_url: str` and abstract `class PaymentProvider` with `async create_subscription(self, *, razorpay_plan_id: str, organization_id: int) -> SubscriptionCheckout`, `async cancel_subscription(self, provider_subscription_id: str, *, at_cycle_end: bool = True) -> None`, `def verify_webhook_signature(self, body: bytes, signature: str) -> bool`.
  - `razorpay_provider.py`: `class RazorpayProvider(PaymentProvider)` + module-level factory `def get_provider() -> PaymentProvider` (singleton).

- [ ] **Step 1: Install the SDK**

Add `razorpay` to `api/requirements.txt` (alphabetical near `stripe`), then:

Run: `source venv/bin/activate && pip install razorpay`
Expected: installs cleanly (pure-python package).

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_razorpay_provider.py
"""RazorpayProvider — SDK fully mocked; asserts request shapes + signature path."""

from unittest.mock import MagicMock, patch

import pytest

from api.services.billing.providers import razorpay_provider
from api.services.billing.providers.base import SubscriptionCheckout


@pytest.fixture
def provider():
    p = razorpay_provider.RazorpayProvider(key_id="rzp_test_x", key_secret="secret")
    p._client = MagicMock()
    return p


async def test_create_subscription_returns_checkout(provider):
    provider._client.subscription.create.return_value = {
        "id": "sub_123",
        "short_url": "https://rzp.io/i/abc",
        "status": "created",
    }
    result = await provider.create_subscription(
        razorpay_plan_id="plan_abc", organization_id=42
    )
    assert result == SubscriptionCheckout(
        provider_subscription_id="sub_123", checkout_url="https://rzp.io/i/abc"
    )
    payload = provider._client.subscription.create.call_args.args[0]
    assert payload["plan_id"] == "plan_abc"
    assert payload["total_count"] >= 12
    assert payload["notes"]["organization_id"] == "42"


async def test_cancel_subscription_at_cycle_end(provider):
    await provider.cancel_subscription("sub_123", at_cycle_end=True)
    provider._client.subscription.cancel.assert_called_once_with(
        "sub_123", {"cancel_at_cycle_end": 1}
    )


def test_verify_webhook_signature_delegates(provider):
    provider._client.utility.verify_webhook_signature.return_value = True
    with patch.object(razorpay_provider, "RAZORPAY_WEBHOOK_SECRET", "whsec"):
        assert provider.verify_webhook_signature(b'{"a":1}', "sig") is True


def test_verify_webhook_signature_false_on_error(provider):
    provider._client.utility.verify_webhook_signature.side_effect = Exception("bad sig")
    with patch.object(razorpay_provider, "RAZORPAY_WEBHOOK_SECRET", "whsec"):
        assert provider.verify_webhook_signature(b"{}", "sig") is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_razorpay_provider.py -v`
Expected: FAIL — import errors.

- [ ] **Step 4: Implement**

`api/constants.py` (after the Stripe block, line ~68):

```python
# Razorpay (saas phase 2): subscription billing. Like Stripe above, only
# meaningful with BILLING_ENGINE == "local".
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
```

`api/services/billing/providers/base.py`:

```python
"""PaymentProvider abstraction (spec §5). Razorpay first; Stripe later."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SubscriptionCheckout:
    provider_subscription_id: str
    checkout_url: str


class PaymentProvider(ABC):
    @abstractmethod
    async def create_subscription(
        self, *, razorpay_plan_id: str, organization_id: int
    ) -> SubscriptionCheckout: ...

    @abstractmethod
    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool = True
    ) -> None: ...

    @abstractmethod
    def verify_webhook_signature(self, body: bytes, signature: str) -> bool: ...
```

`api/services/billing/providers/razorpay_provider.py`:

```python
"""Razorpay Subscriptions driver.

The SDK is synchronous; calls run in a thread via asyncio.to_thread so the
event loop is never blocked.
"""

import asyncio
from functools import lru_cache

import razorpay
from loguru import logger

from api.constants import (
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET,
)
from api.services.billing.providers.base import PaymentProvider, SubscriptionCheckout

# 10 years of monthly cycles; Razorpay requires total_count on subscriptions.
_TOTAL_COUNT = 120


class RazorpayProvider(PaymentProvider):
    def __init__(self, key_id: str, key_secret: str):
        self._client = razorpay.Client(auth=(key_id, key_secret))

    async def create_subscription(
        self, *, razorpay_plan_id: str, organization_id: int
    ) -> SubscriptionCheckout:
        payload = {
            "plan_id": razorpay_plan_id,
            "total_count": _TOTAL_COUNT,
            "customer_notify": 1,
            "notes": {"organization_id": str(organization_id)},
        }
        sub = await asyncio.to_thread(self._client.subscription.create, payload)
        return SubscriptionCheckout(
            provider_subscription_id=sub["id"], checkout_url=sub["short_url"]
        )

    async def cancel_subscription(
        self, provider_subscription_id: str, *, at_cycle_end: bool = True
    ) -> None:
        await asyncio.to_thread(
            self._client.subscription.cancel,
            provider_subscription_id,
            {"cancel_at_cycle_end": 1 if at_cycle_end else 0},
        )

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        try:
            self._client.utility.verify_webhook_signature(
                body.decode("utf-8"), signature, RAZORPAY_WEBHOOK_SECRET
            )
            return True
        except Exception:
            logger.warning("Razorpay webhook signature verification failed")
            return False


@lru_cache(maxsize=1)
def get_provider() -> PaymentProvider:
    return RazorpayProvider(key_id=RAZORPAY_KEY_ID, key_secret=RAZORPAY_KEY_SECRET)
```

(`razorpay-python`'s `verify_webhook_signature` raises `SignatureVerificationError` on mismatch and returns `True` otherwise; the broad except keeps us fail-closed. The test's `total_count >= 12` assertion matches `_TOTAL_COUNT`. `subscription.cancel` in the SDK takes `(sub_id, data)` — if the installed SDK version differs, adapt the call and the test together.)

`api/services/saas_config.py` — extend the problems list built around line 28:

```python
if BILLING_PAYMENTS_ENABLED:
    for name, value in (
        ("RAZORPAY_KEY_ID", RAZORPAY_KEY_ID),
        ("RAZORPAY_KEY_SECRET", RAZORPAY_KEY_SECRET),
        ("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET),
    ):
        if not value:
            problems.append(f"{name} is required when BILLING_PAYMENTS_ENABLED=true")
```

(Match the file's actual accumulator variable/style; add the constants to its imports.)

- [ ] **Step 5: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_razorpay_provider.py -v`
Expected: PASS. Also run any existing saas_config test: `python -m pytest api/tests/ -k saas_config -v`.

- [ ] **Step 6: Commit**

```bash
git add api/requirements.txt api/constants.py api/services/saas_config.py api/services/billing/providers/
git add api/tests/test_razorpay_provider.py
git commit -m "feat(saas-p2): Razorpay driver behind PaymentProvider abstraction"
```

---

### Task 8: Subscription webhook service (lifecycle + ledger reset/renewal)

**Files:**
- Create: `api/services/billing/subscription_service.py`
- Test: `api/tests/test_subscription_service.py`

**Interfaces:**
- Consumes: `db_client` methods from Task 2; `billing_service.credit(organization_id, amount_cents, type, *, description=None, created_by=None, idempotency_key=None)` (`api/services/billing/billing_service.py:99`); `billing_service.get_balance_cents` (line 78); `trial.CENTS_PER_MINUTE` (`api/services/billing/trial.py:9`).
- Produces:
  - `async handle_event(event: dict, event_id: str) -> None` — dispatch on `event["event"]` ∈ {`subscription.activated`, `subscription.charged`, `subscription.halted`, `subscription.cancelled`, `payment.failed`}; unknown events are logged and ignored.
  - Razorpay event shape consumed: `event["payload"]["subscription"]["entity"]` → `{id, plan_id, status, current_end (unix), notes: {organization_id}}`; `event["payload"]["payment"]["entity"]` → `{id, amount, currency, status}`.
  - Ledger effects on `subscription.charged`: `plan_period_reset` (−current balance, key `razorpay:{event_id}:reset`, skipped when balance ≤ 0) then `plan_renewal` (+`plan.included_minutes * 100`, key `razorpay:{event_id}:renewal`), then invoice row.
  - Org state effects: activated/charged → `subscription_status="active"`, `plan_id` mapped via `get_plan_by_razorpay_plan_id`, `razorpay_subscription_id`, `current_period_end` from `current_end`; halted → `"halted"`; cancelled → `"cancelled"`; payment.failed → invoice row `status="failed"` only (grace period — no state change).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_subscription_service.py
"""Razorpay webhook handling: lifecycle transitions + idempotent ledger grants."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service, subscription_service


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    # identical committing-fixture shape to test_payment_service.py:19
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(provider_id: str, balance_cents: int = 0):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=provider_id, credit_balance_cents=balance_cents
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org
    async with maker() as s:
        await s.execute(delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids)))
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


def _sub_event(event_type, *, sub_id, plan_id, org_id, current_end=1893456000,
               payment_id="pay_1", amount=149900):
    return {
        "entity": "event",
        "event": event_type,
        "payload": {
            "subscription": {
                "entity": {
                    "id": sub_id,
                    "plan_id": plan_id,
                    "status": event_type.split(".")[-1],
                    "current_end": current_end,
                    "notes": {"organization_id": str(org_id)},
                }
            },
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                }
            },
        },
    }


async def _linked_starter_plan():
    starter = await db_client.get_plan_by_tier_key("starter")
    if starter.razorpay_plan_id is None:
        starter = await db_client.update_plan(starter.id, razorpay_plan_id="plan_starter_t8")
    return starter


async def test_activated_links_org(real_db):
    org = await real_db("org_sub_activated")
    plan = await _linked_starter_plan()
    ev = _sub_event("subscription.activated", sub_id="sub_act",
                    plan_id=plan.razorpay_plan_id, org_id=org.id)
    await subscription_service.handle_event(ev, "evt_act_1")
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_act")
    assert fresh.id == org.id
    assert fresh.subscription_status == "active"
    assert fresh.plan_id == plan.id
    assert fresh.current_period_end == datetime.fromtimestamp(1893456000, tz=timezone.utc)


async def test_charged_resets_then_grants_idempotently(real_db):
    org = await real_db("org_sub_charged", balance_cents=700)  # leftover trial
    plan = await _linked_starter_plan()
    ev = _sub_event("subscription.charged", sub_id="sub_chg",
                    plan_id=plan.razorpay_plan_id, org_id=org.id, payment_id="pay_chg")
    await subscription_service.handle_event(ev, "evt_chg_1")
    # leftover 700 expired, then 300 min * 100 granted
    assert await billing_service.get_balance_cents(org.id) == 30000
    # replay: no double grant
    await subscription_service.handle_event(ev, "evt_chg_1")
    assert await billing_service.get_balance_cents(org.id) == 30000
    invoices = await db_client.list_subscription_invoices(org.id)
    assert len(invoices) == 1
    assert invoices[0].razorpay_payment_id == "pay_chg"
    entries = await db_client.list_ledger_entries(org.id)
    types = [e.type for e in entries]
    assert "plan_renewal" in types
    assert "plan_period_reset" in types


async def test_charged_zero_balance_skips_reset(real_db):
    org = await real_db("org_sub_zero", balance_cents=0)
    plan = await _linked_starter_plan()
    ev = _sub_event("subscription.charged", sub_id="sub_zero",
                    plan_id=plan.razorpay_plan_id, org_id=org.id, payment_id="pay_zero")
    await subscription_service.handle_event(ev, "evt_zero_1")
    entries = await db_client.list_ledger_entries(org.id)
    assert [e.type for e in entries if e.type == "plan_period_reset"] == []
    assert await billing_service.get_balance_cents(org.id) == 30000


async def test_halted_and_cancelled_set_status(real_db):
    org = await real_db("org_sub_halted")
    plan = await _linked_starter_plan()
    act = _sub_event("subscription.activated", sub_id="sub_hlt",
                     plan_id=plan.razorpay_plan_id, org_id=org.id)
    await subscription_service.handle_event(act, "evt_h_0")
    await subscription_service.handle_event(
        _sub_event("subscription.halted", sub_id="sub_hlt",
                   plan_id=plan.razorpay_plan_id, org_id=org.id), "evt_h_1")
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_hlt")
    assert fresh.subscription_status == "halted"
    await subscription_service.handle_event(
        _sub_event("subscription.cancelled", sub_id="sub_hlt",
                   plan_id=plan.razorpay_plan_id, org_id=org.id), "evt_h_2")
    fresh = await db_client.get_org_by_razorpay_subscription_id("sub_hlt")
    assert fresh.subscription_status == "cancelled"


async def test_unknown_event_ignored(real_db):
    await subscription_service.handle_event({"event": "invoice.paid", "payload": {}}, "evt_x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_service.py -v`
Expected: FAIL — `ImportError` on `subscription_service`.

- [ ] **Step 3: Implement `api/services/billing/subscription_service.py`**

```python
"""Razorpay subscription lifecycle → org state + ledger (spec §4/§5).

No rollover: each successful charge expires the previous period's remaining
balance (plan_period_reset) then grants the new allowance (plan_renewal).
Both entries are idempotent on the Razorpay event id via the ledger's
(organization_id, idempotency_key) unique constraint.
"""

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service
from api.services.billing.trial import CENTS_PER_MINUTE


def _subscription_entity(event: dict) -> Optional[dict]:
    return (event.get("payload") or {}).get("subscription", {}).get("entity")


def _payment_entity(event: dict) -> Optional[dict]:
    return (event.get("payload") or {}).get("payment", {}).get("entity")


async def _resolve_org(sub: dict) -> Optional[OrganizationModel]:
    org = await db_client.get_org_by_razorpay_subscription_id(sub["id"])
    if org is not None:
        return org
    org_id = (sub.get("notes") or {}).get("organization_id")
    if org_id is None:
        return None
    return await db_client.get_organization_by_id(int(org_id))


def _period_end(sub: dict) -> Optional[datetime]:
    ts = sub.get("current_end")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


async def _link_org(org: OrganizationModel, sub: dict, *, status: str) -> None:
    plan = None
    if sub.get("plan_id"):
        plan = await db_client.get_plan_by_razorpay_plan_id(sub["plan_id"])
    await db_client.update_org_subscription(
        org.id,
        plan_id=plan.id if plan else org.plan_id,
        razorpay_subscription_id=sub["id"],
        subscription_status=status,
        current_period_end=_period_end(sub),
    )


async def _handle_activated(event: dict, event_id: str) -> None:
    sub = _subscription_entity(event)
    org = sub and await _resolve_org(sub)
    if org is None:
        logger.warning(f"razorpay activated: no org resolvable (event {event_id})")
        return
    await _link_org(org, sub, status="active")


async def _handle_charged(event: dict, event_id: str) -> None:
    sub = _subscription_entity(event)
    org = sub and await _resolve_org(sub)
    if org is None:
        logger.warning(f"razorpay charged: no org resolvable (event {event_id})")
        return
    await _link_org(org, sub, status="active")
    plan = await db_client.get_plan_by_razorpay_plan_id(sub.get("plan_id") or "")
    if plan is None:
        logger.error(
            f"razorpay charged: unknown plan {sub.get('plan_id')} for org {org.id}"
        )
        return

    # 1. Expire the previous period's remaining balance (no rollover).
    balance = await billing_service.get_balance_cents(org.id)
    if balance > 0:
        await billing_service.credit(
            org.id,
            -balance,
            "plan_period_reset",
            description=f"Plan period reset ({plan.tier_key})",
            idempotency_key=f"razorpay:{event_id}:reset",
        )
    # 2. Grant the new period's allowance.
    await billing_service.credit(
        org.id,
        plan.included_minutes * CENTS_PER_MINUTE,
        "plan_renewal",
        description=f"{plan.display_name}: {plan.included_minutes} minutes",
        idempotency_key=f"razorpay:{event_id}:renewal",
    )
    # 3. Record the invoice for payment history.
    payment = _payment_entity(event)
    if payment:
        await db_client.record_subscription_invoice(
            organization_id=org.id,
            razorpay_payment_id=payment["id"],
            razorpay_subscription_id=sub["id"],
            amount_cents=int(payment.get("amount", 0)),
            currency=(payment.get("currency") or "INR").lower(),
            status="captured",
        )


async def _handle_status_only(event: dict, event_id: str, status: str) -> None:
    sub = _subscription_entity(event)
    org = sub and await _resolve_org(sub)
    if org is None:
        logger.warning(f"razorpay {status}: no org resolvable (event {event_id})")
        return
    await db_client.update_org_subscription(org.id, subscription_status=status)


async def _handle_payment_failed(event: dict, event_id: str) -> None:
    # Grace period: no state change until Razorpay emits subscription.halted.
    payment = _payment_entity(event)
    sub = _subscription_entity(event)
    org = sub and await _resolve_org(sub)
    if org is None or payment is None:
        logger.warning(f"razorpay payment.failed: unresolvable (event {event_id})")
        return
    await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id=payment["id"],
        razorpay_subscription_id=sub["id"] if sub else None,
        amount_cents=int(payment.get("amount", 0)),
        currency=(payment.get("currency") or "INR").lower(),
        status="failed",
    )


async def handle_event(event: dict, event_id: str) -> None:
    event_type = event.get("event")
    if event_type == "subscription.activated":
        await _handle_activated(event, event_id)
    elif event_type == "subscription.charged":
        await _handle_charged(event, event_id)
    elif event_type == "subscription.halted":
        await _handle_status_only(event, event_id, "halted")
    elif event_type == "subscription.cancelled":
        await _handle_status_only(event, event_id, "cancelled")
    elif event_type == "payment.failed":
        await _handle_payment_failed(event, event_id)
    else:
        logger.info(f"razorpay: ignoring event type {event_type}")
```

(Use the real single-org getter name found in Task 4 for `get_organization_by_id`. Retry-safety of `_handle_charged`: reset applies first and is idempotent; a crash between reset and renewal is healed by Razorpay's webhook retry — reset replay is absorbed by the idempotency key, then the grant lands.)

- [ ] **Step 4: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/subscription_service.py api/tests/test_subscription_service.py
git commit -m "feat(saas-p2): subscription lifecycle service with idempotent renewal grants"
```

---

### Task 9: Routes — Razorpay webhook + customer subscription endpoints

**Files:**
- Modify: `api/routes/webhooks.py` (add `/webhooks/razorpay` beside `/webhooks/stripe` at line 22)
- Modify: `api/routes/billing.py` (add subscription endpoints beside the pack routes; `_require_enabled()` at line 44)
- Modify: `api/routes/main.py` only if new routers were created (not needed — both routers are already mounted at lines 46–49)
- Test: `api/tests/test_subscription_routes.py`

**Interfaces:**
- Consumes: `get_provider()` (Task 7), `subscription_service.handle_event` (Task 8), `plan_limits.get_org_limits` (Task 4), `db_client` plan methods (Task 2), the existing auth dependency used by `routes/billing.py` (same `user` dependency as `GET /billing/packs` at line 49).
- Produces (all under `/api/v1`):
  - `POST /webhooks/razorpay` — 404 unless `BILLING_PAYMENTS_ENABLED`; 400 `invalid_signature` unless `X-Razorpay-Signature` verifies; dispatches to `subscription_service.handle_event(event, event_id)` where `event_id` = `X-Razorpay-Event-Id` header (fallback: `sha256(body).hexdigest()[:32]`); returns `{"received": True}`.
  - `GET /billing/plans` → `list[PlanPublicResponse]` (`tier_key, display_name, price_cents, currency, included_minutes, max_agents, max_concurrent_calls, daily_call_cap, max_active_campaigns, is_current`).
  - `GET /billing/subscription` → `SubscriptionResponse` (`plan_tier: str | None, plan_display_name: str | None, subscription_status: str | None, current_period_end: str | None, included_minutes: int | None`).
  - `POST /billing/subscribe` body `{tier_key: str}` → `{checkout_url: str}`; 404 unknown tier; 409 `plan_not_purchasable` when `razorpay_plan_id` is NULL; 409 `already_subscribed` when org `subscription_status == "active"`.
  - `POST /billing/change-plan` body `{tier_key: str}` → cancels current subscription immediately via provider, then returns new `{checkout_url}`.
  - `POST /billing/cancel` → provider cancel `at_cycle_end=True`; returns `{"status": "cancellation_scheduled"}`; 409 `no_active_subscription` when not active.
  - `GET /billing/invoices` → `list[InvoiceResponse]` (`id, razorpay_payment_id, amount_cents, currency, status, created_at`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_subscription_routes.py
"""Webhook + customer subscription routes. Provider is always mocked."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

# Use the same app/test-client + auth-override fixtures as api/tests/test_billing_balance.py
# (it exercises routes/billing_balance.py the same way these routes need).

from api.services.billing.providers.base import SubscriptionCheckout


def _mock_provider(verify=True):
    provider = MagicMock()
    provider.verify_webhook_signature.return_value = verify
    provider.create_subscription = AsyncMock(
        return_value=SubscriptionCheckout(
            provider_subscription_id="sub_new", checkout_url="https://rzp.io/i/new"
        )
    )
    provider.cancel_subscription = AsyncMock()
    return provider


async def test_webhook_rejects_bad_signature(client_payments_enabled):
    with patch("api.routes.webhooks.get_provider", return_value=_mock_provider(verify=False)):
        resp = await client_payments_enabled.post(
            "/api/v1/webhooks/razorpay",
            content=json.dumps({"event": "subscription.charged"}),
            headers={"X-Razorpay-Signature": "bad"},
        )
    assert resp.status_code == 400


async def test_webhook_dispatches_verified_event(client_payments_enabled):
    handled = AsyncMock()
    with patch("api.routes.webhooks.get_provider", return_value=_mock_provider()), \
         patch("api.routes.webhooks.subscription_service") as svc:
        svc.handle_event = handled
        resp = await client_payments_enabled.post(
            "/api/v1/webhooks/razorpay",
            content=json.dumps({"event": "subscription.charged", "payload": {}}),
            headers={"X-Razorpay-Signature": "good", "X-Razorpay-Event-Id": "evt_9"},
        )
    assert resp.status_code == 200
    handled.assert_awaited_once()
    assert handled.await_args.args[1] == "evt_9"


async def test_list_plans_public(client_payments_enabled):
    resp = await client_payments_enabled.get("/api/v1/billing/plans")
    assert resp.status_code == 200
    tiers = [p["tier_key"] for p in resp.json()]
    assert "starter" in tiers


async def test_subscribe_requires_linked_plan(client_payments_enabled):
    # seeded plans have razorpay_plan_id = NULL → not purchasable yet
    resp = await client_payments_enabled.post(
        "/api/v1/billing/subscribe", json={"tier_key": "starter"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "plan_not_purchasable"


async def test_subscribe_returns_checkout_url(client_payments_enabled, linked_plan):
    with patch("api.routes.billing.get_provider", return_value=_mock_provider()):
        resp = await client_payments_enabled.post(
            "/api/v1/billing/subscribe", json={"tier_key": linked_plan.tier_key}
        )
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://rzp.io/i/new"


async def test_cancel_without_subscription_409(client_payments_enabled):
    resp = await client_payments_enabled.post("/api/v1/billing/cancel")
    assert resp.status_code == 409
```

(Fixture notes for the implementer: `client_payments_enabled` = the billing-route test client from `test_billing_balance.py`'s pattern plus `monkeypatch.setattr("api.routes.billing.BILLING_PAYMENTS_ENABLED", True)` and same for `api.routes.webhooks`; `linked_plan` = a fixture that sets `razorpay_plan_id` on a purpose-created plan via `db_client.create_plan(tier_key="t9_linked", ..., razorpay_plan_id="plan_t9")` and deletes it on teardown. Keep the auth-user org consistent with what the client fixture authenticates as.)

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_routes.py -v`
Expected: FAIL — 404s on the new paths.

- [ ] **Step 3: Implement webhook route in `api/routes/webhooks.py`**

```python
import hashlib

from api.services.billing import subscription_service
from api.services.billing.providers.razorpay_provider import get_provider


@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    if not BILLING_PAYMENTS_ENABLED:
        raise HTTPException(status_code=404)
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if not get_provider().verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="invalid_signature")
    event = json.loads(body)
    event_id = request.headers.get(
        "X-Razorpay-Event-Id", hashlib.sha256(body).hexdigest()[:32]
    )
    await subscription_service.handle_event(event, event_id)
    return {"received": True}
```

(Add `json` import; mirror the Stripe route's structure at line 22.)

- [ ] **Step 4: Implement customer routes in `api/routes/billing.py`**

```python
from api.services.billing import plan_limits  # noqa: F401 (UPGRADE_PROMPT reuse)
from api.services.billing.providers.razorpay_provider import get_provider


class PlanPublicResponse(BaseModel):
    tier_key: str
    display_name: str
    price_cents: int
    currency: str
    included_minutes: int
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None
    is_current: bool


class SubscriptionResponse(BaseModel):
    plan_tier: str | None
    plan_display_name: str | None
    subscription_status: str | None
    current_period_end: str | None
    included_minutes: int | None


class SubscribeRequest(BaseModel):
    tier_key: str


class CheckoutUrlResponse(BaseModel):
    checkout_url: str


class InvoiceResponse(BaseModel):
    id: int
    razorpay_payment_id: str
    amount_cents: int
    currency: str
    status: str
    created_at: str | None


@router.get("/plans", response_model=list[PlanPublicResponse])
async def list_plans(user=Depends(get_user)):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    plans = await db_client.list_active_plans()
    return [
        PlanPublicResponse(
            tier_key=p.tier_key,
            display_name=p.display_name,
            price_cents=p.price_cents,
            currency=p.currency,
            included_minutes=p.included_minutes,
            max_agents=p.max_agents,
            max_concurrent_calls=p.max_concurrent_calls,
            daily_call_cap=p.daily_call_cap,
            max_active_campaigns=p.max_active_campaigns,
            is_current=bool(org and org.plan_id == p.id),
        )
        for p in plans
    ]


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(user=Depends(get_user)):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    plan = await db_client.get_plan_by_id(org.plan_id) if org and org.plan_id else None
    return SubscriptionResponse(
        plan_tier=plan.tier_key if plan else None,
        plan_display_name=plan.display_name if plan else None,
        subscription_status=org.subscription_status if org else None,
        current_period_end=(
            org.current_period_end.isoformat()
            if org and org.current_period_end
            else None
        ),
        included_minutes=plan.included_minutes if plan else None,
    )


async def _start_checkout(org, tier_key: str) -> CheckoutUrlResponse:
    plan = await db_client.get_plan_by_tier_key(tier_key)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="plan_not_found")
    if not plan.razorpay_plan_id:
        raise HTTPException(status_code=409, detail="plan_not_purchasable")
    checkout = await get_provider().create_subscription(
        razorpay_plan_id=plan.razorpay_plan_id, organization_id=org.id
    )
    # Store the pending subscription id so the activation webhook can resolve
    # the org even if Razorpay drops the notes field.
    await db_client.update_org_subscription(
        org.id, razorpay_subscription_id=checkout.provider_subscription_id
    )
    return CheckoutUrlResponse(checkout_url=checkout.checkout_url)


@router.post("/subscribe", response_model=CheckoutUrlResponse)
async def subscribe(body: SubscribeRequest, user=Depends(get_user)):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status == "active":
        raise HTTPException(status_code=409, detail="already_subscribed")
    return await _start_checkout(org, body.tier_key)


@router.post("/change-plan", response_model=CheckoutUrlResponse)
async def change_plan(body: SubscribeRequest, user=Depends(get_user)):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status != "active" or not org.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="no_active_subscription")
    # v1 plan change: cancel now, re-subscribe. Remaining minutes expire on the
    # new plan's first charge (plan_period_reset), per the no-rollover rule.
    await get_provider().cancel_subscription(
        org.razorpay_subscription_id, at_cycle_end=False
    )
    return await _start_checkout(org, body.tier_key)


@router.post("/cancel")
async def cancel_subscription(user=Depends(get_user)):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status != "active" or not org.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="no_active_subscription")
    await get_provider().cancel_subscription(
        org.razorpay_subscription_id, at_cycle_end=True
    )
    return {"status": "cancellation_scheduled"}


@router.get("/invoices", response_model=list[InvoiceResponse])
async def list_invoices(user=Depends(get_user)):
    _require_enabled()
    invoices = await db_client.list_subscription_invoices(user.selected_organization_id)
    return [
        InvoiceResponse(
            id=i.id,
            razorpay_payment_id=i.razorpay_payment_id,
            amount_cents=i.amount_cents,
            currency=i.currency,
            status=i.status,
            created_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in invoices
    ]
```

(Match the existing auth dependency name in `routes/billing.py` — it's whatever `GET /billing/packs` at line 49 uses, likely `Depends(get_user)` from `api/services/auth/depends.py`. Use the real single-org getter name.)

- [ ] **Step 5: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_routes.py api/tests/test_billing_balance.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes/webhooks.py api/routes/billing.py api/tests/test_subscription_routes.py
git commit -m "feat(saas-p2): razorpay webhook + customer subscription routes"
```

---

### Task 10: Block calls for halted/cancelled subscriptions

**Files:**
- Modify: `api/services/quota_service.py` (`_authorize_local_billing`, line 324 — after the Task 6 daily-cap check)
- Test: `api/tests/test_subscription_blocking.py`

**Interfaces:**
- Consumes: `OrganizationModel.subscription_status`, `plan_limits.enforcement_enabled()`, `QuotaCheckResult`.
- Produces: quota denial `error_code="subscription_inactive"` with message `"Your subscription is past due or cancelled. Reactivate it at /billing to resume calling."` when `subscription_status in ("halted", "cancelled")`. Orgs with `subscription_status` `None` (trial) or `"active"` proceed to the normal balance check.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_subscription_blocking.py
"""halted/cancelled orgs are blocked from starting calls in saas mode."""

from unittest.mock import patch

# copy the module-scoped real_db/make_org fixture (accepting subscription_status)
# from test_plan_limits.py, adding subscription_status to make_org's kwargs.

from api.services import quota_service
from api.services.billing import plan_limits


async def test_halted_org_blocked(real_db):
    org = await real_db("org_blk_halted", subscription_status="halted")
    with patch.object(plan_limits, "enforcement_enabled", return_value=True):
        result = await quota_service._check_subscription_state(org.id)
    assert result is not None
    assert result.error_code == "subscription_inactive"


async def test_cancelled_org_blocked(real_db):
    org = await real_db("org_blk_cancelled", subscription_status="cancelled")
    with patch.object(plan_limits, "enforcement_enabled", return_value=True):
        result = await quota_service._check_subscription_state(org.id)
    assert result is not None


async def test_trial_org_not_blocked(real_db):
    org = await real_db("org_blk_trial", subscription_status=None)
    with patch.object(plan_limits, "enforcement_enabled", return_value=True):
        assert await quota_service._check_subscription_state(org.id) is None


async def test_active_org_not_blocked(real_db):
    org = await real_db("org_blk_active", subscription_status="active")
    with patch.object(plan_limits, "enforcement_enabled", return_value=True):
        assert await quota_service._check_subscription_state(org.id) is None


async def test_oss_mode_not_blocked(real_db):
    org = await real_db("org_blk_oss", subscription_status="halted")
    with patch.object(plan_limits, "enforcement_enabled", return_value=False):
        assert await quota_service._check_subscription_state(org.id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_blocking.py -v`
Expected: FAIL — no `_check_subscription_state`.

- [ ] **Step 3: Implement in `api/services/quota_service.py`**

```python
SUBSCRIPTION_INACTIVE_MESSAGE = (
    "Your subscription is past due or cancelled. "
    "Reactivate it at /billing to resume calling."
)


async def _check_subscription_state(organization_id: int) -> QuotaCheckResult | None:
    """Saas-only: halted/cancelled subscriptions block new calls (spec §4)."""
    if not plan_limits.enforcement_enabled():
        return None
    org = await db_client.get_organization_by_id(organization_id)
    if org is not None and org.subscription_status in ("halted", "cancelled"):
        return QuotaCheckResult(
            has_quota=False,
            error_message=SUBSCRIPTION_INACTIVE_MESSAGE,
            error_code="subscription_inactive",
        )
    return None
```

Then call it at the top of `_authorize_local_billing` (before the Task 6 daily-cap check):

```python
blocked = await _check_subscription_state(organization_id)
if blocked is not None:
    return blocked
```

- [ ] **Step 4: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_subscription_blocking.py api/tests/test_quota_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/quota_service.py api/tests/test_subscription_blocking.py
git commit -m "feat(saas-p2): block calls for halted/cancelled subscriptions"
```

---

### Task 11: Full backend regression + OpenAPI regeneration

**Files:**
- Modify: `docs/api-reference/openapi.json` (regenerated)
- Modify: `ui/src/client/*` (regenerated)

- [ ] **Step 1: Run the whole backend suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -x -q`
Expected: PASS apart from the known pre-existing failures listed in the memory note `dev-environment-quirks.md` (verify against that list before treating a failure as new).

- [ ] **Step 2: Regenerate OpenAPI + typed UI client**

Start the API locally (per `docs/contribution/setup.mdx`), then:

```bash
cd ui && npm run generate-client
```

Copy the fresh spec into docs if the repo convention does so (check how `docs/api-reference/openapi.json` was produced in phase 1 — `git log --follow docs/api-reference/openapi.json`).

Expected new SDK functions: `listPlansApiV1BillingPlansGet`, `getSubscriptionApiV1BillingSubscriptionGet`, `subscribeApiV1BillingSubscribePost`, `changePlanApiV1BillingChangePlanPost`, `cancelSubscriptionApiV1BillingCancelPost`, `listInvoicesApiV1BillingInvoicesGet`, plus superuser plan CRUD.

- [ ] **Step 3: Commit**

```bash
git add docs/api-reference/openapi.json ui/src/client
git commit -m "chore(saas-p2): regenerate openapi spec + typed client"
```

---

### Task 12: UI — plans page + billing page rework

**Files:**
- Create: `ui/src/app/billing/plans/page.tsx`
- Create: `ui/src/components/billing/CurrentPlanCard.tsx`
- Modify: `ui/src/app/billing/page.tsx` (saas branch: add `CurrentPlanCard`, invoices table, link to `/billing/plans`)
- Test: `cd ui && npx tsc --noEmit && npm run lint` (no UI unit-test infra exists; type + lint gate)

**Interfaces:**
- Consumes: generated SDK functions from Task 11; `useAppConfig()` (`ui/src/context/AppConfigContext.tsx`) for `deploymentMode === "saas"`; `useAuth()` fetch-guard convention (`!authLoading && user` + `hasFetched` ref — see `MinutesRemainingCard.tsx`); shadcn primitives from `ui/src/components/ui/`; `detailFromError` from `ui/src/lib/apiError.ts` (generated client returns `{data, error}` and never throws).
- Produces: `/billing/plans` pricing page; billing page showing current plan, status badge, renewal date, cancel button, invoices ("Payment history") table.

- [ ] **Step 1: Build `CurrentPlanCard.tsx`**

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import {
  cancelSubscriptionApiV1BillingCancelPost,
  getSubscriptionApiV1BillingSubscriptionGet,
} from "@/client/sdk.gen";
import type { SubscriptionResponse } from "@/client/types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/lib/auth";
import { toast } from "sonner";

const STATUS_VARIANT: Record<string, "default" | "destructive" | "secondary"> = {
  active: "default",
  halted: "destructive",
  cancelled: "secondary",
};

export function CurrentPlanCard() {
  const { user, loading: authLoading } = useAuth();
  const [subscription, setSubscription] =
    useState<SubscriptionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (authLoading || !user || hasFetched.current) return;
    hasFetched.current = true;
    getSubscriptionApiV1BillingSubscriptionGet().then((res) => {
      if (res.data) setSubscription(res.data);
      setLoading(false);
    });
  }, [authLoading, user]);

  const handleCancel = async () => {
    const res = await cancelSubscriptionApiV1BillingCancelPost();
    if (res.error) {
      toast.error("Could not cancel the subscription. Please try again.");
      return;
    }
    toast.success("Cancellation scheduled for the end of the billing period.");
  };

  if (loading) return <Skeleton className="h-40 w-full" />;

  const status = subscription?.subscription_status;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {subscription?.plan_display_name ?? "Free trial"}
          {status && <Badge variant={STATUS_VARIANT[status] ?? "secondary"}>{status}</Badge>}
        </CardTitle>
        <CardDescription>
          {subscription?.included_minutes != null
            ? `${subscription.included_minutes} minutes included each month`
            : "Trial minutes only — pick a plan to keep calling"}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          {subscription?.current_period_end
            ? `Renews ${new Date(subscription.current_period_end).toLocaleDateString()}`
            : "No renewal scheduled"}
        </div>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link href="/billing/plans">
              {status === "active" ? "Change plan" : "View plans"}
            </Link>
          </Button>
          {status === "active" && (
            <Button variant="ghost" onClick={handleCancel}>
              Cancel
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 2: Build `/billing/plans/page.tsx`**

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import {
  changePlanApiV1BillingChangePlanPost,
  getSubscriptionApiV1BillingSubscriptionGet,
  listPlansApiV1BillingPlansGet,
  subscribeApiV1BillingSubscribePost,
} from "@/client/sdk.gen";
import type { PlanPublicResponse } from "@/client/types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAppConfig } from "@/context/AppConfigContext";
import { useAuth } from "@/lib/auth";
import { toast } from "sonner";

function priceLabel(cents: number, currency: string) {
  const amount = (cents / 100).toLocaleString("en-IN");
  return currency === "inr" ? `₹${amount}` : `${currency.toUpperCase()} ${amount}`;
}

function limitLabel(value: number | null | undefined, noun: string) {
  return value == null ? `Unlimited ${noun}` : `${value} ${noun}`;
}

export default function PlansPage() {
  const { config, loading: configLoading } = useAppConfig();
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [plans, setPlans] = useState<PlanPublicResponse[]>([]);
  const [hasActiveSub, setHasActiveSub] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyTier, setBusyTier] = useState<string | null>(null);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (configLoading || authLoading || !user || hasFetched.current) return;
    if (config?.deploymentMode !== "saas") {
      router.replace("/billing");
      return;
    }
    hasFetched.current = true;
    Promise.all([
      listPlansApiV1BillingPlansGet(),
      getSubscriptionApiV1BillingSubscriptionGet(),
    ]).then(([plansRes, subRes]) => {
      if (plansRes.data) setPlans(plansRes.data);
      setHasActiveSub(subRes.data?.subscription_status === "active");
      setLoading(false);
    });
  }, [configLoading, authLoading, user, config?.deploymentMode, router]);

  const choosePlan = async (tierKey: string) => {
    setBusyTier(tierKey);
    const call = hasActiveSub
      ? changePlanApiV1BillingChangePlanPost
      : subscribeApiV1BillingSubscribePost;
    const res = await call({ body: { tier_key: tierKey } });
    setBusyTier(null);
    if (res.error || !res.data) {
      toast.error("Could not start checkout. Please try again.");
      return;
    }
    window.location.href = res.data.checkout_url;
  };

  if (loading) return <Skeleton className="m-8 h-96" />;

  return (
    <div className="mx-auto max-w-5xl p-8">
      <h1 className="mb-2 text-2xl font-semibold">Choose your plan</h1>
      <p className="mb-8 text-muted-foreground">
        Every plan includes monthly calling minutes. Model choice can burn
        minutes faster — the multiplier is shown in the agent builder.
      </p>
      <div className="grid gap-6 md:grid-cols-3">
        {plans.map((plan) => (
          <Card key={plan.tier_key} className={plan.is_current ? "border-primary" : ""}>
            <CardHeader>
              <CardTitle className="flex items-center justify-between">
                {plan.display_name}
                {plan.is_current && <Badge>Current</Badge>}
              </CardTitle>
              <CardDescription>
                <span className="text-2xl font-semibold text-foreground">
                  {priceLabel(plan.price_cents, plan.currency)}
                </span>
                /month
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <div>{plan.included_minutes.toLocaleString()} minutes / month</div>
              <div>{limitLabel(plan.max_agents, "agents")}</div>
              <div>{plan.max_concurrent_calls} concurrent calls</div>
              <div>{limitLabel(plan.max_active_campaigns, "active campaigns")}</div>
              <Button
                className="mt-4 w-full"
                disabled={plan.is_current || busyTier !== null}
                onClick={() => choosePlan(plan.tier_key)}
              >
                {busyTier === plan.tier_key
                  ? "Starting checkout…"
                  : plan.is_current
                    ? "Your plan"
                    : hasActiveSub
                      ? "Switch to this plan"
                      : "Subscribe"}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Rework `ui/src/app/billing/page.tsx` saas branch**

In the existing `isSaasMode` branch: render `<CurrentPlanCard />` above `<MinutesRemainingCard />`; add a "Payment history" `Card` + `Table` fed by `listInvoicesApiV1BillingInvoicesGet()` (columns: Date / Amount / Status; amount = `priceLabel(amount_cents, currency)`; fetch alongside the existing `localLedger` fetch at page.tsx:170); keep the existing "Recent Activity" ledger table. Follow the file's existing state/fetch structure exactly.

- [ ] **Step 4: Type-check and lint**

Run: `cd ui && npx tsc --noEmit && npm run lint`
Expected: clean (or only pre-existing warnings).

- [ ] **Step 5: Manual smoke (API + UI running)**

- `/billing` in saas mode shows plan card ("Free trial" when unsubscribed) + minutes meter + payment history (empty) + activity.
- `/billing/plans` renders the three seeded tiers; Subscribe on an unlinked plan surfaces the 409 toast (checkout can't start until superadmin sets `razorpay_plan_id`).
- OSS mode: `/billing/plans` redirects to `/billing`; `/billing` unchanged.

- [ ] **Step 6: Commit**

```bash
git add ui/src/app/billing ui/src/components/billing/CurrentPlanCard.tsx
git commit -m "feat(saas-p2): plans pricing page + billing page plan/invoice cards"
```

---

### Task 13: Docs + phase-2 smoke checklist

**Files:**
- Modify: `docs/SAAS_SETUP.md` (add a "Razorpay billing" section)
- Create: `docs/superpowers/plans/phase2-smoke-checklist.md` (gitignored dir — force-add like phase 1)

- [ ] **Step 1: Document Razorpay setup in `docs/SAAS_SETUP.md`**

Cover, matching the doc's existing style: creating Razorpay test-mode API keys; env vars `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `BILLING_PAYMENTS_ENABLED=true` (and that saas boot validation now requires the three keys when payments are on); creating the three Plans in the Razorpay dashboard (monthly, INR, amounts matching the seeded `price_cents`) and linking each id via `PATCH /api/v1/superuser/plans/{id}` (`razorpay_plan_id`); configuring the webhook endpoint `https://<host>/api/v1/webhooks/razorpay` with events `subscription.activated`, `subscription.charged`, `subscription.halted`, `subscription.cancelled`, `payment.failed`; trial-limit env vars (`TRIAL_MAX_AGENTS` etc.).

- [ ] **Step 2: Write `docs/superpowers/plans/phase2-smoke-checklist.md`**

```markdown
# Phase 2 smoke checklist (live Razorpay test mode)

Needs: running saas deployment, Clerk login, Razorpay test keys + webhook
configured, plans linked to Razorpay plan ids via superadmin.

- [ ] `/billing/plans` shows Starter/Pro/Scale with INR prices.
- [ ] Subscribe → Razorpay hosted checkout opens (test UPI/card succeeds).
- [ ] After checkout: webhook `subscription.activated` + `subscription.charged`
      land (API logs); org shows plan active, renewal date set.
- [ ] Balance = plan minutes (leftover trial minutes were expired —
      ledger shows `plan_period_reset` then `plan_renewal`).
- [ ] Replay the charged webhook from the Razorpay dashboard → balance unchanged.
- [ ] Payment history row appears on `/billing`.
- [ ] Creating agents beyond the tier cap → 402 with upgrade prompt.
- [ ] Starting a 2nd campaign on Starter → 402 with upgrade prompt.
- [ ] Cancel → "cancellation scheduled"; after period end (or manual cancel in
      dashboard) webhook flips org to cancelled and calls are blocked.
- [ ] Simulate `subscription.halted` → calls blocked, agents still editable.
- [ ] OSS deployment untouched: no plan gates, `/billing/plans` redirects.
```

- [ ] **Step 3: Commit**

```bash
git add docs/SAAS_SETUP.md
git add -f docs/superpowers/plans/phase2-smoke-checklist.md
git commit -m "docs(saas-p2): razorpay setup guide + phase-2 smoke checklist"
```

---

## Self-Review Notes

- **Spec coverage (§4/§5/§6/§9 phase-2 items):** PlanModel + org fields → Task 1; superadmin tunable plans → Task 3; ledger reset/renewal idempotent on event id → Task 8; pre-call/mid-call/post-call billing paths reused unchanged → untouched (existing `quota_service`/`billing_service`); burn multipliers → already shipped (PricingRule engine, phase 1); max agents → Task 5; concurrency from plan → Task 6; daily cap + max campaigns → Tasks 5–6; minutes exhausted auto-pause → Task 6; lifecycle states + grace → Tasks 8/10; PaymentProvider interface + Razorpay driver + verified idempotent webhooks → Tasks 7–9; billing page (plan, renewal, meter, cancel, history, ledger) → Task 12; boot validation for Razorpay keys → Task 7. Deferred (recorded in header decisions): in-place plan change, per-day usage sparkline (ledger data is available; add later), email exhaustion prompts, org-timezone day windows.
- **Known follow-ups for the reviewer:** `subscription.charged` reset amount uses read-then-write (not atomic with the grant) — safe under webhook retries due to idempotency keys, but a concurrent call debit between reset and grant can under- or over-expire by that call's cost; acceptable at launch volume. `/billing/plans` route ordering: FastAPI matches `/billing/plans` before any `/billing/{...}` params — none exist today, keep it that way.
- **Line numbers** referenced (e.g. `routes/workflow.py:401`) drift as tasks land earlier edits — treat them as anchors, re-locate by symbol name.
