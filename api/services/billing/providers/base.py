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
