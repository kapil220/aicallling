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
