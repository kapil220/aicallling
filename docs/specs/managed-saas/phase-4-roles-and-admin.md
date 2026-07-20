# Roles, Permissions & Admin Panel — Design Spec

**Date:** 2026-07-21
**Phase:** 4 of 5 (managed-SaaS program)
**Status:** Approved for planning

---

## Context

Today Dograh has exactly two authorization concepts:

1. **Platform superuser** — `UserModel.is_superuser` (`api/db/models.py:70`), gated by
   the `get_superuser` dependency (`api/services/auth/depends.py:310`) and surfaced
   through `api/routes/superuser.py` (impersonation, workflow-run listing). This is a
   single global flag with no notion of "which org."
2. **Org membership** — `organization_users_association` (`api/db/models.py:45`), a
   plain many-to-many join table with only `user_id` and `organization_id`. Anyone who
   is a member of an org can do anything any other member can do inside that org: build
   workflows, run campaigns, edit integrations, view/adjust billing (once Phase 1 ships
   org-scoped billing reads), invite nobody (there's no invite flow at all — membership
   is only created at signup/org-creation time).

There is **no org-level role**. This is fine for a single-founder-per-org OSS
deployment, but the managed SaaS needs teams: an org owner should be able to bring in
teammates to build/run workflows without handing them the ability to manage billing,
remove other users, or reconfigure integrations.

This spec covers **Phase 4: a minimal two-tier org role (Admin / Member)**, an
authorization layer to enforce it, member-management endpoints, and formalizing the
existing ad hoc `/superuser` surface into a proper platform admin backoffice. It builds
directly on Phase 1's credit ledger/pricing endpoints (gating them by org role) and
prepares the ground for Phase 3's self-serve billing (only Admins should manage
payment methods / top-ups).

### What already exists and is reused (not rebuilt)
- **Auth dependency chain** — `get_user()` (`api/services/auth/depends.py:20`) resolves
  the caller in both auth modes; `get_user_with_selected_organization()`
  (`api/services/auth/depends.py:159`) additionally resolves and validates the caller's
  currently-selected org. Phase 4's org-role dependency wraps this, it does not replace
  it.
- **Dual auth modes** — `AUTH_PROVIDER` env (`api/constants.py:32`) switches between
  Stack Auth (hosted) and local JWT. Org roles must be enforced identically in both;
  role is a property of the membership row, not of the auth provider.
- **Superuser dependency & routes** — `get_superuser` (`api/services/auth/depends.py:310`)
  and `api/routes/superuser.py` (impersonation via Stack Auth, workflow-run listing).
  Phase 4 extends this file/router rather than inventing a parallel admin surface.
- **Org-scoped billing reads/writes from Phase 1** — `api/routes/organization_usage.py`
  (customer-facing balance/ledger read) and the `/superuser/orgs/{org_id}/credits`,
  `/superuser/pricing-rules` endpoints. Phase 4 adds org-role gating to the former and
  keeps the latter under `get_superuser` (platform, not org, admin).
- **Manual tenant isolation** — org-scoped queries already filter by
  `organization_id` throughout the codebase (`api/AGENTS.md`); org-role checks are an
  additional filter on top of, not a replacement for, that isolation.
- **Frontend auth wrappers** — `ui/src/lib/auth/` (session/user context) and the
  existing `ui/src/app/superadmin/*` pages as the pattern to extend.

---

## Goals

1. Every org membership has an explicit **role**: `admin` or `member`.
2. **Admin** can manage everything scoped to the org: workflows, campaigns,
   integrations config, org members (invite/remove/change role), and view/manage
   billing (credits, pricing visibility).
3. **Member** can build and run workflows/campaigns but cannot manage members,
   billing, or integration credentials.
4. Role checks work identically under **both** `AUTH_PROVIDER=local` and
   `AUTH_PROVIDER=stack`.
5. The **platform superuser surface** is formalized into a real backoffice: list orgs
   with balance/usage, grant credits, set pricing (from Phase 1), manage org
   memberships, and impersonate — with impersonation made to work in local mode too.
6. Safe defaults: **existing single-member orgs are backfilled as `admin`** with zero
   behavior change (an org with one member today already implicitly "owns" it).

### Non-goals (deferred)
- Fine-grained per-resource ACLs (e.g., "can edit this workflow but not that one").
- Custom/configurable roles beyond the fixed Admin/Member pair.
- Team hierarchies, groups, or org-to-org sharing.
- SSO/SCIM-driven role provisioning.
- Billing *write* actions for Members even with elevated org role — Phase 3 (Stripe)
  decides whether Members can top up; default here is Admin-only.

---

## Key decisions

| Decision | Choice |
|---|---|
| Role granularity | Exactly two org roles: `admin`, `member`. No custom roles (YAGNI — revisit only if a real customer need appears). |
| Role storage | New `role` column directly on `organization_users_association`, not a separate roles table — the association *is* the membership, and a membership has exactly one role in exactly one org. |
| Default role | `member`. The **first** user in a newly created org (the creator) is `admin`. |
| Superuser vs org-admin | Orthogonal. `is_superuser` is platform-wide and bypasses org-role checks entirely (existing `get_superuser` behavior unchanged); org `admin`/`member` only matters within a single org's boundary. |
| Last-admin guard | An org must always have ≥1 `admin`. Removing/demoting the last admin is rejected at the API layer. |
| Impersonation | Extend to work in local-auth mode by minting a short-lived scoped JWT server-side, instead of relying solely on Stack Auth's impersonation API. |

---

## Data model

### `organization_users_association` (extend) — `api/db/models.py:45`

```
organization_users_association = Table(
    "organization_users",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("organization_id", Integer, ForeignKey("organizations.id"), primary_key=True),
    Column("role", Enum("admin", "member", name="org_role"), nullable=False,
           server_default="member"),
    Column("created_at", DateTime(timezone=True), nullable=False,
           server_default=func.now()),  # new: needed for invite auditing/ordering
)
```

- `role` — the enum lives at the DB level (`org_role`) so invalid values are rejected
  even from ad hoc SQL/scripts, matching the existing `quota_type` enum pattern already
  used on `OrganizationModel`.
- `created_at` — added alongside `role` because member-management UI needs to show
  "member since" and to break ties when picking a fallback admin (oldest member wins;
  see Error handling).
- Because this is currently a plain `Table()` (not a mapped class), reading/writing the
  role means either (a) promoting it to an ORM-mapped association object
  (`OrganizationUserModel` with `user_id`, `organization_id`, `role`, `created_at`,
  `back_populates` on both sides) so it can be queried/updated directly, or (b) using
  SQLAlchemy Core `update()/select()` against the raw table. **Recommendation: promote
  to a mapped association class** — the codebase will need to query "give me my role in
  org X" on every authorized request, and an ORM model is far more ergonomic and
  testable than hand-rolled Core statements sprinkled across routes. This is a
  mechanical refactor (`UserModel.organizations` / `OrganizationModel.users` become
  `secondary=` relationships through the new model via `association_proxy`, or callers
  switch to querying the association model directly) and must preserve every existing
  `.organizations` / `.users` access pattern — an audit of usages is required before
  landing.

### Migration
- Alembic migration adds `role` (enum, `NOT NULL DEFAULT 'member'`) and `created_at`
  (`NOT NULL DEFAULT now()`) to `organization_users`.
- Data backfill (same migration, a data-migration step): for each `organization_id`,
  set `role='admin'` for exactly one row — prefer the org's creator if derivable
  (e.g., the user whose `provider_id`/creation timestamp aligns with the org's
  `created_at`), else the earliest-joined member by `user_id` ordering (best-effort,
  since `created_at` didn't previously exist on this table). All other existing rows
  for that org stay `member`. See **Rollout** for the exact backfill algorithm.

### Role assignment on new memberships
- **Org creation (signup auto-creates org)**: the creating user's association row is
  inserted with `role='admin'` at the same place the org and first membership are
  created today (org bootstrap path invoked from signup / first login).
- **Invite flow (new)**: an Admin explicitly picks a role (`admin` or `member`) when
  inviting; default to `member` in the UI if unspecified.

---

## Authorization layer

### `require_org_role(min_role: Role)` — new dependency, `api/services/auth/depends.py`

Built as a thin wrapper around the existing `get_user_with_selected_organization`
(`api/services/auth/depends.py:159`), which already resolves `(user, organization)` for
the caller's selected org and already 403s if the user isn't a member at all. The new
dependency adds one more check on top:

- Load the caller's `role` for `organization.id` (single indexed lookup on the
  association model / table).
- If `user.is_superuser` — allow unconditionally (platform superuser supersedes org
  roles; matches today's mental model that superuser can do anything).
- Else compare against `min_role`: `admin` requirement rejects `member` callers with
  `403 Forbidden` (`{"detail": "org_admin_required"}`); `member` requirement (the
  default, effectively "any authenticated member") passes for both roles.
- Returns the same `(user, organization, role)` tuple shape so route handlers that
  need to branch on role (e.g., "show extra fields to admins") can do so without a
  second lookup.

Usage pattern in routes:
```
Depends(require_org_role(Role.ADMIN))   # admin-only route
Depends(require_org_role(Role.MEMBER))  # any org member (build/run workflows)
```
`Role.MEMBER` as the floor is functionally equivalent to today's
`get_user_with_selected_organization` and is what most existing routes should be
swapped to for consistency, even though behaviorally nothing changes for them.

A parallel `has_org_role(user, organization, min_role) -> bool` **helper** (not a
FastAPI dependency) is added for the handful of call sites that need a role check
inside business logic rather than at the route boundary (e.g., conditionally including
a "manage" action in a serialized response).

### Route gating

**Admin-only (new `require_org_role(Role.ADMIN)`):**
- Member management: invite, remove, change-role endpoints (new, see below).
- Billing/credits *management* surface exposed to orgs — the read endpoint from Phase 1
  (`api/routes/organization_usage.py`) stays open to Members (visibility, not control)
  but any future org-facing write (e.g., "request more credits", Phase 3 top-up
  initiation) is Admin-only.
- Integration configuration (provider API keys/credentials, webhook config) — wherever
  these currently sit under `get_user_with_selected_organization`, tighten to
  `require_org_role(Role.ADMIN)`.
- Deleting workflows/campaigns (destructive, org-wide-visible actions) — Admin-only;
  creating/editing/running stays Member-accessible.
- Org-level settings (name, selected default configuration, price-per-second display if
  ever editable at org level) — Admin-only.

**Open to Members (`require_org_role(Role.MEMBER)`, i.e. any authenticated member):**
- Create/edit/run workflows.
- Create/launch/monitor campaigns.
- View workflow runs, transcripts, recordings for the org.
- Read-only billing/usage view (balance, current cycle usage) — Members should see
  "why did my call not go through" (insufficient credits) without being able to act on
  it.

An explicit route-by-route audit against `api/routes/*.py` is a required first
implementation step before merging — this spec fixes the *policy*, not the exhaustive
list, since the route inventory will drift between spec-writing and implementation.

---

## Member management

New routes, e.g. `api/routes/organization_members.py`, mounted under
`/organization/members` (co-located conceptually with existing org-scoped routes),
all behind `require_org_role(...)`:

- `GET /organization/members` — `Role.MEMBER` floor (any member can see the roster;
  read-only). Returns `[{user_id, email, role, created_at}]`.
- `POST /organization/members/invite` — `Role.ADMIN`. Body: `{email, role}`.
  - **Local auth mode**: no existing account → create a pending invite record (or, for
    v1 simplicity, an account with `password_hash=None` and an invite/reset token
    emailed out, reusing whatever local password-set flow already exists for new
    accounts) and an association row with the chosen role. Existing local account →
    associate directly (subject to email verification already in place).
  - **Stack mode**: use Stack Auth's invite/add-user API to create or reference the
    Stack user, then create the local `UserModel` (if not already synced) and the
    association row with the chosen role. This mirrors however first-login user sync
    already works today, just adding the role at association-creation time.
  - Idempotent: re-inviting an existing member with a different role updates the role
    (does not error), matching typical "invite" UX expectations.
- `PATCH /organization/members/{user_id}` — `Role.ADMIN`. Body: `{role}`. Changes an
  existing member's role. Subject to the last-admin guard (below).
- `DELETE /organization/members/{user_id}` — `Role.ADMIN`. Removes the association row
  (does not delete the `UserModel` — the user may belong to other orgs). Subject to the
  last-admin guard. A user cannot remove *themselves* via this endpoint if they are the
  last admin, but *can* remove themselves if another admin remains (self-service
  "leave org").

All three mutating endpoints reject acting on a user who isn't currently a member of
the caller's selected org (`404`, not `403`, to avoid leaking membership existence
across orgs) and reject the caller targeting an org other than their selected one (role
is always resolved against `get_user_with_selected_organization`'s org, never a
client-supplied `org_id`, closing an obvious IDOR vector).

---

## Platform admin panel (superuser)

Formalizes and extends `api/routes/superuser.py` (still entirely behind
`get_superuser`, unaffected by org roles) into a real backoffice:

- `GET /superuser/orgs` — list orgs with `credit_balance_cents` (Phase 1), current
  usage-cycle aggregates, member count, and admin(s). Paginated, filterable by name.
- `GET /superuser/orgs/{org_id}` — org detail: members + roles, ledger (Phase 1),
  pricing rules in effect, active configurations.
- `POST /superuser/orgs/{org_id}/credits`, `GET /superuser/orgs/{org_id}/credits`,
  `GET/POST/PATCH /superuser/pricing-rules` — **already specified in Phase 1**, unchanged
  here; Phase 4 just surfaces them in the same backoffice UI as everything else.
- `POST /superuser/orgs/{org_id}/members/{user_id}/role` — superuser override of org
  role (bypasses the last-admin guard only in the sense that superuser can fix a
  broken org with zero admins; still validated to leave the org in a consistent state).
- `POST /superuser/impersonate` — existing endpoint, extended (see below).

### Impersonation across both auth modes

Today `POST /superuser/impersonate` (`api/routes/superuser.py`) is coupled to Stack
Auth's impersonation API (`api/services/auth/stack_auth.py`) — it only works when
`AUTH_PROVIDER=stack`. For local mode, add a parallel code path in the same handler:
when `AUTH_PROVIDER=local`, mint a short-lived (e.g., 15 min) JWT scoped to the target
`user_id` directly from the backend's own JWT signing logic (same signing key/claims
shape as normal local-mode session tokens, plus an `impersonated_by` claim for audit),
returned in the same `ImpersonateResponse{refresh_token, access_token}` shape the
frontend already consumes. This keeps the frontend impersonation flow
(`ui/src/app/impersonate/route.ts`) auth-mode-agnostic — it just receives tokens either
way. All impersonation issuance is logged (superuser id, target user id, org id,
timestamp) regardless of mode, since it's a sensitive capability.

---

## Frontend

- **Org settings → Members page** (new, e.g. `ui/src/app/organization/members/page.tsx`
  or wherever org settings currently live) — Admin-only route. Lists members with role
  badges, invite form (email + role picker), per-row role-change dropdown and remove
  action. Members who navigate here directly (deep link) see a "members only" or
  redirect state rather than the management UI — the page checks role client-side for
  UX but the API is the actual enforcement boundary.
- **Superadmin backoffice** — extends `ui/src/app/superadmin/page.tsx` (org list +
  balances) and adds org-detail and member-management views alongside the existing
  `ui/src/app/superadmin/runs/page.tsx`. `ui/src/app/impersonate/route.ts` needs no
  shape change given the backend keeps the same response contract across auth modes.
- **Role-aware UI gating** — `ui/src/lib/auth/` should expose the caller's current org
  role (piggybacking on whatever already fetches the selected organization) so
  components can conditionally render admin-only actions (delete workflow button,
  billing management links, integration credential forms) without a network round-trip
  per render. This is UX polish, not a security boundary — every gated action still
  re-validates server-side.

---

## Data flow / auth decision points

```
Request arrives with session (Stack or local JWT)
  └─ get_user()                                   [api/services/auth/depends.py:20]
       └─ resolves caller UserModel (is_superuser flag included)

  └─ get_user_with_selected_organization()         [depends.py:159]
       └─ resolves caller's selected OrganizationModel; 403 if not a member at all

  └─ require_org_role(min_role)                    [new]
       ├─ is_superuser? → allow (bypasses org role entirely)
       ├─ lookup role for (user_id, organization.id) on association model
       └─ role >= min_role? → allow : 403 "org_admin_required"

Route handler runs with (user, organization, role) in scope.
```

Superuser routes skip the org-role step entirely — they're gated purely by
`get_superuser` (`depends.py:310`), since they operate cross-org by design.

---

## Error handling & edge cases

- **Last admin cannot be removed or demoted**: `DELETE /organization/members/{user_id}`
  and `PATCH .../role` (to `member`) both check "is target the org's only admin?"
  before applying; if so, `409 Conflict` (`{"detail": "cannot_remove_last_admin"}`).
  Check is done inside a transaction with a row lock on the relevant association rows
  to avoid a race between two concurrent demotions both passing the "not last" check.
- **Role escalation prevention**: a `member` calling `PATCH
  /organization/members/{self}` with `{role: "admin"}` is rejected by
  `require_org_role(Role.ADMIN)` before the handler body runs — Members have no path to
  self-promote. Admins *can* demote themselves (as long as another admin remains),
  which is intentional (e.g., handing off ownership).
- **Cross-tenant access attempts**: every member-management and role-gated endpoint
  resolves the org from the caller's session (`get_user_with_selected_organization`),
  never from a client-supplied `organization_id` path/query param — a Member of org A
  cannot act on org B by guessing an ID, they'd simply be evaluated against org A's
  membership (which they may not even have) and 403/404 accordingly. Superuser routes
  are the sole legitimate cross-org surface and are separately audited/logged.
- **Inviting an email that already belongs to a different account entirely** (local
  mode): reject with a clear conflict rather than silently merging identities — email
  collision handling reuses whatever uniqueness constraint already exists
  (`ix_users_email_lower`, `api/db/models.py`).
- **Org with zero admins** (shouldn't happen post-launch, but possible via a bad manual
  DB edit or backfill edge case): surfaced in the superuser org-detail view as a
  warning; superuser role-override endpoint can repair it directly.
- **Removing the last *member* of an org (leaving it empty)**: out of scope for v1 —
  org deletion/archival isn't part of this phase; the last admin leaving is simply
  disallowed by the guard above, so an org can never reach zero members through normal
  API use.

---

## Testing strategy

Tests run against the test DB via `api/.env.test` per `AGENTS.md`.

**Unit**
- `require_org_role`: superuser bypass; admin passes both floors; member passes
  `Role.MEMBER` floor and fails `Role.ADMIN` floor; non-member (no association row at
  all) fails both (delegated to `get_user_with_selected_organization`'s existing 403).
- Last-admin guard: single-admin org rejects demote/remove of that admin; multi-admin
  org allows either.
- Role assignment on org creation: creator's association row is `admin`.
- Invite idempotency: re-inviting an existing member updates role rather than erroring
  or duplicating the association row.

**Integration**
- Member calling an admin-gated route (member invite, role change, remove, integration
  config, workflow delete) → `403`.
- Admin calling the same routes → succeeds, and the resulting association/role state
  is correct.
- Both flows repeated under `AUTH_PROVIDER=local` and `AUTH_PROVIDER=stack` (the auth
  fixture already parametrizes on this per existing test patterns; role checks must be
  proven mode-agnostic, not just tested once).
- Cross-tenant: a member of org A hitting an org-scoped route while org B has the
  target resource → `403`/`404`, never a state leak.
- Impersonation: local-mode mint-a-JWT path produces a token that
  `get_user()` correctly resolves to the target user, with the audit log entry
  recorded; Stack-mode path unchanged from today's behavior.

**Admin backoffice**
- `GET /superuser/orgs` returns balance/usage/member data consistent with Phase 1's
  ledger and this phase's roles.
- Superuser role-override repairs a zero-admin org.

---

## Rollout

1. Ship the `role`/`created_at` migration with the backfill data-migration in the same
   deploy (default `member`, then promote exactly one admin per org):
   - Prefer the org's earliest member if orderable (best-effort from existing data);
     if genuinely ambiguous (e.g., no reliable ordering signal for pre-existing rows),
     promote **all** current members of orgs with ≤2 members to `admin` rather than
     guess wrong and lock someone out — false-positive admin access is a far smaller
     risk than false-negative (locking a real user out of managing their own org) at
     this stage of the product (few active orgs). Re-tighten manually via the
     superuser backoffice for any org where this default over-grants.
2. Add `require_org_role` and swap existing `get_user_with_selected_organization`
   dependents route-by-route to the appropriate floor (`Role.MEMBER` is a no-op change
   for most; `Role.ADMIN` is the actual tightening) — ship as a series of small, safe
   PRs per route group (billing, integrations, members, workflow-delete) rather than
   one big-bang cutover, so any misclassification is easy to isolate and revert.
3. Ship member-management endpoints + frontend Members page.
4. Extend `/superuser` with org-list/detail and the local-mode impersonation path.
5. Announce roles to existing customers (who is currently admin) before any
   member-invite marketing push, so orgs aren't surprised by who already has admin.

---

## Open questions deferred

- Should Members be allowed to initiate Stripe top-ups (Phase 3) or is that strictly
  Admin-only? Leaning Admin-only by default per this spec; Phase 3 can revisit if
  customers push back.
- Do we need an `owner` tier above `admin` (single, non-removable, e.g. for billing
  legal/contract purposes) distinct from ordinary admins? Deferred until multi-admin
  orgs are common enough to need it — two-tier is intentionally the simplest thing
  that could work.
- Audit log for role changes / member removal beyond basic ledger-style logging —
  revisit alongside broader observability work if compliance requirements emerge.
- SSO/SCIM-driven automatic role assignment for enterprise customers — out of scope
  until there's a concrete enterprise deployment requiring it.
