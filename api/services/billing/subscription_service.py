"""Razorpay subscription lifecycle → org state + ledger (spec §4/§5).

No rollover: each successful charge expires the previous period's remaining
balance (plan_period_reset) then grants the new allowance (plan_renewal).
Both entries are idempotent on the Razorpay event id via the ledger's
(organization_id, idempotency_key) unique constraint.
"""

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from api.db import db_client
from api.db.models import OrganizationModel
from api.services.billing import billing_service
from api.services.billing.trial import CENTS_PER_MINUTE


def _subscription_entity(event: dict) -> Optional[dict]:
    return (event.get("payload") or {}).get("subscription", {}).get("entity")


def _payment_entity(event: dict) -> Optional[dict]:
    return (event.get("payload") or {}).get("payment", {}).get("entity")


async def _resolve_org(sub: dict) -> Optional[OrganizationModel]:
    org = await db_client.get_org_by_razorpay_subscription_id(sub["id"])
    if org is not None:
        return org
    org_id = (sub.get("notes") or {}).get("organization_id")
    if org_id is None:
        return None
    return await db_client.get_organization_by_id(int(org_id))


def _period_end(sub: dict) -> Optional[datetime]:
    ts = sub.get("current_end")
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


async def _link_org(org: OrganizationModel, sub: dict, *, status: str) -> None:
    plan = None
    if sub.get("plan_id"):
        plan = await db_client.get_plan_by_razorpay_plan_id(sub["plan_id"])
    await db_client.update_org_subscription(
        org.id,
        plan_id=plan.id if plan else org.plan_id,
        razorpay_subscription_id=sub["id"],
        subscription_status=status,
        current_period_end=_period_end(sub),
    )


async def _handle_activated(event: dict, event_id: str) -> None:
    sub = _subscription_entity(event)
    org = await _resolve_org(sub) if sub else None
    if org is None:
        logger.warning(f"razorpay activated: no org resolvable (event {event_id})")
        return
    await _link_org(org, sub, status="active")


async def _handle_charged(event: dict, event_id: str) -> None:
    sub = _subscription_entity(event)
    org = await _resolve_org(sub) if sub else None
    if org is None:
        logger.warning(f"razorpay charged: no org resolvable (event {event_id})")
        return
    await _link_org(org, sub, status="active")
    plan = await db_client.get_plan_by_razorpay_plan_id(sub.get("plan_id") or "")
    if plan is None:
        logger.error(
            f"razorpay charged: unknown plan {sub.get('plan_id')} for org {org.id}"
        )
        return

    # 1. Expire the previous period's remaining balance (no rollover).
    balance = await billing_service.get_balance_cents(org.id)
    if balance > 0:
        await billing_service.credit(
            org.id,
            -balance,
            "plan_period_reset",
            description=f"Plan period reset ({plan.tier_key})",
            idempotency_key=f"razorpay:{event_id}:reset",
        )
    # 2. Grant the new period's allowance.
    await billing_service.credit(
        org.id,
        plan.included_minutes * CENTS_PER_MINUTE,
        "plan_renewal",
        description=f"{plan.display_name}: {plan.included_minutes} minutes",
        idempotency_key=f"razorpay:{event_id}:renewal",
    )
    # 3. Record the invoice for payment history.
    payment = _payment_entity(event)
    if payment:
        await db_client.record_subscription_invoice(
            organization_id=org.id,
            razorpay_payment_id=payment["id"],
            razorpay_subscription_id=sub["id"],
            amount_cents=int(payment.get("amount", 0)),
            currency=(payment.get("currency") or "INR").lower(),
            status="captured",
        )


async def _handle_status_only(event: dict, event_id: str, status: str) -> None:
    sub = _subscription_entity(event)
    org = await _resolve_org(sub) if sub else None
    if org is None:
        logger.warning(f"razorpay {status}: no org resolvable (event {event_id})")
        return
    await db_client.update_org_subscription(org.id, subscription_status=status)


async def _handle_payment_failed(event: dict, event_id: str) -> None:
    # Grace period: no state change until Razorpay emits subscription.halted.
    payment = _payment_entity(event)
    sub = _subscription_entity(event)
    org = await _resolve_org(sub) if sub else None
    if org is None or payment is None:
        logger.warning(f"razorpay payment.failed: unresolvable (event {event_id})")
        return
    await db_client.record_subscription_invoice(
        organization_id=org.id,
        razorpay_payment_id=payment["id"],
        razorpay_subscription_id=sub["id"] if sub else None,
        amount_cents=int(payment.get("amount", 0)),
        currency=(payment.get("currency") or "INR").lower(),
        status="failed",
    )


async def handle_event(event: dict, event_id: str) -> None:
    event_type = event.get("event")
    if event_type == "subscription.activated":
        await _handle_activated(event, event_id)
    elif event_type == "subscription.charged":
        await _handle_charged(event, event_id)
    elif event_type == "subscription.halted":
        await _handle_status_only(event, event_id, "halted")
    elif event_type == "subscription.cancelled":
        await _handle_status_only(event, event_id, "cancelled")
    elif event_type == "payment.failed":
        await _handle_payment_failed(event, event_id)
    else:
        logger.info(f"razorpay: ignoring event type {event_type}")
