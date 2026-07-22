"""Platform-provisioned number routes (saas phase 3, spec §8).

Thin handlers over services/telephony/platform_numbers; all org-scoped via
the caller's session. Numbers here are platform-managed: the generic
telephony-config CRUD refuses to touch them (403 platform_managed).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.constants import (
    PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE,
    PLATFORM_NUMBER_COUNTRIES,
)
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.telephony import platform_numbers
from api.services.telephony.platform_numbers import (
    PlatformNumberError,
    rental_cents_for_country,
)

router = APIRouter(prefix="/platform-numbers", tags=["platform-numbers"])


class CountryPricing(BaseModel):
    country: str
    rental_minutes: float
    rental_cents: int


class PricingResponse(BaseModel):
    countries: list[CountryPricing]
    carrier_cents_per_minute: int


class AvailableNumberResponse(BaseModel):
    phone_number: str
    friendly_name: str | None
    locality: str | None
    region: str | None
    rental_cents: int


class PurchaseRequest(BaseModel):
    phone_number: str
    country: str
    inbound_workflow_id: int | None = None


class InboundWorkflowRequest(BaseModel):
    inbound_workflow_id: int | None = None


class PlatformNumberResponse(BaseModel):
    id: int
    address: str
    country_code: str | None
    rental_cents: int | None
    rental_minutes_equivalent: float | None
    next_renewal_at: str | None
    rental_status: str | None
    inbound_workflow_id: int | None
    is_active: bool


def _response(row) -> PlatformNumberResponse:
    return PlatformNumberResponse(
        id=row.id,
        address=row.address,
        country_code=row.country_code,
        rental_cents=row.rental_cents,
        rental_minutes_equivalent=(
            row.rental_cents / 100 if row.rental_cents is not None else None
        ),
        next_renewal_at=(
            row.next_renewal_at.isoformat() if row.next_renewal_at else None
        ),
        rental_status=row.rental_status,
        inbound_workflow_id=row.inbound_workflow_id,
        is_active=row.is_active,
    )


def _http(e: PlatformNumberError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.detail)


@router.get("/pricing", response_model=PricingResponse)
async def get_pricing(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    return PricingResponse(
        countries=[
            CountryPricing(
                country=c,
                rental_minutes=rental_cents_for_country(c) / 100,
                rental_cents=rental_cents_for_country(c),
            )
            for c in sorted(PLATFORM_NUMBER_COUNTRIES)
        ],
        carrier_cents_per_minute=PLATFORM_NUMBER_CARRIER_CENTS_PER_MINUTE,
    )


@router.get("/search", response_model=list[AvailableNumberResponse])
async def search_numbers(
    country: str = Query(...),
    area_code: str | None = Query(None),
    contains: str | None = Query(None),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    try:
        results = await platform_numbers.search(country, area_code, contains)
    except PlatformNumberError as e:
        raise _http(e)
    rental = rental_cents_for_country(country)
    return [
        AvailableNumberResponse(**result, rental_cents=rental) for result in results
    ]


@router.post("/purchase", response_model=PlatformNumberResponse)
async def purchase_number(
    body: PurchaseRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    if body.inbound_workflow_id is not None:
        workflow = await db_client.get_workflow(
            body.inbound_workflow_id,
            organization_id=user.selected_organization_id,
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow_not_found")
    try:
        row = await platform_numbers.purchase(
            user.selected_organization_id,
            phone_number=body.phone_number,
            country=body.country,
            inbound_workflow_id=body.inbound_workflow_id,
        )
    except PlatformNumberError as e:
        raise _http(e)
    return _response(row)


@router.get("", response_model=list[PlatformNumberResponse])
async def list_numbers(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_platform_numbers_for_org(user.selected_organization_id)
    return [_response(r) for r in rows]


@router.post("/{phone_number_id}/release")
async def release_number(
    phone_number_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    try:
        await platform_numbers.release(user.selected_organization_id, phone_number_id)
    except PlatformNumberError as e:
        raise _http(e)
    return {"status": "released"}


@router.put(
    "/{phone_number_id}/inbound-workflow", response_model=PlatformNumberResponse
)
async def set_inbound_workflow(
    phone_number_id: int,
    body: InboundWorkflowRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_platform_numbers_for_org(user.selected_organization_id)
    row = next((r for r in rows if r.id == phone_number_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="number_not_found")
    if body.inbound_workflow_id is not None:
        workflow = await db_client.get_workflow(
            body.inbound_workflow_id,
            organization_id=user.selected_organization_id,
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="workflow_not_found")
    updated = await db_client.update_phone_number(
        phone_number_id,
        row.telephony_configuration_id,
        inbound_workflow_id=body.inbound_workflow_id,
        clear_inbound_workflow=body.inbound_workflow_id is None,
    )
    return _response(updated)
