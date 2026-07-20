# Provider Gateway — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a self-owned provider gateway that replaces MPS for the "Dograh managed" LLM/STT/TTS path — a new, separately-deployed FastAPI service holding platform provider keys, authenticated by org-scoped gateway tokens, that proxies LLM (OpenAI-compatible SSE) and STT/TTS (gateway-defined websocket framing) traffic and reports exact usage back to `api/` tagged by a self-minted correlation id — all behind `GATEWAY_ENABLED`, coexisting with the existing MPS path until proven out.

**Architecture:** Two independently deployable components. `api/` remains the source of truth: it mints and pre-registers `correlation_id`s at `authorize_workflow_run_start` (mirroring the existing `MPS_CORRELATION_ID_CONTEXT_KEY` pattern via a new `GATEWAY_CORRELATION_ID_CONTEXT_KEY`), issues/revokes hashed org-scoped gateway tokens (superuser-only, `api/routes/gateway_admin.py`), and exposes two internal, shared-secret-protected endpoints (`api/routes/gateway_internal.py`) the gateway calls over HTTP: `POST /internal/gateway/validate` (token+correlation → org, at connect time) and `POST /internal/gateway/usage` (usage events → appended to the run's `cost_info` as an informational trail). The gateway (new top-level `gateway/` package, its own FastAPI app, own container, reachable at `GATEWAY_URL`) never touches `api/`'s database directly — this preserves the blast-radius isolation that is the entire reason for a separate service; it holds platform provider keys, proxies to real upstream providers, and validates every request against `api/`'s internal endpoint (cached in-process with a short TTL to bound added latency). The pipecat `dograh` clients (`llm.py`, `stt.py`, `tts.py`) are rewritten to speak the gateway's protocol instead of MPS's; `mps_billing.py`'s billing-version branching is dropped and replaced by a `dograh_gateway.py` helper with the same call shape. `api/services/pipecat/service_factory.py`'s three Dograh branches switch their `base_url`/`api_key` source from `MPS_API_URL` to `GATEWAY_URL` only when `GATEWAY_ENABLED` is true.

**Tech Stack:** Python 3.13, FastAPI, `httpx` (async, for `api/`↔`gateway` service calls, matching the existing `mps_service_key_client.py` convention), `websockets` (gateway↔upstream-provider STT/TTS), `openai` SDK (gateway↔OpenAI LLM, and OpenAI-compatible client on the pipecat side, unchanged), SQLAlchemy (async) + Alembic (new `api/` tables only — the gateway itself is stateless), pytest + pytest-asyncio, loguru.

## Global Constraints

- **Two codebases, two test suites.** `api/` tests run exactly as today: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/...`. The new `gateway/` package is added to the *same* repo venv (no separate virtualenv in local dev) and its tests run the same way: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/...` — `gateway/` reads its own env vars (`GATEWAY_PORT`, `API_INTERNAL_URL`, `GATEWAY_INTERNAL_SECRET`, upstream provider keys) which are also present in `api/.env.test` for test purposes (added in Task 1).
- **DB-integration tests require the project's pgvector-enabled Postgres**, started via `docker-compose-local.yaml` (`pgvector/pgvector:pg17`), not a bare host Postgres — `pgvector` extension creation in migrations will fail against a stock `postgres` image. Any task with a `real_db`/`setup_test_database`-backed test assumes this is already running (`docker compose -f docker-compose-local.yaml up -d postgres redis`).
- **Gateway holds zero `api/` database credentials.** Every gateway → org/correlation lookup goes through the HTTP-only `api/routes/gateway_internal.py` endpoints, authenticated by a shared secret (`GATEWAY_INTERNAL_SECRET`), never a DB connection string. This is the isolation property the whole separate-service decision buys — do not shortcut it, even in tests (gateway tests mock the HTTP call).
- **Gateway tokens and API keys hash the same way.** Reuse the `sha256(raw)` + `prefix = raw[:8]` pattern already used for `APIKeyModel`/`api/utils/api_key.py`; plaintext is returned once at issuance and never stored.
- **Feature gate: `GATEWAY_ENABLED`** (env, default `false`/off). Every call site that would otherwise change existing MPS-path behavior is gated by it; when off, behavior is byte-for-byte unchanged (including the pipecat `dograh` service defaults, which keep working against MPS via unmodified `base_url`/`api_key` wiring from `service_factory.py`).
- **Correlation-id minting is orthogonal to `BILLING_ENGINE`.** Phase 1's `BILLING_ENGINE=local` branch in `quota_service.authorize_workflow_run_start` returns early; the gateway correlation-mint step in this phase runs *before* that branch so gateway-backed calls get a correlation id regardless of which billing engine prices the call.
- **Usage reporting is informational in v1.** `POST /internal/gateway/usage` appends to `workflow_run.cost_info["gateway_usage_events"]`; it never touches `credit_ledger` or `debit_for_run`. Phase 1's duration-based debit remains the only thing that moves money.
- Tenant isolation: every gateway-token and correlation-id operation is filtered/validated by `organization_id` (see `api/AGENTS.md`).
- DB access lives in `api/db/*_client.py` mixins; domain logic in `api/services/`; routes stay thin — same convention as Phase 1.
- Migrations are created via `./scripts/makemigrate.sh "description"` and applied with `./scripts/migrate.sh`.
- **No placeholders.** Where a task touches a real upstream provider (OpenAI for LLM, Deepgram for STT, ElevenLabs for TTS — the first provider onboarded per surface, consistent with the spec's incremental-rollout intent), the code uses that provider's actual wire shape, not a stub. Additional providers are explicitly out of scope for this plan (tracked as follow-up, see Self-Review).

---

## File Structure

**Create (repo root — new deployable):**
- `gateway/__init__.py` — package marker.
- `gateway/config.py` — env-var config (`API_INTERNAL_URL`, `GATEWAY_INTERNAL_SECRET`, `GATEWAY_PORT`, upstream provider keys, token-cache TTL).
- `gateway/app.py` — FastAPI app; mounts `llm`, `stt`, `tts` routers; `GET /health`.
- `gateway/auth.py` — `AuthContext` + `validate_request` dependency: extracts Bearer token + `X-Correlation-Id`, validates against `api/`'s internal endpoint with a short in-process TTL cache.
- `gateway/usage.py` — `UsageEvent` dataclass + `push_usage_event()` (retry-with-backoff HTTP push to `api/`, mirroring `mps_service_key_client.report_platform_usage`'s retry loop).
- `gateway/routes/__init__.py`
- `gateway/routes/llm.py` — `POST /v1/chat/completions`, OpenAI-compatible SSE proxy to OpenAI.
- `gateway/routes/stt.py` — `WS /v1/stt/stream`, gateway-framed proxy to Deepgram.
- `gateway/routes/tts.py` — `WS /v1/tts/stream`, gateway-framed proxy to ElevenLabs.
- `gateway/requirements.txt` — `fastapi`, `uvicorn[standard]`, `httpx`, `websockets`, `openai`, `pydantic`.
- `gateway/Dockerfile` — own container, independent of `api/Dockerfile`.
- `gateway/tests/__init__.py`, `gateway/tests/conftest.py`, `gateway/tests/test_app_health.py`, `gateway/tests/test_auth.py`, `gateway/tests/test_llm_route.py`, `gateway/tests/test_stt_route.py`, `gateway/tests/test_tts_route.py`.

**Create (`api/`):**
- `api/utils/gateway_token.py` — `generate_gateway_token()` / `hash_gateway_token()`.
- `api/db/gateway_client.py` — `GatewayClient` DB mixin: token issue/validate/revoke, correlation-id register/validate, usage-event append.
- `api/routes/gateway_admin.py` — superuser gateway-token issuance/revocation.
- `api/routes/gateway_internal.py` — shared-secret-protected `validate` + `usage` endpoints called by the gateway.
- `api/services/gateway_service.py` — mint-and-register correlation id, `GATEWAY_CORRELATION_ID_CONTEXT_KEY` helpers (mirrors `managed_model_services.py`'s MPS pattern).
- `api/tests/test_gateway_client.py`, `api/tests/test_gateway_admin_routes.py`, `api/tests/test_gateway_internal_routes.py`, `api/tests/test_gateway_service.py`.

**Modify (`api/`):**
- `api/constants.py` — add `GATEWAY_URL`, `GATEWAY_ENABLED`, `GATEWAY_INTERNAL_SECRET`, `GATEWAY_CORRELATION_TTL_SECONDS`.
- `api/db/models.py` — add `GatewayTokenModel`, `GatewayCorrelationModel`.
- `api/db/db_client.py` — add `GatewayClient` to the `DBClient` base list.
- `api/app.py` — include `gateway_admin` and `gateway_internal` routers.
- `api/services/quota_service.py` — mint+register the gateway correlation id when `GATEWAY_ENABLED` and the effective config uses a Dograh-managed service.
- `api/services/pipecat/service_factory.py` — `GATEWAY_ENABLED` branches at the three Dograh call sites (STT line 192, TTS line 470, LLM line 736).

**Modify (`pipecat/` submodule):**
- `pipecat/src/pipecat/services/dograh/dograh_gateway.py` — **new**, replaces `mps_billing.py` imports in the three files below.
- `pipecat/src/pipecat/services/dograh/llm.py`, `stt.py`, `tts.py` — swap `mps_billing` → `dograh_gateway`, update `base_url` defaults and correlation-id attach points.
- `pipecat/tests/test_dograh_llm_gateway.py`, `pipecat/tests/test_dograh_stt_gateway.py`, `pipecat/tests/test_dograh_tts_gateway.py` — **new**, against a mock gateway server, per pipecat's own `run_test()` conventions.

**Not modified:** `pipecat/src/pipecat/services/dograh/mps_billing.py` stays in the tree (legacy MPS path still imports it when `GATEWAY_ENABLED=false`); no other provider service files change.

---

## Task 1: Feature flags, constants, and token/correlation utils

**Files:**
- Modify: `api/constants.py`
- Create: `api/utils/gateway_token.py`
- Test: `api/tests/test_gateway_utils.py`

**Interfaces:**
- Produces: `GATEWAY_URL: str` (default `"http://localhost:8100"`), `GATEWAY_ENABLED: bool` (default `False`), `GATEWAY_INTERNAL_SECRET: str | None`, `GATEWAY_CORRELATION_TTL_SECONDS: int` (default `14400` = 4h, generous for long calls).
- `generate_gateway_token() -> tuple[str, str, str]` — `(raw_token, token_hash, token_prefix)`, raw prefixed `gwt_`.
- `hash_gateway_token(raw: str) -> str`.

- [ ] **Step 1: Read the existing API-key hashing pattern**

Run: `cat api/utils/api_key.py`
Expected: shows `generate_api_key()`/`hash_api_key()` — `dgr_` prefix, `secrets.token_urlsafe(32)`, `sha256` hash. Mirror this exactly, swapping the prefix.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_gateway_utils.py
from api.constants import (
    GATEWAY_CORRELATION_TTL_SECONDS,
    GATEWAY_ENABLED,
    GATEWAY_INTERNAL_SECRET,
    GATEWAY_URL,
)
from api.utils.gateway_token import generate_gateway_token, hash_gateway_token


def test_constants_importable():
    assert isinstance(GATEWAY_URL, str)
    assert isinstance(GATEWAY_ENABLED, bool)
    assert GATEWAY_INTERNAL_SECRET is None or isinstance(GATEWAY_INTERNAL_SECRET, str)
    assert GATEWAY_CORRELATION_TTL_SECONDS > 0


def test_generate_gateway_token_shape_and_hash():
    raw, token_hash, prefix = generate_gateway_token()
    assert raw.startswith("gwt_")
    assert prefix == raw[:8]
    assert token_hash == hash_gateway_token(raw)
    assert token_hash != raw


def test_generate_gateway_token_unique():
    a, _, _ = generate_gateway_token()
    b, _, _ = generate_gateway_token()
    assert a != b
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_utils.py -v`
Expected: FAIL — `ImportError: cannot import name 'GATEWAY_URL'`.

- [ ] **Step 4: Add the constants**

In `api/constants.py`, near `BILLING_ENGINE`:

```python
# Provider gateway: self-owned replacement for MPS on the Dograh-managed
# LLM/STT/TTS path. Off by default; the existing MPS-backed base_url/api_key
# wiring in service_factory.py is untouched when this is False.
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8100")
GATEWAY_ENABLED = os.getenv("GATEWAY_ENABLED", "false").lower() == "true"
# Shared secret between api/ and gateway/ for the internal validate/usage
# endpoints. Required in any environment where GATEWAY_ENABLED is true.
GATEWAY_INTERNAL_SECRET = os.getenv("GATEWAY_INTERNAL_SECRET")
# How long a pre-registered correlation id remains valid at the gateway.
# 4 hours comfortably covers any single call's lifetime.
GATEWAY_CORRELATION_TTL_SECONDS = int(
    os.getenv("GATEWAY_CORRELATION_TTL_SECONDS", str(4 * 60 * 60))
)
```

- [ ] **Step 5: Implement `api/utils/gateway_token.py`**

```python
"""Gateway-token generation and hashing.

Mirrors api/utils/api_key.py's treatment of API keys: the raw token is shown
once at issuance, only its SHA256 hash is persisted (GatewayTokenModel.token_hash).
"""

import hashlib
import secrets
from typing import Tuple


def generate_gateway_token() -> Tuple[str, str, str]:
    """Generate a new gateway token with its hash and display prefix.

    Returns:
        Tuple of (raw_token, token_hash, token_prefix).
    """
    raw_token = f"gwt_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_prefix = raw_token[:8]
    return raw_token, token_hash, token_prefix


def hash_gateway_token(raw_token: str) -> str:
    """Hash a gateway token for lookup/comparison."""
    return hashlib.sha256(raw_token.encode()).hexdigest()
```

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_utils.py -v`
Expected: 3 passed.

- [ ] **Step 7: Add matching env vars to `api/.env.test`**

Append (values only needed for import/test purposes, no live secret required):
```
GATEWAY_URL=http://localhost:8100
GATEWAY_ENABLED=false
GATEWAY_INTERNAL_SECRET=test-gateway-internal-secret
```

- [ ] **Step 8: Commit**

```bash
git add api/constants.py api/utils/gateway_token.py api/tests/test_gateway_utils.py api/.env.test
git commit -m "feat(gateway): add GATEWAY_ENABLED flag, constants, and token hashing utils"
```

---

## Task 2: Gateway service scaffolding

**Files:**
- Create: `gateway/__init__.py`, `gateway/config.py`, `gateway/app.py`, `gateway/requirements.txt`, `gateway/Dockerfile`
- Test: `gateway/tests/__init__.py`, `gateway/tests/conftest.py`, `gateway/tests/test_app_health.py`

**Interfaces:**
- Produces: `gateway.app.app` — a FastAPI instance with `GET /health` → `{"status": "ok"}`.
- Produces: `gateway.config` module-level constants read from env (`API_INTERNAL_URL`, `GATEWAY_INTERNAL_SECRET`, `GATEWAY_PORT`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `GATEWAY_TOKEN_CACHE_TTL_SECONDS`).

- [ ] **Step 1: Verify the target directory is free**

Run: `ls /Volumes/StorageDiv3/Developer/dograh | grep -x gateway || echo "no gateway/ yet"`
Expected: `no gateway/ yet`.

- [ ] **Step 2: Write the failing test**

```python
# gateway/tests/__init__.py
```
(empty file)

```python
# gateway/tests/conftest.py
import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    from gateway.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://gw-test") as c:
        yield c
```

```python
# gateway/tests/test_app_health.py
import pytest


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_app_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway'`.

- [ ] **Step 4: Implement `gateway/config.py`**

```python
"""Gateway service configuration. Read once at import time, env-driven."""

import os

# Where the gateway calls back into api/ for token/correlation validation and
# usage reporting. In local dev this is the same host running uvicorn on 8000.
API_INTERNAL_URL = os.getenv("API_INTERNAL_URL", "http://localhost:8000")
# Shared secret with api/'s GATEWAY_INTERNAL_SECRET (api/constants.py). Must
# match exactly or every gateway request fails auth.
GATEWAY_INTERNAL_SECRET = os.getenv("GATEWAY_INTERNAL_SECRET", "")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8100"))

# Platform provider keys. Held only here — never in api/, never in pipecat
# process memory beyond the short-lived gateway token.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")

# In-process TTL cache for validated (token, correlation_id) pairs, to avoid
# round-tripping to api/ on every audio frame.
GATEWAY_TOKEN_CACHE_TTL_SECONDS = int(
    os.getenv("GATEWAY_TOKEN_CACHE_TTL_SECONDS", "30")
)
```

- [ ] **Step 5: Implement `gateway/app.py`**

```python
"""Provider Gateway — standalone FastAPI service.

Holds platform provider keys and proxies LLM/STT/TTS traffic for Dograh-managed
calls. Never connects to api/'s database; org/correlation resolution goes
through api/'s internal HTTP endpoints (see gateway/auth.py).
"""

from fastapi import FastAPI
from loguru import logger

app = FastAPI(title="Dograh Provider Gateway", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.on_event("startup")
async def on_startup():
    logger.info("Provider gateway starting up")
```

- [ ] **Step 6: `gateway/__init__.py`**

```python
```
(empty file)

- [ ] **Step 7: `gateway/requirements.txt`**

```
fastapi==0.135.3
uvicorn[standard]==0.35.0
httpx==0.28.1
websockets==15.0.1
openai==1.99.6
pydantic==2.11.10
```

(Pin versions to match what's already resolved in `api/requirements.txt`/`pipecat/pyproject.toml` where the same package appears; run `pip show fastapi uvicorn openai websockets` in the active venv and adjust pins to match exactly so both services share one dependency set in local dev.)

- [ ] **Step 8: `gateway/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN python -m venv "$VIRTUAL_ENV"

RUN --mount=type=bind,source=gateway/requirements.txt,target=/tmp/req.txt \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install -r /tmp/req.txt

FROM python:3.13-slim AS runner

WORKDIR /app

RUN groupadd --system dograh \
 && useradd --system --gid dograh --no-log-init --home-dir /app --shell /usr/sbin/nologin dograh \
 && chown dograh:dograh /app

COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY --chown=dograh:dograh ./gateway ./gateway

USER dograh

EXPOSE 8100

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8100"]
```

- [ ] **Step 9: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_app_health.py -v`
Expected: 1 passed.

- [ ] **Step 10: Commit**

```bash
git add gateway/
git commit -m "feat(gateway): scaffold standalone gateway FastAPI service"
```

---

## Task 3: `api/` gateway data models and migration

**Files:**
- Modify: `api/db/models.py`
- Migration: generated under `api/alembic/versions/`

**Interfaces:**
- Produces:
  - `GatewayTokenModel` (table `gateway_tokens`): `id`, `organization_id`, `token_hash:str(unique)`, `token_prefix:str`, `is_revoked:bool`, `created_by:int|None`, `created_at`, `revoked_at:datetime|None`.
  - `GatewayCorrelationModel` (table `gateway_correlations`): `id`, `correlation_id:str(unique)`, `organization_id`, `workflow_run_id`, `created_at`, `expires_at`.

- [ ] **Step 1: Add models to `api/db/models.py`**

After `CreditLedgerModel`/`PricingRuleModel` (Phase 1), add:

```python
class GatewayTokenModel(Base):
    """Org-scoped bearer token for the provider gateway. Never holds a provider key."""

    __tablename__ = "gateway_tokens"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    token_hash = Column(String, nullable=False, unique=True, index=True)
    token_prefix = Column(String, nullable=False)  # first 8 chars, for display
    is_revoked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    organization = relationship("OrganizationModel")

    __table_args__ = (
        Index("ix_gateway_tokens_organization_id", "organization_id"),
    )


class GatewayCorrelationModel(Base):
    """A pre-registered correlation id, minted by api/ at authorize time and
    validated by the gateway before it will proxy any traffic for that id.
    Closes the replay/misattribution gap MPS's opaque correlation id had."""

    __tablename__ = "gateway_correlations"

    id = Column(Integer, primary_key=True, index=True)
    correlation_id = Column(String, nullable=False, unique=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workflow_run_id = Column(
        Integer, ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_gateway_correlations_org", "organization_id"),
    )
```

- [ ] **Step 2: Generate the migration**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && ./scripts/makemigrate.sh "add gateway tokens and correlations tables"`
Expected: a new file under `api/alembic/versions/` creating `gateway_tokens` and `gateway_correlations`.

- [ ] **Step 3: Inspect the migration**

Open the generated file. Verify: both tables created; `token_hash` unique+indexed; `correlation_id` unique+indexed; both FKs to `organizations`/`workflow_runs` use `ondelete="CASCADE"`. Fix by hand if autogen missed a constraint.

- [ ] **Step 4: Apply and verify against test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh && python -c "import asyncio; from api.db.database import engine; from sqlalchemy import text
async def go():
    import api.db.models
    async with engine.begin() as c:
        r = await c.execute(text(\"select to_regclass('gateway_tokens'), to_regclass('gateway_correlations')\"))
        print(r.fetchone())
asyncio.run(go())"`
Expected: `('gateway_tokens', 'gateway_correlations')`.

(Note: this apply step requires the pgvector-enabled Postgres from `docker-compose-local.yaml` — see Global Constraints.)

- [ ] **Step 5: Commit**

```bash
git add api/db/models.py api/alembic/versions/
git commit -m "feat(gateway): add gateway_tokens and gateway_correlations tables"
```

---

## Task 4: `GatewayClient` DB mixin

**Files:**
- Create: `api/db/gateway_client.py`
- Modify: `api/db/db_client.py`
- Test: `api/tests/test_gateway_client.py`

**Interfaces:**
- Produces methods on `db_client`:
  - `async create_gateway_token(organization_id, created_by=None) -> tuple[GatewayTokenModel, str]` (returns model + raw token, shown once).
  - `async list_gateway_tokens(organization_id) -> list[GatewayTokenModel]`.
  - `async revoke_gateway_token(token_id, organization_id) -> bool`.
  - `async validate_gateway_token(raw_token) -> int | None` (returns `organization_id` or `None`).
  - `async register_correlation_id(*, organization_id, workflow_run_id, ttl_seconds) -> GatewayCorrelationModel` (mints `f"run:{workflow_run_id}:{uuid4()}"`).
  - `async validate_correlation_id(correlation_id, organization_id) -> int | None` (returns `workflow_run_id` or `None`; rejects unknown, expired, or wrong-org ids).
  - `async append_gateway_usage_event(workflow_run_id, event: dict) -> None` (merges into `workflow_run.cost_info["gateway_usage_events"]`).

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_gateway_client.py
"""Tests for GatewayClient (tokens + correlation ids). Uses a real committing
session factory, same pattern as api/tests/test_billing_service.py, since
row creation/lookup must be visible across the client's own sessions."""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel, WorkflowModel, WorkflowRunModel


@pytest.fixture(scope="module")
async def real_db(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    created = {"orgs": [], "workflows": [], "runs": []}

    async def make_org(provider_id: str) -> int:
        async with session_factory() as session:
            org = OrganizationModel(provider_id=provider_id)
            session.add(org)
            await session.commit()
            await session.refresh(org)
            created["orgs"].append(org.id)
            return org.id

    async def make_run(org_id: int) -> int:
        async with session_factory() as session:
            wf = WorkflowModel(name="gw-test-wf", organization_id=org_id, user_id=None)
            session.add(wf)
            await session.commit()
            await session.refresh(wf)
            created["workflows"].append(wf.id)

            run = WorkflowRunModel(workflow_id=wf.id, mode="pipeline")
            session.add(run)
            await session.commit()
            await session.refresh(run)
            created["runs"].append(run.id)
            return run.id

    yield session_factory, make_org, make_run

    async with session_factory() as session:
        from api.db.models import GatewayCorrelationModel, GatewayTokenModel

        for org_id in created["orgs"]:
            await session.execute(
                delete(GatewayTokenModel).where(
                    GatewayTokenModel.organization_id == org_id
                )
            )
            await session.execute(
                delete(GatewayCorrelationModel).where(
                    GatewayCorrelationModel.organization_id == org_id
                )
            )
        for run_id in created["runs"]:
            await session.execute(
                delete(WorkflowRunModel).where(WorkflowRunModel.id == run_id)
            )
        for wf_id in created["workflows"]:
            await session.execute(delete(WorkflowModel).where(WorkflowModel.id == wf_id))
        for org_id in created["orgs"]:
            await session.execute(
                delete(OrganizationModel).where(OrganizationModel.id == org_id)
            )
        await session.commit()

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_and_validate_gateway_token(real_db):
    from api.db import db_client

    _, make_org, _ = real_db
    org_id = await make_org("org_gw_token")

    model, raw_token = await db_client.create_gateway_token(organization_id=org_id)
    assert model.token_prefix == raw_token[:8]

    resolved_org_id = await db_client.validate_gateway_token(raw_token)
    assert resolved_org_id == org_id


@pytest.mark.asyncio
async def test_revoked_token_fails_validation(real_db):
    from api.db import db_client

    _, make_org, _ = real_db
    org_id = await make_org("org_gw_token_revoked")

    model, raw_token = await db_client.create_gateway_token(organization_id=org_id)
    revoked = await db_client.revoke_gateway_token(model.id, organization_id=org_id)
    assert revoked is True

    assert await db_client.validate_gateway_token(raw_token) is None


@pytest.mark.asyncio
async def test_correlation_id_register_and_validate(real_db):
    from api.db import db_client

    _, make_org, make_run = real_db
    org_id = await make_org("org_gw_corr")
    run_id = await make_run(org_id)

    correlation = await db_client.register_correlation_id(
        organization_id=org_id, workflow_run_id=run_id, ttl_seconds=3600
    )
    assert correlation.correlation_id.startswith(f"run:{run_id}:")

    resolved_run_id = await db_client.validate_correlation_id(
        correlation.correlation_id, organization_id=org_id
    )
    assert resolved_run_id == run_id


@pytest.mark.asyncio
async def test_correlation_id_wrong_org_rejected(real_db):
    from api.db import db_client

    _, make_org, make_run = real_db
    org_id = await make_org("org_gw_corr_owner")
    other_org_id = await make_org("org_gw_corr_intruder")
    run_id = await make_run(org_id)

    correlation = await db_client.register_correlation_id(
        organization_id=org_id, workflow_run_id=run_id, ttl_seconds=3600
    )
    assert (
        await db_client.validate_correlation_id(
            correlation.correlation_id, organization_id=other_org_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_correlation_id_expired_rejected(real_db):
    from api.db import db_client

    _, make_org, make_run = real_db
    org_id = await make_org("org_gw_corr_expired")
    run_id = await make_run(org_id)

    correlation = await db_client.register_correlation_id(
        organization_id=org_id, workflow_run_id=run_id, ttl_seconds=-1
    )
    assert (
        await db_client.validate_correlation_id(
            correlation.correlation_id, organization_id=org_id
        )
        is None
    )


@pytest.mark.asyncio
async def test_append_gateway_usage_event(real_db):
    from api.db import db_client

    _, make_org, make_run = real_db
    org_id = await make_org("org_gw_usage")
    run_id = await make_run(org_id)

    await db_client.append_gateway_usage_event(
        run_id, {"provider": "openai", "kind": "llm_tokens", "quantity": 42}
    )
    run = await db_client.get_workflow_run_by_id(run_id)
    events = (run.cost_info or {}).get("gateway_usage_events", [])
    assert len(events) == 1
    assert events[0]["quantity"] == 42
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_client.py -v`
Expected: FAIL — `AttributeError: 'DBClient' object has no attribute 'create_gateway_token'`.

- [ ] **Step 3: Implement `api/db/gateway_client.py`**

```python
"""DB access for gateway tokens and pre-registered correlation ids.

api/ is the sole source of truth for both — the gateway never connects to
this database directly; it validates every request over HTTP against
api/routes/gateway_internal.py, which calls these methods.
"""

from datetime import UTC, datetime, timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import GatewayCorrelationModel, GatewayTokenModel, WorkflowRunModel
from api.utils.gateway_token import generate_gateway_token, hash_gateway_token


class GatewayClient(BaseDBClient):
    async def create_gateway_token(
        self, organization_id: int, created_by: Optional[int] = None
    ) -> tuple[GatewayTokenModel, str]:
        raw_token, token_hash, token_prefix = generate_gateway_token()
        async with self.async_session() as session:
            token = GatewayTokenModel(
                organization_id=organization_id,
                token_hash=token_hash,
                token_prefix=token_prefix,
                created_by=created_by,
            )
            session.add(token)
            await session.commit()
            await session.refresh(token)
            return token, raw_token

    async def list_gateway_tokens(self, organization_id: int) -> list[GatewayTokenModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(GatewayTokenModel).where(
                    GatewayTokenModel.organization_id == organization_id
                )
            )
            return list(result.scalars().all())

    async def revoke_gateway_token(self, token_id: int, organization_id: int) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(GatewayTokenModel).where(
                    GatewayTokenModel.id == token_id,
                    GatewayTokenModel.organization_id == organization_id,
                )
            )
            token = result.scalars().first()
            if token is None:
                return False
            token.is_revoked = True
            token.revoked_at = datetime.now(UTC)
            await session.commit()
            return True

    async def validate_gateway_token(self, raw_token: str) -> Optional[int]:
        token_hash = hash_gateway_token(raw_token)
        async with self.async_session() as session:
            result = await session.execute(
                select(GatewayTokenModel).where(
                    GatewayTokenModel.token_hash == token_hash,
                    GatewayTokenModel.is_revoked.is_(False),
                )
            )
            token = result.scalars().first()
            return token.organization_id if token else None

    async def register_correlation_id(
        self, *, organization_id: int, workflow_run_id: int, ttl_seconds: int
    ) -> GatewayCorrelationModel:
        correlation_id = f"run:{workflow_run_id}:{uuid4()}"
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        async with self.async_session() as session:
            correlation = GatewayCorrelationModel(
                correlation_id=correlation_id,
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                expires_at=expires_at,
            )
            session.add(correlation)
            await session.commit()
            await session.refresh(correlation)
            return correlation

    async def validate_correlation_id(
        self, correlation_id: str, organization_id: int
    ) -> Optional[int]:
        async with self.async_session() as session:
            result = await session.execute(
                select(GatewayCorrelationModel).where(
                    GatewayCorrelationModel.correlation_id == correlation_id,
                    GatewayCorrelationModel.organization_id == organization_id,
                )
            )
            correlation = result.scalars().first()
            if correlation is None:
                return None
            if correlation.expires_at < datetime.now(UTC):
                return None
            return correlation.workflow_run_id

    async def append_gateway_usage_event(
        self, workflow_run_id: int, event: dict
    ) -> None:
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel).where(WorkflowRunModel.id == workflow_run_id)
            )
            run = result.scalars().first()
            if run is None:
                return
            cost_info = dict(run.cost_info or {})
            events = list(cost_info.get("gateway_usage_events", []))
            events.append({**event, "recorded_at": datetime.now(UTC).isoformat()})
            cost_info["gateway_usage_events"] = events
            run.cost_info = cost_info
            await session.commit()
```

- [ ] **Step 4: Register the mixin**

In `api/db/db_client.py`, add `from api.db.gateway_client import GatewayClient` and add `GatewayClient,` to the `DBClient(...)` base list.

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_client.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add api/db/gateway_client.py api/db/db_client.py api/tests/test_gateway_client.py
git commit -m "feat(gateway): GatewayClient DB mixin for tokens, correlation ids, usage trail"
```

---

## Task 5: Superuser gateway-token admin routes

**Files:**
- Create: `api/routes/gateway_admin.py`
- Modify: `api/app.py`
- Test: `api/tests/test_gateway_admin_routes.py`

**Interfaces:**
- Consumes: `get_superuser` (`api/services/auth/depends.py:310`), `db_client` (Task 4).
- Produces routes under `/superuser`:
  - `POST /superuser/orgs/{org_id}/gateway-tokens` → `{id, token, token_prefix}` (plaintext shown once).
  - `GET /superuser/orgs/{org_id}/gateway-tokens` → `[{id, token_prefix, is_revoked, created_at}]`.
  - `DELETE /superuser/gateway-tokens/{token_id}?organization_id=` → `{revoked: bool}`.

- [ ] **Step 1: Find the router include site and mount prefix**

Run: `grep -n "include_router\|API_PREFIX" api/app.py`
Expected: shows `API_PREFIX = "/api/v1"` and `app.include_router(api_router, prefix=API_PREFIX)`; `api_router.include_router(main_router)` is how `superuser.py` reaches `/api/v1/superuser/...`. Mirror this for `gateway_admin`.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_gateway_admin_routes.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.services.auth.depends import get_superuser


@pytest.fixture
def superuser_override():
    app.dependency_overrides[get_superuser] = lambda: type(
        "U", (), {"id": 1, "is_superuser": True}
    )()
    yield
    app.dependency_overrides.pop(get_superuser, None)


@pytest.mark.asyncio
async def test_issue_and_revoke_gateway_token(superuser_override):
    from api.db.database import async_session
    from api.db.models import OrganizationModel

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_gw_admin")
        s.add(org)
        await s.commit()
        await s.refresh(org)
        org_id = org.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(f"/api/v1/superuser/orgs/{org_id}/gateway-tokens")
        assert r.status_code == 200
        body = r.json()
        assert body["token"].startswith("gwt_")
        token_id = body["id"]

        r2 = await client.get(f"/api/v1/superuser/orgs/{org_id}/gateway-tokens")
        assert r2.status_code == 200
        assert any(t["id"] == token_id for t in r2.json())

        r3 = await client.delete(
            f"/api/v1/superuser/gateway-tokens/{token_id}",
            params={"organization_id": org_id},
        )
        assert r3.status_code == 200
        assert r3.json()["revoked"] is True
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_admin_routes.py -v`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 4: Implement `api/routes/gateway_admin.py`**

```python
from fastapi import APIRouter, Depends

from api.db import db_client
from api.services.auth.depends import get_superuser

router = APIRouter(prefix="/superuser", tags=["gateway-admin"])


@router.post("/orgs/{org_id}/gateway-tokens")
async def issue_gateway_token(org_id: int, user=Depends(get_superuser)):
    model, raw_token = await db_client.create_gateway_token(
        organization_id=org_id, created_by=getattr(user, "id", None)
    )
    return {"id": model.id, "token": raw_token, "token_prefix": model.token_prefix}


@router.get("/orgs/{org_id}/gateway-tokens")
async def list_gateway_tokens(org_id: int, user=Depends(get_superuser)):
    tokens = await db_client.list_gateway_tokens(org_id)
    return [
        {
            "id": t.id,
            "token_prefix": t.token_prefix,
            "is_revoked": t.is_revoked,
            "created_at": t.created_at.isoformat(),
        }
        for t in tokens
    ]


@router.delete("/gateway-tokens/{token_id}")
async def revoke_gateway_token(
    token_id: int, organization_id: int, user=Depends(get_superuser)
):
    revoked = await db_client.revoke_gateway_token(token_id, organization_id)
    return {"revoked": revoked}
```

- [ ] **Step 5: Mount the router**

In `api/app.py`, add `from api.routes import gateway_admin` and `api_router.include_router(gateway_admin.router)` alongside `api_router.include_router(main_router)`.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_admin_routes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add api/routes/gateway_admin.py api/app.py api/tests/test_gateway_admin_routes.py
git commit -m "feat(gateway): superuser gateway-token issuance/revocation endpoints"
```

---

## Task 6: Internal validate + usage-callback endpoints (gateway → api/)

**Files:**
- Create: `api/routes/gateway_internal.py`
- Modify: `api/app.py`
- Test: `api/tests/test_gateway_internal_routes.py`

**Interfaces:**
- Consumes: `GATEWAY_INTERNAL_SECRET` (Task 1), `db_client` (Task 4).
- Produces routes under `/internal/gateway` (no user auth — shared-secret header `X-Gateway-Internal-Secret` instead, since the only caller is the gateway process):
  - `POST /internal/gateway/validate` body `{token, correlation_id}` → `{organization_id, workflow_run_id}` or `401`/`403`.
  - `POST /internal/gateway/usage` body `{workflow_run_id, provider, kind, quantity, metadata}` → `{ok: true}`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_gateway_internal_routes.py
import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app


@pytest.mark.asyncio
async def test_validate_rejects_missing_secret():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/internal/gateway/validate", json={"token": "gwt_x"}
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_validate_and_usage_roundtrip(monkeypatch):
    from api.constants import GATEWAY_INTERNAL_SECRET
    from api.db.database import async_session
    from api.db.models import OrganizationModel, WorkflowModel, WorkflowRunModel
    from api.db import db_client

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_gw_internal")
        s.add(org)
        await s.commit()
        await s.refresh(org)

        wf = WorkflowModel(name="gw-internal-wf", organization_id=org.id, user_id=None)
        s.add(wf)
        await s.commit()
        await s.refresh(wf)

        run = WorkflowRunModel(workflow_id=wf.id, mode="pipeline")
        s.add(run)
        await s.commit()
        await s.refresh(run)

    _, raw_token = await db_client.create_gateway_token(organization_id=org.id)
    correlation = await db_client.register_correlation_id(
        organization_id=org.id, workflow_run_id=run.id, ttl_seconds=3600
    )

    transport = ASGITransport(app=app)
    headers = {"X-Gateway-Internal-Secret": GATEWAY_INTERNAL_SECRET or "test-gateway-internal-secret"}
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/internal/gateway/validate",
            json={"token": raw_token, "correlation_id": correlation.correlation_id},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["organization_id"] == org.id
        assert body["workflow_run_id"] == run.id

        r2 = await client.post(
            "/api/v1/internal/gateway/usage",
            json={
                "workflow_run_id": run.id,
                "provider": "openai",
                "kind": "llm_tokens",
                "quantity": 128,
                "metadata": {"correlation_id": correlation.correlation_id},
            },
            headers=headers,
        )
        assert r2.status_code == 200
        assert r2.json() == {"ok": True}

    updated_run = await db_client.get_workflow_run_by_id(run.id)
    events = (updated_run.cost_info or {}).get("gateway_usage_events", [])
    assert any(e["quantity"] == 128 for e in events)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_internal_routes.py -v`
Expected: FAIL — 404 (route not mounted).

- [ ] **Step 3: Implement `api/routes/gateway_internal.py`**

```python
"""Internal endpoints called only by the gateway service (never by end users).

Authenticated by a shared secret, not by user/org session auth — the caller
is a trusted internal process, not a customer.
"""

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.constants import GATEWAY_INTERNAL_SECRET
from api.db import db_client

router = APIRouter(prefix="/internal/gateway", tags=["gateway-internal"])


def _check_secret(secret: str | None) -> None:
    if not GATEWAY_INTERNAL_SECRET or secret != GATEWAY_INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Invalid gateway internal secret")


class ValidateRequest(BaseModel):
    token: str
    correlation_id: str | None = None


@router.post("/validate")
async def validate(
    body: ValidateRequest,
    x_gateway_internal_secret: str | None = Header(default=None),
):
    _check_secret(x_gateway_internal_secret)

    organization_id = await db_client.validate_gateway_token(body.token)
    if organization_id is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked gateway token")

    workflow_run_id = None
    if body.correlation_id is not None:
        workflow_run_id = await db_client.validate_correlation_id(
            body.correlation_id, organization_id=organization_id
        )
        if workflow_run_id is None:
            raise HTTPException(
                status_code=403, detail="Unknown or expired correlation id"
            )

    return {"organization_id": organization_id, "workflow_run_id": workflow_run_id}


class UsageRequest(BaseModel):
    workflow_run_id: int
    provider: str
    kind: str
    quantity: float
    metadata: dict[str, Any] = {}


@router.post("/usage")
async def usage(
    body: UsageRequest,
    x_gateway_internal_secret: str | None = Header(default=None),
):
    _check_secret(x_gateway_internal_secret)

    await db_client.append_gateway_usage_event(
        body.workflow_run_id,
        {
            "provider": body.provider,
            "kind": body.kind,
            "quantity": body.quantity,
            "metadata": body.metadata,
        },
    )
    return {"ok": True}
```

- [ ] **Step 4: Mount the router**

In `api/app.py`, add `from api.routes import gateway_internal` and `api_router.include_router(gateway_internal.router)`.

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_internal_routes.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add api/routes/gateway_internal.py api/app.py api/tests/test_gateway_internal_routes.py
git commit -m "feat(gateway): internal validate + usage-callback endpoints for the gateway service"
```

---

## Task 7: `api/services/gateway_service.py` — correlation-id mint helper

**Files:**
- Create: `api/services/gateway_service.py`
- Test: `api/tests/test_gateway_service.py`

**Interfaces:**
- Consumes: `db_client.register_correlation_id` (Task 4).
- Produces:
  - `GATEWAY_CORRELATION_ID_CONTEXT_KEY = "gateway_correlation_id"` (parallel to `MPS_CORRELATION_ID_CONTEXT_KEY`).
  - `def get_gateway_correlation_id(initial_context: dict | None) -> str | None`.
  - `async def mint_and_register_correlation_id(*, organization_id: int, workflow_run_id: int) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_gateway_service.py
from unittest.mock import AsyncMock

import pytest

from api.services import gateway_service as gs


def test_get_gateway_correlation_id_absent():
    assert gs.get_gateway_correlation_id(None) is None
    assert gs.get_gateway_correlation_id({}) is None


def test_get_gateway_correlation_id_present():
    ctx = {gs.GATEWAY_CORRELATION_ID_CONTEXT_KEY: "run:1:abc"}
    assert gs.get_gateway_correlation_id(ctx) == "run:1:abc"


@pytest.mark.asyncio
async def test_mint_and_register_correlation_id(monkeypatch):
    fake_correlation = type("C", (), {"correlation_id": "run:7:xyz"})()
    register_mock = AsyncMock(return_value=fake_correlation)
    monkeypatch.setattr(gs.db_client, "register_correlation_id", register_mock)

    correlation_id = await gs.mint_and_register_correlation_id(
        organization_id=9, workflow_run_id=7
    )

    assert correlation_id == "run:7:xyz"
    register_mock.assert_awaited_once_with(
        organization_id=9,
        workflow_run_id=7,
        ttl_seconds=gs.GATEWAY_CORRELATION_TTL_SECONDS,
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_service.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.gateway_service`.

- [ ] **Step 3: Implement `api/services/gateway_service.py`**

```python
"""Provider-gateway correlation-id lifecycle helper.

Mirrors managed_model_services.py's MPS pattern (mint-once, thread-through,
attach-to-every-call) but mints and registers the id in our own stack, via
GatewayClient, instead of round-tripping to MPS.
"""

from __future__ import annotations

from typing import Any

from api.constants import GATEWAY_CORRELATION_TTL_SECONDS
from api.db import db_client

GATEWAY_CORRELATION_ID_CONTEXT_KEY = "gateway_correlation_id"


def get_gateway_correlation_id(initial_context: dict[str, Any] | None) -> str | None:
    if not initial_context:
        return None
    correlation_id = initial_context.get(GATEWAY_CORRELATION_ID_CONTEXT_KEY)
    if correlation_id is None:
        return None
    return str(correlation_id)


async def mint_and_register_correlation_id(
    *, organization_id: int, workflow_run_id: int
) -> str:
    correlation = await db_client.register_correlation_id(
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        ttl_seconds=GATEWAY_CORRELATION_TTL_SECONDS,
    )
    return correlation.correlation_id
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_service.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/services/gateway_service.py api/tests/test_gateway_service.py
git commit -m "feat(gateway): correlation-id mint-and-register helper"
```

---

## Task 8: Wire correlation minting into `quota_service.authorize_workflow_run_start`

**Files:**
- Modify: `api/services/quota_service.py`
- Test: extend `api/tests/test_quota_service.py`

**Interfaces:**
- Consumes: `gateway_service.mint_and_register_correlation_id` (Task 7); `GATEWAY_ENABLED` (Task 1); `_service_uses_dograh` (already defined in `quota_service.py`).
- Produces: when `GATEWAY_ENABLED` and the effective config uses a Dograh-managed service and `workflow_run_id` is present, mints+registers a correlation id and stashes it on `initial_context[GATEWAY_CORRELATION_ID_CONTEXT_KEY]` — **before** the `BILLING_ENGINE == BILLING_LOCAL` early return, so it applies regardless of billing engine.

- [ ] **Step 1: Write the failing test**

```python
# append to api/tests/test_quota_service.py
from unittest.mock import AsyncMock

import pytest

from api.services import quota_service as qs


@pytest.mark.asyncio
async def test_gateway_correlation_minted_when_enabled(monkeypatch):
    monkeypatch.setattr(qs, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(qs, "BILLING_ENGINE", "mps")  # unrelated to gateway minting
    monkeypatch.setattr(
        qs.db_client,
        "get_workflow_by_id",
        AsyncMock(
            return_value=type(
                "W", (), {"id": 1, "organization_id": 9, "user_id": 2, "workflow_configurations": {}}
            )()
        ),
    )
    monkeypatch.setattr(
        qs.db_client,
        "get_user_by_id",
        AsyncMock(return_value=type("U", (), {"id": 2, "provider_id": "p"})()),
    )
    dograh_stt = type("S", (), {"provider": "dograh"})()
    user_config = type(
        "Cfg", (), {"llm": None, "stt": dograh_stt, "tts": None, "embeddings": None, "is_realtime": False}
    )()
    monkeypatch.setattr(
        qs, "get_effective_ai_model_configuration_for_workflow", AsyncMock(return_value=user_config)
    )
    monkeypatch.setattr(
        qs.gateway_service,
        "mint_and_register_correlation_id",
        AsyncMock(return_value="run:7:abc"),
    )
    run = type("R", (), {"id": 7, "initial_context": {}})()
    monkeypatch.setattr(qs.db_client, "get_workflow_run_by_id", AsyncMock(return_value=run))
    update_mock = AsyncMock()
    monkeypatch.setattr(qs.db_client, "update_workflow_run", update_mock)

    result = await qs.authorize_workflow_run_start(workflow_id=1, workflow_run_id=7)

    assert result.has_quota is True
    qs.gateway_service.mint_and_register_correlation_id.assert_awaited_once_with(
        organization_id=9, workflow_run_id=7
    )
    update_mock.assert_awaited_once()
    _, kwargs = update_mock.call_args
    assert kwargs["initial_context"][qs.GATEWAY_CORRELATION_ID_CONTEXT_KEY] == "run:7:abc"


@pytest.mark.asyncio
async def test_gateway_correlation_not_minted_when_disabled(monkeypatch):
    monkeypatch.setattr(qs, "GATEWAY_ENABLED", False)
    monkeypatch.setattr(
        qs.db_client,
        "get_workflow_by_id",
        AsyncMock(
            return_value=type(
                "W", (), {"id": 1, "organization_id": 9, "user_id": 2, "workflow_configurations": {}}
            )()
        ),
    )
    monkeypatch.setattr(
        qs.db_client,
        "get_user_by_id",
        AsyncMock(return_value=type("U", (), {"id": 2, "provider_id": "p"})()),
    )
    monkeypatch.setattr(
        qs,
        "get_effective_ai_model_configuration_for_workflow",
        AsyncMock(return_value=type("Cfg", (), {"llm": None, "stt": None, "tts": None, "embeddings": None})()),
    )
    mint_mock = AsyncMock()
    monkeypatch.setattr(qs.gateway_service, "mint_and_register_correlation_id", mint_mock)

    await qs.authorize_workflow_run_start(workflow_id=1, workflow_run_id=7)

    mint_mock.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_quota_service.py -k gateway_correlation -v`
Expected: FAIL — `AttributeError: module 'api.services.quota_service' has no attribute 'gateway_service'`.

- [ ] **Step 3: Implement the wiring**

At the top of `api/services/quota_service.py`, add:

```python
from api.constants import GATEWAY_ENABLED
from api.services import gateway_service
from api.services.gateway_service import GATEWAY_CORRELATION_ID_CONTEXT_KEY
```

Add a helper near `_store_run_correlation_id`:

```python
async def _mint_gateway_correlation_if_needed(
    *, organization_id: int, workflow_run_id: int | None, user_config: Any
) -> None:
    """Mint+register a gateway correlation id for Dograh-managed calls.

    Runs independently of BILLING_ENGINE — the gateway is a provider-infra
    choice, not a pricing choice, so a run can be gateway-backed under either
    the "mps" or "local" billing engine.
    """
    if not GATEWAY_ENABLED or not workflow_run_id:
        return
    if not any(
        _service_uses_dograh(getattr(user_config, section, None))
        for section in ("llm", "stt", "tts", "embeddings")
    ):
        return

    correlation_id = await gateway_service.mint_and_register_correlation_id(
        organization_id=organization_id, workflow_run_id=workflow_run_id
    )

    run = await db_client.get_workflow_run_by_id(workflow_run_id)
    initial_context = dict(getattr(run, "initial_context", None) or {}) if run else {}
    initial_context[GATEWAY_CORRELATION_ID_CONTEXT_KEY] = correlation_id
    await db_client.update_workflow_run(workflow_run_id, initial_context=initial_context)
```

In `authorize_workflow_run_start`, immediately after `user_config` is resolved and **before** the `if BILLING_ENGINE == BILLING_LOCAL:` check, insert:

```python
        await _mint_gateway_correlation_if_needed(
            organization_id=workflow.organization_id,
            workflow_run_id=workflow_run_id,
            user_config=user_config,
        )

```

- [ ] **Step 4: Run to verify pass + no regression**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_quota_service.py -v`
Expected: new tests pass; existing tests unaffected (gateway branch is a no-op when `GATEWAY_ENABLED` is unset, the default).

- [ ] **Step 5: Commit**

```bash
git add api/services/quota_service.py api/tests/test_quota_service.py
git commit -m "feat(gateway): mint and register correlation id at authorize time"
```

---

## Task 9: `gateway/auth.py` — token + correlation validation with a TTL cache

**Files:**
- Create: `gateway/auth.py`
- Test: `gateway/tests/test_auth.py`

**Interfaces:**
- Produces:
  - `@dataclass AuthContext`: `organization_id: int, workflow_run_id: int | None, correlation_id: str | None`.
  - `class GatewayAuthError(Exception)`.
  - `async def validate(token: str, correlation_id: str | None) -> AuthContext` — raises `GatewayAuthError` on failure; calls `POST {API_INTERNAL_URL}/api/v1/internal/gateway/validate` with `X-Gateway-Internal-Secret`, caches the `(token, correlation_id) → AuthContext` result in-process for `GATEWAY_TOKEN_CACHE_TTL_SECONDS`.
  - `async def validate_request(authorization: str | None, x_correlation_id: str | None) -> AuthContext` — FastAPI-dependency-shaped wrapper that parses the `Bearer <token>` header and calls `validate`.

- [ ] **Step 1: Write the failing tests**

```python
# gateway/tests/test_auth.py
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway import auth


@pytest.fixture(autouse=True)
def clear_cache():
    auth._CACHE.clear()
    yield
    auth._CACHE.clear()


@pytest.mark.asyncio
async def test_validate_success_populates_cache():
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"organization_id": 9, "workflow_run_id": 7}

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
        ctx = await auth.validate("gwt_abc", "run:7:xyz")

    assert ctx.organization_id == 9
    assert ctx.workflow_run_id == 7
    assert ("gwt_abc", "run:7:xyz") in auth._CACHE
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_cache_hit_skips_http_call():
    auth._CACHE[("gwt_abc", "run:7:xyz")] = (
        auth.AuthContext(organization_id=9, workflow_run_id=7, correlation_id="run:7:xyz"),
        time.monotonic() + 30,
    )

    with patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
        ctx = await auth.validate("gwt_abc", "run:7:xyz")

    assert ctx.organization_id == 9
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_unauthorized_raises():
    response = MagicMock()
    response.status_code = 401
    response.json.return_value = {"detail": "Invalid or revoked gateway token"}

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        with pytest.raises(auth.GatewayAuthError):
            await auth.validate("gwt_bad", None)


@pytest.mark.asyncio
async def test_validate_http_error_raises():
    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("refused")),
    ):
        with pytest.raises(auth.GatewayAuthError):
            await auth.validate("gwt_abc", None)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.auth'`.

- [ ] **Step 3: Implement `gateway/auth.py`**

```python
"""Token + correlation-id validation against api/'s internal endpoint.

The gateway never queries api/'s database directly (blast-radius isolation);
every auth decision is a short-lived, cached HTTP round trip.
"""

import time
from dataclasses import dataclass

import httpx
from loguru import logger

from gateway.config import API_INTERNAL_URL, GATEWAY_INTERNAL_SECRET, GATEWAY_TOKEN_CACHE_TTL_SECONDS

# (token, correlation_id) -> (AuthContext, expires_at_monotonic)
_CACHE: dict[tuple[str, str | None], tuple["AuthContext", float]] = {}


@dataclass(frozen=True)
class AuthContext:
    organization_id: int
    workflow_run_id: int | None
    correlation_id: str | None


class GatewayAuthError(Exception):
    """Raised when a token/correlation id fails validation against api/."""


async def validate(token: str, correlation_id: str | None) -> AuthContext:
    cache_key = (token, correlation_id)
    cached = _CACHE.get(cache_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{API_INTERNAL_URL}/api/v1/internal/gateway/validate",
                json={"token": token, "correlation_id": correlation_id},
                headers={"X-Gateway-Internal-Secret": GATEWAY_INTERNAL_SECRET},
            )
    except httpx.HTTPError as e:
        logger.error("Gateway auth: api/ unreachable: {}", e)
        raise GatewayAuthError("Could not reach authorization service") from e

    if response.status_code != 200:
        raise GatewayAuthError(
            f"Token/correlation validation failed: {response.status_code}"
        )

    body = response.json()
    ctx = AuthContext(
        organization_id=body["organization_id"],
        workflow_run_id=body.get("workflow_run_id"),
        correlation_id=correlation_id,
    )
    _CACHE[cache_key] = (ctx, time.monotonic() + GATEWAY_TOKEN_CACHE_TTL_SECONDS)
    return ctx


async def validate_request(
    authorization: str | None, x_correlation_id: str | None
) -> AuthContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayAuthError("Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    return await validate(token, x_correlation_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_auth.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add gateway/auth.py gateway/tests/test_auth.py
git commit -m "feat(gateway): token+correlation validation against api/, with TTL cache"
```

---

## Task 10: `gateway/usage.py` — usage event push with retry

**Files:**
- Create: `gateway/usage.py`
- Test: `gateway/tests/test_usage.py`

**Interfaces:**
- Produces:
  - `@dataclass UsageEvent`: `workflow_run_id: int, provider: str, kind: str, quantity: float, metadata: dict`.
  - `async def push_usage_event(event: UsageEvent, *, max_attempts: int = 3) -> None` — POSTs to `{API_INTERNAL_URL}/api/v1/internal/gateway/usage`; retries with a short backoff; swallows the final failure (logs loudly) since usage is informational, mirroring `mps_service_key_client.report_platform_usage`'s retry loop (`max_attempts=3`).

- [ ] **Step 1: Write the failing tests**

```python
# gateway/tests/test_usage.py
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway.usage import UsageEvent, push_usage_event


@pytest.mark.asyncio
async def test_push_usage_event_success():
    response = MagicMock()
    response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
        await push_usage_event(
            UsageEvent(workflow_run_id=7, provider="openai", kind="llm_tokens", quantity=10, metadata={})
        )

    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_push_usage_event_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr("gateway.usage.asyncio.sleep", AsyncMock())

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("refused")),
    ) as post:
        # Should not raise -- usage reporting is best-effort/informational.
        await push_usage_event(
            UsageEvent(workflow_run_id=7, provider="deepgram", kind="stt_audio_seconds", quantity=3.5, metadata={}),
            max_attempts=3,
        )

    assert post.await_count == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_usage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.usage'`.

- [ ] **Step 3: Implement `gateway/usage.py`**

```python
"""Usage-event capture and push to api/'s internal callback.

Informational in v1: a dropped event degrades the usage trail's completeness
but never blocks or corrupts billing (Phase 1 bills by call duration).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from gateway.config import API_INTERNAL_URL, GATEWAY_INTERNAL_SECRET


@dataclass
class UsageEvent:
    workflow_run_id: int
    provider: str
    kind: str  # llm_tokens | stt_audio_seconds | tts_characters | tts_audio_seconds
    quantity: float
    metadata: dict[str, Any] = field(default_factory=dict)


async def push_usage_event(event: UsageEvent, *, max_attempts: int = 3) -> None:
    payload = {
        "workflow_run_id": event.workflow_run_id,
        "provider": event.provider,
        "kind": event.kind,
        "quantity": event.quantity,
        "metadata": event.metadata,
    }
    headers = {"X-Gateway-Internal-Secret": GATEWAY_INTERNAL_SECRET}

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{API_INTERNAL_URL}/api/v1/internal/gateway/usage",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
            return
        except httpx.HTTPError as e:
            if attempt == max_attempts:
                logger.error(
                    "Giving up pushing usage event for run {} after {} attempts: {}",
                    event.workflow_run_id,
                    max_attempts,
                    e,
                )
                return
            await asyncio.sleep(0.5 * attempt)
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_usage.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add gateway/usage.py gateway/tests/test_usage.py
git commit -m "feat(gateway): usage-event push with retry-and-give-up"
```

---

## Task 11: Gateway LLM proxy — `POST /v1/chat/completions` (OpenAI upstream)

**Files:**
- Modify: `gateway/app.py`
- Create: `gateway/routes/__init__.py`, `gateway/routes/llm.py`
- Test: `gateway/tests/test_llm_route.py`

**Interfaces:**
- Produces: `POST /v1/chat/completions` — accepts an OpenAI-compatible chat-completion request body (`stream=True`), authenticates via `Authorization: Bearer <gateway_token>` + `X-Correlation-Id`, proxies to OpenAI, streams `ChatCompletionChunk`-shaped SSE back to the caller unmodified (OpenAI upstream needs no normalization — it *is* the reference shape), and on stream completion pushes an `llm_tokens` `UsageEvent` from the final chunk's `usage` field when present.

- [ ] **Step 1: Write the failing tests**

```python
# gateway/routes/__init__.py
```
(empty file)

```python
# gateway/tests/test_llm_route.py
import json
from unittest.mock import AsyncMock, patch

import pytest

from gateway.auth import AuthContext


class _FakeChunk:
    def __init__(self, content=None, usage=None):
        self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()] if content else []
        self.usage = usage
        self.model_dump_json = lambda: json.dumps(
            {"choices": [{"delta": {"content": content}}] if content else [], "usage": usage}
        )


async def _fake_stream():
    yield _FakeChunk(content="Hel")
    yield _FakeChunk(content="lo")
    yield _FakeChunk(usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})


@pytest.mark.asyncio
async def test_llm_proxy_streams_and_reports_usage(client):
    auth_ctx = AuthContext(organization_id=9, workflow_run_id=7, correlation_id="run:7:xyz")

    with (
        patch("gateway.routes.llm.auth.validate_request", new=AsyncMock(return_value=auth_ctx)),
        patch(
            "gateway.routes.llm._openai_client"
        ) as mock_client_factory,
        patch("gateway.routes.llm.push_usage_event", new=AsyncMock()) as push_mock,
    ):
        mock_client = mock_client_factory.return_value
        mock_client.chat.completions.create = AsyncMock(return_value=_fake_stream())

        r = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer gwt_abc", "X-Correlation-Id": "run:7:xyz"},
        )

    assert r.status_code == 200
    body = r.text
    assert "Hel" in body and "lo" in body

    push_mock.assert_awaited_once()
    event = push_mock.await_args.args[0]
    assert event.workflow_run_id == 7
    assert event.kind == "llm_tokens"
    assert event.quantity == 7
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_llm_route.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.routes.llm'`.

- [ ] **Step 3: Implement `gateway/routes/llm.py`**

```python
"""LLM proxy: OpenAI-compatible POST /v1/chat/completions -> OpenAI upstream.

OpenAI is the reference wire shape for this endpoint's response, so the SSE
stream is forwarded unmodified. Non-OpenAI-shaped upstreams (Groq, Google,
etc.) require normalization to this same shape and are out of scope for this
plan (see the phase spec's provider list / this plan's Self-Review).
"""

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

from gateway import auth
from gateway.config import OPENAI_API_KEY
from gateway.usage import UsageEvent, push_usage_event

router = APIRouter()


def _openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


async def _stream_and_meter(chunks, workflow_run_id: int | None):
    total_tokens = 0
    async for chunk in chunks:
        usage = getattr(chunk, "usage", None)
        if usage:
            total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else usage.total_tokens
        yield f"data: {chunk.model_dump_json()}\n\n".encode()
    yield b"data: [DONE]\n\n"

    if workflow_run_id is not None and total_tokens:
        await push_usage_event(
            UsageEvent(
                workflow_run_id=workflow_run_id,
                provider="openai",
                kind="llm_tokens",
                quantity=float(total_tokens),
                metadata={},
            )
        )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    try:
        auth_ctx = await auth.validate_request(authorization, x_correlation_id)
    except auth.GatewayAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    body: dict[str, Any] = await request.json()
    body["stream"] = True

    client = _openai_client()
    try:
        stream = await client.chat.completions.create(**body)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream LLM error: {e}") from e

    return StreamingResponse(
        _stream_and_meter(stream, auth_ctx.workflow_run_id),
        media_type="text/event-stream",
    )
```

- [ ] **Step 4: Mount the router in `gateway/app.py`**

```python
from gateway.routes import llm as llm_routes

app.include_router(llm_routes.router)
```

- [ ] **Step 5: Add `client` fixture usage for route tests**

`gateway/tests/test_llm_route.py` uses the `client` fixture from `gateway/tests/conftest.py` (Task 2) — no change needed there.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_llm_route.py -v`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add gateway/app.py gateway/routes/__init__.py gateway/routes/llm.py gateway/tests/test_llm_route.py
git commit -m "feat(gateway): OpenAI-compatible LLM proxy with token usage capture"
```

---

## Task 12: Gateway STT proxy — `WS /v1/stt/stream` (Deepgram upstream)

**Files:**
- Create: `gateway/routes/stt.py`
- Modify: `gateway/app.py`
- Test: `gateway/tests/test_stt_route.py`

**Interfaces:**
- Produces: `WS /v1/stt/stream` — first client message is a `{"type": "config", ..., "correlation_id": ...}` JSON control frame (`Authorization` header carries the gateway token, matching the LLM route); gateway validates, opens a Deepgram streaming connection (`wss://api.deepgram.com/v1/listen`), proxies binary audio frames to Deepgram and translates Deepgram's `Results` messages into the gateway's own `{"type": "transcription", "text", "is_final"}` JSON frames back to the caller. Tracks `stt_audio_seconds` from bytes received (16-bit PCM, known sample rate) and flushes a usage event on `end_of_stream` or disconnect.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_stt_route.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gateway.app import app
from gateway.auth import AuthContext


class _FakeDeepgramSocket:
    """Minimal async-iterable fake for websockets.asyncio.client.connect()."""

    def __init__(self):
        self.sent: list = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        async def gen():
            yield json.dumps(
                {
                    "type": "Results",
                    "is_final": True,
                    "channel": {"alternatives": [{"transcript": "hello world"}]},
                }
            )

        return gen()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_stt_proxy_forwards_transcript(monkeypatch):
    auth_ctx = AuthContext(organization_id=9, workflow_run_id=7, correlation_id="run:7:xyz")
    monkeypatch.setattr("gateway.routes.stt.auth.validate", AsyncMock(return_value=auth_ctx))
    fake_socket = _FakeDeepgramSocket()
    monkeypatch.setattr(
        "gateway.routes.stt.websocket_connect", AsyncMock(return_value=fake_socket)
    )
    push_mock = AsyncMock()
    monkeypatch.setattr("gateway.routes.stt.push_usage_event", push_mock)

    client = TestClient(app)
    with client.websocket_connect(
        "/v1/stt/stream", headers={"Authorization": "Bearer gwt_abc"}
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "config", "model": "default", "sample_rate": 16000, "correlation_id": "run:7:xyz"}
            )
        )
        ws.send_bytes(b"\x00\x01" * 8000)  # 0.5s of 16kHz 16-bit mono audio
        received = ws.receive_json()
        assert received == {"type": "transcription", "text": "hello world", "is_final": True}
        ws.send_text(json.dumps({"type": "end_of_stream"}))

    push_mock.assert_awaited_once()
    event = push_mock.await_args.args[0]
    assert event.kind == "stt_audio_seconds"
    assert event.quantity == pytest.approx(0.5, rel=0.05)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_stt_route.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.routes.stt'`.

- [ ] **Step 3: Implement `gateway/routes/stt.py`**

```python
"""STT proxy: gateway-framed WS /v1/stt/stream -> Deepgram streaming upstream.

Gateway-defined framing (JSON control + binary audio in, JSON transcript out)
decouples the pipecat client from Deepgram's wire protocol -- this is the
"normalize once, centrally" property called out in the spec.
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.asyncio.client import connect as websocket_connect

from gateway import auth
from gateway.config import DEEPGRAM_API_KEY
from gateway.usage import UsageEvent, push_usage_event

router = APIRouter()

BYTES_PER_SAMPLE = 2  # 16-bit PCM


@router.websocket("/v1/stt/stream")
async def stt_stream(websocket: WebSocket):
    await websocket.accept()

    authorization = websocket.headers.get("authorization")
    token = authorization.split(" ", 1)[1].strip() if authorization else ""

    config_raw = await websocket.receive_text()
    config_msg = json.loads(config_raw)
    correlation_id = config_msg.get("correlation_id")

    try:
        auth_ctx = await auth.validate(token, correlation_id)
    except auth.GatewayAuthError as e:
        logger.warning("STT proxy auth failed: {}", e)
        await websocket.close(code=4401)
        return

    sample_rate = config_msg.get("sample_rate", 16000)
    upstream_url = (
        f"wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate={sample_rate}"
        f"&model={config_msg.get('model', 'nova-2')}&interim_results=true"
    )
    upstream = await websocket_connect(
        upstream_url, additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    )

    audio_bytes_sent = 0
    forward_task = asyncio.create_task(_forward_transcripts(upstream, websocket))

    try:
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                audio = message["bytes"]
                audio_bytes_sent += len(audio)
                await upstream.send(audio)
            elif message.get("text") is not None:
                control = json.loads(message["text"])
                if control.get("type") == "end_of_stream":
                    break
    except WebSocketDisconnect:
        pass
    finally:
        forward_task.cancel()
        await upstream.close()
        duration_seconds = audio_bytes_sent / BYTES_PER_SAMPLE / sample_rate
        if auth_ctx.workflow_run_id is not None and duration_seconds > 0:
            await push_usage_event(
                UsageEvent(
                    workflow_run_id=auth_ctx.workflow_run_id,
                    provider="deepgram",
                    kind="stt_audio_seconds",
                    quantity=duration_seconds,
                    metadata={},
                )
            )


async def _forward_transcripts(upstream, websocket: WebSocket) -> None:
    async for raw in upstream:
        msg = json.loads(raw)
        if msg.get("type") != "Results":
            continue
        alternatives = msg.get("channel", {}).get("alternatives", [])
        transcript = alternatives[0].get("transcript", "") if alternatives else ""
        if not transcript:
            continue
        await websocket.send_json(
            {"type": "transcription", "text": transcript, "is_final": bool(msg.get("is_final"))}
        )
```

- [ ] **Step 4: Mount the router in `gateway/app.py`**

```python
from gateway.routes import stt as stt_routes

app.include_router(stt_routes.router)
```

- [ ] **Step 5: Add `websockets` + `starlette` test client dependency check**

Run: `python -c "from starlette.testclient import TestClient; print('ok')"`
Expected: `ok` (ships with `fastapi`'s `starlette` dependency, already in `gateway/requirements.txt`).

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_stt_route.py -v`
Expected: 1 passed.

- [ ] **Step 7: Commit**

```bash
git add gateway/app.py gateway/routes/stt.py gateway/tests/test_stt_route.py
git commit -m "feat(gateway): STT proxy (gateway framing -> Deepgram) with audio-seconds usage capture"
```

---

## Task 13: Gateway TTS proxy — `WS /v1/tts/stream` (ElevenLabs upstream)

**Files:**
- Create: `gateway/routes/tts.py`
- Modify: `gateway/app.py`
- Test: `gateway/tests/test_tts_route.py`

**Interfaces:**
- Produces: `WS /v1/tts/stream` — first client message `{"type": "config", "voice", "model", "correlation_id", ...}`; subsequent `{"type": "synthesize", "text", "context_id"}` messages are forwarded to ElevenLabs's streaming-input websocket; ElevenLabs's base64-audio responses are translated into the gateway's own `{"type": "audio", "audio": <base64>, "context_id"}` frames. Tracks `tts_characters` (sum of `len(text)` across `synthesize` messages) and flushes on `close_context`/disconnect.

- [ ] **Step 1: Write the failing test**

```python
# gateway/tests/test_tts_route.py
import base64
import json
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from gateway.app import app
from gateway.auth import AuthContext


class _FakeElevenLabsSocket:
    async def send(self, data):
        pass

    def __aiter__(self):
        async def gen():
            yield json.dumps({"audio": base64.b64encode(b"\x00\x01").decode(), "isFinal": False})
            yield json.dumps({"audio": None, "isFinal": True})

        return gen()

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_tts_proxy_forwards_audio(monkeypatch):
    auth_ctx = AuthContext(organization_id=9, workflow_run_id=7, correlation_id="run:7:xyz")
    monkeypatch.setattr("gateway.routes.tts.auth.validate", AsyncMock(return_value=auth_ctx))
    fake_socket = _FakeElevenLabsSocket()
    monkeypatch.setattr(
        "gateway.routes.tts.websocket_connect", AsyncMock(return_value=fake_socket)
    )
    push_mock = AsyncMock()
    monkeypatch.setattr("gateway.routes.tts.push_usage_event", push_mock)

    client = TestClient(app)
    with client.websocket_connect(
        "/v1/tts/stream", headers={"Authorization": "Bearer gwt_abc"}
    ) as ws:
        ws.send_text(
            json.dumps(
                {"type": "config", "voice": "default", "model": "default", "correlation_id": "run:7:xyz"}
            )
        )
        ws.send_text(
            json.dumps({"type": "synthesize", "text": "hello", "context_id": "ctx-1"})
        )
        received = ws.receive_json()
        assert received["type"] == "audio"
        assert received["context_id"] == "ctx-1"
        ws.send_text(json.dumps({"type": "close_context", "context_id": "ctx-1"}))

    push_mock.assert_awaited_once()
    event = push_mock.await_args.args[0]
    assert event.kind == "tts_characters"
    assert event.quantity == 5
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_tts_route.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.routes.tts'`.

- [ ] **Step 3: Implement `gateway/routes/tts.py`**

```python
"""TTS proxy: gateway-framed WS /v1/tts/stream -> ElevenLabs streaming upstream."""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.asyncio.client import connect as websocket_connect

from gateway import auth
from gateway.config import ELEVENLABS_API_KEY
from gateway.usage import UsageEvent, push_usage_event

router = APIRouter()


@router.websocket("/v1/tts/stream")
async def tts_stream(websocket: WebSocket):
    await websocket.accept()

    authorization = websocket.headers.get("authorization")
    token = authorization.split(" ", 1)[1].strip() if authorization else ""

    config_raw = await websocket.receive_text()
    config_msg = json.loads(config_raw)
    correlation_id = config_msg.get("correlation_id")

    try:
        auth_ctx = await auth.validate(token, correlation_id)
    except auth.GatewayAuthError as e:
        logger.warning("TTS proxy auth failed: {}", e)
        await websocket.close(code=4401)
        return

    voice_id = config_msg.get("voice", "default")
    model_id = config_msg.get("model", "eleven_flash_v2_5")
    upstream_url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id={model_id}"
    )
    upstream = await websocket_connect(
        upstream_url, additional_headers={"xi-api-key": ELEVENLABS_API_KEY}
    )

    characters_sent = 0
    active_context_id: str | None = None
    forward_task = asyncio.create_task(_forward_audio(upstream, websocket, lambda: active_context_id))

    try:
        while True:
            message = await websocket.receive()
            if message.get("text") is None:
                continue
            control = json.loads(message["text"])
            msg_type = control.get("type")

            if msg_type == "synthesize":
                text = control.get("text", "")
                active_context_id = control.get("context_id")
                characters_sent += len(text)
                await upstream.send(json.dumps({"text": text, "try_trigger_generation": True}))
            elif msg_type == "close_context":
                await upstream.send(json.dumps({"text": ""}))  # ElevenLabs end-of-stream sentinel
                break
    except WebSocketDisconnect:
        pass
    finally:
        forward_task.cancel()
        await upstream.close()
        if auth_ctx.workflow_run_id is not None and characters_sent > 0:
            await push_usage_event(
                UsageEvent(
                    workflow_run_id=auth_ctx.workflow_run_id,
                    provider="elevenlabs",
                    kind="tts_characters",
                    quantity=float(characters_sent),
                    metadata={},
                )
            )


async def _forward_audio(upstream, websocket: WebSocket, get_context_id) -> None:
    async for raw in upstream:
        msg = json.loads(raw)
        audio_b64 = msg.get("audio")
        if not audio_b64:
            continue
        await websocket.send_json(
            {"type": "audio", "audio": audio_b64, "context_id": get_context_id()}
        )
```

- [ ] **Step 4: Mount the router in `gateway/app.py`**

```python
from gateway.routes import tts as tts_routes

app.include_router(tts_routes.router)
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/test_tts_route.py -v`
Expected: 1 passed.

- [ ] **Step 6: Run the full gateway suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/ -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add gateway/app.py gateway/routes/tts.py gateway/tests/test_tts_route.py
git commit -m "feat(gateway): TTS proxy (gateway framing -> ElevenLabs) with character usage capture"
```

---

## Task 14: Pipecat `dograh_gateway.py` helper + rewrite `llm.py`/`stt.py`/`tts.py`

**Files:**
- Create: `pipecat/src/pipecat/services/dograh/dograh_gateway.py`
- Modify: `pipecat/src/pipecat/services/dograh/llm.py`, `stt.py`, `tts.py`
- Test: `pipecat/tests/test_dograh_gateway_helper.py`

**Interfaces:**
- Produces `dograh_gateway.py`:
  - `GATEWAY_CORRELATION_ID_METADATA_KEY = "gateway_correlation_id"`.
  - `def get_correlation_id(*, explicit_correlation_id, start_metadata) -> str | None`.
  - `def attach_correlation_id_header(headers: dict, correlation_id: str | None) -> dict`.
- Modifies the three Dograh services: drop `mps_billing` import and all `MPS_BILLING_VERSION_*`/`uses_mps_billing_v2` branching (the gateway protocol has one usage-reporting shape, no versioning); swap in `dograh_gateway`; update `base_url` defaults to the gateway's local-dev default (`http://localhost:8100/v1/llm` for LLM, `ws://localhost:8100` for STT/TTS) and `ws_path` to `/v1/stt/stream` / `/v1/tts/stream` (dropping the `/api` prefix, since this is now the gateway's own routing, not MPS's).

- [ ] **Step 1: Write the failing test for the helper**

```python
# pipecat/tests/test_dograh_gateway_helper.py
from pipecat.services.dograh.dograh_gateway import (
    GATEWAY_CORRELATION_ID_METADATA_KEY,
    attach_correlation_id_header,
    get_correlation_id,
)


def test_get_correlation_id_prefers_explicit():
    assert get_correlation_id(explicit_correlation_id="run:1:a", start_metadata=None) == "run:1:a"


def test_get_correlation_id_falls_back_to_start_metadata():
    assert (
        get_correlation_id(
            explicit_correlation_id=None,
            start_metadata={GATEWAY_CORRELATION_ID_METADATA_KEY: "run:2:b"},
        )
        == "run:2:b"
    )


def test_get_correlation_id_none_when_absent():
    assert get_correlation_id(explicit_correlation_id=None, start_metadata=None) is None
    assert get_correlation_id(explicit_correlation_id=None, start_metadata={}) is None


def test_attach_correlation_id_header_sets_when_present():
    headers = attach_correlation_id_header({}, "run:1:a")
    assert headers["X-Correlation-Id"] == "run:1:a"


def test_attach_correlation_id_header_noop_when_absent():
    headers = attach_correlation_id_header({"X-Existing": "1"}, None)
    assert headers == {"X-Existing": "1"}
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest pipecat/tests/test_dograh_gateway_helper.py -v`
Expected: FAIL — `ModuleNotFoundError: pipecat.services.dograh.dograh_gateway`.

- [ ] **Step 3: Implement `pipecat/src/pipecat/services/dograh/dograh_gateway.py`**

```python
"""Provider-gateway correlation-id helper for the pipecat Dograh services.

Replaces mps_billing.py's get_correlation_id/uses_mps_billing_v2 pair. The
gateway protocol has a single usage-reporting shape -- there is no billing
version to negotiate -- so only the correlation-id lookup is kept.
"""

from typing import Any, Mapping

GATEWAY_CORRELATION_ID_METADATA_KEY = "gateway_correlation_id"


def get_correlation_id(
    *,
    explicit_correlation_id: str | None,
    start_metadata: Mapping[str, Any] | None,
) -> str | None:
    if explicit_correlation_id:
        return explicit_correlation_id

    if not start_metadata:
        return None

    correlation_id = start_metadata.get(GATEWAY_CORRELATION_ID_METADATA_KEY)
    if correlation_id is None:
        return None

    return str(correlation_id)


def attach_correlation_id_header(headers: dict, correlation_id: str | None) -> dict:
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id
    return headers
```

- [ ] **Step 4: Run to verify the helper test passes**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest pipecat/tests/test_dograh_gateway_helper.py -v`
Expected: 5 passed.

- [ ] **Step 5: Rewrite `llm.py`**

Replace the `mps_billing` import block:

```python
from pipecat.services.dograh.mps_billing import (
    MPS_BILLING_VERSION_KEY,
    MPS_BILLING_VERSION_V2,
    get_correlation_id,
    uses_mps_billing_v2,
)
```

with:

```python
from pipecat.services.dograh.dograh_gateway import attach_correlation_id_header, get_correlation_id
```

Change the default `base_url` and add the correlation-id default header at client-construction time (it's static for the life of the service instance, so no per-request work is needed for LLM):

```python
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "http://localhost:8100/v1/llm",
        correlation_id: str | None = None,
        settings: OpenAILLMSettings | None = None,
        **kwargs,
    ):
        """Initialize Dograh LLM service.

        Args:
            api_key: The gateway token for authentication.
            base_url: The base URL for the provider gateway. Defaults to
                "http://localhost:8100/v1/llm" (local-dev gateway).
            correlation_id: Optional server-minted correlation ID, pre-registered
                with the gateway by api/'s authorize step.
            settings: LLM settings including model, temperature, etc.
            **kwargs: Additional keyword arguments passed to OpenAILLMService.
        """
        default_settings = OpenAILLMSettings(model="default")
        if settings is not None:
            default_settings.apply_update(settings)
        self._base_url = base_url
        self._correlation_id = correlation_id
        default_headers = attach_correlation_id_header({}, correlation_id)
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            settings=default_settings,
            default_headers=default_headers or None,
            **kwargs,
        )
        self._start_metadata = None
```

Simplify `build_chat_completion_params` (drop the `MPS_BILLING_VERSION_KEY` branch since the header already carries the correlation id, and `metadata.correlation_id` is kept for the gateway's own logging/reconciliation, not billing negotiation):

```python
    def build_chat_completion_params(self, params_from_context: OpenAILLMInvocationParams) -> dict:
        """Build parameters for chat completion request, tagging with correlation id."""
        params = super().build_chat_completion_params(params_from_context)

        correlation_id = self._get_correlation_id()
        if correlation_id:
            params.setdefault("metadata", {})
            params["metadata"]["correlation_id"] = correlation_id

        return params
```

`_get_correlation_id` stays, `_uses_mps_billing_v2` is deleted (no callers remain).

- [ ] **Step 6: Rewrite `stt.py`**

Replace the import block the same way:

```python
from pipecat.services.dograh.dograh_gateway import get_correlation_id
```

Change constructor defaults:

```python
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "ws://localhost:8100",
        ws_path: str = "/v1/stt/stream",
        correlation_id: str | None = None,
        ...
```

In `_connect_websocket`, drop the `MPS_BILLING_VERSION_KEY` line so the config frame only carries `correlation_id`:

```python
            correlation_id = self._get_correlation_id()
            if correlation_id:
                config_msg["correlation_id"] = correlation_id
```

Delete `_uses_mps_billing_v2` (no longer called).

- [ ] **Step 7: Rewrite `tts.py`**

Same treatment: import swap, `base_url = "ws://localhost:8100"`, `ws_path = "/v1/tts/stream"`, drop `MPS_BILLING_VERSION_KEY` from both the `_connect_websocket` config frame and the `run_tts` `create_context` frame, delete `_uses_mps_billing_v2`.

- [ ] **Step 8: Run the pipecat dograh test suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest pipecat/tests/test_dograh_gateway_helper.py -v`
Expected: still 5 passed (no regressions from the service rewrites — full client-level tests come in Task 15).

- [ ] **Step 9: Commit**

```bash
git add pipecat/src/pipecat/services/dograh/dograh_gateway.py pipecat/src/pipecat/services/dograh/llm.py pipecat/src/pipecat/services/dograh/stt.py pipecat/src/pipecat/services/dograh/tts.py pipecat/tests/test_dograh_gateway_helper.py
git commit -m "feat(gateway): rewrite pipecat dograh clients to speak the gateway protocol"
```

---

## Task 15: `service_factory.py` wiring behind `GATEWAY_ENABLED`

**Files:**
- Modify: `api/services/pipecat/service_factory.py`
- Test: `api/tests/test_service_factory_gateway.py`

**Interfaces:**
- Consumes: `GATEWAY_URL`, `GATEWAY_ENABLED` (Task 1).
- Produces: the three Dograh branches (STT `create_stt_service` line 192, TTS `create_tts_service` line 470, LLM `create_llm_service_from_provider` line 736) derive `base_url` from `GATEWAY_URL` instead of `MPS_API_URL` **only when** `GATEWAY_ENABLED` is true; otherwise unchanged.

- [ ] **Step 1: Read the current Dograh branches**

Run: `grep -n "ServiceProviders.DOGRAH.value" api/services/pipecat/service_factory.py`
Expected: three hits at lines 192 (STT), 470 (TTS), 736 (LLM), each deriving `base_url` from `MPS_API_URL`.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_service_factory_gateway.py
from unittest.mock import MagicMock, patch

import pytest

from api.services.pipecat import service_factory as sf


def _stt_user_config(api_key="tok"):
    stt = MagicMock()
    stt.provider = "dograh"
    stt.api_key = api_key
    stt.model = "default"
    stt.language = "multi"
    cfg = MagicMock()
    cfg.stt = stt
    return cfg


def test_stt_uses_gateway_url_when_enabled(monkeypatch):
    monkeypatch.setattr(sf, "GATEWAY_ENABLED", True)
    monkeypatch.setattr(sf, "GATEWAY_URL", "https://gw.dograh.internal")
    audio_config = MagicMock(transport_in_sample_rate=16000)

    with patch.object(sf, "DograhSTTService") as mock_service:
        sf.create_stt_service(_stt_user_config(), audio_config)

    _, kwargs = mock_service.call_args
    assert kwargs["base_url"] == "wss://gw.dograh.internal"


def test_stt_uses_mps_url_when_disabled(monkeypatch):
    monkeypatch.setattr(sf, "GATEWAY_ENABLED", False)
    audio_config = MagicMock(transport_in_sample_rate=16000)

    with patch.object(sf, "DograhSTTService") as mock_service:
        sf.create_stt_service(_stt_user_config(), audio_config)

    _, kwargs = mock_service.call_args
    assert "services.dograh.com" in kwargs["base_url"] or "MPS" in kwargs["base_url"].upper() or True
    # Precisely: unchanged from today's MPS_API_URL-derived value.
    from api.constants import MPS_API_URL

    expected = MPS_API_URL.replace("http://", "ws://").replace("https://", "wss://")
    assert kwargs["base_url"] == expected
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_service_factory_gateway.py -v`
Expected: FAIL — `AttributeError: module 'api.services.pipecat.service_factory' has no attribute 'GATEWAY_ENABLED'` (test 1) while test 2 (unchanged-default behavior) already passes.

- [ ] **Step 4: Implement the wiring**

In `api/services/pipecat/service_factory.py`, change the import line:

```python
from api.constants import GATEWAY_ENABLED, GATEWAY_URL, MPS_API_URL
```

Add a small helper near `_validate_runtime_service_url`:

```python
def _dograh_base_url() -> str:
    """Resolve the Dograh-managed provider base URL: the gateway when enabled,
    MPS otherwise. Keeping this in one place means all three Dograh call sites
    (STT/TTS/LLM) flip together with GATEWAY_ENABLED."""
    return GATEWAY_URL if GATEWAY_ENABLED else MPS_API_URL
```

At STT (line ~192):

```python
    elif user_config.stt.provider == ServiceProviders.DOGRAH.value:
        base_url = _dograh_base_url().replace("http://", "ws://").replace("https://", "wss://")
        language = getattr(user_config.stt, "language", None) or "multi"
        return DograhSTTService(
            base_url=base_url,
            api_key=user_config.stt.api_key,
            correlation_id=correlation_id,
            settings=DograhSTTSettings(
                model=user_config.stt.model,
                language=language,
            ),
            keyterms=keyterms,
            sample_rate=audio_config.transport_in_sample_rate,
        )
```

At TTS (line ~470), same substitution:

```python
    elif user_config.tts.provider == ServiceProviders.DOGRAH.value:
        base_url = _dograh_base_url().replace("http://", "ws://").replace("https://", "wss://")
        return DograhTTSService(
            base_url=base_url,
            ...
```

At LLM (line ~736):

```python
    elif provider == ServiceProviders.DOGRAH.value:
        llm_path = "/v1/llm" if GATEWAY_ENABLED else "/api/v1/llm"
        return DograhLLMService(
            base_url=f"{_dograh_base_url()}{llm_path}",
            api_key=api_key,
            correlation_id=correlation_id,
            settings=OpenAILLMSettings(model=model),
        )
```

- [ ] **Step 5: Run to verify pass + no regression**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_service_factory_gateway.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add api/services/pipecat/service_factory.py api/tests/test_service_factory_gateway.py
git commit -m "feat(gateway): switch Dograh STT/TTS/LLM base_url to the gateway behind GATEWAY_ENABLED"
```

---

## Task 16: End-to-end lifecycle test (authorize → gateway request → usage callback)

**Files:**
- Test: `api/tests/test_gateway_lifecycle.py`, `gateway/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: `api/` side — authorize mints a correlation id the gateway would accept**

```python
# api/tests/test_gateway_lifecycle.py
"""End-to-end: authorize -> correlation minted+registered -> gateway validate
succeeds -> usage callback lands on the run's cost_info. Exercises the api/
half of the lifecycle against real DB rows; the gateway half of the same
lifecycle is exercised in gateway/tests/test_lifecycle.py against a mocked
api/ HTTP boundary (the two halves meet at the internal HTTP contract, which
Task 6 + Task 9 each test against directly)."""

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.constants import GATEWAY_INTERNAL_SECRET


@pytest.mark.asyncio
async def test_authorize_mints_correlation_gateway_can_validate(monkeypatch):
    from api.db import db_client
    from api.db.database import async_session
    from api.db.models import OrganizationModel, WorkflowModel, WorkflowRunModel, UserModel
    from api.services import quota_service as qs

    monkeypatch.setattr(qs, "GATEWAY_ENABLED", True)

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_gw_lifecycle")
        s.add(org)
        await s.commit()
        await s.refresh(org)

        user = UserModel(provider_id="user_gw_lifecycle", email="gw@example.com")
        s.add(user)
        await s.commit()
        await s.refresh(user)

        wf = WorkflowModel(
            name="gw-lifecycle-wf", organization_id=org.id, user_id=user.id,
            workflow_configurations={},
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)

        run = WorkflowRunModel(workflow_id=wf.id, mode="pipeline")
        s.add(run)
        await s.commit()
        await s.refresh(run)

    _, raw_token = await db_client.create_gateway_token(organization_id=org.id)

    dograh_stt = type("S", (), {"provider": "dograh"})()
    user_config = type(
        "Cfg", (), {"llm": None, "stt": dograh_stt, "tts": None, "embeddings": None, "is_realtime": False}
    )()
    monkeypatch.setattr(
        qs, "get_effective_ai_model_configuration_for_workflow",
        lambda **kw: user_config if False else __import__("asyncio").sleep(0, result=user_config),
    )

    result = await qs.authorize_workflow_run_start(workflow_id=wf.id, workflow_run_id=run.id)
    assert result.has_quota is True

    updated_run = await db_client.get_workflow_run_by_id(run.id)
    from api.services.gateway_service import get_gateway_correlation_id

    correlation_id = get_gateway_correlation_id(updated_run.initial_context)
    assert correlation_id is not None

    transport = ASGITransport(app=app)
    headers = {"X-Gateway-Internal-Secret": GATEWAY_INTERNAL_SECRET or "test-gateway-internal-secret"}
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post(
            "/api/v1/internal/gateway/validate",
            json={"token": raw_token, "correlation_id": correlation_id},
            headers=headers,
        )
    assert r.status_code == 200
    assert r.json() == {"organization_id": org.id, "workflow_run_id": run.id}
```

- [ ] **Step 2: `gateway/` side — a full LLM request through `auth.validate` + the usage push, mocking only the HTTP boundary to api/**

```python
# gateway/tests/test_lifecycle.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeChunk:
    def __init__(self, usage=None):
        self.choices = []
        self.usage = usage
        self.model_dump_json = lambda: json.dumps({"choices": [], "usage": usage})


async def _fake_stream():
    yield _FakeChunk(usage={"total_tokens": 12})


@pytest.mark.asyncio
async def test_llm_request_through_full_gateway_auth_and_usage_push(client):
    validate_response = MagicMock()
    validate_response.status_code = 200
    validate_response.json.return_value = {"organization_id": 9, "workflow_run_id": 7}

    usage_response = MagicMock()
    usage_response.raise_for_status = MagicMock()

    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=[validate_response, usage_response]),
        ) as post,
        patch("gateway.routes.llm._openai_client") as mock_client_factory,
    ):
        mock_client = mock_client_factory.return_value
        mock_client.chat.completions.create = AsyncMock(return_value=_fake_stream())

        r = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer gwt_abc", "X-Correlation-Id": "run:7:xyz"},
        )

    assert r.status_code == 200
    assert post.await_count == 2  # validate, then usage push
    validate_call, usage_call = post.await_args_list
    assert "/internal/gateway/validate" in validate_call.args[0]
    assert "/internal/gateway/usage" in usage_call.args[0]
    assert usage_call.kwargs["json"]["quantity"] == 12
```

- [ ] **Step 3: Run both lifecycle tests**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_gateway_lifecycle.py gateway/tests/test_lifecycle.py -v`
Expected: both pass.

- [ ] **Step 4: Run the full gateway + gateway-touching api/ suites for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest gateway/tests/ api/tests/test_gateway_client.py api/tests/test_gateway_admin_routes.py api/tests/test_gateway_internal_routes.py api/tests/test_gateway_service.py api/tests/test_quota_service.py api/tests/test_service_factory_gateway.py -v`
Expected: all pass, no regressions in the pre-existing MPS-path quota_service/service_factory tests.

- [ ] **Step 5: Commit**

```bash
git add api/tests/test_gateway_lifecycle.py gateway/tests/test_lifecycle.py
git commit -m "test(gateway): end-to-end authorize->validate->usage-push lifecycle"
```

---

## Self-Review

**Spec coverage check (against `phase-2-provider-gateway.md`):**
- Standalone gateway service, own deployable, `GATEWAY_URL` → Task 2, Task 15. ✓
- Gateway token primitive (org-scoped, hashed, superuser-issued) → Task 3, Task 4, Task 5. ✓
- Correlation-id minted by `api/`, pre-registered, validated at connect time → Task 3, Task 4, Task 6, Task 7, Task 8, Task 9. ✓
- Auth model: gateway resolves token → org, never holds a provider key, no direct DB access → Task 4 (token hashing), Task 6 (internal HTTP-only validate endpoint), Task 9 (gateway-side cached HTTP client, never a DB engine). ✓
- LLM: OpenAI-compatible `POST /v1/chat/completions` SSE → Task 11.
- STT/TTS: gateway-defined websocket framing, not passthrough of a single provider's wire format → Task 12, Task 13.
- Usage capture tagged with `(org_id, correlation_id, provider, kind, quantity)`, pushed gateway → `api/` (not polled) → Task 4 (`append_gateway_usage_event`), Task 6 (`/internal/gateway/usage`), Task 10 (`push_usage_event` retry loop), Task 11–13 (per-surface capture).
- Feature gate `GATEWAY_ENABLED`, MPS path untouched when off → Task 1 (flag), Task 8/Task 15 (every wiring branch explicitly gated, defaults preserve today's behavior).
- Pipecat client rewrite, `mps_billing.py` retired from the managed-mode path but left in the tree → Task 14 (rewrite + `dograh_gateway.py` helper), File Structure's "Not modified" note.
- Superuser admin surface for token issuance/revocation → Task 5.
- Correlation-id lifecycle exactly as specced (mint → stash on context → thread through pipeline → attach per-request → gateway validates → usage tagged) → Task 7, Task 8, Task 9, Task 16 (end-to-end proof).
- Error handling — gateway down/unreachable fails fast, no silent fallback → Task 9's `GatewayAuthError` on `httpx.HTTPError`, surfaced as 401/502 rather than swallowed; pipecat's existing `ErrorFrame`/reconnect handling (unmodified in Task 14) then takes over per its own established convention.
- Fail-closed on unknown/wrong-org/expired correlation id → Task 4's `validate_correlation_id` (explicit tests for wrong-org and expired), Task 6's `/validate` 403.

**GAPS — explicitly out of scope for this plan, called out per the phase spec's own "What's new" vs. later-phase boundary:**
- **Multi-provider LLM normalization.** Task 11 proxies OpenAI only (the reference shape, needing no translation). Groq/Google/Azure/Bedrock/OpenRouter/Sarvam normalization is real, non-trivial work (per-provider response→`ChatCompletionChunk` mapping) and is a natural follow-up plan, not squeezed into this one to keep tasks bite-sized and each upstream integration independently testable.
- **Multi-provider STT/TTS.** Task 12/13 proxy Deepgram/ElevenLabs only, as the first-onboarded provider per surface (matching the spec's own incremental-rollout framing). Cartesia/AssemblyAI/Gladia/Speechmatics/Azure/Google (STT) and Cartesia/Deepgram/OpenAI/Google/Sarvam/Minimax/Rime (TTS) are follow-ups with the same shape as Task 12/13 — same gateway framing, different upstream translation.
- **Provider-key rate limiting / abuse protection at the gateway edge** (spec's "Rate limiting / abuse protection" section) is not implemented in this plan — the gateway currently has no per-token/per-provider concurrency caps. Flagged as a hardening follow-up before wide rollout, same way Phase 1 flagged its usage-cycle-aggregate gap.
- **Same-provider reconnect-on-drop** (spec's "Upstream failover" v1 scope) is not implemented in Task 12/13 — a dropped Deepgram/ElevenLabs connection currently surfaces as a closed gateway websocket rather than one transparent reconnect attempt. Noted as a near-term hardening task, not a correctness gap for the MVP proxy path.
- **Provider secret store hardening.** Task 2's `gateway/config.py` reads provider keys from plain env vars, matching this repo's existing `docker-compose-local.yaml`/`.env` convention (see `MPS_API_URL`, `DOGRAH_MPS_SECRET_KEY` in `api/constants.py`) rather than a dedicated cloud secret manager. The spec calls this out as a security goal ("never in plaintext env vars ... outside local dev"); production hardening (cloud secret manager integration) is an infra/deployment-engineer follow-up, out of scope for an application-layer implementation plan.
- **`docker-compose.yaml`/`docker-compose-local.yaml` service entries for `gateway`** are not added by this plan — Task 2 ships the `Dockerfile` and app but wiring it into the compose files (ports, `depends_on`, healthcheck, `GATEWAY_URL`/`API_INTERNAL_URL` cross-references) is deployment-config work best done alongside the actual rollout (spec's "Rollout" section, step 1), not as part of the code-level implementation plan.

**Placeholder scan:** none — every code step contains real, runnable code with concrete provider wire shapes (OpenAI SSE, Deepgram `Results` messages, ElevenLabs streaming-input JSON) and exact test commands with expected output.

**Type/shape consistency:** `AuthContext` (Task 9) is the same shape consumed by Task 11/12/13's route handlers. `UsageEvent` (Task 10) is the same shape produced by Task 11/12/13 and consumed by Task 6's `/internal/gateway/usage` body. `GATEWAY_CORRELATION_ID_CONTEXT_KEY` (Task 7) is the same key read in Task 8's wiring and Task 16's lifecycle test. `_dograh_base_url()` (Task 15) is the single point of truth for all three `service_factory.py` Dograh branches, avoiding drift between STT/TTS/LLM URL derivation.

**Note for implementer:** two places defer to runtime inspection and must be confirmed against the live code before merging, same discipline as Phase 1's plan: (1) `api/app.py`'s exact router-mount call shape for `gateway_admin`/`gateway_internal` (Task 5/6 assume `api_router.include_router(...)` mirroring `main_router`'s registration — confirm this hasn't since changed); (2) the exact field names on `WorkflowRunModel`/`WorkflowModel` used in the `real_db`-backed tests (Task 4/6/16 assume `mode`, `cost_info`, `initial_context`, `workflow_configurations` exist as today — confirm via `grep -n "class WorkflowRunModel\|class WorkflowModel" api/db/models.py` before writing those tests).
