"""Superuser billing administration: grant/adjust credits and manage pricing rules.

These endpoints let an operator seed and inspect the local billing engine's state
(credit balances, ledger, per-architecture pricing) so it can be run and tested
end-to-end. All routes require the platform superuser.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import db_client
from api.services.auth.depends import get_superuser
from api.services.billing import billing_service

router = APIRouter(prefix="/superuser", tags=["billing-admin"])


class GrantCreditsRequest(BaseModel):
    amount_cents: int
    type: str = "adjustment"  # topup | adjustment | refund
    description: str | None = None


class PricingRuleRequest(BaseModel):
    organization_id: int | None = None
    mode: str | None = None
    llm_provider: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    realtime_provider: str | None = None
    price_per_minute_cents: int
    priority: int = 0


class PaymentPackRequest(BaseModel):
    pack_key: str
    display_name: str
    price_cents: int
    credits_granted: int
    currency: str = "usd"
    sort_order: int = 0


def _ledger_row(entry) -> dict:
    return {
        "id": entry.id,
        "amount_cents": entry.amount_cents,
        "balance_after_cents": entry.balance_after_cents,
        "type": entry.type,
        "workflow_run_id": entry.workflow_run_id,
        "description": entry.description,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@router.post("/orgs/{org_id}/credits")
async def grant_credits(
    org_id: int, body: GrantCreditsRequest, user=Depends(get_superuser)
):
    await billing_service.credit(
        org_id,
        body.amount_cents,
        body.type,
        description=body.description,
        created_by=getattr(user, "id", None),
    )
    return {"balance_cents": await billing_service.get_balance_cents(org_id)}


@router.get("/orgs/{org_id}/credits")
async def get_credits(org_id: int, limit: int = 50, user=Depends(get_superuser)):
    balance = await billing_service.get_balance_cents(org_id)
    ledger = await db_client.list_ledger_entries(org_id, limit=limit)
    return {"balance_cents": balance, "ledger": [_ledger_row(e) for e in ledger]}


@router.post("/pricing-rules")
async def create_pricing_rule(body: PricingRuleRequest, user=Depends(get_superuser)):
    rule = await db_client.create_pricing_rule(**body.model_dump())
    return {"id": rule.id}


@router.get("/pricing-rules")
async def list_pricing_rules(
    organization_id: int | None = None, user=Depends(get_superuser)
):
    rules = await db_client.list_pricing_rules(organization_id)
    return [
        {
            "id": r.id,
            "organization_id": r.organization_id,
            "mode": r.mode,
            "llm_provider": r.llm_provider,
            "stt_provider": r.stt_provider,
            "tts_provider": r.tts_provider,
            "realtime_provider": r.realtime_provider,
            "price_per_minute_cents": r.price_per_minute_cents,
            "priority": r.priority,
        }
        for r in rules
    ]


@router.post("/payment-packs")
async def create_payment_pack(body: PaymentPackRequest, user=Depends(get_superuser)):
    """Seed/manage the prepaid credit-pack catalog (Phase 3)."""
    from api.db.models import PaymentPackModel

    async with db_client.async_session() as session:
        pack = PaymentPackModel(**body.model_dump())
        session.add(pack)
        await session.commit()
        await session.refresh(pack)
    return {"id": pack.id, "pack_key": pack.pack_key}


class PlanRequest(BaseModel):
    tier_key: str
    display_name: str
    price_cents: int
    currency: str = "inr"
    included_minutes: int
    max_agents: int | None = None
    max_concurrent_calls: int = 2
    daily_call_cap: int | None = None
    max_active_campaigns: int | None = None
    razorpay_plan_id: str | None = None
    is_active: bool = True
    sort_order: int = 0


class PlanUpdateRequest(BaseModel):
    display_name: str | None = None
    price_cents: int | None = None
    currency: str | None = None
    included_minutes: int | None = None
    max_agents: int | None = None
    max_concurrent_calls: int | None = None
    daily_call_cap: int | None = None
    max_active_campaigns: int | None = None
    razorpay_plan_id: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class PlanAdminResponse(BaseModel):
    id: int
    tier_key: str
    display_name: str
    price_cents: int
    currency: str
    included_minutes: int
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None
    razorpay_plan_id: str | None
    is_active: bool
    sort_order: int


def _plan_admin_response(plan) -> PlanAdminResponse:
    return PlanAdminResponse(
        id=plan.id,
        tier_key=plan.tier_key,
        display_name=plan.display_name,
        price_cents=plan.price_cents,
        currency=plan.currency,
        included_minutes=plan.included_minutes,
        max_agents=plan.max_agents,
        max_concurrent_calls=plan.max_concurrent_calls,
        daily_call_cap=plan.daily_call_cap,
        max_active_campaigns=plan.max_active_campaigns,
        razorpay_plan_id=plan.razorpay_plan_id,
        is_active=plan.is_active,
        sort_order=plan.sort_order,
    )


@router.get("/plans", response_model=list[PlanAdminResponse])
async def list_plans_admin(user=Depends(get_superuser)):
    """Subscription plan catalog management (saas phase 2)."""
    return [_plan_admin_response(p) for p in await db_client.list_all_plans()]


@router.post("/plans", response_model=PlanAdminResponse)
async def create_plan_admin(body: PlanRequest, user=Depends(get_superuser)):
    plan = await db_client.create_plan(**body.model_dump())
    return _plan_admin_response(plan)


@router.patch("/plans/{plan_id}", response_model=PlanAdminResponse)
async def update_plan_admin(
    plan_id: int, body: PlanUpdateRequest, user=Depends(get_superuser)
):
    fields = body.model_dump(exclude_unset=True)
    plan = await db_client.update_plan(plan_id, **fields)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")
    return _plan_admin_response(plan)


@router.get("/payment-packs")
async def list_payment_packs(user=Depends(get_superuser)):
    packs = await db_client.list_active_payment_packs()
    return [
        {
            "id": p.id,
            "pack_key": p.pack_key,
            "display_name": p.display_name,
            "price_cents": p.price_cents,
            "credits_granted": p.credits_granted,
            "currency": p.currency,
        }
        for p in packs
    ]
