# SaaS Phase 1 — Demo-able Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the phase-1 slice of the approved SaaS design (`docs/superpowers/specs/2026-07-21-saas-platform-design.md`): a VoxAgent-branded deployment with `DEPLOYMENT_MODE=saas`, Clerk authentication, trial minutes on the local credit ledger, platform-key model defaults, and Dograh/MPS UI stripped — so a stranger can sign up, verify email, build an agent, and make a free web test call.

**Architecture:** Backend adds a third auth path (`AUTH_PROVIDER=clerk`) mirroring the existing Stack pattern (JWKS-verified JWTs, lazy user/org provisioning), a saas boot validator, a trial-grant service on the existing ledger, and platform-key default model config replacing MPS. Frontend adds a `ClerkProviderWrapper` to the existing auth abstraction, Clerk middleware branch, profile page, saas-mode UI gating, and a `BRAND` constant.

**Tech Stack:** FastAPI, SQLAlchemy async, PyJWT (`jwt` already used in `api/utils/auth.py`), `svix` (new dep, Clerk webhooks), Next.js 15 App Router, `@clerk/nextjs` (new dep), pytest.

## Global Constraints

- Brand name everywhere user-visible: **VoxAgent** (single source: `ui/src/constants/brand.ts`, `api` uses `BRAND_NAME` in `api/constants.py`).
- `DEPLOYMENT_MODE=saas` requires `AUTH_PROVIDER=clerk` and `BILLING_ENGINE=local`; boot fails otherwise (spec §1).
- OSS behavior must remain byte-for-byte unchanged when `DEPLOYMENT_MODE=oss` — every new behavior is gated.
- Org scoping: any org-scoped read/write filters by `organization_id` (see `api/AGENTS.md`).
- Routes thin; logic in `services/`; DB access in `db/` clients.
- Tests run with: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/<file> -xvs` (run from repo root).
- UI generated client: after adding backend routes run `cd ui && npm run generate-client` (backend must be running locally). Always check `response.error` (client does not throw).
- Commit after every task; conventional-commit messages; trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: saas deployment mode constants + boot validation

**Files:**
- Modify: `api/constants.py` (after line 34, near `DEPLOYMENT_MODE`)
- Create: `api/services/saas_config.py`
- Modify: `api/app.py` (import + call validator right before the CORS block at ~line 91)
- Test: `api/tests/test_saas_config.py`

**Interfaces:**
- Produces: `api.constants`: `DEPLOYMENT_MODE_SAAS = "saas"`, `IS_SAAS_MODE: bool`, `BRAND_NAME = "VoxAgent"`, `CLERK_ISSUER: str | None`, `CLERK_WEBHOOK_SECRET: str | None`, `TRIAL_MINUTES: int`, `PLATFORM_OPENAI_API_KEY`, `PLATFORM_DEEPGRAM_API_KEY`, `PLATFORM_ELEVENLABS_API_KEY` (all `str | None`).
- Produces: `api.services.saas_config.validate_saas_config() -> None` (raises `RuntimeError` listing every problem; no-op unless saas mode).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_saas_config.py
import pytest


def _set_valid_saas_env(monkeypatch):
    monkeypatch.setattr("api.services.saas_config.DEPLOYMENT_MODE", "saas")
    monkeypatch.setattr("api.services.saas_config.AUTH_PROVIDER", "clerk")
    monkeypatch.setattr("api.services.saas_config.BILLING_ENGINE", "local")
    monkeypatch.setattr(
        "api.services.saas_config.CLERK_ISSUER", "https://x.clerk.accounts.dev"
    )
    monkeypatch.setattr("api.services.saas_config.CLERK_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setattr("api.services.saas_config.OSS_JWT_SECRET", "a-strong-secret")
    monkeypatch.setattr(
        "api.services.saas_config.CORS_ALLOWED_ORIGINS", ["https://app.voxagent.com"]
    )


def test_oss_mode_is_never_validated(monkeypatch):
    from api.services.saas_config import validate_saas_config

    monkeypatch.setattr("api.services.saas_config.DEPLOYMENT_MODE", "oss")
    validate_saas_config()  # must not raise, regardless of other settings


def test_valid_saas_config_passes(monkeypatch):
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    validate_saas_config()


@pytest.mark.parametrize(
    "attr,bad_value,fragment",
    [
        ("AUTH_PROVIDER", "local", "AUTH_PROVIDER"),
        ("BILLING_ENGINE", "mps", "BILLING_ENGINE"),
        ("CLERK_ISSUER", None, "CLERK_ISSUER"),
        ("CLERK_WEBHOOK_SECRET", None, "CLERK_WEBHOOK_SECRET"),
        ("OSS_JWT_SECRET", "change-me-in-production", "OSS_JWT_SECRET"),
        ("CORS_ALLOWED_ORIGINS", [], "CORS_ALLOWED_ORIGINS"),
    ],
)
def test_invalid_saas_config_fails(monkeypatch, attr, bad_value, fragment):
    from api.services.saas_config import validate_saas_config

    _set_valid_saas_env(monkeypatch)
    monkeypatch.setattr(f"api.services.saas_config.{attr}", bad_value)
    with pytest.raises(RuntimeError, match=fragment):
        validate_saas_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest api/tests/test_saas_config.py -xvs`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.services.saas_config'`

- [ ] **Step 3: Add constants**

In `api/constants.py`, insert directly below the `AUTH_PROVIDER` line (line 32):

```python
DEPLOYMENT_MODE_SAAS = "saas"
IS_SAAS_MODE = DEPLOYMENT_MODE == DEPLOYMENT_MODE_SAAS

BRAND_NAME = "VoxAgent"

# Clerk auth (AUTH_PROVIDER=clerk). Issuer is the Clerk frontend API URL,
# e.g. https://your-app.clerk.accounts.dev — JWKS is fetched from
# f"{CLERK_ISSUER}/.well-known/jwks.json".
CLERK_ISSUER = os.getenv("CLERK_ISSUER")
CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET")

# Free trial minutes granted once per newly provisioned organization (saas mode).
TRIAL_MINUTES = int(os.getenv("TRIAL_MINUTES", "15"))

# Platform-held AI provider keys (saas mode): injected as org default model
# configuration so tenants never handle provider keys.
PLATFORM_OPENAI_API_KEY = os.getenv("PLATFORM_OPENAI_API_KEY")
PLATFORM_DEEPGRAM_API_KEY = os.getenv("PLATFORM_DEEPGRAM_API_KEY")
PLATFORM_ELEVENLABS_API_KEY = os.getenv("PLATFORM_ELEVENLABS_API_KEY")
```

- [ ] **Step 4: Write the validator**

```python
# api/services/saas_config.py
"""Boot-time configuration validation for DEPLOYMENT_MODE=saas.

Fails fast with one aggregated error instead of letting a misconfigured
deployment limp into production (spec §1).
"""

from api.constants import (
    AUTH_PROVIDER,
    BILLING_ENGINE,
    CLERK_ISSUER,
    CLERK_WEBHOOK_SECRET,
    CORS_ALLOWED_ORIGINS,
    DEPLOYMENT_MODE,
    DEPLOYMENT_MODE_SAAS,
    OSS_JWT_SECRET,
)

_DEFAULT_JWT_SECRET = "change-me-in-production"


def validate_saas_config() -> None:
    if DEPLOYMENT_MODE != DEPLOYMENT_MODE_SAAS:
        return

    problems: list[str] = []
    if AUTH_PROVIDER != "clerk":
        problems.append("AUTH_PROVIDER must be 'clerk' in saas mode")
    if BILLING_ENGINE != "local":
        problems.append("BILLING_ENGINE must be 'local' in saas mode")
    if not CLERK_ISSUER:
        problems.append("CLERK_ISSUER is required in saas mode")
    if not CLERK_WEBHOOK_SECRET:
        problems.append("CLERK_WEBHOOK_SECRET is required in saas mode")
    if not OSS_JWT_SECRET or OSS_JWT_SECRET == _DEFAULT_JWT_SECRET:
        problems.append("OSS_JWT_SECRET must be set to a non-default value")
    if not CORS_ALLOWED_ORIGINS:
        problems.append("CORS_ALLOWED_ORIGINS must be an explicit allowlist")

    if problems:
        raise RuntimeError(
            "Invalid saas deployment configuration:\n- " + "\n- ".join(problems)
        )
```

Note: `OSS_JWT_SECRET` still signs app-level tokens (superadmin impersonation) in saas mode, hence the check. Payments/Razorpay keys are validated in phase 2 when `BILLING_PAYMENTS_ENABLED` turns on (spec §1).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest api/tests/test_saas_config.py -xvs`
Expected: 8 PASS

- [ ] **Step 6: Wire into app startup**

In `api/app.py`, immediately before the `# Configure CORS.` comment (~line 91):

```python
from api.services.saas_config import validate_saas_config

validate_saas_config()
```

Also extend the CORS branch: the existing code already treats `DEPLOYMENT_MODE != "oss"` as strict-allowlist, so `saas` gets strict CORS for free — verify by reading `api/app.py:91-115`, no change needed if so.

- [ ] **Step 7: Full-suite sanity + commit**

Run: `python -m pytest api/tests/test_saas_config.py -xvs && python -c "import api.app"` (with `api/.env.test` sourced; expect import OK since test env is oss mode)

```bash
git add api/constants.py api/services/saas_config.py api/app.py api/tests/test_saas_config.py
git commit -m "feat(saas): add saas deployment mode with boot-time config validation"
```

---

### Task 2: Clerk JWT verification service

**Files:**
- Create: `api/services/auth/clerk_auth.py`
- Modify: `api/requirements.txt` (add `svix==1.45.1` — used in Task 6, install now with pinned version; PyJWT + cryptography already present via existing JWT usage — verify with `pip show pyjwt cryptography`)
- Test: `api/tests/test_clerk_auth.py`

**Interfaces:**
- Produces: `clerk_auth.verify_clerk_token(authorization: str | None) -> dict` — returns Clerk claims (`sub`, optional `email`, `name`) or raises `fastapi.HTTPException(401)`. Module-level `_get_signing_key(token: str)` is the test seam.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_clerk_auth.py
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

ISSUER = "https://test.clerk.accounts.dev"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(rsa_key, *, iss=ISSUER, exp_delta=3600, sub="user_abc", **extra):
    payload = {
        "iss": iss,
        "sub": sub,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
        **extra,
    }
    return jwt.encode(payload, rsa_key, algorithm="RS256")


@pytest.fixture
def patched(monkeypatch, rsa_key):
    from api.services.auth import clerk_auth

    monkeypatch.setattr(clerk_auth, "CLERK_ISSUER", ISSUER)
    monkeypatch.setattr(
        clerk_auth, "_get_signing_key", lambda token: rsa_key.public_key()
    )
    return clerk_auth


async def test_valid_token_returns_claims(patched, rsa_key):
    claims = await patched.verify_clerk_token(
        f"Bearer {_make_token(rsa_key, email='a@b.com')}"
    )
    assert claims["sub"] == "user_abc"
    assert claims["email"] == "a@b.com"


async def test_missing_header_401(patched):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(None)
    assert exc.value.status_code == 401


async def test_expired_token_401(patched, rsa_key):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(
            f"Bearer {_make_token(rsa_key, exp_delta=-60)}"
        )
    assert exc.value.status_code == 401


async def test_wrong_issuer_401(patched, rsa_key):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(
            f"Bearer {_make_token(rsa_key, iss='https://evil.example.com')}"
        )
    assert exc.value.status_code == 401
```

(The suite's `pytest.ini` already configures `asyncio_mode = auto` — confirm with `grep asyncio api/pytest.ini`; if absent, add `@pytest.mark.asyncio` decorators instead.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest api/tests/test_clerk_auth.py -xvs`
Expected: FAIL — `ImportError` (module doesn't exist)

- [ ] **Step 3: Implement**

```python
# api/services/auth/clerk_auth.py
"""Clerk session-token verification (AUTH_PROVIDER=clerk).

Clerk issues short-lived RS256 session JWTs. We verify them locally against
Clerk's JWKS (cached by PyJWKClient) — no network round-trip per request.
"""

from functools import lru_cache

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from api.constants import CLERK_ISSUER


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(
        f"{CLERK_ISSUER}/.well-known/jwks.json", cache_keys=True, lifespan=3600
    )


def _get_signing_key(token: str):
    """Seam for tests: resolve the public key for this token's `kid`."""
    return _jwks_client().get_signing_key_from_jwt(token).key


async def verify_clerk_token(authorization: str | None) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    try:
        return jwt.decode(
            token,
            _get_signing_key(token),
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            options={"require": ["exp", "iat", "sub"], "verify_aud": False},
            leeway=10,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
```

Add to `api/requirements.txt` (alphabetical position): `svix==1.45.1`. Then `pip install svix==1.45.1`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest api/tests/test_clerk_auth.py -xvs`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add api/services/auth/clerk_auth.py api/tests/test_clerk_auth.py api/requirements.txt
git commit -m "feat(auth): add Clerk JWT verification service"
```

---

### Task 3: Trial-grant service on the local ledger

**Files:**
- Create: `api/services/billing/trial.py`
- Test: `api/tests/test_trial_grant.py`

**Interfaces:**
- Consumes: `api.services.billing.billing_service.credit(organization_id, amount_cents, type, *, description=None, created_by=None, idempotency_key=None)` (exists, `api/services/billing/billing_service.py:99`).
- Produces: `trial.grant_signup_trial(organization_id: int, created_by: int | None = None) -> None` — grants `TRIAL_MINUTES * 100` cents once per org (idempotency key `signup_trial:{organization_id}`), no-op when `TRIAL_MINUTES <= 0` or `BILLING_ENGINE != "local"`.

- [ ] **Step 1: Write the failing test**

Follow the DB-fixture pattern used in `api/tests/test_billing_service.py` (read it first; reuse its org fixture/helpers verbatim — it creates an organization and asserts on `get_balance_cents`).

```python
# api/tests/test_trial_grant.py
import pytest

from api.db import db_client
from api.services.billing import billing_service


@pytest.fixture
def local_billing(monkeypatch):
    monkeypatch.setattr("api.services.billing.trial.BILLING_ENGINE", "local")
    monkeypatch.setattr("api.services.billing.trial.TRIAL_MINUTES", 15)


async def _make_org():
    # Reuse the organization-creation helper pattern from test_billing_service.py.
    org, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id="org_trial_test", user_id=None
    )
    return org


async def test_trial_grant_credits_once(local_billing):
    from api.services.billing.trial import grant_signup_trial

    org = await _make_org()
    await grant_signup_trial(org.id)
    await grant_signup_trial(org.id)  # idempotent — second call is a no-op

    assert await billing_service.get_balance_cents(org.id) == 1500


async def test_trial_grant_disabled_when_zero(local_billing, monkeypatch):
    from api.services.billing.trial import grant_signup_trial

    monkeypatch.setattr("api.services.billing.trial.TRIAL_MINUTES", 0)
    org = await _make_org()
    await grant_signup_trial(org.id)
    assert await billing_service.get_balance_cents(org.id) == 0


async def test_trial_grant_noop_on_mps_engine(local_billing, monkeypatch):
    from api.services.billing.trial import grant_signup_trial

    monkeypatch.setattr("api.services.billing.trial.BILLING_ENGINE", "mps")
    org = await _make_org()
    await grant_signup_trial(org.id)
    assert await billing_service.get_balance_cents(org.id) == 0
```

Adjust `_make_org` to the exact signature in `api/db/organization_client.py` if `user_id=None` is rejected (check how `test_billing_service.py` creates its org and copy that).

- [ ] **Step 2: Run test to verify it fails**

Run: `set -a && source api/.env.test && set +a && python -m pytest api/tests/test_trial_grant.py -xvs`
Expected: FAIL — `ModuleNotFoundError: api.services.billing.trial`

- [ ] **Step 3: Implement**

```python
# api/services/billing/trial.py
"""One-time signup trial grant (spec §6): free minutes, no card."""

from loguru import logger

from api.constants import BILLING_ENGINE, TRIAL_MINUTES
from api.services.billing import billing_service

# 1 minute at 1x burn == 100 ledger cents (spec §4).
CENTS_PER_MINUTE = 100


async def grant_signup_trial(organization_id: int, created_by: int | None = None) -> None:
    if BILLING_ENGINE != "local" or TRIAL_MINUTES <= 0:
        return
    await billing_service.credit(
        organization_id,
        TRIAL_MINUTES * CENTS_PER_MINUTE,
        "grant",
        description=f"Signup trial: {TRIAL_MINUTES} free minutes",
        created_by=created_by,
        idempotency_key=f"signup_trial:{organization_id}",
    )
    logger.info("Granted {}min signup trial to org {}", TRIAL_MINUTES, organization_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest api/tests/test_trial_grant.py -xvs`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add api/services/billing/trial.py api/tests/test_trial_grant.py
git commit -m "feat(billing): one-time signup trial grant on local ledger"
```

---

### Task 4: Platform default model configuration (saas)

**Files:**
- Create: `api/services/configuration/platform_defaults.py`
- Test: `api/tests/test_platform_defaults.py`

**Interfaces:**
- Consumes: `db_client.upsert_configuration(organization_id, key, value)` (used in `api/services/auth/depends.py:150`), `OrganizationConfigurationKey.MODEL_CONFIGURATION_V2` (`api/enums.py`).
- Produces: `platform_defaults.seed_platform_model_configuration(organization_id: int) -> bool` — writes org `MODEL_CONFIGURATION_V2` from `PLATFORM_*` env keys; returns False (and logs a warning) if no platform keys are configured.

Before coding, read the stored V2 config shape: `grep -n "class AIModelConfigurationV2" -A 40 api/schemas/ai_model_configuration.py` and one seeded example from `api/services/configuration/ai_model_configuration.py::convert_legacy_ai_model_configuration_to_v2`. Match that schema exactly — the code below shows intent; field names must match the real `AIModelConfigurationV2` model, validated by constructing the Pydantic object (never a raw dict).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_platform_defaults.py
from api.db import db_client
from api.enums import OrganizationConfigurationKey


async def test_seed_writes_v2_config(monkeypatch):
    from api.services.configuration import platform_defaults

    monkeypatch.setattr(platform_defaults, "PLATFORM_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(platform_defaults, "PLATFORM_DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setattr(platform_defaults, "PLATFORM_ELEVENLABS_API_KEY", "el-test")

    org, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id="org_platform_seed", user_id=None
    )
    assert await platform_defaults.seed_platform_model_configuration(org.id) is True

    stored = await db_client.get_configuration(
        org.id, OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    )
    assert stored is not None
    # Keys are stored server-side; assert they landed in the config blob.
    assert "sk-test" in str(stored)


async def test_seed_skips_without_keys(monkeypatch):
    from api.services.configuration import platform_defaults

    monkeypatch.setattr(platform_defaults, "PLATFORM_OPENAI_API_KEY", None)
    monkeypatch.setattr(platform_defaults, "PLATFORM_DEEPGRAM_API_KEY", None)
    monkeypatch.setattr(platform_defaults, "PLATFORM_ELEVENLABS_API_KEY", None)

    org, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id="org_platform_seed_none", user_id=None
    )
    assert await platform_defaults.seed_platform_model_configuration(org.id) is False
```

(Adjust `get_configuration` to the actual read method name in `api/db/organization_configuration_client.py` — find it with `grep -n "async def get" api/db/organization_configuration_client.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest api/tests/test_platform_defaults.py -xvs`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

```python
# api/services/configuration/platform_defaults.py
"""Seed a new org's model configuration from platform-held provider keys.

In saas mode tenants never supply AI provider keys (spec §7): every new org
gets a working default pipeline (OpenAI LLM + Deepgram STT + ElevenLabs TTS)
backed by the platform's own keys. Superadmin can change any org's config
later through the existing configuration endpoints.
"""

from loguru import logger

from api.constants import (
    PLATFORM_DEEPGRAM_API_KEY,
    PLATFORM_ELEVENLABS_API_KEY,
    PLATFORM_OPENAI_API_KEY,
)
from api.db import db_client
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import AIModelConfigurationV2


def _build_default_v2() -> AIModelConfigurationV2 | None:
    if not (
        PLATFORM_OPENAI_API_KEY
        and PLATFORM_DEEPGRAM_API_KEY
        and PLATFORM_ELEVENLABS_API_KEY
    ):
        return None
    # NOTE: construct via the real schema (validated). Field names below must
    # match api/schemas/ai_model_configuration.py — mirror the structure that
    # convert_legacy_ai_model_configuration_to_v2 produces.
    return AIModelConfigurationV2.model_validate(
        {
            "llm": {
                "provider": "openai",
                "api_key": [PLATFORM_OPENAI_API_KEY],
                "model": "gpt-4o-mini",
            },
            "stt": {
                "provider": "deepgram",
                "api_key": [PLATFORM_DEEPGRAM_API_KEY],
                "model": "nova-2",
            },
            "tts": {
                "provider": "elevenlabs",
                "api_key": [PLATFORM_ELEVENLABS_API_KEY],
                "model": "eleven_turbo_v2_5",
                "voice": "21m00Tcm4TlvDq8ikWAM",
            },
        }
    )


async def seed_platform_model_configuration(organization_id: int) -> bool:
    config = _build_default_v2()
    if config is None:
        logger.warning(
            "No platform AI keys configured; org {} starts without a default "
            "model configuration",
            organization_id,
        )
        return False
    await db_client.upsert_configuration(
        organization_id,
        OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
        config.model_dump(mode="json", exclude_none=True),
    )
    return True
```

If `AIModelConfigurationV2.model_validate` rejects this shape, print one existing org's stored `MODEL_CONFIGURATION_V2` from a dev DB (or read the converter's output structure) and correct the dict — the test asserting the stored blob keeps you honest.

- [ ] **Step 4: Run tests, commit**

Run: `python -m pytest api/tests/test_platform_defaults.py -xvs` → 2 PASS

```bash
git add api/services/configuration/platform_defaults.py api/tests/test_platform_defaults.py
git commit -m "feat(saas): platform-key default model configuration for new orgs"
```

---

### Task 5: Clerk auth path in `get_user` (provision + trial + seed)

**Files:**
- Modify: `api/services/auth/depends.py` (branch in `get_user` after the `AUTH_PROVIDER == "local"` check at line 33; new `_handle_clerk_auth` helper below `_handle_oss_auth`)
- Test: `api/tests/test_clerk_depends.py`

**Interfaces:**
- Consumes: `verify_clerk_token` (Task 2), `grant_signup_trial` (Task 3), `seed_platform_model_configuration` (Task 4), existing `db_client.get_or_create_user_by_provider_id`, `get_or_create_organization_by_provider_id`, `add_user_to_organization`, `update_user_selected_organization`, `update_user_email`.
- Produces: `AUTH_PROVIDER=clerk` requests resolve to a `UserModel` with a selected org; first login creates org + admin membership + trial grant + platform config seed.

- [ ] **Step 1: Write the failing test**

Mirror the structure of `api/tests/test_auth_depends.py` (read it first and reuse its fixtures/DB setup). Core cases:

```python
# api/tests/test_clerk_depends.py
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

CLAIMS = {"sub": "user_clerk_1", "email": "clerk@example.com"}


@pytest.fixture
def clerk_mode(monkeypatch):
    monkeypatch.setattr("api.services.auth.depends.AUTH_PROVIDER", "clerk")


async def _call_get_user():
    from api.services.auth.depends import get_user

    return await get_user(authorization="Bearer fake", x_api_key=None)


async def test_first_login_provisions_user_org_trial_and_config(clerk_mode):
    with (
        patch(
            "api.services.auth.depends.verify_clerk_token",
            AsyncMock(return_value=CLAIMS),
        ),
        patch(
            "api.services.auth.depends.grant_signup_trial", AsyncMock()
        ) as trial,
        patch(
            "api.services.auth.depends.seed_platform_model_configuration",
            AsyncMock(),
        ) as seed,
    ):
        user = await _call_get_user()

    assert user.provider_id == "user_clerk_1"
    assert user.email == "clerk@example.com"
    assert user.selected_organization_id is not None
    trial.assert_awaited_once_with(user.selected_organization_id, created_by=user.id)
    seed.assert_awaited_once_with(user.selected_organization_id)

    # Creator must be org admin (same invariant as local signup).
    from api.db import db_client

    role = await db_client.get_member_role(user.selected_organization_id, user.id)
    assert role == "admin"


async def test_second_login_is_idempotent(clerk_mode):
    with (
        patch(
            "api.services.auth.depends.verify_clerk_token",
            AsyncMock(return_value=CLAIMS),
        ),
        patch("api.services.auth.depends.grant_signup_trial", AsyncMock()) as trial,
        patch(
            "api.services.auth.depends.seed_platform_model_configuration",
            AsyncMock(),
        ) as seed,
    ):
        first = await _call_get_user()
        second = await _call_get_user()

    assert first.id == second.id
    # Provisioning side-effects fire only on org creation.
    assert trial.await_count <= 1
    assert seed.await_count <= 1


async def test_invalid_token_401(clerk_mode):
    with patch(
        "api.services.auth.depends.verify_clerk_token",
        AsyncMock(side_effect=HTTPException(status_code=401, detail="bad")),
    ):
        with pytest.raises(HTTPException) as exc:
            await _call_get_user()
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest api/tests/test_clerk_depends.py -xvs`
Expected: FAIL — `ImportError: cannot import name 'verify_clerk_token' from 'api.services.auth.depends'` (patch target missing)

- [ ] **Step 3: Implement the branch**

In `api/services/auth/depends.py`:

Add imports (top of file):

```python
from api.services.auth.clerk_auth import verify_clerk_token
from api.services.billing.trial import grant_signup_trial
from api.services.configuration.platform_defaults import (
    seed_platform_model_configuration,
)
```

In `get_user`, insert after the `AUTH_PROVIDER == "local"` block (line 33-34):

```python
    # ------------------------------------------------------------------
    # Clerk-hosted auth (saas mode)
    # ------------------------------------------------------------------
    if AUTH_PROVIDER == "clerk":
        return await _handle_clerk_auth(authorization)
```

Add the helper below `_handle_oss_auth`:

```python
async def _handle_clerk_auth(authorization: str | None) -> UserModel:
    """Resolve a Clerk session token to a local user, provisioning on first login.

    Clerk is identity-only (spec §2): orgs/roles stay internal. Each Clerk user
    gets one auto-created org (provider_id `org_<clerk_user_id>`), mirroring the
    local-auth signup invariants: creator is admin, selected org set, trial
    granted, platform model config seeded.
    """
    claims = await verify_clerk_token(authorization)
    provider_id = claims["sub"]

    try:
        user_model, user_was_created = await db_client.get_or_create_user_by_provider_id(
            provider_id
        )
        clerk_email = claims.get("email")
        if clerk_email and user_model.email != clerk_email:
            await db_client.update_user_email(user_model.id, clerk_email)
            user_model.email = clerk_email
        if user_was_created:
            capture_event(
                distinct_id=provider_id,
                event=PostHogEvent.SIGNED_UP,
                properties={"auth_provider": "clerk"},
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error while creating user from database {e}"
        )

    try:
        organization, org_was_created = (
            await db_client.get_or_create_organization_by_provider_id(
                org_provider_id=f"org_{provider_id}", user_id=user_model.id
            )
        )
        if user_model.selected_organization_id != organization.id:
            await db_client.add_user_to_organization(
                user_model.id,
                organization.id,
                role="admin" if org_was_created else "member",
            )
            await db_client.update_user_selected_organization(
                user_model.id, organization.id
            )
            user_model.selected_organization_id = organization.id

        if org_was_created:
            try:
                await grant_signup_trial(organization.id, created_by=user_model.id)
            except Exception:
                logger.warning(
                    "Trial grant failed for org {}", organization.id, exc_info=True
                )
            try:
                await seed_platform_model_configuration(organization.id)
            except Exception:
                logger.warning(
                    "Platform config seed failed for org {}",
                    organization.id,
                    exc_info=True,
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to map user to organization: {exc}"
        )

    return user_model
```

Note the mock in the test awaits `trial.assert_awaited_once_with(org_id, created_by=user.id)` — signature must match exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest api/tests/test_clerk_depends.py api/tests/test_auth_depends.py -xvs`
Expected: all PASS (existing local/stack tests must stay green)

- [ ] **Step 5: Commit**

```bash
git add api/services/auth/depends.py api/tests/test_clerk_depends.py
git commit -m "feat(auth): Clerk auth path with lazy provisioning, trial grant, platform config seed"
```

---

### Task 6: Clerk webhooks (`user.updated`, `user.deleted`)

**Files:**
- Create: `api/routes/clerk_webhooks.py`
- Modify: `api/routes/main.py` (import + `router.include_router(clerk_webhooks_router)` alongside the other webhook router)
- Test: `api/tests/test_clerk_webhooks.py`

**Interfaces:**
- Consumes: `svix.webhooks.Webhook` (verification), `CLERK_WEBHOOK_SECRET`, `db_client.get_user_by_provider_id` (verify exact name via `grep -n "provider_id" api/db/user_client.py`), `db_client.update_user_email`, `db_client.get_api_keys_by_organization` + `archive_api_key` (`api/db/api_key_client.py:38,86`).
- Produces: `POST /api/v1/webhooks/clerk` — 204 on handled/ignored events, 401 on bad signature, 404 in non-saas mode.

- [ ] **Step 1: Write the failing test**

First read `api/tests/test_signup_creator_is_admin.py` and copy its app/HTTP-client fixture pattern (async client against `api.app:app`); the tests below assume an `async_client` fixture with that shape — rename to match the suite's convention.

```python
# api/tests/test_clerk_webhooks.py
import json
import time

import pytest

from api.db import db_client

SECRET = "whsec_" + ("a" * 32)


def _signed_headers(body: str) -> dict:
    from svix.webhooks import Webhook

    msg_id = "msg_test_1"
    timestamp = str(int(time.time()))
    signature = Webhook(SECRET).sign(msg_id, timestamp, body)
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": signature,
        "content-type": "application/json",
    }


@pytest.fixture(autouse=True)
def saas_webhook_env(monkeypatch):
    monkeypatch.setattr("api.routes.clerk_webhooks.CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr("api.routes.clerk_webhooks.IS_SAAS_MODE", True)


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


async def test_user_updated_syncs_email(async_client):
    user, _ = await db_client.get_or_create_user_by_provider_id("user_wh_1")
    body = json.dumps(
        {
            "type": "user.updated",
            "data": {
                "id": "user_wh_1",
                "primary_email_address_id": "em_1",
                "email_addresses": [
                    {"id": "em_1", "email_address": "new@example.com"}
                ],
            },
        }
    )
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204
    refreshed = await db_client.get_user_by_id(user.id)
    assert refreshed.email == "new@example.com"


async def test_user_deleted_archives_api_keys(async_client):
    # Provision user + org + one API key, then delete via webhook.
    user, _ = await db_client.get_or_create_user_by_provider_id("user_wh_2")
    org, _ = await db_client.get_or_create_organization_by_provider_id(
        org_provider_id="org_user_wh_2", user_id=user.id
    )
    await db_client.update_user_selected_organization(user.id, org.id)
    await db_client.create_api_key(
        organization_id=org.id, name="k", created_by=user.id
    )  # match real signature per api/db/api_key_client.py:12

    body = json.dumps({"type": "user.deleted", "data": {"id": "user_wh_2"}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204
    keys = await db_client.get_api_keys_by_organization(org.id)
    assert all(not k.is_active for k in keys)  # adjust to the model's archive flag


async def test_unknown_event_is_accepted(async_client):
    body = json.dumps({"type": "session.created", "data": {"id": "sess_1"}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 204


async def test_route_hidden_outside_saas(async_client, monkeypatch):
    monkeypatch.setattr("api.routes.clerk_webhooks.IS_SAAS_MODE", False)
    body = json.dumps({"type": "user.updated", "data": {}})
    resp = await async_client.post(
        "/api/v1/webhooks/clerk", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 404
```

Two call sites marked with comments must be adjusted to the real signatures before running: `create_api_key` (`api/db/api_key_client.py:12`) and the archive flag name on `APIKeyModel` (check `api/db/models.py`).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest api/tests/test_clerk_webhooks.py -xvs`
Expected: FAIL — route module missing

- [ ] **Step 3: Implement**

```python
# api/routes/clerk_webhooks.py
"""Clerk → app sync webhooks (spec §2). Svix-signed."""

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
    except WebhookVerificationError:
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
```

Wire in `api/routes/main.py`: add `from api.routes.clerk_webhooks import router as clerk_webhooks_router` and `router.include_router(clerk_webhooks_router)` next to `webhooks_router`.

If `db_client.get_user_by_provider_id` doesn't exist under that name, find the actual getter (`grep -n "def get_user" api/db/user_client.py`) and use it; add a thin client method only if none exists.

- [ ] **Step 4: Run tests, commit**

Run: `python -m pytest api/tests/test_clerk_webhooks.py -xvs` → all PASS

```bash
git add api/routes/clerk_webhooks.py api/routes/main.py api/tests/test_clerk_webhooks.py
git commit -m "feat(auth): Clerk sync webhooks with svix signature verification"
```

---

### Task 7: Balance endpoint (ungated by payments flag)

**Files:**
- Create: `api/routes/billing_balance.py` (separate module: `routes/billing.py` is 404-gated by `BILLING_PAYMENTS_ENABLED`, which stays off in phase 1)
- Modify: `api/routes/main.py` (include router)
- Test: `api/tests/test_billing_balance.py`

**Interfaces:**
- Consumes: `billing_service.get_balance_cents(organization_id)` (`api/services/billing/billing_service.py:78`), `get_user_with_selected_organization` dependency.
- Produces: `GET /api/v1/billing/balance` → `{"balance_cents": int, "minutes_equivalent": float}` (404 when `BILLING_ENGINE != "local"`). The UI (Tasks 12) reads this.

- [ ] **Step 1: Write the failing test**

Copy the authenticated-route test pattern from `api/tests/test_signup_creator_is_admin.py` (signup via `/api/v1/auth/signup` in local mode, then call with the returned Bearer token). Cases:
1. fresh org → `balance_cents == 0`, `minutes_equivalent == 0`
2. after `billing_service.credit(org_id, 1500, "grant")` → `{"balance_cents": 1500, "minutes_equivalent": 15.0}`
3. with `BILLING_ENGINE` monkeypatched to `"mps"` in the route module → 404

```python
# api/tests/test_billing_balance.py — core assertion shape
async def test_balance_reflects_ledger(auth_client, org_id):
    from api.services.billing import billing_service

    await billing_service.credit(org_id, 1500, "grant", description="test")
    resp = await auth_client.get("/api/v1/billing/balance")
    assert resp.status_code == 200
    assert resp.json() == {"balance_cents": 1500, "minutes_equivalent": 15.0}
```

- [ ] **Step 2: Run to verify failure** — 404 (route absent)

- [ ] **Step 3: Implement**

```python
# api/routes/billing_balance.py
"""Credit balance read — available whenever the local ledger is on,
independent of BILLING_PAYMENTS_ENABLED (which gates purchase routes only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.constants import BILLING_ENGINE
from api.db.models import UserModel
from api.services.auth.depends import get_user_with_selected_organization
from api.services.billing import billing_service

router = APIRouter(prefix="/billing", tags=["billing"])


class BalanceResponse(BaseModel):
    balance_cents: int
    minutes_equivalent: float


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    user: Annotated[UserModel, Depends(get_user_with_selected_organization)],
) -> BalanceResponse:
    if BILLING_ENGINE != "local":
        raise HTTPException(status_code=404)
    balance = await billing_service.get_balance_cents(user.selected_organization_id)
    return BalanceResponse(
        balance_cents=balance, minutes_equivalent=round(balance / 100, 1)
    )
```

Wire into `api/routes/main.py` next to `billing_router`.

- [ ] **Step 4: Run tests, commit**

```bash
git add api/routes/billing_balance.py api/routes/main.py api/tests/test_billing_balance.py
git commit -m "feat(billing): balance endpoint independent of payments gating"
```

---

### Task 8: Frontend Clerk provider + middleware + auth pages

**Files:**
- Modify: `ui/package.json` (`cd ui && npm install @clerk/nextjs`)
- Create: `ui/src/lib/auth/providers/ClerkProviderWrapper.tsx`
- Modify: `ui/src/lib/auth/providers/AuthProvider.tsx` (add lazy import + `clerk` branch)
- Modify: `ui/src/middleware.ts` (Clerk branch)
- Modify: `ui/src/app/auth/login/page.tsx`, `ui/src/app/auth/signup/page.tsx` (render Clerk components when provider is clerk)
- Modify: `ui/.env.example` (add `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=`, `CLERK_SECRET_KEY=`)

**Interfaces:**
- Consumes: backend `/api/v1/health` `auth_provider: "clerk"` (comes free from `AUTH_PROVIDER=clerk` — `HealthResponse` already echoes it), `AuthContextType` (`ui/src/lib/auth/providers/AuthProvider.tsx:11-22`).
- Produces: `useAuth()` works unchanged for every consumer; `getAccessToken()` returns a Clerk session token accepted by Task 5's backend path.

No component-level unit-test harness exists in `ui/` — verification for frontend tasks is `npm run build` (type-check + lint) plus the manual smoke flow in Task 13.

- [ ] **Step 1: Install and create the wrapper**

```tsx
// ui/src/lib/auth/providers/ClerkProviderWrapper.tsx
'use client';

import { ClerkProvider, useAuth as useClerkAuth, useClerk, useUser } from '@clerk/nextjs';
import React, { useMemo } from 'react';

import type { AuthUser } from '../types';
import { AuthContext } from './AuthProvider';

function ClerkAuthBridge({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useClerkAuth();
  const { user } = useUser();
  const clerk = useClerk();

  const contextValue = useMemo(
    () => ({
      user: (user
        ? {
            id: user.id,
            email: user.primaryEmailAddress?.emailAddress ?? '',
            name: user.fullName ?? undefined,
          }
        : null) as AuthUser | null,
      isAuthenticated: !!isSignedIn,
      loading: !isLoaded,
      getAccessToken: async () => (await getToken()) ?? '',
      redirectToLogin: () => {
        window.location.href = '/auth/login';
      },
      logout: async () => {
        await clerk.signOut();
        window.location.href = '/auth/login';
      },
      provider: 'clerk' as const,
    }),
    [user, isSignedIn, isLoaded, getToken, clerk],
  );

  return <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>;
}

export function ClerkProviderWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ClerkProvider signInUrl="/auth/login" signUpUrl="/auth/signup">
      <ClerkAuthBridge>{children}</ClerkAuthBridge>
    </ClerkProvider>
  );
}
```

Check `ui/src/lib/auth/types.ts` for the exact `AuthUser` shape and adapt the mapped object (it may require fields like `provider_id`).

- [ ] **Step 2: Branch in AuthProvider**

In `ui/src/lib/auth/providers/AuthProvider.tsx`, after the `LocalProviderWrapper` lazy import (line 33-37):

```tsx
const ClerkProviderWrapper = lazy(() =>
  import('./ClerkProviderWrapper').then(module => ({
    default: module.ClerkProviderWrapper
  }))
);
```

and after the `authProvider === 'stack'` branch (line 66-74):

```tsx
  if (authProvider === 'clerk') {
    return (
      <Suspense fallback={LoadingFallback}>
        <ClerkProviderWrapper>
          {children}
        </ClerkProviderWrapper>
      </Suspense>
    );
  }
```

- [ ] **Step 3: Middleware branch**

Replace the body of `middleware` in `ui/src/middleware.ts` (keep `fetchAuthProvider` and `config` as-is):

```ts
import { clerkMiddleware, createRouteMatcher } from '@clerk/nextjs/server';

const isPublicClerkRoute = createRouteMatcher(['/auth/login(.*)', '/auth/signup(.*)']);

const protectedClerkMiddleware = clerkMiddleware(async (auth, request) => {
  if (!isPublicClerkRoute(request)) {
    await auth.protect({ unauthenticatedUrl: new URL('/auth/login', request.url).toString() });
  }
});

export async function middleware(request: NextRequest, event: NextFetchEvent) {
  const authProvider = await fetchAuthProvider();

  if (authProvider === 'clerk') {
    return protectedClerkMiddleware(request, event);
  }

  if (authProvider !== 'local') {
    return NextResponse.next();
  }
  // ...existing OSS cookie logic unchanged...
}
```

(Import `NextFetchEvent` from `next/server`.)

- [ ] **Step 4: Auth pages**

In `ui/src/app/auth/login/page.tsx` and `signup/page.tsx`: fetch the provider via the existing client hook/config (`fetch('/api/config/auth')` as `AuthProvider` does, or read from a small `useEffect`), and when it's `clerk` render:

```tsx
import { SignIn } from '@clerk/nextjs';
// inside the page component, clerk branch:
return (
  <div className="flex min-h-screen items-center justify-center">
    <SignIn routing="hash" signUpUrl="/auth/signup" fallbackRedirectUrl="/after-sign-in" />
  </div>
);
```

(`SignUp` equivalently on the signup page with `signInUrl="/auth/login"`.) The existing local email/password form stays as the non-clerk branch. In the Clerk dashboard, configure: email verification **required** at sign-up (hard gate, spec §2), Google OAuth enabled.

- [ ] **Step 5: Verify + commit**

Run: `cd ui && npm run build`
Expected: clean build (Clerk publishable key can be a dummy `pk_test_...` in `ui/.env` for build).

```bash
git add ui/package.json ui/package-lock.json ui/src/lib/auth/providers/ ui/src/middleware.ts ui/src/app/auth/ ui/.env.example
git commit -m "feat(ui): Clerk auth provider, middleware protection, hosted auth pages"
```

---

### Task 9: Profile page (Clerk UserProfile + workspace profile)

**Files:**
- Modify: `api/enums.py` (add `WORKSPACE_PROFILE = "WORKSPACE_PROFILE"` to `UserConfigurationKey`, line 99-105)
- Create: `api/routes/workspace_profile.py`; wire in `api/routes/main.py`
- Test: `api/tests/test_workspace_profile.py`
- Create: `ui/src/app/profile/page.tsx`
- Modify: sidebar user menu (`ui/src/components/layout/AppSidebar.tsx` footer) — link to `/profile`, and render Clerk `<UserButton />` when `provider === 'clerk'`

**Interfaces:**
- Produces: `GET/PUT /api/v1/user/workspace-profile` with body/response `{"company_name": str | None, "timezone": str | None}` stored under `UserConfigurationKey.WORKSPACE_PROFILE` in the existing per-user keyed JSON store (`db_client.get_user_configuration_by_key` / `upsert_user_configuration_by_key` — verify exact method names in `api/db/user_configuration_client.py` via `grep -n "async def" api/db/user_configuration_client.py`; the onboarding-state route in `api/routes/user.py` shows the working read/write pattern to copy).

- [ ] **Step 1: Backend TDD**

Test (authenticated-route pattern as in Task 7): PUT `{"company_name": "Acme", "timezone": "Asia/Kolkata"}` → 200; GET returns the same; GET before any PUT returns `{"company_name": null, "timezone": null}`; invalid timezone (`"Not/AZone"`) → 422.

Implementation:

```python
# api/routes/workspace_profile.py
from typing import Annotated
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from api.db import db_client
from api.db.models import UserModel
from api.enums import UserConfigurationKey
from api.services.auth.depends import get_user

router = APIRouter(prefix="/user", tags=["user"])


class WorkspaceProfile(BaseModel):
    company_name: str | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v):
        if v is not None and v not in available_timezones():
            raise ValueError("Unknown IANA timezone")
        return v


@router.get("/workspace-profile", response_model=WorkspaceProfile)
async def get_workspace_profile(user: Annotated[UserModel, Depends(get_user)]):
    stored = await db_client.get_user_configuration_by_key(
        user.id, UserConfigurationKey.WORKSPACE_PROFILE.value
    )
    return WorkspaceProfile(**(stored or {}))


@router.put("/workspace-profile", response_model=WorkspaceProfile)
async def put_workspace_profile(
    body: WorkspaceProfile, user: Annotated[UserModel, Depends(get_user)]
):
    await db_client.upsert_user_configuration_by_key(
        user.id,
        UserConfigurationKey.WORKSPACE_PROFILE.value,
        body.model_dump(),
    )
    return body
```

(Adapt the two `db_client` method names to the real keyed-store accessors found in step-1 grep; the onboarding endpoints in `routes/user.py` use them.)

- [ ] **Step 2: Frontend page**

`ui/src/app/profile/page.tsx`: when `useAuth().provider === 'clerk'` render Clerk's `<UserProfile routing="hash" />` (name, avatar upload, email, password, linked Google, delete account — all Clerk-native) above a "Workspace" card with two inputs (Company name, Timezone select using `Intl.supportedValuesOf('timeZone')`) that GET/PUT `/api/v1/user/workspace-profile` through the generated client (`npm run generate-client` after backend lands; check `response.error` per convention). For non-clerk providers render only the Workspace card.

- [ ] **Step 3: Verify + commit**

Run: `python -m pytest api/tests/test_workspace_profile.py -xvs && cd ui && npm run generate-client && npm run build`

```bash
git add api/enums.py api/routes/workspace_profile.py api/routes/main.py api/tests/test_workspace_profile.py ui/src/app/profile/ ui/src/client/ ui/src/components/layout/AppSidebar.tsx
git commit -m "feat(profile): Clerk user profile page and workspace profile settings"
```

---

### Task 10: saas-mode UI gating (hide MPS/upsells/key fields) + trial meter

**Files:**
- Modify: `ui/src/app/billing/page.tsx` (saas branch: show local balance via `GET /api/v1/billing/balance` + existing ledger table; hide MPS purchase URL button + `DograhCreditsCard` + app.dograh.com banner)
- Modify: `ui/src/components/billing/DograhCreditsCard.tsx` call sites (render nothing when `deploymentMode === 'saas'`)
- Modify: `ui/src/components/ServiceConfigurationForm.tsx` and `ui/src/components/AIModelConfigurationV2Editor.tsx` (hide API-key inputs when `deploymentMode === 'saas'`; keys are platform-managed — backend masking already prevents leakage on read, `api/routes/organization.py:244`)
- Create: `ui/src/components/billing/MinutesRemainingCard.tsx`; render on `ui/src/app/overview/page.tsx`
- Modify: `ui/src/app/api/config/version/route.ts` consumers — confirm `deploymentMode` propagates `"saas"` through `AppConfigContext` (it passes the raw health value; verify, no change expected)

**Interfaces:**
- Consumes: `deploymentMode` from `AppConfigContext` (`ui/src/context/`), `getBalanceApiV1BillingBalanceGet` from the regenerated client (Task 7).

- [ ] **Step 1: MinutesRemainingCard**

```tsx
// ui/src/components/billing/MinutesRemainingCard.tsx
'use client';

import { useEffect, useRef, useState } from 'react';

import { getBalanceApiV1BillingBalanceGet } from '@/client/sdk.gen';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useAuth } from '@/lib/auth/providers/AuthProvider';

export function MinutesRemainingCard() {
  const { user, loading: authLoading } = useAuth();
  const [minutes, setMinutes] = useState<number | null>(null);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (authLoading || !user || hasFetched.current) return;
    hasFetched.current = true;
    getBalanceApiV1BillingBalanceGet().then((response) => {
      if (!response.error && response.data) {
        setMinutes(response.data.minutes_equivalent);
      }
    });
  }, [authLoading, user]);

  if (minutes === null) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Call minutes remaining</CardTitle>
      </CardHeader>
      <CardContent>
        <span className="text-3xl font-semibold">{minutes}</span>
        <span className="ml-1 text-muted-foreground">min at standard rate</span>
      </CardContent>
    </Card>
  );
}
```

Render it on `/overview` and `/billing` only when `deploymentMode === 'saas'`.

- [ ] **Step 2: Gating sweep**

Find every saas-inappropriate element and gate it: `grep -rn "dograh.com\|DograhCredits\|mps" ui/src/app/billing ui/src/components/billing ui/src/components/ServiceConfigurationForm.tsx --include=*.tsx -i`. For each hit, wrap in `deploymentMode !== 'saas'` (or remove when dead in all modes). In `ServiceConfigurationForm.tsx`/`AIModelConfigurationV2Editor.tsx`, hide key inputs behind `deploymentMode !== 'saas'` while leaving provider/model/voice pickers fully visible (spec §7: full model freedom, no key handling).

- [ ] **Step 3: Verify + commit**

Run: `cd ui && npm run build`. Manual: with backend in saas mode, `/billing` shows minutes + ledger, no purchase/MPS UI; model config shows providers but no key fields.

```bash
git add ui/src/app/billing ui/src/app/overview ui/src/components/billing ui/src/components/ServiceConfigurationForm.tsx ui/src/components/AIModelConfigurationV2Editor.tsx
git commit -m "feat(ui): saas-mode gating - minutes meter, hide MPS/upsell/key fields"
```

---

### Task 11: Rebrand to VoxAgent

**Files:**
- Create: `ui/src/constants/brand.ts`
- Modify: every user-visible "Dograh" string in `ui/src/` (sweep below), `ui/src/app/layout.tsx` metadata, `ui/public/` logo reference in `AppSidebar.tsx`/`AuthShell.tsx`
- Modify: `api/app.py` FastAPI `title` → use `BRAND_NAME` from `api/constants.py` (Task 1)

- [ ] **Step 1: Brand constant**

```ts
// ui/src/constants/brand.ts
export const BRAND_NAME = 'VoxAgent';
export const BRAND_TAGLINE = 'Build AI voice agents that call for you';
```

- [ ] **Step 2: Sweep**

Run: `grep -rln "Dograh\|dograh" ui/src --include='*.tsx' --include='*.ts' | grep -v client/ | grep -v node_modules`

For each file: replace **user-visible** strings (titles, headers, alt text, empty-state copy, `metadata.title`) with `BRAND_NAME` imports; leave internal identifiers (cookie names like `dograh_auth_token`, API paths, env var names) untouched in phase 1 — renaming those is churn with migration cost and zero user visibility. Replace the logo image usage with a text wordmark (`<span className="text-xl font-bold">{BRAND_NAME}</span>`) until a real logo exists. Sidebar OSS "update available" check (`useLatestReleaseVersion`) must not render in saas mode (it points at Dograh's GitHub releases) — it's already gated to `deploymentMode === "oss"`, verify.

- [ ] **Step 3: Verify + commit**

Run: `cd ui && npm run build`, then visually: no "Dograh" anywhere in the rendered app in saas mode (spot-check `/`, `/overview`, `/billing`, `/auth/login`, sidebar, tab title).

```bash
git add ui/src api/app.py
git commit -m "feat(brand): rebrand UI to VoxAgent behind brand constants"
```

---

### Task 12: Env templates, compose, and setup docs

**Files:**
- Modify: `api/.env.example` (add saas block), `ui/.env.example` (Clerk keys — done in Task 8, verify)
- Create: `docs/SAAS_SETUP.md`

- [ ] **Step 1: `api/.env.example` additions**

```bash
# --- SaaS deployment (DEPLOYMENT_MODE=saas) ---
# DEPLOYMENT_MODE=saas
# AUTH_PROVIDER=clerk
# BILLING_ENGINE=local
# OSS_JWT_SECRET=<generate: openssl rand -hex 32>
# CORS_ALLOWED_ORIGINS=https://app.yourdomain.com
# CLERK_ISSUER=https://your-app.clerk.accounts.dev
# CLERK_WEBHOOK_SECRET=whsec_...
# TRIAL_MINUTES=15
# PLATFORM_OPENAI_API_KEY=sk-...
# PLATFORM_DEEPGRAM_API_KEY=...
# PLATFORM_ELEVENLABS_API_KEY=...
```

- [ ] **Step 2: `docs/SAAS_SETUP.md`**

Write a runbook covering: Clerk dashboard setup (application, **require email verification at sign-up**, enable Google OAuth, webhook endpoint `https://<api-domain>/api/v1/webhooks/clerk` subscribed to `user.updated` + `user.deleted`, copy issuer/publishable/secret/webhook-secret), env var table (backend + frontend), local saas-mode run instructions (`docker-compose-local.yaml` infra + both apps with saas env), and the pricing prerequisite: **seed a global pricing rule** via the existing superadmin endpoints (`POST /api/v1/billing-admin/pricing-rules` — check exact path in `api/routes/billing_admin.py`) because unpriced calls fail closed; include a curl example creating a default 10¢/min rule.

- [ ] **Step 3: Commit**

```bash
git add api/.env.example docs/SAAS_SETUP.md ui/.env.example
git commit -m "docs(saas): setup runbook and env templates for saas mode"
```

---

### Task 13: End-to-end smoke verification

**Files:**
- Create: `docs/superpowers/plans/phase1-smoke-checklist.md` (filled in during the run)

- [ ] **Step 1: Full backend suite**

Run: `set -a && source api/.env.test && set +a && python -m pytest api/tests/ -x -q`
Expected: everything green (including all pre-existing tests — OSS behavior unchanged).

- [ ] **Step 2: Manual smoke in saas mode**

Start infra (`docker compose -f docker-compose-local.yaml up -d`), run migrations, start api + ui with saas env (real Clerk test-instance keys, at least `PLATFORM_OPENAI_API_KEY`/`PLATFORM_DEEPGRAM_API_KEY`/`PLATFORM_ELEVENLABS_API_KEY` set, one pricing rule seeded). Walk through and record each item:

1. Unauthenticated `/overview` → redirected to `/auth/login` (Clerk).
2. Sign up with a new email → verification code required (hard gate) → lands in app.
3. Sign up/in with Google → works.
4. `/overview` shows "Call minutes remaining: 15".
5. Create an agent from template; open builder; model config shows providers, **no key fields**.
6. Web test call connects and the agent talks (platform keys + trial minutes); after the call, balance drops and `/billing` ledger shows the debit.
7. `/profile`: change name, upload avatar, change password (Clerk); save company + timezone (workspace card) and reload — persisted.
8. Delete the Clerk test user from the Clerk dashboard → webhook archives the org's API keys (verify in DB or superadmin).
9. No "Dograh" visible anywhere; tab title and sidebar say VoxAgent.
10. Boot the API with `DEPLOYMENT_MODE=saas` but `CLERK_ISSUER` unset → refuses to start with a clear aggregated error.
11. Boot everything in plain OSS mode (`DEPLOYMENT_MODE=oss`, `AUTH_PROVIDER=local`) → email/password login still works, UI unchanged.

- [ ] **Step 3: Fix anything that failed, re-run, commit the checklist**

```bash
git add docs/superpowers/plans/phase1-smoke-checklist.md
git commit -m "test(saas): phase 1 end-to-end smoke checklist results"
```
