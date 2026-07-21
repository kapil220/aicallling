# Phase 1 SaaS Conversion — End-to-End Smoke Checklist (Task 13)

Run on branch `saas-phase1`, 2026-07-21. Environment: no real Clerk keys or
AI provider keys available. Items requiring those credentials are marked
PENDING with exact operator steps.

## Step 1 — Full backend suite

**PASS** (pre-existing failures unchanged; no phase-1 regression)

```
source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q
=== 43 failed, 1035 passed, 30 warnings in 74.01s ===
```

The 43 failures are pre-existing and reproduce identically on the
pre-phase-1 base commit `00e3f1a` (verified by running the same test
files/subset in a throwaway `git worktree` of that commit):

- `test_billing_service.py` (3) — FK failures, pre-existing (matches brief).
- `test_ts_bridge.py` (16) — Node/ESM bridge failures, pre-existing (matches brief).
- `test_credentials_admin_gating.py` (1), `test_mcp_save_workflow.py` (6),
  `test_org_membership_client.py` (2-3, flaky), `test_organization_members_routes.py`
  (5), `test_superuser_orgs_routes.py` (3), `test_workflow_archive_admin_gating.py`
  (1) — cross-test isolation flakiness in org-membership/admin/superuser
  routes tests, pre-existing (matches brief; identical failure set on base
  commit `00e3f1a` with the same subset).

None of the failing test files import or exercise any phase-1 module
(`api/services/auth/`, `api/services/billing/trial.py`,
`api/services/configuration/platform_defaults.py`,
`api/routes/clerk_webhooks.py`, `api/routes/billing_balance.py`,
`api/routes/workspace_profile.py`, `api/services/saas_config.py`) — verified
by grep across the failing test files. **No new failures caused by phase-1
code.**

Incidental finding (not a code defect): `api/tests/test_sdk_sync.py` was
flaky locally due to stray macOS AppleDouble (`.___pycache__`, `._*.pyc`)
resource-fork files under `sdk/python/src/dograh_sdk/typed/` tripping a
`UnicodeDecodeError` when the test read that tree as UTF-8. Cleaned with
`find . -name '._*' -delete`; passes cleanly after cleanup (folded into the
1035-passed count above).

## Step 2 — Manual smoke in saas mode

### 1. Unauthenticated `/overview` → redirected to `/auth/login` (Clerk)
**PENDING — requires real credentials**

Operator steps:
1. `docker compose -f docker-compose-local.yaml up -d`
2. Run DB migrations.
3. Start API with `DEPLOYMENT_MODE=saas AUTH_PROVIDER=clerk BILLING_ENGINE=local` plus real `CLERK_ISSUER`, `CLERK_WEBHOOK_SECRET`, `CLERK_SECRET_KEY` (and UI `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`).
4. Start UI, open `/overview` unauthenticated in a private browser window.
5. Confirm redirect to `/auth/login`.

### 2. Sign up with a new email → verification code required (hard gate) → lands in app
**PENDING — requires real credentials**

Operator steps: with the saas stack from item 1 running, sign up with a
fresh email at `/auth/signup`, confirm Clerk's email verification-code step
is enforced (cannot be skipped), then confirm landing in `/overview` after
verifying.

### 3. Sign up/in with Google → works
**PENDING — requires real credentials**

Operator steps: in the Clerk dashboard, enable the Google OAuth social
connection for the test instance; then use "Continue with Google" on
`/auth/login` or `/auth/signup` and confirm successful sign-in.

### 4. `/overview` shows "Call minutes remaining: 15"
**PENDING — requires real credentials**

Operator steps: after signing up (item 2) with a pricing rule seeded and the
trial-grant logic active, load `/overview` and confirm the copy reads "Call
minutes remaining: 15". (The one-time signup trial grant is unit-tested at
the code level — `api/services/billing/trial.py` and related tests all pass
— but the literal live UI string needs a real signup to observe.)

### 5. Create an agent from template; open builder; model config shows providers, no key fields
**PENDING — requires real credentials (platform AI keys)**

Operator steps: with `PLATFORM_OPENAI_API_KEY` / `PLATFORM_DEEPGRAM_API_KEY`
/ `PLATFORM_ELEVENLABS_API_KEY` set, create an agent from a template, open
the builder, open the AI Model Configuration editor, and confirm provider
options render but no raw API-key input fields are shown in saas mode.

### 6. Web test call connects and agent talks; balance drops; `/billing` ledger shows the debit
**PENDING — requires real credentials (Clerk + platform AI keys)**

Operator steps: place a web test call from the builder using the agent from
item 5, confirm audio round-trips both directions, end the call, then check
`/billing` for a new debit line matching call duration and a reduced "Call
minutes remaining" balance.

### 7. `/profile`: name/avatar/password (Clerk); company+timezone (workspace card) persists on reload
**PENDING — requires real credentials (Clerk)**

Operator steps: on `/profile`, change display name, upload an avatar, and
change password via Clerk's flow; separately edit company name and timezone
in the workspace card, save, then reload the page to confirm persistence.
(The workspace-card persistence path — `api/routes/workspace_profile.py` —
is covered by passing unit tests; only the live Clerk-backed name/avatar/
password flow and the UI persistence-after-reload need a real run.)

### 8. Delete the Clerk test user from the Clerk dashboard → webhook archives the org's API keys
**PENDING — requires real credentials (Clerk)**

Operator steps: in the Clerk dashboard, delete the test user created in item
2. Confirm the `user.deleted` webhook fires against
`api/routes/clerk_webhooks.py`, and verify in the DB (or superadmin UI) that
the org's API/service keys are archived, not left active.

### 9. No "Dograh" visible anywhere; tab title and sidebar say VoxAgent
**PASS**

- `ui/src/app/layout.tsx` sets `metadata.title = BRAND_NAME`, and
  `ui/src/constants/brand.ts` defines `BRAND_NAME = 'VoxAgent'`.
- Grep sweep of `ui/src/**/*.{ts,tsx}` for `Dograh`/`dograh` (excluding
  generated `src/client`) turned up only: internal identifiers (`dograh_auth_token`
  cookie name, `total_dograh_tokens`/`dograh_token_usage` generated-client
  fields, `DograhFormState`/`isDograhEffectiveConfig` internal variable
  names in `AIModelConfigurationV2Editor.tsx`), real upstream infra links
  (`docs.dograh.com`/`www.dograh.com` hrefs with generic link text,
  `github.com/dograh-hq/dograh` star badge, Slack community slug), and the
  `isOSS`-gated `DocumentUpload.tsx` notice about "Dograh's managed Model
  Proxy Service" (only rendered when `deploymentMode === 'oss'`, never in
  saas mode) — all previously catalogued and judged in
  `.superpowers/sdd/task-11-report.md`.
- One residual item, already flagged as a known gap in the task-11 report
  and not a new finding here: `EmbedDialog.tsx`'s inline-integration example
  code block contains `window.DograhWidget`, `dograh-inline-container`, and
  an example function literally named `DograhAgent()` — these document the
  real global/DOM-id contract of the shipped `ui/public/embed/dograh-widget.js`
  script and are not mode-gated. Cosmetic only (example code text inside a
  dialog, not primary UI chrome); recommend a follow-up ticket to rebrand
  the embed script itself, per task 11's own recommendation. No other
  user-visible "Dograh" strings render in saas mode.

### 10. Boot the API with `DEPLOYMENT_MODE=saas` but `CLERK_ISSUER` unset → refuses to start with a clear aggregated error
**PASS**

```
DEPLOYMENT_MODE=saas AUTH_PROVIDER=clerk BILLING_ENGINE=local (CLERK_ISSUER unset)
python -c "import api.app"
→ RuntimeError: Invalid saas deployment configuration:
  - CLERK_ISSUER is required in saas mode
  - CLERK_WEBHOOK_SECRET is required in saas mode
  - OSS_JWT_SECRET must be set to a non-default value
  - CORS_ALLOWED_ORIGINS must be an explicit allowlist
```

With all saas vars set to dummies (`CLERK_ISSUER=https://x.clerk.accounts.dev`,
`CLERK_WEBHOOK_SECRET=whsec_x`, `OSS_JWT_SECRET=notdefault`,
`CORS_ALLOWED_ORIGINS=http://localhost:3010`), `python -c "import api.app"`
imports cleanly (`IMPORT_OK`), confirming the validator only blocks on
missing/default config, not on the presence of dummy values.

### 11. Boot everything in plain OSS mode (`DEPLOYMENT_MODE=oss`, `AUTH_PROVIDER=local`) → email/password login still works, UI unchanged
**PASS**

- Default test env (`api/.env.test`, `DEPLOYMENT_MODE`/`AUTH_PROVIDER` unset,
  which is the OSS default) imports `api.app` cleanly.
- Targeted OSS auth tests all pass:
  `api/tests/test_auth_depends.py`, `api/tests/test_signup_creator_is_admin.py`,
  `api/tests/test_local_impersonation.py` → `3 passed`.
- UI production build compiles cleanly (see below); OSS-only UI branches
  (welcome header, GitHub star badge, latest-release check) are unchanged
  from task 11's rebrand pass, which only touched display strings, not
  gating logic — confirmed by reading task 11's own sweep table.

## Supporting verification (not a brief-numbered item, run to support the above)

**UI production build — PASS**

```
find ui/src -name '._*' -delete && cd ui && npm run build
→ Compiled successfully. All 45 routes generated. No type/lint errors.
```

## Summary

| # | Item | Result |
|---|---|---|
| Step 1 | Full backend suite | PASS |
| 1 | Unauth `/overview` redirect | PENDING — requires real credentials |
| 2 | Signup + verification code | PENDING — requires real credentials |
| 3 | Google sign-in | PENDING — requires real credentials |
| 4 | "Call minutes remaining: 15" | PENDING — requires real credentials |
| 5 | Agent builder, no key fields | PENDING — requires real credentials |
| 6 | Web test call + ledger debit | PENDING — requires real credentials |
| 7 | Profile Clerk flow + workspace persistence | PENDING — requires real credentials |
| 8 | Clerk user deletion → webhook archive | PENDING — requires real credentials |
| 9 | No "Dograh" visible / VoxAgent branding | PASS |
| 10 | Saas boot refuses without CLERK_ISSUER | PASS |
| 11 | OSS mode boot + login unchanged | PASS |

**4 PASS (Step 1 + items 9-11), 8 PENDING — requires real credentials (items 1-8), 0 FAIL.**
