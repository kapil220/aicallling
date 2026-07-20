# Google Sheets Integration — Design Spec

**Date:** 2026-07-21
**Phase:** 5 of 5 (managed-SaaS program)
**Status:** Approved for planning

---

## Context

Dograh campaigns dial a list of leads and, per call, produce a rich result: transcript,
recording, gathered context, disposition, cost. Today the *only* lead source is a CSV
file uploaded to object storage, and results never flow anywhere except the product's
own UI/API — there is no built-in way to push outcomes into the spreadsheet a sales/ops
team actually lives in.

Many prospective managed-SaaS customers run their outreach lists and result tracking out
of **Google Sheets**. This phase adds Google Sheets as a **bidirectional** campaign
integration:

1. **Read** — a spreadsheet tab becomes a campaign's lead source, replacing/joining CSV.
2. **Write-back** — after each call completes, its disposition/outcome/artifacts are
   written back to a results sheet, so the customer's spreadsheet becomes a live
   dashboard without them opening Dograh.

This is deliberately built **before** WhatsApp (a separate, later integration) because it
reuses more of the existing campaign machinery and unblocks a common customer request
with the lowest net-new surface area.

### What already exists and is reused (not rebuilt)
- **Source abstraction** — `api/services/campaign/source_sync.py` defines
  `CampaignSourceSyncService` (ABC, line 26) with `normalize_headers()` and
  `validate_source_data()` (phone-number-column enforcement) already factored out of the
  CSV implementation. Sheets reuses this base class as-is.
- **Source factory** — `api/services/campaign/source_sync_factory.py::get_sync_service()`
  (line 5) — a one-line dict lookup. This is *the* extension point: adding
  `"google_sheets": GoogleSheetsSyncService` is the entire wiring change.
- **CSV reference implementation** — `api/services/campaign/sources/csv.py`
  (`CSVSyncService`) is the pattern to mirror: `_fetch_csv_data` → parse → build
  `context_vars` per row → require `phone_number` → generate a deterministic
  `source_uuid` → bulk-insert `QueuedRunModel` rows. Sheets follows the identical shape
  with a different fetch/parse step.
- **Campaign orchestration** — `api/tasks/campaign_tasks.py::sync_campaign_source`
  (line 21, an arq task) already calls `get_sync_service(campaign.source_type)` and
  drives campaign state `created → syncing → running`. Unchanged for Sheets; it just
  starts resolving a different service class.
- **Campaign/queued-run models** — `CampaignModel` (`api/db/models.py:681`,
  `source_type` / `source_id`) and `QueuedRunModel` (`api/db/models.py:787`,
  `source_uuid` / `context_variables`) are reused unmodified for storage of *what to
  call*.
- **Per-call result models** — `WorkflowRunModel` (`api/db/models.py:518`:
  `gathered_context`, `usage_info`, `cost_info`, `recording_url`, `transcript_url`,
  `call_type`, `state`, `campaign_id`, `queued_run_id`) and `call_disposition_codes` on
  `WorkflowModel` (`api/db/models.py:457`) are the read side for write-back — no new
  per-call fields are needed.
- **Third-party credential framework** — `IntegrationModel` (`api/db/models.py:335`,
  provider + `connection_details` JSON), `ExternalCredentialModel`
  (`api/db/models.py:953`, encrypted secret storage keyed by `credential_uuid`), and
  `ToolModel`/`ToolType.INTEGRATION` (`api/enums.py:145`, already annotated "future:
  Google Calendar, Salesforce, etc.") together are the existing pattern for storing
  per-org OAuth connections. Google Sheets is the first concrete tenant of this pattern
  for OAuth (rather than static API-key) credentials.
- **Post-call integration hook** — `api/tasks/workflow_completion.py::process_workflow_completion`
  (line 53) already runs `run_integrations_post_workflow_run` (`api/tasks/run_integrations.py`)
  *after* recording/transcript upload completes and the run is otherwise finalized. This
  is the trigger point for write-back — no new completion signal needed.
- **Signed-URL storage pattern** — CSV downloads via `storage_fs.aget_signed_url`
  (`api/services/storage`). Not reused directly (Sheets API replaces object storage
  fetch) but kept as the template for how the read path fetches remote data.

### What's new
- Google OAuth (per-org "Connect Google Sheets") using the existing
  `IntegrationModel` / `ExternalCredentialModel` framework, but for the *first* OAuth
  (as opposed to static API key) integration — token refresh handling is new.
- `GoogleSheetsSyncService` (read side, implements `CampaignSourceSyncService`).
- A write-back service invoked from the post-call integration path, plus a small
  campaign-level config (target spreadsheet/tab/column mapping).
- A `google-api-python-client` / `google-auth` dependency — **zero existing usage** of
  any Google SDK in the repo today.

---

## Goals

1. A customer can **connect a Google account** to their organization once, and use it as
   the credential for both reading leads and writing results across all their campaigns.
2. A campaign can be **created from a Google Sheet** (spreadsheet + tab + optional range)
   instead of a CSV upload, using the *same* campaign-creation flow and the *same*
   `phone_number`-column validation the CSV path already enforces.
3. After each call in a Sheets-sourced (or any) campaign, the **call outcome is written
   back** to a configured results sheet — either updating the lead's original row or
   appending a new row — without manual export/import.
4. Write-back is **idempotent**: retries of the same completion event never produce
   duplicate rows or corrupt an existing row.
5. Failures (revoked token, deleted sheet, quota exhaustion) are **isolated per
   campaign/run** — they degrade to logged/surfaced errors, never crash the call pipeline
   or block other campaigns.
6. Shipped **behind a flag**, off by default, so it doesn't affect existing CSV-only
   deployments.

### Non-goals
- **Real-time bidirectional sync.** No live polling/watching for edits mid-campaign;
  reads happen once at sync time, writes happen once per call completion (see
  Idempotency). A user editing the sheet while a campaign runs does not affect
  already-queued runs.
- **Arbitrary spreadsheet formulas / computed cells.** We read/write plain cell values
  only; we do not evaluate or preserve formulas, conditional formatting, or charts.
- **Non-Google spreadsheet formats** (Excel Online, Airtable, Notion databases, etc.) —
  explicitly out of scope for this phase.
- **WhatsApp integration.** A separate, later spec ("Phase 5b — WhatsApp Integration")
  covers WhatsApp-based lead intake / conversational messaging. It shares none of this
  phase's code paths beyond the general `IntegrationModel` credential pattern and is not
  designed here.
- **Multi-sheet joins / lookups.** One tab = one source; no VLOOKUP-style cross-tab
  enrichment.

---

## Google OAuth

### Connection model
- One **Google account connection per organization**, stored as an `IntegrationModel`
  row (`provider="google_sheets"`, `api/db/models.py:335`) plus an
  `ExternalCredentialModel` row (`api/db/models.py:953`) holding the encrypted
  `refresh_token` (and short-lived `access_token` + `expires_at` cached in
  `connection_details` for fast reads without decrypting on every call — the
  `access_token` itself is not sensitive enough to bar caching since it's already short
  TTL, but is still encrypted at rest same as the refresh token).
- UI flow: org admin clicks "Connect Google Sheets" → standard OAuth 2.0 authorization
  code flow → callback exchanges code for tokens → `IntegrationModel` +
  `ExternalCredentialModel` rows created, scoped to the org (`organization_id`), *not*
  the individual user, so any org member can create Sheets campaigns using the org's
  connection (mirrors how other org-level integrations already work).
- Only **one active connection per org** for `provider="google_sheets"` in v1 (no
  per-campaign account selection) — keeps the credential-resolution path trivial:
  "does this org have an active Google Sheets integration row."

### Scopes
Principle of least privilege — request only:
- `https://www.googleapis.com/auth/spreadsheets` — read/write cell values in sheets the
  connected account has access to. (A read-only variant exists but write-back needs the
  full scope; we do not request two separate scopes for read vs. write since almost
  every customer wants both directions.)
- `https://www.googleapis.com/auth/drive.file` — **not** the broad `drive.readonly` —
  scoped to files the app has been explicitly given access to (via the Google **Picker**
  UI, see below), so we never get blanket read access to a customer's entire Drive.

Sheet selection uses the **Google Picker API** (client-side) so the user explicitly
grants per-file access rather than pasting a spreadsheet ID/URL blind; this is what
justifies `drive.file` instead of a broader Drive scope and also removes a class of
support issues ("sheet not found" because the service account/OAuth identity was never
shared on the file).

### Token storage & refresh
- Refresh token: long-lived, stored encrypted in `ExternalCredentialModel` (existing
  encryption-at-rest mechanism for credentials — reused unchanged).
- Access token: short-lived (~1hr), cached alongside with an `expires_at` timestamp;
  refreshed lazily on first use after expiry via the standard OAuth refresh-token grant.
- Refresh happens in a thin `GoogleOAuthClient` (part of the read/write services below)
  that wraps `google.oauth2.credentials.Credentials` and calls `.refresh()` when
  `expires_at` has passed, then persists the new access token + expiry back onto the
  `ExternalCredentialModel` row. No new background refresh job in v1 — refresh-on-use is
  sufficient given campaign sync and write-back are the only call sites.
- **Revocation:** if a refresh attempt fails with `invalid_grant` (token revoked or
  expired past the refresh window), the integration is marked inactive
  (`IntegrationModel.is_active = False`) and surfaced to the org (see Error handling).

### Alternative considered: Service Account
A Google Cloud **service account** (with the customer manually sharing their sheet with
the service account's email) is a viable alternative that avoids OAuth-consent-screen
verification overhead and per-user token refresh entirely. It is **not** the v1 default
because it requires a manual, easy-to-get-wrong step (share the sheet) with no
discoverability, and it doesn't map cleanly to "the org's own Google identity" for
Drive Picker-based sheet selection. It is called out here as a fallback path worth
revisiting if Google's OAuth consent-screen verification (needed to move
`drive.file`/`spreadsheets` out of the "unverified app" testing-mode 100-user cap)
proves to be a bottleneck for launch — a service account has no such user cap.

---

## READ: Google Sheets as a lead source

### `GoogleSheetsSyncService` (`api/services/campaign/sources/google_sheets.py` — new)
Implements `CampaignSourceSyncService` (`api/services/campaign/source_sync.py:26`),
mirroring `CSVSyncService` method-for-method:

- `async def validate_source(source_id, organization_id) -> ValidationResult` — fetches
  the header row + a sample of data rows via the Sheets API `values.get` call, then
  delegates to the inherited `validate_source_data()` (unchanged: still requires a
  `phone_number` column after `normalize_headers()`).
- `async def sync_source_data(campaign_id) -> int` — mirrors
  `CSVSyncService.sync_source_data` (`api/services/campaign/sources/csv.py:64`):
  1. Load campaign, parse `campaign.source_id` into `(spreadsheet_id, sheet_name, range)`
     (see encoding below).
  2. Resolve the org's Google credentials via `IntegrationModel` lookup + the OAuth
     refresh helper above.
  3. Call Sheets API `values.get(spreadsheetId, range=f"{sheet_name}!{range}")` to fetch
     all rows (header + data) in one batch call — no server-side pagination needed for
     typical campaign sizes (Sheets API returns up to the grid's populated extent in a
     single response; very large sheets are chunked internally by the client library,
     not something this service needs to hand-roll).
  4. `headers = normalize_headers(rows[0])`; for each data row, zip into `context_vars`
     exactly as CSV does, pad short rows, skip rows with no `phone_number`.
  5. **`source_uuid = f"gsheet_{spreadsheet_id}_{sheet_name}_{row_idx}"`** — deterministic
     per spreadsheet+tab+row, mirroring CSV's `csv_{file_hash}_row_{idx}` pattern (line
     103 in `csv.py`). Using the row index (not a content hash) is intentional: it lets
     write-back address "the sheet row this lead came from" directly (see below), which
     a content hash cannot do if a cell value changes between sync and write-back.
  6. Bulk-insert `QueuedRunModel` rows exactly as CSV does
     (`db_client.bulk_create_queued_runs`).

### `source_id` encoding
CSV's `source_id` is a bare file key. Sheets needs three pieces of information, so
`source_id` becomes a small delimited/JSON-encoded string, e.g.:
```
gsheet:{spreadsheet_id}:{sheet_name}:{a1_range}
```
(`a1_range` optional — defaults to the full used range of the tab if omitted). This
keeps `CampaignModel.source_id` a single `String` column (`api/db/models.py:696`, no
schema change) while carrying everything `GoogleSheetsSyncService` needs. The
campaign-creation payload from the UI sends spreadsheet id / tab / range as separate
fields (post-Picker selection); the API route composes this encoded string before
persisting.

### Registration
- `api/services/campaign/source_sync_factory.py:8` — extend the `services` dict:
  ```
  services = {
      "csv": CSVSyncService,
      "google_sheets": GoogleSheetsSyncService,
  }
  ```
  This is the entire routing change; `sync_campaign_source`
  (`api/tasks/campaign_tasks.py:21`) needs no changes since it already calls
  `get_sync_service(campaign.source_type)` generically.
- `api/routes/campaign.py:155` — the campaign-creation request model's
  `source_type: str = Field(..., pattern="^csv$")` must be relaxed to
  `pattern="^(csv|google_sheets)$"`.
- `api/routes/campaign.py:946` (`get_campaign_source_download_url`) explicitly rejects
  non-CSV sources with a 400 ("Download URL only available for CSV sources"). This is
  **correct behavior to keep** for Sheets — there is no analogous "download the original
  file" concept; a Sheets-sourced campaign's canonical source is the live spreadsheet,
  linked back to via a `spreadsheet_url` computed from the encoded `source_id` and shown
  in the UI instead of a download button. No route change needed here beyond the
  existing check already covering it (any `source_type != "csv"` falls into the same
  rejection branch it already has, including the new `google_sheets` value).

---

## WRITE-BACK: call results into a results sheet

### Trigger point
`api/tasks/workflow_completion.py::process_workflow_completion` (line 53), **after**
Step 3 (`run_integrations_post_workflow_run`, line 171) — i.e. write-back is added as a
new step in the same sequential completion task, after recording/transcript URLs are
final and QA/webhook integrations have run (so `gathered_context` reflects any
QA-node enrichment and disposition codes are final). Scoped by a new
`has_sheets_writeback_configured(workflow_run)` guard (checks whether the run's campaign
has write-back config, see below) so runs outside any campaign, or campaigns without
write-back configured, pay zero cost.

### What gets written
Per completed `WorkflowRunModel` (`api/db/models.py:518`), a row is written/updated with
a **configurable column mapping** from a fixed set of source fields:

| Source field | From |
|---|---|
| Call state | `WorkflowRunModel.state` |
| Disposition | `WorkflowModel.call_disposition_codes` (`api/db/models.py:457`) matched/derived for this run — same resolution the existing dashboard uses |
| Duration | `WorkflowRunModel.usage_info` (`call_duration_seconds`, same field Phase 1 billing reads) |
| Cost | `WorkflowRunModel.cost_info` |
| Recording URL | `WorkflowRunModel.recording_url` |
| Transcript URL | `WorkflowRunModel.transcript_url` |
| Gathered context (any subset) | `WorkflowRunModel.gathered_context` — customer picks which keys become columns |
| Call timestamp | run completion time |

Column mapping is **campaign-level config**: `{sheet_column_letter_or_header: source_field_path}`.
Only fields the customer maps are written — no dumping of the full `gathered_context`
blob into one cell by default (though "raw JSON dump" remains available as one mappable
"field" for customers who want it).

### Row addressing: update-in-place vs. append
Two write-back modes, selected per campaign:
1. **Update mode** (default for Sheets-sourced campaigns) — the lead's original row is
   updated in place. Row number is recovered directly from `source_uuid`
   (`gsheet_{spreadsheet_id}_{sheet}_{row_idx}` — the `row_idx` suffix *is* the sheet
   row), via `QueuedRunModel.source_uuid` → `WorkflowRunModel.queued_run_id`
   (`api/db/models.py:518`/`787` FK chain already links a run back to its queued row).
   Write targets a *separate* results tab/column range from the lead-source tab (or the
   same tab, different columns) — never overwrites the lead's own input columns.
2. **Append mode** (default, and only option, for CSV-sourced campaigns writing to a
   Sheets destination) — since a CSV-sourced run has no sheet row to address, each
   completed call appends one row to a results sheet. This also covers the "any campaign
   can push results to Sheets even if leads came from CSV" case explicitly — write-back
   and read-in are independently configurable, not coupled to the same source type.

### Idempotency
Write-back is keyed by `workflow_run_id`. Before writing, the service checks a
**write-back ledger** (new lightweight table, `campaign_sheets_writeback` —
`workflow_run_id` unique, `written_at`) and no-ops if a row already exists for this run.
This mirrors the ledger-based idempotency pattern from Phase 1's billing debits
(`api/services/billing/billing_service.py` — see `phase-1-billing-engine-core.md`,
`idempotency_key=debit:{run_id}`), applied here to guard against
`process_workflow_completion` being retried (e.g. after a transient Sheets API error) and
appending/updating twice. For **update mode**, idempotency is doubly safe since a repeat
`values.update` on the same row+range is naturally idempotent (same cells, same values);
the ledger check exists primarily to protect **append mode**, where a retry would
otherwise create a duplicate row.

### Batching & backoff
- Google Sheets API v4 quota (default project-level): **300 write requests per minute
  per project**, 60/min per user — a per-call, per-write API call would exhaust this
  quickly under concurrent campaign load across all orgs sharing this app's Google Cloud
  project.
- Write-back does **not** call the Sheets API synchronously inline in the completion
  task. It enqueues a small write-back job (arq task, same queue infra as
  `campaign_tasks.py`) that a dedicated worker **batches**: multiple pending write-backs
  for the *same spreadsheet* within a short window (e.g. 2–5s debounce, or up to N rows)
  are combined into a single `values.batchUpdate` call, which counts as one write request
  regardless of row count.
- Exponential backoff with jitter on `429`/`5xx` from the Sheets API (standard
  Google-recommended retry policy), capped at a small number of attempts before the
  write-back is marked `failed` in the ledger table and surfaced (see Error handling) —
  never blocks the campaign or retries the call itself.

---

## Data models / config

### `CampaignModel` extension (`api/db/models.py:681`)
- `writeback_config` (JSON, nullable, default `null`) — new column:
  ```json
  {
    "provider": "google_sheets",
    "spreadsheet_id": "...",
    "sheet_name": "Results",
    "mode": "update" | "append",
    "column_mapping": {
      "F": "call_disposition",
      "G": "usage_info.call_duration_seconds",
      "H": "recording_url",
      "I": "gathered_context.customer_intent"
    }
  }
  ```
  Kept as a JSON blob (same pattern as other flexible per-campaign config already on
  `CampaignModel`) rather than new normalized columns, since the shape is
  provider-specific and this is the only provider in v1.

### `campaign_sheets_writeback` (new table)
Idempotency ledger for write-back, described above.

| Field | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `workflow_run_id` | FK workflow_runs, unique | idempotency key |
| `campaign_id` | FK campaigns | indexed, for batching lookups |
| `state` | enum | `pending` / `written` / `failed` |
| `error` | str, nullable | last error message if `failed` |
| `attempts` | int | for backoff bookkeeping |
| `written_at` | datetime, nullable | |

### `IntegrationModel` row (existing model, new `provider` value)
`provider = "google_sheets"`, `connection_details` JSON holds
`{ "google_account_email": "...", "access_token_expires_at": "...", "scopes": [...] }`
(display/debug metadata only — actual tokens live in `ExternalCredentialModel`, never in
`connection_details`, to keep credential storage in the one place that's encrypted).

---

## Google API quota, batching, backoff (summary)

- **Reads:** one `values.get` per campaign sync — campaign creation/sync is a rare,
  user-initiated action, not per-call, so read quota is a non-issue at expected volume.
- **Writes:** batched per spreadsheet via `values.batchUpdate`, debounced, with capped
  exponential backoff on `429`/`5xx` as described above.
- **Per-project quota** is shared across *all* orgs using this integration (single OAuth
  app/Cloud project). If aggregate write volume across all customers approaches the
  300/min ceiling, the batching window widens adaptively (increase debounce) rather than
  erroring — a future phase could request a quota increase from Google or shard across
  multiple Cloud projects, called out as an open question below.

---

## Data flow

```
Connect account (one-time, per org)
  └─ OAuth consent → IntegrationModel + ExternalCredentialModel (provider=google_sheets)

Create campaign from a Sheet
  └─ UI: Google Picker → user selects spreadsheet + tab
       └─ POST /campaigns { source_type: "google_sheets", source_id: "gsheet:<id>:<tab>:<range>",
                             writeback_config: {...} }
            └─ campaign.py:155 pattern now allows "google_sheets"

Sync leads (existing arq task, unchanged)
  └─ sync_campaign_source(campaign_id)
       └─ get_sync_service("google_sheets") → GoogleSheetsSyncService
            └─ resolve org's Google credentials (refresh if needed)
            └─ values.get(spreadsheet_id, tab!range) → header + rows
            └─ validate_source_data() (phone_number required, same as CSV)
            └─ per row: context_vars, source_uuid = gsheet_{id}_{tab}_{row}
            └─ bulk_create_queued_runs(...)
       └─ campaign state: syncing → running

Dial (existing campaign_call_dispatcher, unchanged)
  └─ QueuedRunModel → WorkflowRunModel per call

Call completes
  └─ process_workflow_completion(workflow_run_id)   [existing task]
       ├─ upload recording/transcript                [existing]
       ├─ run_integrations_post_workflow_run          [existing: QA, webhooks]
       └─ [new] if campaign.writeback_config present:
            └─ enqueue sheets_writeback(workflow_run_id)
                 └─ [worker] check campaign_sheets_writeback ledger (idempotent skip)
                 └─ batch with other pending write-backs for same spreadsheet
                 └─ values.batchUpdate (update-in-place row, or append)
                      ├─ success → ledger row = written
                      └─ 429/5xx → backoff + retry (bounded)
                           └─ exhausted → ledger row = failed, surfaced to org
```

---

## Error handling & edge cases

- **Revoked / expired refresh token:** refresh call fails with `invalid_grant` →
  `IntegrationModel.is_active = False`; in-flight sync/write-back jobs for that org fail
  fast with a clear "reconnect Google Sheets" error surfaced on the campaign (not a
  generic 500); campaign pauses rather than silently dropping leads or results.
- **Deleted or renamed sheet/tab:** Sheets API returns 400/404 on `values.get` /
  `values.update` with the stale `spreadsheet_id`/`sheet_name`. Sync/write-back fails
  that operation, logs the campaign into a `failed`-attributable state, and surfaces
  "source sheet no longer accessible" — does not silently skip rows or invent columns.
- **Permission loss mid-campaign** (customer revokes app access in Drive, or un-shares
  the file from a service-account-alternative deployment): manifests as a 403 from the
  Sheets API on the next sync or write-back attempt; treated the same as revoked token —
  campaign write-back marked `failed` per run in the ledger (retriable once access is
  restored, since the ledger only marks `written` on success) while reads pause the
  campaign.
- **Header changes after initial sync:** since sync happens once at campaign creation,
  header changes made *after* sync don't affect already-queued runs (consistent with the
  "no real-time sync" non-goal). A re-sync (if the customer edits and wants a refresh)
  re-validates headers from scratch via the same `validate_source_data()` path CSV uses,
  and rejects if `phone_number` is now missing — same failure mode as a bad CSV re-upload.
- **Missing `phone_number` column:** identical behavior to CSV today — `validate_source`
  returns `ValidationResult(is_valid=False, ...)` before any queued runs are created;
  surfaced at campaign-creation time, not mid-sync.
- **Malformed / short rows:** identical to CSV's existing pad-and-zip handling
  (`csv.py`'s `padded_row = row_values + [""] * (...)`) — reused verbatim in the Sheets
  service.
- **API quota exhaustion:** bounded exponential backoff (above); if attempts are
  exhausted, the ledger row is `failed` with the error recorded, and a per-campaign
  "N results failed to write back" indicator is surfaced (not a silent drop) — the
  underlying call data is never lost since it still lives on `WorkflowRunModel`
  regardless of write-back outcome, so a manual/backfill re-write is always possible
  later without re-running calls.
- **Write-back to a row that's been manually edited/deleted by the customer in the
  interim:** `values.update` targets an explicit row/range; if that row has since been
  deleted (shifting all rows up), the update silently lands on the wrong row — this is a
  known limitation of index-based addressing (see Open questions) and is mitigated by
  documenting to customers that mid-campaign manual row deletion in the source tab is
  unsupported while write-back is in update mode.

---

## Security

- **Token encryption at rest:** refresh/access tokens stored via the existing
  `ExternalCredentialModel` encrypted-secret mechanism — no plaintext tokens in the
  `integrations` or `campaigns` tables (`connection_details` on `IntegrationModel` holds
  only non-secret metadata, per the Data models section above).
- **Least-scope OAuth:** `spreadsheets` + `drive.file` only (never a broad
  `drive.readonly`/`drive` scope) — see OAuth section. Google Picker enforces per-file
  consent for `drive.file`, so a connected org's credential cannot read arbitrary Drive
  content even if a token were somehow exfiltrated.
- **Per-org isolation:** the Google connection is strictly `organization_id`-scoped on
  `IntegrationModel`; `GoogleSheetsSyncService` and the write-back worker always resolve
  credentials via the campaign's `organization_id`, never a global/shared credential —
  no cross-org data leakage path.
- **No token logging:** logging in `GoogleSheetsSyncService` and the write-back worker
  follows the existing convention (loguru, `csv.py`'s pattern) of logging file/campaign
  identifiers and error messages only — access/refresh tokens and full row payloads
  (which may contain PII from the customer's leads) are never logged; only counts and
  row indices are.
- **Spreadsheet content is customer PII by nature** (names, phone numbers) — this phase
  introduces no new PII storage beyond what CSV already stores in
  `QueuedRunModel.context_variables`; write-back similarly only writes fields the
  customer explicitly maps, and only to a destination the customer's own OAuth identity
  already has access to.

---

## Testing strategy

Tests run against the test DB via `api/.env.test` per AGENTS.md; Google Sheets API calls
are mocked (no live Google API dependency in CI).

**Unit**
- Header normalization + `phone_number` validation: reused `validate_source_data()` path
  exercised through `GoogleSheetsSyncService.validate_source` with mocked API responses
  (valid headers, missing `phone_number`, empty sheet, header-only sheet).
- `source_uuid` generation: deterministic `gsheet_{spreadsheet_id}_{sheet}_{row}` for a
  range of row indices; stability across repeated syncs of unchanged data.
- `source_id` encode/decode round-trip (`gsheet:id:tab:range` parsing, missing-range
  default-to-full-tab behavior).
- Write-back column mapping: given a `WorkflowRunModel` fixture and a `column_mapping`
  config, correct cell values are produced for each mapped field
  (`usage_info.call_duration_seconds`, nested `gathered_context.*` paths, disposition
  resolution, missing/null field → blank cell not an error).
- Idempotency: two write-back attempts for the same `workflow_run_id` against the
  `campaign_sheets_writeback` ledger → second is a no-op (asserted via mocked API call
  count == 1).
- OAuth token refresh: expired `access_token` triggers a refresh call and persists the
  new expiry; `invalid_grant` marks the integration inactive.

**Integration (mocked Google Sheets API)**
- Full read sync: mocked `values.get` response → `sync_campaign_source` produces the
  expected `QueuedRunModel` rows, campaign transitions `syncing → running`, `total_rows`
  set correctly — mirrors the existing CSV sync integration tests structurally.
- Full write-back: mocked `values.batchUpdate` → given N completed runs for a campaign
  with write-back configured, correct rows/columns are targeted for both `update` and
  `append` modes.
- Retry/backoff: simulated `429` responses → backoff attempted, eventual success or
  `failed` ledger state after exhausting retries, without raising into the completion
  task (campaign completion path never breaks due to a Sheets error).
- Validation rejection: campaign creation from a sheet missing `phone_number` is rejected
  before any `QueuedRunModel` rows are created, same contract as CSV.
- Factory registration: `get_sync_service("google_sheets")` returns
  `GoogleSheetsSyncService`; unknown source types still raise `ValueError` as today.

---

## Rollout

1. Ship `IntegrationModel`/`ExternalCredentialModel` OAuth flow, `GoogleSheetsSyncService`,
   write-back worker + ledger table + migration, and the relaxed `campaign.py:155` regex,
   all behind a `GOOGLE_SHEETS_INTEGRATION` flag (default off).
2. Register the Google Cloud OAuth app in **testing mode** (100-user cap) initially;
   verify end-to-end on an internal/staging org — connect, create campaign from a real
   sheet, run a small campaign, confirm write-back lands correctly in both `update` and
   `append` modes.
3. Submit the OAuth consent screen for Google verification (needed for `spreadsheets` +
   `drive.file` scopes at production scale) in parallel with staging validation, since
   verification lead time can be days-to-weeks.
4. Enable for a small set of design-partner orgs; monitor write-back ledger `failed`
   rates and Sheets API quota headroom.
5. Flip flag on generally for the hosted deployment once verification clears and
   design-partner feedback is incorporated.

---

## Open questions deferred
- **Row-shift resilience for update mode** — if a customer manually inserts/deletes rows
  in the source tab mid-campaign, index-based `source_uuid` addressing can target the
  wrong row on write-back. A future iteration could key off a stable ID column
  (customer-provided) instead of row index, at the cost of requiring one more column
  convention — deferred pending real customer feedback on how often this actually
  happens.
- **Multiple Google accounts per org** (e.g. different departments' spreadsheets) — v1
  is one connection per org; revisit if requested.
- **Cross-org Sheets API quota sharing** — if aggregate write volume nears the
  per-project ceiling, evaluate requesting a Google quota increase vs. sharding across
  multiple Cloud projects/OAuth apps per customer tier.
- **Live/near-real-time write-back** (e.g. sub-second after call end vs. batched) — v1's
  debounce window trades a few seconds of latency for drastically fewer API calls;
  revisit only if a customer use case specifically needs near-instant sheet updates.
- **WhatsApp integration** — fully deferred to "Phase 5b — WhatsApp Integration," a
  separate spec covering WhatsApp-based lead intake and/or messaging, not designed here.
