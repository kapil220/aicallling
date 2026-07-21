"""Clerk -> app sync webhooks (spec §2). Svix-signed."""

from fastapi import APIRouter, HTTPException, Request, Response
from loguru import logger
from svix.webhooks import Webhook, WebhookVerificationError

from api.constants import CLERK_WEBHOOK_SECRET, IS_SAAS_MODE
from api.db import db_client

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/clerk", status_code=204)
async def clerk_webhook(request: Request) -> Response:
    if not IS_SAAS_MODE:
        raise HTTPException(status_code=404)

    payload = await request.body()
    try:
        event = Webhook(CLERK_WEBHOOK_SECRET).verify(payload, dict(request.headers))
    except (WebhookVerificationError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = event.get("type")
    data = event.get("data", {})
    provider_id = data.get("id")

    if event_type == "user.updated" and provider_id:
        emails = data.get("email_addresses") or []
        primary_id = data.get("primary_email_address_id")
        email = next(
            (
                e.get("email_address")
                for e in emails
                if e.get("id") == primary_id or primary_id is None
            ),
            None,
        )
        user = await db_client.get_user_by_provider_id(provider_id)
        if user and email and user.email != email:
            await db_client.update_user_email(user.id, email)
            logger.info("Clerk webhook: synced email for user {}", user.id)

    elif event_type == "user.deleted" and provider_id:
        user = await db_client.get_user_by_provider_id(provider_id)
        if user and user.selected_organization_id:
            keys = await db_client.get_api_keys_by_organization(
                user.selected_organization_id
            )
            for key in keys:
                await db_client.archive_api_key(key.id)
            logger.info(
                "Clerk webhook: user {} deleted; archived {} API keys",
                user.id,
                len(keys),
            )
        # Data retention beyond key revocation is a phase-2 policy decision
        # recorded in the spec (§2); Clerk-side deletion already blocks login.

    return Response(status_code=204)
