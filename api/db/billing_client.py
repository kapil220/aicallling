"""Database client for the local billing engine (credit ledger + pricing rules).

Balance mutations lock the organization row (``SELECT ... FOR UPDATE``) so
concurrent calls for one org serialize their read-modify-write and cannot each
authorize/deduct against the same credits. Ledger writes are idempotent on
``(organization_id, idempotency_key)``.
"""

from typing import Optional

from loguru import logger
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
        """Return active rules visible to an org: its own rules plus global rules.

        When ``organization_id`` is None only global (org-less) rules are returned.
        """
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
        """Append a ledger row and update the cached balance, atomically.

        Idempotent: if ``idempotency_key`` already exists for the org, the existing
        row is returned untouched. A debit that would drive the balance negative is
        clamped to zero (safety net — callers pre-authorize).
        """
        async with self.async_session() as session:
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
                logger.warning(
                    "Ledger entry for org {} would go negative ({}); clamping to 0",
                    organization_id,
                    new_balance,
                )
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
            await session.commit()
            await session.refresh(entry)
            return entry

    async def list_ledger_entries(
        self, organization_id: int, limit: int = 50
    ) -> list[CreditLedgerModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CreditLedgerModel)
                .where(CreditLedgerModel.organization_id == organization_id)
                .order_by(CreditLedgerModel.id.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def create_pricing_rule(self, **fields) -> PricingRuleModel:
        async with self.async_session() as session:
            rule = PricingRuleModel(**fields)
            session.add(rule)
            await session.commit()
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
