"""Org member management: roster, invite, role change, remove.

All mutating routes resolve the target org from the caller's session
(`get_user_with_selected_organization` -> `require_org_role`), never from a
client-supplied org id, and reject acting on a non-member with 404 rather than 403
to avoid leaking cross-org membership existence.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import db_client
from api.db.org_membership_client import LastAdminError
from api.enums import Role
from api.services.auth.depends import require_org_role

router = APIRouter(prefix="/organization/members", tags=["organization-members"])


class MemberResponse(BaseModel):
    user_id: int
    email: str | None
    role: str
    created_at: str


class InviteMemberRequest(BaseModel):
    email: str
    role: str = "member"


class ChangeRoleRequest(BaseModel):
    role: str


def _member_response(m: dict) -> MemberResponse:
    return MemberResponse(
        user_id=m["user_id"],
        email=m["email"],
        role=m["role"],
        created_at=m["created_at"].isoformat(),
    )


@router.get("", response_model=list[MemberResponse])
async def list_members(dep=Depends(require_org_role(Role.MEMBER))):
    user, _role = dep
    members = await db_client.list_org_members(user.selected_organization_id)
    return [_member_response(m) for m in members]


@router.post("/invite", response_model=MemberResponse)
async def invite_member(
    body: InviteMemberRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    target = await db_client.get_user_by_email(body.email)
    if target is None:
        # v1: invite targets an existing local account. Pending-invite creation for
        # brand-new emails (local mode) and Stack Auth's add-user API (stack mode)
        # are tracked as a follow-up — see the Phase 4 plan Self-Review.
        raise HTTPException(status_code=404, detail="user_not_found")

    org_id = user.selected_organization_id
    # Re-invites intentionally change an existing member's role.
    await db_client.add_user_to_organization(
        target.id, org_id, role=body.role, overwrite_role=True
    )

    members = await db_client.list_org_members(org_id)
    row = next(m for m in members if m["user_id"] == target.id)
    return _member_response(row)


@router.patch("/{user_id}", response_model=MemberResponse)
async def change_member_role(
    user_id: int,
    body: ChangeRoleRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    org_id = user.selected_organization_id
    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.set_member_role(org_id, user_id, body.role)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    members = await db_client.list_org_members(org_id)
    row = next(m for m in members if m["user_id"] == user_id)
    return _member_response(row)


@router.delete("/{user_id}")
async def remove_member(
    user_id: int,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    org_id = user.selected_organization_id
    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.remove_member(org_id, user_id)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    return {"status": "removed", "user_id": user_id}
