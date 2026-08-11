"""Platform number routes — service mocked; guard tests hit real db rows."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from api.app import app
from api.constants import DATABASE_URL
from api.db import db_client
from api.db.models import OrganizationModel
from api.services.auth.depends import get_user, get_user_with_selected_organization
from api.services.telephony.platform_numbers import PlatformNumberError


@pytest_asyncio.fixture(scope="module")
async def real_db(setup_test_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    orig_engine, orig_maker = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, maker
    org_ids: list[int] = []

    async def make_org():
        async with maker() as s:
            org = OrganizationModel(provider_id=f"org_pnr_{uuid.uuid4().hex[:10]}")
            s.add(org)
            await s.commit()
            await s.refresh(org)
            org_ids.append(org.id)
            return org

    yield make_org
    async with maker() as s:
        await s.execute(
            delete(OrganizationModel).where(OrganizationModel.id.in_(org_ids))
        )
        await s.commit()
    db_client.engine, db_client.async_session = orig_engine, orig_maker
    await engine.dispose()


@pytest_asyncio.fixture
async def org(real_db):
    return await real_db()


@pytest.fixture
def client(org):
    fake_user = type(
        "U", (), {"id": 1, "selected_organization_id": org.id, "is_superuser": False}
    )()
    app.dependency_overrides[get_user_with_selected_organization] = lambda: fake_user
    app.dependency_overrides[get_user] = lambda: fake_user
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    app.dependency_overrides.pop(get_user_with_selected_organization, None)
    app.dependency_overrides.pop(get_user, None)


@pytest.mark.asyncio
async def test_pricing_endpoint(client):
    async with client as c:
        resp = await c.get("/api/v1/platform-numbers/pricing")
    assert resp.status_code == 200
    body = resp.json()
    countries = {p["country"] for p in body["countries"]}
    assert {"US", "CA", "IN"} <= countries
    assert body["carrier_cents_per_minute"] == 50


@pytest.mark.asyncio
async def test_search_delegates_to_service(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.search",
        AsyncMock(
            return_value=[
                {
                    "phone_number": "+14155551234",
                    "friendly_name": "",
                    "locality": "SF",
                    "region": "CA",
                }
            ]
        ),
    ):
        async with client as c:
            resp = await c.get(
                "/api/v1/platform-numbers/search",
                params={"country": "US", "area_code": "415"},
            )
    assert resp.status_code == 200
    assert resp.json()[0]["phone_number"] == "+14155551234"
    assert resp.json()[0]["rental_cents"] == 1000


@pytest.mark.asyncio
async def test_search_service_error_maps_to_http(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.search",
        AsyncMock(side_effect=PlatformNumberError(400, "country_not_supported")),
    ):
        async with client as c:
            resp = await c.get(
                "/api/v1/platform-numbers/search", params={"country": "DE"}
            )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "country_not_supported"


@pytest.mark.asyncio
async def test_purchase_and_response_shape(client):
    fake_row = type(
        "Row",
        (),
        {
            "id": 7,
            "address": "+14155551234",
            "country_code": "US",
            "rental_cents": 1000,
            "next_renewal_at": datetime.now(timezone.utc),
            "rental_status": "active",
            "inbound_workflow_id": None,
            "is_active": True,
        },
    )()
    with patch(
        "api.routes.platform_numbers.platform_numbers.purchase",
        AsyncMock(return_value=fake_row),
    ) as purchase:
        async with client as c:
            resp = await c.post(
                "/api/v1/platform-numbers/purchase",
                json={"phone_number": "+14155551234", "country": "US"},
            )
    assert resp.status_code == 200
    assert resp.json()["address"] == "+14155551234"
    assert resp.json()["rental_minutes_equivalent"] == 10.0
    assert purchase.call_args.kwargs["phone_number"] == "+14155551234"


@pytest.mark.asyncio
async def test_release_route(client):
    with patch(
        "api.routes.platform_numbers.platform_numbers.release", AsyncMock()
    ) as release:
        async with client as c:
            resp = await c.post("/api/v1/platform-numbers/9/release")
    assert resp.status_code == 200
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_platform_config_blocked_from_generic_delete(client, org):
    config = await db_client.create_platform_telephony_configuration(
        org.id, {"account_sid": "AC_g", "auth_token": "t", "from_numbers": []}
    )
    async with client as c:
        resp = await c.delete(f"/api/v1/organizations/telephony-configs/{config.id}")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "platform_managed"
