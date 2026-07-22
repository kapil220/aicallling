"""Customer-facing, org-scoped payment routes (Phase 3).

Prepaid credit packs via Stripe Checkout. All routes require
``BILLING_PAYMENTS_ENABLED`` and resolve the org from the caller's session.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.constants import BILLING_PAYMENTS_ENABLED, UI_APP_URL
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing import payment_service
from api.services.billing.providers.razorpay_provider import get_provider

router = APIRouter(prefix="/billing", tags=["billing"])


class PackResponse(BaseModel):
    pack_key: str
    display_name: str
    price_cents: int
    credits_granted: int
    currency: str


class CheckoutRequest(BaseModel):
    pack_key: str


class CheckoutResponse(BaseModel):
    checkout_url: str


class PaymentHistoryItem(BaseModel):
    id: int
    amount_cents_paid: int
    credits_granted: int
    currency: str
    status: str
    created_at: str


def _require_enabled() -> None:
    if not BILLING_PAYMENTS_ENABLED:
        raise HTTPException(status_code=404, detail="payments_not_enabled")


@router.get("/packs", response_model=list[PackResponse])
async def list_packs(user: UserModel = Depends(get_user_with_selected_organization)):
    _require_enabled()
    packs = await payment_service.list_active_packs()
    return [
        PackResponse(
            pack_key=p.pack_key,
            display_name=p.display_name,
            price_cents=p.price_cents,
            credits_granted=p.credits_granted,
            currency=p.currency,
        )
        for p in packs
    ]


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    pack = await db_client.get_payment_pack_by_key(body.pack_key)
    if pack is None or not pack.is_active:
        raise HTTPException(status_code=404, detail="pack_not_found")

    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization_not_found")

    base = UI_APP_URL.rstrip("/") if UI_APP_URL else ""
    try:
        result = await payment_service.create_checkout_session(
            org,
            pack,
            success_url=f"{base}/billing?checkout=success",
            cancel_url=f"{base}/billing?checkout=cancelled",
        )
    except payment_service.DuplicateCheckoutError:
        raise HTTPException(status_code=409, detail="checkout_already_pending")

    return CheckoutResponse(checkout_url=result.checkout_url)


@router.get("/payments", response_model=list[PaymentHistoryItem])
async def list_payments(
    limit: int = 50,
    cursor: int | None = None,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    payments = await db_client.list_payments_for_org(
        user.selected_organization_id, limit=limit, cursor=cursor
    )
    return [
        PaymentHistoryItem(
            id=p.id,
            amount_cents_paid=p.amount_cents_paid,
            credits_granted=p.credits_granted,
            currency=p.currency,
            status=p.status,
            created_at=p.created_at.isoformat() if p.created_at else "",
        )
        for p in payments
    ]


# --- Subscription plans (saas phase 2, Razorpay) ---------------------------


class PlanPublicResponse(BaseModel):
    tier_key: str
    display_name: str
    price_cents: int
    currency: str
    included_minutes: int
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None
    is_current: bool


class SubscriptionResponse(BaseModel):
    plan_tier: str | None
    plan_display_name: str | None
    subscription_status: str | None
    current_period_end: str | None
    included_minutes: int | None


class SubscribeRequest(BaseModel):
    tier_key: str


class CheckoutUrlResponse(BaseModel):
    checkout_url: str


class InvoiceResponse(BaseModel):
    id: int
    razorpay_payment_id: str
    amount_cents: int
    currency: str
    status: str
    created_at: str | None


@router.get("/plans", response_model=list[PlanPublicResponse])
async def list_plans(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    plans = await db_client.list_active_plans()
    return [
        PlanPublicResponse(
            tier_key=p.tier_key,
            display_name=p.display_name,
            price_cents=p.price_cents,
            currency=p.currency,
            included_minutes=p.included_minutes,
            max_agents=p.max_agents,
            max_concurrent_calls=p.max_concurrent_calls,
            daily_call_cap=p.daily_call_cap,
            max_active_campaigns=p.max_active_campaigns,
            is_current=bool(org and org.plan_id == p.id),
        )
        for p in plans
    ]


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    plan = await db_client.get_plan_by_id(org.plan_id) if org and org.plan_id else None
    return SubscriptionResponse(
        plan_tier=plan.tier_key if plan else None,
        plan_display_name=plan.display_name if plan else None,
        subscription_status=org.subscription_status if org else None,
        current_period_end=(
            org.current_period_end.isoformat()
            if org and org.current_period_end
            else None
        ),
        included_minutes=plan.included_minutes if plan else None,
    )


async def _start_checkout(org, tier_key: str) -> CheckoutUrlResponse:
    plan = await db_client.get_plan_by_tier_key(tier_key)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=404, detail="plan_not_found")
    if not plan.razorpay_plan_id:
        raise HTTPException(status_code=409, detail="plan_not_purchasable")
    checkout = await get_provider().create_subscription(
        razorpay_plan_id=plan.razorpay_plan_id, organization_id=org.id
    )
    # Store the pending subscription id so the activation webhook can resolve
    # the org even if Razorpay drops the notes field.
    await db_client.update_org_subscription(
        org.id, razorpay_subscription_id=checkout.provider_subscription_id
    )
    return CheckoutUrlResponse(checkout_url=checkout.checkout_url)


@router.post("/subscribe", response_model=CheckoutUrlResponse)
async def subscribe(
    body: SubscribeRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status == "active":
        raise HTTPException(status_code=409, detail="already_subscribed")
    return await _start_checkout(org, body.tier_key)


@router.post("/change-plan", response_model=CheckoutUrlResponse)
async def change_plan(
    body: SubscribeRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status != "active" or not org.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="no_active_subscription")
    # v1 plan change: cancel now, re-subscribe. Remaining minutes expire on the
    # new plan's first charge (plan_period_reset), per the no-rollover rule.
    await get_provider().cancel_subscription(
        org.razorpay_subscription_id, at_cycle_end=False
    )
    return await _start_checkout(org, body.tier_key)


@router.post("/cancel")
async def cancel_subscription(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org.subscription_status != "active" or not org.razorpay_subscription_id:
        raise HTTPException(status_code=409, detail="no_active_subscription")
    await get_provider().cancel_subscription(
        org.razorpay_subscription_id, at_cycle_end=True
    )
    return {"status": "cancellation_scheduled"}


@router.get("/invoices", response_model=list[InvoiceResponse])
async def list_invoices(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    _require_enabled()
    invoices = await db_client.list_subscription_invoices(user.selected_organization_id)
    return [
        InvoiceResponse(
            id=i.id,
            razorpay_payment_id=i.razorpay_payment_id,
            amount_cents=i.amount_cents,
            currency=i.currency,
            status=i.status,
            created_at=i.created_at.isoformat() if i.created_at else None,
        )
        for i in invoices
    ]
