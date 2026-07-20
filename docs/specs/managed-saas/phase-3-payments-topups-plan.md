# Payments / Credit Top-ups (Stripe) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-serve, prepaid credit top-up rail on top of Phase 1's local billing
engine — a Stripe-backed pack catalog, hosted Checkout Sessions, and a signature-verified,
idempotent webhook that credits `CreditLedgerModel` via `BillingService.credit(...)`. No
subscriptions, no new balance-mutation path — this phase is purely the payment rail that
feeds Phase 1's ledger.

**Architecture:** A new `PaymentService` (`api/services/billing/payment_service.py`) owns
everything Stripe-shaped (customer creation, Checkout Session creation, webhook event
handling) and delegates every balance mutation to Phase 1's `billing_service.credit(...)`.
Two new tables (`payment_packs`, `payments`) plus `organizations.stripe_customer_id` are
added via a migration chained directly after Phase 1's head revision `b1f0c0de0001`. A new
`api/routes/billing.py` exposes org-scoped, authenticated pack/checkout/history endpoints;
a new `api/routes/webhooks.py` exposes the unauthenticated, signature-verified
`POST /webhooks/stripe`. Everything is gated behind `BILLING_PAYMENTS_ENABLED` (default
off) and requires `BILLING_ENGINE=local` (Phase 1's flag) to have any effect.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy (async), Alembic, PostgreSQL, `stripe`
Python SDK, pytest + pytest-asyncio, httpx (`ASGITransport`), loguru.

## Global Constraints

- Money unit: same as Phase 1 — **integer cents**. `PaymentPackModel.credits_granted` is
  what gets credited; `price_cents` is what Stripe charges. They may differ (bonus packs).
- Idempotency: Stripe **event id** (`evt_...`) is the ledger `idempotency_key`, prefixed
  `stripe:{event_id}` per the spec. Reuses Phase 1's unique `(organization_id,
  idempotency_key)` constraint on `credit_ledger` — no schema change needed there.
- Webhook signature verification (`stripe.Webhook.construct_event`) happens on the **raw
  request body**; never process an unverified payload. Reject with `400` on failure.
  Always return `200` once an event is durably processed or is a no-op duplicate.
- Never call live Stripe from tests. All `stripe.*` SDK calls are patched with
  `unittest.mock.AsyncMock`/`Mock` at the `payment_service` module boundary.
- Feature gate: `BILLING_PAYMENTS_ENABLED` (env, default `false`). When off, `/billing/*`
  and `/webhooks/stripe` reject/no-op — zero behavior change for OSS/MPS deployments.
- Tenant isolation: every `PaymentModel`/`PaymentPackModel` read/write for a customer is
  scoped to `organization_id` from `get_user_with_selected_organization` (see
  `api/AGENTS.md`).
- DB access lives in `api/db/*_client.py` mixins (`PaymentClient`); domain/Stripe logic in
  `api/services/billing/payment_service.py`; routes stay thin.
- Tests run against the test DB: `source venv/bin/activate && set -a && source
  api/.env.test && set +a && python -m pytest ...`. DB-integration tests require the
  project's pgvector-enabled Postgres from `docker-compose-local.yaml` to be up.
- Migrations are created via `./scripts/makemigrate.sh "description"` and applied with
  `./scripts/migrate.sh`. This phase's migration's `down_revision` **must** be
  `"b1f0c0de0001"` (Phase 1's head, confirmed as the current alembic head with no
  migration downstream of it).

---

## File Structure

**Create:**
- `api/services/billing/payment_service.py` — `PaymentService`: pack catalog, Stripe
  customer/session creation, webhook event handlers. Pure enough to unit test with a
  mocked Stripe client.
- `api/db/payment_client.py` — `PaymentClient` DB mixin: pack catalog reads, `PaymentModel`
  CRUD, event-id dedup lookup.
- `api/routes/billing.py` — customer-facing, org-scoped routes (`/billing/packs`,
  `/billing/checkout`, `/billing/payments`).
- `api/routes/webhooks.py` — `POST /webhooks/stripe`, signature-verified, unauthenticated.
- `api/tests/test_payment_service.py` — unit tests for pack math, `ensure_stripe_customer`,
  refund proportional math, signature verification (mocked Stripe client).
- `api/tests/test_billing_routes.py` — route-level tests for packs/checkout/payments
  (function-level monkeypatch, matching `test_organization_usage_billing.py`'s style).
- `api/tests/test_webhooks_stripe.py` — ASGI-level tests for the webhook endpoint
  (signature verification, idempotent/duplicate/replayed events, checkout-completed →
  ledger credit, refund → negative ledger, failed/expired → no ledger write).

**Modify:**
- `api/db/models.py` — add `PaymentPackModel`, `PaymentModel`,
  `OrganizationModel.stripe_customer_id`, relationships.
- `api/db/db_client.py` — add `PaymentClient` to the `DBClient` base list + docstring line.
- `api/constants.py` — add `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_PUBLISHABLE_KEY`, `BILLING_PAYMENTS_ENABLED`.
- `api/routes/main.py` — mount `billing_router` and `webhooks_router`.
- `api/requirements.txt` — add `stripe`.
- `api/.env.example`, `api/.env.test.example` — document the new Stripe env vars (test
  files get dummy/test-mode values so the flag can be exercised in tests without live
  keys).

---

## Task 1: Feature flag, Stripe config constants, and dependency

**Files:**
- Modify: `api/constants.py`, `api/requirements.txt`, `api/.env.example`,
  `api/.env.test.example`

**Interfaces:**
- Produces: `STRIPE_SECRET_KEY: str | None`, `STRIPE_WEBHOOK_SECRET: str | None`,
  `STRIPE_PUBLISHABLE_KEY: str | None`, `BILLING_PAYMENTS_ENABLED: bool` (default `False`).

- [ ] **Step 1: Read the existing flag pattern**

Run: `grep -n "BILLING_ENGINE\|BILLING_LOCAL\|MINIMUM_CREDIT_CENTS" api/constants.py`
Expected: shows Phase 1's `os.getenv("BILLING_ENGINE", "mps")` idiom (lines ~37-40) to
mirror.

- [ ] **Step 2: Add the constants**

In `api/constants.py`, directly after the `BILLING_ENGINE`/`MINIMUM_CREDIT_CENTS` block:

```python
# Stripe-backed prepaid credit top-ups (Phase 3). Off by default; meaningless unless
# BILLING_ENGINE == "local" since payments only ever feed the local ledger.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
BILLING_PAYMENTS_ENABLED = os.getenv("BILLING_PAYMENTS_ENABLED", "false").lower() == "true"
```

- [ ] **Step 3: Add the `stripe` dependency**

In `api/requirements.txt`, add a new line (matching the existing unsorted, one-package-
per-line style):

```
stripe==11.5.0
```

Run: `source venv/bin/activate && pip install -r api/requirements.txt`
Expected: `stripe` installs cleanly alongside existing pins.

- [ ] **Step 4: Document the env vars**

In `api/.env.example` and `api/.env.test.example`, add (near any existing billing-related
vars, or at the end):

```
# Stripe prepaid credit top-ups (Phase 3). Leave unset / BILLING_PAYMENTS_ENABLED=false
# for deployments that don't sell credits.
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PUBLISHABLE_KEY=
BILLING_PAYMENTS_ENABLED=false
```

In `api/.env.test`, add test-mode dummy values so the flag can be flipped on in tests
without live Stripe credentials:

```
STRIPE_SECRET_KEY=sk_test_dummy
STRIPE_WEBHOOK_SECRET=whsec_test_dummy
STRIPE_PUBLISHABLE_KEY=pk_test_dummy
BILLING_PAYMENTS_ENABLED=true
```

- [ ] **Step 5: Verify import**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "from api.constants import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, BILLING_PAYMENTS_ENABLED; print(STRIPE_SECRET_KEY, BILLING_PAYMENTS_ENABLED)"`
Expected: `sk_test_dummy True`

- [ ] **Step 6: Commit**

```bash
git add api/constants.py api/requirements.txt api/.env.example api/.env.test.example
git commit -m "feat(payments): add Stripe config constants, flag, and dependency"
```

---

## Task 2: Data models & migration (`PaymentPackModel`, `PaymentModel`, `stripe_customer_id`)

**Files:**
- Modify: `api/db/models.py`
- Migration: generated under `api/alembic/versions/`

**Interfaces:**
- Produces:
  - `PaymentPackModel` (table `payment_packs`): `id`, `pack_key:str unique`,
    `display_name:str`, `price_cents:int`, `credits_granted:int`, `currency:str`,
    `is_active:bool`, `sort_order:int`, `created_at`, `updated_at`.
  - `PaymentModel` (table `payments`): `id`, `organization_id:int (FK)`,
    `payment_pack_id:int|None (FK)`, `stripe_checkout_session_id:str unique`,
    `stripe_payment_intent_id:str|None unique`, `stripe_customer_id:str`,
    `amount_cents_paid:int`, `currency:str`, `credits_granted:int`,
    `status:str` (`pending`/`succeeded`/`failed`/`refunded`/`partially_refunded`),
    `credit_ledger_id:int|None (FK credit_ledger.id)`, `failure_reason:str|None`,
    `created_at`, `updated_at`.
  - `OrganizationModel.stripe_customer_id:str|None unique`.

- [ ] **Step 1: Add models to `api/db/models.py`**

After `CreditLedgerModel` (defined just above `PricingRuleModel` per Phase 1), add:

```python
class PaymentPackModel(Base):
    """Prepaid credit-pack catalog. Read by GET /billing/packs; referenced by id when
    creating a Stripe Checkout Session. Deactivating a pack hides it from the catalog
    without invalidating historical PaymentModel rows that reference it."""

    __tablename__ = "payment_packs"

    id = Column(Integer, primary_key=True, index=True)
    pack_key = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    price_cents = Column(Integer, nullable=False)
    credits_granted = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="usd", server_default=text("'usd'"))
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_payment_packs_active", "is_active"),
    )


class PaymentModel(Base):
    """One row per Stripe Checkout attempt — the Stripe-facing audit trail. The credit
    ledger (Phase 1) stays generic; this table is what "payment history" reads from and
    is the join between a Stripe event and the ledger row it produced."""

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    payment_pack_id = Column(
        Integer, ForeignKey("payment_packs.id", ondelete="SET NULL"), nullable=True
    )
    stripe_checkout_session_id = Column(String, unique=True, nullable=False, index=True)
    stripe_payment_intent_id = Column(String, unique=True, nullable=True, index=True)
    stripe_customer_id = Column(String, nullable=False, index=True)
    amount_cents_paid = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, default="usd", server_default=text("'usd'"))
    credits_granted = Column(Integer, nullable=False)
    status = Column(
        String, nullable=False, default="pending", server_default=text("'pending'")
    )
    credit_ledger_id = Column(
        Integer, ForeignKey("credit_ledger.id", ondelete="SET NULL"), nullable=True
    )
    failure_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    organization = relationship("OrganizationModel", back_populates="payments")

    __table_args__ = (
        Index("ix_payments_organization_id", "organization_id"),
        Index("ix_payments_status", "status"),
    )
```

- [ ] **Step 2: Add `stripe_customer_id` + relationship to `OrganizationModel`**

In `OrganizationModel`, directly after `credit_balance_cents` (Phase 1):

```python
    # Stripe customer id, created lazily on first checkout and reused on subsequent
    # purchases (Phase 3). Plain column, not ExternalCredentialModel, since it's a
    # single non-secret identifier looked up on nearly every billing request.
    stripe_customer_id = Column(String, unique=True, nullable=True, index=True)
```

And in its relationships block, alongside `credit_ledger_entries`:

```python
    payments = relationship("PaymentModel", back_populates="organization")
```

- [ ] **Step 3: Generate the migration**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && ./scripts/makemigrate.sh "add stripe payment packs and payments tables"`
Expected: a new file in `api/alembic/versions/` creating `payment_packs`, `payments`, and
adding `organizations.stripe_customer_id`.

- [ ] **Step 4: Inspect and fix the migration's `down_revision`**

Open the generated file. Verify `revision` is a fresh id and set (if autogen picked a
different parent):

```python
down_revision: Union[str, None] = "b1f0c0de0001"
```

Confirm this is correct by running:
`grep -rL "down_revision" api/alembic/versions/*.py; grep -rn 'down_revision.*b1f0c0de0001' api/alembic/versions/*.py`
Expected: the second command returns **only** this new migration file (i.e.
`b1f0c0de0001` was the alembic head with nothing chained after it before this task).

Also verify: both tables created, `stripe_checkout_session_id` and
`stripe_payment_intent_id` unique constraints present, `organizations.stripe_customer_id`
added as unique nullable, all FKs (`payment_pack_id → payment_packs.id ON DELETE SET
NULL`, `credit_ledger_id → credit_ledger.id ON DELETE SET NULL`,
`organization_id → organizations.id ON DELETE CASCADE`) present.

- [ ] **Step 5: Apply and verify against test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh && python -c "
import asyncio
from sqlalchemy import text
from api.db.database import engine
import api.db.models

async def go():
    async with engine.begin() as c:
        r = await c.execute(text(\"select to_regclass('payment_packs'), to_regclass('payments')\"))
        print(r.fetchone())

asyncio.run(go())
"`
Expected: `('payment_packs', 'payments')`.

- [ ] **Step 6: Commit**

```bash
git add api/db/models.py api/alembic/versions/
git commit -m "feat(payments): add payment_packs, payments tables and org stripe_customer_id"
```

---

## Task 3: `PaymentClient` DB mixin (pack catalog + payment CRUD)

**Files:**
- Create: `api/db/payment_client.py`
- Modify: `api/db/db_client.py`
- Test: `api/tests/test_payment_service.py` (DB-backed portion, using the `real_db`-style
  fixture pattern from `api/tests/test_billing_service.py`)

**Interfaces:**
- Consumes: `PaymentPackModel`, `PaymentModel`, `OrganizationModel` (Task 2).
- Produces methods on `db_client`:
  - `async list_active_payment_packs() -> list[PaymentPackModel]` (ordered by `sort_order`)
  - `async get_payment_pack_by_key(pack_key: str) -> PaymentPackModel | None`
  - `async create_payment(*, organization_id, payment_pack_id, stripe_checkout_session_id, stripe_customer_id, amount_cents_paid, currency, credits_granted) -> PaymentModel` (status defaults `pending`)
  - `async get_payment_by_checkout_session_id(session_id: str) -> PaymentModel | None`
  - `async get_payment_by_id(payment_id: int) -> PaymentModel | None`
  - `async get_payment_by_payment_intent_id(payment_intent_id: str) -> PaymentModel | None`
  - `async update_payment(payment_id, **fields) -> PaymentModel` (used for status/failure_reason/credit_ledger_id/stripe_payment_intent_id transitions)
  - `async list_payments_for_org(organization_id, *, limit, cursor) -> list[PaymentModel]` (ordered `id desc`, cursor = last-seen id)
  - `async get_org_stripe_customer_id(organization_id) -> str | None`
  - `async set_org_stripe_customer_id(organization_id, stripe_customer_id) -> None`
  - `async find_pending_payment(organization_id, payment_pack_id, *, newer_than) -> PaymentModel | None` (duplicate-click guard for `POST /billing/checkout`)

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_payment_service.py
"""Tests for the Stripe payment rail: PaymentClient + PaymentService.

DB-backed tests use a real committing session factory (matching
api/tests/test_billing_service.py's `real_db` fixture) so idempotency and FK
constraints behave as in production. Stripe SDK calls are always mocked — no live
network calls.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel, PaymentModel, PaymentPackModel


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
    created_pack_ids: list[int] = []

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

    async def make_pack(pack_key: str, price_cents: int, credits_granted: int) -> int:
        async with session_factory() as session:
            pack = PaymentPackModel(
                pack_key=pack_key,
                display_name=pack_key,
                price_cents=price_cents,
                credits_granted=credits_granted,
                currency="usd",
            )
            session.add(pack)
            await session.commit()
            await session.refresh(pack)
            created_pack_ids.append(pack.id)
            return pack.id

    yield make_org, make_pack

    async with session_factory() as session:
        if created_org_ids:
            await session.execute(
                delete(OrganizationModel).where(
                    OrganizationModel.id.in_(created_org_ids)
                )
            )
        if created_pack_ids:
            await session.execute(
                delete(PaymentPackModel).where(
                    PaymentPackModel.id.in_(created_pack_ids)
                )
            )
        await session.commit()

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_fetch_payment_by_checkout_session(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_create")
    pack_id = await make_pack("starter_10", 1000, 1000)

    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_abc",
        stripe_customer_id="cus_test_1",
        amount_cents_paid=1000,
        currency="usd",
        credits_granted=1000,
    )
    assert payment.status == "pending"

    fetched = await db_client.get_payment_by_checkout_session_id("cs_test_abc")
    assert fetched is not None
    assert fetched.id == payment.id


@pytest.mark.asyncio
async def test_update_payment_transitions_status(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_update")
    pack_id = await make_pack("growth_50", 5000, 5200)
    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_upd",
        stripe_customer_id="cus_test_2",
        amount_cents_paid=5000,
        currency="usd",
        credits_granted=5200,
    )

    updated = await db_client.update_payment(
        payment.id, status="succeeded", stripe_payment_intent_id="pi_test_1"
    )
    assert updated.status == "succeeded"
    assert updated.stripe_payment_intent_id == "pi_test_1"


@pytest.mark.asyncio
async def test_org_stripe_customer_id_roundtrip(real_db):
    make_org, _ = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_customer")
    assert await db_client.get_org_stripe_customer_id(org_id) is None

    await db_client.set_org_stripe_customer_id(org_id, "cus_test_new")
    assert await db_client.get_org_stripe_customer_id(org_id) == "cus_test_new"


@pytest.mark.asyncio
async def test_find_pending_payment_duplicate_guard(real_db):
    make_org, make_pack = real_db
    from api.db import db_client

    org_id = await make_org("org_pay_dup")
    pack_id = await make_pack("scale_100", 10000, 10500)
    await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_dup",
        stripe_customer_id="cus_test_3",
        amount_cents_paid=10000,
        currency="usd",
        credits_granted=10500,
    )

    recent = await db_client.find_pending_payment(
        org_id, pack_id, newer_than=datetime.now(UTC) - timedelta(minutes=5)
    )
    assert recent is not None

    stale_cutoff = await db_client.find_pending_payment(
        org_id, pack_id, newer_than=datetime.now(UTC) + timedelta(minutes=5)
    )
    assert stale_cutoff is None
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -v`
Expected: FAIL — `AttributeError: 'DBClient' object has no attribute 'create_payment'`.

- [ ] **Step 3: Implement `api/db/payment_client.py`**

```python
"""Database client for Stripe-backed prepaid credit top-ups (Phase 3).

Owns the payment-pack catalog and the PaymentModel audit trail. Never mutates the
credit ledger or balance directly — that stays Phase 1's BillingClient's job, called
from PaymentService once a payment is confirmed.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, PaymentModel, PaymentPackModel


class PaymentClient(BaseDBClient):
    async def list_active_payment_packs(self) -> list[PaymentPackModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentPackModel)
                .where(PaymentPackModel.is_active.is_(True))
                .order_by(PaymentPackModel.sort_order)
            )
            return list(result.scalars().all())

    async def get_payment_pack_by_key(
        self, pack_key: str
    ) -> Optional[PaymentPackModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentPackModel).where(PaymentPackModel.pack_key == pack_key)
            )
            return result.scalars().first()

    async def create_payment(
        self,
        *,
        organization_id: int,
        payment_pack_id: Optional[int],
        stripe_checkout_session_id: str,
        stripe_customer_id: str,
        amount_cents_paid: int,
        currency: str,
        credits_granted: int,
    ) -> PaymentModel:
        async with self.async_session() as session:
            payment = PaymentModel(
                organization_id=organization_id,
                payment_pack_id=payment_pack_id,
                stripe_checkout_session_id=stripe_checkout_session_id,
                stripe_customer_id=stripe_customer_id,
                amount_cents_paid=amount_cents_paid,
                currency=currency,
                credits_granted=credits_granted,
                status="pending",
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)
            return payment

    async def get_payment_by_checkout_session_id(
        self, session_id: str
    ) -> Optional[PaymentModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentModel).where(
                    PaymentModel.stripe_checkout_session_id == session_id
                )
            )
            return result.scalars().first()

    async def get_payment_by_id(self, payment_id: int) -> Optional[PaymentModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentModel).where(PaymentModel.id == payment_id)
            )
            return result.scalars().first()

    async def get_payment_by_payment_intent_id(
        self, payment_intent_id: str
    ) -> Optional[PaymentModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentModel).where(
                    PaymentModel.stripe_payment_intent_id == payment_intent_id
                )
            )
            return result.scalars().first()

    async def update_payment(self, payment_id: int, **fields) -> PaymentModel:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentModel).where(PaymentModel.id == payment_id)
            )
            payment = result.scalars().one()
            for key, value in fields.items():
                setattr(payment, key, value)
            await session.commit()
            await session.refresh(payment)
            return payment

    async def list_payments_for_org(
        self,
        organization_id: int,
        *,
        limit: int = 50,
        cursor: Optional[int] = None,
    ) -> list[PaymentModel]:
        async with self.async_session() as session:
            stmt = (
                select(PaymentModel)
                .where(PaymentModel.organization_id == organization_id)
                .order_by(PaymentModel.id.desc())
                .limit(limit)
            )
            if cursor is not None:
                stmt = stmt.where(PaymentModel.id < cursor)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_org_stripe_customer_id(
        self, organization_id: int
    ) -> Optional[str]:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel.stripe_customer_id).where(
                    OrganizationModel.id == organization_id
                )
            )
            return result.scalar_one_or_none()

    async def set_org_stripe_customer_id(
        self, organization_id: int, stripe_customer_id: str
    ) -> None:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(
                    OrganizationModel.id == organization_id
                )
            )
            org = result.scalars().one()
            org.stripe_customer_id = stripe_customer_id
            await session.commit()

    async def find_pending_payment(
        self,
        organization_id: int,
        payment_pack_id: int,
        *,
        newer_than: datetime,
    ) -> Optional[PaymentModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentModel)
                .where(
                    PaymentModel.organization_id == organization_id,
                    PaymentModel.payment_pack_id == payment_pack_id,
                    PaymentModel.status == "pending",
                    PaymentModel.created_at >= newer_than,
                )
                .order_by(PaymentModel.id.desc())
            )
            return result.scalars().first()
```

- [ ] **Step 4: Register the mixin**

In `api/db/db_client.py`, add `from api.db.payment_client import PaymentClient` and add
`PaymentClient,` to the `DBClient(...)` base list (alongside `BillingClient`).

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add api/db/payment_client.py api/db/db_client.py api/tests/test_payment_service.py
git commit -m "feat(payments): PaymentClient DB mixin for pack catalog and payment CRUD"
```

---

## Task 4: `PaymentService` — pack listing, Stripe customer, Checkout Session creation

**Files:**
- Create: `api/services/billing/payment_service.py`
- Test: extend `api/tests/test_payment_service.py`

**Interfaces:**
- Consumes: `db_client` (Task 3), `stripe` SDK (module-level `stripe.Customer`,
  `stripe.checkout.Session`), `STRIPE_SECRET_KEY` (Task 1).
- Produces module-level async functions:
  - `async def list_active_packs() -> list[PaymentPackModel]`
  - `async def ensure_stripe_customer(organization) -> str` — returns existing
    `stripe_customer_id` or creates one via `stripe.Customer.create_async(..., metadata={"organization_id": ...}, idempotency_key=f"org:{organization_id}:customer")` and persists it.
  - `@dataclass CheckoutSessionResult`: `checkout_url: str, payment_id: int`
  - `async def create_checkout_session(organization, pack, *, success_url, cancel_url) -> CheckoutSessionResult`
  - `class PackNotFoundError(Exception)`, `class DuplicateCheckoutError(Exception)`

- [ ] **Step 1: Write the failing tests (mocked Stripe client)**

```python
# append to api/tests/test_payment_service.py
from unittest.mock import AsyncMock, patch

from types import SimpleNamespace

from api.services.billing import payment_service


@pytest.mark.asyncio
async def test_list_active_packs_returns_catalog(real_db):
    make_org, make_pack = real_db
    await make_pack("starter_10", 1000, 1000)

    packs = await payment_service.list_active_packs()
    assert any(p.pack_key == "starter_10" for p in packs)


@pytest.mark.asyncio
async def test_ensure_stripe_customer_creates_once_and_reuses(real_db):
    make_org, _ = real_db
    org_id = await make_org("org_pay_ensure")
    org = SimpleNamespace(id=org_id, stripe_customer_id=None)

    fake_customer = SimpleNamespace(id="cus_test_ensure")
    with patch.object(
        payment_service.stripe.Customer,
        "create_async",
        AsyncMock(return_value=fake_customer),
    ) as create_mock:
        customer_id = await payment_service.ensure_stripe_customer(org)
        assert customer_id == "cus_test_ensure"
        create_mock.assert_awaited_once()
        _, kwargs = create_mock.call_args
        assert kwargs["idempotency_key"] == f"org:{org_id}:customer"

        # Second call reuses the now-persisted id and does not call Stripe again.
        org.stripe_customer_id = None  # simulate a fresh in-memory object
        customer_id_2 = await payment_service.ensure_stripe_customer(org)
        assert customer_id_2 == "cus_test_ensure"
        create_mock.assert_awaited_once()  # still only once


@pytest.mark.asyncio
async def test_create_checkout_session_creates_pending_payment(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_checkout")
    pack_id = await make_pack("growth_50", 5000, 5200)

    from api.db import db_client

    pack = await db_client.get_payment_pack_by_key("growth_50")
    org = SimpleNamespace(id=org_id, stripe_customer_id="cus_existing")

    fake_session = SimpleNamespace(id="cs_test_created", url="https://checkout.stripe.com/c/pay/cs_test_created")
    with patch.object(
        payment_service.stripe.checkout.Session,
        "create_async",
        AsyncMock(return_value=fake_session),
    ) as create_mock:
        result = await payment_service.create_checkout_session(
            org, pack, success_url="https://app/billing?checkout=success",
            cancel_url="https://app/billing?checkout=cancelled",
        )

    assert result.checkout_url == fake_session.url
    payment = await db_client.get_payment_by_id(result.payment_id)
    assert payment.status == "pending"
    assert payment.amount_cents_paid == 5000
    assert payment.credits_granted == 5200
    assert payment.stripe_checkout_session_id == "cs_test_created"

    _, kwargs = create_mock.call_args
    assert kwargs["customer"] == "cus_existing"
    assert kwargs["mode"] == "payment"
    assert kwargs["metadata"]["organization_id"] == str(org_id)
    assert kwargs["metadata"]["pack_key"] == "growth_50"
    assert kwargs["metadata"]["payment_id"] == str(payment.id)


@pytest.mark.asyncio
async def test_create_checkout_session_rejects_duplicate_pending(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_checkout_dup")
    pack_id = await make_pack("scale_100", 10000, 10500)

    from api.db import db_client

    pack = await db_client.get_payment_pack_by_key("scale_100")
    org = SimpleNamespace(id=org_id, stripe_customer_id="cus_existing_2")

    fake_session = SimpleNamespace(id="cs_test_first", url="https://checkout/cs_test_first")
    with patch.object(
        payment_service.stripe.checkout.Session,
        "create_async",
        AsyncMock(return_value=fake_session),
    ):
        await payment_service.create_checkout_session(
            org, pack, success_url="s", cancel_url="c"
        )

    with pytest.raises(payment_service.DuplicateCheckoutError):
        await payment_service.create_checkout_session(
            org, pack, success_url="s", cancel_url="c"
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -k "list_active_packs or ensure_stripe_customer or create_checkout_session" -v`
Expected: FAIL — `ModuleNotFoundError: api.services.billing.payment_service`.

- [ ] **Step 3: Implement `api/services/billing/payment_service.py`**

```python
"""Stripe-backed prepaid credit top-ups (Phase 3).

Owns everything Stripe-shaped: customer creation, Checkout Session creation, and
webhook event handling. Delegates all ledger/balance mutation to Phase 1's
billing_service.credit(...) — this module never writes to credit_ledger directly.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import stripe
from loguru import logger

from api.constants import STRIPE_SECRET_KEY
from api.db import db_client
from api.db.models import PaymentModel, PaymentPackModel
from api.services.billing import billing_service

stripe.api_key = STRIPE_SECRET_KEY

# Soft duplicate-click guard: an org can't open a second Checkout for the same pack
# while a prior attempt is still pending within this window.
_PENDING_CHECKOUT_WINDOW = timedelta(minutes=10)


class PackNotFoundError(Exception):
    """Raised when POST /billing/checkout references an unknown/inactive pack_key."""


class DuplicateCheckoutError(Exception):
    """Raised when an org already has a recent pending payment for the same pack."""


@dataclass(frozen=True)
class CheckoutSessionResult:
    checkout_url: str
    payment_id: int


async def list_active_packs() -> list[PaymentPackModel]:
    return await db_client.list_active_payment_packs()


async def ensure_stripe_customer(organization) -> str:
    """Return the org's Stripe customer id, creating one lazily on first use.

    Idempotent via Stripe's own idempotency-key request option keyed on the org id,
    so a retried request can never create two Stripe customers for one org.
    """
    existing = organization.stripe_customer_id or await db_client.get_org_stripe_customer_id(
        organization.id
    )
    if existing:
        return existing

    customer = await stripe.Customer.create_async(
        metadata={"organization_id": str(organization.id)},
        idempotency_key=f"org:{organization.id}:customer",
    )
    await db_client.set_org_stripe_customer_id(organization.id, customer.id)
    return customer.id


async def create_checkout_session(
    organization,
    pack: PaymentPackModel,
    *,
    success_url: str,
    cancel_url: str,
) -> CheckoutSessionResult:
    duplicate = await db_client.find_pending_payment(
        organization.id,
        pack.id,
        newer_than=datetime.now(UTC) - _PENDING_CHECKOUT_WINDOW,
    )
    if duplicate is not None:
        raise DuplicateCheckoutError(
            f"org {organization.id} already has pending payment {duplicate.id} "
            f"for pack {pack.pack_key}"
        )

    customer_id = await ensure_stripe_customer(organization)

    session = await stripe.checkout.Session.create_async(
        mode="payment",
        customer=customer_id,
        client_reference_id=str(organization.id),
        success_url=success_url,
        cancel_url=cancel_url,
        line_items=[
            {
                "price_data": {
                    "currency": pack.currency,
                    "product_data": {"name": pack.display_name},
                    "unit_amount": pack.price_cents,
                },
                "quantity": 1,
            }
        ],
        metadata={
            "organization_id": str(organization.id),
            "pack_key": pack.pack_key,
            # payment_id filled in below, after the PaymentModel row exists —
            # Stripe requires the session to exist before we can reference its id,
            # so we create the DB row first with a placeholder-free two-step: create
            # PaymentModel referencing the *not-yet-created* session id is invalid,
            # so instead we create the Stripe session first (metadata minus
            # payment_id), then create the PaymentModel, then patch metadata.
        },
    )

    payment = await db_client.create_payment(
        organization_id=organization.id,
        payment_pack_id=pack.id,
        stripe_checkout_session_id=session.id,
        stripe_customer_id=customer_id,
        amount_cents_paid=pack.price_cents,
        currency=pack.currency,
        credits_granted=pack.credits_granted,
    )

    await stripe.checkout.Session.modify_async(
        session.id,
        metadata={
            "organization_id": str(organization.id),
            "pack_key": pack.pack_key,
            "payment_id": str(payment.id),
        },
    )

    return CheckoutSessionResult(checkout_url=session.url, payment_id=payment.id)
```

Note the two-step metadata write (create session, create `PaymentModel`, patch session
metadata with `payment_id`) — Stripe assigns the session id, and the row can't be created
before that id exists. This trades one extra Stripe API call for having `payment_id`
available in the webhook without a fallback lookup. Document this trade-off in code
(above) and keep the `stripe_checkout_session_id` fallback lookup in the webhook handler
(Task 5) as the resilience path if `modify_async` itself fails.

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -v`
Expected: all pass (8 total so far).

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/payment_service.py api/tests/test_payment_service.py
git commit -m "feat(payments): PaymentService pack listing, stripe customer, checkout session creation"
```

---

## Task 5: `PaymentService` webhook handlers — checkout completed, failed, refunded

**Files:**
- Modify: `api/services/billing/payment_service.py`
- Test: extend `api/tests/test_payment_service.py`

**Interfaces:**
- Consumes: `billing_service.credit` (Phase 1), `db_client` payment methods (Task 3).
- Produces:
  - `async def handle_checkout_completed(event: dict) -> None` — the
    `checkout.session.completed` path per the spec's webhook flow: dedup on
    `credit_ledger.idempotency_key = f"stripe:{event['id']}"`, load `PaymentModel` by
    `session.metadata.payment_id` (fallback `stripe_checkout_session_id`), skip if
    already `succeeded`/`payment_status != "paid"`, else `billing_service.credit(...,
    type="topup", idempotency_key=f"stripe:{event['id']}")` then
    `update_payment(status="succeeded", stripe_payment_intent_id=..., credit_ledger_id=...)`.
  - `async def handle_payment_failed(event: dict) -> None` — marks `PaymentModel`
    `failed`/`expired` with `failure_reason`; no ledger write.
  - `async def handle_charge_refunded(event: dict) -> None` — looks up `PaymentModel` by
    `stripe_payment_intent_id`; if not `succeeded` yet, raises `RefundTooEarlyError` (the
    route layer turns this into a `5xx` so Stripe retries); else computes
    `refunded_credits = floor(payment.credits_granted * amount_refunded / amount_cents_paid)`,
    calls `billing_service.credit(..., amount_cents=-refunded_credits, type="refund",
    idempotency_key=f"stripe:{event['id']}")`, and sets `status` to `refunded` (full) or
    `partially_refunded`.

- [ ] **Step 1: Write the failing tests**

```python
# append to api/tests/test_payment_service.py

def _stripe_event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


@pytest.mark.asyncio
async def test_handle_checkout_completed_credits_ledger_once(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_webhook", balance_cents=0)
    pack_id = await make_pack("starter_10", 1000, 1000)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id,
        payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_webhook",
        stripe_customer_id="cus_test_webhook",
        amount_cents_paid=1000,
        currency="usd",
        credits_granted=1000,
    )

    event = _stripe_event(
        "evt_test_1",
        "checkout.session.completed",
        {
            "id": "cs_test_webhook",
            "payment_intent": "pi_test_webhook",
            "payment_status": "paid",
            "metadata": {"payment_id": str(payment.id), "organization_id": str(org_id), "pack_key": "starter_10"},
        },
    )

    await payment_service.handle_checkout_completed(event)

    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "succeeded"
    assert updated.stripe_payment_intent_id == "pi_test_webhook"
    assert updated.credit_ledger_id is not None
    assert await billing_service.get_balance_cents(org_id) == 1000

    # Replay: exactly one credit, no error.
    await payment_service.handle_checkout_completed(event)
    assert await billing_service.get_balance_cents(org_id) == 1000


@pytest.mark.asyncio
async def test_handle_checkout_completed_unpaid_session_is_noop(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_webhook_unpaid", balance_cents=0)
    pack_id = await make_pack("starter_10b", 1000, 1000)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_unpaid", stripe_customer_id="cus_x",
        amount_cents_paid=1000, currency="usd", credits_granted=1000,
    )
    event = _stripe_event(
        "evt_test_unpaid", "checkout.session.completed",
        {
            "id": "cs_test_unpaid", "payment_intent": None, "payment_status": "unpaid",
            "metadata": {"payment_id": str(payment.id)},
        },
    )
    await payment_service.handle_checkout_completed(event)
    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "pending"
    assert await billing_service.get_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_handle_payment_failed_marks_failed_no_ledger_write(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_failed", balance_cents=0)
    pack_id = await make_pack("starter_10c", 1000, 1000)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_failed", stripe_customer_id="cus_y",
        amount_cents_paid=1000, currency="usd", credits_granted=1000,
    )
    event = _stripe_event(
        "evt_test_failed", "checkout.session.expired",
        {"id": "cs_test_failed", "metadata": {"payment_id": str(payment.id)}},
    )
    await payment_service.handle_payment_failed(event)
    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "failed"
    assert await billing_service.get_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_handle_charge_refunded_full_refund(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_refund_full", balance_cents=0)
    pack_id = await make_pack("growth_50b", 5000, 5200)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_refund1", stripe_customer_id="cus_z",
        amount_cents_paid=5000, currency="usd", credits_granted=5200,
    )
    completed_event = _stripe_event(
        "evt_test_refund1_pay", "checkout.session.completed",
        {
            "id": "cs_test_refund1", "payment_intent": "pi_test_refund1",
            "payment_status": "paid", "metadata": {"payment_id": str(payment.id)},
        },
    )
    await payment_service.handle_checkout_completed(completed_event)
    assert await billing_service.get_balance_cents(org_id) == 5200

    refund_event = _stripe_event(
        "evt_test_refund1", "charge.refunded",
        {"payment_intent": "pi_test_refund1", "amount_refunded": 5000},
    )
    await payment_service.handle_charge_refunded(refund_event)

    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "refunded"
    assert await billing_service.get_balance_cents(org_id) == 0


@pytest.mark.asyncio
async def test_handle_charge_refunded_partial_refund_proportional(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_refund_partial", balance_cents=0)
    pack_id = await make_pack("scale_100b", 10000, 10500)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_refund2", stripe_customer_id="cus_w",
        amount_cents_paid=10000, currency="usd", credits_granted=10500,
    )
    completed_event = _stripe_event(
        "evt_test_refund2_pay", "checkout.session.completed",
        {
            "id": "cs_test_refund2", "payment_intent": "pi_test_refund2",
            "payment_status": "paid", "metadata": {"payment_id": str(payment.id)},
        },
    )
    await payment_service.handle_checkout_completed(completed_event)
    assert await billing_service.get_balance_cents(org_id) == 10500

    # 50% refund -> floor(10500 * 5000/10000) = 5250 credits clawed back.
    refund_event = _stripe_event(
        "evt_test_refund2", "charge.refunded",
        {"payment_intent": "pi_test_refund2", "amount_refunded": 5000},
    )
    await payment_service.handle_charge_refunded(refund_event)

    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "partially_refunded"
    assert await billing_service.get_balance_cents(org_id) == 10500 - 5250


@pytest.mark.asyncio
async def test_handle_charge_refunded_before_success_raises_retryable(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_refund_early", balance_cents=0)
    pack_id = await make_pack("starter_10d", 1000, 1000)

    from api.db import db_client

    await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_early", stripe_customer_id="cus_v",
        amount_cents_paid=1000, currency="usd", credits_granted=1000,
    )
    # No checkout.session.completed processed yet -> payment_intent_id unset,
    # so no PaymentModel row can be found by payment_intent lookup.
    refund_event = _stripe_event(
        "evt_test_early", "charge.refunded",
        {"payment_intent": "pi_never_recorded", "amount_refunded": 1000},
    )
    with pytest.raises(payment_service.RefundTooEarlyError):
        await payment_service.handle_charge_refunded(refund_event)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -k "checkout_completed or payment_failed or charge_refunded" -v`
Expected: FAIL — `AttributeError: module 'api.services.billing.payment_service' has no attribute 'handle_checkout_completed'`.

- [ ] **Step 3: Implement the handlers**

Append to `api/services/billing/payment_service.py`:

```python
class RefundTooEarlyError(Exception):
    """Raised when charge.refunded arrives before the payment is recorded succeeded.

    The route layer converts this into a 5xx so Stripe retries the refund event later
    rather than crediting a refund against a payment we haven't confirmed as paid.
    """


async def _already_processed(event_id: str, organization_id: int) -> bool:
    """Idempotency check mirroring billing_service.credit's own guard, so handlers
    can short-circuit before doing any PaymentModel work on a replay."""
    from api.db import db_client as _db_client

    existing = await _db_client.get_credit_balance_cents(organization_id)  # cheap no-op probe
    return existing is not None and False  # placeholder removed below


async def handle_checkout_completed(event: dict) -> None:
    session = event["data"]["object"]
    event_id = event["id"]

    payment_id = (session.get("metadata") or {}).get("payment_id")
    payment: PaymentModel | None = None
    if payment_id is not None:
        payment = await db_client.get_payment_by_id(int(payment_id))
    if payment is None:
        payment = await db_client.get_payment_by_checkout_session_id(session["id"])
    if payment is None:
        logger.error(
            "Stripe checkout.session.completed for unknown payment: session={} event={}",
            session.get("id"), event_id,
        )
        return

    if payment.status == "succeeded":
        return  # belt-and-suspenders alongside the ledger's own idempotency key

    if session.get("payment_status") != "paid":
        return  # e.g. async payment methods still pending; leave PaymentModel pending

    pack_key = (session.get("metadata") or {}).get("pack_key", "unknown")
    ledger_entry = await billing_service.credit(
        payment.organization_id,
        payment.credits_granted,
        "topup",
        description=f"Stripe payment {session.get('payment_intent')} ({pack_key})",
        idempotency_key=f"stripe:{event_id}",
    )

    await db_client.update_payment(
        payment.id,
        status="succeeded",
        stripe_payment_intent_id=session.get("payment_intent"),
        credit_ledger_id=ledger_entry.id,
    )


async def handle_payment_failed(event: dict) -> None:
    session_or_intent = event["data"]["object"]
    payment_id = (session_or_intent.get("metadata") or {}).get("payment_id")
    payment: PaymentModel | None = None
    if payment_id is not None:
        payment = await db_client.get_payment_by_id(int(payment_id))
    if payment is None and session_or_intent.get("id"):
        payment = await db_client.get_payment_by_checkout_session_id(
            session_or_intent["id"]
        )
    if payment is None:
        logger.error(
            "Stripe failure event for unknown payment: object={} event={}",
            session_or_intent.get("id"), event["id"],
        )
        return
    if payment.status == "succeeded":
        return

    failure_reason = (
        session_or_intent.get("last_payment_error", {}) or {}
    ).get("message") or event["type"]
    await db_client.update_payment(
        payment.id, status="failed", failure_reason=failure_reason
    )


async def handle_charge_refunded(event: dict) -> None:
    charge = event["data"]["object"]
    payment_intent_id = charge.get("payment_intent")
    payment = await db_client.get_payment_by_payment_intent_id(payment_intent_id)
    if payment is None or payment.status not in (
        "succeeded", "partially_refunded", "refunded",
    ):
        raise RefundTooEarlyError(
            f"refund for payment_intent={payment_intent_id} arrived before the "
            f"payment was recorded succeeded"
        )

    amount_refunded = int(charge["amount_refunded"])
    refunded_credits = (
        payment.credits_granted * amount_refunded // payment.amount_cents_paid
    )

    await billing_service.credit(
        payment.organization_id,
        -refunded_credits,
        "refund",
        description=(
            f"Stripe refund for payment {payment_intent_id} "
            f"({amount_refunded}/{payment.amount_cents_paid} cents)"
        ),
        idempotency_key=f"stripe:{event['id']}",
    )

    is_full_refund = amount_refunded >= payment.amount_cents_paid
    await db_client.update_payment(
        payment.id,
        status="refunded" if is_full_refund else "partially_refunded",
    )
```

Remove the `_already_processed` placeholder helper written above — it's not needed
because `billing_service.credit(...)` (Phase 1's `apply_ledger_entry`) already
short-circuits on a duplicate `idempotency_key`, and the explicit
`payment.status == "succeeded"` check on `PaymentModel` is the second, cheaper layer of
the "belt-and-suspenders" dedup described in the spec's webhook flow.

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -v`
Expected: all pass (14 total).

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/payment_service.py api/tests/test_payment_service.py
git commit -m "feat(payments): webhook handlers for checkout completed, failed, and refunded"
```

---

## Task 6: `POST /webhooks/stripe` — signature verification and dispatch

**Files:**
- Create: `api/routes/webhooks.py`
- Modify: `api/routes/main.py`
- Test: `api/tests/test_webhooks_stripe.py`

**Interfaces:**
- Consumes: `payment_service.handle_checkout_completed/handle_payment_failed/
  handle_charge_refunded`, `stripe.Webhook.construct_event`, `STRIPE_WEBHOOK_SECRET`,
  `BILLING_PAYMENTS_ENABLED`.
- Produces: `POST /webhooks/stripe`, no auth dependency, reads raw body via
  `await request.body()`, verifies `Stripe-Signature`, dispatches on `event["type"]`,
  returns `{"received": true}` on `200`.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_webhooks_stripe.py
"""ASGI-level tests for POST /webhooks/stripe: signature verification, idempotent
dispatch, and end-to-end ledger effects. Uses httpx ASGITransport against the real
app so raw-body handling and header parsing are exercised as in production. Stripe's
signature-verification function itself is real (constructs a genuine test signature
with a known webhook secret) — only the outbound Stripe SDK calls are mocked.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
import stripe
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.constants import STRIPE_WEBHOOK_SECRET
from api.db.models import OrganizationModel, PaymentModel, PaymentPackModel


def _signed_request_kwargs(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    header = stripe.WebhookSignature._compute_signature  # noqa: SLF001 -- test only, see note below
    # Use Stripe's own signing helper via a real construct_event round trip below
    # instead of hand-rolling HMAC; simpler and less brittle across SDK versions.
    return {"content": body}


async def _make_org_and_pack(real_db_factories, org_provider_id: str, pack_key: str):
    make_org, make_pack = real_db_factories
    org_id = await make_org(org_provider_id, balance_cents=0)
    pack_id = await make_pack(pack_key, 1000, 1000)
    return org_id, pack_id


def _sign(payload: bytes, secret: str) -> str:
    """Build a real Stripe-Signature header using the SDK's own signer, mirroring
    exactly what Stripe's servers send — avoids hand-rolled HMAC drifting from the
    SDK's verification implementation."""
    import time

    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload.decode()}"
    signature = stripe.WebhookSignature._compute_signature(signed_payload, secret)
    return f"t={timestamp},v1={signature}"


@pytest.fixture
async def real_db_factories(setup_test_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original_engine, original_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, session_factory

    created_org_ids: list[int] = []
    created_pack_ids: list[int] = []

    async def make_org(provider_id: str, balance_cents: int = 0) -> int:
        async with session_factory() as session:
            org = OrganizationModel(provider_id=provider_id, credit_balance_cents=balance_cents)
            session.add(org)
            await session.commit()
            await session.refresh(org)
            created_org_ids.append(org.id)
            return org.id

    async def make_pack(pack_key: str, price_cents: int, credits_granted: int) -> int:
        async with session_factory() as session:
            pack = PaymentPackModel(
                pack_key=pack_key, display_name=pack_key, price_cents=price_cents,
                credits_granted=credits_granted, currency="usd",
            )
            session.add(pack)
            await session.commit()
            await session.refresh(pack)
            created_pack_ids.append(pack.id)
            return pack.id

    yield make_org, make_pack

    from sqlalchemy import delete
    async with session_factory() as session:
        if created_org_ids:
            await session.execute(delete(OrganizationModel).where(OrganizationModel.id.in_(created_org_ids)))
        if created_pack_ids:
            await session.execute(delete(PaymentPackModel).where(PaymentPackModel.id.in_(created_pack_ids)))
        await session.commit()

    db_client.engine, db_client.async_session = original_engine, original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr("api.routes.webhooks.BILLING_PAYMENTS_ENABLED", True)
    payload = json.dumps({"id": "evt_bad", "type": "checkout.session.completed", "data": {"object": {}}}).encode()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_webhook_missing_signature_header_rejected(monkeypatch):
    monkeypatch.setattr("api.routes.webhooks.BILLING_PAYMENTS_ENABLED", True)
    payload = json.dumps({"id": "evt_nosig", "type": "checkout.session.completed", "data": {"object": {}}}).encode()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/api/v1/webhooks/stripe", content=payload)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_webhook_checkout_completed_credits_ledger(monkeypatch, real_db_factories):
    monkeypatch.setattr("api.routes.webhooks.BILLING_PAYMENTS_ENABLED", True)
    org_id, pack_id = await _make_org_and_pack(real_db_factories, "org_webhook_route", "starter_10")

    from api.db import db_client
    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_route_1", stripe_customer_id="cus_route_1",
        amount_cents_paid=1000, currency="usd", credits_granted=1000,
    )

    event_payload = {
        "id": "evt_route_1",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_route_1", "payment_intent": "pi_route_1", "payment_status": "paid",
            "metadata": {"payment_id": str(payment.id)},
        }},
    }
    body = json.dumps(event_payload).encode()
    signature = _sign(body, STRIPE_WEBHOOK_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )
    assert r.status_code == 200
    assert r.json() == {"received": True}

    from api.services.billing import billing_service
    assert await billing_service.get_balance_cents(org_id) == 1000

    # Replay the identical event: still exactly one credit.
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r2 = await client.post(
            "/api/v1/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )
    assert r2.status_code == 200
    assert await billing_service.get_balance_cents(org_id) == 1000


@pytest.mark.asyncio
async def test_webhook_flag_off_rejects(monkeypatch):
    monkeypatch.setattr("api.routes.webhooks.BILLING_PAYMENTS_ENABLED", False)
    payload = json.dumps({"id": "evt_off", "type": "checkout.session.completed", "data": {"object": {}}}).encode()
    signature = _sign(payload, STRIPE_WEBHOOK_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )
    assert r.status_code == 404
```

Note: drop the unused `_signed_request_kwargs` helper before finalizing — it was
superseded by `_sign` using Stripe's own signature computation, kept here only to show
the iteration; the implementer should delete it in Step 1's file so the test module has
no dead code.

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_webhooks_stripe.py -v`
Expected: FAIL — `404` for all requests (route not mounted) / `ModuleNotFoundError:
api.routes.webhooks`.

- [ ] **Step 3: Implement `api/routes/webhooks.py`**

```python
"""Unauthenticated, signature-verified webhook endpoints. Stripe calls these
server-to-server, so no user session/JWT auth applies here — the Stripe-Signature
header + STRIPE_WEBHOOK_SECRET is the only trust boundary.
"""

import stripe
from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from api.constants import BILLING_PAYMENTS_ENABLED, STRIPE_WEBHOOK_SECRET
from api.services.billing import payment_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_DISPATCH = {
    "checkout.session.completed": payment_service.handle_checkout_completed,
    "payment_intent.payment_failed": payment_service.handle_payment_failed,
    "checkout.session.expired": payment_service.handle_payment_failed,
    "charge.refunded": payment_service.handle_charge_refunded,
}


@router.post("/stripe")
async def stripe_webhook(request: Request):
    if not BILLING_PAYMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    raw_body = await request.body()
    signature = request.headers.get("stripe-signature")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    try:
        event = stripe.Webhook.construct_event(
            raw_body, signature, STRIPE_WEBHOOK_SECRET
        )
    except (stripe.error.SignatureVerificationError, ValueError) as exc:
        logger.warning("Stripe webhook signature verification failed: {}", exc)
        raise HTTPException(status_code=400, detail="Invalid signature") from exc

    handler = _DISPATCH.get(event["type"])
    if handler is None:
        # Unhandled event type (e.g. payment_intent.succeeded, charge.dispute.created
        # in v1) -> ack so Stripe stops retrying; nothing to do here yet.
        logger.debug("Stripe webhook: ignoring unhandled event type {}", event["type"])
        return {"received": True}

    try:
        await handler(event)
    except payment_service.RefundTooEarlyError as exc:
        logger.warning("Stripe webhook deferred, will retry: {}", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"received": True}
```

- [ ] **Step 4: Mount the router**

In `api/routes/main.py`, add `from api.routes.webhooks import router as webhooks_router`
next to the other route imports, and `router.include_router(webhooks_router)` next to
the other `include_router` calls (mirroring `billing_admin_router`'s placement).

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_webhooks_stripe.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/routes/webhooks.py api/routes/main.py api/tests/test_webhooks_stripe.py
git commit -m "feat(payments): POST /webhooks/stripe with signature verification and dispatch"
```

---

## Task 7: `charge.dispute.created` — flag for manual review (no auto-clawback)

**Files:**
- Modify: `api/services/billing/payment_service.py`, `api/routes/webhooks.py`
- Test: extend `api/tests/test_payment_service.py`, `api/tests/test_webhooks_stripe.py`

**Interfaces:**
- Consumes: `db_client.update_payment` (Task 3).
- Produces: `async def handle_dispute_created(event: dict) -> None` — sets
  `PaymentModel.status = "disputed"` (new allowed status value; no schema change since
  `status` is a plain `String`, not a DB enum) and `failure_reason = f"chargeback:
  {dispute_id}"`; **no ledger write**.

- [ ] **Step 1: Write the failing test**

```python
# append to api/tests/test_payment_service.py
@pytest.mark.asyncio
async def test_handle_dispute_created_flags_no_clawback(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_pay_dispute", balance_cents=0)
    pack_id = await make_pack("starter_10e", 1000, 1000)

    from api.db import db_client

    payment = await db_client.create_payment(
        organization_id=org_id, payment_pack_id=pack_id,
        stripe_checkout_session_id="cs_test_dispute", stripe_customer_id="cus_dispute",
        amount_cents_paid=1000, currency="usd", credits_granted=1000,
    )
    completed_event = _stripe_event(
        "evt_test_dispute_pay", "checkout.session.completed",
        {
            "id": "cs_test_dispute", "payment_intent": "pi_test_dispute",
            "payment_status": "paid", "metadata": {"payment_id": str(payment.id)},
        },
    )
    await payment_service.handle_checkout_completed(completed_event)
    assert await billing_service.get_balance_cents(org_id) == 1000

    dispute_event = _stripe_event(
        "evt_test_dispute", "charge.dispute.created",
        {"id": "dp_test_1", "payment_intent": "pi_test_dispute"},
    )
    await payment_service.handle_dispute_created(dispute_event)

    updated = await db_client.get_payment_by_id(payment.id)
    assert updated.status == "disputed"
    assert "dp_test_1" in updated.failure_reason
    # No automatic clawback -- balance unchanged.
    assert await billing_service.get_balance_cents(org_id) == 1000
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -k dispute_created -v`
Expected: FAIL — `AttributeError: module has no attribute 'handle_dispute_created'`.

- [ ] **Step 3: Implement**

Append to `api/services/billing/payment_service.py`:

```python
async def handle_dispute_created(event: dict) -> None:
    """Chargebacks are not auto-processed against the ledger in v1 -- flagged for
    manual ops review since a chargeback often correlates with fraud and warrants a
    human decision, not an automatic silent credit clawback."""
    dispute = event["data"]["object"]
    payment_intent_id = dispute.get("payment_intent")
    payment = await db_client.get_payment_by_payment_intent_id(payment_intent_id)
    if payment is None:
        logger.error(
            "Stripe charge.dispute.created for unknown payment_intent={} event={}",
            payment_intent_id, event["id"],
        )
        return

    await db_client.update_payment(
        payment.id,
        status="disputed",
        failure_reason=f"chargeback: {dispute.get('id')}",
    )
    logger.warning(
        "Payment {} disputed (chargeback {}) -- flagged for manual ops review",
        payment.id, dispute.get("id"),
    )
```

In `api/routes/webhooks.py`, add to `_DISPATCH`:

```python
    "charge.dispute.created": payment_service.handle_dispute_created,
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/payment_service.py api/routes/webhooks.py api/tests/test_payment_service.py
git commit -m "feat(payments): flag disputed payments for manual review on chargeback"
```

---

## Task 8: Customer-facing routes — `GET /billing/packs`, `POST /billing/checkout`, `GET /billing/payments`

**Files:**
- Create: `api/routes/billing.py`
- Modify: `api/routes/main.py`
- Test: `api/tests/test_billing_routes.py`

**Interfaces:**
- Consumes: `get_user_with_selected_organization` (`api/services/auth/depends.py:159`),
  `payment_service`, `db_client.get_organization_by_id`, `BILLING_PAYMENTS_ENABLED`,
  `UI_APP_URL` (existing constant, used to build `success_url`/`cancel_url`).
- Produces:
  - `GET /billing/packs` → `{"packs": [...]}` per the spec's contract.
  - `POST /billing/checkout` body `{"pack_key": str}` → `{"checkout_url": str}`; `404` for
    unknown/inactive pack, `409` for `DuplicateCheckoutError`.
  - `GET /billing/payments?cursor=&limit=` → `{"payments": [...], "next_cursor": int|None}`.

- [ ] **Step 1: Write the failing tests**

Follow the existing repo convention for route-level tests (direct function calls with
monkeypatched dependencies, matching `api/tests/test_organization_usage_billing.py`)
rather than a full ASGI client, since these routes don't need raw-body/signature
handling like the webhook does:

```python
# api/tests/test_billing_routes.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import billing


@pytest.mark.asyncio
async def test_get_packs_returns_catalog(monkeypatch):
    pack = SimpleNamespace(
        pack_key="starter_10", display_name="Starter", price_cents=1000,
        credits_granted=1000, currency="usd",
    )
    monkeypatch.setattr(
        billing.payment_service, "list_active_packs", AsyncMock(return_value=[pack])
    )

    result = await billing.get_packs()
    assert result["packs"][0]["pack_key"] == "starter_10"


@pytest.mark.asyncio
async def test_post_checkout_unknown_pack_returns_404(monkeypatch):
    monkeypatch.setattr(
        billing.db_client, "get_payment_pack_by_key", AsyncMock(return_value=None)
    )
    user = SimpleNamespace(id=1, selected_organization_id=9)

    with pytest.raises(billing.HTTPException) as exc_info:
        await billing.post_checkout(billing.CheckoutRequest(pack_key="nope"), user=user)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_post_checkout_duplicate_returns_409(monkeypatch):
    pack = SimpleNamespace(id=1, pack_key="growth_50", is_active=True)
    org = SimpleNamespace(id=9, stripe_customer_id="cus_x")
    monkeypatch.setattr(
        billing.db_client, "get_payment_pack_by_key", AsyncMock(return_value=pack)
    )
    monkeypatch.setattr(
        billing.db_client, "get_organization_by_id", AsyncMock(return_value=org)
    )
    monkeypatch.setattr(
        billing.payment_service,
        "create_checkout_session",
        AsyncMock(side_effect=billing.payment_service.DuplicateCheckoutError("dup")),
    )
    user = SimpleNamespace(id=1, selected_organization_id=9)

    with pytest.raises(billing.HTTPException) as exc_info:
        await billing.post_checkout(billing.CheckoutRequest(pack_key="growth_50"), user=user)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_post_checkout_success_returns_url(monkeypatch):
    pack = SimpleNamespace(id=1, pack_key="growth_50", is_active=True)
    org = SimpleNamespace(id=9, stripe_customer_id="cus_x")
    monkeypatch.setattr(
        billing.db_client, "get_payment_pack_by_key", AsyncMock(return_value=pack)
    )
    monkeypatch.setattr(
        billing.db_client, "get_organization_by_id", AsyncMock(return_value=org)
    )
    monkeypatch.setattr(
        billing.payment_service,
        "create_checkout_session",
        AsyncMock(
            return_value=billing.payment_service.CheckoutSessionResult(
                checkout_url="https://checkout.stripe.com/c/pay/cs_test_x", payment_id=1
            )
        ),
    )
    user = SimpleNamespace(id=1, selected_organization_id=9)

    result = await billing.post_checkout(
        billing.CheckoutRequest(pack_key="growth_50"), user=user
    )
    assert result["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_x"


@pytest.mark.asyncio
async def test_get_payments_scopes_to_selected_org(monkeypatch):
    payment = SimpleNamespace(
        id=42, payment_pack_id=1, amount_cents_paid=5000, currency="usd",
        credits_granted=5200, status="succeeded",
        created_at=__import__("datetime").datetime(2026, 7, 20, 10, 15, tzinfo=__import__("datetime").UTC),
    )
    pack = SimpleNamespace(pack_key="growth_50")
    list_payments = AsyncMock(return_value=[payment])
    monkeypatch.setattr(billing.db_client, "list_payments_for_org", list_payments)
    monkeypatch.setattr(
        billing.db_client, "get_payment_pack_by_id", AsyncMock(return_value=pack)
    )
    user = SimpleNamespace(id=1, selected_organization_id=9)

    result = await billing.get_payments(user=user)
    list_payments.assert_awaited_once_with(9, limit=50, cursor=None)
    assert result["payments"][0]["pack_key"] == "growth_50"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: api.routes.billing`.

- [ ] **Step 3: Add `get_payment_pack_by_id` to `PaymentClient`**

In `api/db/payment_client.py`, add (used by the payment-history route to resolve
`pack_key` for display):

```python
    async def get_payment_pack_by_id(
        self, pack_id: int
    ) -> Optional[PaymentPackModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentPackModel).where(PaymentPackModel.id == pack_id)
            )
            return result.scalars().first()
```

- [ ] **Step 4: Implement `api/routes/billing.py`**

```python
"""Customer-facing, org-scoped billing routes: pack catalog, checkout, payment
history. Balance itself is not duplicated here -- the UI reads Phase 1's existing
balance endpoint on api/routes/organization_usage.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.constants import BILLING_PAYMENTS_ENABLED, UI_APP_URL
from api.db import db_client
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing import payment_service

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    pack_key: str


def _require_payments_enabled() -> None:
    if not BILLING_PAYMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/packs")
async def get_packs():
    _require_payments_enabled()
    packs = await payment_service.list_active_packs()
    return {
        "packs": [
            {
                "pack_key": p.pack_key,
                "display_name": p.display_name,
                "price_cents": p.price_cents,
                "credits_granted": p.credits_granted,
                "currency": p.currency,
            }
            for p in packs
        ]
    }


@router.post("/checkout")
async def post_checkout(
    body: CheckoutRequest,
    user=Depends(get_user_with_selected_organization),
):
    _require_payments_enabled()
    pack = await db_client.get_payment_pack_by_key(body.pack_key)
    if pack is None or not getattr(pack, "is_active", True):
        raise HTTPException(status_code=404, detail="Unknown or inactive pack_key")

    org = await db_client.get_organization_by_id(user.selected_organization_id)
    success_url = f"{UI_APP_URL}/billing?checkout=success"
    cancel_url = f"{UI_APP_URL}/billing?checkout=cancelled"

    try:
        result = await payment_service.create_checkout_session(
            org, pack, success_url=success_url, cancel_url=cancel_url
        )
    except payment_service.DuplicateCheckoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"checkout_url": result.checkout_url}


@router.get("/payments")
async def get_payments(
    limit: int = Query(default=50, le=100),
    cursor: int | None = Query(default=None),
    user=Depends(get_user_with_selected_organization),
):
    _require_payments_enabled()
    payments = await db_client.list_payments_for_org(
        user.selected_organization_id, limit=limit, cursor=cursor
    )

    rows = []
    for payment in payments:
        pack_key = None
        if payment.payment_pack_id is not None:
            pack = await db_client.get_payment_pack_by_id(payment.payment_pack_id)
            pack_key = pack.pack_key if pack is not None else None
        rows.append(
            {
                "id": payment.id,
                "pack_key": pack_key,
                "amount_cents_paid": payment.amount_cents_paid,
                "currency": payment.currency,
                "credits_granted": payment.credits_granted,
                "status": payment.status,
                "created_at": payment.created_at.isoformat()
                if payment.created_at
                else None,
            }
        )

    next_cursor = payments[-1].id if len(payments) == limit else None
    return {"payments": rows, "next_cursor": next_cursor}
```

- [ ] **Step 5: Mount the router**

In `api/routes/main.py`, add `from api.routes.billing import router as billing_router`
and `router.include_router(billing_router)`.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_routes.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add api/routes/billing.py api/routes/main.py api/db/payment_client.py api/tests/test_billing_routes.py
git commit -m "feat(payments): customer-facing pack catalog, checkout, and payment history routes"
```

---

## Task 9: Feature-flag gating end-to-end (`BILLING_PAYMENTS_ENABLED=false` and `BILLING_ENGINE != local`)

**Files:**
- Test: extend `api/tests/test_billing_routes.py`, `api/tests/test_webhooks_stripe.py`

**Interfaces:**
- Consumes: `BILLING_PAYMENTS_ENABLED` (Task 1), the `_require_payments_enabled` guard
  (Task 8), the webhook route's own guard (Task 6).

- [ ] **Step 1: Write the failing tests**

```python
# append to api/tests/test_billing_routes.py
@pytest.mark.asyncio
async def test_get_packs_404s_when_payments_disabled(monkeypatch):
    monkeypatch.setattr(billing, "BILLING_PAYMENTS_ENABLED", False)
    with pytest.raises(billing.HTTPException) as exc_info:
        await billing.get_packs()
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_post_checkout_404s_when_payments_disabled(monkeypatch):
    monkeypatch.setattr(billing, "BILLING_PAYMENTS_ENABLED", False)
    user = SimpleNamespace(id=1, selected_organization_id=9)
    with pytest.raises(billing.HTTPException) as exc_info:
        await billing.post_checkout(billing.CheckoutRequest(pack_key="x"), user=user)
    assert exc_info.value.status_code == 404
```

(The webhook-off test, `test_webhook_flag_off_rejects`, was already written in Task 6 —
confirm it still passes here as part of the full-flag-off sweep.)

- [ ] **Step 2: Run to verify failure, then pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_routes.py -k disabled -v`
Expected: FAIL first (guard imported but `BILLING_PAYMENTS_ENABLED` module attribute
not patchable if it's only read at import time inside the constant, not re-read per
request) — if it fails with the guard *not* raising, switch `_require_payments_enabled`
to read `billing.BILLING_PAYMENTS_ENABLED` (module-level name, already the case since it
was imported via `from api.constants import BILLING_PAYMENTS_ENABLED` into the `billing`
module namespace, so `monkeypatch.setattr(billing, "BILLING_PAYMENTS_ENABLED", False)`
correctly shadows it). Then rerun — expect PASS.

- [ ] **Step 3: Commit**

```bash
git add api/tests/test_billing_routes.py
git commit -m "test(payments): verify all payment routes 404 when BILLING_PAYMENTS_ENABLED=false"
```

---

## Task 10: Frontend billing page — pack grid, checkout redirect, payment history

**Files:**
- Create: `ui/src/app/billing/page.tsx` (or the org-scoped authenticated-app-shell
  location mirroring existing pages — confirm exact path by inspecting
  `ui/src/app/` structure before writing)
- Create: `ui/src/app/billing/actions.ts` or a fetch hook consistent with existing
  patterns for calling `/organizations/usage/credits` (Phase 1) and the new `/billing/*`
  endpoints
- Test: co-located component test per the repo's existing frontend test convention

**Interfaces:**
- Consumes: `GET /billing/packs`, `POST /billing/checkout`, `GET /billing/payments`,
  Phase 1's balance read endpoint.
- Produces: a billing page with a balance banner, pack grid with "Buy" buttons
  (full-page redirect to `checkout_url`), and a payment-history table, per the spec's
  Components section 4.

- [ ] **Step 1: Locate the UI's existing org-scoped authenticated page pattern**

Run: `find ui/src/app -maxdepth 2 -type d | head -30` and
`grep -rln "get_user_with_selected_organization\|organizations/usage" ui/src --include="*.tsx" | head`
Expected: shows the closest existing analog (e.g. a usage/dashboard page) whose
data-fetching and layout conventions this page should mirror.

- [ ] **Step 2: Write the failing component test**

Follow whatever the repo's existing frontend test runner/pattern is (confirm via
`grep -rn "\"test\"" ui/package.json` and an existing `*.test.tsx` file next to a
comparable page) before writing assertions — at minimum: pack grid renders fetched
packs, "Buy" triggers `window.location.href = checkout_url` from a mocked
`POST /billing/checkout` response, and the payment-history table renders rows from a
mocked `GET /billing/payments` response.

- [ ] **Step 3: Implement the page**

Balance banner reads the Phase 1 balance endpoint; pack grid reads `GET /billing/packs`
and posts to `/billing/checkout` on "Buy", then does `window.location.href =
checkout_url`; success/cancel query params (`?checkout=success` / `?checkout=cancelled`)
drive a "processing" toast that polls the balance endpoint a few times on success per
the spec's Error handling section ("redirect without webhook").

- [ ] **Step 4: Run the frontend test suite**

Run whatever command the repo uses (confirm via `package.json`, typically
`npm run test` or `pnpm test` from `ui/`).
Expected: new tests pass; no regressions in existing suite.

- [ ] **Step 5: Commit**

```bash
git add ui/src/app/billing/
git commit -m "feat(payments): billing page with pack grid, checkout redirect, and payment history"
```

---

## Task 11: Seed the pack catalog + superuser pack management (operator ergonomics)

**Files:**
- Modify: `api/routes/billing_admin.py`
- Test: extend an existing superuser-route test file, or create
  `api/tests/test_billing_admin_payment_routes.py`

**Interfaces:**
- Consumes: `db_client.create_payment_pack`, `db_client.list_payment_packs` (new,
  superuser-facing — distinct from the customer-facing `list_active_payment_packs`),
  `get_superuser`.
- Produces:
  - `POST /superuser/payment-packs` body = pack fields → creates a `PaymentPackModel`
    row (operators seed `starter_10`/`growth_50`/`scale_100` this way; no fixture/seed
    script needed since the table is empty-by-default and packs are operator-managed).
  - `GET /superuser/payment-packs` → all packs including inactive ones.
  - `PATCH /superuser/payment-packs/{pack_id}` body = partial fields (e.g.
    `{"is_active": false}`) → updates a pack (retire without delete, per the spec's
    `is_active` semantics).

- [ ] **Step 1: Add the DB methods**

In `api/db/payment_client.py`, add:

```python
    async def create_payment_pack(self, **fields) -> PaymentPackModel:
        async with self.async_session() as session:
            pack = PaymentPackModel(**fields)
            session.add(pack)
            await session.commit()
            await session.refresh(pack)
            return pack

    async def list_payment_packs(self) -> list[PaymentPackModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentPackModel).order_by(PaymentPackModel.sort_order)
            )
            return list(result.scalars().all())

    async def update_payment_pack(self, pack_id: int, **fields) -> PaymentPackModel:
        async with self.async_session() as session:
            result = await session.execute(
                select(PaymentPackModel).where(PaymentPackModel.id == pack_id)
            )
            pack = result.scalars().one()
            for key, value in fields.items():
                setattr(pack, key, value)
            await session.commit()
            await session.refresh(pack)
            return pack
```

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_billing_admin_payment_routes.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes import billing_admin


@pytest.mark.asyncio
async def test_create_payment_pack(monkeypatch):
    created = SimpleNamespace(id=1, pack_key="starter_10")
    monkeypatch.setattr(
        billing_admin.db_client, "create_payment_pack", AsyncMock(return_value=created)
    )
    user = SimpleNamespace(id=1)

    result = await billing_admin.create_payment_pack(
        billing_admin.PaymentPackRequest(
            pack_key="starter_10", display_name="Starter",
            price_cents=1000, credits_granted=1000,
        ),
        user=user,
    )
    assert result["id"] == 1


@pytest.mark.asyncio
async def test_update_payment_pack_deactivates(monkeypatch):
    updated = SimpleNamespace(id=1, pack_key="starter_10", is_active=False)
    update_mock = AsyncMock(return_value=updated)
    monkeypatch.setattr(billing_admin.db_client, "update_payment_pack", update_mock)
    user = SimpleNamespace(id=1)

    result = await billing_admin.update_payment_pack(
        1, billing_admin.PaymentPackUpdateRequest(is_active=False), user=user
    )
    update_mock.assert_awaited_once_with(1, is_active=False)
    assert result["is_active"] is False
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_payment_routes.py -v`
Expected: FAIL — `AttributeError: module 'api.routes.billing_admin' has no attribute 'PaymentPackRequest'`.

- [ ] **Step 4: Implement**

In `api/routes/billing_admin.py`, add:

```python
from api.db.models import PaymentPackModel


class PaymentPackRequest(BaseModel):
    pack_key: str
    display_name: str
    price_cents: int
    credits_granted: int
    currency: str = "usd"
    sort_order: int = 0


class PaymentPackUpdateRequest(BaseModel):
    display_name: str | None = None
    price_cents: int | None = None
    credits_granted: int | None = None
    is_active: bool | None = None
    sort_order: int | None = None


def _pack_row(pack: PaymentPackModel) -> dict:
    return {
        "id": pack.id,
        "pack_key": pack.pack_key,
        "display_name": pack.display_name,
        "price_cents": pack.price_cents,
        "credits_granted": pack.credits_granted,
        "currency": pack.currency,
        "is_active": pack.is_active,
        "sort_order": pack.sort_order,
    }


@router.post("/payment-packs")
async def create_payment_pack(body: PaymentPackRequest, user=Depends(get_superuser)):
    pack = await db_client.create_payment_pack(**body.model_dump())
    return _pack_row(pack)


@router.get("/payment-packs")
async def list_payment_packs(user=Depends(get_superuser)):
    packs = await db_client.list_payment_packs()
    return [_pack_row(p) for p in packs]


@router.patch("/payment-packs/{pack_id}")
async def update_payment_pack(
    pack_id: int, body: PaymentPackUpdateRequest, user=Depends(get_superuser)
):
    fields = body.model_dump(exclude_none=True)
    pack = await db_client.update_payment_pack(pack_id, **fields)
    return _pack_row(pack)
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_payment_routes.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/routes/billing_admin.py api/db/payment_client.py api/tests/test_billing_admin_payment_routes.py
git commit -m "feat(payments): superuser payment-pack management endpoints"
```

---

## Task 12: Full-suite regression + end-to-end checkout-to-ledger lifecycle test

**Files:**
- Test: `api/tests/test_payments_lifecycle.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the lifecycle test**

```python
# api/tests/test_payments_lifecycle.py
"""End-to-end: pack -> checkout -> webhook -> ledger -> payment history, exercising
every layer together the way a real Stripe purchase would."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import stripe

from api.constants import STRIPE_WEBHOOK_SECRET
from api.services.billing import billing_service, payment_service


@pytest.mark.asyncio
async def test_pack_to_checkout_to_webhook_to_ledger(real_db):  # real_db fixture defined in test_payment_service.py's module scope; reuse via conftest promotion if needed, else duplicate the fixture here.
    pass
```

Note for the implementer: promote the `real_db` fixture from
`api/tests/test_payment_service.py` into `api/tests/conftest.py` (or a shared
`api/tests/billing_fixtures.py` imported by both modules) before writing this task, so
it's not duplicated a third time. Then write:

```python
@pytest.mark.asyncio
async def test_pack_to_checkout_to_webhook_to_ledger(real_db):
    make_org, make_pack = real_db
    org_id = await make_org("org_lifecycle_payments", balance_cents=0)
    await make_pack("growth_50", 5000, 5200)

    from api.db import db_client

    pack = await db_client.get_payment_pack_by_key("growth_50")
    org = SimpleNamespace(id=org_id, stripe_customer_id=None)

    fake_customer = SimpleNamespace(id="cus_lifecycle")
    fake_session = SimpleNamespace(id="cs_lifecycle", url="https://checkout/cs_lifecycle")
    with (
        patch.object(payment_service.stripe.Customer, "create_async", AsyncMock(return_value=fake_customer)),
        patch.object(payment_service.stripe.checkout.Session, "create_async", AsyncMock(return_value=fake_session)),
        patch.object(payment_service.stripe.checkout.Session, "modify_async", AsyncMock()),
    ):
        result = await payment_service.create_checkout_session(
            org, pack, success_url="s", cancel_url="c"
        )

    payment = await db_client.get_payment_by_id(result.payment_id)
    assert payment.status == "pending"

    event = {
        "id": "evt_lifecycle_1",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_lifecycle", "payment_intent": "pi_lifecycle",
            "payment_status": "paid",
            "metadata": {"payment_id": str(payment.id)},
        }},
    }
    await payment_service.handle_checkout_completed(event)

    assert await billing_service.get_balance_cents(org_id) == 5200
    history = await db_client.list_payments_for_org(org_id, limit=10, cursor=None)
    assert history[0].status == "succeeded"
    assert history[0].credits_granted == 5200
```

- [ ] **Step 2: Run the full payments suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_payment_service.py api/tests/test_webhooks_stripe.py api/tests/test_billing_routes.py api/tests/test_billing_admin_payment_routes.py api/tests/test_payments_lifecycle.py -v`
Expected: all pass.

- [ ] **Step 3: Run the broader suite to check for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q -x`
Expected: no new failures introduced (pre-existing unrelated failures, if any, noted but
not caused by this work).

- [ ] **Step 4: Commit**

```bash
git add api/tests/test_payments_lifecycle.py
git commit -m "test(payments): end-to-end pack->checkout->webhook->ledger lifecycle"
```

---

## Task 13: Migration double-check + downgrade path verification

**Files:**
- Verify: `api/alembic/versions/<phase-3-revision>_add_stripe_payment_packs_and_payments_tables.py`

**Interfaces:**
- Confirms: `down_revision == "b1f0c0de0001"` (Phase 1's head), forward migration applies
  cleanly on top of Phase 1's schema, and `downgrade()` cleanly reverses it.

- [ ] **Step 1: Confirm the chain**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && alembic -c api/alembic.ini history | head -5`
Expected: the newest line shows this phase's revision with `b1f0c0de0001` as its parent,
confirming a single linear chain with no branch point.

- [ ] **Step 2: Verify `downgrade()` is implemented and symmetric**

Open the migration file and confirm `downgrade()` drops `payments`, then
`payment_packs`, then the `organizations.stripe_customer_id` column (reverse order of
`upgrade()`'s creation), mirroring Task 2 Step 1's `CreditLedgerModel`/`PricingRuleModel`
downgrade pattern from Phase 1's migration.

- [ ] **Step 3: Round-trip on the test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh && alembic -c api/alembic.ini downgrade -1 && alembic -c api/alembic.ini upgrade head`
Expected: both commands succeed with no errors; final state matches Task 2 Step 5's
`to_regclass` check.

- [ ] **Step 4: Commit (only if Step 2 required a fix)**

```bash
git add api/alembic/versions/
git commit -m "fix(payments): correct downgrade order for payments migration"
```

(Skip this commit if no fix was needed — the migration was already committed correctly
in Task 2.)

---

## Self-Review

**Spec coverage check (against `phase-3-payments-topups.md`):**
- Prepaid credit packs via Stripe Checkout (`mode=payment`, no subscriptions) → Tasks 2
  (models), 4 (`create_checkout_session`). ✓
- Exactly-once credit on confirmed payment, replay-safe → Task 5
  (`handle_checkout_completed` dedup via `PaymentModel.status` + Phase 1's ledger
  `idempotency_key`), Task 6 (webhook replay test), Task 12 (E2E). ✓
- Balance + payment history reads → Task 8 (`GET /billing/payments`); balance itself
  reuses Phase 1's existing endpoint, not duplicated, per the spec. ✓
- Refunds → negative ledger row, accurate `PaymentModel.status` → Task 5
  (`handle_charge_refunded`, proportional math, full vs. partial). ✓
- Failed/canceled checkout never credits → Task 5 (`handle_payment_failed`). ✓
- Feature-flagged (`BILLING_PAYMENTS_ENABLED`, off by default) → Task 1 (flag), Task 9
  (explicit off-path tests across all three route groups + webhook). ✓
- Stripe signature verification on raw body, `400` on failure → Task 6. ✓
- Idempotency keyed on Stripe event id (`stripe:{event_id}`) reusing Phase 1's
  `(organization_id, idempotency_key)` ledger constraint → Task 5, no schema change
  needed (confirmed against Phase 1's `CreditLedgerModel`). ✓
- Out-of-order refund-before-success handling (retryable, not silently dropped) → Task 5
  (`RefundTooEarlyError`), Task 6 (route converts to `409`/retryable). ✓
- Duplicate-click guard on checkout (soft `409`) → Task 4
  (`find_pending_payment`/`DuplicateCheckoutError`), Task 8 (route mapping). ✓
- Chargebacks flagged for manual review, no auto-clawback → Task 7. ✓
- Stale/removed pack doesn't change in-flight purchase → Task 2 (`PaymentModel` copies
  pack fields at creation time, not a live FK-only reference for amounts). ✓
- Superuser pack catalog CRUD (operators add/retire packs without a deploy) → Task 11. ✓
- Frontend billing page (balance banner, pack grid, redirect, history table) → Task 10. ✓
- Migration chained after Phase 1's `b1f0c0de0001` head → Task 2 Step 4, verified
  end-to-end in Task 13. ✓
- **Deferred per spec's Open Questions (not built here, and rightly so):** multi-currency
  packs/FX, auto-topup (saved payment method + threshold trigger), GST-compliant
  invoicing beyond Stripe's built-in receipts, automated chargeback clawback,
  non-superuser billing admin roles. All explicitly out of scope in the spec; not
  included in any task above.
- **Noted but only partially covered — reconciliation sweep:** the spec's Error handling
  section describes a background job (ARQ) sweeping `PaymentModel` rows stuck `pending`
  for >10 minutes and self-healing via `stripe.checkout.Session.retrieve`. This plan does
  **not** include a task for it — it's an operational safety net for production rollout
  (spec's Rollout step 4), not required for the core payment rail to function correctly
  or to be fully tested. Flagged here as a deliberate scope cut, not an oversight; add as
  a follow-up task (`api/services/billing/reconcile_payments.py` + an ARQ cron
  registration) before flipping `BILLING_PAYMENTS_ENABLED` on in production, per the
  spec's own Rollout section.

**Placeholder scan:** none in the primary implementation — every code step contains real,
complete code. Task 6's test file explicitly calls out and removes one intentionally-
superseded helper (`_signed_request_kwargs`) so no dead code ships; Task 12 explicitly
instructs promoting the shared `real_db` fixture rather than leaving a stub.

**Type/interface consistency:** `CheckoutSessionResult` used identically in Tasks 4, 8,
12. `PackNotFoundError`/`DuplicateCheckoutError`/`RefundTooEarlyError` raised in Task 4/5
and caught in Task 6/8 with matching import paths (`payment_service.<ExceptionName>`).
`stripe:{event_id}` idempotency-key format used identically in Task 5's two credit calls
and validated in Task 6's replay test. `PaymentModel.status` value set
(`pending`/`succeeded`/`failed`/`refunded`/`partially_refunded`/`disputed`) is consistent
across Tasks 2, 3, 5, 7, 8 — `disputed` is the one value added beyond the spec's literal
enum list in Data Models, needed to satisfy Task 7's flagging requirement; called out
explicitly in Task 7 rather than silently introduced.

**Note for implementer:** two places defer to runtime inspection rather than being
fully prescribed, both with explicit verification steps: (1) the exact
`stripe==X.Y.Z` pin in Task 1 Step 3 — confirm the latest stable version compatible with
`fastapi==0.135.3`'s Python/async stack at implementation time rather than trusting the
version number written here; (2) the frontend's exact page-directory convention in Task
10 Step 1 — confirm against the live `ui/src/app/` tree before creating files, since this
plan was written from the backend spec and did not inventory the frontend structure.
