# SaaS Platform Design — Voice-AI Agents, Sold Self-Serve

Date: 2026-07-21. Status: approved by owner (design review, parts 1–3).
Baseline audit: `docs/PLATFORM.md`.

## Goal

Turn the existing Dograh-based codebase into a hosted, self-serve SaaS: anyone signs up, subscribes to a monthly plan with included call minutes, builds voice AI agents with free choice of AI models (powered by platform-held provider keys), connects telephony (their own provider or a platform-provisioned number), and runs campaigns.

## Decisions (owner-approved)

| Decision | Choice |
|---|---|
| Business model | Hosted SaaS, self-serve signup |
| Auth | Clerk (hosted), hard email-verification gate |
| Profile | Full (name, avatar upload, timezone, company, password, linked Google, delete account) |
| Deployment posture | New first-class `DEPLOYMENT_MODE=saas` |
| Provider keys | Platform keys; users never handle AI provider keys |
| Pricing | Subscription plans with included monthly minutes (no rollover) |
| Payments | Razorpay first, behind a `PaymentProvider` abstraction (Stripe later) |
| Trial | Free trial minutes on signup, no card |
| Model selection | Full provider/model freedom with per-combo minute-burn multiplier |
| Telephony | Both: BYO provider (guided setup) AND platform-provisioned numbers (Twilio, US/CA + India) |
| Campaigns | Available on all plans; limits scale by tier |
| Hardening v1 | Credential encryption at rest, webhook signature verification, no-default-secrets boot check |
| Added scope | Monitoring/observability, production deployment design |
| Build order | Demo-first: rebrand + auth + trial → billing → platform numbers → monitoring/launch |

## 1. `DEPLOYMENT_MODE=saas`

New mode in `api/constants.py` alongside `oss`, checked wherever `DEPLOYMENT_MODE` is read today.

In saas mode:
- `BILLING_ENGINE=local` forced; all MPS (`services.dograh.com`) calls disabled.
- Payments enabled; strict CORS allowlist mandatory.
- Boot-time validation: refuse to start if any of these are missing/defaulted: Clerk keys, `CREDENTIAL_ENCRYPTION_KEY`, app JWT secret, CORS origins — plus Razorpay keys + webhook secret once `BILLING_PAYMENTS_ENABLED=true` (payments stay off during phase 1).
- Sentry/telemetry enabled.
- `/health` reports `deployment_mode: saas`; the frontend branches on it (billing UI on, Dograh upsells/MPS cards off, own branding).

OSS behavior remains untouched.

## 2. Auth: Clerk

- **Backend:** `AUTH_PROVIDER=clerk` as a third path in `api/services/auth/` mirroring `stack_auth.py`:
  - Verify Clerk session JWTs via JWKS locally (no per-request network call).
  - First login lazily provisions `UserModel` (provider_id = Clerk user id) + `OrganizationModel` + admin membership, and grants trial minutes (idempotent, one per Clerk user).
  - Clerk webhooks (`user.updated`, `user.deleted`) sync email/name and drive account-deletion cascade (archive org data per retention policy). Signature-verified (Svix).
- **Frontend:** third implementation in the existing provider abstraction (`ui/src/lib/auth/providers/`): `ClerkProviderWrapper` using `@clerk/nextjs`, exposing the same `useAuth()` shape (`getAccessToken` returns the Clerk session token). Middleware requires a session for all app routes.
- **Hard verification gate:** configured in Clerk — email verification required to complete signup; unverified users never reach the dashboard.
- **Google sign-in, magic links, password reset:** Clerk-native; no custom build.
- **Orgs stay internal:** Clerk is identity only. Existing org/membership/roles, last-admin guard, superadmin backoffice, and org-scoping all unchanged. Superadmin impersonation gets a Clerk-mode path (mint app-level impersonation token, pattern exists for local mode).
- API-key auth (`X-API-Key`) continues to work unchanged for the public API.

## 3. Profile

- Clerk `UserProfile` component: name, avatar upload, email, password, linked accounts, delete account.
- App-level "Workspace profile": company name + timezone stored in the existing `UserConfiguration` JSON store (timezone feeds campaign scheduling defaults).
- Sidebar user menu switches to Clerk components in saas mode.

## 4. Pricing: plans + included minutes

### Plans
`PlanModel`: tier key, display name, monthly price (INR first), included minutes, limits (max agents, concurrent calls, daily outbound calls, max active campaigns), Razorpay plan id, active flag. Org gets `plan_tier`, `subscription_id`, `subscription_status`, `current_period_end`.

Launch placeholders (tunable in superadmin):

| | Starter | Pro | Scale |
|---|---|---|---|
| Included minutes/mo | 300 | 1,500 | 6,000 |
| Max agents | 3 | 15 | unlimited |
| Concurrent calls | 2 | 10 | 25 |
| Max active campaigns | 1 | 5 | unlimited |

### Minutes on the existing ledger
- The tested credit ledger stays the accounting engine. Internally 1 minute = 100 ledger units at 1× burn.
- On each successful subscription charge: expire the previous period's remaining plan balance (negative adjustment entry, reason `plan_period_reset`), then grant the new period's allowance (reason `plan_renewal`). No rollover. Both entries idempotent on Razorpay event id.
- Pre-call authorization, mid-call affordability cutoff, and post-call debit reuse the existing local-billing paths unchanged.
- UI presents everything in minutes (balance / 100 at 1×), with burn multipliers explained inline.

### Burn multipliers
- The existing `PricingRule` engine prices each provider combo; surfaced to users as a multiplier (e.g. 2.0× minutes). Managed in superadmin. Unpriced combos fail closed (call blocked with a clear error) so new providers can't run at a loss.

### Limit enforcement
- Max agents: checked at workflow create.
- Concurrency: existing org concurrency slots, sourced from plan.
- Daily call cap + max active campaigns: checked at dispatch/campaign start.
- Minutes exhausted: calls blocked pre-start; running campaigns auto-pause (existing pause machinery); in-app + email upgrade prompt.

### Subscription lifecycle
- `active` → normal. `halted` (failed charge) → grace period, org becomes past-due: agents visible/editable, calls blocked. Cancelled/expired after grace → same blocked state until resubscribe. All transitions driven by Razorpay webhooks.

## 5. Payments: Razorpay behind `PaymentProvider`

- Interface: `create_subscription(org, plan) → checkout_ref`, `change_plan(...)`, `cancel(...)`, `handle_webhook(request) → BillingEvent`.
- V1 driver: Razorpay Subscriptions (UPI Autopay / cards / e-mandates). Webhooks: `subscription.activated/charged/halted/cancelled`, `payment.failed`; signature-verified; idempotent by event id (pattern exists in the Stripe code).
- Existing Stripe integration is kept dormant and becomes the second driver for international customers later.
- Billing page rework: current plan, renewal date, minutes meter (used/remaining, per-day sparkline), upgrade/downgrade/cancel, payment history, ledger detail (existing table).

## 6. Trial

- One-time grant on org provisioning: `TRIAL_MINUTES` env (default 15), reason `signup_trial`. Usable on web test calls and phone calls. No card.
- Abuse controls: Clerk hard email verification + per-IP signup rate limit.
- Dashboard shows trial minutes remaining; exhaustion → subscribe prompt.

## 7. Model selection: full freedom, platform keys

- Existing model-configuration UI ships with full provider matrix; in saas mode all key fields are removed.
- Platform keys live in server config (encrypted at rest) and are injected at pipeline-build time (`service_factory`); keys never appear in any API response (audit masking paths; strip in saas mode).
- Each saved combo displays its burn multiplier before save, in the workflow header, and on per-run usage.
- Fix: re-enable acyclic-graph validation in `api/services/workflow/workflow_graph.py`.

## 8. Telephony

### BYO (exists; add guided setup)
- Keep existing multi-provider connect flow; add a wizard: credential validation call, webhook auto-configuration via provider API where supported (Twilio/Plivo/Telnyx), test call step, and per-provider setup docs in-app.
- Carrier charges remain on the user's account; stated clearly in the UI. AI minutes still burn from the plan.

### Platform numbers (new)
- "Get a number": search by country/area code via platform Twilio account (subaccount per org), one-click purchase, webhooks auto-configured, inbound routed to a selected agent.
- Pricing: fixed monthly rental deducted from the ledger (shown in minutes-equivalent) + per-minute carrier burn added on top of AI minutes for calls on that number. Rental auto-renews monthly (ARQ job); insufficient balance → warning, then release after grace.
- Release flow + superadmin oversight of all provisioned numbers.
- Scope: Twilio only; US/CA + India numbers at launch (regulatory bundles handled manually where required).

## 9. Campaigns

- Available on all plans. Tier scales concurrency, daily call cap, max active campaigns.
- Pre-start check: minutes can plausibly cover the batch (batch size × avg expected minutes at combo burn); auto-pause on exhaustion.
- Everything else (CSV, scheduling windows, retries, circuit breaker, redial) ships as-is.

## 10. Hardening (v1 essentials only)

- **Credential encryption at rest:** Fernet envelope encryption with boot-required `CREDENTIAL_ENCRYPTION_KEY` for telephony credentials, integration secrets, platform provider keys; migration re-encrypts existing rows; decrypt-on-use only.
- **Webhook signature verification:** Razorpay, Clerk (Svix), telephony (Twilio exists — verify coverage).
- **Boot-time secret validation** in saas mode (part of §1).
- Deferred to phase 2: broad rate limiting, compose secret cleanup, strict-CSP work.

## 11. Monitoring & observability

- Sentry (hooks exist) enabled in saas mode, API + UI.
- Prometheus `/metrics` on the API: request latency, active calls, campaign throughput, call success/failure, webhook failures, ledger errors. Grafana dashboards.
- Alerts: webhook failure spikes, call error-rate, payment failures.
- Admin analytics page (superadmin): signups, active orgs, MRR, minutes consumed, top orgs.

## 12. Production deployment

- Start: single VPS (≥8 vCPU / 16 GB; voice pipelines are CPU-heavy — plan ~1 vCPU per 2 concurrent calls of headroom), Docker Compose derived from existing files with hardened env.
- Postgres nightly backups + WAL archiving; Redis persistence (AOF); MinIO or S3 for audio.
- TLS via Caddy/nginx on own domain; Cloudflare in front; TURN (coturn) per existing compose profile.
- Zero-downtime updates via existing `scripts/rolling_update.sh`.
- Scale path: split ARQ workers and API onto separate hosts; Postgres to managed service.

## 13. Build order (demo-first)

0. **Docs baseline** — `docs/PLATFORM.md` (done alongside this spec).
1. **Demo-able product:** rebrand (own name/logo/colors), strip Dograh banners + MPS UI, `DEPLOYMENT_MODE=saas` skeleton + boot validation, Clerk auth (backend + frontend), profile, trial minutes on the local ledger, saas-mode key hiding. Outcome: a sellable-looking demo — signup → verify → build agent → free test call.
2. **Billing:** plans, Razorpay subscriptions + webhooks, minutes meter + billing page, limit enforcement, lifecycle states.
3. **Platform numbers:** Twilio search/buy/release, rental billing, inbound routing.
4. **Launch pass:** credential encryption, monitoring stack, deployment hardening, e2e smoke (signup → verify → trial call → subscribe [Razorpay test mode] → BYO + platform-number call → campaign), user-facing docs refresh.

Each phase lands with pytest coverage following existing patterns (auth deps, billing service, webhook handlers all have test precedents).

## Out of scope for v1 (recorded)

- BYOK for AI providers; Stripe driver activation; one-off minute booster packs; email invites for non-registered users; number provisioning beyond Twilio/US/CA/India; `/automation` page; broad rate limiting & compose secret cleanup (phase 2 hardening); SSO/enterprise auth.

## Risks

- **Margin risk:** minute pricing must cover worst-case provider combos — mitigated by fail-closed pricing rules and superadmin-tunable multipliers.
- **Razorpay subscriptions + webhooks** are the least-reused surface (all-new integration) — mitigate with test-mode e2e before launch and idempotent event handling.
- **Trial abuse:** free minutes with platform keys — mitigated by hard email verification, per-IP limits, low default trial amount, and observability on trial usage.
- **Clerk dependency:** outage blocks logins (not running calls, which use API keys/JWTs already issued). Acceptable for v1.
