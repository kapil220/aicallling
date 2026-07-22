# SaaS Deployment Setup Guide

This runbook covers the complete setup for deploying Dograh in SaaS mode (`DEPLOYMENT_MODE=saas`), including Clerk authentication, environment configuration, and billing initialization.

## Overview

SaaS mode enables:
- Multi-tenant architecture with organization isolation
- Clerk-based email/OAuth authentication
- Local billing engine with credit-based call authorization
- Platform-managed AI provider credentials
- Free trial minutes per new tenant

## Step 1: Clerk Dashboard Setup

### Create a Clerk Application

1. Go to [Clerk Dashboard](https://dashboard.clerk.com)
2. Create a new application (or use an existing one)
3. Note the **Application ID** for later reference

### Configure Sign-Up Settings

1. In the Clerk dashboard, navigate to **User & Authentication** > **Email, Phone, Username**
2. Under **Email address**, enable **Email address** and toggle **Require for sign up** ✓
3. This ensures all new users provide a verified email address

### Enable OAuth (Optional but Recommended)

1. Navigate to **Social Connections**
2. Enable **Google** (or other desired providers)
3. Configure OAuth credentials as needed

### Customize the Session Token (Required)

The backend resolves each request's user from the Clerk session token's claims
(`api/services/auth/depends.py::_handle_clerk_auth` reads `claims["email"]`),
but Clerk's default session token does **not** include the user's email
address. Without this step, every request authenticates the user by their
Clerk `sub` (id) but `claims.get("email")` is always `None`, so the local
user's email is never synced.

1. In the Clerk dashboard, navigate to **Sessions** > **Customize session token**
2. Add the following claim:
   ```json
   { "email": "{{user.primary_email_address}}" }
   ```
3. Save. New session tokens issued after this change will include the `email`
   claim; existing sessions pick it up on their next refresh.

### Create a Webhook Endpoint

1. Navigate to **Webhooks**
2. Click **Add endpoint**
3. Set **Endpoint URL** to: `https://<api-domain>/api/v1/webhooks/clerk`
   - For local development: `http://localhost:8000/api/v1/webhooks/clerk`
4. Under **Subscribe to events**, select:
   - ✓ `user.updated`
   - ✓ `user.deleted`
5. Copy the **Signing Secret** (starts with `whsec_`) — you'll need this as `CLERK_WEBHOOK_SECRET`

### Copy Credentials

Collect the following from your Clerk dashboard:

| Credential | Where to find | Environment variable |
|-----------|---|---|
| **Frontend API URL** | Settings > API Keys | `CLERK_ISSUER` |
| **Publishable Key** | API Keys > Frontend API | `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (frontend only) |
| **Secret Key** | API Keys > Backend API | `CLERK_SECRET_KEY` (frontend only) |
| **Webhook Signing Secret** | Webhooks > [your endpoint] | `CLERK_WEBHOOK_SECRET` |

**CLERK_ISSUER example**: `https://your-app.clerk.accounts.dev`

## Step 2: Environment Configuration

### Backend Environment Variables (`api/.env`)

Copy `api/.env.example` and set the following for SaaS mode:

| Variable | Required | Description | Example |
|----------|----------|---|---|
| `DEPLOYMENT_MODE` | Yes | Set to `saas` to enable SaaS features | `saas` |
| `AUTH_PROVIDER` | Yes | Must be `clerk` in SaaS mode | `clerk` |
| `BILLING_ENGINE` | Yes | Must be `local` in SaaS mode (self-owned ledger) | `local` |
| `CLERK_ISSUER` | Yes | Clerk Frontend API URL from dashboard | `https://your-app.clerk.accounts.dev` |
| `CLERK_WEBHOOK_SECRET` | Yes | Webhook signing secret (starts with `whsec_`) | `whsec_...` |
| `OSS_JWT_SECRET` | Yes | Secret for internal JWT signing (must differ from default) | Output of `openssl rand -hex 32` |
| `CORS_ALLOWED_ORIGINS` | Yes | Comma-separated allowlist of frontend URLs | `https://app.yourdomain.com` |
| `TRIAL_MINUTES` | No | Free trial minutes per new organization (default: 15) | `15` |
| `PLATFORM_OPENAI_API_KEY` | No | OpenAI API key (platform-managed for tenants) | `sk-...` |
| `PLATFORM_DEEPGRAM_API_KEY` | No | Deepgram API key (platform-managed for tenants) | `...` |
| `PLATFORM_ELEVENLABS_API_KEY` | No | ElevenLabs API key (platform-managed for tenants) | `...` |

### Frontend Environment Variables (`ui/.env`)

Copy `ui/.env.example` and set:

| Variable | Description | Example |
|----------|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Frontend-safe Clerk publishable key | `pk_live_...` or `pk_test_...` |
| `CLERK_SECRET_KEY` | Backend secret for Clerk SDK (server-side only) | `sk_live_...` or `sk_test_...` |
| `NEXT_PUBLIC_BACKEND_URL` | Backend API endpoint (visible to browser) | `https://api.yourdomain.com` or `http://localhost:8000` |

### Validation at Boot

The backend validates SaaS configuration on startup (see `api/services/saas_config.py`). The following are enforced:

- `AUTH_PROVIDER` must be `"clerk"`
- `BILLING_ENGINE` must be `"local"`
- `CLERK_ISSUER` must be set (non-empty string)
- `CLERK_WEBHOOK_SECRET` must be set (non-empty string)
- `OSS_JWT_SECRET` must be set and must **not** be the default value `"change-me-in-production"`
- `CORS_ALLOWED_ORIGINS` must be an explicit, non-empty allowlist (no wildcard)

If any check fails, the backend raises a `RuntimeError` listing all problems and exits immediately.

## Step 3: Pricing Rule Initialization

### Why Pricing Rules Matter

Calls are authorized based on available credits. If a call's model configuration has no matching pricing rule, the call fails closed (denied). You must seed at least one global pricing rule before users can make calls.

### Create a Global Default Pricing Rule

Use the superadmin endpoint to seed a default pricing rule. First, you need a superuser account.

#### Option A: Make an Existing User Superuser (Quick Setup)

1. Connect to your PostgreSQL database:
   ```bash
   psql postgresql://postgres:postgres@localhost:5432/postgres
   ```

2. Update a user's superuser status:
   ```sql
   UPDATE users SET is_superuser = TRUE WHERE id = <user_id>;
   ```

3. Find the user ID by querying for an email or provider_id:
   ```sql
   SELECT id, email, provider_id FROM users WHERE email = 'your-email@example.com';
   ```

#### Option B: Create a Superuser via API (After Existing Superuser)

Contact your SaaS operator or use an existing superuser to create additional superusers (not covered in this doc).

### Seed the Default 10¢/min Pricing Rule

Once you have a superuser, create a default pricing rule for all organizations.

#### Using curl (Linux/macOS):

```bash
# Set your superuser's auth token (via Clerk sign-in + inspect network tab, or use X-API-Key if using API keys)
BEARER_TOKEN="eyJhbGc..."  # Your Clerk JWT from browser auth

curl -X POST http://localhost:8000/api/v1/superuser/pricing-rules \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "organization_id": null,
    "mode": null,
    "llm_provider": null,
    "stt_provider": null,
    "tts_provider": null,
    "realtime_provider": null,
    "price_per_minute_cents": 10,
    "priority": 0
  }'
```

**What this does:**
- `organization_id: null` → applies to all organizations (global rule)
- All provider fields `null` → applies to all provider combinations
- `price_per_minute_cents: 10` → 10 cents per minute of call
- `priority: 0` → base priority (used for tie-breaking if multiple rules match)

**Success response:**
```json
{
  "id": 1
}
```

#### Using Python (for scripts):

```python
import httpx
import asyncio

async def seed_pricing_rule():
    async with httpx.AsyncClient() as client:
        # Ensure you're authenticated (Bearer token or X-API-Key header)
        headers = {
            "Authorization": "Bearer <your-superuser-token>",
        }
        response = await client.post(
            "http://localhost:8000/api/v1/superuser/pricing-rules",
            headers=headers,
            json={
                "organization_id": None,
                "mode": None,
                "llm_provider": None,
                "stt_provider": None,
                "tts_provider": None,
                "realtime_provider": None,
                "price_per_minute_cents": 10,
                "priority": 0,
            },
        )
        print(response.json())

asyncio.run(seed_pricing_rule())
```

### Verify the Pricing Rule

```bash
curl http://localhost:8000/api/v1/superuser/pricing-rules \
  -H "Authorization: Bearer $BEARER_TOKEN"
```

Response should include your newly created rule.

## Step 4: Running SaaS Mode Locally

### Start Infrastructure

Start the database, cache, and object storage services:

```bash
docker-compose -f docker-compose-local.yaml up -d
```

Wait for health checks to pass:
```bash
docker-compose -f docker-compose-local.yaml ps
```

### Run Database Migrations

```bash
source venv/bin/activate
./scripts/migrate.sh
```

### Start the Backend

In one terminal:

```bash
source venv/bin/activate
set -a && source api/.env && set +a
uvicorn api.app:app --reload --port 8000
```

### Start the Frontend

In another terminal:

```bash
cd ui
npm install
set -a && source .env && set +a
npm run dev
```

The frontend will start on `http://localhost:3000`.

### Test the Deployment

1. Open `http://localhost:3000` in your browser
2. Click **Sign Up**
3. Enter an email address (Clerk will send a verification link)
4. Verify your email and create an organization
5. Once inside, you should be able to create workflows and make calls (if you seeded a pricing rule)

### Webhook Testing (Local)

For local development, Clerk webhooks cannot reach `http://localhost:8000` from Clerk's servers. Options:

- **Use ngrok** to expose your local backend: `ngrok http 8000`, then update the Clerk webhook endpoint to `https://<your-ngrok-url>/api/v1/webhooks/clerk`
- **Skip webhook verification in development** by using a test signing secret (not recommended for production)
- **Use Clerk's webhook test feature** in the dashboard to manually trigger events

Once deployed to a real domain, webhooks will work automatically.

## Step 5: Razorpay Billing (Phase 2)

Subscription plans are sold through Razorpay Subscriptions. Payments stay off
until `BILLING_PAYMENTS_ENABLED=true`, so you can run the platform on trial
minutes alone and enable billing later.

### Environment Variables

Add to `api/.env`:

```bash
BILLING_PAYMENTS_ENABLED=true
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
```

When `BILLING_PAYMENTS_ENABLED=true`, saas boot validation refuses to start
unless all three Razorpay variables are set.

### Create Plans in Razorpay

The database ships with three seeded tiers (`starter`, `pro`, `scale` — see
`GET /api/v1/superuser/plans`). For each tier:

1. In the Razorpay dashboard (test mode first), go to **Subscriptions** >
   **Plans** > **Create Plan**: monthly billing, INR, amount matching the
   tier's `price_cents` (e.g. Starter ₹1,499).
2. Link the Razorpay plan id to the tier:
   ```bash
   curl -X PATCH http://localhost:8000/api/v1/superuser/plans/<plan_id> \
     -H "Authorization: Bearer <superuser_token>" \
     -H "Content-Type: application/json" \
     -d '{"razorpay_plan_id": "plan_XXXXXXXX"}'
   ```

A tier without a `razorpay_plan_id` returns `409 plan_not_purchasable` at
checkout, so nothing is sellable until you link it. Prices, included minutes,
and limits are tunable via the same superadmin endpoint.

### Configure the Webhook

In Razorpay dashboard > **Settings** > **Webhooks**, add
`https://<your-host>/api/v1/webhooks/razorpay` with the secret from
`RAZORPAY_WEBHOOK_SECRET` and these events:

- `subscription.activated`
- `subscription.charged`
- `subscription.halted`
- `subscription.cancelled`
- `payment.failed`

Every `subscription.charged` expires the previous period's remaining balance
and grants the new period's minutes (no rollover); replayed events are no-ops
via ledger idempotency keys.

### Trial Limits

Orgs without an active plan run under trial limits, tunable via env:
`TRIAL_MAX_AGENTS` (3), `TRIAL_MAX_CONCURRENT_CALLS` (2),
`TRIAL_DAILY_CALL_CAP` (20), `TRIAL_MAX_ACTIVE_CAMPAIGNS` (1). Plan tiers set
these per subscription; `halted`/`cancelled` subscriptions block new calls
while keeping agents editable.

## Troubleshooting

### Backend Won't Start: SaaS Config Validation Fails

Check `api/.env` for the listed missing/invalid variables:
- `CLERK_ISSUER` not set → add Clerk Frontend API URL
- `CLERK_WEBHOOK_SECRET` not set → add webhook signing secret from Clerk dashboard
- `OSS_JWT_SECRET` still set to default → run `openssl rand -hex 32` and use the output
- `CORS_ALLOWED_ORIGINS` is empty → set to at least one frontend URL (no wildcards)

### Calls Are Being Denied

Check that:
1. A pricing rule exists: `curl http://localhost:8000/api/v1/superuser/pricing-rules -H "Authorization: Bearer <token>"`
2. The organization has credits: `curl http://localhost:8000/api/v1/superuser/orgs/{org_id}/credits -H "Authorization: Bearer <token>"`
   - If balance is 0, use `POST /api/v1/superuser/orgs/{org_id}/credits` to grant credits

### Webhook Events Not Syncing

- Verify the endpoint is reachable from Clerk's servers (use ngrok locally)
- Check the webhook signing secret matches `CLERK_WEBHOOK_SECRET` exactly
- View webhook logs in Clerk dashboard under **Webhooks** > **Your Endpoint** > **Logs**

### Users Can't Sign Up

- Ensure email verification is enabled in Clerk dashboard (**User & Authentication** > **Email**)
- Check that the `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` is correctly set in `ui/.env`

## Summary

1. ✅ Set up Clerk application and webhook
2. ✅ Configure backend and frontend `.env` files
3. ✅ Start Docker Compose infrastructure
4. ✅ Run migrations
5. ✅ Make a user superuser and seed default pricing rule
6. ✅ Start backend and frontend
7. ✅ Test sign-up and call authorization

SaaS mode is now ready for testing or deployment.
