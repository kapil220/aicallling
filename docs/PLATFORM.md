# Platform Documentation & Audit

Audit date: 2026-07-21. This document describes what the codebase contains today, how it fits together, and what works vs. what doesn't. It is the baseline for the SaaS transformation described in `docs/superpowers/specs/2026-07-21-saas-platform-design.md`.

## What this codebase is

A fork/import of **Dograh**, an open-source voice-AI platform (Vapi/Retell category): build conversational voice agents as flow graphs, run them over telephony or WebRTC, and manage batch calling campaigns.

```
├── api/          FastAPI backend (~568 py files, ~120 test files, 93 alembic migrations)
├── ui/           Next.js 15 / React 19 frontend (App Router, Tailwind v4, shadcn/ui)
├── pipecat/      Vendored fork of the pipecat voice-pipeline framework (not a real git submodule)
├── docs/         Mintlify docs
├── scripts/      Setup / run / deploy scripts (sh + ps1)
├── docker-compose.yaml        Full-stack deployment (api, ui, postgres, redis, minio, cloudflared, nginx, coturn)
└── docker-compose-local.yaml  Infra-only (postgres, redis, minio) for native dev
```

Routes are thin and mounted under `/api/v1`; domain logic lives in `api/services/`, DB access in `api/db/`, background jobs (ARQ/Redis) in `api/tasks/`.

## Deployment-mode switches (api/constants.py)

| Env var | Default | Meaning |
|---|---|---|
| `DEPLOYMENT_MODE` | `oss` | `oss` vs managed. Gates CORS policy, Sentry, MPS billing-account creation. |
| `AUTH_PROVIDER` | `local` | `local` (email/password JWT) vs `stack` (Stack Auth). |
| `BILLING_ENGINE` | `mps` | `mps` = delegate credits to Dograh's external cloud (services.dograh.com); `local` = self-contained credit ledger. |
| `BILLING_PAYMENTS_ENABLED` | `false` | Gates all Stripe routes (404 when off). |

The frontend adapts at runtime from `GET /api/v1/health` (`deployment_mode`, `auth_provider`).

## Subsystems

### Authentication (working)
- Three mechanisms resolved in order in `api/services/auth/depends.py::get_user`: API key (`X-API-Key`), local email/password, Stack Auth.
- Local auth: `api/routes/auth.py` (`/auth/signup`, `/auth/login`, `/auth/me`), bcrypt hashes, HS256 JWT (`OSS_JWT_SECRET`, 30-day expiry). Signup creates user + org + admin membership.
- Users: `UserModel` (unique `provider_id`, case-insensitive-unique email, `is_superuser`, `selected_organization_id`). Orgs: `OrganizationModel`. Membership: `organization_users` with role admin/member.
- API keys: hashed, prefix-displayed, soft-archive (`api/db/api_key_client.py`, `/user/api-keys`).
- Well tested. Gap: no email verification, no password reset, no OAuth on the local path.

### Roles / multi-tenancy (working)
- `require_org_role(min_role)` dependency; last-admin protection with row locks (`api/db/org_membership_client.py`); superuser flag bypass.
- All resources scoped by `selected_organization_id`; cross-org FK references re-validated. Enforced consistently and tested.
- Gap: member invites only work for already-registered emails (`api/routes/organization_members.py`, marked v1 TODO).

### Billing & credits (working, but off by default)
- **Local engine** (`BILLING_ENGINE=local`, fully implemented + tested): append-only `CreditLedgerModel` in cents with running balance and idempotency keys; cached `credit_balance_cents` under row lock; `apply_ledger_entry` is the single mutation path (`api/db/billing_client.py`, `api/services/billing/`).
- Per-minute pricing via `PricingRuleModel` (mode + llm/stt/tts/realtime provider tuple, wildcard, most-specific-wins; `api/services/billing/pricing.py`). Unpriced calls fail closed.
- Pre-call authorization + **mid-call affordability cap** (call ends when balance is spent) wired into `api/services/pipecat/run_pipeline.py`.
- Stripe: customer creation, Checkout Sessions, webhooks (credit on success, clawback on refund), `PaymentPackModel` catalog, `PaymentModel` audit trail (`api/services/billing/payment_service.py`, `api/routes/billing.py`, `api/routes/webhooks.py`).
- Superadmin backoffice: grant/adjust credits, ledger, pricing rules, packs, org list/detail, impersonation (`api/routes/billing_admin.py`, `api/routes/superuser.py`).
- **MPS engine** (default): quota + credits live in Dograh's external service — unusable for an independent deployment.

### Voice agents / workflow builder (working)
- Agents are ReactFlow-style graphs: node types `startCall`, `agentNode`, `endCall`, `globalNode` (+ trigger/webhook/qa) with prompts, extraction variables, tools, documents, MCP (`api/services/workflow/dto.py`).
- Self-describing node specs drive the UI forms (`api/services/workflow/node_specs/`, served by `/node-types`).
- Runtime engine: `api/services/workflow/pipecat_engine.py` — node transitions as LLM tools, variable extraction, context summarization, end-call handling.
- Versioning: draft/published/archived with version history, revert, publish (`api/db/workflow_client.py`).
- Frontend editor: `ui/src/app/workflow/[workflowId]/RenderWorkflow.tsx` — React Flow canvas, Zustand + zundo undo/redo, validation, version panel, embedded WebRTC voice tester and text-chat simulator.
- Known issue: acyclic-graph validation is commented out (`api/services/workflow/workflow_graph.py`), so cyclic workflows are accepted.

### Model / pipeline selection (working)
- Provider registry (`api/services/configuration/registry.py`): LLM/TTS/STT/realtime across ~25 providers (OpenAI, Deepgram, ElevenLabs, Cartesia, Google/Vertex, Azure, Groq, OpenRouter, Bedrock, AssemblyAI, Gladia, Speechmatics, Rime, MiniMax, Sarvam, Smallest, Camb, HuggingFace, Speaches; realtime: OpenAI Realtime, Gemini Live, Grok, Ultravox, Azure Realtime).
- Service instantiation in `api/services/pipecat/service_factory.py`; per-workflow overrides deep-merged onto org config (`api/services/configuration/resolve.py`); key masking on read.
- Frontend: `ModelConfigurationV2` editor, voice selector, per-workflow overrides.
- **Security gap: credentials stored as plaintext JSON** (`ExternalCredentialModel.credential_data`, `IntegrationModel.credentials`, org configuration keys). No encryption-at-rest layer exists.

### Telephony (working, bring-your-own only)
- Provider framework (`api/services/telephony/`): twilio, telnyx, plivo, vonage, cloudonix, vobiz, ari (Asterisk). Twilio most complete (signature verification, status callbacks, transfers, cost capture).
- Outbound `/telephony/initiate-call`; inbound provider detection + routing to workflows; WebSocket media streams; WebRTC (SmallWebRTC) for browser calls; TURN support.
- Numbers are registered manually against a config (`telephony_phone_numbers`); **no carrier search/purchase/provisioning flow exists**.

### Campaigns (working)
- State machine `created → syncing → running ⇄ paused → completed/failed` on `CampaignModel`.
- CSV contact source (only source wired; Google Sheets exists as write-back only). Redis pub/sub orchestrator (`api/services/campaign/campaign_orchestrator.py`): batching, retries (busy/no-answer/voicemail), schedule windows (timezone + weekly slots), stale detection, circuit breaker, per-second rate limiting + org concurrency slots, redial.
- ARQ workers: `sync_campaign_source`, `process_campaign_batch`. Note: ARQ `cron_jobs` is empty; periodic work runs in-process (matters for HA).
- Full CRUD + start/pause/resume/redial UI with advanced settings.

### Call runs, recordings, usage (working)
- `WorkflowRunModel` lifecycle with pinned workflow-definition snapshot; completion task uploads mixed/user/bot WAVs + transcript to MinIO/S3 (`api/tasks/workflow_completion.py`); signed URL downloads.
- `usage_info`/`cost_info` per run; usage dashboards, daily rollups, CSV export; billing debit post-call.

### Frontend route map (all working unless noted)
`/overview`, `/workflow` (+ create/editor/runs/settings), `/campaigns` (+ new/detail/edit), `/model-configurations`, `/telephony-configurations`, `/tools`, `/files`, `/recordings`, `/api-keys`, `/usage`, `/billing`, `/reports`, `/settings`, `/organization/members`, `/superadmin` (orgs/runs/impersonation), `/auth/login`, `/auth/signup`, Stack handler at `/handler/*`.
- `/automation` — "Coming soon" stub, not in nav.
- Billing "Buy credits" (`ui/src/lib/billing/topup.ts`) deliberately throws `Top-up not wired yet`.
- API layer: hey-api generated client (`ui/src/client/`), reverse proxy at `/api/v1/[...path]`, Bearer-token interceptor. Note: generated client does not throw on 4xx/5xx — callers must check `response.error`.

## What works vs. what doesn't — summary

**Works (tested):** local auth, orgs/roles, superadmin, credit ledger + pricing + mid-call cutoff, Stripe checkout/webhooks, workflow builder + versioning, 25-provider model selection, 7-provider BYO telephony, WebRTC test calls, campaigns end-to-end, recordings/transcripts, usage reporting, API keys, MCP server, 93-migration clean schema, CI.

**Doesn't work / missing for an independent SaaS:**
1. Default billing depends on Dograh's cloud (MPS) — must run `BILLING_ENGINE=local`.
2. Self-serve credit purchase UI is a stub.
3. No email verification / password reset / OAuth on local auth; no email service at all.
4. No trial-credit grant on signup.
5. Credentials/API keys unencrypted at rest.
6. No phone-number provisioning (BYO only).
7. Workflow cycle validation disabled.
8. Dograh branding + app.dograh.com upsells throughout the UI.
9. Invites limited to existing accounts.

**Infra/deployment footguns:**
- `OSS_JWT_SECRET` defaults to `change-me-in-production` and is missing from `api/.env.example` (compose fails hard without it; `scripts/start_docker.sh` generates it).
- pipecat is vendored but scripts/CI treat it as a git submodule (no `.gitmodules`) — submodule steps silently no-op.
- Hardcoded secrets in compose: redis `redissecret`, minio `minioadmin`, a real PostHog key.
- Registry default mismatch: compose uses `dograhai`, `start_docker.sh` uses `ghcr.io/dograh-hq`.
- `certs/` referenced by the `remote` profile but absent (generated at deploy time).
- `stripe` unpinned in `api/requirements.txt`.

## Running it today

- Native dev: `docker compose -f docker-compose-local.yaml up` (postgres/redis/minio), then api (uvicorn) + ui (`npm run dev`) from source; migrations via `scripts/migrate.sh`. See `docs/contribution/setup.mdx`.
- Full stack: `scripts/start_docker.sh` (pulls images, generates JWT secret).
- Tests: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/...`
