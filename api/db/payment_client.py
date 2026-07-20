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
