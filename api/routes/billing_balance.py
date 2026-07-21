"""Credit balance read — available whenever the local ledger is on,
independent of BILLING_PAYMENTS_ENABLED (which gates purchase routes only).
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.constants import BILLING_ENGINE
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing import billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


class BalanceResponse(BaseModel):
    balance_cents: int
    minutes_equivalent: float


class LedgerEntryResponse(BaseModel):
    id: int
    created_at: Optional[str] = None
    amount_cents: int
    type: str
    description: Optional[str] = None
    workflow_run_id: Optional[int] = None


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    user: Annotated[UserModel, Depends(get_user_with_selected_organization)],
) -> BalanceResponse:
    if BILLING_ENGINE != "local":
        raise HTTPException(status_code=404)
    balance = await billing_service.get_balance_cents(user.selected_organization_id)
    return BalanceResponse(
        balance_cents=balance, minutes_equivalent=round(balance / 100, 1)
    )


@router.get("/ledger", response_model=list[LedgerEntryResponse])
async def get_ledger(
    user: Annotated[UserModel, Depends(get_user_with_selected_organization)],
    limit: int = Query(50, ge=1, le=200),
) -> list[LedgerEntryResponse]:
    """Recent credit-ledger entries for the caller's org (local billing engine only)."""
    if BILLING_ENGINE != "local":
        raise HTTPException(status_code=404)
    entries = await db_client.list_ledger_entries(
        user.selected_organization_id, limit=limit
    )
    return [
        LedgerEntryResponse(
            id=e.id,
            created_at=e.created_at.isoformat() if e.created_at else None,
            amount_cents=e.amount_cents,
            type=e.type,
            description=e.description,
            workflow_run_id=e.workflow_run_id,
        )
        for e in entries
    ]
