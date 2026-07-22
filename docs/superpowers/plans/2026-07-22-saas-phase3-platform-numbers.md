# SaaS Phase 3 — Platform Twilio Numbers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "Get a number": search/purchase/release Twilio numbers from a platform Twilio account (subaccount per org), with monthly rental billed from the credit ledger, per-minute carrier burn on top of AI minutes, auto-renewal via an ARQ cron job with grace-period release, inbound routing to a selected agent, and superadmin oversight.

**Architecture:** Each org gets a Twilio **subaccount** created lazily on first purchase; its credentials are stored as a normal `TelephonyConfigurationModel` row flagged `is_platform=True`. Purchased numbers become normal `TelephonyPhoneNumberModel` rows (flagged platform, with rental columns), so the ENTIRE existing machinery — inbound resolver `find_inbound_route_by_account`, Twilio signature verification, caller-ID pools, outbound transport — works unchanged. A new `PlatformTwilioClient` (aiohttp, mirroring `TwilioProvider`'s raw-REST style) talks to Twilio's search/subaccount/purchase/release APIs. A daily ARQ cron charges rentals idempotently and releases numbers after grace. Post-call, platform-number runs get an extra carrier debit. All new behavior is gated on `IS_SAAS_MODE` — OSS untouched.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, aiohttp (Twilio REST), ARQ cron, pytest (`real_db` committing-fixture pattern), Next.js 15 + shadcn/ui + generated client.

**Spec:** `docs/superpowers/specs/2026-07-21-saas-platform-design.md` §8 "Platform numbers" (phase 3 of §13).

## Global Constraints

- Saas-only: every new route/service short-circuits unless `IS_SAAS_MODE` (`api/constants.py:35`). OSS behavior byte-for-byte unchanged.
- Org scoping (api/AGENTS.md): every read/write of org-scoped rows filters by `organization_id`; webhook-derived org resolution stays validated by the existing `find_inbound_route_by_account`.
- Ledger writes only via `billing_service.credit(...)` / existing debit helpers. New ledger `type` values: `number_rental`, `carrier_debit`. Idempotency keys: `number_rental:{phone_id}:initial`, `number_rental:{phone_id}:{YYYY-MM-DD}` (renewal date), `carrier:{workflow_run_id}`.
- 1 minute = 100 ledger cents (`CENTS_PER_MINUTE`, `api/services/billing/trial.py:9`); rentals are priced in minutes-equivalent.
- Countries at launch: US, CA, IN only (`PLATFORM_NUMBER_COUNTRIES` env, default `"US,CA,IN"`). India regulatory bundles are handled manually — surface Twilio's error message verbatim on purchase failure.
- The ledger's `apply_ledger_entry` clamps balances at 0 (`api/db/billing_client.py:86-93`) — always pre-check balance to *detect* insufficiency before debiting.
- Routes stay thin; domain logic in `api/services/` (api/AGENTS.md).
- Tests: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/<file> -v` from repo root. Run `find . -name '._*' -delete` before full-suite runs (external-volume quirk).
- Commit after every task (`feat(saas-p3):` prefix). Never `--no-verify`.
- Twilio HTTP is NEVER called in tests — `PlatformTwilioClient` is always mocked.

**Decisions locked during planning** (owner can veto):
1. Subaccount credentials live in a flagged `TelephonyConfigurationModel` (name `"Platform numbers"`, provider `"twilio"`, `is_platform=True`) so inbound routing/signatures/outbound reuse existing code paths. Users cannot edit/delete platform configs or platform numbers through the generic telephony routes (403 guards).
2. Rental pricing: env-driven minutes-equivalent per country — `PLATFORM_NUMBER_RENTAL_MINUTES_US=10`, `_CA=10`, `_IN=20` (fallback `PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT=10`). The price is locked into the row (`rental_cents`) at purchase time.
3. Carrier burn: flat `PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE=50` (0.5 min-equivalent per minute) debited post-call as a separate `carrier_debit` ledger entry, idempotent on the run id. Twilio's actual per-call price API (`get_call_cost`) stays unused in v1 (rate is superadmin-tunable via env; fail-open to the flat rate keeps margin risk bounded by pricing, not code).
4. Renewal cadence: every 30 days from purchase (`next_renewal_at`), charged by a daily ARQ cron at 02:00 UTC. Insufficient balance → `rental_status='past_due'` (warning surfaced in UI); still unpaid `NUMBER_RENTAL_GRACE_DAYS=7` after `next_renewal_at` → release at Twilio + deactivate row. Email warnings deferred to Phase 4 (no email infra — same decision as Phase 2).
5. Release = Twilio `DELETE IncomingPhoneNumbers/{sid}` + row `is_active=False, rental_status='released'` (row kept for history; unique `(org, address_normalized)` means re-purchasing the same number reactivates the row).

---

### Task 1: Schema — platform flags + rental columns

**Files:**
- Modify: `api/db/models.py` (`TelephonyConfigurationModel` at line ~452, `TelephonyPhoneNumberModel` at line ~493)
- Create: `api/alembic/versions/b5f50d0de0005_add_platform_number_columns.py`
- Test: `api/tests/test_platform_number_schema.py`

**Interfaces:**
- Consumes: migration chain head `b4e40d0de0004` (phase 2).
- Produces: `TelephonyConfigurationModel.is_platform` (Boolean, NOT NULL, default False). `TelephonyPhoneNumberModel` new columns: `is_platform` (Boolean NOT NULL default False), `provider_sid` (String nullable — Twilio IncomingPhoneNumber SID), `rental_cents` (Integer nullable), `next_renewal_at` (DateTime(timezone=True) nullable), `rental_status` (String nullable — `active | past_due | released`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_number_schema.py
"""Schema checks for phase-3 platform-number columns (migration b5f50d0de0005)."""

from api.db import db_client
from api.db.models import TelephonyConfigurationModel, TelephonyPhoneNumberModel


async def test_new_columns_default(db_session):
    async with db_client.async_session() as s:
        config = TelephonyConfigurationModel(
            organization_id=None,  # replaced below; flush needs a real org
        )
        # Column presence is what we assert; use the mapper, not a DB round-trip
        assert hasattr(config, "is_platform")
        phone_cols = TelephonyPhoneNumberModel.__table__.columns
        for name in ("is_platform", "provider_sid", "rental_cents", "next_renewal_at", "rental_status"):
            assert name in phone_cols, name
        assert phone_cols["is_platform"].default.arg is False
        await s.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_schema.py -v`
Expected: FAIL — `AssertionError: is_platform`.

- [ ] **Step 3: Add columns to models**

In `TelephonyConfigurationModel` (after `is_default_outbound`):

```python
    # Phase 3 (saas): True for the org's platform-managed Twilio subaccount
    # config. Hidden from user CRUD; provisioned/owned by platform_numbers.
    is_platform = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
```

In `TelephonyPhoneNumberModel` (after `is_default_caller_id`):

```python
    # Phase 3 (saas): platform-provisioned number metadata. rental_status:
    # active | past_due | released. provider_sid = Twilio IncomingPhoneNumber SID.
    is_platform = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    provider_sid = Column(String, nullable=True)
    rental_cents = Column(Integer, nullable=True)
    next_renewal_at = Column(DateTime(timezone=True), nullable=True)
    rental_status = Column(String, nullable=True)
```

- [ ] **Step 4: Write the migration**

`api/alembic/versions/b5f50d0de0005_add_platform_number_columns.py` (revision id continues the hand-picked `b#…de000#` billing series):

```python
"""add platform number columns

Revision ID: b5f50d0de0005
Revises: b4e40d0de0004
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5f50d0de0005"
down_revision: Union[str, None] = "b4e40d0de0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "telephony_configurations",
        sa.Column(
            "is_platform", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "telephony_phone_numbers",
        sa.Column(
            "is_platform", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("provider_sid", sa.String(), nullable=True)
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("rental_cents", sa.Integer(), nullable=True)
    )
    op.add_column(
        "telephony_phone_numbers",
        sa.Column("next_renewal_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "telephony_phone_numbers", sa.Column("rental_status", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_telephony_phone_numbers_platform_renewal",
        "telephony_phone_numbers",
        ["is_platform", "next_renewal_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telephony_phone_numbers_platform_renewal",
        table_name="telephony_phone_numbers",
    )
    op.drop_column("telephony_phone_numbers", "rental_status")
    op.drop_column("telephony_phone_numbers", "next_renewal_at")
    op.drop_column("telephony_phone_numbers", "rental_cents")
    op.drop_column("telephony_phone_numbers", "provider_sid")
    op.drop_column("telephony_phone_numbers", "is_platform")
    op.drop_column("telephony_configurations", "is_platform")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_schema.py api/tests/test_plan_models.py -v`
Expected: PASS (session fixture re-runs `alembic upgrade head`).

- [ ] **Step 6: Commit**

```bash
git add api/db/models.py api/alembic/versions/b5f50d0de0005_add_platform_number_columns.py api/tests/test_platform_number_schema.py
git commit -m "feat(saas-p3): platform flags + rental columns on telephony tables"
```

---

### Task 2: DB client methods for platform configs/numbers

**Files:**
- Modify: `api/db/telephony_configuration_client.py`
- Modify: `api/db/telephony_phone_number_client.py`
- Test: `api/tests/test_platform_number_client.py`

**Interfaces:**
- Consumes: Task 1 columns; existing `create_telephony_configuration` / `create_phone_number` / `update_phone_number` signatures (see their defs in the two client files).
- Produces (all on `db_client`):
  - `get_platform_telephony_configuration(organization_id: int) -> TelephonyConfigurationModel | None` — the org's `is_platform=True` config.
  - `create_platform_telephony_configuration(organization_id: int, credentials: dict) -> TelephonyConfigurationModel` — name `"Platform numbers"`, provider `"twilio"`, `is_platform=True`, never default-outbound-stealing (pass `is_default_outbound=False`; set flag after create via direct session like existing helpers).
  - `list_platform_numbers_for_org(organization_id: int) -> list[TelephonyPhoneNumberModel]` — `is_platform=True`, newest first.
  - `list_all_platform_numbers(*, limit: int = 200) -> list[tuple[TelephonyPhoneNumberModel, int]]` — (row, organization_id) across ALL orgs, for superadmin.
  - `list_due_platform_numbers(now: datetime) -> list[TelephonyPhoneNumberModel]` — `is_platform=True AND is_active=True AND rental_status != 'released' AND next_renewal_at <= now`.
  - `update_platform_number_rental(phone_number_id: int, *, rental_status=..., next_renewal_at=..., is_active=...) -> None` — sentinel-based partial update (copy the `_UNSET` pattern from `api/db/plan_client.py`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_number_client.py
"""Platform telephony config + number client methods (phase 3).

Requires pgvector Postgres (docker-compose-local).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org():
        async with maker() as s:
            org = OrganizationModel(provider_id=f"org_p3_{uuid.uuid4().hex[:10]}")
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
        await s.commit()  # CASCADE removes configs + numbers
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def _platform_number(org, config, address, **kw):
    phone = await db_client.create_phone_number(
        organization_id=org.id,
        telephony_configuration_id=config.id,
        address=address,
        country_code="US",
    )
    async with db_client.async_session() as s:
        from api.db.models import TelephonyPhoneNumberModel

        row = await s.get(TelephonyPhoneNumberModel, phone.id)
        row.is_platform = True
        row.provider_sid = kw.get("provider_sid", "PN_test")
        row.rental_cents = kw.get("rental_cents", 1000)
        row.next_renewal_at = kw.get("next_renewal_at")
        row.rental_status = kw.get("rental_status", "active")
        await s.commit()
    return phone


async def test_platform_config_create_and_get(real_db):
    org = await real_db()
    assert await db_client.get_platform_telephony_configuration(org.id) is None
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_sub", "auth_token": "tok", "from_numbers": []}
    )
    assert config.is_platform is True
    found = await db_client.get_platform_telephony_configuration(org.id)
    assert found.id == config.id
    # Scoped: another org sees nothing
    other = await real_db()
    assert await db_client.get_platform_telephony_configuration(other.id) is None


async def test_due_numbers_and_rental_update(real_db):
    org = await real_db()
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_sub2", "auth_token": "tok", "from_numbers": []}
    )
    now = datetime.now(timezone.utc)
    due = await _platform_number(
        org, config, "+15550001111", next_renewal_at=now - timedelta(days=1)
    )
    not_due = await _platform_number(
        org, config, "+15550002222", next_renewal_at=now + timedelta(days=10)
    )
    due_rows = await db_client.list_due_platform_numbers(now)
    due_ids = {r.id for r in due_rows}
    assert due.id in due_ids
    assert not_due.id not in due_ids

    await db_client.update_platform_number_rental(
        due.id, rental_status="past_due"
    )
    listed = await db_client.list_platform_numbers_for_org(org.id)
    by_id = {r.id: r for r in listed}
    assert by_id[due.id].rental_status == "past_due"
    assert by_id[due.id].next_renewal_at is not None  # untouched by partial update

    await db_client.update_platform_number_rental(
        due.id, rental_status="released", is_active=False
    )
    released = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert released[due.id].is_active is False
    # released numbers no longer show up as due
    assert due.id not in {r.id for r in await db_client.list_due_platform_numbers(now)}


async def test_list_all_platform_numbers_spans_orgs(real_db):
    org_a, org_b = await real_db(), await real_db()
    for org, addr in ((org_a, "+15550003333"), (org_b, "+15550004444")):
        config = await db_client.create_platform_telephony_configuration(
            org.id, {"account_sid": f"AC_{org.id}", "auth_token": "t", "from_numbers": []}
        )
        await _platform_number(org, config, addr)
    rows = await db_client.list_all_platform_numbers()
    org_ids = {org_id for _, org_id in rows}
    assert {org_a.id, org_b.id} <= org_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_client.py -v`
Expected: FAIL — `AttributeError: ... 'get_platform_telephony_configuration'`.

- [ ] **Step 3: Implement client methods**

Append to `TelephonyConfigurationClient` (`api/db/telephony_configuration_client.py`):

```python
    async def get_platform_telephony_configuration(self, organization_id: int):
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyConfigurationModel).where(
                    TelephonyConfigurationModel.organization_id == organization_id,
                    TelephonyConfigurationModel.is_platform.is_(True),
                )
            )
            return result.scalars().first()

    async def create_platform_telephony_configuration(
        self, organization_id: int, credentials: dict
    ):
        config = await self.create_telephony_configuration(
            organization_id,
            name="Platform numbers",
            provider="twilio",
            credentials=credentials,
            is_default_outbound=False,
        )
        async with self.async_session() as session:
            row = await session.get(TelephonyConfigurationModel, config.id)
            row.is_platform = True
            await session.commit()
            await session.refresh(row)
            return row
```

(Match `create_telephony_configuration`'s real parameter names — read its def first; if the first config auto-becomes default-outbound, that's acceptable: platform numbers are then also usable as outbound caller IDs, which is desired.)

Append to `TelephonyPhoneNumberClient` (`api/db/telephony_phone_number_client.py`):

```python
_UNSET = object()


class ...:  # (append methods inside the existing class body)

    async def list_platform_numbers_for_org(self, organization_id: int):
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .where(
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.is_platform.is_(True),
                )
                .order_by(TelephonyPhoneNumberModel.id.desc())
            )
            return list(result.scalars().all())

    async def list_all_platform_numbers(self, *, limit: int = 200):
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .where(TelephonyPhoneNumberModel.is_platform.is_(True))
                .order_by(TelephonyPhoneNumberModel.id.desc())
                .limit(limit)
            )
            return [(row, row.organization_id) for row in result.scalars().all()]

    async def list_due_platform_numbers(self, now):
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.is_platform.is_(True),
                    TelephonyPhoneNumberModel.is_active.is_(True),
                    TelephonyPhoneNumberModel.rental_status != "released",
                    TelephonyPhoneNumberModel.next_renewal_at <= now,
                )
            )
            return list(result.scalars().all())

    async def update_platform_number_rental(
        self,
        phone_number_id: int,
        *,
        rental_status=_UNSET,
        next_renewal_at=_UNSET,
        is_active=_UNSET,
    ) -> None:
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if row is None:
                return
            if rental_status is not _UNSET:
                row.rental_status = rental_status
            if next_renewal_at is not _UNSET:
                row.next_renewal_at = next_renewal_at
            if is_active is not _UNSET:
                row.is_active = is_active
            await session.commit()
```

(Place `_UNSET = object()` at module level, not in the class. Match the file's existing imports — `select`, `TelephonyPhoneNumberModel` are already imported.)

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/db/telephony_configuration_client.py api/db/telephony_phone_number_client.py api/tests/test_platform_number_client.py
git commit -m "feat(saas-p3): db client methods for platform configs and numbers"
```

---

### Task 3: Constants + `PlatformTwilioClient` (Twilio REST wrapper)

**Files:**
- Modify: `api/constants.py` (append after the Razorpay block)
- Create: `api/services/telephony/platform_twilio.py`
- Test: `api/tests/test_platform_twilio_client.py`

**Interfaces:**
- Consumes: aiohttp (already a dependency — `TwilioProvider` uses it).
- Produces:
  - Constants: `PLATFORM_TWILIO_ACCOUNT_SID`, `PLATFORM_TWILIO_AUTH_TOKEN` (env, default None); `PLATFORM_NUMBER_COUNTRIES` (default `"US,CA,IN"`, parsed to a set); `PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT=10`; per-country dict `PLATFORM_NUMBER_RENTAL_MINUTES` built from `PLATFORM_NUMBER_RENTAL_MINUTES_<CC>` envs (IN default 20); `PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE=50`; `NUMBER_RENTAL_GRACE_DAYS=7`.
  - `class PlatformTwilioError(Exception)` with `.detail: str`.
  - `class PlatformTwilioClient` with:
    - `__init__(self, account_sid: str, auth_token: str)`
    - `async search_available_numbers(self, *, country: str, area_code: str | None = None, contains: str | None = None, limit: int = 20) -> list[dict]` — GET `https://api.twilio.com/2010-04-01/Accounts/{sid}/AvailablePhoneNumbers/{country}/Local.json`; returns `[{"phone_number", "friendly_name", "locality", "region"}]`.
    - `async create_subaccount(self, friendly_name: str) -> dict` — POST `https://api.twilio.com/2010-04-01/Accounts.json`; returns `{"sid", "auth_token"}`.
    - `async purchase_number(self, *, subaccount_sid: str, subaccount_token: str, phone_number: str, voice_url: str) -> dict` — POST `.../Accounts/{subaccount_sid}/IncomingPhoneNumbers.json` with `PhoneNumber`, `VoiceUrl`, `VoiceMethod=POST`, authed as the subaccount; returns `{"sid", "phone_number"}`.
    - `async release_number(self, *, subaccount_sid: str, subaccount_token: str, number_sid: str) -> None` — DELETE `.../IncomingPhoneNumbers/{number_sid}.json`.
  - Module factory `def get_platform_twilio() -> PlatformTwilioClient` — raises `PlatformTwilioError("platform_twilio_not_configured")` if env vars missing.
  - Non-2xx Twilio responses raise `PlatformTwilioError` carrying Twilio's `message` field (regulatory-bundle errors for IN surface verbatim, per spec).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_twilio_client.py
"""PlatformTwilioClient — aiohttp fully mocked; asserts URLs, auth, payloads."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony import platform_twilio
from api.services.telephony.platform_twilio import (
    PlatformTwilioClient,
    PlatformTwilioError,
)


def _mock_response(status=200, payload=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp):
    session = MagicMock()
    for verb in ("get", "post", "delete"):
        getattr(session, verb).return_value = resp
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def client():
    return PlatformTwilioClient("AC_platform", "platform_token")


async def test_search_builds_url_and_parses(client):
    resp = _mock_response(
        payload={
            "available_phone_numbers": [
                {
                    "phone_number": "+14155551234",
                    "friendly_name": "(415) 555-1234",
                    "locality": "San Francisco",
                    "region": "CA",
                }
            ]
        }
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        results = await client.search_available_numbers(
            country="US", area_code="415", limit=5
        )
    assert results == [
        {
            "phone_number": "+14155551234",
            "friendly_name": "(415) 555-1234",
            "locality": "San Francisco",
            "region": "CA",
        }
    ]
    url = session.get.call_args.args[0]
    assert "Accounts/AC_platform/AvailablePhoneNumbers/US/Local.json" in url
    params = session.get.call_args.kwargs["params"]
    assert params["AreaCode"] == "415"
    assert params["PageSize"] == 5


async def test_create_subaccount(client):
    resp = _mock_response(payload={"sid": "AC_sub", "auth_token": "sub_tok"})
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        sub = await client.create_subaccount("voxagent-org-42")
    assert sub == {"sid": "AC_sub", "auth_token": "sub_tok"}
    assert session.post.call_args.args[0].endswith("/Accounts.json")
    assert session.post.call_args.kwargs["data"]["FriendlyName"] == "voxagent-org-42"


async def test_purchase_number_sets_voice_url(client):
    resp = _mock_response(
        status=201, payload={"sid": "PN_1", "phone_number": "+14155551234"}
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        result = await client.purchase_number(
            subaccount_sid="AC_sub",
            subaccount_token="sub_tok",
            phone_number="+14155551234",
            voice_url="https://api.example.com/api/v1/telephony/inbound/run",
        )
    assert result == {"sid": "PN_1", "phone_number": "+14155551234"}
    assert "Accounts/AC_sub/IncomingPhoneNumbers.json" in session.post.call_args.args[0]
    data = session.post.call_args.kwargs["data"]
    assert data["PhoneNumber"] == "+14155551234"
    assert data["VoiceUrl"].endswith("/inbound/run")
    assert data["VoiceMethod"] == "POST"


async def test_twilio_error_surfaces_message(client):
    resp = _mock_response(
        status=400, payload={"message": "Regulatory bundle required for IN"}
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        with pytest.raises(PlatformTwilioError) as err:
            await client.search_available_numbers(country="IN")
    assert "Regulatory bundle" in str(err.value)


async def test_release_number(client):
    resp = _mock_response(status=204)
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        await client.release_number(
            subaccount_sid="AC_sub", subaccount_token="sub_tok", number_sid="PN_1"
        )
    assert "IncomingPhoneNumbers/PN_1.json" in session.delete.call_args.args[0]


def test_factory_requires_env(monkeypatch):
    monkeypatch.setattr(platform_twilio, "PLATFORM_TWILIO_ACCOUNT_SID", None)
    platform_twilio.get_platform_twilio.cache_clear()
    with pytest.raises(PlatformTwilioError):
        platform_twilio.get_platform_twilio()
    platform_twilio.get_platform_twilio.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_twilio_client.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Add constants**

Append to `api/constants.py` after the Razorpay block:

```python
# Platform Twilio numbers (saas phase 3): parent account that provisions
# per-org subaccounts and purchases numbers. Rentals are priced in
# minutes-equivalent (1 minute == 100 ledger cents).
PLATFORM_TWILIO_ACCOUNT_SID = os.getenv("PLATFORM_TWILIO_ACCOUNT_SID")
PLATFORM_TWILIO_AUTH_TOKEN = os.getenv("PLATFORM_TWILIO_AUTH_TOKEN")
PLATFORM_NUMBER_COUNTRIES = {
    c.strip().upper()
    for c in os.getenv("PLATFORM_NUMBER_COUNTRIES", "US,CA,IN").split(",")
    if c.strip()
}
PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT = int(
    os.getenv("PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT", "10")
)
PLATFORM_NUMBER_RENTAL_MINUTES = {
    country: int(
        os.getenv(
            f"PLATFORM_NUMBER_RENTAL_MINUTES_{country}",
            str(20 if country == "IN" else PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT),
        )
    )
    for country in PLATFORM_NUMBER_COUNTRIES
}
PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE = int(
    os.getenv("PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE", "50")
)
NUMBER_RENTAL_GRACE_DAYS = int(os.getenv("NUMBER_RENTAL_GRACE_DAYS", "7"))
```

- [ ] **Step 4: Implement `api/services/telephony/platform_twilio.py`**

```python
"""Raw-REST client for the PLATFORM Twilio account (saas phase 3).

Mirrors TwilioProvider's aiohttp style (services/telephony/providers/twilio/
provider.py) rather than the twilio SDK. Subaccounts isolate each org's
numbers; purchase/release are executed as the subaccount.
"""

from functools import lru_cache
from typing import Optional

import aiohttp

from api.constants import (
    PLATFORM_TWILIO_ACCOUNT_SID,
    PLATFORM_TWILIO_AUTH_TOKEN,
)

_API = "https://api.twilio.com/2010-04-01"


class PlatformTwilioError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def _check(resp) -> dict:
    if resp.status >= 300:
        try:
            payload = await resp.json()
            message = payload.get("message") or str(payload)
        except Exception:
            message = f"Twilio error (HTTP {resp.status})"
        raise PlatformTwilioError(message)
    if resp.status == 204:
        return {}
    return await resp.json()


class PlatformTwilioClient:
    def __init__(self, account_sid: str, auth_token: str):
        self._account_sid = account_sid
        self._auth = aiohttp.BasicAuth(account_sid, auth_token)

    async def search_available_numbers(
        self,
        *,
        country: str,
        area_code: Optional[str] = None,
        contains: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        url = (
            f"{_API}/Accounts/{self._account_sid}"
            f"/AvailablePhoneNumbers/{country}/Local.json"
        )
        params: dict = {"PageSize": limit}
        if area_code:
            params["AreaCode"] = area_code
        if contains:
            params["Contains"] = contains
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, auth=self._auth) as resp:
                payload = await _check(resp)
        return [
            {
                "phone_number": n.get("phone_number"),
                "friendly_name": n.get("friendly_name"),
                "locality": n.get("locality"),
                "region": n.get("region"),
            }
            for n in payload.get("available_phone_numbers", [])
        ]

    async def create_subaccount(self, friendly_name: str) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API}/Accounts.json",
                data={"FriendlyName": friendly_name},
                auth=self._auth,
            ) as resp:
                payload = await _check(resp)
        return {"sid": payload["sid"], "auth_token": payload["auth_token"]}

    async def purchase_number(
        self,
        *,
        subaccount_sid: str,
        subaccount_token: str,
        phone_number: str,
        voice_url: str,
    ) -> dict:
        sub_auth = aiohttp.BasicAuth(subaccount_sid, subaccount_token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API}/Accounts/{subaccount_sid}/IncomingPhoneNumbers.json",
                data={
                    "PhoneNumber": phone_number,
                    "VoiceUrl": voice_url,
                    "VoiceMethod": "POST",
                },
                auth=sub_auth,
            ) as resp:
                payload = await _check(resp)
        return {"sid": payload["sid"], "phone_number": payload["phone_number"]}

    async def release_number(
        self, *, subaccount_sid: str, subaccount_token: str, number_sid: str
    ) -> None:
        sub_auth = aiohttp.BasicAuth(subaccount_sid, subaccount_token)
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{_API}/Accounts/{subaccount_sid}/IncomingPhoneNumbers/{number_sid}.json",
                auth=sub_auth,
            ) as resp:
                await _check(resp)


@lru_cache(maxsize=1)
def get_platform_twilio() -> PlatformTwilioClient:
    if not PLATFORM_TWILIO_ACCOUNT_SID or not PLATFORM_TWILIO_AUTH_TOKEN:
        raise PlatformTwilioError("platform_twilio_not_configured")
    return PlatformTwilioClient(
        PLATFORM_TWILIO_ACCOUNT_SID, PLATFORM_TWILIO_AUTH_TOKEN
    )
```

(Note: `get_platform_twilio` reads module-level constants; the factory test monkeypatches the module attribute and clears the cache. lru_cache on a raising function does not cache the exception — safe.)

- [ ] **Step 5: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_twilio_client.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/constants.py api/services/telephony/platform_twilio.py api/tests/test_platform_twilio_client.py
git commit -m "feat(saas-p3): platform Twilio REST client + rental constants"
```

---

### Task 4: `platform_numbers` service — search / purchase / release

**Files:**
- Create: `api/services/telephony/platform_numbers.py`
- Test: `api/tests/test_platform_numbers_service.py`

**Interfaces:**
- Consumes: Task 2 db methods; Task 3 `get_platform_twilio()` / `PlatformTwilioError`; `billing_service.credit / get_balance_cents` (`api/services/billing/billing_service.py:78,99`); `CENTS_PER_MINUTE` (`api/services/billing/trial.py:9`); `IS_SAAS_MODE`, `PLATFORM_NUMBER_COUNTRIES`, `PLATFORM_NUMBER_RENTAL_MINUTES` (Task 3); webhook base URL from `api/utils/common.get_backend_endpoints` (same helper `_sync_inbound_for_phone_number` uses at `api/routes/organization.py:482` — read that call site and copy exactly how the inbound URL is built); `db_client.create_phone_number / update_phone_number` (existing).
- Produces:
  - `class PlatformNumberError(Exception)` with `.status_code: int` and `.detail: str`.
  - `def rental_cents_for_country(country: str) -> int` — `PLATFORM_NUMBER_RENTAL_MINUTES[country] * CENTS_PER_MINUTE`.
  - `async search(country: str, area_code: str | None, contains: str | None) -> list[dict]` — validates saas mode + allowed country, delegates to the platform client.
  - `async purchase(organization_id: int, *, phone_number: str, country: str, inbound_workflow_id: int | None) -> TelephonyPhoneNumberModel` — full flow: validate mode/country → pre-check balance ≥ rental → ensure subaccount config (create via Twilio + `create_platform_telephony_configuration` if absent) → buy at Twilio with VoiceUrl set → create phone row (`is_platform=True`, `provider_sid`, `rental_cents`, `next_renewal_at = now+30d`, `rental_status="active"`, `country_code`, `inbound_workflow_id`) → debit first rental (`number_rental:{phone_id}:initial`).
  - `async release(organization_id: int, phone_number_id: int) -> None` — org-scoped lookup, Twilio release, row → `is_active=False, rental_status="released"`.
  - Error codes used by routes: 404 `platform_numbers_unavailable` (not saas), 400 `country_not_supported`, 402 `insufficient_balance_for_rental`, 404 `number_not_found`, 502 (Twilio error message passthrough).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_numbers_service.py
"""platform_numbers service: purchase/release flows with Twilio mocked."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service
from api.services.telephony import platform_numbers
from api.services.telephony.platform_numbers import PlatformNumberError


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org(balance_cents: int = 0):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_pn_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
        if balance_cents:
            await billing_service.credit(org.id, balance_cents, "topup")
        return org

    yield make_org
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


@pytest.fixture
def twilio():
    client = MagicMock()
    client.search_available_numbers = AsyncMock(
        return_value=[{"phone_number": "+14155551234", "friendly_name": "",
                       "locality": "SF", "region": "CA"}]
    )
    client.create_subaccount = AsyncMock(
        return_value={"sid": "AC_sub", "auth_token": "sub_tok"}
    )
    client.purchase_number = AsyncMock(
        return_value={"sid": "PN_new", "phone_number": "+14155551234"}
    )
    client.release_number = AsyncMock()
    with (
        patch.object(platform_numbers, "get_platform_twilio", return_value=client),
        patch.object(platform_numbers, "_saas_enabled", return_value=True),
    ):
        yield client


async def test_purchase_full_flow(real_db, twilio):
    org = await real_db(balance_cents=5000)  # rental US = 10 min = 1000
    phone = await platform_numbers.purchase(
        org.id, phone_number="+14155551234", country="US", inbound_workflow_id=None
    )
    assert phone.is_platform is True
    assert phone.provider_sid == "PN_new"
    assert phone.rental_cents == 1000
    assert phone.rental_status == "active"
    assert phone.next_renewal_at > datetime.now(timezone.utc)
    # subaccount config created + reused
    config = await db_client.get_platform_telephony_configuration(org.id)
    assert config.credentials["account_sid"] == "AC_sub"
    twilio.create_subaccount.assert_awaited_once()
    # voice_url wired to the shared inbound dispatcher
    voice_url = twilio.purchase_number.call_args.kwargs["voice_url"]
    assert voice_url.endswith("/api/v1/telephony/inbound/run")
    # first month charged
    assert await billing_service.get_balance_cents(org.id) == 4000
    # second purchase reuses the subaccount
    twilio.purchase_number.return_value = {"sid": "PN_2", "phone_number": "+14155551235"}
    await platform_numbers.purchase(
        org.id, phone_number="+14155551235", country="US", inbound_workflow_id=None
    )
    twilio.create_subaccount.assert_awaited_once()  # still once


async def test_purchase_insufficient_balance(real_db, twilio):
    org = await real_db(balance_cents=100)
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.purchase(
            org.id, phone_number="+14155551236", country="US", inbound_workflow_id=None
        )
    assert err.value.status_code == 402
    twilio.purchase_number.assert_not_awaited()  # never bought at Twilio


async def test_purchase_unsupported_country(real_db, twilio):
    org = await real_db(balance_cents=5000)
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.purchase(
            org.id, phone_number="+4915510000000", country="DE", inbound_workflow_id=None
        )
    assert err.value.status_code == 400


async def test_release_flow(real_db, twilio):
    org = await real_db(balance_cents=5000)
    phone = await platform_numbers.purchase(
        org.id, phone_number="+14155551237", country="US", inbound_workflow_id=None
    )
    await platform_numbers.release(org.id, phone.id)
    twilio.release_number.assert_awaited_once()
    assert twilio.release_number.call_args.kwargs["number_sid"] == "PN_new"
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "released"
    assert rows[phone.id].is_active is False


async def test_release_cross_org_404(real_db, twilio):
    org_a = await real_db(balance_cents=5000)
    org_b = await real_db()
    phone = await platform_numbers.purchase(
        org_a.id, phone_number="+14155551238", country="US", inbound_workflow_id=None
    )
    with pytest.raises(PlatformNumberError) as err:
        await platform_numbers.release(org_b.id, phone.id)
    assert err.value.status_code == 404


async def test_disabled_outside_saas(real_db):
    org = await real_db()
    with patch.object(platform_numbers, "_saas_enabled", return_value=False):
        with pytest.raises(PlatformNumberError) as err:
            await platform_numbers.search("US", None, None)
        assert err.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_numbers_service.py -v`
Expected: FAIL — import error.

- [ ] **Step 3: Implement `api/services/telephony/platform_numbers.py`**

```python
"""Platform-provisioned Twilio numbers (spec §8, saas phase 3).

Purchase creates (lazily) a per-org Twilio subaccount, buys the number into
it with the shared inbound dispatcher as VoiceUrl, records a flagged
telephony_phone_numbers row, and charges the first month's rental from the
credit ledger. Release relinquishes the number at Twilio and deactivates the
row (kept for history).
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from api.constants import (
    IS_SAAS_MODE,
    PLATFORM_NUMBER_COUNTRIES,
    PLATFORM_NUMBER_RENTAL_MINUTES,
    PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT,
)
from api.db import db_client
from api.services.billing import billing_service
from api.services.billing.trial import CENTS_PER_MINUTE
from api.services.telephony.platform_twilio import (
    PlatformTwilioError,
    get_platform_twilio,
)

RENTAL_PERIOD_DAYS = 30


class PlatformNumberError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _saas_enabled() -> bool:
    return IS_SAAS_MODE


def rental_cents_for_country(country: str) -> int:
    minutes = PLATFORM_NUMBER_RENTAL_MINUTES.get(
        country.upper(), PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT
    )
    return minutes * CENTS_PER_MINUTE


def _require_available(country: str) -> None:
    if not _saas_enabled():
        raise PlatformNumberError(404, "platform_numbers_unavailable")
    if country.upper() not in PLATFORM_NUMBER_COUNTRIES:
        raise PlatformNumberError(400, "country_not_supported")


def _inbound_voice_url() -> str:
    # Same URL configure_inbound wires for BYO numbers — the shared dispatcher
    # resolves org + workflow from (subaccount sid, called number).
    from api.utils.common import get_backend_endpoints

    backend, _ = get_backend_endpoints()
    return f"{backend}/api/v1/telephony/inbound/run"


async def search(country: str, area_code, contains) -> list[dict]:
    _require_available(country)
    try:
        client = get_platform_twilio()
        return await client.search_available_numbers(
            country=country.upper(), area_code=area_code, contains=contains
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)


async def _ensure_subaccount(organization_id: int) -> dict:
    config = await db_client.get_platform_telephony_configuration(organization_id)
    if config is not None:
        return config.credentials
    client = get_platform_twilio()
    sub = await client.create_subaccount(f"voxagent-org-{organization_id}")
    credentials = {
        "account_sid": sub["sid"],
        "auth_token": sub["auth_token"],
        "from_numbers": [],
    }
    await db_client.create_platform_telephony_configuration(
        organization_id, credentials
    )
    return credentials


async def purchase(
    organization_id: int,
    *,
    phone_number: str,
    country: str,
    inbound_workflow_id,
):
    _require_available(country)
    rental = rental_cents_for_country(country)
    balance = await billing_service.get_balance_cents(organization_id)
    if balance < rental:
        raise PlatformNumberError(402, "insufficient_balance_for_rental")

    try:
        credentials = await _ensure_subaccount(organization_id)
        client = get_platform_twilio()
        bought = await client.purchase_number(
            subaccount_sid=credentials["account_sid"],
            subaccount_token=credentials["auth_token"],
            phone_number=phone_number,
            voice_url=_inbound_voice_url(),
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)

    config = await db_client.get_platform_telephony_configuration(organization_id)
    phone = await db_client.create_phone_number(
        organization_id=organization_id,
        telephony_configuration_id=config.id,
        address=bought["phone_number"],
        country_code=country.upper(),
        label="Platform number",
        inbound_workflow_id=inbound_workflow_id,
    )
    next_renewal = datetime.now(timezone.utc) + timedelta(days=RENTAL_PERIOD_DAYS)
    async with db_client.async_session() as session:
        from api.db.models import TelephonyPhoneNumberModel

        row = await session.get(TelephonyPhoneNumberModel, phone.id)
        row.is_platform = True
        row.provider_sid = bought["sid"]
        row.rental_cents = rental
        row.next_renewal_at = next_renewal
        row.rental_status = "active"
        await session.commit()
        await session.refresh(row)

    await billing_service.credit(
        organization_id,
        -rental,
        "number_rental",
        description=f"Number rental (first month): {bought['phone_number']}",
        idempotency_key=f"number_rental:{phone.id}:initial",
    )
    logger.info(
        f"Platform number purchased org={organization_id} number={bought['phone_number']}"
    )
    return row


async def release(organization_id: int, phone_number_id: int) -> None:
    if not _saas_enabled():
        raise PlatformNumberError(404, "platform_numbers_unavailable")
    rows = await db_client.list_platform_numbers_for_org(organization_id)
    row = next((r for r in rows if r.id == phone_number_id), None)
    if row is None or row.rental_status == "released":
        raise PlatformNumberError(404, "number_not_found")
    config = await db_client.get_platform_telephony_configuration(organization_id)
    try:
        client = get_platform_twilio()
        await client.release_number(
            subaccount_sid=config.credentials["account_sid"],
            subaccount_token=config.credentials["auth_token"],
            number_sid=row.provider_sid,
        )
    except PlatformTwilioError as e:
        raise PlatformNumberError(502, e.detail)
    await db_client.update_platform_number_rental(
        phone_number_id, rental_status="released", is_active=False
    )
```

(If `get_backend_endpoints` has a different name/signature, read `api/routes/organization.py:482` `_sync_inbound_for_phone_number` and copy exactly how IT builds the inbound URL — the two must stay identical. Adjust `_inbound_voice_url` accordingly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_numbers_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/services/telephony/platform_numbers.py api/tests/test_platform_numbers_service.py
git commit -m "feat(saas-p3): platform number search/purchase/release service"
```

---

### Task 5: Rental renewal service + daily ARQ cron

**Files:**
- Create: `api/services/billing/number_rental.py`
- Create: `api/tasks/rental_billing.py`
- Modify: `api/tasks/arq.py` (functions list + cron_jobs at lines 52-63)
- Modify: `api/tasks/function_names.py`
- Test: `api/tests/test_number_rental.py`

**Interfaces:**
- Consumes: `db_client.list_due_platform_numbers / update_platform_number_rental / get_platform_telephony_configuration` (Task 2); `billing_service.get_balance_cents / credit`; `platform_numbers.release` internals — reuse `PlatformTwilioClient.release_number` via the same path (call `platform_numbers.release(org_id, phone_id)` but tolerate its saas gate: renewal only runs in saas anyway); `NUMBER_RENTAL_GRACE_DAYS` (Task 3); `arq.cron`.
- Produces:
  - `async charge_due_rentals(now: datetime | None = None) -> dict` in `number_rental.py` — returns `{"charged": int, "past_due": int, "released": int}`. Per due number:
    - balance ≥ `rental_cents` → debit with `idempotency_key=f"number_rental:{row.id}:{row.next_renewal_at.date().isoformat()}"`, advance `next_renewal_at += 30d`, `rental_status="active"`.
    - balance < rental and `now < next_renewal_at + grace` → `rental_status="past_due"` (charged next daily run if topped up).
    - balance < rental and past grace → release at Twilio + `rental_status="released", is_active=False`.
  - ARQ task `async def charge_number_rentals(ctx)` in `api/tasks/rental_billing.py`, registered in `WorkerSettings.functions` and `cron_jobs = [cron(charge_number_rentals, hour=2, minute=0)]`; `FunctionNames.CHARGE_NUMBER_RENTALS = "charge_number_rentals"`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_number_rental.py
"""Daily rental renewal: charge, past_due, grace release. Twilio mocked."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel, TelephonyPhoneNumberModel
from api.services.billing import billing_service, number_rental


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_setup(balance_cents: int, next_renewal_at):
        async with maker() as s:
            org = OrganizationModel(
                provider_id=f"org_rent_{uuid.uuid4().hex[:10]}",
                credit_balance_cents=0,
            )
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
        if balance_cents:
            await billing_service.credit(org.id, balance_cents, "topup")
        config = await db_client.create_platform_telephony_configuration(
            org.id, {"account_sid": "AC_r", "auth_token": "t", "from_numbers": []}
        )
        phone = await db_client.create_phone_number(
            organization_id=org.id,
            telephony_configuration_id=config.id,
            address=f"+1555{uuid.uuid4().int % 10**7:07d}",
        )
        async with maker() as s:
            row = await s.get(TelephonyPhoneNumberModel, phone.id)
            row.is_platform = True
            row.provider_sid = "PN_r"
            row.rental_cents = 1000
            row.next_renewal_at = next_renewal_at
            row.rental_status = "active"
            await s.commit()
        return org, phone

    yield make_setup
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


def _twilio():
    client = MagicMock()
    client.release_number = AsyncMock()
    return client


async def test_due_number_charged_and_advanced(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(5000, now - timedelta(hours=1))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["charged"] >= 1
    assert await billing_service.get_balance_cents(org.id) == 4000
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].next_renewal_at > now + timedelta(days=29)
    assert rows[phone.id].rental_status == "active"
    # idempotent: same renewal date can't double-charge
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        await number_rental.charge_due_rentals(now)
    assert await billing_service.get_balance_cents(org.id) == 4000


async def test_insufficient_balance_marks_past_due(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(hours=1))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["past_due"] >= 1
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "past_due"
    assert rows[phone.id].is_active is True  # still usable during grace
    assert await billing_service.get_balance_cents(org.id) == 100  # untouched


async def test_past_grace_releases_number(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(days=8))  # grace = 7
    twilio = _twilio()
    with patch.object(number_rental, "get_platform_twilio", return_value=twilio):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["released"] >= 1
    twilio.release_number.assert_awaited_once()
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "released"
    assert rows[phone.id].is_active is False


async def test_topped_up_past_due_recovers(real_db):
    now = datetime.now(timezone.utc)
    org, phone = await real_db(100, now - timedelta(days=2))
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        await number_rental.charge_due_rentals(now)  # -> past_due
    await billing_service.credit(org.id, 5000, "topup")
    with patch.object(number_rental, "get_platform_twilio", return_value=_twilio()):
        stats = await number_rental.charge_due_rentals(now)
    assert stats["charged"] >= 1
    rows = {r.id: r for r in await db_client.list_platform_numbers_for_org(org.id)}
    assert rows[phone.id].rental_status == "active"


def test_cron_registered():
    from api.tasks.arq import WorkerSettings
    from api.tasks.rental_billing import charge_number_rentals

    assert charge_number_rentals in WorkerSettings.functions
    assert any(
        getattr(cj, "coroutine", None) in (charge_number_rentals, "charge_number_rentals")
        or getattr(cj, "name", "") == "charge_number_rentals"
        for cj in WorkerSettings.cron_jobs
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_number_rental.py -v`
Expected: FAIL — `ImportError: ... number_rental`.

- [ ] **Step 3: Implement `api/services/billing/number_rental.py`**

```python
"""Monthly platform-number rental billing (spec §8, saas phase 3).

Runs from a daily ARQ cron. Charges are idempotent on the renewal date, so
re-running the same day is a no-op. Insufficient balance marks the number
past_due (still routable during grace); past grace the number is released at
Twilio and the row deactivated.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from api.constants import NUMBER_RENTAL_GRACE_DAYS
from api.db import db_client
from api.services.billing import billing_service
from api.services.telephony.platform_twilio import (
    PlatformTwilioError,
    get_platform_twilio,
)

RENTAL_PERIOD_DAYS = 30


async def _release_at_twilio(row) -> bool:
    config = await db_client.get_platform_telephony_configuration(row.organization_id)
    if config is None or not row.provider_sid:
        return False
    try:
        client = get_platform_twilio()
        await client.release_number(
            subaccount_sid=config.credentials["account_sid"],
            subaccount_token=config.credentials["auth_token"],
            number_sid=row.provider_sid,
        )
        return True
    except PlatformTwilioError as e:
        logger.error(f"Failed to release number {row.id} at Twilio: {e.detail}")
        return False


async def charge_due_rentals(now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    grace = timedelta(days=NUMBER_RENTAL_GRACE_DAYS)
    stats = {"charged": 0, "past_due": 0, "released": 0}

    for row in await db_client.list_due_platform_numbers(now):
        try:
            rental = int(row.rental_cents or 0)
            balance = await billing_service.get_balance_cents(row.organization_id)
            if rental and balance >= rental:
                await billing_service.credit(
                    row.organization_id,
                    -rental,
                    "number_rental",
                    description=f"Number rental renewal: {row.address}",
                    idempotency_key=(
                        f"number_rental:{row.id}:{row.next_renewal_at.date().isoformat()}"
                    ),
                )
                await db_client.update_platform_number_rental(
                    row.id,
                    rental_status="active",
                    next_renewal_at=row.next_renewal_at
                    + timedelta(days=RENTAL_PERIOD_DAYS),
                )
                stats["charged"] += 1
            elif now >= row.next_renewal_at + grace:
                released = await _release_at_twilio(row)
                await db_client.update_platform_number_rental(
                    row.id, rental_status="released", is_active=False
                )
                stats["released"] += 1
                logger.warning(
                    f"Released platform number {row.address} (org "
                    f"{row.organization_id}) after {NUMBER_RENTAL_GRACE_DAYS}d grace"
                    f"{'' if released else ' (Twilio release failed — orphaned)'}"
                )
            else:
                await db_client.update_platform_number_rental(
                    row.id, rental_status="past_due"
                )
                stats["past_due"] += 1
                logger.warning(
                    f"Platform number {row.address} past due (org "
                    f"{row.organization_id}): balance {balance} < rental {rental}"
                )
        except Exception:
            logger.exception(f"Rental processing failed for number {row.id}")

    logger.info(f"Rental run complete: {stats}")
    return stats
```

- [ ] **Step 4: Implement the ARQ task + registration**

`api/tasks/rental_billing.py`:

```python
"""Daily platform-number rental billing (ARQ cron, saas phase 3)."""

from loguru import logger

from api.constants import IS_SAAS_MODE
from api.services.billing import number_rental


async def charge_number_rentals(_ctx) -> dict:
    if not IS_SAAS_MODE:
        return {"skipped": "not_saas"}
    stats = await number_rental.charge_due_rentals()
    logger.info(f"charge_number_rentals: {stats}")
    return stats
```

`api/tasks/arq.py` — add imports and registration (match existing style at lines 52-63):

```python
from arq import cron

from api.tasks.rental_billing import charge_number_rentals

# inside WorkerSettings:
    functions = [..., charge_number_rentals]           # append to existing list
    cron_jobs = [cron(charge_number_rentals, hour=2, minute=0)]
```

`api/tasks/function_names.py` — add:

```python
    CHARGE_NUMBER_RENTALS = "charge_number_rentals"
```

- [ ] **Step 5: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_number_rental.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/services/billing/number_rental.py api/tasks/rental_billing.py api/tasks/arq.py api/tasks/function_names.py api/tests/test_number_rental.py
git commit -m "feat(saas-p3): daily rental renewal cron with grace-period release"
```

---

### Task 6: Carrier per-minute burn on platform-number calls

**Files:**
- Modify: `api/services/workflow_run_billing.py` (`_debit_local_billing` at line 50)
- Test: `api/tests/test_carrier_debit.py`

**Interfaces:**
- Consumes: `workflow_run.initial_context["telephony_configuration_id"]`; `db_client.get_telephony_configuration(id)` (existing, `api/db/telephony_configuration_client.py`); `PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE` (Task 3); `billing_service.credit`; `IS_SAAS_MODE`.
- Produces: after the existing AI-minutes debit in `_debit_local_billing`, if saas mode AND the run's telephony configuration `is_platform`: debit `round(duration_seconds * PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE / 60)` cents with `type="carrier_debit"`, `idempotency_key=f"carrier:{workflow_run.id}"`. Zero-duration or non-platform runs unchanged.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_carrier_debit.py
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
        return org, config

    yield make_org
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


async def test_platform_call_gets_carrier_debit(real_db):
    org, config = await real_db(10000, platform_config=True)
    run = _FakeRun(uuid.uuid4().int % 10**9, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    # AI: 120s @ 100c/min = 200; carrier: 120s @ 50c/min = 100
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200 - 100
    # replay-safe
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200 - 100


async def test_byo_call_no_carrier_debit(real_db):
    org, config = await real_db(10000, platform_config=False)
    run = _FakeRun(uuid.uuid4().int % 10**9, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", True):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200


async def test_oss_mode_no_carrier_debit(real_db):
    org, config = await real_db(10000, platform_config=True)
    run = _FakeRun(uuid.uuid4().int % 10**9, config.id)
    with patch.object(workflow_run_billing, "IS_SAAS_MODE", False):
        await workflow_run_billing._debit_local_billing(run, org.id)
    assert await billing_service.get_balance_cents(org.id) == 10000 - 200
```

(Check `_debit_local_billing`'s exact signature first — if `workflow_run` must be a real model or the function reads other attrs, extend `_FakeRun` to match. If `IS_SAAS_MODE` isn't imported into `workflow_run_billing` yet, the patch target is added in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_carrier_debit.py -v`
Expected: FAIL — no carrier debit applied (balance only minus 200) or AttributeError on `IS_SAAS_MODE`.

- [ ] **Step 3: Implement in `api/services/workflow_run_billing.py`**

Add imports:

```python
from api.constants import IS_SAAS_MODE, PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE
```

Append to the end of `_debit_local_billing` (after the existing `debit_for_run` call):

```python
    await _debit_carrier_if_platform(workflow_run, organization_id)
```

Add the helper below `_debit_local_billing`:

```python
async def _debit_carrier_if_platform(workflow_run, organization_id: int) -> None:
    """Spec §8: platform-number calls burn extra carrier minutes on top of AI
    minutes. Flat env-tunable rate; idempotent per run."""
    if not IS_SAAS_MODE:
        return
    duration = _duration_seconds_from_usage_info(workflow_run.usage_info)
    if not duration:
        return
    config_id = (workflow_run.initial_context or {}).get("telephony_configuration_id")
    if not config_id:
        return
    config = await db_client.get_telephony_configuration(int(config_id))
    if config is None or not getattr(config, "is_platform", False):
        return
    carrier_cents = round(
        float(duration) * PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE / 60
    )
    if carrier_cents <= 0:
        return
    await billing_service.credit(
        organization_id,
        -carrier_cents,
        "carrier_debit",
        description=f"Carrier minutes for run {workflow_run.id}",
        idempotency_key=f"carrier:{workflow_run.id}",
    )
```

(Match the module's real helper name for duration — the explore report says `_duration_seconds_from_usage_info(usage_info)` at lines 25-33; verify signature. `db_client` is already imported in this module.)

- [ ] **Step 4: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_carrier_debit.py api/tests/test_workflow_run_billing.py -v`
Expected: PASS (existing workflow_run_billing tests unaffected — OSS/non-platform paths unchanged).

- [ ] **Step 5: Commit**

```bash
git add api/services/workflow_run_billing.py api/tests/test_carrier_debit.py
git commit -m "feat(saas-p3): carrier per-minute burn on platform-number calls"
```

---

### Task 7: Routes — customer + superadmin + platform-config guards

**Files:**
- Create: `api/routes/platform_numbers.py`
- Modify: `api/routes/main.py` (import + `include_router`, lines 7-49 pattern)
- Modify: `api/routes/organization.py` (403 guards in `update_telephony_configuration` at ~line 608, `delete_telephony_configuration` at ~664, and phone-number `PUT`/`DELETE` at ~835/900 — block when the target row/config `is_platform`)
- Modify: `api/routes/billing_admin.py` (superadmin listing)
- Test: `api/tests/test_platform_number_routes.py`

**Interfaces:**
- Consumes: `platform_numbers` service (Task 4), `plan_limits`-style saas gating via the service's own `PlatformNumberError`; auth dep `get_user_with_selected_organization`; `_ensure_workflow_belongs_to_org` (`api/routes/organization.py` helper — for inbound workflow validation, reimplement locally with `db_client.get_workflow_by_id`-equivalent org check if not importable cleanly); `db_client.list_platform_numbers_for_org / list_all_platform_numbers / update_phone_number`; `rental_cents_for_country`, `PLATFORM_NUMBER_COUNTRIES`, `PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE`.
- Produces (`router = APIRouter(prefix="/platform-numbers", tags=["platform-numbers"])`, mounted under `/api/v1`):
  - `GET /platform-numbers/pricing` → `{countries: [{country, rental_minutes, rental_cents}], carrier_cents_per_minute}`.
  - `GET /platform-numbers/search?country=US&area_code=&contains=` → `list[AvailableNumberResponse{phone_number, friendly_name, locality, region, rental_cents}]`.
  - `POST /platform-numbers/purchase` body `{phone_number, country, inbound_workflow_id?}` → `PlatformNumberResponse`.
  - `GET /platform-numbers` → `list[PlatformNumberResponse{id, address, country_code, rental_cents, rental_minutes_equivalent, next_renewal_at, rental_status, inbound_workflow_id, is_active}]`.
  - `POST /platform-numbers/{id}/release` → `{"status": "released"}`.
  - `PUT /platform-numbers/{id}/inbound-workflow` body `{inbound_workflow_id: int | null}` → updates routing via existing `db_client.update_phone_number(..., inbound_workflow_id=..., clear_inbound_workflow=...)` (VoiceUrl already points at the shared dispatcher — no Twilio sync needed).
  - Superadmin: `GET /superuser/platform-numbers` in `billing_admin.py` → all orgs' platform numbers.
  - `PlatformNumberError` → `HTTPException(status_code=e.status_code, detail=e.detail)` via a small translator used by every handler.
  - Guards: generic telephony-config `PUT`/`DELETE` and phone-number `PUT`/`DELETE` return 403 `platform_managed` when the row/config is `is_platform` (platform numbers are managed only through these new routes).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_number_routes.py
"""Platform number routes — service mocked; guard tests hit real db rows."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.app import app
from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.telephony.platform_numbers import PlatformNumberError


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org():
        async with maker() as s:
            org = OrganizationModel(provider_id=f"org_pnr_{uuid.uuid4().hex[:10]}")
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
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


@pytest_asyncio.fixture
async def org(real_db):
    return await real_db()


@pytest.fixture
def client(org):
    fake_user = type(
        "U", (), {"id": 1, "selected_organization_id": org.id, "is_superuser": False}
    )()
    app.dependency_overrides[get_user_with_selected_organization] = lambda: fake_user
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_pricing_endpoint(client):
    async with client as c:
        resp = await c.get("/api/v1/platform-numbers/pricing")
    assert resp.status_code == 200
    body = resp.json()
    countries = {p["country"] for p in body["countries"]}
    assert {"US", "CA", "IN"} <= countries
    assert body["carrier_cents_per_minute"] == 50


@pytest.mark.asyncio
async def test_search_delegates_to_service(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.search",
        AsyncMock(return_value=[{"phone_number": "+14155551234",
                                 "friendly_name": "", "locality": "SF",
                                 "region": "CA"}]),
    ):
        async with client as c:
            resp = await c.get(
                "/api/v1/platform-numbers/search",
                params={"country": "US", "area_code": "415"},
            )
    assert resp.status_code == 200
    assert resp.json()[0]["phone_number"] == "+14155551234"
    assert resp.json()[0]["rental_cents"] == 1000


@pytest.mark.asyncio
async def test_search_service_error_maps_to_http(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.search",
        AsyncMock(side_effect=PlatformNumberError(400, "country_not_supported")),
    ):
        async with client as c:
            resp = await c.get(
                "/api/v1/platform-numbers/search", params={"country": "DE"}
            )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "country_not_supported"


@pytest.mark.asyncio
async def test_purchase_and_list(client, org):
    fake_row = type(
        "Row",
        (),
        {
            "id": 7,
            "address": "+14155551234",
            "country_code": "US",
            "rental_cents": 1000,
            "next_renewal_at": datetime.now(timezone.utc),
            "rental_status": "active",
            "inbound_workflow_id": None,
            "is_active": True,
        },
    )()
    with patch(
        "api.routes.platform_numbers.platform_numbers.purchase",
        AsyncMock(return_value=fake_row),
    ) as purchase:
        async with client as c:
            resp = await c.post(
                "/api/v1/platform-numbers/purchase",
                json={"phone_number": "+14155551234", "country": "US"},
            )
    assert resp.status_code == 200
    assert resp.json()["address"] == "+14155551234"
    assert resp.json()["rental_minutes_equivalent"] == 10.0
    assert purchase.call_args.kwargs["phone_number"] == "+14155551234"


@pytest.mark.asyncio
async def test_release_route(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.release", AsyncMock()
    ) as release:
        async with client as c:
            resp = await c.post("/api/v1/platform-numbers/9/release")
    assert resp.status_code == 200
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_platform_config_blocked_from_generic_delete(client, org):
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_g", "auth_token": "t", "from_numbers": []}
    )
    async with client as c:
        resp = await c.delete(
            f"/api/v1/organizations/telephony-configs/{config.id}"
        )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "platform_managed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_routes.py -v`
Expected: FAIL — 404s on new paths.

- [ ] **Step 3: Implement `api/routes/platform_numbers.py`**

```python
"""Platform-provisioned number routes (saas phase 3, spec §8).

Thin handlers over services/telephony/platform_numbers; all org-scoped via
the caller's session. Numbers here are platform-managed: the generic
telephony-config CRUD refuses to touch them (403 platform_managed).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.constants import (
    PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE,
    PLATFORM_NUMBER_COUNTRIES,
)
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.telephony import platform_numbers
from api.services.telephony.platform_numbers import (
    PlatformNumberError,
    rental_cents_for_country,
)

router = APIRouter(prefix="/platform-numbers", tags=["platform-numbers"])


class CountryPricing(BaseModel):
    country: str
    rental_minutes: float
    rental_cents: int


class PricingResponse(BaseModel):
    countries: list[CountryPricing]
    carrier_cents_per_minute: int


class AvailableNumberResponse(BaseModel):
    phone_number: str
    friendly_name: str | None
    locality: str | None
    region: str | None
    rental_cents: int


class PurchaseRequest(BaseModel):
    phone_number: str
    country: str
    inbound_workflow_id: int | None = None


class InboundWorkflowRequest(BaseModel):
    inbound_workflow_id: int | None = None


class PlatformNumberResponse(BaseModel):
    id: int
    address: str
    country_code: str | None
    rental_cents: int | None
    rental_minutes_equivalent: float | None
    next_renewal_at: str | None
    rental_status: str | None
    inbound_workflow_id: int | None
    is_active: bool


def _response(row) -> PlatformNumberResponse:
    return PlatformNumberResponse(
        id=row.id,
        address=row.address,
        country_code=row.country_code,
        rental_cents=row.rental_cents,
        rental_minutes_equivalent=(
            row.rental_cents / 100 if row.rental_cents is not None else None
        ),
        next_renewal_at=(
            row.next_renewal_at.isoformat() if row.next_renewal_at else None
        ),
        rental_status=row.rental_status,
        inbound_workflow_id=row.inbound_workflow_id,
        is_active=row.is_active,
    )


def _http(e: PlatformNumberError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/pricing", response_model=PricingResponse)
async def get_pricing(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    return PricingResponse(
        countries=[
            CountryPricing(
                country=c,
                rental_minutes=rental_cents_for_country(c) / 100,
                rental_cents=rental_cents_for_country(c),
            )
            for c in sorted(PLATFORM_NUMBER_COUNTRIES)
        ],
        carrier_cents_per_minute=PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE,
    )


@router.get("/search", response_model=list[AvailableNumberResponse])
async def search_numbers(
    country: str = Query(...),
    area_code: str | None = Query(None),
    contains: str | None = Query(None),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    try:
        results = await platform_numbers.search(country, area_code, contains)
    except PlatformNumberError as e:
        raise _http(e)
    rental = rental_cents_for_country(country)
    return [
        AvailableNumberResponse(**result, rental_cents=rental) for result in results
    ]


@router.post("/purchase", response_model=PlatformNumberResponse)
async def purchase_number(
    body: PurchaseRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    if body.inbound_workflow_id is not None:
        workflow_counts = await db_client.get_workflow_counts(
            organization_id=user.selected_organization_id
        )  # cheap existence sanity; strict ownership check below
        workflow = await db_client.get_workflow(
            body.inbound_workflow_id, organization_id=user.selected_organization_id
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow_not_found")
    try:
        row = await platform_numbers.purchase(
            user.selected_organization_id,
            phone_number=body.phone_number,
            country=body.country,
            inbound_workflow_id=body.inbound_workflow_id,
        )
    except PlatformNumberError as e:
        raise _http(e)
    return _response(row)


@router.get("", response_model=list[PlatformNumberResponse])
async def list_numbers(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_platform_numbers_for_org(
        user.selected_organization_id
    )
    return [_response(r) for r in rows]


@router.post("/{phone_number_id}/release")
async def release_number(
    phone_number_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    try:
        await platform_numbers.release(
            user.selected_organization_id, phone_number_id
        )
    except PlatformNumberError as e:
        raise _http(e)
    return {"status": "released"}


@router.put("/{phone_number_id}/inbound-workflow", response_model=PlatformNumberResponse)
async def set_inbound_workflow(
    phone_number_id: int,
    body: InboundWorkflowRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_platform_numbers_for_org(
        user.selected_organization_id
    )
    row = next((r for r in rows if r.id == phone_number_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="number_not_found")
    if body.inbound_workflow_id is not None:
        workflow = await db_client.get_workflow(
            body.inbound_workflow_id, organization_id=user.selected_organization_id
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow_not_found")
    updated = await db_client.update_phone_number(
        phone_number_id,
        row.telephony_configuration_id,
        inbound_workflow_id=body.inbound_workflow_id,
        clear_inbound_workflow=body.inbound_workflow_id is None,
    )
    return _response(updated)
```

(Two lookups to verify before writing: the exact org-scoped workflow getter — grep `def get_workflow` in `api/db/workflow_client.py` and use the variant that takes `organization_id`, dropping the `get_workflow_counts` sanity line if redundant; and `update_phone_number`'s exact signature at `api/db/telephony_phone_number_client.py:264` — match its parameter names.)

Mount in `api/routes/main.py` (follow lines 7-49 pattern):

```python
from api.routes.platform_numbers import router as platform_numbers_router
# ...
router.include_router(platform_numbers_router)
```

- [ ] **Step 4: Add platform guards to generic routes**

In `api/routes/organization.py`:
- In `update_telephony_configuration` (~line 608) and `delete_telephony_configuration` (~line 664), after the existing `_ensure_config_belongs_to_org` resolution:

```python
    if getattr(config, "is_platform", False):
        raise HTTPException(status_code=403, detail="platform_managed")
```

- In the phone-number `PUT` (~line 835) and `DELETE` (~line 900) handlers, after the phone row is resolved:

```python
    if getattr(phone_number, "is_platform", False):
        raise HTTPException(status_code=403, detail="platform_managed")
```

(Match the local variable names in each handler.)

Superadmin listing — append to `api/routes/billing_admin.py`:

```python
class PlatformNumberAdminResponse(BaseModel):
    id: int
    organization_id: int
    address: str
    country_code: str | None
    rental_cents: int | None
    next_renewal_at: str | None
    rental_status: str | None
    is_active: bool


@router.get("/platform-numbers", response_model=list[PlatformNumberAdminResponse])
async def list_platform_numbers_admin(user=Depends(get_superuser)):
    """All platform-provisioned numbers across orgs (spec §8 oversight)."""
    rows = await db_client.list_all_platform_numbers()
    return [
        PlatformNumberAdminResponse(
            id=row.id,
            organization_id=org_id,
            address=row.address,
            country_code=row.country_code,
            rental_cents=row.rental_cents,
            next_renewal_at=(
                row.next_renewal_at.isoformat() if row.next_renewal_at else None
            ),
            rental_status=row.rental_status,
            is_active=row.is_active,
        )
        for row, org_id in rows
    ]
```

- [ ] **Step 5: Run tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_platform_number_routes.py api/tests/test_billing_admin_plans.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/routes/platform_numbers.py api/routes/main.py api/routes/organization.py api/routes/billing_admin.py api/tests/test_platform_number_routes.py
git commit -m "feat(saas-p3): platform number routes, superadmin oversight, config guards"
```

---

### Task 8: Full regression + OpenAPI/client regeneration

- [ ] **Step 1: Full backend suite**

Run: `find . -name '._*' -delete; source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q`
Expected: only the known pre-existing failures (ts_bridge Node/ESM, org-membership/superuser isolation flakiness, 3 billing_service FK tests — verify any new failure against the base branch before treating it as a regression, same method as Phase 2).

- [ ] **Step 2: Regenerate OpenAPI (minified, matching repo format) + UI client**

```bash
source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "
import json
from api.app import app
with open('docs/api-reference/openapi.json', 'w') as f:
    json.dump(app.openapi(), f, separators=(',', ':'))
"
# serve spec for codegen, then:
(uvicorn api.app:app --port 8000 &) && sleep 8 && cd ui && npm run generate-client && cd .. && pkill -f "uvicorn api.app:app"
```

Expected new SDK functions: `getPricingApiV1PlatformNumbersPricingGet`, `searchNumbersApiV1PlatformNumbersSearchGet`, `purchaseNumberApiV1PlatformNumbersPurchasePost`, `listNumbersApiV1PlatformNumbersGet`, `releaseNumberApiV1PlatformNumbersPhoneNumberIdReleasePost`, `setInboundWorkflowApiV1PlatformNumbersPhoneNumberIdInboundWorkflowPut`, `listPlatformNumbersAdminApiV1SuperuserPlatformNumbersGet`.

- [ ] **Step 3: Commit**

```bash
git add docs/api-reference/openapi.json ui/src/client
git commit -m "chore(saas-p3): regenerate openapi spec + typed client"
```

---

### Task 9: UI — Phone numbers page ("Get a number")

**Files:**
- Create: `ui/src/app/phone-numbers/page.tsx`
- Create: `ui/src/components/telephony/GetNumberDialog.tsx`
- Modify: `ui/src/components/layout/AppSidebar.tsx` (add nav item to the `BUILD` group, saas-gated)
- Test: `cd ui && npx tsc --noEmit && npm run lint`

**Interfaces:**
- Consumes: generated SDK from Task 8; `useAppConfig()` (`config?.deploymentMode === "saas"` gate — redirect pattern from `ui/src/app/billing/plans/page.tsx`); `useAuth()` fetch-guard convention; `getWorkflowsSummaryApiV1WorkflowSummaryGet` for the inbound-agent `Select` (copy the exact pattern from `ui/src/components/telephony/PhoneNumberDialog.tsx` incl. the `"__none__"` sentinel); shadcn `Dialog`/`Select`/`Table`/`AlertDialog` primitives; `detailFromError` (`@/lib/apiError`).
- Produces: `/phone-numbers` page — table of platform numbers (Number, Country, Status badge [`active`=default, `past_due`=destructive, `released`=secondary], Renews on, Rental (min/mo), Inbound agent Select (calls `setInboundWorkflowApi...Put` on change), Release button with `AlertDialog` confirm); "Get a number" button opening `GetNumberDialog`; a `past_due` amber banner ("Top up at /billing or this number will be released") when any number is past due. `GetNumberDialog`: two internal steps in one `DialogContent` — step 1: country `Select` (from pricing endpoint, shows rental min/mo) + area-code `Input` + Search button; step 2: results list, each row shows number + locality + rental, Buy button (optional inbound agent `Select` above the list); on purchase success → toast, close, refresh list. Surface backend `detail` strings via `detailFromError` (esp. 402 insufficient balance and Twilio 502 messages).
- Sidebar: `{ title: "Phone Numbers", url: "/phone-numbers", icon: Hash }` (lucide `Hash` or `PhoneCall`), rendered only when `config?.deploymentMode === "saas"` (follow how AppSidebar already branches on deploymentMode at ~line 192).

- [ ] **Step 1: Build `GetNumberDialog.tsx`** — controlled `open/onOpenChange` dialog modeled on `ConfigFormDialog.tsx`; internal `step` state (`"search" | "results"`); state: `country`, `areaCode`, `results`, `busyNumber`, `inboundWorkflowId`, `workflows`; fetch pricing on open (`getPricingApiV1PlatformNumbersPricingGet`), workflows via `getWorkflowsSummaryApiV1WorkflowSummaryGet({ query: { status: "active" } })`; Search → `searchNumbersApiV1PlatformNumbersSearchGet({ query: { country, area_code: areaCode || undefined } })`; Buy → `purchaseNumberApiV1PlatformNumbersPurchasePost({ body: { phone_number, country, inbound_workflow_id } })`; always check `response.error` and toast `detailFromError(...)`; call `props.onPurchased()` on success.

- [ ] **Step 2: Build `phone-numbers/page.tsx`** — saas-gate + auth-guard exactly like `billing/plans/page.tsx` (redirect `/` when not saas); fetch `listNumbersApiV1PlatformNumbersGet` + workflows; render header + "Get a number" `Button`, past-due banner, and the numbers `Table`; inbound-agent per-row `Select` (optimistic update, toast on error); Release via `AlertDialog` → `releaseNumberApiV1PlatformNumbersPhoneNumberIdReleasePost({ path: { phone_number_id } })` → refresh.

- [ ] **Step 3: Register sidebar item** in `AppSidebar.tsx` `BUILD` group, gated to saas mode.

- [ ] **Step 4: Verify**

Run: `cd ui && find . -name '._*' -delete; npx tsc --noEmit && npm run lint`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add ui/src/app/phone-numbers ui/src/components/telephony/GetNumberDialog.tsx ui/src/components/layout/AppSidebar.tsx
git commit -m "feat(saas-p3): phone numbers page with get-a-number and release flows"
```

---

### Task 10: Docs + smoke checklist

**Files:**
- Modify: `docs/SAAS_SETUP.md` (add "Step 6: Platform Twilio Numbers" before Troubleshooting, matching the Razorpay section's style)
- Create: `docs/superpowers/plans/phase3-smoke-checklist.md` (force-add — dir is gitignored)

- [ ] **Step 1: Document in `docs/SAAS_SETUP.md`**: platform account env vars (`PLATFORM_TWILIO_ACCOUNT_SID`, `PLATFORM_TWILIO_AUTH_TOKEN`); rental/carrier/grace env vars with defaults (`PLATFORM_NUMBER_RENTAL_MINUTES_DEFAULT=10`, `_IN=20`, `PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE=50`, `NUMBER_RENTAL_GRACE_DAYS=7`, `PLATFORM_NUMBER_COUNTRIES=US,CA,IN`); how subaccounts appear in the Twilio console (`voxagent-org-{id}`); the ARQ worker must be running for daily rental billing (cron 02:00 UTC); India regulatory-bundle caveat (Twilio errors surface verbatim; handle bundles manually in the console); superadmin oversight endpoint `GET /api/v1/superuser/platform-numbers`.

- [ ] **Step 2: Write `docs/superpowers/plans/phase3-smoke-checklist.md`**

```markdown
# Phase 3 smoke checklist (live Twilio test credentials)

Needs: saas deployment with ARQ worker, platform Twilio account (trial ok for
US), org with ledger balance >= rental.

- [ ] /phone-numbers shows pricing; search US by area code returns numbers.
- [ ] Buy a number: subaccount voxagent-org-{id} appears in Twilio console,
      number purchased inside it, VoiceUrl = .../api/v1/telephony/inbound/run.
- [ ] Ledger shows number_rental debit (minutes-equivalent) immediately.
- [ ] Assign an inbound agent; call the number from a phone -> agent answers
      (inbound resolver matches subaccount sid + called number).
- [ ] Post-call ledger shows AI debit + carrier_debit for the platform call.
- [ ] BYO-number call shows NO carrier_debit.
- [ ] Number appears as an outbound caller-id option (from_numbers pool).
- [ ] Platform config/number invisible to generic telephony edit/delete (403).
- [ ] Drain balance below rental, run charge_number_rentals (trigger via
      `arq api.tasks.arq.WorkerSettings` locally or wait for cron) ->
      rental_status past_due + banner on /phone-numbers.
- [ ] Top up -> next run charges and returns to active.
- [ ] Set next_renewal_at 8 days back with low balance -> run releases the
      number at Twilio and marks released.
- [ ] Release flow from UI removes the number at Twilio.
- [ ] Superadmin GET /superuser/platform-numbers lists all numbers.
- [ ] OSS mode: /phone-numbers redirects; routes 404.
```

- [ ] **Step 3: Commit**

```bash
git add docs/SAAS_SETUP.md
git add -f docs/superpowers/plans/phase3-smoke-checklist.md
git commit -m "docs(saas-p3): platform numbers setup guide + smoke checklist"
```

---

## Self-Review Notes

- **Spec §8 coverage:** search by country/area code → Tasks 3/4/7; subaccount per org → Task 4 `_ensure_subaccount`; one-click purchase + webhooks auto-configured → Task 4 (VoiceUrl set at purchase — no post-hoc sync needed); inbound routed to selected agent → existing `find_inbound_route_by_account` + `inbound_workflow_id` (purchase param + `PUT /inbound-workflow`); fixed monthly rental in minutes-equivalent → Tasks 3/4 (`rental_cents`, UI shows min/mo); per-minute carrier burn → Task 6; auto-renew monthly (ARQ) → Task 5; insufficient balance → warning then release after grace → Task 5 (`past_due` + UI banner; release at day 7); release flow → Tasks 4/7/9; superadmin oversight → Task 7; Twilio only US/CA+IN → country allowlist, regulatory errors passthrough.
- **Known deliberate gaps:** email warnings (Phase 4, no infra); Twilio actual-price reconciliation (flat carrier rate v1); `from_numbers` key in platform config credentials stays `[]` (the factory attaches live numbers from the phone table at load time — verify `_normalize_with_phone_numbers` at `services/telephony/factory.py:203` does this for outbound caller-id use).
- **Verify-before-write points flagged inline:** `create_telephony_configuration` param names (Task 2), `get_backend_endpoints` URL construction (Task 4), `_duration_seconds_from_usage_info` signature (Task 6), org-scoped `get_workflow` getter + `update_phone_number` signature (Task 7), organization.py handler variable names (Task 7).
- **Line numbers drift** as tasks land — treat them as anchors, re-locate by symbol.
