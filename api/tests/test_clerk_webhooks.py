import json
import time
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

SECRET = "whsec_" + ("a" * 32)


def _signed_headers(body: str) -> dict:
    from svix.webhooks import Webhook

    msg_id = "msg_test_1"
    now = datetime.now(timezone.utc)
    signature = Webhook(SECRET).sign(msg_id, now, body)
    return {
        "svix-id": msg_id,
        "svix-timestamp": str(int(now.timestamp())),
        "svix-signature": signature,
        "content-type": "application/json",
    }


@pytest.fixture
async def async_client(db_session):
    """Unauthenticated HTTP client for webhook routes (no user auth required)."""
    from api.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def saas_webhook_env(monkeypatch):
    monkeypatch.setattr("api.routes.clerk_webhooks.CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr("api.routes.clerk_webhooks.IS_SAAS_MODE", True)


@pytest.mark.asyncio
async def test_bad_signature_rejected(async_client):
    body = json.dumps({"type": "user.updated", "data": {}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk",
        content=body,
        headers={
            "svix-id": "msg_x",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,invalid",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_user_updated_syncs_email(async_client, db_session):
    user, _ = await db_session.get_or_create_user_by_provider_id("user_wh_1")
    body = json.dumps(
        {
            "type": "user.updated",
            "data": {
                "id": "user_wh_1",
                "primary_email_address_id": "em_1",
                "email_addresses": [{"id": "em_1", "email_address": "new@example.com"}],
            },
        }
    )
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204
    refreshed = await db_session.get_user_by_id(user.id)
    assert refreshed.email == "new@example.com"


@pytest.mark.asyncio
async def test_user_deleted_archives_api_keys(async_client, db_session):
    # Provision user + org + one API key, then delete via webhook.
    user, _ = await db_session.get_or_create_user_by_provider_id("user_wh_2")
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_user_wh_2", user_id=user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    await db_session.create_api_key(
        organization_id=org.id, name="k", created_by=user.id
    )

    body = json.dumps({"type": "user.deleted", "data": {"id": "user_wh_2"}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204
    keys = await db_session.get_api_keys_by_organization(org.id, include_archived=True)
    assert all(not k.is_active for k in keys)


@pytest.mark.asyncio
async def test_user_deleted_does_not_archive_shared_selected_org_keys(
    async_client, db_session
):
    """user.deleted must target the deleted user's own provisioned org
    (provider_id f"org_{clerk_user_id}"), never `selected_organization_id` —
    which may point at a shared org whose keys belong to other members."""
    user, _ = await db_session.get_or_create_user_by_provider_id("user_wh_3")
    own_org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_user_wh_3", user_id=user.id
    )
    shared_org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_shared_team", user_id=user.id
    )
    # The deleted user's *selected* org is the shared one, not their own.
    await db_session.update_user_selected_organization(user.id, shared_org.id)
    await db_session.create_api_key(
        organization_id=own_org.id, name="own", created_by=user.id
    )
    await db_session.create_api_key(
        organization_id=shared_org.id, name="shared", created_by=user.id
    )

    body = json.dumps({"type": "user.deleted", "data": {"id": "user_wh_3"}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204

    own_keys = await db_session.get_api_keys_by_organization(
        own_org.id, include_archived=True
    )
    shared_keys = await db_session.get_api_keys_by_organization(
        shared_org.id, include_archived=True
    )
    assert all(not k.is_active for k in own_keys)
    assert all(k.is_active for k in shared_keys)


@pytest.mark.asyncio
async def test_user_updated_skips_sync_when_primary_email_missing(
    async_client, db_session
):
    """No primary_email_address_id means we can't tell which address is
    authoritative — skip the sync rather than picking an arbitrary one."""
    user, _ = await db_session.get_or_create_user_by_provider_id("user_wh_4")
    await db_session.update_user_email(user.id, "original@example.com")

    body = json.dumps(
        {
            "type": "user.updated",
            "data": {
                "id": "user_wh_4",
                "primary_email_address_id": None,
                "email_addresses": [
                    {"id": "em_a", "email_address": "a@example.com"},
                    {"id": "em_b", "email_address": "b@example.com"},
                ],
            },
        }
    )
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204
    refreshed = await db_session.get_user_by_id(user.id)
    assert refreshed.email == "original@example.com"


@pytest.mark.asyncio
async def test_unknown_event_is_accepted(async_client):
    body = json.dumps({"type": "session.created", "data": {"id": "sess_1"}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_route_hidden_outside_saas(async_client, monkeypatch):
    monkeypatch.setattr("api.routes.clerk_webhooks.IS_SAAS_MODE", False)
    body = json.dumps({"type": "user.updated", "data": {}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 404
