"""RazorpayProvider — SDK fully mocked; asserts request shapes + signature path."""

from unittest.mock import MagicMock, patch

import pytest

from api.services.billing.providers import razorpay_provider
from api.services.billing.providers.base import SubscriptionCheckout


@pytest.fixture
def provider():
    p = razorpay_provider.RazorpayProvider(key_id="rzp_test_x", key_secret="secret")
    p._client = MagicMock()
    return p


async def test_create_subscription_returns_checkout(provider):
    provider._client.subscription.create.return_value = {
        "id": "sub_123",
        "short_url": "https://rzp.io/i/abc",
        "status": "created",
    }
    result = await provider.create_subscription(
        razorpay_plan_id="plan_abc", organization_id=42
    )
    assert result == SubscriptionCheckout(
        provider_subscription_id="sub_123", checkout_url="https://rzp.io/i/abc"
    )
    payload = provider._client.subscription.create.call_args.args[0]
    assert payload["plan_id"] == "plan_abc"
    assert payload["total_count"] >= 12
    assert payload["notes"]["organization_id"] == "42"


async def test_cancel_subscription_at_cycle_end(provider):
    await provider.cancel_subscription("sub_123", at_cycle_end=True)
    provider._client.subscription.cancel.assert_called_once_with(
        "sub_123", {"cancel_at_cycle_end": 1}
    )


async def test_cancel_subscription_immediate(provider):
    await provider.cancel_subscription("sub_123", at_cycle_end=False)
    provider._client.subscription.cancel.assert_called_once_with(
        "sub_123", {"cancel_at_cycle_end": 0}
    )


def test_verify_webhook_signature_delegates(provider):
    provider._client.utility.verify_webhook_signature.return_value = True
    with patch.object(razorpay_provider, "RAZORPAY_WEBHOOK_SECRET", "whsec"):
        assert provider.verify_webhook_signature(b'{"a":1}', "sig") is True


def test_verify_webhook_signature_false_on_error(provider):
    provider._client.utility.verify_webhook_signature.side_effect = Exception("bad sig")
    with patch.object(razorpay_provider, "RAZORPAY_WEBHOOK_SECRET", "whsec"):
        assert provider.verify_webhook_signature(b"{}", "sig") is False
