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
