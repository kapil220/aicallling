"""Plan-tier limit resolution (saas phase 2).

Every enforcement site gates on enforcement_enabled() so OSS deployments
are untouched. A limit of None means unlimited.
"""

from dataclasses import dataclass

from api.constants import (
    IS_SAAS_MODE,
    TRIAL_DAILY_CALL_CAP,
    TRIAL_MAX_ACTIVE_CAMPAIGNS,
    TRIAL_MAX_AGENTS,
    TRIAL_MAX_CONCURRENT_CALLS,
)
from api.db import db_client

UPGRADE_PROMPT = "Upgrade your plan at /billing to raise this limit."


@dataclass(frozen=True)
class PlanLimits:
    max_agents: int | None
    max_concurrent_calls: int
    daily_call_cap: int | None
    max_active_campaigns: int | None


TRIAL_LIMITS = PlanLimits(
    max_agents=TRIAL_MAX_AGENTS,
    max_concurrent_calls=TRIAL_MAX_CONCURRENT_CALLS,
    daily_call_cap=TRIAL_DAILY_CALL_CAP,
    max_active_campaigns=TRIAL_MAX_ACTIVE_CAMPAIGNS,
)


def enforcement_enabled() -> bool:
    return IS_SAAS_MODE


async def check_can_create_agent(organization_id: int) -> str | None:
    """Returns an error message when the org is at its agent cap, else None."""
    if not enforcement_enabled():
        return None
    limits = await get_org_limits(organization_id)
    if limits.max_agents is None:
        return None
    counts = await db_client.get_workflow_counts(organization_id=organization_id)
    if counts.get("active", 0) >= limits.max_agents:
        return (
            f"agent_limit_reached: your plan allows {limits.max_agents} active "
            f"agents. {UPGRADE_PROMPT}"
        )
    return None


async def check_can_start_campaign(organization_id: int) -> str | None:
    """Returns an error message when the org is at its active-campaign cap."""
    if not enforcement_enabled():
        return None
    limits = await get_org_limits(organization_id)
    if limits.max_active_campaigns is None:
        return None
    active = await db_client.count_active_campaigns(organization_id)
    if active >= limits.max_active_campaigns:
        return (
            f"campaign_limit_reached: your plan allows {limits.max_active_campaigns} "
            f"active campaigns. {UPGRADE_PROMPT}"
        )
    return None


async def get_org_limits(organization_id: int) -> PlanLimits:
    org = await db_client.get_organization_by_id(organization_id)
    if org is None or org.plan_id is None:
        return TRIAL_LIMITS
    plan = await db_client.get_plan_by_id(org.plan_id)
    if plan is None:
        return TRIAL_LIMITS
    return PlanLimits(
        max_agents=plan.max_agents,
        max_concurrent_calls=plan.max_concurrent_calls,
        daily_call_cap=plan.daily_call_cap,
        max_active_campaigns=plan.max_active_campaigns,
    )
