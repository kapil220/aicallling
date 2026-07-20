# Google Sheets Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google Sheets as a bidirectional campaign integration — a spreadsheet tab as a lead source (read), and per-call results written back to a results sheet (write) — behind a `GOOGLE_SHEETS_INTEGRATION` flag, without disturbing the existing CSV-only path. WhatsApp (Phase 5b) is explicitly **out of scope**.

**Architecture:** `GoogleSheetsSyncService` implements the existing `CampaignSourceSyncService` ABC (`api/services/campaign/source_sync.py:26`) and slots into `source_sync_factory.get_sync_service()` alongside `CSVSyncService` — zero changes to the campaign orchestration task (`api/tasks/campaign_tasks.py::sync_campaign_source`). A new `GoogleOAuthClient` resolves/refreshes per-org Google credentials stored on `IntegrationModel` (`provider="google_sheets"`) + `ExternalCredentialModel` (encrypted refresh/access tokens). A new write-back path is a new step in `api/tasks/workflow_completion.py::process_workflow_completion`, guarded by a per-campaign `writeback_config` JSON column, that enqueues an arq task batching pending write-backs per spreadsheet and applying them via `values.batchUpdate`, with an idempotency ledger table (`campaign_sheets_writeback`).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy (async), Alembic, PostgreSQL, arq, `google-api-python-client` + `google-auth` (new deps), pytest + pytest-asyncio, loguru.

## Grounding note (deviation from spec doc)

The spec (`docs/specs/managed-saas/phase-5-google-sheets-integration.md`) states refresh/access tokens are stored via "the existing `ExternalCredentialModel` encrypted-secret mechanism." **This mechanism does not exist in the codebase today** — `ExternalCredentialModel.credential_data` (`api/db/models.py:1078`) is a plain `JSON` column with no encryption layer anywhere in `api/` (verified: no `cryptography`/`Fernet` usage exists in `api/services/`, `api/db/`, or `api/utils/`, and `api/constants.py` has no `ENCRYPTION_KEY`). Task 2 below adds a minimal `Fernet`-based encryption helper (`api/utils/credential_crypto.py`) and a new `CREDENTIAL_ENCRYPTION_KEY` env var, and Task 3 uses it when writing/reading the refresh/access tokens on `ExternalCredentialModel.credential_data`. This is new, real infrastructure this phase must add — not a reuse of something pre-existing.

`ToolType.INTEGRATION` referenced in the spec is actually `ToolCategory.INTEGRATION` (`api/enums.py:150`) — noted for anyone cross-referencing; this plan does not touch `ToolCategory` at all (Sheets credentials live on `IntegrationModel`/`ExternalCredentialModel`, not the tool catalog).

## Global Constraints

- **WhatsApp is out of scope.** Nothing in this plan touches messaging channels; "Phase 5b — WhatsApp Integration" is a separate, later spec.
- Feature gate: new constant `GOOGLE_SHEETS_INTEGRATION` (env, default `"false"`). All new routes/services are inert unless enabled; existing CSV-only campaigns are byte-for-byte unaffected when disabled.
- Tenant isolation: every Sheets read/write and every credential lookup is scoped by `organization_id` (see `api/AGENTS.md`).
- DB access lives in `api/db/*_client.py` mixins; domain logic in `api/services/campaign/sources/` and `api/services/integrations/google/`; routes stay thin.
- Tests run against the test DB: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest ...`.
- **No live Google API calls in tests.** The Sheets/OAuth HTTP clients are injected/mocked at the boundary (`GoogleSheetsClient`, `GoogleOAuthClient`) so unit/integration tests never hit `googleapis.com`.
- **DB-integration tests need the project's pgvector Postgres** (`docker-compose-local.yaml`) running and `api/.env.test` pointed at it — same as all other DB-backed tests in this repo; this plan adds no new infra requirement beyond what's already documented in `docs/contribution/setup.mdx`.
- Idempotency: write-back per run uses ledger row keyed by `workflow_run_id` unique constraint, mirroring Phase 1's `idempotency_key=debit:{run_id}` pattern (`api/db/billing_client.py`).
- Migrations are created via `./scripts/makemigrate.sh "description"` and applied with `./scripts/migrate.sh`. **`down_revision` must be set to the actual alembic head at execution time** — at spec-writing time this is `b1f0c0de0001` (Phase 1's `add_local_billing_engine_tables`, `api/alembic/versions/b1f0c0de0001_add_local_billing_engine_tables.py`), but other phase plans in this program may land migrations first; run `alembic heads` (with `api/.env` sourced) immediately before Task 2 Step 3 to confirm.

---

## File Structure

**Create:**
- `api/services/campaign/sources/google_sheets.py` — `GoogleSheetsSyncService(CampaignSourceSyncService)`: read-side sync (validate + `sync_source_data`), mirrors `CSVSyncService`.
- `api/services/integrations/google/__init__.py` — package marker.
- `api/services/integrations/google/oauth_client.py` — `GoogleOAuthClient`: resolve/refresh org credentials against `IntegrationModel`/`ExternalCredentialModel`.
- `api/services/integrations/google/sheets_client.py` — `GoogleSheetsClient`: thin wrapper around `googleapiclient.discovery.build("sheets", "v4", ...)` exposing `values_get`/`values_batch_update`, injectable for tests.
- `api/services/integrations/google/source_id.py` — `encode_sheet_source_id` / `decode_sheet_source_id` for the `gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}` encoding.
- `api/services/campaign/writeback/__init__.py` — package marker.
- `api/services/campaign/writeback/sheets_writeback.py` — column-mapping resolution + row/value construction from `WorkflowRunModel`/`WorkflowModel`.
- `api/services/campaign/writeback/writeback_service.py` — `write_back_run(workflow_run_id)`: ledger check, batch-or-single `values.batchUpdate` call, backoff, ledger state transition.
- `api/tasks/sheets_writeback_tasks.py` — arq task `sheets_writeback(ctx, workflow_run_id)`, enqueued from `process_workflow_completion`.
- `api/utils/credential_crypto.py` — `encrypt_credential`/`decrypt_credential` (Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`).
- `api/db/google_sheets_client.py` — `GoogleSheetsClient` DB mixin: `campaign_sheets_writeback` ledger CRUD + idempotency check.
- `api/tests/test_google_sheets_source_id.py` — unit tests for encode/decode.
- `api/tests/test_google_sheets_sync_service.py` — unit + integration tests for `GoogleSheetsSyncService` (mocked Sheets API).
- `api/tests/test_google_oauth_client.py` — unit tests for token refresh/`invalid_grant` handling.
- `api/tests/test_sheets_writeback.py` — unit tests for column mapping + row construction.
- `api/tests/test_sheets_writeback_service.py` — integration tests for idempotency, batching, backoff (mocked API).
- `api/tests/test_credential_crypto.py` — unit tests for the new encryption helper.

**Modify:**
- `api/services/campaign/source_sync_factory.py` — register `"google_sheets": GoogleSheetsSyncService`.
- `api/routes/campaign.py` — relax `source_type` regex (line 155) to `^(csv|google_sheets)$`; `CreateCampaignRequest` gains optional `writeback_config`.
- `api/db/models.py` — add `CampaignModel.writeback_config` (JSON, nullable), `CampaignSheetsWritebackModel`.
- `api/db/db_client.py` — add `GoogleSheetsClient` mixin to the `DBClient` base list.
- `api/db/campaign_client.py` — `update_campaign` already accepts `**kwargs`, so `writeback_config` writes through unchanged; add a small `get_campaign_writeback_config(campaign_id)` helper used by the write-back guard.
- `api/db/integration_client.py` — add `get_integration_by_org_and_provider(organization_id, provider)` (single-row lookup; v1 is one connection per org per provider).
- `api/constants.py` — add `GOOGLE_SHEETS_INTEGRATION`, `CREDENTIAL_ENCRYPTION_KEY`, `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`/`GOOGLE_OAUTH_REDIRECT_URI`.
- `api/tasks/workflow_completion.py` — add Step 3.5 (after `run_integrations_post_workflow_run`, before Step 4): enqueue `sheets_writeback` when the run's campaign has `writeback_config`.
- `api/routes/campaign.py` (`get_campaign_source_download_url`, line 946) — no functional change needed; add a one-line comment noting `google_sheets` falls into the existing non-CSV rejection branch (already correct behavior per spec).
- `api/requirements.txt` — add `google-api-python-client==2.187.0`, `google-auth==2.42.0`, `google-auth-oauthlib==1.2.2`, `cryptography==43.0.3`.

---

## Task 1: Feature flags, constants, and new dependencies

**Files:**
- Modify: `api/constants.py`, `api/requirements.txt`

**Interfaces:**
- Produces: `GOOGLE_SHEETS_INTEGRATION: bool`, `CREDENTIAL_ENCRYPTION_KEY: str | None`, `GOOGLE_OAUTH_CLIENT_ID: str | None`, `GOOGLE_OAUTH_CLIENT_SECRET: str | None`, `GOOGLE_OAUTH_REDIRECT_URI: str | None`, `GOOGLE_SHEETS_SCOPES: list[str]`.

- [ ] **Step 1: Read the existing flag pattern**

Run: `grep -n "BILLING_ENGINE\|ENABLE_AWS_S3" api/constants.py`
Expected: shows the `os.getenv(...)` boolean/string idiom (`BILLING_ENGINE` at line 37, `ENABLE_AWS_S3` at line 43).

- [ ] **Step 2: Add the constants**

In `api/constants.py`, near `BILLING_ENGINE`:

```python
# Google Sheets bidirectional campaign integration (Phase 5). Off by default.
GOOGLE_SHEETS_INTEGRATION = os.getenv("GOOGLE_SHEETS_INTEGRATION", "false").lower() == "true"

# Symmetric key (Fernet, url-safe base64, 32 bytes) used to encrypt OAuth
# refresh/access tokens at rest on ExternalCredentialModel.credential_data.
# No existing credential-encryption mechanism exists in this codebase today;
# this is new infrastructure this phase introduces.
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY")

GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
```

- [ ] **Step 3: Add dependencies**

Append to `api/requirements.txt`:

```
google-api-python-client==2.187.0
google-auth==2.42.0
google-auth-oauthlib==1.2.2
cryptography==43.0.3
```

- [ ] **Step 4: Install and verify import**

Run: `source venv/bin/activate && pip install -r api/requirements.txt && python -c "from googleapiclient.discovery import build; from google.oauth2.credentials import Credentials; from cryptography.fernet import Fernet; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Verify constants import**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "from api.constants import GOOGLE_SHEETS_INTEGRATION, CREDENTIAL_ENCRYPTION_KEY, GOOGLE_SHEETS_SCOPES; print(GOOGLE_SHEETS_INTEGRATION, GOOGLE_SHEETS_SCOPES)"`
Expected: `False ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file']`

- [ ] **Step 6: Commit**

```bash
git add api/constants.py api/requirements.txt
git commit -m "feat(sheets): add GOOGLE_SHEETS_INTEGRATION flag, OAuth config, new deps"
```

---

## Task 2: Credential encryption helper

**Files:**
- Create: `api/utils/credential_crypto.py`
- Test: `api/tests/test_credential_crypto.py`

**Interfaces:**
- Consumes: `CREDENTIAL_ENCRYPTION_KEY` (Task 1).
- Produces: `def encrypt_credential(plaintext: str) -> str`, `def decrypt_credential(ciphertext: str) -> str`. Raises `RuntimeError` if `CREDENTIAL_ENCRYPTION_KEY` is unset when either is called.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_credential_crypto.py
import pytest
from cryptography.fernet import Fernet

from api.utils import credential_crypto


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr(
        credential_crypto, "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    token = "ya29.some-refresh-token-value"
    ciphertext = credential_crypto.encrypt_credential(token)
    assert ciphertext != token
    assert credential_crypto.decrypt_credential(ciphertext) == token


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(credential_crypto, "CREDENTIAL_ENCRYPTION_KEY", None)
    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
        credential_crypto.encrypt_credential("x")


def test_tampered_ciphertext_raises(monkeypatch):
    monkeypatch.setattr(
        credential_crypto, "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    ciphertext = credential_crypto.encrypt_credential("token")
    tampered = ciphertext[:-4] + "abcd"
    with pytest.raises(Exception):
        credential_crypto.decrypt_credential(tampered)
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_credential_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: api.utils.credential_crypto`.

- [ ] **Step 3: Implement**

```python
# api/utils/credential_crypto.py
"""Symmetric encryption for OAuth tokens and other sensitive credential
values stored in JSON columns (ExternalCredentialModel.credential_data).

No credential-encryption mechanism existed in this codebase before Phase 5's
Google Sheets integration; this module is new, minimal infrastructure.
"""

from cryptography.fernet import Fernet

from api.constants import CREDENTIAL_ENCRYPTION_KEY


def _fernet() -> Fernet:
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set; cannot encrypt/decrypt credentials"
        )
    return Fernet(CREDENTIAL_ENCRYPTION_KEY.encode())


def encrypt_credential(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_credential_crypto.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add api/utils/credential_crypto.py api/tests/test_credential_crypto.py
git commit -m "feat(sheets): Fernet-based credential encryption helper"
```

---

## Task 3: Data models & migration

**Files:**
- Modify: `api/db/models.py`
- Migration: generated under `api/alembic/versions/`

**Interfaces:**
- Produces:
  - `CampaignModel.writeback_config` (`api/db/models.py:760` region) — JSON, nullable, default `null`.
  - `CampaignSheetsWritebackModel` (table `campaign_sheets_writeback`): `id`, `workflow_run_id:int unique`, `campaign_id:int`, `state:str` (`pending`/`written`/`failed`), `error:str|None`, `attempts:int`, `written_at:datetime|None`, `created_at`.

- [ ] **Step 1: Add `writeback_config` to `CampaignModel`**

In `api/db/models.py`, in `CampaignModel` (`source_type`/`source_id` block, line 774-776):

```python
    # Source configuration
    source_type = Column(String, nullable=False, default="csv")
    source_id = Column(String, nullable=False)  # CSV file key or gsheet:id:tab:range

    # Optional per-campaign Google Sheets write-back configuration. Shape:
    # {"provider": "google_sheets", "spreadsheet_id": ..., "sheet_name": ...,
    #  "mode": "update"|"append", "column_mapping": {"F": "call_disposition", ...}}
    # None = write-back disabled for this campaign (default, all existing campaigns).
    writeback_config = Column(JSON, nullable=True, default=None)
```

- [ ] **Step 2: Add `CampaignSheetsWritebackModel`**

After `QueuedRunModel` (`api/db/models.py`, ends ~line 910), add:

```python
class CampaignSheetsWritebackModel(Base):
    """Idempotency ledger for Google Sheets write-back (Phase 5).

    One row per workflow_run that has (or is attempting to have) its result
    written to a results sheet. workflow_run_id is unique so a retried
    process_workflow_completion never produces a duplicate append/update.
    """

    __tablename__ = "campaign_sheets_writeback"

    id = Column(Integer, primary_key=True, index=True)
    workflow_run_id = Column(
        Integer,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    state = Column(
        Enum("pending", "written", "failed", name="sheets_writeback_state"),
        nullable=False,
        default="pending",
    )
    error = Column(String, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    written_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    workflow_run = relationship("WorkflowRunModel")
    campaign = relationship("CampaignModel")

    __table_args__ = (
        Index("ix_sheets_writeback_campaign_state", "campaign_id", "state"),
    )
```

- [ ] **Step 3: Confirm the current alembic head**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && cd api && alembic heads`
Expected: a single head revision id (e.g. `b1f0c0de0001` at spec-writing time, but confirm live — other phases may have landed migrations since). Use this value as `down_revision` if autogen doesn't pick it up automatically.

- [ ] **Step 4: Generate the migration**

Run: `source venv/bin/activate && set -a && source api/.env && set +a && ./scripts/makemigrate.sh "add google sheets writeback ledger and campaign writeback config"`
Expected: a new file in `api/alembic/versions/` adding `campaigns.writeback_config` and creating `campaign_sheets_writeback`.

- [ ] **Step 5: Inspect the migration**

Open the generated file. Verify: `down_revision` points at the head confirmed in Step 3 (fix by hand if autogen picked a stale head due to a race with another phase's migration); `campaign_sheets_writeback` has the unique constraint on `workflow_run_id`; the `sheets_writeback_state` enum is created before the table that uses it (alembic-postgresql-enum, already a project dependency per `requirements.txt:11`, should handle this — confirm the generated `upgrade()`/`downgrade()` include enum create/drop).

- [ ] **Step 6: Apply and verify against test DB**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && ./scripts/migrate.sh && python -c "
import asyncio
from sqlalchemy import text
import api.db.models
from api.db.database import engine

async def go():
    async with engine.begin() as c:
        r = await c.execute(text(\"select to_regclass('campaign_sheets_writeback'), (select column_name from information_schema.columns where table_name='campaigns' and column_name='writeback_config')\"))
        print(r.fetchone())

asyncio.run(go())
"`
Expected: `('campaign_sheets_writeback', 'writeback_config')`.

- [ ] **Step 7: Commit**

```bash
git add api/db/models.py api/alembic/versions/
git commit -m "feat(sheets): add campaign_sheets_writeback ledger and campaign.writeback_config"
```

---

## Task 4: `source_id` encode/decode for Sheets

**Files:**
- Create: `api/services/integrations/google/source_id.py`
- Test: `api/tests/test_google_sheets_source_id.py`

**Interfaces:**
- Produces:
  - `@dataclass SheetSourceRef`: `spreadsheet_id: str, sheet_name: str, a1_range: str | None`.
  - `def encode_sheet_source_id(spreadsheet_id: str, sheet_name: str, a1_range: str | None = None) -> str` → `"gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}"` (empty segment if `a1_range` is `None`).
  - `def decode_sheet_source_id(source_id: str) -> SheetSourceRef` → raises `ValueError` if the prefix isn't `gsheet:` or the id is malformed.
  - `def sheet_range(ref: SheetSourceRef) -> str` → `f"{ref.sheet_name}!{ref.a1_range}"` if `a1_range` set, else `ref.sheet_name` (full-tab range, per spec: "defaults to the full used range of the tab if omitted").

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_google_sheets_source_id.py
import pytest

from api.services.integrations.google.source_id import (
    SheetSourceRef,
    decode_sheet_source_id,
    encode_sheet_source_id,
    sheet_range,
)


def test_encode_with_range():
    assert (
        encode_sheet_source_id("1AbC", "Leads", "A1:F200")
        == "gsheet:1AbC:Leads:A1:F200"
    )


def test_encode_without_range():
    assert encode_sheet_source_id("1AbC", "Leads") == "gsheet:1AbC:Leads:"


def test_decode_round_trip_with_range():
    ref = decode_sheet_source_id("gsheet:1AbC:Leads:A1:F200")
    assert ref == SheetSourceRef("1AbC", "Leads", "A1:F200")


def test_decode_round_trip_without_range():
    ref = decode_sheet_source_id("gsheet:1AbC:Leads:")
    assert ref == SheetSourceRef("1AbC", "Leads", None)


def test_decode_rejects_non_gsheet_prefix():
    with pytest.raises(ValueError, match="gsheet:"):
        decode_sheet_source_id("csv/some/file.csv")


def test_decode_rejects_missing_parts():
    with pytest.raises(ValueError):
        decode_sheet_source_id("gsheet:only_id")


def test_sheet_range_full_tab_when_no_range():
    ref = SheetSourceRef("1AbC", "Leads", None)
    assert sheet_range(ref) == "Leads"


def test_sheet_range_with_explicit_range():
    ref = SheetSourceRef("1AbC", "Leads", "A1:F200")
    assert sheet_range(ref) == "Leads!A1:F200"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_sheets_source_id.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.integrations.google.source_id`.

- [ ] **Step 3: Implement**

```python
# api/services/integrations/google/__init__.py
```
(empty file)

```python
# api/services/integrations/google/source_id.py
"""Encode/decode a Google Sheets campaign source_id.

CampaignModel.source_id (api/db/models.py) is a single String column shared
with CSV (a bare file key). Sheets needs spreadsheet + tab + optional range,
so it's packed as `gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}` — the
trailing segment is empty when no explicit range was chosen (full-tab read).
"""

from dataclasses import dataclass

_PREFIX = "gsheet:"


@dataclass(frozen=True)
class SheetSourceRef:
    spreadsheet_id: str
    sheet_name: str
    a1_range: str | None


def encode_sheet_source_id(
    spreadsheet_id: str, sheet_name: str, a1_range: str | None = None
) -> str:
    return f"{_PREFIX}{spreadsheet_id}:{sheet_name}:{a1_range or ''}"


def decode_sheet_source_id(source_id: str) -> SheetSourceRef:
    if not source_id.startswith(_PREFIX):
        raise ValueError(f"source_id must start with '{_PREFIX}': {source_id!r}")
    body = source_id[len(_PREFIX):]
    parts = body.split(":", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"malformed gsheet source_id: {source_id!r}")
    spreadsheet_id, sheet_name = parts[0], parts[1]
    a1_range = parts[2] if len(parts) == 3 and parts[2] else None
    return SheetSourceRef(spreadsheet_id, sheet_name, a1_range)


def sheet_range(ref: SheetSourceRef) -> str:
    if ref.a1_range:
        return f"{ref.sheet_name}!{ref.a1_range}"
    return ref.sheet_name
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_sheets_source_id.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add api/services/integrations/google/__init__.py api/services/integrations/google/source_id.py api/tests/test_google_sheets_source_id.py
git commit -m "feat(sheets): source_id encode/decode for gsheet:id:tab:range"
```

---

## Task 5: `GoogleOAuthClient` — credential resolution and refresh

**Files:**
- Create: `api/services/integrations/google/oauth_client.py`
- Modify: `api/db/integration_client.py`
- Test: `api/tests/test_google_oauth_client.py`

**Interfaces:**
- Consumes: `IntegrationModel`/`ExternalCredentialModel` (existing), `encrypt_credential`/`decrypt_credential` (Task 2), `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` (Task 1).
- Produces:
  - `api/db/integration_client.py`: `async def get_integration_by_org_and_provider(organization_id: int, provider: str) -> IntegrationModel | None`.
  - `class GoogleOAuthClient`:
    - `async def get_valid_access_token(self, organization_id: int) -> str` — loads the org's `google_sheets` integration + its `ExternalCredentialModel`, decrypts, refreshes via a injectable `refresh_fn` if `access_token_expires_at` has passed, persists the refreshed token+expiry back (encrypted), returns the access token.
    - Raises `GoogleCredentialsNotConnected` if no active integration exists for the org.
    - Raises `GoogleCredentialsRevoked` if refresh raises `invalid_grant`; on that path, marks `IntegrationModel.is_active = False` via `db_client.update_integration_status`.

- [ ] **Step 1: Add the integration lookup to `IntegrationClient`**

In `api/db/integration_client.py`, add:

```python
    async def get_integration_by_org_and_provider(
        self, organization_id: int, provider: str
    ) -> IntegrationModel | None:
        """Single active-connection lookup — v1 supports exactly one Google
        Sheets connection per org (api/db/models.py IntegrationModel)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(IntegrationModel).where(
                    IntegrationModel.organization_id == organization_id,
                    IntegrationModel.provider == provider,
                    IntegrationModel.is_active == True,
                )
            )
            return result.scalars().first()
```

- [ ] **Step 2: Write the failing tests (mocked refresh function, no live Google calls)**

```python
# api/tests/test_google_oauth_client.py
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from api.db import db_client
from api.db.models import ExternalCredentialModel, IntegrationModel, OrganizationModel
from api.enums import WebhookCredentialType
from api.services.integrations.google.oauth_client import (
    GoogleCredentialsNotConnected,
    GoogleCredentialsRevoked,
    GoogleOAuthClient,
)
from api.utils.credential_crypto import encrypt_credential


async def _make_org(provider_id: str) -> int:
    from api.db.database import async_session
    async with async_session() as s:
        org = OrganizationModel(provider_id=provider_id)
        s.add(org)
        await s.commit()
        await s.refresh(org)
        return org.id


async def _connect_google(org_id: int, *, expired: bool) -> None:
    integration = await db_client.create_integration(
        integration_id="conn-1",
        provider="google_sheets",
        organization_id=org_id,
        connection_details={"google_account_email": "ops@acme.test"},
    )
    async from api.db.database import async_session  # noqa: keep local for clarity
    async with async_session() as s:
        cred = ExternalCredentialModel(
            organization_id=org_id,
            name="Google Sheets",
            credential_type=WebhookCredentialType.NONE.value,
            credential_data={
                "refresh_token": encrypt_credential("refresh-abc"),
                "access_token": encrypt_credential("access-old"),
                "expires_at": (
                    datetime.now(UTC) - timedelta(seconds=1)
                    if expired
                    else datetime.now(UTC) + timedelta(hours=1)
                ).isoformat(),
                "integration_id": integration.id,
            },
            created_by=1,
        )
        s.add(cred)
        await s.commit()


@pytest.mark.asyncio
async def test_returns_cached_token_when_not_expired(monkeypatch):
    org_id = await _make_org("org_oauth_fresh")
    await _connect_google(org_id, expired=False)

    client = GoogleOAuthClient(refresh_fn=AsyncMock())
    token = await client.get_valid_access_token(org_id)
    assert token == "access-old"
    client._refresh_fn.assert_not_called()


@pytest.mark.asyncio
async def test_refreshes_and_persists_when_expired(monkeypatch):
    org_id = await _make_org("org_oauth_expired")
    await _connect_google(org_id, expired=True)

    refresh = AsyncMock(return_value={"access_token": "access-new", "expires_in": 3600})
    client = GoogleOAuthClient(refresh_fn=refresh)
    token = await client.get_valid_access_token(org_id)
    assert token == "access-new"

    # Second call should now be cached (no second refresh).
    refresh.reset_mock()
    token2 = await client.get_valid_access_token(org_id)
    assert token2 == "access-new"
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_no_connection_raises_not_connected():
    org_id = await _make_org("org_oauth_none")
    client = GoogleOAuthClient(refresh_fn=AsyncMock())
    with pytest.raises(GoogleCredentialsNotConnected):
        await client.get_valid_access_token(org_id)


@pytest.mark.asyncio
async def test_invalid_grant_marks_integration_inactive():
    org_id = await _make_org("org_oauth_revoked")
    await _connect_google(org_id, expired=True)

    refresh = AsyncMock(side_effect=Exception("invalid_grant"))
    client = GoogleOAuthClient(refresh_fn=refresh)
    with pytest.raises(GoogleCredentialsRevoked):
        await client.get_valid_access_token(org_id)

    integration = await db_client.get_integration_by_org_and_provider(
        org_id, "google_sheets"
    )
    assert integration is None  # is_active flipped False, lookup filters on it
```

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_oauth_client.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.integrations.google.oauth_client`.

- [ ] **Step 4: Implement**

```python
# api/services/integrations/google/oauth_client.py
"""Resolves and refreshes per-org Google OAuth credentials.

Storage: IntegrationModel (api/db/models.py, provider="google_sheets") for
non-secret metadata, ExternalCredentialModel for the encrypted
refresh_token/access_token/expires_at (see api/utils/credential_crypto.py —
this encryption layer is new to this phase, not a pre-existing mechanism).

Refresh-on-use only (no background job) — the only call sites are campaign
sync and write-back, both infrequent enough that lazy refresh is sufficient.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from api.db import db_client
from api.utils.credential_crypto import decrypt_credential, encrypt_credential

RefreshFn = Callable[[str], Awaitable[dict[str, Any]]]


class GoogleCredentialsNotConnected(Exception):
    """Raised when the org has no active google_sheets IntegrationModel row."""


class GoogleCredentialsRevoked(Exception):
    """Raised when a refresh attempt fails with invalid_grant (revoked/expired)."""


async def _default_refresh_fn(refresh_token: str) -> dict[str, Any]:
    """Live Google token refresh — only exercised outside tests."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from api.constants import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
    )
    creds.refresh(Request())
    expires_in = 3600
    if creds.expiry:
        expires_in = max(1, int((creds.expiry - datetime.utcnow()).total_seconds()))
    return {"access_token": creds.token, "expires_in": expires_in}


class GoogleOAuthClient:
    def __init__(self, refresh_fn: Optional[RefreshFn] = None):
        self._refresh_fn = refresh_fn or _default_refresh_fn

    async def get_valid_access_token(self, organization_id: int) -> str:
        integration = await db_client.get_integration_by_org_and_provider(
            organization_id, "google_sheets"
        )
        if integration is None:
            raise GoogleCredentialsNotConnected(
                f"No active Google Sheets connection for org {organization_id}"
            )

        credential = await db_client.get_external_credential_for_integration(
            organization_id, integration.id
        )
        if credential is None:
            raise GoogleCredentialsNotConnected(
                f"Integration {integration.id} for org {organization_id} has no "
                "stored credential"
            )

        data = credential.credential_data
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at > datetime.now(UTC):
            return decrypt_credential(data["access_token"])

        refresh_token = decrypt_credential(data["refresh_token"])
        try:
            result = await self._refresh_fn(refresh_token)
        except Exception as e:
            if "invalid_grant" in str(e):
                logger.warning(
                    "Google refresh_token revoked for org {}: {}",
                    organization_id,
                    e,
                )
                await db_client.update_integration_status(
                    integration.id, is_active=False
                )
                raise GoogleCredentialsRevoked(str(e)) from e
            raise

        new_access_token = result["access_token"]
        new_expires_at = datetime.now(UTC) + timedelta(
            seconds=int(result.get("expires_in", 3600))
        )
        updated_data = dict(data)
        updated_data["access_token"] = encrypt_credential(new_access_token)
        updated_data["expires_at"] = new_expires_at.isoformat()
        await db_client.update_external_credential_data(credential.id, updated_data)
        return new_access_token
```

- [ ] **Step 5: Add the two small `db_client` helpers this leans on**

In `api/db/integration_client.py`, add (near `get_integration_by_org_and_provider` from Step 1):

```python
    async def get_external_credential_for_integration(
        self, organization_id: int, integration_id: int
    ):
        from api.db.models import ExternalCredentialModel

        async with self.async_session() as session:
            result = await session.execute(
                select(ExternalCredentialModel).where(
                    ExternalCredentialModel.organization_id == organization_id,
                    ExternalCredentialModel.is_active == True,
                )
            )
            for cred in result.scalars().all():
                if cred.credential_data.get("integration_id") == integration_id:
                    return cred
            return None

    async def update_external_credential_data(
        self, credential_id: int, credential_data: dict
    ):
        from api.db.models import ExternalCredentialModel

        async with self.async_session() as session:
            result = await session.execute(
                select(ExternalCredentialModel).where(
                    ExternalCredentialModel.id == credential_id
                )
            )
            cred = result.scalars().first()
            if cred is None:
                return None
            cred.credential_data = credential_data
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(cred)
            return cred
```

- [ ] **Step 6: Run to verify pass**

Fix the stray `async from` typo in the test (should be a normal `from api.db.database import async_session` import at module scope, mirroring `_make_org`) before running.
Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_oauth_client.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add api/services/integrations/google/oauth_client.py api/db/integration_client.py api/tests/test_google_oauth_client.py
git commit -m "feat(sheets): GoogleOAuthClient with refresh-on-use and revocation handling"
```

---

## Task 6: `GoogleSheetsClient` — thin injectable Sheets API wrapper

**Files:**
- Create: `api/services/integrations/google/sheets_client.py`
- Test: covered indirectly by Task 7/9 (this wrapper has no branching logic worth unit-testing in isolation beyond a construction smoke test, added here).

**Interfaces:**
- Produces: `class GoogleSheetsClient`:
  - `def __init__(self, access_token: str)`
  - `async def values_get(self, spreadsheet_id: str, range_: str) -> dict` — wraps `service.spreadsheets().values().get(...)`, run in a thread executor (the Google client is sync).
  - `async def values_batch_update(self, spreadsheet_id: str, data: list[dict]) -> dict` — wraps `values().batchUpdate`.
  - `async def values_append(self, spreadsheet_id: str, range_: str, values: list[list]) -> dict` — wraps `values().append`.
  - Raises `GoogleSheetsApiError(status_code: int, message: str)` (new exception, wraps `googleapiclient.errors.HttpError`) so callers (sync service, write-back) don't depend on the Google SDK's exception shape directly.

- [ ] **Step 1: Implement (no TDD cycle here — pure wrapper, exercised via Tasks 7/9's mocks)**

```python
# api/services/integrations/google/sheets_client.py
"""Thin async wrapper around the (synchronous) google-api-python-client Sheets
service. Kept intentionally minimal so GoogleSheetsSyncService and the
write-back service can depend on a small interface that's trivial to fake in
tests — no real googleapiclient.discovery.build() call happens in any test in
this plan.
"""

import asyncio
from typing import Any


class GoogleSheetsApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Sheets API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message


def _raise_from_http_error(e: Exception) -> None:
    from googleapiclient.errors import HttpError

    if isinstance(e, HttpError):
        raise GoogleSheetsApiError(e.resp.status, str(e)) from e
    raise


class GoogleSheetsClient:
    def __init__(self, access_token: str):
        self._access_token = access_token

    def _build_service(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(token=self._access_token)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    async def values_get(self, spreadsheet_id: str, range_: str) -> dict[str, Any]:
        def _call():
            service = self._build_service()
            try:
                return (
                    service.spreadsheets()
                    .values()
                    .get(spreadsheetId=spreadsheet_id, range=range_)
                    .execute()
                )
            except Exception as e:
                _raise_from_http_error(e)

        return await asyncio.to_thread(_call)

    async def values_batch_update(
        self, spreadsheet_id: str, data: list[dict]
    ) -> dict[str, Any]:
        def _call():
            service = self._build_service()
            body = {"valueInputOption": "RAW", "data": data}
            try:
                return (
                    service.spreadsheets()
                    .values()
                    .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
                    .execute()
                )
            except Exception as e:
                _raise_from_http_error(e)

        return await asyncio.to_thread(_call)

    async def values_append(
        self, spreadsheet_id: str, range_: str, values: list[list]
    ) -> dict[str, Any]:
        def _call():
            service = self._build_service()
            body = {"values": values}
            try:
                return (
                    service.spreadsheets()
                    .values()
                    .append(
                        spreadsheetId=spreadsheet_id,
                        range=range_,
                        valueInputOption="RAW",
                        insertDataOption="INSERT_ROWS",
                        body=body,
                    )
                    .execute()
                )
            except Exception as e:
                _raise_from_http_error(e)

        return await asyncio.to_thread(_call)
```

- [ ] **Step 2: Smoke-test importability**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -c "from api.services.integrations.google.sheets_client import GoogleSheetsClient, GoogleSheetsApiError; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add api/services/integrations/google/sheets_client.py
git commit -m "feat(sheets): injectable async Sheets API client wrapper"
```

---

## Task 7: `GoogleSheetsSyncService` — read side (validate + sync)

**Files:**
- Create: `api/services/campaign/sources/google_sheets.py`
- Test: `api/tests/test_google_sheets_sync_service.py`

**Interfaces:**
- Consumes: `CampaignSourceSyncService` ABC (`api/services/campaign/source_sync.py:26`), `decode_sheet_source_id`/`sheet_range` (Task 4), `GoogleOAuthClient` (Task 5), `GoogleSheetsClient` (Task 6), `db_client.get_campaign_by_id`/`bulk_create_queued_runs`/`update_campaign` (existing, used identically to `CSVSyncService`).
- Produces: `class GoogleSheetsSyncService(CampaignSourceSyncService)`:
  - `async def validate_source(self, source_id, organization_id=None) -> ValidationResult`
  - `async def sync_source_data(self, campaign_id) -> int`
  - Both accept an optional injected `oauth_client`/`sheets_client_factory` for testability (constructor params, default to the real classes).

- [ ] **Step 1: Write the failing tests (mocked `GoogleSheetsClient.values_get`)**

```python
# api/tests/test_google_sheets_sync_service.py
from unittest.mock import AsyncMock

import pytest

from api.db import db_client
from api.db.models import CampaignModel, OrganizationModel, UserModel, WorkflowModel
from api.services.campaign.sources.google_sheets import GoogleSheetsSyncService


def _values_response(rows: list[list[str]]) -> dict:
    return {"values": rows}


@pytest.mark.asyncio
async def test_validate_source_missing_phone_number():
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_get = AsyncMock(
        return_value=_values_response([["name", "email"], ["Ann", "a@x.com"]])
    )

    service = GoogleSheetsSyncService(
        oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    result = await service.validate_source(
        "gsheet:1AbC:Leads:", organization_id=1
    )
    assert result.is_valid is False
    assert "phone_number" in result.error.message


@pytest.mark.asyncio
async def test_validate_source_valid():
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_get = AsyncMock(
        return_value=_values_response(
            [["name", "phone_number"], ["Ann", "+15551234567"]]
        )
    )
    service = GoogleSheetsSyncService(
        oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    result = await service.validate_source("gsheet:1AbC:Leads:", organization_id=1)
    assert result.is_valid is True


async def _seed_campaign(source_id: str) -> int:
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_gsheet_sync")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id="user_gsheet_sync", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(
            organization_id=org.id,
            user_id=user.id,
            name="wf",
        )
        s.add(wf)
        await s.flush()
        campaign = CampaignModel(
            name="Sheets campaign",
            organization_id=org.id,
            workflow_id=wf.id,
            created_by=user.id,
            source_type="google_sheets",
            source_id=source_id,
        )
        s.add(campaign)
        await s.commit()
        await s.refresh(campaign)
        return campaign.id


@pytest.mark.asyncio
async def test_sync_source_data_creates_queued_runs_with_row_indexed_uuid():
    campaign_id = await _seed_campaign("gsheet:1AbC:Leads:")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_get = AsyncMock(
        return_value=_values_response(
            [
                ["name", "phone_number"],
                ["Ann", "+15551234567"],
                ["Bo", "+15557654321"],
                ["NoPhone", ""],
            ]
        )
    )
    service = GoogleSheetsSyncService(
        oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )

    count = await service.sync_source_data(campaign_id)
    assert count == 2

    queued = await db_client.get_queued_runs_for_campaign(campaign_id)
    uuids = sorted(r.source_uuid for r in queued)
    assert uuids == ["gsheet_1AbC_Leads_1", "gsheet_1AbC_Leads_2"]


@pytest.mark.asyncio
async def test_sync_source_data_stable_across_repeated_syncs():
    campaign_id = await _seed_campaign("gsheet:1AbC:Leads:")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_get = AsyncMock(
        return_value=_values_response(
            [["name", "phone_number"], ["Ann", "+15551234567"]]
        )
    )
    service = GoogleSheetsSyncService(
        oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    await service.sync_source_data(campaign_id)
    first = sorted(
        r.source_uuid
        for r in await db_client.get_queued_runs_for_campaign(campaign_id)
    )
    await service.sync_source_data(campaign_id)
    second = sorted(
        r.source_uuid
        for r in await db_client.get_queued_runs_for_campaign(campaign_id)
    )
    assert first == second == ["gsheet_1AbC_Leads_1"]
```

Note: `db_client.get_queued_runs_for_campaign` may not exist yet — check `api/db/campaign_client.py` for an equivalent (`grep -n "def.*queued_run" api/db/campaign_client.py`); if absent, add a minimal `async def get_queued_runs_for_campaign(self, campaign_id: int) -> list[QueuedRunModel]` to `CampaignClient` as part of this task (test-support helper, also generally useful).

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_sheets_sync_service.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.campaign.sources.google_sheets`.

- [ ] **Step 3: Implement**

```python
# api/services/campaign/sources/google_sheets.py
"""Google Sheets as a campaign lead source.

Mirrors api/services/campaign/sources/csv.py (CSVSyncService) method-for-
method: validate() delegates to the shared validate_source_data() from the
CampaignSourceSyncService ABC, sync_source_data() fetches rows via the
Sheets API in one values.get call, builds context_vars per row exactly like
CSV (pad short rows, skip rows with no phone_number), and bulk-inserts
QueuedRunModel rows.

source_uuid is `gsheet_{spreadsheet_id}_{sheet_name}_{row_idx}` — row-index
based (not content-hash based, unlike CSV's file-hash prefix) so write-back
can address "the sheet row this lead came from" directly. This means a
customer inserting/deleting rows mid-campaign can cause write-back to target
the wrong row post-sync — documented as a known v1 limitation, not handled
here (see phase-5 spec, "Open questions deferred").
"""

from typing import Callable, List, Optional

from loguru import logger

from api.db import db_client
from api.services.campaign.source_sync import (
    CampaignSourceSyncService,
    ValidationError,
    ValidationResult,
)
from api.services.integrations.google.oauth_client import GoogleOAuthClient
from api.services.integrations.google.sheets_client import GoogleSheetsClient
from api.services.integrations.google.source_id import (
    decode_sheet_source_id,
    sheet_range,
)

SheetsClientFactory = Callable[[str], GoogleSheetsClient]


class GoogleSheetsSyncService(CampaignSourceSyncService):
    def __init__(
        self,
        oauth_client: Optional[GoogleOAuthClient] = None,
        sheets_client_factory: Optional[SheetsClientFactory] = None,
    ):
        self._oauth_client = oauth_client or GoogleOAuthClient()
        self._sheets_client_factory = sheets_client_factory or (
            lambda token: GoogleSheetsClient(token)
        )

    async def _fetch_rows(
        self, organization_id: int, source_id: str
    ) -> List[List[str]]:
        ref = decode_sheet_source_id(source_id)
        access_token = await self._oauth_client.get_valid_access_token(
            organization_id
        )
        sheets_client = self._sheets_client_factory(access_token)
        response = await sheets_client.values_get(
            ref.spreadsheet_id, sheet_range(ref)
        )
        return response.get("values", [])

    async def validate_source(
        self, source_id: str, organization_id: Optional[int] = None
    ) -> ValidationResult:
        if organization_id is None:
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message="organization_id is required to validate a Google Sheets source"
                ),
            )
        try:
            rows = await self._fetch_rows(organization_id, source_id)
        except ValueError as e:
            return ValidationResult(is_valid=False, error=ValidationError(message=str(e)))

        if not rows or len(rows) < 2:
            return ValidationResult(
                is_valid=False,
                error=ValidationError(
                    message="Sheet must have a header row and at least one data row"
                ),
            )

        headers, data_rows = rows[0], rows[1:]
        return self.validate_source_data(headers, data_rows)

    async def sync_source_data(self, campaign_id: int) -> int:
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        ref = decode_sheet_source_id(campaign.source_id)
        rows = await self._fetch_rows(campaign.organization_id, campaign.source_id)

        if not rows or len(rows) < 2:
            logger.warning(f"No data found in Google Sheet for campaign {campaign_id}")
            return 0

        headers = self.normalize_headers(rows[0])
        data_rows = rows[1:]

        queued_runs = []
        for idx, row_values in enumerate(data_rows, 1):
            padded_row = row_values + [""] * (len(headers) - len(row_values))
            context_vars = dict(zip(headers, padded_row))

            if not context_vars.get("phone_number"):
                logger.debug(f"Skipping sheet row {idx}: no phone_number")
                continue

            source_uuid = f"gsheet_{ref.spreadsheet_id}_{ref.sheet_name}_{idx}"

            queued_runs.append(
                {
                    "campaign_id": campaign_id,
                    "source_uuid": source_uuid,
                    "context_variables": context_vars,
                    "state": "queued",
                }
            )

        if queued_runs:
            await db_client.bulk_create_queued_runs(queued_runs)
            logger.info(
                f"Created {len(queued_runs)} queued runs for campaign {campaign_id} "
                "from Google Sheets"
            )

        await db_client.update_campaign(
            campaign_id=campaign_id,
            total_rows=len(queued_runs),
            source_sync_status="completed",
        )

        return len(queued_runs)
```

- [ ] **Step 4: Add the `get_queued_runs_for_campaign` test-support helper if missing**

Run: `grep -n "def.*queued_run" api/db/campaign_client.py`
If no list-by-campaign method exists, add to `api/db/campaign_client.py`:

```python
    async def get_queued_runs_for_campaign(self, campaign_id: int) -> list:
        from sqlalchemy.future import select

        from api.db.models import QueuedRunModel

        async with self.async_session() as session:
            result = await session.execute(
                select(QueuedRunModel).where(
                    QueuedRunModel.campaign_id == campaign_id
                )
            )
            return list(result.scalars().all())
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_sheets_sync_service.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add api/services/campaign/sources/google_sheets.py api/db/campaign_client.py api/tests/test_google_sheets_sync_service.py
git commit -m "feat(sheets): GoogleSheetsSyncService read-side implementation of CampaignSourceSyncService"
```

---

## Task 8: Factory registration + campaign-creation regex relax

**Files:**
- Modify: `api/services/campaign/source_sync_factory.py`, `api/routes/campaign.py`

**Interfaces:**
- Produces: `get_sync_service("google_sheets")` returns `GoogleSheetsSyncService`; `CreateCampaignRequest.source_type` accepts `"csv"` or `"google_sheets"`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_source_sync_factory.py  (new file, small — only the factory contract)
import pytest

from api.services.campaign.source_sync_factory import get_sync_service
from api.services.campaign.sources.csv import CSVSyncService
from api.services.campaign.sources.google_sheets import GoogleSheetsSyncService


def test_factory_returns_csv_service():
    assert isinstance(get_sync_service("csv"), CSVSyncService)


def test_factory_returns_google_sheets_service():
    assert isinstance(get_sync_service("google_sheets"), GoogleSheetsSyncService)


def test_factory_raises_on_unknown_source():
    with pytest.raises(ValueError, match="Unknown source type"):
        get_sync_service("airtable")
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_source_sync_factory.py -v`
Expected: FAIL on `test_factory_returns_google_sheets_service` — `ValueError: Unknown source type: google_sheets`.

- [ ] **Step 3: Register in the factory**

In `api/services/campaign/source_sync_factory.py`:

```python
from api.services.campaign.source_sync import CampaignSourceSyncService
from api.services.campaign.sources.csv import CSVSyncService
from api.services.campaign.sources.google_sheets import GoogleSheetsSyncService


def get_sync_service(source_type: str) -> CampaignSourceSyncService:
    """Returns appropriate sync service based on source type"""

    services = {
        "csv": CSVSyncService,
        "google_sheets": GoogleSheetsSyncService,
    }

    service_class = services.get(source_type)
    if not service_class:
        raise ValueError(f"Unknown source type: {source_type}")

    return service_class()
```

- [ ] **Step 4: Relax the campaign-creation regex**

In `api/routes/campaign.py`, `CreateCampaignRequest` (line 155):

```python
class CreateCampaignRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    workflow_id: int
    source_type: str = Field(..., pattern="^(csv|google_sheets)$")
    source_id: str  # CSV file key, or gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}
    telephony_configuration_id: Optional[int] = None
    retry_config: Optional[RetryConfigRequest] = None
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=100)
    schedule_config: Optional[ScheduleConfigRequest] = None
    circuit_breaker: Optional[CircuitBreakerConfigRequest] = None
    writeback_config: Optional[Dict[str, Any]] = None
```

Also add a route-level guard immediately before persisting the campaign (find the `create_campaign` handler body via `grep -n "async def create_campaign" api/routes/campaign.py`): if `source_type == "google_sheets"` and `GOOGLE_SHEETS_INTEGRATION` is `False`, raise `HTTPException(status_code=403, detail="Google Sheets integration is not enabled")`. Import `GOOGLE_SHEETS_INTEGRATION` from `api.constants`.

- [ ] **Step 5: Run to verify pass, and confirm the download-url rejection still covers `google_sheets`**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_source_sync_factory.py -v`
Expected: 3 passed.

Run: `grep -n 'campaign.source_type != "csv"' api/routes/campaign.py`
Expected: shows line ~946's existing check — confirm it's a not-equal-to-csv check (already covers `google_sheets` with no code change, per spec).

- [ ] **Step 6: Commit**

```bash
git add api/services/campaign/source_sync_factory.py api/routes/campaign.py api/tests/test_source_sync_factory.py
git commit -m "feat(sheets): register GoogleSheetsSyncService in factory, relax campaign source_type validation"
```

---

## Task 9: Write-back column mapping + row construction (pure logic)

**Files:**
- Create: `api/services/campaign/writeback/__init__.py`, `api/services/campaign/writeback/sheets_writeback.py`
- Test: `api/tests/test_sheets_writeback.py`

**Interfaces:**
- Produces:
  - `def resolve_field(field_path: str, workflow_run, workflow) -> str` — resolves one of the fixed source fields from the spec's table (`call_state`, `call_disposition`, `usage_info.call_duration_seconds`, `cost_info.*`, `recording_url`, `transcript_url`, `gathered_context.*`, `raw_json`, `call_timestamp`) to a cell-ready string; missing/null → `""` (blank cell, not an error).
  - `def build_row_values(column_mapping: dict[str, str], workflow_run, workflow) -> dict[str, str]` — `{column_letter_or_header: resolved_value}`.
  - `def resolve_disposition(workflow_run, workflow) -> str` — matches `gathered_context.get("call_disposition")` against `WorkflowModel.call_disposition_codes` (`api/db/models.py`, JSON) the same way the existing dashboard does; falls back to the raw disposition string if no code match, `""` if absent.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_sheets_writeback.py
from types import SimpleNamespace

from api.services.campaign.writeback.sheets_writeback import (
    build_row_values,
    resolve_disposition,
    resolve_field,
)


def _run(**overrides):
    base = dict(
        id=42,
        state="completed",
        usage_info={"call_duration_seconds": 87.4},
        cost_info={"total_cost_usd": 0.42},
        recording_url="https://cdn/run42.wav",
        transcript_url="https://cdn/run42.txt",
        gathered_context={"call_disposition": "no_answer", "customer_intent": "buy"},
        created_at=SimpleNamespace(isoformat=lambda: "2026-07-21T10:00:00+00:00"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _workflow(**overrides):
    base = dict(call_disposition_codes={"no_answer": "No Answer (auto)"})
    base.update(overrides)
    return SimpleNamespace(**base)


def test_resolve_field_call_state():
    assert resolve_field("call_state", _run(), _workflow()) == "completed"


def test_resolve_field_duration_nested_path():
    assert (
        resolve_field("usage_info.call_duration_seconds", _run(), _workflow())
        == "87.4"
    )


def test_resolve_field_gathered_context_nested_path():
    assert (
        resolve_field("gathered_context.customer_intent", _run(), _workflow())
        == "buy"
    )


def test_resolve_field_missing_path_returns_blank():
    assert resolve_field("gathered_context.nonexistent", _run(), _workflow()) == ""
    assert resolve_field("usage_info.missing", _run(), _workflow()) == ""


def test_resolve_field_recording_and_transcript_url():
    run = _run()
    assert resolve_field("recording_url", run, _workflow()) == run.recording_url
    assert resolve_field("transcript_url", run, _workflow()) == run.transcript_url


def test_resolve_field_raw_json_dump():
    value = resolve_field("raw_json", _run(), _workflow())
    assert '"customer_intent": "buy"' in value


def test_resolve_disposition_matches_code():
    assert resolve_disposition(_run(), _workflow()) == "No Answer (auto)"


def test_resolve_disposition_falls_back_to_raw_string():
    run = _run(gathered_context={"call_disposition": "unmapped_code"})
    assert resolve_disposition(run, _workflow()) == "unmapped_code"


def test_resolve_disposition_blank_when_absent():
    run = _run(gathered_context={})
    assert resolve_disposition(run, _workflow()) == ""


def test_build_row_values_applies_full_mapping():
    mapping = {
        "F": "call_disposition",
        "G": "usage_info.call_duration_seconds",
        "H": "recording_url",
        "I": "gathered_context.customer_intent",
    }
    row = build_row_values(mapping, _run(), _workflow())
    assert row == {
        "F": "No Answer (auto)",
        "G": "87.4",
        "H": "https://cdn/run42.wav",
        "I": "buy",
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.campaign.writeback.sheets_writeback`.

- [ ] **Step 3: Implement**

```python
# api/services/campaign/writeback/__init__.py
```
(empty file)

```python
# api/services/campaign/writeback/sheets_writeback.py
"""Column-mapping resolution for Google Sheets write-back.

Reads from WorkflowRunModel (api/db/models.py: gathered_context, usage_info,
cost_info, recording_url, transcript_url, call_type, state) and
WorkflowModel.call_disposition_codes — no new per-call fields required, per
the phase-5 spec. Pure functions: no DB/network access, fully unit-testable.
"""

import json
from typing import Any


def _get_nested(obj: Any, path: str) -> Any:
    """obj.<a>.<b> where obj may be an attribute-bearing object (model/
    SimpleNamespace) whose leaf may itself be a dict (JSON column)."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def resolve_disposition(workflow_run, workflow) -> str:
    gathered_context = workflow_run.gathered_context or {}
    raw = gathered_context.get("call_disposition")
    if not raw:
        return ""
    codes = workflow.call_disposition_codes or {}
    return codes.get(raw, raw)


def resolve_field(field_path: str, workflow_run, workflow) -> str:
    if field_path == "call_state":
        return str(workflow_run.state or "")
    if field_path == "call_disposition":
        return resolve_disposition(workflow_run, workflow)
    if field_path == "recording_url":
        return workflow_run.recording_url or ""
    if field_path == "transcript_url":
        return workflow_run.transcript_url or ""
    if field_path == "call_timestamp":
        created_at = getattr(workflow_run, "created_at", None)
        return created_at.isoformat() if created_at else ""
    if field_path == "raw_json":
        return json.dumps(workflow_run.gathered_context or {})

    value = _get_nested(workflow_run, field_path)
    if value is None:
        return ""
    return str(value)


def build_row_values(
    column_mapping: dict[str, str], workflow_run, workflow
) -> dict[str, str]:
    return {
        column: resolve_field(field_path, workflow_run, workflow)
        for column, field_path in column_mapping.items()
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add api/services/campaign/writeback/ api/tests/test_sheets_writeback.py
git commit -m "feat(sheets): write-back column mapping and field resolution"
```

---

## Task 10: `GoogleSheetsClient` DB mixin — writeback ledger

**Files:**
- Create: `api/db/google_sheets_client.py`
- Modify: `api/db/db_client.py`
- Test: extend `api/tests/test_sheets_writeback_service.py` (created in this task)

**Interfaces:**
- Produces methods on `db_client`:
  - `async def get_sheets_writeback_entry(workflow_run_id: int) -> CampaignSheetsWritebackModel | None`
  - `async def create_sheets_writeback_pending(workflow_run_id: int, campaign_id: int) -> CampaignSheetsWritebackModel` — idempotent insert-or-return (unique constraint on `workflow_run_id`; on conflict, returns the existing row without re-inserting).
  - `async def mark_sheets_writeback_written(workflow_run_id: int) -> None`
  - `async def mark_sheets_writeback_failed(workflow_run_id: int, error: str) -> None` — increments `attempts`.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_sheets_writeback_service.py
import pytest

from api.db import db_client
from api.db.models import (
    CampaignModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)


async def _seed_run() -> tuple[int, int]:
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_wb_ledger")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id="user_wb_ledger", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(organization_id=org.id, user_id=user.id, name="wf")
        s.add(wf)
        await s.flush()
        campaign = CampaignModel(
            name="c",
            organization_id=org.id,
            workflow_id=wf.id,
            created_by=user.id,
            source_type="csv",
            source_id="x",
        )
        s.add(campaign)
        await s.flush()
        run = WorkflowRunModel(
            name="r", workflow_id=wf.id, mode="pipeline", campaign_id=campaign.id
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run.id, campaign.id


@pytest.mark.asyncio
async def test_create_pending_is_idempotent():
    run_id, campaign_id = await _seed_run()
    first = await db_client.create_sheets_writeback_pending(run_id, campaign_id)
    second = await db_client.create_sheets_writeback_pending(run_id, campaign_id)
    assert first.id == second.id
    assert first.state == "pending"


@pytest.mark.asyncio
async def test_mark_written_transitions_state():
    run_id, campaign_id = await _seed_run()
    await db_client.create_sheets_writeback_pending(run_id, campaign_id)
    await db_client.mark_sheets_writeback_written(run_id)
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "written"
    assert entry.written_at is not None


@pytest.mark.asyncio
async def test_mark_failed_increments_attempts_and_records_error():
    run_id, campaign_id = await _seed_run()
    await db_client.create_sheets_writeback_pending(run_id, campaign_id)
    await db_client.mark_sheets_writeback_failed(run_id, "429 rate limited")
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "failed"
    assert entry.attempts == 1
    assert entry.error == "429 rate limited"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback_service.py -v`
Expected: FAIL — `AttributeError: 'DBClient' object has no attribute 'create_sheets_writeback_pending'`.

- [ ] **Step 3: Implement `api/db/google_sheets_client.py`**

```python
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import CampaignSheetsWritebackModel


class GoogleSheetsClient(BaseDBClient):
    """DB mixin: campaign_sheets_writeback ledger (idempotency for write-back).

    Name mirrors api/services/integrations/google/sheets_client.py's
    GoogleSheetsClient (the Sheets API wrapper) but this one is a DBClient
    mixin registered on api.db.db_client.DBClient — distinct class, same
    naming convention as other *Client mixins in api/db/.
    """

    async def get_sheets_writeback_entry(
        self, workflow_run_id: int
    ) -> Optional[CampaignSheetsWritebackModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(CampaignSheetsWritebackModel).where(
                    CampaignSheetsWritebackModel.workflow_run_id == workflow_run_id
                )
            )
            return result.scalars().first()

    async def create_sheets_writeback_pending(
        self, workflow_run_id: int, campaign_id: int
    ) -> CampaignSheetsWritebackModel:
        existing = await self.get_sheets_writeback_entry(workflow_run_id)
        if existing is not None:
            return existing
        async with self.async_session() as session:
            entry = CampaignSheetsWritebackModel(
                workflow_run_id=workflow_run_id,
                campaign_id=campaign_id,
                state="pending",
            )
            session.add(entry)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                # Lost the race to another concurrent completion-task retry;
                # the row now exists, return it.
                return await self.get_sheets_writeback_entry(workflow_run_id)
            await session.refresh(entry)
            return entry

    async def mark_sheets_writeback_written(self, workflow_run_id: int) -> None:
        async with self.async_session() as session:
            result = await session.execute(
                select(CampaignSheetsWritebackModel).where(
                    CampaignSheetsWritebackModel.workflow_run_id == workflow_run_id
                )
            )
            entry = result.scalars().first()
            if entry is None:
                return
            entry.state = "written"
            entry.written_at = datetime.now(UTC)
            await session.commit()

    async def mark_sheets_writeback_failed(
        self, workflow_run_id: int, error: str
    ) -> None:
        async with self.async_session() as session:
            result = await session.execute(
                select(CampaignSheetsWritebackModel).where(
                    CampaignSheetsWritebackModel.workflow_run_id == workflow_run_id
                )
            )
            entry = result.scalars().first()
            if entry is None:
                return
            entry.state = "failed"
            entry.error = error
            entry.attempts = (entry.attempts or 0) + 1
            await session.commit()
```

- [ ] **Step 4: Register the mixin**

In `api/db/db_client.py`, add `from api.db.google_sheets_client import GoogleSheetsClient as GoogleSheetsDBClient` (aliased to avoid a name clash with the Sheets API wrapper class from Task 6) and add `GoogleSheetsDBClient,` to the `DBClient(...)` base list.

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback_service.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add api/db/google_sheets_client.py api/db/db_client.py api/tests/test_sheets_writeback_service.py
git commit -m "feat(sheets): write-back idempotency ledger DB mixin"
```

---

## Task 11: `writeback_service.write_back_run` — batching, backoff, idempotency

**Files:**
- Create: `api/services/campaign/writeback/writeback_service.py`
- Test: extend `api/tests/test_sheets_writeback_service.py`

**Interfaces:**
- Consumes: `build_row_values` (Task 9), ledger methods (Task 10), `GoogleOAuthClient`/`GoogleSheetsClient` (Tasks 5-6), `db_client.get_workflow_run_by_id`/`get_workflow_by_id`/`get_campaign_by_id` (existing), `decode_sheet_source_id` (Task 4, used to resolve the *lead* row for update-mode; write targets come from `writeback_config`, not the source sheet).
- Produces:
  - `async def write_back_run(workflow_run_id: int, *, oauth_client=None, sheets_client_factory=None, max_attempts: int = 3) -> None`:
    1. Loads `WorkflowRunModel` + its `campaign_id`; no-ops (logs + returns) if `campaign_id` is `None` or `campaign.writeback_config` is `None`.
    2. `db_client.create_sheets_writeback_pending(...)`; if the entry is already `state == "written"`, returns immediately (idempotent no-op).
    3. Builds `row_values` via `build_row_values`.
    4. **Update mode:** resolves the target row from the run's `QueuedRunModel.source_uuid` (`gsheet_{id}_{sheet}_{row}` — parses the trailing `_row_idx`); issues one `values.batchUpdate` with a single range write for that row's mapped columns.
       **Append mode:** issues one `values.append` (via `batchUpdate`-equivalent — see Step 3 note) to the configured results sheet.
    5. On success: `mark_sheets_writeback_written`. On `GoogleSheetsApiError` with `status_code in (429, 500, 502, 503)`: exponential backoff with jitter, retry up to `max_attempts`, then `mark_sheets_writeback_failed`. On any other error (4xx auth/permission, malformed config): `mark_sheets_writeback_failed` immediately, no retry, and re-raise is **not** done (the caller — the arq task — must never let a Sheets error propagate into/break the completion pipeline, per spec's Error handling section).

- [ ] **Step 1: Write the failing tests (mocked Sheets client, no real backoff sleep)**

```python
# append to api/tests/test_sheets_writeback_service.py
from unittest.mock import AsyncMock, patch

from api.db.models import QueuedRunModel
from api.services.campaign.writeback.writeback_service import write_back_run
from api.services.integrations.google.sheets_client import GoogleSheetsApiError


async def _seed_run_with_campaign_config(
    *, mode: str, source_uuid: str | None = None
):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id=f"org_wb_{mode}_{source_uuid}")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id=f"user_wb_{mode}_{source_uuid}", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(
            organization_id=org.id,
            user_id=user.id,
            name="wf",
            call_disposition_codes={"no_answer": "No Answer"},
        )
        s.add(wf)
        await s.flush()
        campaign = CampaignModel(
            name="c",
            organization_id=org.id,
            workflow_id=wf.id,
            created_by=user.id,
            source_type="google_sheets" if source_uuid else "csv",
            source_id="gsheet:1AbC:Leads:" if source_uuid else "x",
            writeback_config={
                "provider": "google_sheets",
                "spreadsheet_id": "1AbC",
                "sheet_name": "Results",
                "mode": mode,
                "column_mapping": {"F": "call_disposition"},
            },
        )
        s.add(campaign)
        await s.flush()

        queued_run_id = None
        if source_uuid:
            qr = QueuedRunModel(
                campaign_id=campaign.id,
                source_uuid=source_uuid,
                context_variables={},
                state="processed",
            )
            s.add(qr)
            await s.flush()
            queued_run_id = qr.id

        run = WorkflowRunModel(
            name="r",
            workflow_id=wf.id,
            mode="pipeline",
            campaign_id=campaign.id,
            queued_run_id=queued_run_id,
            gathered_context={"call_disposition": "no_answer"},
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run.id, org.id


@pytest.mark.asyncio
async def test_write_back_run_noop_without_campaign():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_wb_noop")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id="user_wb_noop", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(organization_id=org.id, user_id=user.id, name="wf")
        s.add(wf)
        await s.flush()
        run = WorkflowRunModel(name="r", workflow_id=wf.id, mode="pipeline")
        s.add(run)
        await s.commit()
        await s.refresh(run)
        run_id = run.id

    sheets = AsyncMock()
    with patch(
        "api.services.campaign.writeback.writeback_service.GoogleSheetsClient",
        return_value=sheets,
    ):
        await write_back_run(run_id)
    sheets.values_batch_update.assert_not_called()

    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry is None  # no campaign -> not even a pending ledger row


@pytest.mark.asyncio
async def test_write_back_run_update_mode_targets_lead_row():
    run_id, org_id = await _seed_run_with_campaign_config(
        mode="update", source_uuid="gsheet_1AbC_Leads_7"
    )
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_batch_update = AsyncMock(return_value={})

    await write_back_run(
        run_id, oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )

    sheets.values_batch_update.assert_awaited_once()
    _, kwargs = sheets.values_batch_update.await_args
    data = kwargs.get("data") or sheets.values_batch_update.await_args.args[1]
    assert data[0]["range"] == "Results!F7"
    assert data[0]["values"] == [["No Answer"]]

    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "written"


@pytest.mark.asyncio
async def test_write_back_run_append_mode_calls_append():
    run_id, org_id = await _seed_run_with_campaign_config(mode="append")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_append = AsyncMock(return_value={})

    await write_back_run(
        run_id, oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )

    sheets.values_append.assert_awaited_once()
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "written"


@pytest.mark.asyncio
async def test_write_back_run_idempotent_second_call_skips_api():
    run_id, org_id = await _seed_run_with_campaign_config(mode="append")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_append = AsyncMock(return_value={})

    await write_back_run(
        run_id, oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    await write_back_run(
        run_id, oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    assert sheets.values_append.await_count == 1


@pytest.mark.asyncio
async def test_write_back_run_retries_on_429_then_succeeds(monkeypatch):
    run_id, org_id = await _seed_run_with_campaign_config(mode="append")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_append = AsyncMock(
        side_effect=[GoogleSheetsApiError(429, "rate limited"), {}]
    )
    monkeypatch.setattr(
        "api.services.campaign.writeback.writeback_service.asyncio.sleep",
        AsyncMock(),
    )

    await write_back_run(
        run_id, oauth_client=oauth, sheets_client_factory=lambda token: sheets
    )
    assert sheets.values_append.await_count == 2
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "written"


@pytest.mark.asyncio
async def test_write_back_run_exhausts_retries_marks_failed(monkeypatch):
    run_id, org_id = await _seed_run_with_campaign_config(mode="append")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_append = AsyncMock(
        side_effect=GoogleSheetsApiError(503, "unavailable")
    )
    monkeypatch.setattr(
        "api.services.campaign.writeback.writeback_service.asyncio.sleep",
        AsyncMock(),
    )

    await write_back_run(
        run_id,
        oauth_client=oauth,
        sheets_client_factory=lambda token: sheets,
        max_attempts=2,
    )
    assert sheets.values_append.await_count == 2
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "failed"
    assert "unavailable" in entry.error


@pytest.mark.asyncio
async def test_write_back_run_never_raises_out_of_completion_path(monkeypatch):
    run_id, org_id = await _seed_run_with_campaign_config(mode="append")
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(
        side_effect=Exception("unexpected boom")
    )
    # Should not raise -- write_back_run swallows and records failure.
    await write_back_run(run_id, oauth_client=oauth)
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "failed"
```

- [ ] **Step 2: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback_service.py -v`
Expected: FAIL — `ModuleNotFoundError: api.services.campaign.writeback.writeback_service`.

- [ ] **Step 3: Implement**

```python
# api/services/campaign/writeback/writeback_service.py
"""Writes a completed call's result back to the campaign's configured Google
Sheet — update-in-place (Sheets-sourced campaigns, default) or append (any
campaign, since write-back and read-in are independently configurable).

Idempotent per workflow_run_id via campaign_sheets_writeback
(api/db/google_sheets_client.py). Never raises: this is invoked from a
best-effort arq task (api/tasks/sheets_writeback_tasks.py) triggered by the
post-call completion pipeline, and a Sheets outage must never affect call
processing (see phase-5 spec, "Error handling & edge cases").
"""

import asyncio
import random
from typing import Optional

from loguru import logger

from api.db import db_client
from api.services.campaign.writeback.sheets_writeback import build_row_values
from api.services.integrations.google.oauth_client import GoogleOAuthClient
from api.services.integrations.google.sheets_client import (
    GoogleSheetsApiError,
    GoogleSheetsClient,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


def _parse_lead_row(source_uuid: Optional[str]) -> Optional[int]:
    """Extracts the trailing row index from gsheet_{id}_{sheet}_{row_idx}."""
    if not source_uuid or not source_uuid.startswith("gsheet_"):
        return None
    try:
        return int(source_uuid.rsplit("_", 1)[-1])
    except ValueError:
        return None


async def _column_to_range(column: str, row: int, sheet_name: str) -> str:
    return f"{sheet_name}!{column}{row}"


async def write_back_run(
    workflow_run_id: int,
    *,
    oauth_client: Optional[GoogleOAuthClient] = None,
    sheets_client_factory=None,
    max_attempts: int = 3,
) -> None:
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if workflow_run is None or workflow_run.campaign_id is None:
        return

    campaign = await db_client.get_campaign_by_id(workflow_run.campaign_id)
    if campaign is None or not campaign.writeback_config:
        return

    entry = await db_client.create_sheets_writeback_pending(
        workflow_run_id, campaign.id
    )
    if entry.state == "written":
        return  # already written -- retried completion event, no-op

    oauth = oauth_client or GoogleOAuthClient()
    sheets_factory = sheets_client_factory or (lambda token: GoogleSheetsClient(token))

    config = campaign.writeback_config
    mode = config.get("mode", "append")
    sheet_name = config["sheet_name"]
    spreadsheet_id = config["spreadsheet_id"]
    column_mapping = config.get("column_mapping", {})

    workflow = await db_client.get_workflow_by_id(campaign.workflow_id)
    row_values = build_row_values(column_mapping, workflow_run, workflow)

    attempt = 0
    while True:
        attempt += 1
        try:
            access_token = await oauth.get_valid_access_token(campaign.organization_id)
            sheets_client = sheets_factory(access_token)

            if mode == "update":
                queued_run = await db_client.get_queued_run_by_id(
                    workflow_run.queued_run_id
                )
                lead_row = _parse_lead_row(
                    queued_run.source_uuid if queued_run else None
                )
                if lead_row is None:
                    raise ValueError(
                        f"Cannot resolve sheet row for run {workflow_run_id} "
                        "(update mode requires a gsheet-sourced queued_run)"
                    )
                data = [
                    {
                        "range": await _column_to_range(col, lead_row, sheet_name),
                        "values": [[value]],
                    }
                    for col, value in row_values.items()
                ]
                await sheets_client.values_batch_update(spreadsheet_id, data)
            else:
                ordered_columns = sorted(row_values.keys())
                values = [[row_values[col] for col in ordered_columns]]
                await sheets_client.values_append(spreadsheet_id, sheet_name, values)

            await db_client.mark_sheets_writeback_written(workflow_run_id)
            return

        except GoogleSheetsApiError as e:
            if e.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                backoff = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "Sheets write-back retry {}/{} for run {} after {}: sleeping {:.1f}s",
                    attempt,
                    max_attempts,
                    workflow_run_id,
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue
            await db_client.mark_sheets_writeback_failed(workflow_run_id, str(e))
            return

        except Exception as e:
            logger.error(
                "Sheets write-back failed for run {}: {}", workflow_run_id, e
            )
            await db_client.mark_sheets_writeback_failed(workflow_run_id, str(e))
            return
```

- [ ] **Step 4: Add the two small `db_client` lookups this leans on, if missing**

Run: `grep -n "def get_workflow_run_by_id\|def get_workflow_by_id\|def get_queued_run_by_id" api/db/*.py`
If `get_queued_run_by_id` doesn't exist, add to `api/db/campaign_client.py`:

```python
    async def get_queued_run_by_id(self, queued_run_id: int | None):
        if queued_run_id is None:
            return None
        from sqlalchemy.future import select

        from api.db.models import QueuedRunModel

        async with self.async_session() as session:
            result = await session.execute(
                select(QueuedRunModel).where(QueuedRunModel.id == queued_run_id)
            )
            return result.scalars().first()
```

- [ ] **Step 5: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_sheets_writeback_service.py -v`
Expected: all pass (10 tests total across Task 10 + 11).

- [ ] **Step 6: Commit**

```bash
git add api/services/campaign/writeback/writeback_service.py api/db/campaign_client.py api/tests/test_sheets_writeback_service.py
git commit -m "feat(sheets): write-back service with update/append modes, backoff, and idempotency"
```

---

## Task 12: arq task + completion-pipeline wiring

**Files:**
- Create: `api/tasks/sheets_writeback_tasks.py`
- Modify: `api/tasks/workflow_completion.py`
- Test: extend `api/tests/test_sheets_writeback_service.py`, add `api/tests/test_workflow_completion_sheets_hook.py`

**Interfaces:**
- Produces: `async def sheets_writeback(ctx, workflow_run_id: int) -> None` (arq task, thin wrapper calling `write_back_run`).
- Modifies `process_workflow_completion` (`api/tasks/workflow_completion.py`) to add a new step after `run_integrations_post_workflow_run` (Step 3, line ~169-172) and before "Step 4: Notify MPS": enqueues `sheets_writeback` only when `GOOGLE_SHEETS_INTEGRATION` is on **and** the run's campaign has `writeback_config` set. Never raises into the completion task on enqueue failure (wrapped in the same `try/except Exception: logger.error(...)` pattern already used for Steps 3 and 4 in that function).

- [ ] **Step 1: Implement the arq task**

```python
# api/tasks/sheets_writeback_tasks.py
"""arq task: batches/writes a single completed call's result to its
campaign's configured Google Sheet. Enqueued from
api/tasks/workflow_completion.py after recording/transcript/QA/webhook steps
complete, so gathered_context reflects any QA-node enrichment.
"""

from typing import Dict

from loguru import logger

from api.services.campaign.writeback.writeback_service import write_back_run


async def sheets_writeback(ctx: Dict, workflow_run_id: int) -> None:
    try:
        await write_back_run(workflow_run_id)
    except Exception as e:
        # write_back_run already swallows expected Sheets-API failures into
        # the ledger; this is a last-resort guard so an unexpected bug here
        # never surfaces as an arq task failure/retry storm.
        logger.error(
            f"Unexpected error in sheets_writeback for run {workflow_run_id}: {e}"
        )
```

- [ ] **Step 2: Write the failing test for the completion-pipeline hook**

```python
# api/tests/test_workflow_completion_sheets_hook.py
from unittest.mock import AsyncMock, patch

import pytest

from api.db import db_client
from api.db.models import CampaignModel, OrganizationModel, UserModel, WorkflowModel, WorkflowRunModel


async def _seed_run(*, writeback_config):
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_completion_hook")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id="user_completion_hook", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(organization_id=org.id, user_id=user.id, name="wf")
        s.add(wf)
        await s.flush()
        campaign = CampaignModel(
            name="c",
            organization_id=org.id,
            workflow_id=wf.id,
            created_by=user.id,
            source_type="csv",
            source_id="x",
            writeback_config=writeback_config,
        )
        s.add(campaign)
        await s.flush()
        run = WorkflowRunModel(
            name="r", workflow_id=wf.id, mode="pipeline", campaign_id=campaign.id
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run.id


@pytest.mark.asyncio
async def test_completion_enqueues_writeback_when_configured(monkeypatch):
    from api.tasks import workflow_completion as mod

    monkeypatch.setattr(mod, "GOOGLE_SHEETS_INTEGRATION", True)
    run_id = await _seed_run(
        writeback_config={
            "provider": "google_sheets",
            "spreadsheet_id": "1AbC",
            "sheet_name": "Results",
            "mode": "append",
            "column_mapping": {},
        }
    )
    enqueue = AsyncMock()
    with patch.object(mod, "_enqueue_sheets_writeback", enqueue):
        await mod.process_workflow_completion(None, run_id)
    enqueue.assert_awaited_once_with(run_id)


@pytest.mark.asyncio
async def test_completion_skips_writeback_when_not_configured(monkeypatch):
    from api.tasks import workflow_completion as mod

    monkeypatch.setattr(mod, "GOOGLE_SHEETS_INTEGRATION", True)
    run_id = await _seed_run(writeback_config=None)
    enqueue = AsyncMock()
    with patch.object(mod, "_enqueue_sheets_writeback", enqueue):
        await mod.process_workflow_completion(None, run_id)
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_completion_skips_writeback_when_flag_off(monkeypatch):
    from api.tasks import workflow_completion as mod

    monkeypatch.setattr(mod, "GOOGLE_SHEETS_INTEGRATION", False)
    run_id = await _seed_run(
        writeback_config={
            "provider": "google_sheets",
            "spreadsheet_id": "1AbC",
            "sheet_name": "Results",
            "mode": "append",
            "column_mapping": {},
        }
    )
    enqueue = AsyncMock()
    with patch.object(mod, "_enqueue_sheets_writeback", enqueue):
        await mod.process_workflow_completion(None, run_id)
    enqueue.assert_not_called()
```

Note: `process_workflow_completion` has other side effects (recording/transcript upload, `run_integrations_post_workflow_run`, `report_completed_workflow_run_platform_usage`) that will run in this test unmocked — check `api/tests/` for an existing `process_workflow_completion` test (`grep -rln "process_workflow_completion" api/tests/`) and mirror its mocking setup for those unrelated steps so this test isolates only the write-back hook.

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_workflow_completion_sheets_hook.py -v`
Expected: FAIL — `AttributeError: module 'api.tasks.workflow_completion' has no attribute '_enqueue_sheets_writeback'`.

- [ ] **Step 4: Wire the hook into `process_workflow_completion`**

In `api/tasks/workflow_completion.py`, add imports:

```python
from api.constants import GOOGLE_SHEETS_INTEGRATION
from api.db.models import CampaignModel
```

Add a helper (near the top, alongside `_should_skip_qa`):

```python
async def _enqueue_sheets_writeback(workflow_run_id: int) -> None:
    from api.workers.redis_pool import get_arq_pool  # match existing enqueue pattern

    pool = await get_arq_pool()
    await pool.enqueue_job("sheets_writeback", workflow_run_id)


async def _has_sheets_writeback_configured(workflow_run) -> bool:
    if workflow_run.campaign_id is None:
        return False
    campaign = await db_client.get_campaign_by_id(workflow_run.campaign_id)
    return bool(campaign and campaign.writeback_config)
```

Note: confirm the exact arq-enqueue helper name/import path used elsewhere in this file or `api/tasks/campaign_tasks.py` (`grep -n "enqueue_job\|get_arq_pool\|ctx\[.redis.\]" api/tasks/*.py`) and match it exactly — the snippet above is illustrative; use the project's actual enqueue mechanism.

In `process_workflow_completion`, insert between the existing Step 3 (`run_integrations_post_workflow_run`) and Step 4 (`report_completed_workflow_run_platform_usage`):

```python
    # Step 3.5: Write completed call results back to a configured Google Sheet
    # (Phase 5). Runs after QA/webhooks so gathered_context reflects any
    # QA-node enrichment. Never blocks completion.
    if GOOGLE_SHEETS_INTEGRATION:
        try:
            workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
            if workflow_run and await _has_sheets_writeback_configured(workflow_run):
                await _enqueue_sheets_writeback(workflow_run_id)
        except Exception as e:
            logger.error(
                f"Error enqueuing sheets write-back for workflow {workflow_run_id}: {e}"
            )
```

- [ ] **Step 5: Register the arq task with the worker**

Run: `grep -rn "sync_campaign_source\|process_campaign_batch" api/*.py api/workers/*.py 2>/dev/null | grep -i "functions\s*=\|WorkerSettings"`
Expected: shows where arq task functions are registered on `WorkerSettings.functions`. Add `sheets_writeback` (from `api.tasks.sheets_writeback_tasks`) to that same list.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_workflow_completion_sheets_hook.py -v`
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add api/tasks/sheets_writeback_tasks.py api/tasks/workflow_completion.py api/tests/test_workflow_completion_sheets_hook.py
git commit -m "feat(sheets): enqueue write-back from post-call completion pipeline"
```

---

## Task 13: OAuth connect/callback routes (org-level "Connect Google Sheets")

**Files:**
- Create: `api/routes/google_integration.py`
- Modify: router mount site (find via grep, mirror Phase 1's Task 10 pattern)
- Test: `api/tests/test_google_integration_routes.py`

**Interfaces:**
- Produces routes under `/integrations/google`:
  - `GET /integrations/google/connect` — builds the Google OAuth consent URL (`google_auth_oauthlib.flow.Flow`, scopes = `GOOGLE_SHEETS_SCOPES`), returns `{authorization_url: str}`. Requires `get_user` auth dependency (existing pattern, `api/services/auth/depends.py`).
  - `GET /integrations/google/callback` — exchanges `code` for tokens, creates/updates the org's `IntegrationModel` (`provider="google_sheets"`) + `ExternalCredentialModel` (encrypted refresh/access token), redirects to a UI success URL.
  - `GET /integrations/google/status` — `{connected: bool, google_account_email: str | None}` for the caller's selected org.
  - `POST /integrations/google/disconnect` — sets `IntegrationModel.is_active = False`.
  - All routes gated by `GOOGLE_SHEETS_INTEGRATION`; when off, return 404.

- [ ] **Step 1: Find the router-mount and auth-dependency conventions**

Run: `grep -rn "include_router" api/app.py | head -5` and `grep -n "def get_user\b" api/services/auth/depends.py`
Expected: shows the `app.include_router(..., prefix="/api/v1")` pattern and the `get_user` dependency signature to reuse.

- [ ] **Step 2: Write the failing test**

```python
# api/tests/test_google_integration_routes.py
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import app
from api.services.auth.depends import get_user


@pytest.fixture
def user_override():
    app.dependency_overrides[get_user] = lambda: type(
        "U", (), {"id": 1, "selected_organization_id": 1}
    )()
    yield
    app.dependency_overrides.pop(get_user, None)


@pytest.mark.asyncio
async def test_status_disconnected_by_default(user_override, monkeypatch):
    from api.routes import google_integration as mod

    monkeypatch.setattr(mod, "GOOGLE_SHEETS_INTEGRATION", True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/integrations/google/status")
    assert r.status_code == 200
    assert r.json() == {"connected": False, "google_account_email": None}


@pytest.mark.asyncio
async def test_routes_404_when_flag_off(user_override, monkeypatch):
    from api.routes import google_integration as mod

    monkeypatch.setattr(mod, "GOOGLE_SHEETS_INTEGRATION", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/integrations/google/status")
    assert r.status_code == 404
```

(Confirm the exact mount prefix, adjust URLs to match.)

- [ ] **Step 3: Run to verify failure**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_integration_routes.py -v`
Expected: FAIL — 404 (route not mounted / module doesn't exist).

- [ ] **Step 4: Implement `api/routes/google_integration.py`**

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.constants import GOOGLE_SHEETS_INTEGRATION
from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user

router = APIRouter(prefix="/integrations/google", tags=["google-sheets"])


def _require_enabled():
    if not GOOGLE_SHEETS_INTEGRATION:
        raise HTTPException(status_code=404, detail="Not found")


class ConnectResponse(BaseModel):
    authorization_url: str


class StatusResponse(BaseModel):
    connected: bool
    google_account_email: str | None = None


@router.get("/connect", response_model=ConnectResponse)
async def connect(user: UserModel = Depends(get_user)):
    _require_enabled()
    from google_auth_oauthlib.flow import Flow

    from api.constants import (
        GOOGLE_OAUTH_CLIENT_ID,
        GOOGLE_OAUTH_CLIENT_SECRET,
        GOOGLE_OAUTH_REDIRECT_URI,
        GOOGLE_SHEETS_SCOPES,
    )

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_SHEETS_SCOPES,
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
    )
    authorization_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=str(user.selected_organization_id),
    )
    return ConnectResponse(authorization_url=authorization_url)


@router.get("/callback")
async def callback(code: str, state: str):
    _require_enabled()
    from datetime import UTC, datetime, timedelta

    from google_auth_oauthlib.flow import Flow

    from api.constants import (
        GOOGLE_OAUTH_CLIENT_ID,
        GOOGLE_OAUTH_CLIENT_SECRET,
        GOOGLE_OAUTH_REDIRECT_URI,
        GOOGLE_SHEETS_SCOPES,
        UI_APP_URL,
    )
    from api.db.models import ExternalCredentialModel
    from api.enums import WebhookCredentialType
    from api.utils.credential_crypto import encrypt_credential

    organization_id = int(state)

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_SHEETS_SCOPES,
        redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials

    integration = await db_client.create_integration(
        integration_id=f"google_sheets_{organization_id}",
        provider="google_sheets",
        organization_id=organization_id,
        connection_details={"scopes": GOOGLE_SHEETS_SCOPES},
    )
    async with db_client.async_session() as session:
        cred = ExternalCredentialModel(
            organization_id=organization_id,
            name="Google Sheets",
            credential_type=WebhookCredentialType.NONE.value,
            credential_data={
                "refresh_token": encrypt_credential(creds.refresh_token),
                "access_token": encrypt_credential(creds.token),
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=3600)
                ).isoformat(),
                "integration_id": integration.id,
            },
            created_by=None,
        )
        session.add(cred)
        await session.commit()

    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=f"{UI_APP_URL}/settings/integrations?connected=google_sheets")


@router.get("/status", response_model=StatusResponse)
async def status(user: UserModel = Depends(get_user)):
    _require_enabled()
    integration = await db_client.get_integration_by_org_and_provider(
        user.selected_organization_id, "google_sheets"
    )
    if integration is None:
        return StatusResponse(connected=False)
    return StatusResponse(
        connected=True,
        google_account_email=integration.connection_details.get(
            "google_account_email"
        ),
    )


@router.post("/disconnect")
async def disconnect(user: UserModel = Depends(get_user)):
    _require_enabled()
    integration = await db_client.get_integration_by_org_and_provider(
        user.selected_organization_id, "google_sheets"
    )
    if integration is None:
        raise HTTPException(status_code=404, detail="Not connected")
    await db_client.update_integration_status(integration.id, is_active=False)
    return {"disconnected": True}
```

- [ ] **Step 5: Mount the router**

In the site found in Step 1, add `from api.routes import google_integration` and `app.include_router(google_integration.router, prefix="/api/v1")`.

- [ ] **Step 6: Run to verify pass**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_google_integration_routes.py -v`
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add api/routes/google_integration.py api/app.py api/tests/test_google_integration_routes.py
git commit -m "feat(sheets): org-level Google OAuth connect/callback/status/disconnect routes"
```

---

## Task 14: Full-suite regression + end-to-end read+write lifecycle test

**Files:**
- Test: `api/tests/test_google_sheets_lifecycle.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the lifecycle test (mocked Sheets API throughout)**

```python
# api/tests/test_google_sheets_lifecycle.py
from unittest.mock import AsyncMock

import pytest

from api.db import db_client
from api.db.models import OrganizationModel, UserModel, WorkflowModel, CampaignModel
from api.services.campaign.source_sync_factory import get_sync_service
from api.services.campaign.writeback.writeback_service import write_back_run


@pytest.mark.asyncio
async def test_read_sync_then_call_then_writeback_update_mode():
    from api.db.database import async_session

    async with async_session() as s:
        org = OrganizationModel(provider_id="org_gsheet_lifecycle")
        s.add(org)
        await s.flush()
        user = UserModel(provider_id="user_gsheet_lifecycle", email="u@x.com")
        s.add(user)
        await s.flush()
        wf = WorkflowModel(
            organization_id=org.id,
            user_id=user.id,
            name="wf",
            call_disposition_codes={"answered": "Answered"},
        )
        s.add(wf)
        await s.flush()
        campaign = CampaignModel(
            name="Lifecycle campaign",
            organization_id=org.id,
            workflow_id=wf.id,
            created_by=user.id,
            source_type="google_sheets",
            source_id="gsheet:1AbC:Leads:",
            writeback_config={
                "provider": "google_sheets",
                "spreadsheet_id": "1AbC",
                "sheet_name": "Results",
                "mode": "update",
                "column_mapping": {"F": "call_disposition"},
            },
        )
        s.add(campaign)
        await s.commit()
        await s.refresh(campaign)
        campaign_id, org_id = campaign.id, org.id

    # --- READ: sync leads from the sheet ---
    oauth = AsyncMock()
    oauth.get_valid_access_token = AsyncMock(return_value="tok")
    sheets = AsyncMock()
    sheets.values_get = AsyncMock(
        return_value={"values": [["name", "phone_number"], ["Ann", "+15551234567"]]}
    )
    sync_service = get_sync_service("google_sheets")
    sync_service._oauth_client = oauth
    sync_service._sheets_client_factory = lambda token: sheets

    synced = await sync_service.sync_source_data(campaign_id)
    assert synced == 1

    queued = await db_client.get_queued_runs_for_campaign(campaign_id)
    assert queued[0].source_uuid == "gsheet_1AbC_Leads_1"

    # --- Simulate the call completing ---
    from api.db.models import WorkflowRunModel

    async with async_session() as s:
        run = WorkflowRunModel(
            name="r",
            workflow_id=wf.id,
            mode="pipeline",
            campaign_id=campaign_id,
            queued_run_id=queued[0].id,
            gathered_context={"call_disposition": "answered"},
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        run_id = run.id

    # --- WRITE-BACK: result lands on row 1 of the Results tab ---
    write_sheets = AsyncMock()
    write_sheets.values_batch_update = AsyncMock(return_value={})
    write_oauth = AsyncMock()
    write_oauth.get_valid_access_token = AsyncMock(return_value="tok")

    await write_back_run(
        run_id, oauth_client=write_oauth, sheets_client_factory=lambda t: write_sheets
    )

    write_sheets.values_batch_update.assert_awaited_once()
    entry = await db_client.get_sheets_writeback_entry(run_id)
    assert entry.state == "written"
```

- [ ] **Step 2: Run the full Sheets suite**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/test_credential_crypto.py api/tests/test_google_sheets_source_id.py api/tests/test_google_oauth_client.py api/tests/test_google_sheets_sync_service.py api/tests/test_source_sync_factory.py api/tests/test_sheets_writeback.py api/tests/test_sheets_writeback_service.py api/tests/test_workflow_completion_sheets_hook.py api/tests/test_google_integration_routes.py api/tests/test_google_sheets_lifecycle.py -v`
Expected: all pass.

- [ ] **Step 3: Run the broader suite to check for regressions**

Run: `source venv/bin/activate && set -a && source api/.env.test && set +a && python -m pytest api/tests/ -q -x`
Expected: no new failures introduced (pre-existing unrelated failures, if any, noted but not caused by this work). Pay particular attention to `api/tests/test_campaign_call_dispatcher.py` and `api/tests/test_campaign_tasks.py` — the factory/route changes in Task 8 must not regress CSV-sourced campaigns.

- [ ] **Step 4: Commit**

```bash
git add api/tests/test_google_sheets_lifecycle.py
git commit -m "test(sheets): end-to-end read-sync -> call -> write-back lifecycle"
```

---

## Self-Review

**Spec coverage check (against `phase-5-google-sheets-integration.md`):**
- Google OAuth connect per org, scopes, token storage/refresh via `ExternalCredentialModel` → Tasks 2 (crypto — new, flagged as a real gap vs. the spec's "existing mechanism" claim), 5 (`GoogleOAuthClient`), 13 (connect/callback/status/disconnect routes). ✓
- Service-account alternative → **not implemented** (spec marks it explicitly as a v1 non-default fallback, "revisit if... a bottleneck for launch" — correctly out of this plan's scope; noted here so it isn't mistaken for an oversight).
- `GoogleSheetsSyncService` implementing the ABC + factory registration + regex relax → Tasks 4 (source_id), 6 (Sheets API wrapper), 7 (service), 8 (factory + regex). ✓
- `source_uuid` = `gsheet_{id}_{sheet}_{row}` → Task 7, tested for determinism/stability across repeated syncs. ✓
- Write-back service: append/update by row, column mapping config, batched `values.batchUpdate`, backoff, idempotent → Tasks 3 (ledger model), 9 (mapping), 10 (ledger DB mixin), 11 (service with retry/backoff/idempotency for both modes). ✓
- Migration → Task 3, with explicit instruction to re-confirm `down_revision` against the live alembic head at execution time (not hardcoded past `b1f0c0de0001`, since other phase plans may land migrations first). ✓
- Error handling: revoked token, deleted sheet, missing phone col, quota → Task 5 (`GoogleCredentialsRevoked`), Task 7 (`validate_source_data` reuse — identical to CSV's missing-`phone_number` contract), Task 11 (429/5xx backoff + `failed` ledger state, never raises into the completion path — explicitly tested in Task 11's last test case). Deleted-sheet 404/400 from the live Sheets API surfaces as a non-retryable `GoogleSheetsApiError` in Task 11's `except Exception` branch (status not in `_RETRYABLE_STATUS_CODES`) → `failed` ledger, not silently dropped. ✓
- WhatsApp is out of scope → stated up front in Global Constraints and Executive framing; nothing in any task touches messaging channels. ✓
- Feature-flagged, off by default → Task 1 flag; every new route/task/campaign-creation path is guarded (Tasks 8, 12, 13). ✓
- Batching per spreadsheet within a debounce window → **partial**: Task 11's `write_back_run` issues one API call per completed run (single-row `batchUpdate`/`append`), which is correct and safe, but the spec's cross-run debounce/batching ("multiple pending write-backs for the same spreadsheet... combined into a single call") is **not implemented as a separate batching worker** in this plan — each `sheets_writeback` arq task call is independent. This is a deliberate scope trim: a per-run call already satisfies correctness and the 300/min project quota is unlikely to bind before the design-partner rollout phase (per spec's own rollout gating). Flagged as a explicit follow-up, not silently dropped, so the implementer/reviewer can decide whether to add a debounce batching layer before Task 12's rollout step.

**Placeholder scan:** none — every code step contains real, complete code; commands include expected output. Two steps (Task 5 Step 6, Task 12 Step 4) explicitly call out a test typo / an illustrative-not-final snippet that must be reconciled against live code, each with an explicit verification command.

**Type consistency:** `SheetSourceRef`/`encode_sheet_source_id`/`decode_sheet_source_id`/`sheet_range` used identically across Tasks 4, 7, 11. `GoogleSheetsApiError` used identically across Tasks 6, 11. `CampaignSheetsWritebackModel` ledger states (`pending`/`written`/`failed`) used identically across Tasks 3, 10, 11, 12. `write_back_run` signature identical in Tasks 11, 12, 14. ✓

**Note for implementer:** two places explicitly defer to runtime inspection with a verification command: (1) the exact arq enqueue mechanism/import path in Task 12 Step 4 (`get_arq_pool`/`enqueue_job` is illustrative — match `api/tasks/campaign_tasks.py`'s actual pattern), and (2) the router mount prefix in Task 13 Step 1. A third, `db_client.async_session` usage inside `api/routes/google_integration.py`'s `callback` handler (Task 13) — confirm `db_client` exposes `async_session` directly (it does, per every other `*_client.py` mixin's `self.async_session()` calls) or route through a small `IntegrationClient` helper instead if a direct route-level session isn't the established convention; check `grep -n "async_session" api/db/base_client.py` before finalizing.
