# Billing Engine Core — Design Spec

**Date:** 2026-07-21
**Phase:** 1 of 5 (managed-SaaS program)
**Status:** Approved for planning

---

## Context

Dograh is being turned into a managed SaaS that Dograh (the operator) hosts, provides
all provider API keys for, and bills customers per-minute according to the
"architecture" (LLM + STT + TTS combination, or realtime provider) each call uses.

Today the credit ledger, quota enforcement, USD rating, and payment processing all live
in an **external closed service, "MPS"** (`services.dograh.com`), reached via
`api/services/mps_service_key_client.py`. This repo only *measures* call duration and
per-provider usage and *reports* it to MPS. To run our own managed SaaS we must build a
**local replacement for the billing brain**.

This spec covers **Phase 1: the billing engine core** — a local credit ledger,
per-architecture pricing, pre-call authorization, and post-call deduction. It reuses the
existing metering that already runs in the pipeline. Later phases (central key-proxy
gateway, Stripe top-ups, roles & admin panel, Google Sheets / WhatsApp integrations) are
out of scope here and get their own specs.

### What already exists and is reused (not rebuilt)
- **Duration & usage metering** — `api/services/pipecat/pipeline_metrics_aggregator.py`
  emits `call_duration_seconds` plus per-provider token/char/second usage; persisted to
  `WorkflowRunModel.usage_info` at completion (`event_handlers.py:320-333`).
- **Pre-call hook** — `api/services/quota_service.py::authorize_workflow_run_start()`
  (line 323) is the single entry point already called before a run starts.
- **Post-call hook** — `api/services/workflow_run_billing.py::report_workflow_run_platform_usage()`
  (line 49) is already called on run completion.
- **Per-second rate field** — `OrganizationModel.price_per_second_usd`
  (`api/db/models.py:152`).
- **Usage aggregates** — `OrganizationUsageCycleModel` (`api/db/models.py:631`).
- **Max-duration guard** — `api/services/pipecat/pipeline_engine_callbacks_processor.py`
  (`max_call_duration_seconds`, wired in `run_pipeline.py`) — reused for mid-call cutoff.
- **Architecture resolution** — the effective config
  (`api/services/configuration/ai_model_configuration.py::get_effective_ai_model_configuration_for_workflow()`)
  tells us `mode` (pipeline/realtime) and the selected providers per call.
- **Superuser surface** — `get_superuser` dependency + `/superuser` routes
  (`api/routes/superuser.py`) for the minimal admin controls.

---

## Goals

1. An organization has a **credit balance** held locally.
2. Each call is **priced per-minute** by the architecture it uses.
3. Credits are **checked before a call** (reject if insufficient) and **deducted after**
   (per-second, from measured duration).
4. If credits **run out mid-call**, the call ends gracefully (hard cutoff), so no
   negative balance / unpaid usage can accrue.
5. Operators can **grant/adjust credits and set pricing** via superuser endpoints so the
   engine is usable and testable end-to-end.
6. Existing OSS / MPS deployments are **unaffected** — the local engine is behind a flag.

### Non-goals (later phases)
- Central key-proxy gateway (Phase 2).
- Stripe / self-serve top-ups (Phase 3).
- Roles/permissions beyond the existing `is_superuser` (Phase 4).
- Sheets / WhatsApp (Phase 5).
- Customer-facing billing UI beyond read endpoints.

---

## Key decisions

| Decision | Choice |
|---|---|
| Credit unit | **1 credit = 1 cent (US$0.01)**. Balances stored as **integer cents**. Matches existing `dograh_tokens = cost_usd * 100`. No float money. |
| Rounding | **Per-second.** `cost_cents = round(duration_seconds × price_per_minute_cents / 60)`. |
| Overdraft | **Hard cutoff mid-call.** No negative balances. |
| Feature gate | New env/config flag (e.g. `BILLING_ENGINE=local`) selects the local engine; MPS/OSS paths remain as alternates. |

---

## Data models (new)

Added to `api/db/models.py`, with an Alembic migration.

### `CreditLedgerModel` (`credit_ledger`)
Append-only. Source of truth for balance.

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `organization_id` | FK organizations | indexed |
| `amount_cents` | int | signed: +topup/adjustment, −debit |
| `balance_after_cents` | int | running balance after this row |
| `type` | enum | `topup` / `debit` / `adjustment` / `refund` |
| `workflow_run_id` | FK workflow_runs, nullable | set on `debit` rows |
| `description` | str | human-readable reason |
| `idempotency_key` | str, unique nullable | prevents double-debit on retries |
| `created_by` | FK users, nullable | for admin adjustments |
| `created_at` | datetime | |

Unique constraint on `(organization_id, idempotency_key)` where key is not null — a
run's debit uses `debit:{workflow_run_id}` so completion retries never double-charge.

### `PricingRuleModel` (`pricing_rules`)
Resolves an architecture → per-minute rate.

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `organization_id` | FK organizations, nullable | null = global default rule |
| `mode` | str, nullable | `pipeline` / `realtime`; null = any |
| `llm_provider` | str, nullable | matches `ServiceProviders` value; null = any |
| `stt_provider` | str, nullable | null = any |
| `tts_provider` | str, nullable | null = any |
| `realtime_provider` | str, nullable | null = any |
| `price_per_minute_cents` | int | the rate |
| `priority` | int | higher wins on ties; default derived from specificity |
| `is_active` | bool | default true |

### `OrganizationModel` (extend)
- `credit_balance_cents` (int, default 0) — **cached** balance for fast reads and for
  the `SELECT ... FOR UPDATE` atomic deduction. The ledger remains authoritative; a
  reconcile check asserts cache == latest ledger `balance_after_cents`.

---

## Components

### 1. `BillingService` (`api/services/billing/billing_service.py`) — new
The local billing brain. Pure, well-bounded, unit-testable.

- `resolve_rate(effective_config, organization_id) -> RateResult`
  Resolves the architecture from the effective config and picks the best
  `PricingRuleModel` (most-specific match by non-null field count, then `priority`),
  falling back to `org.price_per_second_usd × 60`, then a global default. Returns the
  per-minute cents and which rule matched (for observability).
- `get_balance_cents(organization_id) -> int`
- `authorize(organization_id, rate) -> AuthResult`
  Ensures `balance ≥ max(MINIMUM_CENTS, one_minute_estimate)`. `MINIMUM_CENTS`
  replaces `MINIMUM_DOGRAH_CREDITS_FOR_CALL`.
- `credit(organization_id, amount_cents, type, *, description, created_by, idempotency_key)`
  Atomic: row-lock org, append ledger row, update cache.
- `debit_for_run(workflow_run, rate) -> LedgerRow`
  Computes `cost_cents = round(duration_seconds × rate / 60)`, writes an idempotent
  `debit` row (`idempotency_key=debit:{run_id}`), decrements cache. All inside one
  `SELECT ... FOR UPDATE` transaction on the org row to serialize concurrent calls.
- `max_affordable_seconds(organization_id, rate) -> int`
  Used by the mid-call cutoff to cap the run.

Concurrency contract: every balance mutation acquires a row lock on the org row first,
so N concurrent calls for one org serialize their reads-modify-writes and cannot each
authorize against the same credits.

### 2. Pre-call authorization wiring
In `quota_service.authorize_workflow_run_start()`, add a `local` branch (selected by the
feature flag) that: resolves the effective config for the run → `resolve_rate` →
`authorize`. On failure, raise the same insufficient-credits error shape the callers
already handle (`api/routes/telephony.py`, campaign dispatcher). Also compute and stash
`max_affordable_seconds` into the run context for the pipeline cutoff.

### 3. Mid-call hard cutoff
`run_pipeline` already sets `max_call_duration_seconds`. When the local engine is active,
set it to `min(configured_max, max_affordable_seconds)` so the existing
`pipeline_engine_callbacks_processor` guard ends the call gracefully when the paid-for
seconds are exhausted. No new mid-call ticking loop needed for v1 — the pre-authorized
cap bounds exposure to at most one rate-rounding interval.

### 4. Post-call deduction wiring
In `workflow_run_billing.report_workflow_run_platform_usage()`, add a `local` branch:
re-resolve the rate (stored on the run's `cost_info` at authorize time to avoid drift) →
`debit_for_run` → update `OrganizationUsageCycleModel` aggregates
(`used_dograh_tokens`, `total_duration_seconds`, `used_amount_usd`). Idempotent on retry.

### 5. Admin controls (superuser)
New routes in `api/routes/superuser.py` (or a `billing_admin.py` included under
`/superuser`), all `Depends(get_superuser)`:
- `POST /superuser/orgs/{org_id}/credits` — grant/adjust (writes ledger `topup`/`adjustment`).
- `GET  /superuser/orgs/{org_id}/credits` — balance + paginated ledger.
- `GET/POST/PATCH /superuser/pricing-rules` — list/create/update pricing rules.

Customer-facing **read** endpoint for balance/ledger added to `api/routes/organization_usage.py`
(reuses `get_user_with_selected_organization`, org-scoped) so the existing usage UI can
show a balance. No write path for customers in Phase 1 (that's Stripe, Phase 3).

---

## Data flow

```
Call start
  └─ authorize_workflow_run_start()
       └─ [local] resolve effective config → resolve_rate → authorize(balance ≥ min)
            ├─ insufficient → reject (existing error path)
            └─ ok → stash rate + max_affordable_seconds on run.cost_info / context

Pipeline run
  └─ max_call_duration_seconds = min(configured, max_affordable_seconds)
       └─ existing guard ends call gracefully at the cap

Call complete
  └─ report_workflow_run_platform_usage()
       └─ [local] cost = round(duration_s × rate/60)
            └─ debit_for_run (idempotent, row-locked) → update balance + usage cycle
```

---

## Error handling & edge cases

- **Concurrent calls, one org:** serialized via `SELECT ... FOR UPDATE` on the org row.
- **Completion job retried:** idempotency key `debit:{run_id}` makes the debit a no-op
  the second time.
- **Rate missing / no rule matches:** fall back org rate → global default; if still none,
  fail authorization closed (reject the call) and log loudly — never bill $0 silently.
- **Rate drift between authorize and settle:** rate resolved at authorize time is stored
  on the run and reused at settle, so pricing-rule edits mid-call don't change the charge.
- **Duration missing at settle:** if `call_duration_seconds` absent, fall back to the
  MPS correlation duration path already present, else 0 with a warning (no charge).
- **Balance cache vs ledger divergence:** a reconcile assertion (and a repair script)
  recompute cache from the ledger; ledger is authoritative.
- **Flag off (OSS/MPS):** zero behavior change — existing MPS branches run unchanged.

---

## Testing strategy

Tests run against the test DB via `api/.env.test` per AGENTS.md.

**Unit (`BillingService`)**
- Rate resolution: specificity ordering, priority tie-break, org fallback, global
  default, no-match → fail-closed.
- `debit_for_run`: per-second rounding correctness across boundary durations
  (0s, 1s, 59s, 60s, 61s, 90s).
- Idempotency: double `debit_for_run` for same run → single ledger row, one deduction.
- Concurrency: two simultaneous debits/authorizes for one org serialize; no lost update
  (simulate with concurrent sessions / row-lock test).

**Integration (lifecycle)**
- authorize → run → settle: balance decreases by exactly the priced amount; ledger and
  usage-cycle aggregates updated.
- Insufficient credits → authorize rejects with the expected error shape.
- Mid-call cutoff: low balance caps `max_call_duration_seconds`.
- Flag off: MPS path still selected; no local ledger writes.

**Admin**
- Grant credits writes a ledger row and updates cache; balance read reflects it.
- Pricing-rule CRUD; resolution picks the newly-created rule.

---

## Rollout

1. Ship models + migration + `BillingService` + admin endpoints behind
   `BILLING_ENGINE=local` (default off).
2. Seed a global default pricing rule + per-architecture rules via admin endpoints.
3. Enable on a staging org; run authorize→settle end-to-end; verify ledger balances.
4. Flip flag on for the hosted deployment.

---

## Open questions deferred to their phases
- Real per-token/char cost capture at source → **Phase 2 gateway** (this phase prices by
  duration × architecture rate, which is the product's billing model regardless).
- Self-serve credit purchase → **Phase 3 Stripe**.
- Non-superuser admin roles for billing management → **Phase 4**.
