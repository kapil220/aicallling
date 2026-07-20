"""Stripe-backed prepaid credit top-ups (Phase 3).

Owns everything Stripe-shaped: customer creation, Checkout Session creation, and
webhook event handling. Delegates all ledger/balance mutation to Phase 1's
``billing_service.credit(...)`` — this module never writes to ``credit_ledger``
directly. Idempotency for credits/refunds is keyed on the Stripe event id
(``stripe:{event_id}``), reusing the ledger's unique-key guard.
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


class RefundTooEarlyError(Exception):
    """Raised when charge.refunded arrives before the payment is recorded succeeded.

    The route layer converts this into a 5xx so Stripe retries the refund event later
    rather than crediting a refund against a payment we haven't confirmed as paid.
    """


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
    existing = (
        organization.stripe_customer_id
        or await db_client.get_org_stripe_customer_id(organization.id)
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

    # Stripe assigns the session id, and the PaymentModel row can't be created before
    # that id exists — so create the session first, create the row, then patch the
    # session metadata with the resulting payment_id (one extra API call in exchange
    # for having payment_id available in the webhook). The webhook keeps a
    # stripe_checkout_session_id fallback lookup as the resilience path.
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


async def _find_payment(obj: dict) -> PaymentModel | None:
    payment_id = (obj.get("metadata") or {}).get("payment_id")
    payment: PaymentModel | None = None
    if payment_id is not None:
        payment = await db_client.get_payment_by_id(int(payment_id))
    if payment is None and obj.get("id"):
        payment = await db_client.get_payment_by_checkout_session_id(obj["id"])
    return payment


async def handle_checkout_completed(event: dict) -> None:
    session = event["data"]["object"]
    event_id = event["id"]

    payment = await _find_payment(session)
    if payment is None:
        logger.error(
            "Stripe checkout.session.completed for unknown payment: session={} event={}",
            session.get("id"),
            event_id,
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
    obj = event["data"]["object"]
    payment = await _find_payment(obj)
    if payment is None:
        logger.error(
            "Stripe failure event for unknown payment: object={} event={}",
            obj.get("id"),
            event["id"],
        )
        return
    if payment.status == "succeeded":
        return

    failure_reason = (obj.get("last_payment_error", {}) or {}).get("message") or event[
        "type"
    ]
    await db_client.update_payment(
        payment.id, status="failed", failure_reason=failure_reason
    )


async def handle_charge_refunded(event: dict) -> None:
    charge = event["data"]["object"]
    payment_intent_id = charge.get("payment_intent")
    payment = await db_client.get_payment_by_payment_intent_id(payment_intent_id)
    if payment is None or payment.status not in (
        "succeeded",
        "partially_refunded",
        "refunded",
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
        description=f"Stripe refund for payment_intent {payment_intent_id}",
        idempotency_key=f"stripe:{event['id']}",
    )

    fully_refunded = amount_refunded >= payment.amount_cents_paid
    await db_client.update_payment(
        payment.id,
        status="refunded" if fully_refunded else "partially_refunded",
    )
