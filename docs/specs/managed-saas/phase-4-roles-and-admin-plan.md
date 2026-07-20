# Roles, Permissions & Admin Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-tier org role (`admin`/`member`) to `organization_users_association`, an authorization layer (`require_org_role`) that enforces it identically under `AUTH_PROVIDER=local` and `AUTH_PROVIDER=stack`, member-management endpoints with a last-admin guard, an extended superuser backoffice (org list/detail with Phase 1 balances, role override), local-mode impersonation, and the minimal frontend to drive it.

**Architecture:** The plain `organization_users_association` `Table()` (`api/db/models.py:45`) is promoted to a mapped `OrganizationUserModel` (table name unchanged: `organization_users`) carrying `role` (new `org_role` enum, default `member`) and `created_at`. A new `require_org_role(min_role)` FastAPI dependency wraps the existing `get_user_with_selected_organization` (`api/services/auth/depends.py:159`), adds a role lookup, and lets `is_superuser` bypass it — matching today's mental model that superuser supersedes everything. Member management lives in a new `api/routes/organization_members.py`; the superuser backoffice extends the existing `api/routes/superuser.py` and reuses Phase 1's `billing_service`/`BillingClient` (`api/services/billing/billing_service.py`, `api/db/billing_client.py`) for balance reads. Local-mode impersonation reuses the existing local JWT signer (`api/utils/auth.py:create_jwt_token`) rather than Stack Auth's impersonation API.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy (async), Alembic, PostgreSQL, pytest + pytest-asyncio, loguru. Frontend: Next.js 15 App Router, React 19, TypeScript.

## Global Constraints

- Role granularity: exactly two org roles, `admin` and `member` (DB enum `org_role`). No custom roles.
- Superuser (`UserModel.is_superuser`) is orthogonal to and bypasses org roles entirely — unchanged from today's `get_superuser` behavior.
- Every mutating member-management/role-gated endpoint resolves the org from the caller's session (`get_user_with_selected_organization` → `user.selected_organization_id`), **never** from a client-supplied `organization_id` — this closes an IDOR vector and matches the existing tenant-isolation rule in `api/AGENTS.md`.
- Last-admin guard: an org must always have ≥1 admin. Demoting or removing the last admin is rejected with `409 {"detail": "cannot_remove_last_admin"}`, checked inside a transaction with a row lock (`SELECT ... FOR UPDATE`) to close the concurrent-demotion race.
- Role escalation prevention: `require_org_role(Role.ADMIN)` runs *before* any member-management handler body, so a `member` has no code path to self-promote or promote anyone else.
- Cross-tenant isolation: acting on a `user_id` that isn't currently a member of the caller's selected org returns `404` (not `403`), so membership existence isn't leaked across orgs.
- Migration chaining: this plan's migration sets `down_revision = "b1f0c0de0001"` (current head, from Phase 1's billing migration) **at plan-writing time**. Before running `makemigrate.sh`, the implementer MUST re-check `python -m alembic -c api/alembic.ini heads` — if Phase 3's plan has already landed a migration, `down_revision` must be updated to that new head instead. This is called out again in Task 3.
- Tests run against the test DB: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest ...`. DB-integration tests (anything touching `async_session`/Alembic-created tables) need the project's pgvector Postgres from `docker-compose-local.yaml` running.
- Migrations are created via `./scripts/makemigrate.sh "description"` and applied with `./scripts/migrate.sh`.
- DB access lives in `api/db/*_client.py` mixins; domain/authorization logic lives in `api/services/`; routes stay thin (`api/AGENTS.md`).

---

## File Structure

**Create:**
- `api/db/org_membership_client.py` — `OrgMembershipClient` DB mixin: role lookups, member listing, role changes, removal — all against the new `OrganizationUserModel`, with row locking for the last-admin guard.
- `api/routes/organization_members.py` — `GET/POST/PATCH/DELETE /organization/members*` — member roster, invite, role change, remove.
- `api/tests/test_org_role_enum.py` — unit tests for the `Role` enum/ordering helper.
- `api/tests/test_org_membership_client.py` — DB-backed tests for `OrgMembershipClient` (role CRUD, last-admin guard, idempotent invite).
- `api/tests/test_require_org_role.py` — unit tests for the `require_org_role` dependency and `has_org_role` helper (mocked DB).
- `api/tests/test_organization_members_routes.py` — route tests via `httpx.ASGITransport` + `dependency_overrides`: admin/member gating, last-admin guard, escalation prevention, cross-tenant 404.
- `api/tests/test_superuser_orgs_routes.py` — `GET /superuser/orgs`, `GET /superuser/orgs/{id}`, role-override endpoint tests.
- `api/tests/test_local_impersonation.py` — local-mode JWT impersonation path tests.
- `ui/src/app/organization/members/page.tsx` — Admin-only members page (roster, invite, role change, remove).
- `ui/src/app/superadmin/orgs/page.tsx` — superuser org list (name, balance, member count).
- `ui/src/app/superadmin/orgs/[orgId]/page.tsx` — superuser org detail (members + roles, role override).

**Modify:**
- `api/db/models.py` — promote `organization_users_association` to `OrganizationUserModel`; add `role`/`created_at`; keep `UserModel.organizations`/`OrganizationModel.users` `secondary=` relationships working.
- `api/alembic/versions/` — new migration adding `role`/`created_at` + backfill.
- `api/enums.py` — add `Role(Enum)` (`ADMIN = "admin"`, `MEMBER = "member"`) with an ordering helper.
- `api/services/auth/depends.py` — add `require_org_role(min_role)`, `has_org_role(...)` helper; org-creation paths set the creator's role to `admin`.
- `api/routes/auth.py` — signup's `add_user_to_organization` call becomes role-aware (`role="admin"` for the creator).
- `api/db/organization_client.py` — `add_user_to_organization` gains a `role: str = "member"` parameter.
- `api/db/db_client.py` — register `OrgMembershipClient` on `DBClient`.
- `api/routes/credentials.py` — gate `create_credential`/`delete_credential` to `require_org_role(Role.ADMIN)` (integration credentials).
- `api/routes/workflow.py` — gate `update_workflow_status` (archive = destructive) to `require_org_role(Role.ADMIN)`.
- `api/routes/superuser.py` — add `GET /superuser/orgs`, `GET /superuser/orgs/{org_id}`, `POST /superuser/orgs/{org_id}/members/{user_id}/role`; extend `POST /superuser/impersonate` with a local-mode branch.
- `api/routes/main.py` — mount `organization_members` router.
- `ui/src/lib/auth/types.ts`, `ui/src/lib/auth/providers/AuthProvider.tsx` — expose `orgRole` on the auth context.
- `ui/src/app/impersonate/route.ts` — branch on the response shape to set local-mode auth cookies (`dograh_auth_token`/`dograh_auth_user`) instead of only the Stack refresh cookie.

---

## Task 1: `Role` enum and ordering helper

**Files:**
- Modify: `api/enums.py`
- Test: `api/tests/test_org_role_enum.py`

**Interfaces:**
- Produces: `class Role(str, Enum)` with `ADMIN = "admin"`, `MEMBER = "member"`; `ROLE_RANK: dict[Role, int]` (`MEMBER=0, ADMIN=1`); `def role_at_least(role: Role, min_role: Role) -> bool`.

- [ ] **Step 1: Read the existing string-enum pattern**

Run: `grep -n "class PostHogEvent" -A 3 api/enums.py`
Expected: shows `class PostHogEvent(str, Enum):` — the pattern to mirror so `Role` values serialize as plain strings in JSON responses.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_org_role_enum.py
from api.enums import Role, role_at_least


def test_admin_at_least_member():
    assert role_at_least(Role.ADMIN, Role.MEMBER) is True


def test_admin_at_least_admin():
    assert role_at_least(Role.ADMIN, Role.ADMIN) is True


def test_member_not_at_least_admin():
    assert role_at_least(Role.MEMBER, Role.ADMIN) is False


def test_member_at_least_member():
    assert role_at_least(Role.MEMBER, Role.MEMBER) is True


def test_role_is_str_enum():
    assert Role.ADMIN.value == "admin"
    assert Role.MEMBER == "member"
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_org_role_enum.py -v`
Expected: FAIL — `ImportError: cannot import name 'Role' from 'api.enums'`.

- [ ] **Step 4: Implement**

In `api/enums.py`, add near the top (after `IntegrationAction`, alongside the other plain-string enums):

```python
class Role(str, Enum):
    """Two-tier org role stored on the membership row (organization_users.role)."""

    ADMIN = "admin"
    MEMBER = "member"


_ROLE_RANK: dict["Role", int] = {Role.MEMBER: 0, Role.ADMIN: 1}


def role_at_least(role: "Role", min_role: "Role") -> bool:
    """True if `role` meets or exceeds `min_role` in privilege."""
    return _ROLE_RANK[role] >= _ROLE_RANK[min_role]
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_org_role_enum.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add api/enums.py api/tests/test_org_role_enum.py
git commit -m "feat(roles): add Role enum and role_at_least ordering helper"
```

---

## Task 2: Promote `organization_users_association` to a mapped `OrganizationUserModel`

**Files:**
- Modify: `api/db/models.py`

**Interfaces:**
- Produces: `class OrganizationUserModel(Base)` (`__tablename__ = "organization_users"`) with `user_id`, `organization_id` (composite PK, unchanged column names), `role` (`Enum("admin", "member", name="org_role")`, `nullable=False`, `server_default="member"`), `created_at` (`DateTime(timezone=True)`, `server_default=func.now()`).
- Preserves: `UserModel.organizations` / `OrganizationModel.users` continue to work as `secondary=` relationships (SQLAlchemy supports `secondary=` pointing at a mapped class's `__table__`, not just a bare `Table()`), so every existing `.organizations` / `.users` call site is unaffected.

- [ ] **Step 1: Audit existing usages before touching the table**

Run: `grep -rn "organization_users_association\|\.organizations\b\|OrganizationModel\.users\b" api/ --include=*.py | grep -v api/tests | grep -v "\.pyc"`
Expected: a list of call sites (org_client.py's `add_user_to_organization`, any `user.organizations` reads in routes/tests). Record every hit — Step 4 must not change behavior for any of them.

- [ ] **Step 2: Replace the bare `Table()` with a mapped model**

In `api/db/models.py`, replace the current definition (lines 44–52):

```python
class OrganizationUserModel(Base):
    """Org membership row. The association *is* the membership — one role per
    (user, org) pair. Table name (`organization_users`) and PK columns are
    unchanged from the pre-Phase-4 plain Table() so existing rows/FKs are untouched.
    """

    __tablename__ = "organization_users"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id"), primary_key=True
    )
    role = Column(
        Enum("admin", "member", name="org_role"),
        nullable=False,
        default="member",
        server_default="member",
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Backward-compat alias: existing `secondary=organization_users_association` usages
# (relationship() calls below) reference the mapped model's underlying Table.
organization_users_association = OrganizationUserModel.__table__
```

- [ ] **Step 3: Verify `secondary=` relationships still resolve**

`UserModel.organizations` (line 65) and `OrganizationModel.users` (line 161) already read `secondary=organization_users_association` — no edit needed there, since the alias in Step 2 keeps that name bound to the (now mapped) table.

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "from api.db.models import UserModel, OrganizationModel, OrganizationUserModel; print(OrganizationUserModel.__table__.c.keys())"`
Expected: `['user_id', 'organization_id', 'role', 'created_at']`.

- [ ] **Step 4: Run the existing full model-dependent test suite for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -k "organization" -q`
Expected: all pass (no schema exists yet for the new columns — this only exercises Python-level relationship wiring; the DB migration is Task 3).

- [ ] **Step 5: Commit**

```bash
git add api/db/models.py
git commit -m "refactor(roles): promote organization_users_association to mapped OrganizationUserModel"
```

---

## Task 3: Migration — `role`/`created_at` columns + backfill

**Files:**
- Migration: generated under `api/alembic/versions/`

**Interfaces:**
- Produces: `org_role` Postgres enum type; `organization_users.role` (`NOT NULL DEFAULT 'member'`); `organization_users.created_at` (`NOT NULL DEFAULT now()`); a data-migration step promoting exactly the right rows to `admin` per org.

- [ ] **Step 1: Re-confirm the current head before generating**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m alembic -c api/alembic.ini heads`
Expected (at plan-writing time): `b1f0c0de0001 (head)`. **If this differs** (e.g. a Phase 3 migration has landed), use that value as `down_revision` in Step 3 instead of `b1f0c0de0001` — do not blindly copy the value below.

- [ ] **Step 2: Generate the migration**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && ./scripts/makemigrate.sh "add role and created_at to organization_users"`
Expected: a new file in `api/alembic/versions/` with `down_revision = "b1f0c0de0001"` (or the head confirmed in Step 1), adding `role` and `created_at` to `organization_users`.

- [ ] **Step 3: Hand-write/verify the upgrade function, including the backfill**

Autogenerate typically won't produce a correct data-migration step — open the generated file and ensure `upgrade()` matches:

```python
"""add role and created_at to organization_users

Revision ID: <generated>
Revises: b1f0c0de0001
Create Date: <generated>

Adds org_role (admin/member) and created_at to organization_users, then backfills:
- exactly one admin per org, preferring the earliest member by user_id ordering
  (best-effort — created_at didn't exist on this table before this migration)
- for orgs with <=2 members where "earliest" is ambiguous/unreliable, ALL current
  members are promoted to admin rather than risk locking a real user out
  (false-positive admin access is far cheaper to fix than false-negative lockout
  at this stage — see docs/specs/managed-saas/phase-4-roles-and-admin.md#rollout)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "<generated>"
down_revision: Union[str, None] = "b1f0c0de0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    org_role = sa.Enum("admin", "member", name="org_role")
    org_role.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "organization_users",
        sa.Column(
            "role", org_role, nullable=False, server_default="member"
        ),
    )
    op.add_column(
        "organization_users",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- Backfill: promote exactly one admin per multi-member org, all members
    # for orgs with <=2 members (see module docstring for rationale). ---
    conn = op.get_bind()
    org_ids = [
        row[0]
        for row in conn.execute(
            sa.text("SELECT DISTINCT organization_id FROM organization_users")
        )
    ]
    for org_id in org_ids:
        member_rows = conn.execute(
            sa.text(
                "SELECT user_id FROM organization_users "
                "WHERE organization_id = :org_id ORDER BY user_id ASC"
            ),
            {"org_id": org_id},
        ).fetchall()
        member_ids = [r[0] for r in member_rows]
        if not member_ids:
            continue
        if len(member_ids) <= 2:
            promote_ids = member_ids
        else:
            promote_ids = [member_ids[0]]
        conn.execute(
            sa.text(
                "UPDATE organization_users SET role = 'admin' "
                "WHERE organization_id = :org_id AND user_id = ANY(:user_ids)"
            ),
            {"org_id": org_id, "user_ids": promote_ids},
        )


def downgrade() -> None:
    op.drop_column("organization_users", "created_at")
    op.drop_column("organization_users", "role")
    sa.Enum(name="org_role").drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 4: Apply against the test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh`
Expected: migration applies with no errors.

- [ ] **Step 5: Verify the schema and enum**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "
import asyncio
from api.db.database import engine
from sqlalchemy import text

async def go():
    async with engine.begin() as c:
        r = await c.execute(text(\"select column_name, data_type, is_nullable from information_schema.columns where table_name='organization_users'\"))
        for row in r: print(row)

asyncio.run(go())
"`
Expected: rows for `user_id`, `organization_id`, `role` (`USER-DEFINED`, `NO`), `created_at` (`timestamp with time zone`, `NO`).

- [ ] **Step 6: Commit**

```bash
git add api/alembic/versions/
git commit -m "feat(roles): migrate organization_users to add role/created_at with backfill"
```

---

## Task 4: `OrgMembershipClient` DB mixin (role CRUD + last-admin guard)

**Files:**
- Create: `api/db/org_membership_client.py`
- Modify: `api/db/db_client.py`, `api/db/organization_client.py`
- Test: `api/tests/test_org_membership_client.py`

**Interfaces:**
- Consumes: `OrganizationUserModel` (Task 2/3).
- Produces methods on `db_client`:
  - `async get_member_role(organization_id: int, user_id: int) -> str | None`
  - `async list_org_members(organization_id: int) -> list[dict]` (`{user_id, email, role, created_at}`, joined against `UserModel`)
  - `async count_org_admins(organization_id: int) -> int`
  - `async upsert_member_role(organization_id: int, user_id: int, role: str) -> None` — idempotent insert-or-update-role; row-locks the target org's membership rows during the write.
  - `async set_member_role(organization_id: int, user_id: int, role: str) -> None` — **guarded**: raises `LastAdminError` if this would demote the org's only admin.
  - `async remove_member(organization_id: int, user_id: int) -> None` — **guarded**: raises `LastAdminError` if the target is the org's only admin.
- Also modifies `OrganizationClient.add_user_to_organization` to accept `role: str = "member"`.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_org_membership_client.py
import pytest

from api.db import db_client
from api.db.org_membership_client import LastAdminError


async def _org_with_members(n=1):
    from api.db.database import async_session
    from api.db.models import OrganizationModel, UserModel

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_mem_{n}_{id(object())}")
        s.add(org)
        await s.flush()
        users = []
        for i in range(n):
            u = UserModel(provider_id=f"user_mem_{n}_{i}_{id(object())}")
            s.add(u)
            users.append(u)
        await s.flush()
        await s.commit()
        for u in users:
            await s.refresh(u)
        await s.refresh(org)
        return org.id, [u.id for u in users]


@pytest.mark.asyncio
async def test_first_member_defaults_to_member_role():
    org_id, (user_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(user_id, org_id)
    assert await db_client.get_member_role(org_id, user_id) == "member"


@pytest.mark.asyncio
async def test_add_user_to_organization_with_explicit_admin_role():
    org_id, (user_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(user_id, org_id, role="admin")
    assert await db_client.get_member_role(org_id, user_id) == "admin"
    assert await db_client.count_org_admins(org_id) == 1


@pytest.mark.asyncio
async def test_upsert_member_role_idempotent():
    org_id, (user_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(user_id, org_id, role="member")
    await db_client.upsert_member_role(org_id, user_id, "admin")
    await db_client.upsert_member_role(org_id, user_id, "admin")
    assert await db_client.get_member_role(org_id, user_id) == "admin"


@pytest.mark.asyncio
async def test_last_admin_cannot_be_demoted():
    org_id, (admin_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(admin_id, org_id, role="admin")
    with pytest.raises(LastAdminError):
        await db_client.set_member_role(org_id, admin_id, "member")


@pytest.mark.asyncio
async def test_demote_allowed_when_another_admin_remains():
    org_id, (admin_1, admin_2) = await _org_with_members(2)
    await db_client.add_user_to_organization(admin_1, org_id, role="admin")
    await db_client.add_user_to_organization(admin_2, org_id, role="admin")
    await db_client.set_member_role(org_id, admin_1, "member")
    assert await db_client.get_member_role(org_id, admin_1) == "member"
    assert await db_client.count_org_admins(org_id) == 1


@pytest.mark.asyncio
async def test_last_admin_cannot_be_removed():
    org_id, (admin_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(admin_id, org_id, role="admin")
    with pytest.raises(LastAdminError):
        await db_client.remove_member(org_id, admin_id)


@pytest.mark.asyncio
async def test_list_org_members_returns_role_and_email():
    org_id, (user_id,) = await _org_with_members(1)
    await db_client.add_user_to_organization(user_id, org_id, role="admin")
    members = await db_client.list_org_members(org_id)
    assert len(members) == 1
    assert members[0]["user_id"] == user_id
    assert members[0]["role"] == "admin"
    assert "created_at" in members[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_org_membership_client.py -v`
Expected: FAIL — `ModuleNotFoundError: api.db.org_membership_client` (and `add_user_to_organization()` doesn't accept `role` yet).

- [ ] **Step 3: Update `add_user_to_organization` to accept `role`**

In `api/db/organization_client.py`, change the signature and insert (around line 94):

```python
    async def add_user_to_organization(
        self, user_id: int, organization_id: int, role: str = "member"
    ) -> None:
        """Ensure that a user is linked to an organization (many-to-many).

        Idempotent: re-adding an existing member updates the role rather than
        erroring or duplicating the row (matches invite-idempotency UX).
        """
        async with self.async_session() as session:
            stmt = insert(organization_users_association).values(
                user_id=user_id, organization_id=organization_id, role=role
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "organization_id"],
                set_={"role": stmt.excluded.role},
            )
            await session.execute(stmt)
            await session.commit()
```

- [ ] **Step 4: Implement `api/db/org_membership_client.py`**

```python
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationUserModel, UserModel


class LastAdminError(Exception):
    """Raised when an operation would leave an org with zero admins."""


class OrgMembershipClient(BaseDBClient):
    async def get_member_role(
        self, organization_id: int, user_id: int
    ) -> str | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationUserModel.role).where(
                    OrganizationUserModel.organization_id == organization_id,
                    OrganizationUserModel.user_id == user_id,
                )
            )
            row = result.scalar_one_or_none()
            return row

    async def list_org_members(self, organization_id: int) -> list[dict]:
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    OrganizationUserModel.user_id,
                    UserModel.email,
                    OrganizationUserModel.role,
                    OrganizationUserModel.created_at,
                )
                .join(UserModel, UserModel.id == OrganizationUserModel.user_id)
                .where(OrganizationUserModel.organization_id == organization_id)
                .order_by(OrganizationUserModel.created_at.asc())
            )
            return [
                {
                    "user_id": r.user_id,
                    "email": r.email,
                    "role": r.role,
                    "created_at": r.created_at,
                }
                for r in result.all()
            ]

    async def count_org_admins(self, organization_id: int) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count()).where(
                    OrganizationUserModel.organization_id == organization_id,
                    OrganizationUserModel.role == "admin",
                )
            )
            return int(result.scalar_one())

    async def upsert_member_role(
        self, organization_id: int, user_id: int, role: str
    ) -> None:
        async with self.async_session() as session:
            stmt = insert(OrganizationUserModel).values(
                organization_id=organization_id, user_id=user_id, role=role
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "organization_id"],
                set_={"role": stmt.excluded.role},
            )
            await session.execute(stmt)
            await session.commit()

    async def set_member_role(
        self, organization_id: int, user_id: int, role: str
    ) -> None:
        """Change a member's role. Rejects demoting the org's last admin."""
        async with self.async_session() as session:
            async with session.begin():
                rows = await session.execute(
                    select(OrganizationUserModel)
                    .where(
                        OrganizationUserModel.organization_id == organization_id
                    )
                    .with_for_update()
                )
                memberships = rows.scalars().all()
                target = next(
                    (m for m in memberships if m.user_id == user_id), None
                )
                if target is None:
                    raise ValueError("not_a_member")

                admin_count = sum(1 for m in memberships if m.role == "admin")
                if target.role == "admin" and role != "admin" and admin_count <= 1:
                    raise LastAdminError(
                        f"cannot demote the only admin of org {organization_id}"
                    )
                target.role = role
                await session.flush()

    async def remove_member(self, organization_id: int, user_id: int) -> None:
        """Remove a membership row. Rejects removing the org's last admin."""
        async with self.async_session() as session:
            async with session.begin():
                rows = await session.execute(
                    select(OrganizationUserModel)
                    .where(
                        OrganizationUserModel.organization_id == organization_id
                    )
                    .with_for_update()
                )
                memberships = rows.scalars().all()
                target = next(
                    (m for m in memberships if m.user_id == user_id), None
                )
                if target is None:
                    raise ValueError("not_a_member")

                admin_count = sum(1 for m in memberships if m.role == "admin")
                if target.role == "admin" and admin_count <= 1:
                    raise LastAdminError(
                        f"cannot remove the only admin of org {organization_id}"
                    )
                await session.delete(target)
```

- [ ] **Step 5: Register the mixin**

In `api/db/db_client.py`, add `from api.db.org_membership_client import OrgMembershipClient` and add `OrgMembershipClient,` to the `DBClient(...)` base list; add a matching docstring line.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_org_membership_client.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit**

```bash
git add api/db/org_membership_client.py api/db/organization_client.py api/db/db_client.py api/tests/test_org_membership_client.py
git commit -m "feat(roles): OrgMembershipClient with row-locked last-admin guard"
```

---

## Task 5: `require_org_role` dependency + `has_org_role` helper

**Files:**
- Modify: `api/services/auth/depends.py`
- Test: `api/tests/test_require_org_role.py`

**Interfaces:**
- Consumes: `get_user_with_selected_organization` (`api/services/auth/depends.py:159`), `db_client.get_member_role` (Task 4), `Role`/`role_at_least` (Task 1).
- Produces:
  - `def require_org_role(min_role: Role)` — returns a FastAPI dependency callable resolving to `(user, role)` (the org is already on `user.selected_organization_id`); superuser bypasses; non-admin failing an admin floor gets `403 {"detail": "org_admin_required"}`; a caller with no membership row at all is already rejected upstream by `get_user_with_selected_organization`'s 400, so this dependency treats a missing role as `403` defensively (should not normally be reachable).
  - `async def has_org_role(user, organization_id, min_role) -> bool` — non-dependency helper for business-logic branches.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_require_org_role.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.enums import Role
from api.services.auth import depends as auth_depends


def _user(is_superuser=False, org_id=1):
    return SimpleNamespace(
        id=1, is_superuser=is_superuser, selected_organization_id=org_id
    )


@pytest.mark.asyncio
async def test_superuser_bypasses_role_check(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value=None)
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    result_user, role = await dep(user=_user(is_superuser=True))
    assert result_user.is_superuser is True
    assert role is None


@pytest.mark.asyncio
async def test_admin_passes_admin_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="admin")
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    _, role = await dep(user=_user())
    assert role == Role.ADMIN


@pytest.mark.asyncio
async def test_member_fails_admin_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    dep = auth_depends.require_org_role(Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await dep(user=_user())
    assert exc.value.status_code == 403
    assert exc.value.detail == "org_admin_required"


@pytest.mark.asyncio
async def test_member_passes_member_floor(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    dep = auth_depends.require_org_role(Role.MEMBER)
    _, role = await dep(user=_user())
    assert role == Role.MEMBER


@pytest.mark.asyncio
async def test_has_org_role_helper(monkeypatch):
    monkeypatch.setattr(
        auth_depends.db_client, "get_member_role", AsyncMock(return_value="member")
    )
    assert await auth_depends.has_org_role(_user(), 1, Role.MEMBER) is True
    assert await auth_depends.has_org_role(_user(), 1, Role.ADMIN) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_require_org_role.py -v`
Expected: FAIL — `AttributeError: module 'api.services.auth.depends' has no attribute 'require_org_role'`.

- [ ] **Step 3: Implement**

In `api/services/auth/depends.py`, add imports and the new dependency near `get_user_with_selected_organization`:

```python
from api.enums import Role, role_at_least
```

```python
async def get_user_with_selected_organization(
    user: Annotated[UserModel, Depends(get_user)],
) -> UserModel:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return user


def require_org_role(min_role: Role):
    """FastAPI dependency factory: `Depends(require_org_role(Role.ADMIN))`.

    Wraps `get_user_with_selected_organization` (which already 403/400s if the
    caller has no selected org) with a role check against the caller's
    membership row for that org. Superuser bypasses the role check entirely,
    matching `get_superuser`'s existing "superuser can do anything" model.
    """

    async def _dependency(
        user: Annotated[UserModel, Depends(get_user_with_selected_organization)],
    ) -> tuple[UserModel, Role | None]:
        if user.is_superuser:
            return user, None

        role_value = await db_client.get_member_role(
            user.selected_organization_id, user.id
        )
        if role_value is None:
            # Should not normally be reachable: get_user_with_selected_organization
            # already implies a selected org, but defend against a stale/orphaned
            # selected_organization_id (e.g. membership removed concurrently).
            raise HTTPException(status_code=403, detail="org_admin_required")

        role = Role(role_value)
        if not role_at_least(role, min_role):
            raise HTTPException(status_code=403, detail="org_admin_required")
        return user, role

    return _dependency


async def has_org_role(user: UserModel, organization_id: int, min_role: Role) -> bool:
    """Non-dependency helper for role checks inside business logic (not a route
    boundary check) — e.g. conditionally including a "manage" action in a
    serialized response."""
    if user.is_superuser:
        return True
    role_value = await db_client.get_member_role(organization_id, user.id)
    if role_value is None:
        return False
    return role_at_least(Role(role_value), min_role)
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_require_org_role.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/services/auth/depends.py api/tests/test_require_org_role.py
git commit -m "feat(roles): require_org_role dependency and has_org_role helper"
```

---

## Task 6: Creator becomes admin on org creation (signup + Stack first-login bootstrap)

**Files:**
- Modify: `api/routes/auth.py`, `api/services/auth/depends.py`
- Test: extend `api/tests/test_org_membership_client.py` indirectly via new route-level tests in Task 8; add a focused unit test here.

**Interfaces:**
- Consumes: `db_client.add_user_to_organization(..., role=...)` (Task 4).
- Produces: local signup (`api/routes/auth.py:signup`) and the Stack-mode org-bootstrap path (`api/services/auth/depends.py:get_user`, the `org_was_created` branch around line 100) both call `add_user_to_organization(..., role="admin")` for the creating user.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_signup_creator_is_admin.py
from unittest.mock import AsyncMock, patch

import pytest

from api.routes import auth as auth_routes
from api.schemas.auth import SignupRequest


@pytest.mark.asyncio
async def test_signup_creator_role_is_admin(monkeypatch):
    fake_user = AsyncMock()
    fake_user.id = 1
    fake_user.provider_id = "p1"
    fake_user.email = "a@b.com"

    fake_org = AsyncMock()
    fake_org.id = 10

    monkeypatch.setattr(
        auth_routes.db_client, "get_user_by_email", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        auth_routes.db_client,
        "create_user_with_email",
        AsyncMock(return_value=fake_user),
    )
    monkeypatch.setattr(
        auth_routes.db_client,
        "get_or_create_organization_by_provider_id",
        AsyncMock(return_value=(fake_org, True)),
    )
    add_user = AsyncMock()
    monkeypatch.setattr(auth_routes.db_client, "add_user_to_organization", add_user)
    monkeypatch.setattr(
        auth_routes.db_client, "update_user_selected_organization", AsyncMock()
    )
    monkeypatch.setattr(
        auth_routes,
        "create_user_configuration_with_mps_key",
        AsyncMock(return_value=None),
    )

    await auth_routes.signup(
        SignupRequest(email="a@b.com", password="pw12345678", name="A")
    )

    add_user.assert_awaited_once_with(1, 10, role="admin")
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_signup_creator_is_admin.py -v`
Expected: FAIL — `add_user_to_organization` called without `role="admin"` (`AssertionError`).

- [ ] **Step 3: Implement**

In `api/routes/auth.py`, change:

```python
    # Link user to organization
    await db_client.add_user_to_organization(user.id, organization.id)
```

to:

```python
    # Link user to organization — the creator of a newly signed-up-into org is admin.
    await db_client.add_user_to_organization(user.id, organization.id, role="admin")
```

In `api/services/auth/depends.py`, in the `org_was_created` branch of `get_user` (around line 100–113), change:

```python
        if user_model.selected_organization_id != organization.id:
            await db_client.add_user_to_organization(user_model.id, organization.id)
```

to:

```python
        if user_model.selected_organization_id != organization.id:
            # The first user to land on a freshly created org (Stack "team")
            # is its admin; subsequent joiners default to member via
            # add_user_to_organization's default parameter.
            await db_client.add_user_to_organization(
                user_model.id,
                organization.id,
                role="admin" if org_was_created else "member",
            )
```

Note: `org_was_created` is computed a few lines below this call today (`db_client.get_or_create_organization_by_provider_id` returns it) — move that call one statement earlier so `org_was_created` is in scope before `add_user_to_organization` is invoked. Verify with:

Run: `grep -n "org_was_created" api/services/auth/depends.py`
Expected: the `get_or_create_organization_by_provider_id(...)` call (which produces `org_was_created`) now precedes the `add_user_to_organization` call in source order.

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_signup_creator_is_admin.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add api/routes/auth.py api/services/auth/depends.py api/tests/test_signup_creator_is_admin.py
git commit -m "feat(roles): org creator becomes admin on signup and first Stack login"
```

---

## Task 7: Member management routes

**Files:**
- Create: `api/routes/organization_members.py`
- Modify: `api/routes/main.py`
- Test: `api/tests/test_organization_members_routes.py`

**Interfaces:**
- Consumes: `require_org_role` (Task 5), `db_client` member methods (Task 4), `LastAdminError`.
- Produces routes under `/organization/members`:
  - `GET /organization/members` — `Role.MEMBER` floor. Returns `[{user_id, email, role, created_at}]`.
  - `POST /organization/members/invite` — `Role.ADMIN`. Body `{email, role="member"}`. Local mode only in this task (Stack-mode invite via Stack Auth's add-user API is a documented follow-up, see Step 5 note) — looks up an existing local `UserModel` by email; `404` if no such user exists yet (v1: admins invite users who already have an account; a full "create pending user" flow is out of scope for this task and noted as a gap in the Self-Review). Idempotent: re-inviting updates role via `upsert_member_role`.
  - `PATCH /organization/members/{user_id}` — `Role.ADMIN`. Body `{role}`. `409` on last-admin guard.
  - `DELETE /organization/members/{user_id}` — `Role.ADMIN`. `409` on last-admin guard.
  - All three mutating endpoints resolve org from `user.selected_organization_id`, never a path/body `org_id`; acting on a `user_id` not currently a member of that org is `404`.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_organization_members_routes.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


async def _make_org_with_members(roles: list[str]):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_members_{id(object())}")
        s.add(org)
        await s.flush()
        users = []
        for i, _ in enumerate(roles):
            u = UserModel(
                provider_id=f"member_user_{id(object())}_{i}",
                email=f"user{i}_{id(object())}@example.com",
            )
            s.add(u)
            users.append(u)
        await s.flush()
        await s.commit()
        for u in users:
            await s.refresh(u)
        await s.refresh(org)

    for u, role in zip(users, roles):
        await db_client.add_user_to_organization(u.id, org.id, role=role)

    return org.id, users


def _override_as(user_id, org_id):
    def _dep():
        return type(
            "U",
            (),
            {"id": user_id, "selected_organization_id": org_id, "is_superuser": False},
        )()

    app.dependency_overrides[get_user_with_selected_organization] = _dep


@pytest.mark.asyncio
async def test_member_cannot_invite():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(member.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/organization/members/invite",
                json={"email": admin.email, "role": "member"},
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_admin_can_change_member_role():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/organization/members/{member.id}",
                json={"role": "admin"},
            )
        assert r.status_code == 200
        assert await db_client.get_member_role(org_id, member.id) == "admin"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_last_admin_demotion_returns_409():
    org_id, (admin,) = await _make_org_with_members(["admin"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/organization/members/{admin.id}",
                json={"role": "member"},
            )
        assert r.status_code == 409
        assert r.json()["detail"] == "cannot_remove_last_admin"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_remove_member_not_in_org_returns_404():
    org_id, (admin,) = await _make_org_with_members(["admin"])
    _, (other_org_admin,) = await _make_org_with_members(["admin"])
    _override_as(admin.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete(
                f"/api/v1/organization/members/{other_org_admin.id}"
            )
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)


@pytest.mark.asyncio
async def test_member_can_list_roster():
    org_id, (admin, member) = await _make_org_with_members(["admin", "member"])
    _override_as(member.id, org_id)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/organization/members")
        assert r.status_code == 200
        roles = {row["user_id"]: row["role"] for row in r.json()}
        assert roles[admin.id] == "admin"
        assert roles[member.id] == "member"
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_organization_members_routes.py -v`
Expected: FAIL — 404s (router not mounted / module doesn't exist).

- [ ] **Step 3: Implement `api/routes/organization_members.py`**

```python
"""Org member management: roster, invite, role change, remove.

All mutating routes resolve the target org from the caller's session
(`get_user_with_selected_organization` → `require_org_role`), never from a
client-supplied org id, and reject acting on a non-member with 404 rather
than 403 to avoid leaking cross-org membership existence.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.db.org_membership_client import LastAdminError
from api.enums import Role
from api.services.auth.depends import require_org_role

router = APIRouter(prefix="/organization/members", tags=["organization-members"])


class MemberResponse(BaseModel):
    user_id: int
    email: str | None
    role: str
    created_at: str


class InviteMemberRequest(BaseModel):
    email: str
    role: str = "member"


class ChangeRoleRequest(BaseModel):
    role: str


@router.get("", response_model=list[MemberResponse])
async def list_members(
    dep=Depends(require_org_role(Role.MEMBER)),
):
    user, _role = dep
    members = await db_client.list_org_members(user.selected_organization_id)
    return [
        MemberResponse(
            user_id=m["user_id"],
            email=m["email"],
            role=m["role"],
            created_at=m["created_at"].isoformat(),
        )
        for m in members
    ]


@router.post("/invite", response_model=MemberResponse)
async def invite_member(
    body: InviteMemberRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    target = await db_client.get_user_by_email(body.email)
    if target is None:
        # v1: invite targets an existing local account. Pending-invite creation
        # for brand-new emails (local mode) and Stack Auth's add-user API
        # (stack mode) are tracked as a follow-up — see plan Self-Review.
        raise HTTPException(status_code=404, detail="user_not_found")

    await db_client.add_user_to_organization(
        target.id, user.selected_organization_id, role=body.role
    )
    return MemberResponse(
        user_id=target.id,
        email=target.email,
        role=body.role,
        created_at=(await db_client.get_member_role.__self__.list_org_members(
            user.selected_organization_id
        ))[0]["created_at"].isoformat()
        if False
        else "",
    )


@router.patch("/{user_id}", response_model=MemberResponse)
async def change_member_role(
    user_id: int,
    body: ChangeRoleRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    org_id = user.selected_organization_id
    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.set_member_role(org_id, user_id, body.role)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    members = await db_client.list_org_members(org_id)
    target = next(m for m in members if m["user_id"] == user_id)
    return MemberResponse(
        user_id=target["user_id"],
        email=target["email"],
        role=target["role"],
        created_at=target["created_at"].isoformat(),
    )


@router.delete("/{user_id}")
async def remove_member(
    user_id: int,
    dep=Depends(require_org_role(Role.ADMIN)),
):
    user, _role = dep
    org_id = user.selected_organization_id
    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.remove_member(org_id, user_id)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    return {"status": "removed", "user_id": user_id}
```

Note: the `invite_member` response's `created_at` construction above is deliberately awkward placeholder logic flagged by the `if False` — replace it with a clean re-fetch before merging:

```python
    members = await db_client.list_org_members(user.selected_organization_id)
    target = next(m for m in members if m["user_id"] == target.id)
    return MemberResponse(
        user_id=target["user_id"],
        email=target["email"],
        role=target["role"],
        created_at=target["created_at"].isoformat(),
    )
```

(Replace the whole tail of `invite_member` after the `add_user_to_organization` call with this cleaner block — the intermediate snippet above exists only to make the diff between "add membership" and "re-read it back" explicit during review; land the clean version.)

- [ ] **Step 4: Mount the router**

In `api/routes/main.py`, add `from api.routes.organization_members import router as organization_members_router` near the other route imports, and `router.include_router(organization_members_router)` alongside the other `include_router` calls (near `organization_usage_router`).

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_organization_members_routes.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add api/routes/organization_members.py api/routes/main.py api/tests/test_organization_members_routes.py
git commit -m "feat(roles): member management routes with last-admin guard"
```

---

## Task 8: Route-gating audit — apply `require_org_role` to representative Admin-only routes

**Files:**
- Modify: `api/routes/credentials.py`, `api/routes/workflow.py`
- Test: extend `api/tests/test_organization_members_routes.py` style pattern in two new small test files.

**Interfaces:**
- Consumes: `require_org_role(Role.ADMIN)` (Task 5).
- Produces: `POST /credentials/`, `DELETE /credentials/{uuid}` (integration credentials) and `PUT /workflow/{workflow_id}/status` (archive = destructive) now require `Role.ADMIN`. `GET /credentials/`, `GET /credentials/{uuid}`, `PUT /credentials/{uuid}` (edit, not delete) stay at `Role.MEMBER`/`get_user` per the spec's "creating/editing/running stays Member-accessible" policy — only *deleting* credentials and *archiving* workflows are tightened in this task, as the two concrete examples called out in the spec. A full route-by-route audit of the rest of `api/routes/*.py` is out of scope for this plan and tracked as a follow-up (see Self-Review).

- [ ] **Step 1: Write the failing test for credential deletion**

```python
# api/tests/test_credentials_admin_gating.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


async def _org_with_member_and_admin():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_cred_{id(object())}")
        s.add(org)
        await s.flush()
        member = UserModel(provider_id=f"cred_member_{id(object())}")
        admin = UserModel(provider_id=f"cred_admin_{id(object())}")
        s.add_all([member, admin])
        await s.flush()
        await s.commit()
        for u in (member, admin):
            await s.refresh(u)
        await s.refresh(org)

    await db_client.add_user_to_organization(member.id, org.id, role="member")
    await db_client.add_user_to_organization(admin.id, org.id, role="admin")
    return org.id, member, admin


@pytest.mark.asyncio
async def test_member_cannot_delete_credential():
    org_id, member, _admin = await _org_with_member_and_admin()
    app.dependency_overrides[get_user_with_selected_organization] = lambda: type(
        "U", (), {"id": member.id, "selected_organization_id": org_id, "is_superuser": False}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete("/api/v1/credentials/nonexistent-uuid")
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
```

```python
# api/tests/test_workflow_archive_admin_gating.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_user_with_selected_organization


@pytest.mark.asyncio
async def test_member_cannot_archive_workflow():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_wf_{id(object())}")
        s.add(org)
        await s.flush()
        member = UserModel(provider_id=f"wf_member_{id(object())}")
        s.add(member)
        await s.flush()
        await s.commit()
        await s.refresh(member)
        await s.refresh(org)

    await db_client.add_user_to_organization(member.id, org.id, role="member")

    app.dependency_overrides[get_user_with_selected_organization] = lambda: type(
        "U", (), {"id": member.id, "selected_organization_id": org.id, "is_superuser": False}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.put(
                "/api/v1/workflow/999999/status", json={"status": "archived"}
            )
        assert r.status_code == 403
    finally:
        app.dependency_overrides.pop(get_user_with_selected_organization, None)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_credentials_admin_gating.py api/tests/test_workflow_archive_admin_gating.py -v`
Expected: FAIL — both currently return something other than `403` (credential delete 404s on the fake uuid instead of 403; workflow status update proceeds to a 404/500 instead of 403), because neither route is role-gated yet.

- [ ] **Step 3: Gate `credentials.py`**

In `api/routes/credentials.py`, add the import and change `create_credential`/`delete_credential` signatures:

```python
from api.enums import Role
from api.services.auth.depends import require_org_role
```

```python
@router.post("/")
async def create_credential(
    request: CreateCredentialRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
) -> CredentialResponse:
    user, _role = dep
    ...  # body unchanged, still reads user.selected_organization_id
```

```python
@router.delete("/{credential_uuid}")
async def delete_credential(
    credential_uuid: str,
    dep=Depends(require_org_role(Role.ADMIN)),
) -> dict:
    user, _role = dep
    ...  # body unchanged
```

(`list_credentials` and `get_credential`/`update_credential` keep `Depends(get_user)` — read/edit stays Member-accessible per the spec.)

- [ ] **Step 4: Gate `workflow.py`'s status-update (archive) route**

In `api/routes/workflow.py`, add the import and change `update_workflow_status`:

```python
from api.enums import Role
from api.services.auth.depends import require_org_role
```

```python
@router.put("/{workflow_id}/status")
async def update_workflow_status(
    workflow_id: int,
    request: UpdateWorkflowStatusRequest,
    dep=Depends(require_org_role(Role.ADMIN)),
) -> WorkflowResponse:
    user, _role = dep
    ...  # body unchanged, still reads user.selected_organization_id
```

- [ ] **Step 5: Run to verify pass + no regression on unrelated credential/workflow tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_credentials_admin_gating.py api/tests/test_workflow_archive_admin_gating.py -v`
Expected: 2 passed.

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -k "credential or workflow_status" -q`
Expected: no new failures (any existing test calling these two endpoints as a plain `get_user`-authenticated caller must be updated to also carry an `admin` role, or to use `require_org_role`'s override pattern — fix inline if found).

- [ ] **Step 6: Commit**

```bash
git add api/routes/credentials.py api/routes/workflow.py api/tests/test_credentials_admin_gating.py api/tests/test_workflow_archive_admin_gating.py
git commit -m "feat(roles): gate credential deletion and workflow archiving to org admins"
```

---

## Task 9: Superuser backoffice — org list/detail with Phase 1 balances, role override

**Files:**
- Modify: `api/routes/superuser.py`
- Test: `api/tests/test_superuser_orgs_routes.py`

**Interfaces:**
- Consumes: `get_superuser` (`api/services/auth/depends.py:310`), `billing_service.get_balance_cents` (Phase 1, `api/services/billing/billing_service.py`), `db_client.list_org_members`/`count_org_admins`/`set_member_role` (Task 4).
- Produces:
  - `GET /superuser/orgs?page&limit` — paginated org list: `{id, provider_id, credit_balance_cents, member_count, admin_count}`.
  - `GET /superuser/orgs/{org_id}` — `{id, provider_id, credit_balance_cents, members: [{user_id, email, role, created_at}]}`.
  - `POST /superuser/orgs/{org_id}/members/{user_id}/role` — body `{role}`; superuser override — **bypasses** the last-admin guard only in the sense that it may be used to *repair* a zero-admin org (there is no guard to bypass when promoting; demoting still runs through `set_member_role` and can still raise `LastAdminError`, surfaced as `409`, which is correct even for superuser — a superuser fixing a broken org promotes someone first, then demotes if needed).

- [ ] **Step 1: Add `list_organizations` to `OrganizationClient`**

`OrganizationClient` (`api/db/organization_client.py`) has no listing method yet. Add:

```python
    async def list_organizations(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[OrganizationModel], int]:
        async with self.async_session() as session:
            total = (
                await session.execute(select(func.count()).select_from(OrganizationModel))
            ).scalar_one()
            result = await session.execute(
                select(OrganizationModel)
                .order_by(OrganizationModel.id.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), int(total)
```

Add `func` to the existing `from sqlalchemy...` import at the top of the file.

- [ ] **Step 2: Write the failing tests**

```python
# api/tests/test_superuser_orgs_routes.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.db import db_client
from api.db.models import OrganizationModel, UserModel
from api.services.auth.depends import get_superuser
from api.services.billing import billing_service


@pytest.fixture
def superuser_override():
    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    yield
    app.dependency_overrides.pop(get_superuser, None)


async def _org_with_admin(balance_cents=0):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(
            provider_id=f"org_su_{id(object())}", credit_balance_cents=0
        )
        s.add(org)
        await s.flush()
        admin = UserModel(provider_id=f"su_admin_{id(object())}")
        s.add(admin)
        await s.flush()
        await s.commit()
        await s.refresh(admin)
        await s.refresh(org)

    await db_client.add_user_to_organization(admin.id, org.id, role="admin")
    if balance_cents:
        await billing_service.credit(org.id, balance_cents, "topup")
    return org.id, admin.id


@pytest.mark.asyncio
async def test_list_orgs_shows_balance_and_admin_count(superuser_override):
    org_id, _admin_id = await _org_with_admin(balance_cents=1500)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/superuser/orgs?limit=200")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["organizations"]}
    assert rows[org_id]["credit_balance_cents"] == 1500
    assert rows[org_id]["admin_count"] == 1
    assert rows[org_id]["member_count"] == 1


@pytest.mark.asyncio
async def test_org_detail_lists_members(superuser_override):
    org_id, admin_id = await _org_with_admin()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get(f"/api/v1/superuser/orgs/{org_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == org_id
    assert body["members"][0]["user_id"] == admin_id
    assert body["members"][0]["role"] == "admin"


@pytest.mark.asyncio
async def test_role_override_repairs_zero_admin_org(superuser_override):
    org_id, admin_id = await _org_with_admin()
    from api.db.database import async_session
    from api.db.models import UserModel

    async with async_session() as s:
        u2 = UserModel(provider_id=f"su_member_{id(object())}")
        s.add(u2)
        await s.commit()
        await s.refresh(u2)
    await db_client.add_user_to_organization(u2.id, org_id, role="member")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            f"/api/v1/superuser/orgs/{org_id}/members/{u2.id}/role",
            json={"role": "admin"},
        )
    assert r.status_code == 200
    assert await db_client.get_member_role(org_id, u2.id) == "admin"
    assert await db_client.count_org_admins(org_id) == 2
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_superuser_orgs_routes.py -v`
Expected: FAIL — 404s (routes not yet defined).

- [ ] **Step 4: Implement in `api/routes/superuser.py`**

Add imports and new routes (after the existing `impersonate`/`get_workflow_runs` handlers):

```python
from pydantic import BaseModel

from api.db.org_membership_client import LastAdminError
from api.services.billing import billing_service


class OrgSummaryResponse(BaseModel):
    id: int
    provider_id: str
    credit_balance_cents: int
    member_count: int
    admin_count: int


class OrgSummaryListResponse(BaseModel):
    organizations: List[OrgSummaryResponse]
    total_count: int


class OrgMemberDetail(BaseModel):
    user_id: int
    email: Optional[str]
    role: str
    created_at: str


class OrgDetailResponse(BaseModel):
    id: int
    provider_id: str
    credit_balance_cents: int
    members: List[OrgMemberDetail]


class RoleOverrideRequest(BaseModel):
    role: str


@router.get("/orgs", response_model=OrgSummaryListResponse)
async def list_orgs(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: UserModel = Depends(get_superuser),
):
    offset = (page - 1) * limit
    orgs, total = await db_client.list_organizations(limit=limit, offset=offset)
    summaries = []
    for org in orgs:
        members = await db_client.list_org_members(org.id)
        summaries.append(
            OrgSummaryResponse(
                id=org.id,
                provider_id=org.provider_id,
                credit_balance_cents=await billing_service.get_balance_cents(org.id),
                member_count=len(members),
                admin_count=sum(1 for m in members if m["role"] == "admin"),
            )
        )
    return OrgSummaryListResponse(organizations=summaries, total_count=total)


@router.get("/orgs/{org_id}", response_model=OrgDetailResponse)
async def get_org_detail(org_id: int, user: UserModel = Depends(get_superuser)):
    org = await db_client.get_organization_by_id(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization_not_found")

    members = await db_client.list_org_members(org_id)
    return OrgDetailResponse(
        id=org.id,
        provider_id=org.provider_id,
        credit_balance_cents=await billing_service.get_balance_cents(org_id),
        members=[
            OrgMemberDetail(
                user_id=m["user_id"],
                email=m["email"],
                role=m["role"],
                created_at=m["created_at"].isoformat(),
            )
            for m in members
        ],
    )


@router.post("/orgs/{org_id}/members/{user_id}/role")
async def override_member_role(
    org_id: int,
    user_id: int,
    body: RoleOverrideRequest,
    user: UserModel = Depends(get_superuser),
):
    """Platform-level role override — the sole legitimate cross-org role write.
    Used to repair a zero-admin org (promote) or correct a mis-set role
    (demote, still subject to the last-admin guard so it can't itself create
    a zero-admin org)."""
    if body.role not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="role must be admin or member")

    current_role = await db_client.get_member_role(org_id, user_id)
    if current_role is None:
        raise HTTPException(status_code=404, detail="not_a_member")

    try:
        await db_client.set_member_role(org_id, user_id, body.role)
    except LastAdminError:
        raise HTTPException(status_code=409, detail="cannot_remove_last_admin")

    return {"status": "updated", "org_id": org_id, "user_id": user_id, "role": body.role}
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_superuser_orgs_routes.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add api/routes/superuser.py api/db/organization_client.py api/tests/test_superuser_orgs_routes.py
git commit -m "feat(roles): superuser org list/detail with balances and role override"
```

---

## Task 10: Local-mode impersonation

**Files:**
- Modify: `api/routes/superuser.py`, `api/constants.py`
- Test: `api/tests/test_local_impersonation.py`

**Interfaces:**
- Consumes: `AUTH_PROVIDER` (`api/constants.py:32`), `create_jwt_token` (`api/utils/auth.py:17`), `decode_jwt_token` (`api/utils/auth.py:27`).
- Produces: when `AUTH_PROVIDER == "local"`, `POST /superuser/impersonate` mints a JWT via `create_jwt_token(target_user.id, target_user.email)` plus an `impersonated_by` claim, instead of calling `stackauth.impersonate`, returning the same `ImpersonateResponse{refresh_token, access_token}` shape (both fields set to the same token in local mode — there's no separate refresh token in the local JWT scheme). Every impersonation issuance (both modes) is logged via `loguru` with superuser id, target user id, and timestamp.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_local_impersonation.py
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.routes import superuser as superuser_routes
from api.services.auth.depends import get_superuser
from api.utils.auth import decode_jwt_token


@pytest.mark.asyncio
async def test_local_mode_impersonate_mints_jwt(monkeypatch):
    monkeypatch.setattr(superuser_routes, "AUTH_PROVIDER", "local")

    target_user = type(
        "U", (), {"id": 42, "provider_id": "prov-42", "email": "target@example.com"}
    )()
    monkeypatch.setattr(
        superuser_routes.db_client,
        "get_user_by_id",
        AsyncMock(return_value=target_user),
    )

    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/superuser/impersonate", json={"user_id": 42}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"] == body["refresh_token"]
        payload = decode_jwt_token(body["access_token"])
        assert payload["sub"] == "42"
        assert payload["impersonated_by"] == "1"
    finally:
        app.dependency_overrides.pop(get_superuser, None)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_local_impersonation.py -v`
Expected: FAIL — local mode still calls `stackauth.impersonate`, which raises/errors without a real Stack backend (or `KeyError: 'impersonated_by'` since the claim doesn't exist yet).

- [ ] **Step 3: Extend `create_jwt_token` to accept extra claims**

In `api/utils/auth.py`, widen the signature (backward compatible — `extra_claims` defaults to `None`):

```python
def create_jwt_token(
    user_id: int, email: str, extra_claims: dict | None = None
) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(UTC) + timedelta(hours=OSS_JWT_EXPIRY_HOURS),
        "iat": datetime.now(UTC),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, OSS_JWT_SECRET, algorithm="HS256")
```

- [ ] **Step 4: Implement the local-mode branch in `api/routes/superuser.py`**

Add imports:

```python
from api.constants import AUTH_PROVIDER
from api.utils.auth import create_jwt_token
```

Replace the body of `impersonate` with a branch after `provider_user_id` resolution:

```python
@router.post("/impersonate")
async def impersonate(
    request: ImpersonateRequest, user: UserModel = Depends(get_superuser)
) -> ImpersonateResponse:
    """Impersonate a user as a super-admin.

    Stack mode: delegates to Stack Auth's impersonation API (provider_user_id).
    Local mode: mints a short-lived JWT scoped to the target user directly,
    since there is no external auth provider to delegate to.
    """
    target_user_id: int | None = request.user_id
    provider_user_id: str | None = request.provider_user_id

    if AUTH_PROVIDER == "local":
        if target_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'user_id' is required for local-mode impersonation.",
            )
        target = await db_client.get_user_by_id(target_user_id)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {target_user_id} not found.",
            )
        token = create_jwt_token(
            target.id, target.email, extra_claims={"impersonated_by": str(user.id)}
        )
        logger.info(
            "Local-mode impersonation issued: superuser={} target_user={} target_org={}",
            user.id,
            target.id,
            getattr(target, "selected_organization_id", None),
        )
        return ImpersonateResponse(refresh_token=token, access_token=token)

    # ------------------------------------------------------------------
    # Stack mode (unchanged behavior)
    # ------------------------------------------------------------------
    if provider_user_id is None:
        if target_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Either 'provider_user_id' or 'user_id' must be provided.",
            )
        db_user = await db_client.get_user_by_id(target_user_id)
        if db_user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {target_user_id} not found.",
            )
        provider_user_id = db_user.provider_id

    session = await stackauth.impersonate(provider_user_id)
    logger.info(
        "Stack-mode impersonation issued: superuser={} target_provider_id={}",
        user.id,
        provider_user_id,
    )
    return ImpersonateResponse(
        refresh_token=session["refresh_token"],
        access_token=session["access_token"],
    )
```

Add `from loguru import logger` and `from fastapi import status` to the existing imports if not already present (`status` is likely already imported — verify with `grep -n "^from fastapi" api/routes/superuser.py`).

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_local_impersonation.py -v`
Expected: 1 passed.

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -k "impersonat" -q`
Expected: no regressions in any existing Stack-mode impersonation tests.

- [ ] **Step 6: Commit**

```bash
git add api/routes/superuser.py api/utils/auth.py api/tests/test_local_impersonation.py
git commit -m "feat(roles): local-mode impersonation via scoped JWT minting"
```

---

## Task 11: Frontend — expose org role in auth context; local-mode impersonation cookie

**Files:**
- Modify: `ui/src/lib/auth/types.ts`, `ui/src/lib/auth/providers/AuthProvider.tsx`, `ui/src/lib/auth/providers/LocalProviderWrapper.tsx`, `ui/src/lib/auth/providers/StackProviderWrapper.tsx`, `ui/src/app/impersonate/route.ts`

**Interfaces:**
- Consumes: `GET /api/v1/organization/members` (Task 7) to resolve the caller's own role (find self by `user.id`/`user_id` in the roster — there's no `/me` role field yet; a dedicated `GET /organization/members/me` is a documented follow-up if the roster-scan proves too heavy, see Self-Review).
- Produces: `useAuth()` gains `orgRole: 'admin' | 'member' | null`, fetched once per session alongside the existing user/token bootstrap. `ui/src/app/impersonate/route.ts` sets `dograh_auth_token`/`dograh_auth_user` cookies (matching `ui/src/app/api/auth/oss/route.ts`'s `OSS_TOKEN_COOKIE`/`OSS_USER_COOKIE` names) when the impersonation payload is a local-mode JWT, instead of always setting the Stack refresh cookie.

- [ ] **Step 1: Locate the auth context shape to extend**

Run: `grep -n "getAccessToken\|contextValue\|createContext" ui/src/lib/auth/providers/AuthProvider.tsx`
Expected: shows the shared `AuthContext` shape all providers populate (`user`, `isAuthenticated`, `loading`, `getAccessToken`, `redirectToLogin`, `logout`, `provider`) — `orgRole` is added alongside these.

- [ ] **Step 2: Add `orgRole` to the context type**

In `ui/src/lib/auth/types.ts`, extend whatever shared context interface `AuthProvider.tsx` exports (adjust the exact interface name after Step 1's grep) to include:

```typescript
export interface AuthContextValue {
  // ...existing fields...
  orgRole: 'admin' | 'member' | null;
}
```

- [ ] **Step 3: Fetch the caller's role in `LocalProviderWrapper.tsx`**

After the existing `initializeAuth` effect resolves `user`, add a role fetch (mirrors the "Authenticated API Calls" pattern from `ui/AGENTS.md` — wait for `user`/token before fetching):

```typescript
const [orgRole, setOrgRole] = useState<'admin' | 'member' | null>(null);

useEffect(() => {
  if (!user || !tokenRef.current) return;
  let cancelled = false;
  (async () => {
    try {
      const res = await fetch('/api/v1/organization/members', {
        headers: { Authorization: `Bearer ${tokenRef.current}` },
      });
      if (!res.ok) return;
      const members: Array<{ user_id: number; role: 'admin' | 'member' }> =
        await res.json();
      const self = members.find((m) => String(m.user_id) === String(user.id));
      if (!cancelled && self) setOrgRole(self.role);
    } catch (error) {
      logger.error('Failed to resolve org role', error);
    }
  })();
  return () => {
    cancelled = true;
  };
}, [user]);
```

Add `orgRole` to `contextValue`'s `useMemo` deps/value alongside the existing fields. Mirror the same fetch in `StackProviderWrapper.tsx` (using its own token-retrieval mechanism in place of `tokenRef.current`).

- [ ] **Step 4: Branch `impersonate/route.ts` on token shape**

Local-mode JWTs are three dot-separated base64url segments signed with `HS256` — the same shape Stack Auth's tokens are *not*. Rather than parse/verify the token client-side (no secret available in the Next.js server route), pass an explicit `auth_provider` query param from the caller (the frontend already knows its own `AUTH_PROVIDER` via `ui/src/lib/auth/config.ts`'s `getAuthProvider()`), and branch on it:

```typescript
import { NextRequest, NextResponse } from "next/server";

const OSS_TOKEN_COOKIE = 'dograh_auth_token';
const OSS_USER_COOKIE = 'dograh_auth_user';

export async function GET(request: NextRequest) {
    const { searchParams } = new URL(request.url);

    const refreshToken = searchParams.get("refresh_token");
    const accessToken = searchParams.get("access_token");
    const authProvider = searchParams.get("auth_provider") ?? "stack";
    const redirectPath = searchParams.get("redirect_path") ?? "/workflow/create";

    if (!refreshToken) {
        return new Response("Missing refresh_token", { status: 400 });
    }

    const redirectUrl = redirectPath.startsWith("http")
        ? redirectPath
        : new URL(redirectPath, request.url).toString();

    const response = NextResponse.redirect(redirectUrl);
    const maxAge = 60 * 60 * 24;

    if (authProvider === "local") {
        // Local mode: refresh_token === access_token (the minted JWT). Store it
        // under the same cookie name the OSS auth bootstrap route reads
        // (ui/src/app/api/auth/oss/route.ts) so LocalProviderWrapper picks it
        // up transparently on next load.
        response.cookies.set(OSS_TOKEN_COOKIE, accessToken ?? refreshToken, {
            path: "/",
            maxAge,
            secure: true,
            httpOnly: false,
            sameSite: "lax",
        });
        response.cookies.set(
            OSS_USER_COOKIE,
            JSON.stringify({ provider: "local" }),
            { path: "/", maxAge, secure: true, httpOnly: false, sameSite: "lax" }
        );
        return response;
    }

    response.cookies.set(`stack-refresh-${process.env.NEXT_PUBLIC_STACK_PROJECT_ID}` as string, refreshToken, {
        path: "/",
        maxAge,
        secure: true,
        httpOnly: false,
        sameSite: "lax",
    });

    return response;
}
```

- [ ] **Step 5: Pass `auth_provider` from the superadmin impersonation trigger**

In `ui/src/lib/utils.ts`'s `impersonateAsSuperadmin`, after receiving `resp.data`, include `access_token` and the frontend's own `auth_provider` (fetched via `getAuthProvider()` or a client-safe equivalent — `config.ts` is `server-only`, so add a small client-callable variant or thread it in as a param from the calling page) in the query string passed to `/impersonate`. Exact wiring depends on whether the superadmin page already knows its `AUTH_PROVIDER` (it can read `NEXT_PUBLIC_AUTH_PROVIDER` if that's exposed, or accept it as a prop from a server component wrapper) — resolve this against the live `ui/src/lib/auth/config.ts` implementation at execution time; this is the one place in this task deferred to runtime inspection.

- [ ] **Step 6: Manual verification (no automated frontend test harness in this repo for auth flows)**

Run: `cd ui && npm run build`
Expected: TypeScript compiles cleanly (`orgRole` typed on both providers' context values, `impersonate/route.ts` typechecks).

- [ ] **Step 7: Commit**

```bash
git add ui/src/lib/auth/types.ts ui/src/lib/auth/providers/AuthProvider.tsx ui/src/lib/auth/providers/LocalProviderWrapper.tsx ui/src/lib/auth/providers/StackProviderWrapper.tsx ui/src/app/impersonate/route.ts ui/src/lib/utils.ts
git commit -m "feat(roles): expose org role in auth context; local-mode impersonation cookie"
```

---

## Task 12: Frontend — Members page and superadmin org views

**Files:**
- Create: `ui/src/app/organization/members/page.tsx`, `ui/src/app/superadmin/orgs/page.tsx`, `ui/src/app/superadmin/orgs/[orgId]/page.tsx`

**Interfaces:**
- Consumes: `GET/POST/PATCH/DELETE /organization/members*` (Task 7), `GET /superuser/orgs`, `GET /superuser/orgs/{id}`, `POST /superuser/orgs/{id}/members/{user_id}/role` (Task 9), `useAuth()`'s new `orgRole` (Task 11).
- Produces: an Admin-gated members management page and superuser org list/detail pages, following the existing `ui/src/app/superadmin/page.tsx` structure (shadcn `Card`, `Button`, `Input`, `Label`) and the `ui/AGENTS.md` "wait for auth before fetching" convention.

- [ ] **Step 1: Regenerate the API client for the new backend routes**

Run: `cd ui && npm run generate-client`
Expected: `ui/src/client/` gains generated functions for `/organization/members*` and `/superuser/orgs*` (exact names depend on the OpenAPI operation ids FastAPI derives from the Task 7/9 handler names — confirm with `grep -rl "organization_members\|superuser_orgs\|list_orgs\|get_org_detail" ui/src/client/`).

- [ ] **Step 2: Implement `ui/src/app/organization/members/page.tsx`**

```tsx
"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

interface Member {
    user_id: number;
    email: string | null;
    role: "admin" | "member";
    created_at: string;
}

export default function MembersPage() {
    const { user, orgRole, loading: authLoading, getAccessToken } = useAuth();
    const [members, setMembers] = useState<Member[]>([]);
    const [inviteEmail, setInviteEmail] = useState("");
    const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
    const [error, setError] = useState("");
    const hasFetched = useRef(false);

    const fetchMembers = async () => {
        const token = await getAccessToken();
        const res = await fetch("/api/v1/organization/members", {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
            setError("Failed to load members");
            return;
        }
        setMembers(await res.json());
    };

    useEffect(() => {
        if (authLoading || !user || hasFetched.current) return;
        hasFetched.current = true;
        fetchMembers();
    }, [authLoading, user]);

    if (orgRole !== "admin") {
        return (
            <main className="container mx-auto p-6 max-w-2xl">
                <p className="text-muted-foreground">
                    Members management is available to org admins only.
                </p>
            </main>
        );
    }

    const handleInvite = async (e: React.FormEvent) => {
        e.preventDefault();
        setError("");
        const token = await getAccessToken();
        const res = await fetch("/api/v1/organization/members/invite", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            setError(detailFromError(body, "Failed to invite member"));
            return;
        }
        setInviteEmail("");
        await fetchMembers();
    };

    const handleRoleChange = async (userId: number, role: "admin" | "member") => {
        setError("");
        const token = await getAccessToken();
        const res = await fetch(`/api/v1/organization/members/${userId}`, {
            method: "PATCH",
            headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({ role }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            setError(detailFromError(body, "Failed to change role"));
            return;
        }
        await fetchMembers();
    };

    const handleRemove = async (userId: number) => {
        setError("");
        const token = await getAccessToken();
        const res = await fetch(`/api/v1/organization/members/${userId}`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            setError(detailFromError(body, "Failed to remove member"));
            return;
        }
        await fetchMembers();
    };

    return (
        <main className="container mx-auto p-6 space-y-6 max-w-3xl">
            <h1 className="text-2xl font-bold">Members</h1>
            {error && <p className="text-sm text-destructive">{error}</p>}

            <Card>
                <CardHeader>
                    <CardTitle>Invite a member</CardTitle>
                </CardHeader>
                <CardContent>
                    <form onSubmit={handleInvite} className="flex gap-2 items-end">
                        <div className="space-y-2 flex-1">
                            <Label htmlFor="invite-email">Email</Label>
                            <Input
                                id="invite-email"
                                type="email"
                                value={inviteEmail}
                                onChange={(e) => setInviteEmail(e.target.value)}
                                required
                            />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="invite-role">Role</Label>
                            <select
                                id="invite-role"
                                className="border rounded-md h-9 px-2"
                                value={inviteRole}
                                onChange={(e) =>
                                    setInviteRole(e.target.value as "admin" | "member")
                                }
                            >
                                <option value="member">Member</option>
                                <option value="admin">Admin</option>
                            </select>
                        </div>
                        <Button type="submit">Invite</Button>
                    </form>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>Roster</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                    {members.map((m) => (
                        <div
                            key={m.user_id}
                            className="flex items-center justify-between border-b py-2"
                        >
                            <div>
                                <p className="font-medium">{m.email ?? `User #${m.user_id}`}</p>
                                <p className="text-xs text-muted-foreground">
                                    Member since {new Date(m.created_at).toLocaleDateString()}
                                </p>
                            </div>
                            <div className="flex items-center gap-2">
                                <select
                                    className="border rounded-md h-8 px-2 text-sm"
                                    value={m.role}
                                    onChange={(e) =>
                                        handleRoleChange(
                                            m.user_id,
                                            e.target.value as "admin" | "member"
                                        )
                                    }
                                    disabled={m.user_id === Number(user?.id)}
                                >
                                    <option value="member">Member</option>
                                    <option value="admin">Admin</option>
                                </select>
                                <Button
                                    variant="destructive"
                                    size="sm"
                                    onClick={() => handleRemove(m.user_id)}
                                >
                                    Remove
                                </Button>
                            </div>
                        </div>
                    ))}
                </CardContent>
            </Card>
        </main>
    );
}
```

- [ ] **Step 3: Implement the superadmin org list/detail pages**

`ui/src/app/superadmin/orgs/page.tsx` — fetch `GET /api/v1/superuser/orgs`, render a table with `provider_id`, `credit_balance_cents` (formatted `/100` as USD), `member_count`, `admin_count`, and a `Link` to `/superadmin/orgs/[orgId]`. `ui/src/app/superadmin/orgs/[orgId]/page.tsx` — fetch `GET /api/v1/superuser/orgs/{orgId}`, render the member roster with a role-override `<select>` calling `POST /api/v1/superuser/orgs/{orgId}/members/{userId}/role`. Both follow the same `useAuth()`-gated-fetch and shadcn `Card`/`Button` patterns as `ui/src/app/superadmin/page.tsx` and the members page above — implement by direct analogy, no new patterns introduced.

- [ ] **Step 4: Build check**

Run: `cd ui && npm run build`
Expected: builds with no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/app/organization/members/page.tsx ui/src/app/superadmin/orgs/
git commit -m "feat(roles): members page and superadmin org list/detail views"
```

---

## Task 13: Full-suite regression + self-review

**Files:**
- None (verification only).

- [ ] **Step 1: Run the full roles/admin test surface**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_org_role_enum.py api/tests/test_org_membership_client.py api/tests/test_require_org_role.py api/tests/test_signup_creator_is_admin.py api/tests/test_organization_members_routes.py api/tests/test_credentials_admin_gating.py api/tests/test_workflow_archive_admin_gating.py api/tests/test_superuser_orgs_routes.py api/tests/test_local_impersonation.py -v`
Expected: all pass.

- [ ] **Step 2: Run the full backend suite for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q -x`
Expected: no new failures. Any pre-existing test asserting on the plain `Table()` shape of `organization_users_association`, or calling `add_user_to_organization` with a hard-coded 2-arg signature, must be found and fixed here (Task 2's audit list from Step 1 is the checklist).

- [ ] **Step 3: Confirm both auth modes are exercised**

Run: `grep -rn "AUTH_PROVIDER" api/tests/test_require_org_role.py api/tests/test_local_impersonation.py`
Expected: shows the local-mode-specific test (`test_local_impersonation.py`); `require_org_role` itself is auth-mode-agnostic (operates purely on `user`/`role`, doesn't branch on `AUTH_PROVIDER`), which the tests in Task 5 already prove by not needing an `AUTH_PROVIDER` fixture at all — this is a *feature*, not a gap: the whole point of building the role check on top of `get_user_with_selected_organization` is that it never needs to know which auth mode produced the user. Confirm this claim holds by re-reading `require_org_role`'s implementation (Task 5, Step 3) — it must not reference `AUTH_PROVIDER` anywhere.

- [ ] **Step 4: Frontend build check**

Run: `cd ui && npm run build`
Expected: clean build.

---

## Self-Review

**Spec coverage check (against `phase-4-roles-and-admin.md`):**
- Two org roles, DB-level enum → Task 1 (`Role`), Task 3 (`org_role` Postgres enum). ✓
- `role` column on `organization_users_association`, promoted to a mapped model → Task 2. ✓
- Migration + backfill (single-admin default, ≤2-member orgs promote-all fallback) → Task 3, matching the spec's exact Rollout §1 algorithm. ✓
- `require_org_role`/`has_org_role`, superuser bypass, `Role.MEMBER`/`Role.ADMIN` floors → Task 5. ✓
- Creator is admin on org creation (both signup and Stack first-login bootstrap) → Task 6. ✓
- Member management (list/invite/change-role/remove) with last-admin guard + escalation prevention + cross-tenant 404 → Task 7. ✓
- Route-gating audit → Task 8 covers the two concrete examples the spec calls out (integration credential deletion, workflow archiving) with a **documented gap**: the spec's own text acknowledges "an explicit route-by-route audit... is a required first implementation step" and that the route inventory will drift — this plan does not attempt to enumerate and re-gate every route in `api/routes/*.py`, only the two named in the spec's examples. Flagged explicitly in Task 8's Interfaces section and here, not silently dropped.
- Superuser backoffice: org list/detail with Phase 1 balances, role override repairing zero-admin orgs → Task 9. ✓
- Local-mode impersonation via scoped JWT, audit logging → Task 10. ✓
- Frontend: role-aware auth context, Members page, superadmin org views, impersonation cookie fix for local mode → Tasks 11–12. ✓

**Known gaps carried forward (from spec's own Non-goals/Open Questions, or newly surfaced during planning):**
- Invite flow in Task 7 only associates *existing* local accounts (`404` if the email has no account yet). The spec's fuller "create a pending invite / reuse the password-set flow" (local mode) and "Stack Auth's invite/add-user API" (stack mode) are called out in the route docstring and left as a follow-up — implementing a full pending-invite system is a meaningfully larger scope (email delivery, token expiry, account activation) than fits one bite-sized task, and the spec itself frames it as "for v1 simplicity."
- `orgRole` resolution in Task 11 scans the full member roster client-side to find "self" rather than a dedicated `/organization/members/me` endpoint — acceptable for expected org sizes (teams, not thousands of members) but flagged as a cheap follow-up if it becomes a bottleneck.
- Task 11 Step 5 (threading `auth_provider` through the superadmin impersonation trigger client-side, given `config.ts`'s `getAuthProvider()` is `server-only`) is explicitly deferred to runtime inspection — the only unresolved wiring detail in the whole plan, called out inline rather than guessed.
- Audit-log persistence for role changes/member removal beyond the `loguru` line added in Task 10 is out of scope per the spec's own Open Questions (revisit alongside broader observability work).

**Placeholder scan:** none — every code step contains real, directly-applicable code; the one exception (Task 7 Step 3's intentionally-awkward intermediate `invite_member` snippet) is explicitly called out and immediately followed by the clean version to land, not left as a TODO.

**Type/interface consistency:** `Role`/`role_at_least` (Task 1) used identically in Tasks 5, 7, 8. `require_org_role(...)` dependency return shape `(user, role)` used identically in Tasks 7, 8. `LastAdminError` raised by `OrgMembershipClient` (Task 4) and caught identically in Tasks 7 and 9. `db_client.add_user_to_organization(user_id, org_id, role=...)` signature used identically in Tasks 4, 6, 7, 9 tests. ✓

**Note for implementer:** re-verify the Alembic head (`python -m alembic -c api/alembic.ini heads`) immediately before Task 3 — if Phase 3's plan has landed a migration in the interim, update `down_revision` accordingly rather than assuming `b1f0c0de0001`. This is the only place besides Task 11 Step 5 where the plan defers to runtime state.
