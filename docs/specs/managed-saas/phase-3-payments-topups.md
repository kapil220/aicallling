# Payments / Credit Top-ups (Stripe) — Design Spec

**Date:** 2026-07-21
**Phase:** 3 of 5 (managed-SaaS program)
**Status:** Approved for planning

---

## Context

Phase 1 (`docs/specs/managed-saas/phase-1-billing-engine-core.md`) built the local
billing brain: `CreditLedgerModel` as the append-only source of truth for balance,
`OrganizationModel.credit_balance_cents` as a cached fast-read balance, and
`BillingService.credit(organization_id, amount_cents, type, *, description,
created_by, idempotency_key)` as the *only* sanctioned way to add credits to an org.
Phase 1 explicitly deferred the write path for customers: "No write path for customers
in Phase 1 (that's Stripe, Phase 3)."

This phase gives customers a **self-serve way to buy credits**: pick a prepaid credit
pack, pay via Stripe Checkout, and have the ledger topped up automatically and
idempotently when Stripe confirms payment. It does not touch pricing, authorization, or
debiting — those stay exactly as Phase 1 defined them. It also does not build a
customer-facing billing *engine*; it builds the payment rail that feeds Phase 1's ledger.

**Model decision: prepaid credit packs, not postpaid/metered subscriptions.** A customer
buys a fixed bundle (e.g. "$50 → 5,200 credits") via a one-time Stripe Checkout Session.
There is no recurring subscription, no usage-based invoice, and no credit line. This
matches Phase 1's hard-cutoff, no-negative-balance design: money is always collected
*before* the credits it pays for can be spent. Auto-topup (charge a saved payment method
automatically when balance crosses a threshold) is a natural extension of this same
one-time-Checkout primitive and is noted as a follow-up in Open Questions, not built here.

### What already exists and is reused (not rebuilt)
- **Credit ledger & balance cache** — `CreditLedgerModel` (`credit_ledger` table) and
  `OrganizationModel.credit_balance_cents`, both from Phase 1
  (`api/db/models.py`, per the Phase 1 spec).
- **`BillingService.credit(...)`** (`api/services/billing/billing_service.py`) — the
  atomic, row-locked credit-grant primitive. Phase 3 calls this with `type='topup'` on
  successful payment and `type='refund'` on Stripe refund events; it does **not**
  reimplement balance mutation.
- **Idempotency pattern** — Phase 1 established `idempotency_key` as unique per
  `(organization_id, idempotency_key)` on the ledger, used there as `debit:{run_id}`.
  Phase 3 reuses the exact same column and constraint, keyed off the Stripe event id.
- **Org/user auth** — `OrganizationModel` (`api/db/models.py:105`),
  `get_user_with_selected_organization` (`api/services/auth/depends.py:159`) for
  org-scoped, authenticated endpoints (checkout creation, pack listing, payment history).
- **Config storage pattern** — `OrganizationConfigurationModel`
  (`api/db/models.py:198`, key/value JSON per org) is the existing precedent for
  per-org settings; we follow the same shape for anything org-scoped that isn't a
  first-class column.
- **External credential storage precedent** — `ExternalCredentialModel`
  (`api/db/models.py:953`) shows the repo's existing pattern for storing
  provider-issued identifiers/secrets per org; Phase 3's `stripe_customer_id` follows
  the same spirit but is added as a plain column (see Data Models) since it is a single
  non-secret identifier, not a credential blob.
- **Routes pattern** — `api/routes/organization_usage.py` is the precedent for an
  org-scoped, user-authenticated read route; Phase 3 adds a sibling `billing` route
  module following the same dependency-injection style.
- **Billing UI anchor** — `api/services/quota_service.py:36` already tells rejected
  users to "purchase more credits from `/billing`"; this phase is what makes that URL
  real.

---

## Goals

1. A customer can **buy a fixed credit pack** through Stripe Checkout without leaving
   the product (redirect out, redirect back).
2. On confirmed payment, the org's Phase 1 ledger is credited **exactly once**, even if
   Stripe redelivers the webhook, the customer double-clicks, or events arrive
   out of order.
3. A customer can see their **current balance** (Phase 1 read endpoint) and their
   **payment history** (this phase).
4. Refunds and failed/canceled payments are **reflected accurately**: a Stripe refund
   produces a ledger `refund` row; a failed/canceled Checkout never credits the ledger.
5. The whole feature ships **behind a flag** so staging can run against Stripe test mode
   before it's exposed to real customers.

### Non-goals (later phases / explicitly out of scope)
- Postpaid / usage-based subscriptions or metered billing via Stripe Billing — Phase 1's
  model is prepaid-only; this phase does not introduce a credit line.
- Invoicing, tax compliance (GST/VAT registration, invoice numbering) beyond enabling
  Stripe Tax at the Checkout Session level.
- Dunning / retry logic for failed recurring charges — there is no recurring charge in
  this phase.
- Multi-currency pricing (packs are USD-only for v1; see Open Questions).
- Non-superuser billing admin roles (Phase 4).
- Central key-proxy gateway (Phase 2, orthogonal).

---

## Key decisions

| Decision | Choice |
|---|---|
| Payment model | **Prepaid credit packs** via one-time Stripe Checkout Sessions (`mode=payment`). No subscriptions. |
| Source of truth for balance | Unchanged from Phase 1: `CreditLedgerModel`. This phase only ever *writes* to it via `BillingService.credit(type='topup'/'refund')`. |
| Idempotency key | **Stripe event id** (`evt_...`), stored as the ledger `idempotency_key`. Guarantees a replayed/duplicate webhook delivery cannot double-credit. |
| Ledger vs. Payment record | `CreditLedgerModel` remains append-only and minimal (Phase 1 shape unchanged). A new `PaymentModel` row is the Stripe-facing record (session/intent ids, amounts, status) and is what "payment history" reads from. The ledger row references the payment (see Data Models). |
| Pack catalog storage | New `PaymentPackModel` table (not hardcoded config), so operators can add/retire packs without a deploy. |
| Webhook auth | Stripe signature verification (`STRIPE_WEBHOOK_SECRET`) on the raw request body; no other auth on the webhook route (Stripe can't send our session cookies/JWTs). |
| Feature gate | `BILLING_PAYMENTS_ENABLED` (or reuse `BILLING_ENGINE=local` as a prerequisite gate, since payments are meaningless without the local ledger) — default off. |

---

## Data models (new)

Added to `api/db/models.py`, with an Alembic migration.

### `PaymentPackModel` (`payment_packs`)
The credit-pack catalog. Read by `GET /billing/packs`; referenced by id when creating a
Checkout Session.

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `pack_key` | str, unique | stable slug, e.g. `starter_10`, `growth_50`, `scale_100` |
| `display_name` | str | e.g. "Starter" |
| `price_cents` | int | what the customer pays, e.g. `1000` for $10.00 |
| `credits_granted` | int | credits added on success, e.g. `1000`; may exceed `price_cents` to express a bonus (e.g. $100 pack grants 10,500 credits) |
| `currency` | str | `usd` for v1 (see Open Questions) |
| `is_active` | bool | inactive packs are hidden from `GET /billing/packs` but historical `PaymentModel` rows referencing them remain valid |
| `sort_order` | int | display ordering |
| `created_at` / `updated_at` | datetime | |

Example seed rows (illustrative, not final pricing):

| pack_key | price_cents | credits_granted | note |
|---|---|---|---|
| `starter_10` | 1000 | 1000 | no bonus |
| `growth_50` | 5000 | 5200 | 4% bonus |
| `scale_100` | 10000 | 10500 | 5% bonus |

### `PaymentModel` (`payments`)
One row per Checkout attempt. This is the Stripe-facing audit trail; the ledger stays
generic per Phase 1's design.

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `organization_id` | FK organizations | indexed |
| `payment_pack_id` | FK payment_packs, nullable | null if pack was later deleted; amounts below are still authoritative |
| `stripe_checkout_session_id` | str, unique, indexed | `cs_...`, set at creation |
| `stripe_payment_intent_id` | str, unique nullable, indexed | `pi_...`, set once Stripe attaches it |
| `stripe_customer_id` | str, indexed | denormalized copy of the org's Stripe customer id at time of purchase |
| `amount_cents_paid` | int | actual amount charged (mirrors `price_cents` at purchase time; packs can change later) |
| `currency` | str | |
| `credits_granted` | int | mirrors `PaymentPackModel.credits_granted` at purchase time |
| `status` | enum | `pending` / `succeeded` / `failed` / `refunded` / `partially_refunded` |
| `credit_ledger_id` | FK credit_ledger, nullable | set once the ledger `topup` row is written; the join between payment and ledger |
| `failure_reason` | str, nullable | Stripe's decline/cancellation reason, for support |
| `created_at` / `updated_at` | datetime | |

Row lifecycle: created `pending` at Checkout Session creation → updated to `succeeded`
(and `credit_ledger_id` set) by the webhook handler → optionally `refunded` /
`partially_refunded` later by a separate refund event.

### `OrganizationModel` (extend)
- `stripe_customer_id` (str, unique nullable) — created lazily on first checkout
  (`Stripe Customer.create`) and reused on subsequent purchases so Stripe can show a
  customer their full payment history and so we can support saved payment
  methods for a future auto-topup. Stored as a plain column (like
  `price_per_second_usd`, `api/db/models.py:152`) rather than in
  `ExternalCredentialModel`, since it's a single non-secret identifier looked up on
  nearly every billing request, not a credential blob.

### `CreditLedgerModel` (Phase 1 — unchanged, referenced here)
Phase 3 writes rows with:
- `type='topup'`, `amount_cents=credits_granted` (positive), `idempotency_key=stripe:{event_id}`,
  `description='Stripe payment {payment_intent_id} ({pack_key})'`.
- `type='refund'`, `amount_cents=-refunded_credits` (negative), `idempotency_key=stripe:{event_id}`,
  `description='Stripe refund {refund_id} for payment {payment_intent_id}'`.

No schema change needed to `CreditLedgerModel` — Phase 1 already defined `type` as an
enum including `refund`, and `idempotency_key` as the dedup key. This is precisely the
extension point Phase 1 was designed for.

---

## Components

### 1. `PaymentService` (`api/services/billing/payment_service.py`) — new
Owns everything Stripe-shaped; delegates all ledger/balance mutation to Phase 1's
`BillingService`. Pure enough to unit test with a mocked Stripe client.

- `list_active_packs() -> list[PaymentPackModel]`
- `ensure_stripe_customer(organization) -> str`
  Returns `organization.stripe_customer_id`, creating a Stripe Customer (with org id
  in metadata) and persisting it if absent. Idempotent via Stripe's
  `idempotency_key` request option keyed on `org:{organization_id}:customer` so a
  retried request can't create two Stripe customers for one org.
- `create_checkout_session(organization, pack, success_url, cancel_url) -> CheckoutSessionResult`
  Creates a `PaymentModel` row (`status=pending`) *and* a Stripe Checkout Session
  (`mode=payment`, `customer=stripe_customer_id`, one line item derived from the pack,
  `metadata={organization_id, payment_id, pack_key}`, `client_reference_id=organization_id`).
  Stores `stripe_checkout_session_id` on the `PaymentModel` row before returning the
  Checkout URL to the caller. Metadata carries the internal `payment_id` so the webhook
  handler never has to guess which row to update.
- `handle_checkout_completed(event) -> None`
  The `checkout.session.completed` handler (see Webhook flow below).
- `handle_payment_failed(event) -> None`
  Marks the `PaymentModel` row `failed` with `failure_reason`; no ledger write.
- `handle_charge_refunded(event) -> None`
  Computes the refunded credit amount proportionally to `amount_refunded /
  amount_cents_paid`, writes a `refund` ledger row via `BillingService.credit(...,
  type='refund')` with a negative `amount_cents`, and updates `PaymentModel.status`.
- `get_payment_history(organization_id, *, limit, cursor) -> list[PaymentModel]`

### 2. Webhook endpoint (`api/routes/webhooks.py` or `api/routes/billing.py`) — new
`POST /webhooks/stripe`, unauthenticated (no user session — Stripe calls this
server-to-server) but **signature-verified**:
1. Read the raw request body (must not be JSON-parsed by a generic body-parsing
   middleware first — Stripe signatures are computed over the exact raw bytes).
2. Verify `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET`
   (`stripe.Webhook.construct_event`). Reject with `400` on failure — never process an
   unverified payload.
3. Dispatch on `event.type`:
   - `checkout.session.completed` → `PaymentService.handle_checkout_completed`
   - `payment_intent.succeeded` → secondary confirmation path only (see Idempotency
     below); primary credit grant happens on `checkout.session.completed`.
   - `payment_intent.payment_failed` / `checkout.session.expired` → `handle_payment_failed`
   - `charge.refunded` → `handle_charge_refunded`
   - `charge.dispute.created` (chargeback) → log + mark `PaymentModel` flagged for
     manual review; no automatic ledger action in v1 (see Error handling).
4. Always return `200` once the event is durably processed (or was a no-op duplicate),
   so Stripe stops retrying. Return non-2xx only on genuine processing failure so Stripe
   retries with backoff.

### 3. API routes (`api/routes/billing.py`) — new
All under `get_user_with_selected_organization`, org-scoped, same pattern as
`api/routes/organization_usage.py`:
- `GET /billing/packs` — active pack catalog.
- `POST /billing/checkout` — body `{pack_key}`; creates/reuses Stripe customer,
  creates `PaymentModel` + Checkout Session, returns `{checkout_url}`.
- `GET /billing/payments` — paginated payment history for the caller's org.
- Balance itself is **not** duplicated here — the UI calls Phase 1's existing balance
  read endpoint on `api/routes/organization_usage.py`.

### 4. Frontend billing page (`ui/src/app/billing/` or wherever the org-scoped
authenticated app shell lives, mirroring existing pages under `ui/src/app/`) — new
- Balance banner: reads the Phase 1 balance endpoint (already wired for the usage UI).
- Pack grid: reads `GET /billing/packs`, "Buy" button calls `POST /billing/checkout`
  and does a full-page redirect to `checkout_url` (Stripe-hosted page; no Stripe
  Elements/PCI scope in our frontend).
- Return handling: `success_url` and `cancel_url` both point back to the billing page
  with a query param (`?checkout=success` / `?checkout=cancelled`); on `success` the
  page shows a "processing" toast and polls the balance endpoint a few times (payment
  webhook may land a few hundred ms to a few seconds after redirect — see Error
  handling: "redirect without webhook").
- Payment history table: reads `GET /billing/payments`, shows pack, amount, credits,
  status, date.

---

## API Contracts

### `GET /billing/packs`
Success `200`:
```json
{
  "packs": [
    {"pack_key": "starter_10", "display_name": "Starter", "price_cents": 1000, "credits_granted": 1000, "currency": "usd"},
    {"pack_key": "growth_50", "display_name": "Growth", "price_cents": 5000, "credits_granted": 5200, "currency": "usd"}
  ]
}
```

### `POST /billing/checkout`
Request:
```json
{"pack_key": "growth_50"}
```
Success `200`:
```json
{"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_..."}
```
Errors:
- `404` — unknown/inactive `pack_key`.
- `409` — org has an existing `pending` `PaymentModel` created in the last N minutes for
  the same pack that hasn't expired (soft duplicate-click guard; Stripe Checkout
  Sessions also self-expire after 24h).
- `503` — Stripe API unreachable; do not create a `PaymentModel` row if the Stripe call
  itself fails (create the DB row only after the Stripe session is confirmed created, or
  wrap both in a transaction that rolls back on Stripe failure).

### `GET /billing/payments?cursor=&limit=`
Success `200`:
```json
{
  "payments": [
    {
      "id": 42,
      "pack_key": "growth_50",
      "amount_cents_paid": 5000,
      "currency": "usd",
      "credits_granted": 5200,
      "status": "succeeded",
      "created_at": "2026-07-20T10:15:00Z"
    }
  ],
  "next_cursor": null
}
```

### `POST /webhooks/stripe`
Request: raw Stripe event payload + `Stripe-Signature` header.
Success `200`: `{"received": true}`
Errors:
- `400` — signature verification failed, or malformed payload.
- `422` — event references an unknown `payment_id` in metadata (log loudly; return
  `200` anyway after logging, since retrying won't fix a metadata bug — see Error
  handling).
- `5xx` — transient failure (DB unavailable etc.); Stripe will retry per its backoff
  schedule (up to ~3 days).

---

## Webhook flow — `checkout.session.completed`

```
Stripe sends checkout.session.completed
  └─ verify signature (STRIPE_WEBHOOK_SECRET)
  └─ look up event.id → has this event.id already produced a credit_ledger row?
       (query CreditLedgerModel by idempotency_key = f"stripe:{event.id}")
       ├─ yes → no-op, return 200  (duplicate delivery / redelivery after our own retry)
       └─ no  → continue
  └─ payment_id = session.metadata.payment_id
  └─ load PaymentModel by payment_id (fallback: by stripe_checkout_session_id)
       ├─ not found → log error (metadata bug / stale env), return 200 (don't retry-loop forever)
       └─ found → continue
  └─ if PaymentModel.status == 'succeeded' already → no-op, return 200 (belt-and-suspenders
       alongside the ledger idempotency check above)
  └─ session.payment_status == 'paid'?
       ├─ no (e.g. async payment methods still pending) → leave PaymentModel pending, return 200
       └─ yes → continue
  └─ BillingService.credit(
         organization_id=payment.organization_id,
         amount_cents=payment.credits_granted,
         type='topup',
         description=f"Stripe payment {session.payment_intent} ({payment.pack_key})",
         idempotency_key=f"stripe:{event.id}",
     )
  └─ update PaymentModel: status='succeeded', stripe_payment_intent_id=session.payment_intent,
       credit_ledger_id=<new ledger row id>
  └─ commit, return 200
```

The dedup check and the `BillingService.credit` call happen inside the same DB
transaction as the `PaymentModel` update, so a crash between "ledger written" and
"payment row updated" can't happen silently — either both commit or neither does, and a
Stripe retry safely replays the whole block (the ledger's unique `idempotency_key`
constraint makes the second `credit()` attempt a guaranteed no-op / conflict that the
handler treats as "already applied").

`payment_intent.succeeded` is intentionally **not** the primary trigger: for Checkout
Sessions, `checkout.session.completed` carries the metadata we set (`payment_id`,
`organization_id`), while `payment_intent.succeeded` may arrive first, last, or (for
some payment methods) not distinctly at all. It's handled as a secondary confirmation
that's a no-op if the checkout-completed path already credited the ledger.

---

## Error handling & edge cases

- **Webhook arrives before the customer is redirected back:** normal and expected —
  Stripe processes payment server-side faster than the browser redirect completes. The
  frontend's "processing" poll (see Frontend section) handles the customer-facing race;
  the backend is unaffected either way since crediting is driven entirely by the
  webhook, never by the redirect.
- **Customer is redirected back but the webhook is delayed/lost:** the balance won't
  update immediately; UI shows "processing" and polls for a bounded window (e.g. 30s),
  then falls back to "we'll email you once confirmed" messaging. A background
  reconciliation job (see Rollout) sweeps `PaymentModel` rows stuck `pending` for >10
  minutes and calls `stripe.checkout.Session.retrieve` to self-heal if Stripe shows them
  paid — protects against a permanently dropped webhook (network partition, Stripe
  outage) rather than relying on Stripe's redelivery alone.
- **Duplicate/replayed webhook events:** handled by the ledger's unique
  `(organization_id, idempotency_key)` constraint keyed on the Stripe event id — a
  second `BillingService.credit()` call for the same `event.id` is rejected/no-ops at
  the DB level, never double-credits.
- **Out-of-order events** (e.g. `charge.refunded` arrives before we've marked the
  payment `succeeded` because `checkout.session.completed` is still retrying): the
  refund handler looks up `PaymentModel` by `stripe_payment_intent_id`; if not yet
  `succeeded`, it defers by returning a `5xx` so Stripe retries the refund event later
  rather than crediting a refund against a payment we haven't recorded as paid yet.
- **Partial refunds:** `charge.refunded` includes `amount_refunded` which may be less
  than the full charge; the ledger refund is computed proportionally
  (`credits_granted * amount_refunded / amount_cents_paid`, floored) and
  `PaymentModel.status` becomes `partially_refunded`. If a partial refund would refund
  more credits than the org currently holds (already spent), the ledger still writes
  the negative row — Phase 1's design allows balance to go negative only via ledger
  history reconciliation, never via live spend (hard cutoff prevents spend-driven
  negative balance; a refund-driven negative balance is a separate, rarer condition
  that support/ops handle manually, flagged via balance going negative).
- **Chargebacks (`charge.dispute.created`):** not auto-processed against the ledger in
  v1 — flagged on the `PaymentModel` for manual ops review, since a chargeback often
  correlates with fraud and warrants a human decision (e.g. also freezing the org),
  not an automatic silent credit clawback. Full automation deferred (Open Questions).
- **Currency:** v1 is USD-only end to end (pack price, Stripe Checkout currency, credit
  cents). No FX conversion logic exists or is needed yet.
- **Stale/removed pack referenced mid-checkout:** `PaymentPackModel` fields are copied
  onto `PaymentModel` at Checkout Session creation time, so deactivating or repricing a
  pack after a session is created doesn't change what the in-flight purchase grants.
- **Flag off:** `/billing/*` routes and the webhook route can exist but are gated to
  return `404`/reject if `BILLING_PAYMENTS_ENABLED` is false, so OSS/MPS deployments
  without Stripe configured see no behavior change.

---

## Env / config

| Variable | Purpose |
|---|---|
| `STRIPE_SECRET_KEY` | Server-side Stripe API key (test or live). |
| `STRIPE_WEBHOOK_SECRET` | Verifies `Stripe-Signature` on `/webhooks/stripe`. |
| `STRIPE_PUBLISHABLE_KEY` | Exposed to the frontend only if/when we move off pure
  redirect-to-Checkout toward embedded Stripe Elements; not required for v1's
  hosted-Checkout-redirect flow, but reserved so the env contract doesn't need to
  change later. |
| `BILLING_PAYMENTS_ENABLED` | Feature flag; default `false`. Gates routes + webhook
  processing. Should imply/require `BILLING_ENGINE=local` (Phase 1 flag) since payments
  are meaningless without the local ledger. |

Stripe Tax (automatic sales-tax/VAT calculation on Checkout Sessions) can be enabled
per Stripe account settings and toggled on the Checkout Session
(`automatic_tax={enabled: true}`) without any schema change here — flagged as a
follow-up config toggle, not built into this phase's DDL.

---

## Testing strategy

Tests run against the test DB via `api/.env.test` per AGENTS.md. Stripe calls are
mocked with the `stripe` Python SDK's test fixtures / a mocked HTTP client; no live
Stripe test-mode network calls in unit tests. A small number of integration tests may
use Stripe's official test-mode fixtures (`stripe listen --forward-to` / test event
payloads) run manually or in a dedicated CI job, not the default suite.

**Unit (`PaymentService`)**
- Pack → credits math: `credits_granted` copied correctly onto `PaymentModel` at
  session creation; proportional refund math (`amount_refunded / amount_cents_paid`)
  across boundary cases (full refund, 1-cent refund, refund equal to amount paid).
- `ensure_stripe_customer`: creates once, reuses on second call; idempotency key passed
  to Stripe client prevents duplicate-customer creation on retry.
- Webhook signature verification: valid signature accepted; tampered payload / wrong
  secret rejected with `400`; missing header rejected.
- Idempotent webhook handling: same `event.id` processed twice → exactly one
  `CreditLedgerModel` row, `PaymentModel` unchanged on the second call.
- Out-of-order refund-before-success: refund event for a not-yet-`succeeded` payment
  returns a retryable error rather than crediting.

**Integration (checkout → webhook → ledger)**
- `POST /billing/checkout` → `PaymentModel(status=pending)` created, Stripe session
  creation called with correct line item and metadata.
- Simulated `checkout.session.completed` webhook (constructed test event, real
  signature computed with a test `STRIPE_WEBHOOK_SECRET`) → `PaymentModel.status` flips
  to `succeeded`, `CreditLedgerModel` gains a `topup` row with the expected
  `amount_cents`, `OrganizationModel.credit_balance_cents` increases by the same amount
  (reusing Phase 1's balance-cache assertions).
- Duplicate delivery of the same event → still exactly one ledger row (replay the exact
  same test event a second time).
- Failed/expired checkout event → `PaymentModel.status='failed'`, no ledger row, balance
  unchanged.
- Refund event → negative `refund` ledger row, balance decreases, `PaymentModel.status`
  reflects full vs. partial refund correctly.
- Flag off (`BILLING_PAYMENTS_ENABLED=false`) → routes reject / webhook is a no-op; no
  ledger writes possible through this path.

**Frontend**
- Checkout button triggers redirect to the URL returned by `POST /billing/checkout`.
- Success return state polls balance and eventually reflects the new balance (mock the
  balance endpoint transitioning from old → new value across polls).
- Payment history table renders pack name, amount, status correctly from
  `GET /billing/payments`.

---

## Rollout

1. Ship models + migration (`PaymentPackModel`, `PaymentModel`,
   `OrganizationModel.stripe_customer_id`) + `PaymentService` + routes + webhook behind
   `BILLING_PAYMENTS_ENABLED` (default off), requiring `BILLING_ENGINE=local`.
2. Configure a Stripe **test-mode** account for staging: `STRIPE_SECRET_KEY`,
   `STRIPE_WEBHOOK_SECRET` (via `stripe listen` locally / a registered test webhook
   endpoint on staging), seed `PaymentPackModel` rows.
3. Run the full checkout → Checkout-hosted-page → webhook → ledger loop on staging with
   Stripe test cards (including a declined-card case and a refund via the Stripe
   dashboard) before touching production keys.
4. Stand up the stuck-`pending` reconciliation sweep (background job, e.g. via the
   existing ARQ worker infra) as a safety net before enabling in production.
5. Switch to Stripe **live-mode** keys for the hosted deployment, register the
   production webhook endpoint, flip `BILLING_PAYMENTS_ENABLED` on for the hosted org(s).
6. Monitor the first N real payments manually (payment succeeded → ledger credited →
   balance visible in UI) before removing the manual watch.

---

## Open questions deferred to their phases (or beyond)
- **Multi-currency packs / FX:** v1 is USD-only; supporting local currencies (Stripe
  supports this natively per Checkout Session) is a follow-up once there's demand.
- **Auto-topup:** charge a saved payment method automatically when
  `credit_balance_cents` drops below a per-org threshold, using the
  `stripe_customer_id` + a saved payment method (Stripe `SetupIntent` at first
  purchase). Same `BillingService.credit(type='topup')` sink; new trigger only. Not
  built in Phase 3.
- **Invoicing / GST-compliant receipts beyond Stripe's built-in receipt emails:**
  deferred; revisit if/when selling into markets with mandatory invoice formats.
- **Automated chargeback handling** (auto-suspend org, auto-clawback beyond the manual
  flag in v1): deferred pending real fraud-rate data.
- **Non-superuser billing admin roles** (e.g. an org admin managing their own org's pack
  visibility) — Phase 4.
