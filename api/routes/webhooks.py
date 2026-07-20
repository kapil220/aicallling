"""Stripe webhook receiver (Phase 3): unauthenticated, signature-verified.

The signature is verified on the raw request body via
``stripe.Webhook.construct_event``; an unverified payload is never processed.
Returns 200 once an event is durably processed or is a no-op duplicate; returns
500 on ``RefundTooEarlyError`` so Stripe retries later.
"""

import stripe
from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from api.constants import BILLING_PAYMENTS_ENABLED, STRIPE_WEBHOOK_SECRET
from api.services.billing import payment_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# checkout.session.completed -> credit; failures -> mark failed; refunds -> claw back.
_FAILURE_EVENTS = {"checkout.session.expired", "checkout.session.async_payment_failed"}


@router.post("/stripe")
async def stripe_webhook(request: Request):
    if not BILLING_PAYMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="payments_not_enabled")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Rejected Stripe webhook with bad signature: {}", e)
        raise HTTPException(status_code=400, detail="invalid_signature")

    event = dict(event)
    event_type = event["type"]

    if event_type == "checkout.session.completed":
        await payment_service.handle_checkout_completed(event)
    elif event_type in _FAILURE_EVENTS:
        await payment_service.handle_payment_failed(event)
    elif event_type == "charge.refunded":
        # RefundTooEarlyError intentionally propagates -> 500 so Stripe retries.
        await payment_service.handle_charge_refunded(event)
    else:
        logger.debug("Ignoring unhandled Stripe event type: {}", event_type)

    return {"received": True}
