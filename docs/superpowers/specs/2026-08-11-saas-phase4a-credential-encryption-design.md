# Phase 4a — Credential Encryption at Rest

Date: 2026-08-11
Status: approved, not implemented
Parent spec: `docs/superpowers/specs/2026-07-21-saas-platform-design.md` §10

## Goal

Encrypt every stored third-party credential so that a database dump, a stolen
backup, or read access to Postgres does not hand over customers' telephony
accounts, integration tokens, and — worst of all — the platform's own AI
provider keys.

## Why this is phase 4a and not all of phase 4

An audit of the parent spec's §10–11 found most of the "hardening" list already
shipped in phases 1–3:

| §10–11 item | State |
| --- | --- |
| Razorpay webhook signature | Done — `api/routes/webhooks.py:68` |
| Clerk/Svix webhook signature | Done — `api/routes/clerk_webhooks.py:20` |
| Telephony webhook signatures | Done — all six providers verify |
| Boot-time secret validation | Done — `api/services/saas_config.py` |
| Sentry (API) | Done and mode-gated — `api/app.py:18` |
| Sentry (UI) | Dependency present, never wired: no `sentry.*.config.ts` |
| **Credential encryption at rest** | **Not started — this spec** |
| Prometheus `/metrics` | Not started — later sub-phase |
| Rate limiting | Not started — later sub-phase |
| Deploy hardening | Not started — later sub-phase |
| Admin analytics page | Not started — later sub-phase |

The remaining items share no code and no risk profile. Encryption is separated
because it is the only one that can destroy data: a botched migration makes
every telephony credential permanently unrecoverable. It is also the only one
that is a genuine launch blocker.

## What is being encrypted

Four locations hold secrets today, all plaintext JSON.

| Column | Model line | Contents |
| --- | --- | --- |
| `telephony_configurations.credentials` | `api/db/models.py:471` | carrier account ids + auth tokens |
| `integrations.connection_details` | `api/db/models.py:599` | integration OAuth/API secrets |
| `external_credentials.credential_data` | `api/db/models.py:1251` | webhook API keys, bearer tokens, basic-auth passwords |
| `organization_configurations.value` | `api/db/models.py:444` | conditional — see below |

`external_credentials.credential_data` already carries the comment "Encrypted
credential data (JSON)". It is not encrypted. The comment is aspirational and
should be corrected as part of this work.

### The fourth column is conditional

`organization_configurations.value` is a mixed bag keyed by the `key` column
(`OrganizationConfigurationKey`, `api/enums.py:79`). Only four of its nine key
types carry secrets:

```
SECRET_CONFIG_KEYS = {
    TELEPHONY_CONFIGURATION,
    TWILIO_CONFIGURATION,     # deprecated, still readable
    LANGFUSE_CREDENTIALS,
    MODEL_CONFIGURATION_V2,
}
```

The rest — `DISPOSITION_CODE_MAPPING`, `CONCURRENT_CALL_LIMIT`,
`ORGANIZATION_PREFERENCES`, and so on — are ordinary configuration that stays
readable in the database for operations and debugging.

`MODEL_CONFIGURATION_V2` deserves emphasis: `seed_platform_model_configuration`
(`api/services/configuration/platform_defaults.py`) copies the platform's raw
`PLATFORM_OPENAI_API_KEY`, `PLATFORM_DEEPGRAM_API_KEY`, and
`PLATFORM_ELEVENLABS_API_KEY` into **every organization's** config row. These
are not tenant secrets; they are the platform's own keys, duplicated per org,
billed to the platform. They are the highest-blast-radius secret in the
database.

Rejected alternative: encrypting the value of every config row regardless of
key. Simpler rule, but it turns all operational configuration into opaque blobs
for no security gain.

Rejected alternative: field-level encryption inside the JSON. Most surgical,
but requires a field map per config schema that silently stores secrets in
plaintext the moment a schema gains a field nobody added to the map.

## Architecture

### Crypto core

One module, `api/services/security/credential_crypto.py`, owns all key
handling. No other module imports `Fernet`.

```
CREDENTIAL_ENCRYPTION_KEYS   (env: comma-separated, first key = primary)
        │
        ▼
credential_crypto.py
  ├─ get_cipher()      -> MultiFernet | None    # None when unconfigured
  ├─ encrypt_json(obj) -> str
  ├─ decrypt_json(raw) -> obj                   # plaintext passes through
  └─ is_encrypted(raw) -> bool
        │
        ▼
EncryptedJSON(TypeDecorator)    # api/db/types.py
  process_bind_param    encrypt when a cipher exists, else store as today
  process_result_value  decrypt when the value looks encrypted, else pass through
```

The cipher is built once at import. `MultiFernet` construction validates every
key, and doing that per row would cost measurably on the inbound-call path.

### Why TypeDecorator and not ORM event listeners

`webhook_credential_client.py:147` writes `credential_data` through a
Core-level `update().values()`. ORM `before_insert` / `before_update` listeners
do not fire for Core statements, so that write would silently store plaintext.
A `TypeDecorator` operates at the bind-parameter level and covers ORM and Core
alike. This is the deciding constraint.

The trade-off `TypeDecorator` cannot solve: `process_bind_param` receives only
the value, never the surrounding row, so "encrypt only when the sibling `key`
column is one of four values" is impossible to express in it. Hence the
asymmetry below.

### Per-column wiring

| Column | Mechanism |
| --- | --- |
| `integrations.connection_details` | `EncryptedJSON`, drop-in |
| `external_credentials.credential_data` | `EncryptedJSON`, drop-in |
| `telephony_configurations.credentials` | `EncryptedJSON`, after the `account_id` change below |
| `organization_configurations.value` | plain `JSON` + explicit calls in `api/db/organization_configuration_client.py` |

The org-config client is the containment boundary for the conditional case: all
writes funnel through `upsert_configuration` (line 35) and all reads through
four `select()` methods in the same file. One other reader exists,
`organization_usage_client.py:327`, which must also decrypt.

### Decryption is content-sniffed, not config-driven

`process_result_value` decides by inspecting the stored value, not by asking
whether encryption is enabled. This is what makes the migration window safe: a
table mid-backfill holds both plaintext and ciphertext and both read correctly.

The discriminator is the JSON *type*, not a string prefix. These columns
declare `default=dict` and every legacy value is a JSON object; an encrypted
value is stored as a JSON string holding the Fernet token. So:

```python
def is_encrypted(raw) -> bool:
    return isinstance(raw, str) and raw.startswith("gAAAAA")
```

Type and prefix together. A dict is unambiguously legacy plaintext, and Fernet
tokens are base64 carrying a `0x80` version byte, which renders as `gAAAAA`.
Relying on the prefix alone would be fragile; relying on the type alone would
misread any column that legitimately stored a bare JSON string.

### Decrypt failure is fatal

If a value is a Fernet token that no configured key decrypts, raise. Returning
`None` or `{}` would turn "wrong key deployed" into calls silently failing to
authenticate against the carrier — far harder to diagnose than a loud error.

### Mode behaviour

Encryption is **optional everywhere, mandatory in saas**. When
`CREDENTIAL_ENCRYPTION_KEYS` is unset, behaviour is byte-for-byte what it is
today; OSS self-hosters are not forced to manage a key. `validate_saas_config`
(`api/services/saas_config.py`) gains a check requiring at least one valid key
when `DEPLOYMENT_MODE=saas`.

The accepted cost: because plaintext passes through the read path permanently,
a row that never gets encrypted is never loud. `--verify` (below) is the
countermeasure, and belongs in the launch checklist.

### Key management

The parent spec named a singular `CREDENTIAL_ENCRYPTION_KEY`. This spec
supersedes that with the plural `CREDENTIAL_ENCRYPTION_KEYS`, because rotation
support is a list from day one. Nothing reads the singular name yet, so there
is no compatibility burden.

`CREDENTIAL_ENCRYPTION_KEYS` is a comma-separated list. The first key encrypts;
all keys are tried for decryption. Rotation is: prepend a new key, restart,
re-encrypt in the background, drop the old key.

```python
f = MultiFernet([Fernet(k) for k in keys])   # keys[0] encrypts
f.decrypt(token)                              # tries all
f.rotate(token)                               # re-wraps under keys[0]
```

Building the list form now costs nothing. Retrofitting rotation onto a
single-key deployment later requires its own migration under a maintenance
window.

Rejected alternative: external KMS. Strongest key custody, but adds a cloud
dependency and per-call latency to a single-VPS deployment that has neither.
Revisit if the deployment story changes.

## Denormalizing `account_id`

### The problem

`telephony_configurations.credentials` is read at the SQL level in three
places, so encrypting the blob breaks them:

| Site | Query |
| --- | --- |
| `telephony_phone_number_client.py:166` | `credentials->>account_id_field == account_id` |
| `telephony_phone_number_client.py:216` | same, for the conflict guard |
| `telephony_configuration_client.py:92,97` | `credentials->>'webhook_public_key'` null/empty check |

The first is `find_inbound_route_by_account`, which resolves the owning
organization on **every inbound call**. Encrypting `credentials` without
addressing it breaks all inbound calling, including the platform numbers
shipped in phase 3.

The field name is per-provider — Twilio's `account_sid`, and so on — which is
why it arrives as the `account_id_field` argument rather than being hardcoded.

### The change

```
telephony_configurations
  + account_id  VARCHAR NULL, indexed
```

Populated on create and update in `telephony_configuration_client.py` by
resolving the provider's `account_id_field` through the existing provider
registry and copying `credentials[field]`. Nullable, because providers such as
ARI have no account concept and already route inbound through a different path
— the existing code returns `None` for them.

An account id is an identifier, not a secret; the `auth_token` stored beside it
is. Exposing it in its own column is not a weakening.

Reads become:

| Site | After |
| --- | --- |
| `telephony_phone_number_client.py:166` | `account_id == account_id` |
| `telephony_phone_number_client.py:216` | same |
| `telephony_configuration_client.py:92,97` | Python filter over the org's configs |

The Telnyx case is deliberately handled differently. It does not test an
account id — it tests whether the customer has pasted their portal-issued
webhook public key, to drive a UI nudge. That field is secret-adjacent and
cannot be denormalized on the same rationale. It is a per-org count over a
handful of rows feeding a warnings endpoint, so loading and filtering in Python
is equivalent in practice.

### Promoting the invariant

`find_conflicting_inbound_route` documents that
`(provider, credentials[account_id_field], address_normalized)` must be
globally unique, "or two orgs would race for the same call". Today only
application code enforces it. With a real column this becomes a partial unique
index on `(provider, account_id, address_normalized)` where `account_id IS NOT
NULL` — strictly stronger than the status quo, and worth doing here rather than
leaving the invariant to a docstring and a pre-insert check.

## Backfill

`scripts/encrypt_credentials.py`, run as `python -m scripts.encrypt_credentials`,
following the working precedent of `scripts/dump_docs_openapi.py`.

(`AGENTS.md` cites `python -m api.services.admin_utils.local_exec` as the
convention for one-off scripts, but no such module exists. Either that
reference or this placement should be reconciled; this spec follows the code.)

```
--dry-run          default ON; reports counts, writes nothing
--apply            perform writes
--table <name>     one table, or all
--batch-size 500   commit per batch
--verify           report plaintext vs encrypted counts; exit 1 if plaintext remains
--decrypt          inverse, for escape-hatch use while a key is still available
```

Idempotent: every row is tested with `is_encrypted()` first, so a re-run after
interruption resumes instead of double-encrypting. Batched with per-batch
commits, so an interruption leaves a partially-converted table the next run
finishes, rather than one transaction that discards an hour of work.

Rejected alternative: an Alembic data migration. Alembic runs inside deploy
automation where the key may be absent, a failure leaves a half-converted
table, and the downgrade path would itself need the key.

Rejected alternative: lazy encryption on next write only. Zero day-one risk,
but stable telephony credentials are rarely edited, so most rows would stay
plaintext indefinitely and the vulnerability would persist.

## Rollout order

Each step depends on the previous one still being readable.

1. Deploy code with `EncryptedJSON` wired and **no key configured**. Behaviour
   is unchanged; this proves the read path is transparent before any data moves.
2. Migration: add `account_id`, backfill it from still-plaintext `credentials`,
   add the partial unique index.
3. Switch the three queries to `account_id`. Inbound routing no longer depends
   on the JSON blob.
4. Set `CREDENTIAL_ENCRYPTION_KEYS` and restart. New writes encrypt; existing
   plaintext still reads.
5. `--dry-run`, check counts, `--apply`, then `--verify`.

Steps 1–3 are individually revertible. After step 4, `--decrypt` is the escape
hatch while a key is still available.

**The one unrecoverable failure is losing every key while encrypted data
exists.** No design prevents this. The keys must be backed up somewhere other
than the VPS holding the database, and the launch checklist must assert it.

## Testing

- Crypto module: round-trip, rotation through `MultiFernet`, plaintext
  passthrough, wrong key raises.
- Per column, a model-level test asserting the database holds ciphertext while
  the ORM returns the original object. This is the test that catches a column
  someone forgot to wire.
- A test that the Core-level `update()` at `webhook_credential_client.py:147`
  encrypts — the path ORM event listeners would have missed.
- Org-config conditional: secret key types encrypt, non-secret key types remain
  readable JSON.
- Backfill: idempotency, mixed plaintext/ciphertext tables, `--verify` exit
  code, `--decrypt` round-trip.
- Inbound routing resolves correctly with `credentials` encrypted — the
  regression that would take down phase 3.
- `validate_saas_config` rejects saas mode with no key, and accepts OSS mode
  with no key.

## Out of scope

Prometheus `/metrics`, rate limiting, deploy hardening, admin analytics, and
wiring Sentry into the UI. Each gets its own spec.

Encrypting non-credential PII (transcripts, recordings, contact lists) is not
addressed here.
