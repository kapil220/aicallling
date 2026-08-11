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
