"""Credit balance read — available whenever the local ledger is on,
independent of BILLING_PAYMENTS_ENABLED (which gates purchase routes only).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.constants import BILLING_ENGINE
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing import billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


class BalanceResponse(BaseModel):
    balance_cents: int
    minutes_equivalent: float


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
