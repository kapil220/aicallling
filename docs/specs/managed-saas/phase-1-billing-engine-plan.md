# Billing Engine Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, self-owned credit-billing engine to Dograh — credit ledger, per-architecture pricing, pre-call authorization, per-second post-call deduction, and mid-call hard cutoff — replacing the external MPS billing brain behind a feature flag.

**Architecture:** A new `BillingService` (pure domain logic) sits between the existing pre-call hook (`quota_service.authorize_workflow_run_start`) and post-call hook (`workflow_run_billing.report_workflow_run_platform_usage`). It reads/writes two new tables (`credit_ledger`, `pricing_rules`) and a cached `organizations.credit_balance_cents` column through a new `BillingClient` DB mixin. All balance mutations are serialized with `SELECT ... FOR UPDATE` on the org row. The engine is selected by a new `BILLING_ENGINE` flag; when unset, the existing MPS/OSS paths run unchanged.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy (async), Alembic, PostgreSQL, pytest + pytest-asyncio, loguru.

## Global Constraints

- Credit unit: **1 credit = 1 cent (US$0.01)**. All balances/amounts are **integer cents**. Never store money as float.
- Rounding: **per-second** — `cost_cents = round(duration_seconds × price_per_minute_cents / 60)`.
- Overdraft: **hard cutoff mid-call** — no negative balances permitted.
- Feature gate: new constant `BILLING_ENGINE` (env, default `"mps"`). Local engine active only when `BILLING_ENGINE == "local"`. When not local, behavior is byte-for-byte unchanged.
- Tenant isolation: every read/write is filtered/validated by `organization_id` (see `api/AGENTS.md`).
- DB access lives in `api/db/*_client.py` mixins; domain logic in `api/services/billing/`; routes stay thin.
- Tests run against the test DB: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest ...`.
- Idempotency: a run's debit uses idempotency key `debit:{workflow_run_id}`; unique per `(organization_id, idempotency_key)`.
- Migrations are created via `./scripts/makemigrate.sh "description"` and applied with `./scripts/migrate.sh`.

---

## File Structure

**Create:**
- `api/services/billing/__init__.py` — package marker.
- `api/services/billing/billing_service.py` — `BillingService`: rate resolution, authorize, credit, debit, affordability. Pure logic; depends on `db_client`.
- `api/services/billing/pricing.py` — `resolve_rate` + `RateResult`/`ArchitectureKey` dataclasses (rate resolution is isolated so it is unit-testable without DB).
- `api/db/billing_client.py` — `BillingClient` DB mixin: ledger + pricing-rule + balance persistence with row locking.
- `api/routes/billing_admin.py` — superuser credit/pricing endpoints (mounted under `/superuser`).
- `api/tests/test_billing_pricing.py` — unit tests for `resolve_rate`.
- `api/tests/test_billing_service.py` — unit tests for authorize/credit/debit/rounding/idempotency (DB-backed).
- `api/tests/test_billing_concurrency.py` — concurrent-debit serialization test.
- `api/tests/test_billing_admin_routes.py` — admin endpoint tests.

**Modify:**
- `api/db/models.py` — add `CreditLedgerModel`, `PricingRuleModel`, `OrganizationModel.credit_balance_cents`, relationships.
- `api/db/db_client.py` — add `BillingClient` to the `DBClient` base list + docstring line.
- `api/constants.py` — add `BILLING_ENGINE` + `MINIMUM_CREDIT_CENTS`.
- `api/services/quota_service.py` — add `local` authorization branch + stash rate/affordable-seconds.
- `api/services/workflow_run_billing.py` — add `local` deduction branch.
- `api/services/pipecat/run_pipeline.py` — cap `max_call_duration_seconds` by affordable seconds when local engine active.
- `api/routes/organization_usage.py` — add customer read endpoint for balance + ledger.
- `api/app.py` (or wherever routers mount) — include `billing_admin` router.

---

## Task 1: Feature flag & constants

**Files:**
- Modify: `api/constants.py`
- Test: `api/tests/test_billing_service.py` (import-smoke only in this task)

**Interfaces:**
- Produces: `BILLING_ENGINE: str` (`"mps"` default, `"local"` to enable), `MINIMUM_CREDIT_CENTS: int` (default `10` = $0.10), `BILLING_LOCAL = "local"` sentinel.

- [ ] **Step 1: Read the existing constant pattern**

Run: `grep -n "DEPLOYMENT_MODE\|os.getenv\|os.environ" api/constants.py | head`
Expected: shows the `os.getenv(...)` idiom used for `DEPLOYMENT_MODE`.

- [ ] **Step 2: Add the constants**

In `api/constants.py`, near `DEPLOYMENT_MODE`:

```python
# Billing engine selector: "mps" (external, default) or "local" (self-owned ledger).
BILLING_ENGINE = os.getenv("BILLING_ENGINE", "mps")
BILLING_LOCAL = "local"
# Minimum credit balance (in integer cents) required to authorize a call. 10 = $0.10.
MINIMUM_CREDIT_CENTS = int(os.getenv("MINIMUM_CREDIT_CENTS", "10"))
```

- [ ] **Step 3: Verify import**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "from api.constants import BILLING_ENGINE, MINIMUM_CREDIT_CENTS, BILLING_LOCAL; print(BILLING_ENGINE, MINIMUM_CREDIT_CENTS, BILLING_LOCAL)"`
Expected: `mps 10 local`

- [ ] **Step 4: Commit**

```bash
git add api/constants.py
git commit -m "feat(billing): add BILLING_ENGINE feature flag and credit constants"
```

---

## Task 2: Data models & migration

**Files:**
- Modify: `api/db/models.py`
- Migration: generated under `api/alembic/versions/`

**Interfaces:**
- Produces:
  - `CreditLedgerModel` (table `credit_ledger`): `id`, `organization_id`, `amount_cents:int`, `balance_after_cents:int`, `type:str` (`topup`/`debit`/`adjustment`/`refund`), `workflow_run_id:int|None`, `description:str|None`, `idempotency_key:str|None`, `created_by:int|None`, `created_at`.
  - `PricingRuleModel` (table `pricing_rules`): `id`, `organization_id:int|None`, `mode:str|None`, `llm_provider:str|None`, `stt_provider:str|None`, `tts_provider:str|None`, `realtime_provider:str|None`, `price_per_minute_cents:int`, `priority:int`, `is_active:bool`.
  - `OrganizationModel.credit_balance_cents:int` (default 0, not null).

- [ ] **Step 1: Add models to `api/db/models.py`**

After `APIKeyModel` (near line 195), add:

```python
class CreditLedgerModel(Base):
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    amount_cents = Column(Integer, nullable=False)  # signed: +topup/-debit
    balance_after_cents = Column(Integer, nullable=False)
    type = Column(String, nullable=False)  # topup | debit | adjustment | refund
    workflow_run_id = Column(
        Integer, ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    description = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    organization = relationship("OrganizationModel", back_populates="credit_ledger_entries")

    __table_args__ = (
        Index("ix_credit_ledger_org", "organization_id"),
        Index("ix_credit_ledger_org_created", "organization_id", "created_at"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="_credit_ledger_idem_uc"
        ),
    )


class PricingRuleModel(Base):
    __tablename__ = "pricing_rules"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )  # null = global default
    mode = Column(String, nullable=True)  # pipeline | realtime | null(any)
    llm_provider = Column(String, nullable=True)
    stt_provider = Column(String, nullable=True)
    tts_provider = Column(String, nullable=True)
    realtime_provider = Column(String, nullable=True)
    price_per_minute_cents = Column(Integer, nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default=text("0"))
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("ix_pricing_rules_org", "organization_id"),
        Index("ix_pricing_rules_active", "is_active"),
    )
```

- [ ] **Step 2: Add the balance column + relationship to `OrganizationModel`**

In `OrganizationModel` (after `price_per_second_usd`, line 152):

```python
    # Cached credit balance in integer cents. Source of truth is credit_ledger;
    # this is the row-locked fast-read/mutation anchor for the local billing engine.
    credit_balance_cents = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
```

And in its relationships block:

```python
    credit_ledger_entries = relationship(
        "CreditLedgerModel", back_populates="organization"
    )
```

- [ ] **Step 3: Generate the migration**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && ./scripts/makemigrate.sh "add local billing engine tables"`
Expected: a new file in `api/alembic/versions/` creating `credit_ledger`, `pricing_rules`, and adding `organizations.credit_balance_cents`.

- [ ] **Step 4: Inspect the migration**

Open the generated file. Verify: both tables created, the unique constraint `_credit_ledger_idem_uc` present, `credit_balance_cents` added with `server_default="0"` and `nullable=False`. Fix by hand if autogen missed the server_default.

- [ ] **Step 5: Apply and verify against test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh && python -c "import asyncio; from api.db.database import engine; from sqlalchemy import text; \
import asyncio; \
async def go():\n import api.db.models\n async with engine.begin() as c:\n  r=await c.execute(text(\"select to_regclass('credit_ledger'), to_regclass('pricing_rules')\"));\n  print(r.fetchone())\nasyncio.run(go())"`
Expected: both regclasses non-null (e.g. `('credit_ledger', 'pricing_rules')`).

- [ ] **Step 6: Commit**

```bash
git add api/db/models.py api/alembic/versions/
git commit -m "feat(billing): add credit_ledger, pricing_rules tables and org balance column"
```

---

## Task 3: Pricing resolution (`resolve_rate`)

**Files:**
- Create: `api/services/billing/__init__.py` (empty)
- Create: `api/services/billing/pricing.py`
- Test: `api/tests/test_billing_pricing.py`

**Interfaces:**
- Consumes: `PricingRuleModel` rows (as plain objects with the fields from Task 2).
- Produces:
  - `@dataclass ArchitectureKey`: `mode:str|None, llm_provider:str|None, stt_provider:str|None, tts_provider:str|None, realtime_provider:str|None`.
  - `@dataclass RateResult`: `price_per_minute_cents:int, matched_rule_id:int|None, source:str` (`"rule"|"org_fallback"|"global_default"|"none"`).
  - `def resolve_rate(arch: ArchitectureKey, rules: list, org_price_per_second_usd: float | None, global_default_cents: int | None) -> RateResult`.

Resolution order: filter `rules` to those whose non-null fields all match `arch` (a null rule field = wildcard). Among matches, pick the one with the most non-null (most specific) fields; tie-break by higher `priority`, then higher `id`. If none, fall back to `round(org_price_per_second_usd * 60 * 100)` if set (→ `org_fallback`), else `global_default_cents` (→ `global_default`), else `RateResult(0, None, "none")`.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_billing_pricing.py
from types import SimpleNamespace

from api.services.billing.pricing import ArchitectureKey, resolve_rate


def _rule(**kw):
    base = dict(id=1, organization_id=None, mode=None, llm_provider=None,
               stt_provider=None, tts_provider=None, realtime_provider=None,
               price_per_minute_cents=100, priority=0, is_active=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_most_specific_rule_wins():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    rules = [
        _rule(id=1, price_per_minute_cents=100),  # global wildcard
        _rule(id=2, mode="pipeline", price_per_minute_cents=90),
        _rule(id=3, mode="pipeline", llm_provider="openai",
              stt_provider="deepgram", tts_provider="elevenlabs",
              price_per_minute_cents=70),
    ]
    res = resolve_rate(arch, rules, None, None)
    assert res.price_per_minute_cents == 70
    assert res.matched_rule_id == 3
    assert res.source == "rule"


def test_priority_breaks_specificity_tie():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    rules = [
        _rule(id=1, mode="pipeline", price_per_minute_cents=90, priority=1),
        _rule(id=2, mode="pipeline", price_per_minute_cents=80, priority=5),
    ]
    res = resolve_rate(arch, rules, None, None)
    assert res.matched_rule_id == 2


def test_non_matching_rule_excluded():
    arch = ArchitectureKey("realtime", None, None, None, "openai_realtime")
    rules = [_rule(id=1, mode="pipeline", price_per_minute_cents=90)]
    res = resolve_rate(arch, rules, None, None)
    assert res.source == "none"


def test_org_fallback_used_when_no_rule():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    res = resolve_rate(arch, [], 0.01, None)  # $0.01/s -> $0.60/min -> 60c
    assert res.price_per_minute_cents == 60
    assert res.source == "org_fallback"


def test_global_default_last_resort():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    res = resolve_rate(arch, [], None, 50)
    assert res.price_per_minute_cents == 50
    assert res.source == "global_default"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_pricing.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.billing.pricing`.

- [ ] **Step 3: Implement**

```python
# api/services/billing/__init__.py
```
(empty file)

```python
# api/services/billing/pricing.py
"""Pure per-architecture pricing resolution for the local billing engine."""

from dataclasses import dataclass

_MATCH_FIELDS = ("mode", "llm_provider", "stt_provider", "tts_provider", "realtime_provider")


@dataclass(frozen=True)
class ArchitectureKey:
    mode: str | None = None
    llm_provider: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    realtime_provider: str | None = None


@dataclass(frozen=True)
class RateResult:
    price_per_minute_cents: int
    matched_rule_id: int | None
    source: str  # rule | org_fallback | global_default | none


def _rule_matches(rule, arch: ArchitectureKey) -> bool:
    for field in _MATCH_FIELDS:
        rule_val = getattr(rule, field)
        if rule_val is not None and rule_val != getattr(arch, field):
            return False
    return True


def _specificity(rule) -> int:
    return sum(1 for field in _MATCH_FIELDS if getattr(rule, field) is not None)


def resolve_rate(
    arch: ArchitectureKey,
    rules: list,
    org_price_per_second_usd: float | None,
    global_default_cents: int | None,
) -> RateResult:
    candidates = [r for r in rules if getattr(r, "is_active", True) and _rule_matches(r, arch)]
    if candidates:
        best = max(candidates, key=lambda r: (_specificity(r), r.priority, r.id))
        return RateResult(int(best.price_per_minute_cents), best.id, "rule")
    if org_price_per_second_usd:
        return RateResult(round(org_price_per_second_usd * 60 * 100), None, "org_fallback")
    if global_default_cents is not None:
        return RateResult(int(global_default_cents), None, "global_default")
    return RateResult(0, None, "none")
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_pricing.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/__init__.py api/services/billing/pricing.py api/tests/test_billing_pricing.py
git commit -m "feat(billing): per-architecture rate resolution"
```

---

## Task 4: `BillingClient` DB mixin (ledger + balance, row-locked)

**Files:**
- Create: `api/db/billing_client.py`
- Modify: `api/db/db_client.py`
- Test: `api/tests/test_billing_service.py` (partial — client-level tests)

**Interfaces:**
- Consumes: `CreditLedgerModel`, `PricingRuleModel`, `OrganizationModel` (Task 2).
- Produces methods on `db_client`:
  - `async get_credit_balance_cents(organization_id: int) -> int`
  - `async list_pricing_rules(organization_id: int | None) -> list[PricingRuleModel]` (returns global rules + this org's rules)
  - `async apply_ledger_entry(*, organization_id, amount_cents, type, workflow_run_id=None, description=None, idempotency_key=None, created_by=None) -> CreditLedgerModel` — **atomic**: locks the org row (`FOR UPDATE`), enforces idempotency (returns the existing row if the `(org, idempotency_key)` already exists), computes `balance_after_cents = balance + amount_cents`, rejects if it would go below zero for `debit` (caller pre-checks, but this is the safety net → clamp to 0 and log), writes the ledger row, updates `organizations.credit_balance_cents`.
  - `async create_pricing_rule(**fields) -> PricingRuleModel`
  - `async get_pricing_rule(rule_id, organization_id) -> PricingRuleModel | None`

- [ ] **Step 1: Write the failing test (idempotency + balance update)**

```python
# api/tests/test_billing_service.py
import pytest

from api.db import db_client
from api.db.models import OrganizationModel


async def _make_org(session_factory, provider_id="org_bill_1"):
    from api.db.database import async_session
    async with async_session() as s:
        org = OrganizationModel(provider_id=provider_id, credit_balance_cents=0)
        s.add(org)
        await s.commit()
        await s.refresh(org)
        return org.id


@pytest.mark.asyncio
async def test_apply_ledger_topup_updates_balance():
    org_id = await _make_org(None, "org_bill_topup")
    await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=500, type="topup", description="seed"
    )
    assert await db_client.get_credit_balance_cents(org_id) == 500


@pytest.mark.asyncio
async def test_apply_ledger_idempotent():
    org_id = await _make_org(None, "org_bill_idem")
    key = "debit:999"
    a = await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=-100, type="debit",
        idempotency_key=key, workflow_run_id=None,
    )
    b = await db_client.apply_ledger_entry(
        organization_id=org_id, amount_cents=-100, type="debit",
        idempotency_key=key, workflow_run_id=None,
    )
    assert a.id == b.id
    assert await db_client.get_credit_balance_cents(org_id) == -0  # one debit only... but clamp
```

Note: the second test seeds no credit; the debit clamps at 0 (safety net). Adjust the final assert to `== 0` after seeding is added in Task 6 tests — for this task, first topup 100 then assert 0. Update the test to topup 100 before the two debits and assert balance `== 0`.

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_service.py -v`
Expected: FAIL — `AttributeError: 'DBClient' object has no attribute 'apply_ledger_entry'`.

- [ ] **Step 3: Implement `api/db/billing_client.py`**

```python
from typing import Optional

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import CreditLedgerModel, OrganizationModel, PricingRuleModel


class BillingClient(BaseDBClient):
    async def get_credit_balance_cents(self, organization_id: int) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel.credit_balance_cents).where(
                    OrganizationModel.id == organization_id
                )
            )
            row = result.scalar_one_or_none()
            return int(row or 0)

    async def list_pricing_rules(
        self, organization_id: Optional[int]
    ) -> list[PricingRuleModel]:
        async with self.async_session() as session:
            stmt = select(PricingRuleModel).where(PricingRuleModel.is_active.is_(True))
            if organization_id is None:
                stmt = stmt.where(PricingRuleModel.organization_id.is_(None))
            else:
                stmt = stmt.where(
                    (PricingRuleModel.organization_id == organization_id)
                    | (PricingRuleModel.organization_id.is_(None))
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def apply_ledger_entry(
        self,
        *,
        organization_id: int,
        amount_cents: int,
        type: str,
        workflow_run_id: Optional[int] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> CreditLedgerModel:
        async with self.async_session() as session:
            async with session.begin():
                # Idempotency short-circuit.
                if idempotency_key is not None:
                    existing = await session.execute(
                        select(CreditLedgerModel).where(
                            CreditLedgerModel.organization_id == organization_id,
                            CreditLedgerModel.idempotency_key == idempotency_key,
                        )
                    )
                    found = existing.scalars().first()
                    if found is not None:
                        return found

                # Row-lock the org balance to serialize concurrent mutations.
                org_row = await session.execute(
                    select(OrganizationModel)
                    .where(OrganizationModel.id == organization_id)
                    .with_for_update()
                )
                org = org_row.scalar_one()
                new_balance = int(org.credit_balance_cents) + int(amount_cents)
                if new_balance < 0:
                    # Safety net; callers pre-authorize. Clamp and record actual delta.
                    amount_cents = -int(org.credit_balance_cents)
                    new_balance = 0

                entry = CreditLedgerModel(
                    organization_id=organization_id,
                    amount_cents=int(amount_cents),
                    balance_after_cents=new_balance,
                    type=type,
                    workflow_run_id=workflow_run_id,
                    description=description,
                    idempotency_key=idempotency_key,
                    created_by=created_by,
                )
                session.add(entry)
                org.credit_balance_cents = new_balance
                await session.flush()
                await session.refresh(entry)
                return entry

    async def create_pricing_rule(self, **fields) -> PricingRuleModel:
        async with self.async_session() as session:
            async with session.begin():
                rule = PricingRuleModel(**fields)
                session.add(rule)
                await session.flush()
                await session.refresh(rule)
                return rule

    async def get_pricing_rule(
        self, rule_id: int, organization_id: Optional[int]
    ) -> Optional[PricingRuleModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(PricingRuleModel).where(PricingRuleModel.id == rule_id)
            )
            rule = result.scalars().first()
            if rule is None:
                return None
            if rule.organization_id not in (None, organization_id):
                return None
            return rule
```

- [ ] **Step 4: Register the mixin**

In `api/db/db_client.py`, add `from api.db.billing_client import BillingClient` and add `BillingClient,` to the `DBClient(...)` base list.

- [ ] **Step 5: Fix the test as noted, then run**

Update `test_apply_ledger_idempotent` to topup 100 before the two debits and assert final balance `== 0`.
Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_service.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/db/billing_client.py api/db/db_client.py api/tests/test_billing_service.py
git commit -m "feat(billing): BillingClient DB mixin with row-locked idempotent ledger"
```

---

## Task 5: `BillingService` — architecture extraction, authorize, credit, debit

**Files:**
- Create: `api/services/billing/billing_service.py`
- Test: extend `api/tests/test_billing_service.py`

**Interfaces:**
- Consumes: `resolve_rate`, `ArchitectureKey`, `RateResult` (Task 3); `db_client` billing methods (Task 4); `MINIMUM_CREDIT_CENTS` (Task 1); effective config from `get_effective_ai_model_configuration_for_workflow` (existing).
- Produces module-level async functions (stateless service):
  - `def architecture_from_config(effective_config) -> ArchitectureKey` — reads `is_realtime`, and the `.llm/.stt/.tts` provider values off the effective config object.
  - `async def resolve_rate_for(organization_id, effective_config) -> RateResult`
  - `async def get_balance_cents(organization_id) -> int`
  - `async def authorize(organization_id, rate: RateResult) -> bool` — `balance >= max(MINIMUM_CREDIT_CENTS, rate.price_per_minute_cents)` (one minute buffer).
  - `async def max_affordable_seconds(organization_id, rate: RateResult) -> int` — `floor(balance / (rate.price_per_minute_cents/60))`; returns a large int if rate is 0.
  - `async def credit(organization_id, amount_cents, type, *, description=None, created_by=None, idempotency_key=None)`
  - `async def debit_for_run(*, organization_id, workflow_run_id, duration_seconds, price_per_minute_cents) -> CreditLedgerModel` — `cost = round(duration_seconds * price_per_minute_cents / 60)`, idempotency key `f"debit:{workflow_run_id}"`.

- [ ] **Step 1: Write failing tests (rounding boundaries + authorize + affordability)**

```python
# append to api/tests/test_billing_service.py
import pytest

from api.services.billing import billing_service
from api.services.billing.pricing import RateResult


@pytest.mark.parametrize("seconds,rate,expected", [
    (0, 6000, 0), (1, 6000, 100), (30, 6000, 3000),
    (59, 6000, 5900), (60, 6000, 6000), (61, 6000, 6100), (90, 6000, 9000),
    (10, 100, 17),  # 10 * 100/60 = 16.67 -> 17
])
def test_cost_rounding(seconds, rate, expected):
    assert billing_service._cost_cents(seconds, rate) == expected


@pytest.mark.asyncio
async def test_authorize_requires_one_minute_buffer():
    org_id = await _make_org(None, "org_bill_auth")
    await billing_service.credit(org_id, 50, "topup")
    assert await billing_service.authorize(org_id, RateResult(100, None, "rule")) is False
    await billing_service.credit(org_id, 60, "topup")  # now 110 >= 100
    assert await billing_service.authorize(org_id, RateResult(100, None, "rule")) is True


@pytest.mark.asyncio
async def test_debit_for_run_idempotent_and_deducts():
    org_id = await _make_org(None, "org_bill_debit")
    await billing_service.credit(org_id, 10000, "topup")
    await billing_service.debit_for_run(
        organization_id=org_id, workflow_run_id=555,
        duration_seconds=90, price_per_minute_cents=6000,
    )
    await billing_service.debit_for_run(
        organization_id=org_id, workflow_run_id=555,
        duration_seconds=90, price_per_minute_cents=6000,
    )
    assert await billing_service.get_balance_cents(org_id) == 1000  # 10000 - 9000, once
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_service.py -k "rounding or authorize or debit_for_run" -v`
Expected: FAIL — `AttributeError`/`ModuleNotFoundError` for `billing_service`.

- [ ] **Step 3: Implement `api/services/billing/billing_service.py`**

```python
"""Local credit billing engine: rate resolution, authorization, ledger ops."""

import math
from typing import Any

from loguru import logger

from api.constants import MINIMUM_CREDIT_CENTS
from api.db import db_client
from api.services.billing.pricing import ArchitectureKey, RateResult, resolve_rate

# Global default price (cents/min) when no rule and no org rate exist. Configurable
# via a global pricing rule; this is the last-resort constant.
GLOBAL_DEFAULT_CENTS_PER_MINUTE: int | None = None


def _cost_cents(duration_seconds: float, price_per_minute_cents: int) -> int:
    return int(round(duration_seconds * price_per_minute_cents / 60))


def _provider_value(section: Any) -> str | None:
    provider = getattr(section, "provider", None)
    if provider is None:
        return None
    return getattr(provider, "value", provider)


def architecture_from_config(effective_config: Any) -> ArchitectureKey:
    is_realtime = bool(getattr(effective_config, "is_realtime", False))
    if is_realtime:
        rt = getattr(effective_config, "llm", None) or getattr(effective_config, "realtime", None)
        return ArchitectureKey(mode="realtime", realtime_provider=_provider_value(rt))
    return ArchitectureKey(
        mode="pipeline",
        llm_provider=_provider_value(getattr(effective_config, "llm", None)),
        stt_provider=_provider_value(getattr(effective_config, "stt", None)),
        tts_provider=_provider_value(getattr(effective_config, "tts", None)),
    )


async def resolve_rate_for(organization_id: int, effective_config: Any) -> RateResult:
    arch = architecture_from_config(effective_config)
    rules = await db_client.list_pricing_rules(organization_id)
    org = await db_client.get_organization_by_id(organization_id)
    org_pps = getattr(org, "price_per_second_usd", None) if org else None
    result = resolve_rate(arch, rules, org_pps, GLOBAL_DEFAULT_CENTS_PER_MINUTE)
    if result.source == "none":
        logger.warning(
            "No pricing rule/rate resolved for org {} arch {}", organization_id, arch
        )
    return result


async def get_balance_cents(organization_id: int) -> int:
    return await db_client.get_credit_balance_cents(organization_id)


async def authorize(organization_id: int, rate: RateResult) -> bool:
    balance = await get_balance_cents(organization_id)
    required = max(MINIMUM_CREDIT_CENTS, rate.price_per_minute_cents)
    return balance >= required


async def max_affordable_seconds(organization_id: int, rate: RateResult) -> int:
    if rate.price_per_minute_cents <= 0:
        return 10 ** 9
    balance = await get_balance_cents(organization_id)
    return int(math.floor(balance / (rate.price_per_minute_cents / 60)))


async def credit(
    organization_id: int,
    amount_cents: int,
    type: str,
    *,
    description: str | None = None,
    created_by: int | None = None,
    idempotency_key: str | None = None,
):
    return await db_client.apply_ledger_entry(
        organization_id=organization_id,
        amount_cents=amount_cents,
        type=type,
        description=description,
        created_by=created_by,
        idempotency_key=idempotency_key,
    )


async def debit_for_run(
    *,
    organization_id: int,
    workflow_run_id: int,
    duration_seconds: float,
    price_per_minute_cents: int,
):
    cost = _cost_cents(duration_seconds, price_per_minute_cents)
    return await db_client.apply_ledger_entry(
        organization_id=organization_id,
        amount_cents=-cost,
        type="debit",
        workflow_run_id=workflow_run_id,
        description=f"call {workflow_run_id}: {duration_seconds}s @ {price_per_minute_cents}c/min",
        idempotency_key=f"debit:{workflow_run_id}",
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_service.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/billing_service.py api/tests/test_billing_service.py
git commit -m "feat(billing): BillingService authorize/credit/debit with per-second rounding"
```

---

## Task 6: Concurrency — serialized debits

**Files:**
- Test: `api/tests/test_billing_concurrency.py`

**Interfaces:**
- Consumes: `billing_service`, `db_client` (Tasks 4–5).

- [ ] **Step 1: Write the failing/《racing》test**

```python
# api/tests/test_billing_concurrency.py
import asyncio

import pytest

from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service


async def _make_org(provider_id):
    from api.db.database import async_session
    async with async_session() as s:
        org = OrganizationModel(provider_id=provider_id, credit_balance_cents=0)
        s.add(org)
        await s.commit()
        await s.refresh(org)
        return org.id


@pytest.mark.asyncio
async def test_concurrent_debits_do_not_lose_updates():
    org_id = await _make_org("org_bill_concurrent")
    await billing_service.credit(org_id, 10000, "topup")

    # 10 concurrent debits of distinct runs, 60s @ 100c/min = 100c each => 1000c total
    await asyncio.gather(*[
        billing_service.debit_for_run(
            organization_id=org_id, workflow_run_id=1000 + i,
            duration_seconds=60, price_per_minute_cents=100,
        )
        for i in range(10)
    ])
    assert await billing_service.get_balance_cents(org_id) == 9000
```

- [ ] **Step 2: Run to verify it passes (row lock already implemented)**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_concurrency.py -v`
Expected: PASS. If it FAILS with a balance > 9000, the `with_for_update()` lock is not serializing — verify Task 4 Step 3 uses `.with_for_update()` and a single `session.begin()` transaction around read+write.

- [ ] **Step 3: Commit**

```bash
git add api/tests/test_billing_concurrency.py
git commit -m "test(billing): concurrent debits serialize via row lock"
```

---

## Task 7: Pre-call authorization wiring (local branch)

**Files:**
- Modify: `api/services/quota_service.py`
- Test: extend `api/tests/test_quota_service.py`

**Interfaces:**
- Consumes: `billing_service.resolve_rate_for/authorize/max_affordable_seconds`; `BILLING_ENGINE`, `BILLING_LOCAL`.
- Produces: when `BILLING_ENGINE == "local"`, `authorize_workflow_run_start` resolves the rate, authorizes against the local ledger, and stashes `{"price_per_minute_cents", "max_affordable_seconds", "rate_source"}` onto the run's `cost_info` (via `db_client.update_workflow_run`) for later settle + cutoff. Returns the existing `QuotaCheckResult` shape (`insufficient_credits` on failure).

- [ ] **Step 1: Write the failing test**

```python
# append to api/tests/test_quota_service.py
from unittest.mock import AsyncMock

import pytest

from api.services import quota_service as qs
from api.services.billing.pricing import RateResult


@pytest.mark.asyncio
async def test_local_authorize_rejects_insufficient(monkeypatch):
    monkeypatch.setattr(qs, "BILLING_ENGINE", "local")
    monkeypatch.setattr(qs.db_client, "get_workflow_by_id",
        AsyncMock(return_value=type("W", (), {"id": 1, "organization_id": 9, "user_id": 2, "workflow_configurations": {}})()))
    monkeypatch.setattr(qs.db_client, "get_user_by_id",
        AsyncMock(return_value=type("U", (), {"id": 2, "provider_id": "p", "selected_organization_id": 9})()))
    monkeypatch.setattr(qs, "get_effective_ai_model_configuration_for_workflow",
        AsyncMock(return_value=object()))
    monkeypatch.setattr(qs.billing_service, "resolve_rate_for",
        AsyncMock(return_value=RateResult(100, 1, "rule")))
    monkeypatch.setattr(qs.billing_service, "authorize", AsyncMock(return_value=False))

    result = await qs.authorize_workflow_run_start(workflow_id=1, workflow_run_id=7)
    assert result.has_quota is False
    assert result.error_code == "insufficient_credits"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_quota_service.py -k local_authorize -v`
Expected: FAIL — `AttributeError: module 'api.services.quota_service' has no attribute 'billing_service'`.

- [ ] **Step 3: Implement the local branch**

At the top of `api/services/quota_service.py` add imports:

```python
from api.constants import BILLING_ENGINE, BILLING_LOCAL, MINIMUM_CREDIT_CENTS
from api.services.billing import billing_service
```

Add a helper:

```python
async def _authorize_local_billing(
    *, organization_id: int, workflow_run_id: int | None, user_config: Any
) -> QuotaCheckResult:
    rate = await billing_service.resolve_rate_for(organization_id, user_config)
    if not await billing_service.authorize(organization_id, rate):
        return _insufficient_billing_v2_quota_result()
    if workflow_run_id:
        affordable = await billing_service.max_affordable_seconds(organization_id, rate)
        run = await db_client.get_workflow_run_by_id(workflow_run_id)
        cost_info = dict(getattr(run, "cost_info", None) or {}) if run else {}
        cost_info.update({
            "price_per_minute_cents": rate.price_per_minute_cents,
            "max_affordable_seconds": affordable,
            "rate_source": rate.source,
        })
        await db_client.update_workflow_run(workflow_run_id, cost_info=cost_info)
    return QuotaCheckResult(has_quota=True)
```

In `authorize_workflow_run_start`, immediately after `user_config` is resolved (after line 370) and before the `DEPLOYMENT_MODE != "oss"` block, insert:

```python
        if BILLING_ENGINE == BILLING_LOCAL:
            return await _authorize_local_billing(
                organization_id=workflow.organization_id,
                workflow_run_id=workflow_run_id,
                user_config=user_config,
            )
```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_quota_service.py -v`
Expected: new test passes; existing tests still pass (local branch only taken when flag set).

- [ ] **Step 5: Commit**

```bash
git add api/services/quota_service.py api/tests/test_quota_service.py
git commit -m "feat(billing): local pre-call authorization branch in quota_service"
```

---

## Task 8: Mid-call hard cutoff by affordable seconds

**Files:**
- Modify: `api/services/pipecat/run_pipeline.py`
- Test: `api/tests/test_billing_cutoff.py`

**Interfaces:**
- Consumes: `cost_info["max_affordable_seconds"]` stashed in Task 7; existing `max_call_duration_seconds` mechanism (run_pipeline.py:359-367).
- Produces: when local engine active and the run has `max_affordable_seconds`, the effective cap becomes `min(configured_max, max_affordable_seconds)`.

- [ ] **Step 1: Locate the cap assignment**

Run: `grep -n "max_call_duration_seconds" api/services/pipecat/run_pipeline.py`
Expected: shows the line(s) where `max_call_duration_seconds` is computed/assigned (~359-367, wired ~677).

- [ ] **Step 2: Write the failing unit test for the helper**

```python
# api/tests/test_billing_cutoff.py
from api.services.pipecat.run_pipeline import _apply_affordable_cap


def test_cap_reduces_to_affordable():
    assert _apply_affordable_cap(300, {"max_affordable_seconds": 120}) == 120


def test_cap_keeps_configured_when_affordable_higher():
    assert _apply_affordable_cap(300, {"max_affordable_seconds": 100000}) == 300


def test_cap_noop_without_affordable():
    assert _apply_affordable_cap(300, {}) == 300
    assert _apply_affordable_cap(300, None) == 300
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_cutoff.py -v`
Expected: FAIL — `ImportError: cannot import name '_apply_affordable_cap'`.

- [ ] **Step 4: Implement the helper and apply it**

Add to `api/services/pipecat/run_pipeline.py`:

```python
def _apply_affordable_cap(configured_max_seconds: int, cost_info: dict | None) -> int:
    affordable = (cost_info or {}).get("max_affordable_seconds")
    if affordable is None:
        return configured_max_seconds
    return min(configured_max_seconds, int(affordable))
```

Then, where `max_call_duration_seconds` is finalized, wrap it (only when `BILLING_ENGINE == BILLING_LOCAL`):

```python
from api.constants import BILLING_ENGINE, BILLING_LOCAL
...
if BILLING_ENGINE == BILLING_LOCAL:
    max_call_duration_seconds = _apply_affordable_cap(
        max_call_duration_seconds, getattr(workflow_run, "cost_info", None)
    )
```

(Use the local `workflow_run` object already available in the pipeline setup; if only the id is available, load `cost_info` via `db_client.get_workflow_run_by_id`.)

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_cutoff.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add api/services/pipecat/run_pipeline.py api/tests/test_billing_cutoff.py
git commit -m "feat(billing): cap call duration by affordable credits (mid-call cutoff)"
```

---

## Task 9: Post-call deduction wiring (local branch)

**Files:**
- Modify: `api/services/workflow_run_billing.py`
- Test: extend `api/tests/test_workflow_run_billing.py`

**Interfaces:**
- Consumes: `billing_service.debit_for_run`; `cost_info["price_per_minute_cents"]` (Task 7); `usage_info["call_duration_seconds"]`; `BILLING_ENGINE`.
- Produces: when `BILLING_ENGINE == "local"`, `report_workflow_run_platform_usage` debits the org ledger by the run's priced cost (idempotent) and returns without touching MPS.

- [ ] **Step 1: Write the failing test**

```python
# append to api/tests/test_workflow_run_billing.py
@pytest.mark.asyncio
async def test_local_billing_debits_run(monkeypatch):
    from api.services import workflow_run_billing as mod

    run = _make_workflow_run()
    run.initial_context = {}
    run.cost_info = {"price_per_minute_cents": 6000}
    run.usage_info = {"call_duration_seconds": 90}

    debit = AsyncMock()
    monkeypatch.setattr(mod, "BILLING_ENGINE", "local")
    monkeypatch.setattr(mod.billing_service, "debit_for_run", debit)

    await mod.report_workflow_run_platform_usage(run)

    debit.assert_awaited_once_with(
        organization_id=42, workflow_run_id=run.id,
        duration_seconds=90.0, price_per_minute_cents=6000,
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_workflow_run_billing.py -k local_billing -v`
Expected: FAIL — `AttributeError` for `billing_service`.

- [ ] **Step 3: Implement the local branch**

In `api/services/workflow_run_billing.py` add imports:

```python
from api.constants import BILLING_ENGINE, BILLING_LOCAL
from api.services.billing import billing_service
```

Add helper + early branch inside `report_workflow_run_platform_usage`, right after the `is_completed` guard and `organization_id` resolution:

```python
    if BILLING_ENGINE == BILLING_LOCAL:
        await _debit_local_billing(workflow_run, organization_id)
        return
```

And the helper:

```python
async def _debit_local_billing(workflow_run, organization_id: int) -> None:
    cost_info = getattr(workflow_run, "cost_info", None) or {}
    rate = cost_info.get("price_per_minute_cents")
    duration = _duration_seconds_from_usage_info(workflow_run)
    if rate is None or duration is None:
        logger.warning(
            "Local billing skip for run {}: rate={} duration={}",
            workflow_run.id, rate, duration,
        )
        return
    await billing_service.debit_for_run(
        organization_id=organization_id,
        workflow_run_id=workflow_run.id,
        duration_seconds=float(duration),
        price_per_minute_cents=int(rate),
    )
```

Note: move the `organization_id` resolution above this branch if it currently sits after (the existing code resolves it at line 60 — the branch must come after that).

- [ ] **Step 4: Run to verify pass + no regression**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_workflow_run_billing.py -v`
Expected: new test passes; existing MPS tests unaffected.

- [ ] **Step 5: Commit**

```bash
git add api/services/workflow_run_billing.py api/tests/test_workflow_run_billing.py
git commit -m "feat(billing): local post-call deduction branch"
```

---

## Task 10: Superuser admin endpoints (grant credits, pricing rules, view balance)

**Files:**
- Create: `api/routes/billing_admin.py`
- Modify: wherever routers are included (find with grep below)
- Test: `api/tests/test_billing_admin_routes.py`

**Interfaces:**
- Consumes: `get_superuser` (`api/services/auth/depends.py:310`), `billing_service`, `db_client`.
- Produces routes under `/superuser`:
  - `POST /superuser/orgs/{org_id}/credits` body `{amount_cents:int, type:str="adjustment", description:str|None}` → writes ledger, returns new balance.
  - `GET /superuser/orgs/{org_id}/credits` → `{balance_cents, ledger:[...]}` (paginated).
  - `POST /superuser/pricing-rules` body = rule fields → creates rule.
  - `GET /superuser/pricing-rules?organization_id=` → list.

- [ ] **Step 1: Find the router include site**

Run: `grep -rn "include_router" api/app.py api/routes/*.py | grep -i "superuser\|prefix" | head`
Expected: shows how `superuser` router is mounted (prefix `/superuser`, tags). Mirror it.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_billing_admin_routes.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.services.auth.depends import get_superuser


@pytest.fixture
def superuser_override():
    app.dependency_overrides[get_superuser] = lambda: type("U", (), {"id": 1, "is_superuser": True})()
    yield
    app.dependency_overrides.pop(get_superuser, None)


@pytest.mark.asyncio
async def test_grant_credits(superuser_override):
    from api.db.database import async_session
    from api.db.models import OrganizationModel
    async with async_session() as s:
        org = OrganizationModel(provider_id="org_admin_grant", credit_balance_cents=0)
        s.add(org); await s.commit(); await s.refresh(org)
        org_id = org.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            f"/api/v1/superuser/orgs/{org_id}/credits",
            json={"amount_cents": 500, "type": "topup", "description": "grant"},
        )
    assert r.status_code == 200
    assert r.json()["balance_cents"] == 500
```

(Confirm the mount prefix — it may be `/api/v1/superuser`. Adjust the URL to match Step 1's finding.)

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_routes.py -v`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 4: Implement `api/routes/billing_admin.py`**

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.db import db_client
from api.services.auth.depends import get_superuser
from api.services.billing import billing_service

router = APIRouter(prefix="/superuser", tags=["billing-admin"])


class GrantCreditsRequest(BaseModel):
    amount_cents: int
    type: str = "adjustment"
    description: str | None = None


class PricingRuleRequest(BaseModel):
    organization_id: int | None = None
    mode: str | None = None
    llm_provider: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    realtime_provider: str | None = None
    price_per_minute_cents: int
    priority: int = 0


@router.post("/orgs/{org_id}/credits")
async def grant_credits(org_id: int, body: GrantCreditsRequest, user=Depends(get_superuser)):
    await billing_service.credit(
        org_id, body.amount_cents, body.type,
        description=body.description, created_by=getattr(user, "id", None),
    )
    return {"balance_cents": await billing_service.get_balance_cents(org_id)}


@router.get("/orgs/{org_id}/credits")
async def get_credits(org_id: int, limit: int = 50, user=Depends(get_superuser)):
    balance = await billing_service.get_balance_cents(org_id)
    ledger = await db_client.list_ledger_entries(org_id, limit=limit)
    return {"balance_cents": balance, "ledger": [
        {"id": e.id, "amount_cents": e.amount_cents, "balance_after_cents": e.balance_after_cents,
         "type": e.type, "description": e.description, "created_at": e.created_at.isoformat()}
        for e in ledger
    ]}


@router.post("/pricing-rules")
async def create_pricing_rule(body: PricingRuleRequest, user=Depends(get_superuser)):
    rule = await db_client.create_pricing_rule(**body.model_dump())
    return {"id": rule.id}


@router.get("/pricing-rules")
async def list_pricing_rules(organization_id: int | None = None, user=Depends(get_superuser)):
    rules = await db_client.list_pricing_rules(organization_id)
    return [{"id": r.id, "organization_id": r.organization_id, "mode": r.mode,
             "llm_provider": r.llm_provider, "stt_provider": r.stt_provider,
             "tts_provider": r.tts_provider, "realtime_provider": r.realtime_provider,
             "price_per_minute_cents": r.price_per_minute_cents, "priority": r.priority}
            for r in rules]
```

- [ ] **Step 5: Add `list_ledger_entries` to `BillingClient`**

In `api/db/billing_client.py`:

```python
    async def list_ledger_entries(self, organization_id: int, limit: int = 50):
        from api.db.models import CreditLedgerModel
        async with self.async_session() as session:
            result = await session.execute(
                select(CreditLedgerModel)
                .where(CreditLedgerModel.organization_id == organization_id)
                .order_by(CreditLedgerModel.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
```

- [ ] **Step 6: Mount the router**

In the router-include site from Step 1, add `from api.routes import billing_admin` and `app.include_router(billing_admin.router, prefix="/api/v1")` (match the existing prefix convention exactly).

- [ ] **Step 7: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_admin_routes.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add api/routes/billing_admin.py api/db/billing_client.py api/app.py api/tests/test_billing_admin_routes.py
git commit -m "feat(billing): superuser endpoints for credits and pricing rules"
```

---

## Task 11: Customer-facing balance read endpoint

**Files:**
- Modify: `api/routes/organization_usage.py`
- Test: extend `api/tests/test_organization_usage_billing.py`

**Interfaces:**
- Consumes: `get_user_with_selected_organization` (`api/services/auth/depends.py:159`), `billing_service`, `db_client.list_ledger_entries`.
- Produces: `GET /organization/usage/credits` → `{balance_cents, ledger:[...]}` scoped to the caller's `selected_organization_id`.

- [ ] **Step 1: Write the failing test**

```python
# append to api/tests/test_organization_usage_billing.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.services.auth.depends import get_user_with_selected_organization


@pytest.mark.asyncio
async def test_customer_credits_read(monkeypatch):
    from api.db.database import async_session
    from api.db.models import OrganizationModel
    async with async_session() as s:
        org = OrganizationModel(provider_id="org_cust_credits", credit_balance_cents=1234)
        s.add(org); await s.commit(); await s.refresh(org)
        org_id = org.id

    app.dependency_overrides[get_user_with_selected_organization] = lambda: type(
        "U", (), {"id": 1, "selected_organization_id": org_id})()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/organization/usage/credits")
        assert r.status_code == 200
        assert r.json()["balance_cents"] == 1234
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
```

(Confirm the exact prefix of the usage router from the file; adjust URL.)

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_organization_usage_billing.py -k customer_credits -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement the endpoint**

In `api/routes/organization_usage.py`, mirroring the existing route style and auth dependency:

```python
from api.services.billing import billing_service


@router.get("/credits")
async def get_organization_credits(
    limit: int = 50,
    user=Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    balance = await billing_service.get_balance_cents(org_id)
    ledger = await db_client.list_ledger_entries(org_id, limit=limit)
    return {
        "balance_cents": balance,
        "ledger": [
            {"id": e.id, "amount_cents": e.amount_cents,
             "balance_after_cents": e.balance_after_cents, "type": e.type,
             "description": e.description, "created_at": e.created_at.isoformat()}
            for e in ledger
        ],
    }
```

(Use the router variable and auth import already present in that file.)

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_organization_usage_billing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/routes/organization_usage.py api/tests/test_organization_usage_billing.py
git commit -m "feat(billing): customer credits/ledger read endpoint"
```

---

## Task 12: Full-suite regression + end-to-end lifecycle test

**Files:**
- Test: `api/tests/test_billing_lifecycle.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the lifecycle test**

```python
# api/tests/test_billing_lifecycle.py
import pytest
from types import SimpleNamespace

from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service


async def _org(pid, cents):
    from api.db.database import async_session
    async with async_session() as s:
        o = OrganizationModel(provider_id=pid, credit_balance_cents=0)
        s.add(o); await s.commit(); await s.refresh(o)
    await billing_service.credit(o.id, cents, "topup")
    return o.id


@pytest.mark.asyncio
async def test_authorize_then_settle_reduces_balance():
    org_id = await _org("org_lifecycle", 10000)
    await db_client.create_pricing_rule(
        organization_id=org_id, mode="pipeline", llm_provider="openai",
        stt_provider="deepgram", tts_provider="elevenlabs",
        price_per_minute_cents=6000, priority=0,
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
        organization_id=org_id, workflow_run_id=42,
        duration_seconds=30, price_per_minute_cents=rate.price_per_minute_cents,
    )
    assert await billing_service.get_balance_cents(org_id) == 7000  # 10000 - 3000
```

- [ ] **Step 2: Run the full billing suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_billing_pricing.py api/tests/test_billing_service.py api/tests/test_billing_concurrency.py api/tests/test_billing_cutoff.py api/tests/test_billing_lifecycle.py api/tests/test_billing_admin_routes.py api/tests/test_quota_service.py api/tests/test_workflow_run_billing.py api/tests/test_organization_usage_billing.py -v`
Expected: all pass.

- [ ] **Step 3: Run the broader suite to check for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q -x`
Expected: no new failures introduced (pre-existing unrelated failures, if any, noted but not caused by this work).

- [ ] **Step 4: Commit**

```bash
git add api/tests/test_billing_lifecycle.py
git commit -m "test(billing): end-to-end authorize->settle lifecycle"
```

---

## Self-Review

**Spec coverage check (against `phase-1-billing-engine-core.md`):**
- Credit ledger + cent unit → Task 2 (models), Task 4 (client). ✓
- Per-architecture pricing rules + resolution → Task 2 (`PricingRuleModel`), Task 3 (`resolve_rate`). ✓
- Pre-call authorize → Task 7. ✓
- Per-second post-call deduct → Task 5 (`_cost_cents`), Task 9 (wiring). ✓
- Hard mid-call cutoff → Task 8. ✓
- Atomic/idempotent/concurrency → Task 4 (row lock + idempotency), Task 6 (race test). ✓
- Admin controls (grant credits, pricing) → Task 10. ✓
- Customer balance read → Task 11. ✓
- Feature-flagged, OSS/MPS untouched → Task 1 flag; every wiring branch guarded (Tasks 7, 8, 9). ✓
- Usage-cycle aggregate update → **GAP**: the spec mentions updating `OrganizationUsageCycleModel` on debit. This is covered indirectly by the existing usage aggregation path (`organization_usage_client.py` reads runs), but if the operator needs live cycle totals synced at debit time, add a follow-up step in Task 9 to call the existing usage-cycle updater. Left as an explicit note rather than silently dropped.

**Placeholder scan:** none — every code step contains real code; commands include expected output.

**Type consistency:** `RateResult`/`ArchitectureKey` used identically across Tasks 3, 5, 7. `apply_ledger_entry` signature identical in Tasks 4, 5, 10. `debit_for_run` signature identical in Tasks 5, 9, 12. `_apply_affordable_cap` identical in Task 8. ✓

**Note for implementer:** the exact router mount prefix (`/api/v1` vs `/api/v1/superuser`) and the effective-config attribute names (`.llm.provider` etc.) must be confirmed against the live code at implementation time (Steps that say "confirm/adjust"). These are the only two places where the plan defers to runtime inspection, and both have explicit verification steps.
