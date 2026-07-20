import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel

from api.constants import AUTH_PROVIDER
from api.db import db_client
from api.db.models import UserModel
from api.db.org_membership_client import LastAdminError
from api.services.auth.depends import get_superuser
from api.services.auth.stack_auth import stackauth
from api.services.billing import billing_service
from api.utils.auth import create_jwt_token

router = APIRouter(prefix="/superuser", tags=["superuser"])


class ImpersonateRequest(BaseModel):
    """Request payload for superadmin impersonation.

    Either ``provider_user_id`` **or** ``user_id`` must be supplied. If both are
    provided, ``provider_user_id`` takes precedence.
    """

    provider_user_id: str | None = None
    user_id: int | None = None


class ImpersonateResponse(BaseModel):
    refresh_token: str
    access_token: str


class SuperuserWorkflowRunResponse(BaseModel):
    id: int
    name: str
    workflow_id: int
    workflow_name: Optional[str]
    user_id: Optional[int]
    organization_id: Optional[int]
    organization_name: Optional[str]
    mode: str
    is_completed: bool
    recording_url: Optional[str]
    transcript_url: Optional[str]
    usage_info: Optional[dict]
    cost_info: Optional[dict]
    initial_context: Optional[dict]
    gathered_context: Optional[dict]
    created_at: datetime


class SuperuserWorkflowRunsListResponse(BaseModel):
    workflow_runs: List[SuperuserWorkflowRunResponse]
    total_count: int
    page: int
    limit: int
    total_pages: int


@router.post("/impersonate")
async def impersonate(
    request: ImpersonateRequest, user: UserModel = Depends(get_superuser)
) -> ImpersonateResponse:
    """Impersonate a user as a super-admin.

    Stack mode: delegates to Stack Auth's impersonation API (provider_user_id).
    Local mode: mints a short-lived JWT scoped to the target user directly, since
    there is no external auth provider to delegate to.
    """
    if AUTH_PROVIDER == "local":
        if request.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'user_id' is required for local-mode impersonation.",
            )
        target = await db_client.get_user_by_id(request.user_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {request.user_id} not found.",
            )
        token = create_jwt_token(
            target.id, target.email, extra_claims={"impersonated_by": str(user.id)}
        )
        logger.info(
            "Local-mode impersonation issued: superuser={} target_user={} target_org={}",
            user.id,
            target.id,
            getattr(target, "selected_organization_id", None),
        )
        return ImpersonateResponse(refresh_token=token, access_token=token)

    # ------------------------------------------------------------------
    # Stack mode (unchanged behavior): resolve provider_user_id, delegate to Stack
    # ------------------------------------------------------------------
    provider_user_id: str | None = request.provider_user_id
    if provider_user_id is None:
        if request.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'provider_user_id' or 'user_id' must be provided.",
            )

        db_user = await db_client.get_user_by_id(request.user_id)
        if db_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {request.user_id} not found.",
            )

        provider_user_id = db_user.provider_id

    session = await stackauth.impersonate(provider_user_id)
    logger.info(
        "Stack-mode impersonation issued: superuser={} target_provider_id={}",
        user.id,
        provider_user_id,
    )
    return ImpersonateResponse(
        refresh_token=session["refresh_token"],
        access_token=session["access_token"],
    )


@router.get("/workflow-runs")
async def get_workflow_runs(
    page: int = Query(1, ge=1, description="Page number (starts from 1)"),
    limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
    filters: Optional[str] = Query(None, description="JSON-encoded filter criteria"),
    sort_by: Optional[str] = Query(
        None, description="Field to sort by (e.g., 'duration', 'created_at')"
    ),
    sort_order: Optional[str] = Query(
        "desc", description="Sort order ('asc' or 'desc')"
    ),
    user: UserModel = Depends(get_superuser),
) -> SuperuserWorkflowRunsListResponse:
    """
    Get paginated list of all workflow runs with organization information.
    Requires superuser privileges.

    Filters should be provided as a JSON-encoded array of filter criteria.
    Example: [{"field": "id", "type": "number", "value": {"value": 680}}]
    """
    offset = (page - 1) * limit

    # Parse filters if provided
    filter_criteria = None
    if filters:
        try:
            filter_criteria = json.loads(filters)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid filter format")

    # Validate sort_order
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    workflow_runs, total_count = await db_client.get_workflow_runs_for_superadmin(
        limit=limit,
        offset=offset,
        filters=filter_criteria,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = (total_count + limit - 1) // limit  # Ceiling division

    return SuperuserWorkflowRunsListResponse(
        workflow_runs=[SuperuserWorkflowRunResponse(**run) for run in workflow_runs],
        total_count=total_count,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


# ---------------------------------------------------------------------------
# Org backoffice (Phase 4): list/detail with Phase 1 credit balances, role override
# ---------------------------------------------------------------------------


class OrgSummaryResponse(BaseModel):
    id: int
    provider_id: str
    credit_balance_cents: int
    member_count: int
    admin_count: int


class OrgSummaryListResponse(BaseModel):
    organizations: List[OrgSummaryResponse]
    total_count: int


class OrgMemberDetail(BaseModel):
    user_id: int
    email: Optional[str]
    role: str
    created_at: str


class OrgDetailResponse(BaseModel):
    id: int
    provider_id: str
    credit_balance_cents: int
    members: List[OrgMemberDetail]


class RoleOverrideRequest(BaseModel):
    role: str


@router.get("/orgs", response_model=OrgSummaryListResponse)
async def list_orgs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: UserModel = Depends(get_superuser),
):
    offset = (page - 1) * limit
    orgs, total = await db_client.list_organizations(limit=limit, offset=offset)
    summaries = []
    for org in orgs:
        members = await db_client.list_org_members(org.id)
        summaries.append(
            OrgSummaryResponse(
                id=org.id,
                provider_id=org.provider_id,
                credit_balance_cents=await billing_service.get_balance_cents(org.id),
                member_count=len(members),
                admin_count=sum(1 for m in members if m["role"] == "admin"),
            )
        )
    return OrgSummaryListResponse(organizations=summaries, total_count=total)


@router.get("/orgs/{org_id}", response_model=OrgDetailResponse)
async def get_org_detail(org_id: int, user: UserModel = Depends(get_superuser)):
    org = await db_client.get_organization_by_id(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization_not_found")

    members = await db_client.list_org_members(org_id)
    return OrgDetailResponse(
        id=org.id,
        provider_id=org.provider_id,
        credit_balance_cents=await billing_service.get_balance_cents(org_id),
        members=[
            OrgMemberDetail(
                user_id=m["user_id"],
                email=m["email"],
                role=m["role"],
                created_at=m["created_at"].isoformat(),
            )
            for m in members
        ],
    )


@router.post("/orgs/{org_id}/members/{user_id}/role")
async def override_member_role(
    org_id: int,
    user_id: int,
    body: RoleOverrideRequest,
    user: UserModel = Depends(get_superuser),
):
    """Platform-level role override — the sole legitimate cross-org role write.
    Repairs a zero-admin org (promote) or corrects a mis-set role (demote, still
    subject to the last-admin guard so it can't itself create a zero-admin org)."""
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.set_member_role(org_id, user_id, body.role)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    return {"status": "updated", "org_id": org_id, "user_id": user_id, "role": body.role}
