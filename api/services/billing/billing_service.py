"""Local credit billing engine: rate resolution, authorization, ledger ops.

Stateless module-level service. Pricing is by call architecture (mode + provider
tuple) resolved against ``pricing_rules``; charging is per-second from measured
call duration. All balance mutations go through the row-locked, idempotent
``db_client.apply_ledger_entry``.
"""

import math
from typing import Any

from loguru import logger

from api.constants import MINIMUM_CREDIT_CENTS
from api.db import db_client
from api.services.billing.pricing import ArchitectureKey, RateResult, resolve_rate

# Last-resort per-minute price (cents) when neither a pricing rule nor an org rate
# exists. Left None so an unpriced call fails authorization loudly rather than
# billing $0 silently. Operators set a global pricing rule to establish a floor.
GLOBAL_DEFAULT_CENTS_PER_MINUTE: int | None = None


def _cost_cents(duration_seconds: float, price_per_minute_cents: int) -> int:
    """Per-second charge: seconds x (rate / 60), rounded to the nearest cent."""
    return int(round(duration_seconds * price_per_minute_cents / 60))


def affordable_cap(configured_max_seconds: int, cost_info: dict | None) -> int:
    """Cap a call's max duration to the credits the org can afford (mid-call cutoff).

    ``max_affordable_seconds`` is stashed on the run's ``cost_info`` by the pre-call
    authorization. Returns the configured max unchanged when no affordability was
    recorded (e.g. local billing not active for this run).
    """
    affordable = (cost_info or {}).get("max_affordable_seconds")
    if affordable is None:
        return configured_max_seconds
    return min(configured_max_seconds, int(affordable))


def _provider_value(section: Any) -> str | None:
    provider = getattr(section, "provider", None)
    if provider is None:
        return None
    return getattr(provider, "value", provider)


def architecture_from_config(effective_config: Any) -> ArchitectureKey:
    """Derive the billable architecture key from an effective model config."""
    is_realtime = bool(getattr(effective_config, "is_realtime", False))
    if is_realtime:
        rt = getattr(effective_config, "llm", None) or getattr(
            effective_config, "realtime", None
        )
        return ArchitectureKey(mode="realtime", realtime_provider=_provider_value(rt))
    return ArchitectureKey(
        mode="pipeline",
        llm_provider=_provider_value(getattr(effective_config, "llm", None)),
        stt_provider=_provider_value(getattr(effective_config, "stt", None)),
        tts_provider=_provider_value(getattr(effective_config, "tts", None)),
    )


async def resolve_rate_for(organization_id: int, effective_config: Any) -> RateResult:
    arch = architecture_from_config(effective_config)
    rules = await db_client.list_pricing_rules(organization_id)
    org = await db_client.get_organization_by_id(organization_id)
    org_pps = getattr(org, "price_per_second_usd", None) if org else None
    result = resolve_rate(arch, rules, org_pps, GLOBAL_DEFAULT_CENTS_PER_MINUTE)
    if result.source == "none":
        logger.warning(
            "No pricing rule/rate resolved for org {} arch {}", organization_id, arch
        )
    return result


async def get_balance_cents(organization_id: int) -> int:
    return await db_client.get_credit_balance_cents(organization_id)


async def authorize(organization_id: int, rate: RateResult) -> bool:
    """Authorize if the balance covers at least one minute (and the minimum)."""
    if rate.price_per_minute_cents <= 0 and rate.source == "none":
        # No rate resolved -> fail closed.
        return False
    balance = await get_balance_cents(organization_id)
    required = max(MINIMUM_CREDIT_CENTS, rate.price_per_minute_cents)
    return balance >= required


async def max_affordable_seconds(organization_id: int, rate: RateResult) -> int:
    if rate.price_per_minute_cents <= 0:
        return 10**9
    balance = await get_balance_cents(organization_id)
    return int(math.floor(balance / (rate.price_per_minute_cents / 60)))


async def credit(
    organization_id: int,
    amount_cents: int,
    type: str,
    *,
    description: str | None = None,
    created_by: int | None = None,
    idempotency_key: str | None = None,
):
    return await db_client.apply_ledger_entry(
        organization_id=organization_id,
        amount_cents=amount_cents,
        type=type,
        description=description,
        created_by=created_by,
        idempotency_key=idempotency_key,
    )


async def debit_for_run(
    *,
    organization_id: int,
    workflow_run_id: int,
    duration_seconds: float,
    price_per_minute_cents: int,
):
    cost = _cost_cents(duration_seconds, price_per_minute_cents)
    return await db_client.apply_ledger_entry(
        organization_id=organization_id,
        amount_cents=-cost,
        type="debit",
        workflow_run_id=workflow_run_id,
        description=(
            f"call {workflow_run_id}: {duration_seconds}s @ "
            f"{price_per_minute_cents}c/min"
        ),
        idempotency_key=f"debit:{workflow_run_id}",
    )
