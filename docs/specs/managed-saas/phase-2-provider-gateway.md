# Provider Gateway — Design Spec

**Date:** 2026-07-21
**Phase:** 2 of 5 (managed-SaaS program)
**Status:** Approved for planning

---

## Context

Phase 1 built the local billing brain: a credit ledger, per-architecture pricing, and
pre/post-call authorization (`docs/specs/managed-saas/phase-1-billing-engine-core.md`).
It prices a call by **duration × architecture rate** — it does not need per-provider
usage to bill. But "dograh managed mode" today still depends on a **second external
closed service**: MPS (`services.dograh.com`, `api/constants.py:34`), which holds the
platform's own OpenAI/Deepgram/ElevenLabs/etc. keys and proxies LLM/STT/TTS traffic for
customers who don't bring their own keys. The pipecat `dograh` service clients
(`pipecat/src/pipecat/services/dograh/{llm,stt,tts}.py`) are hard-wired to MPS's
endpoints, auth scheme, and billing-correlation protocol (`mps_billing.py`).

To fully own the managed SaaS stack we need to stop depending on MPS for the "our keys"
path too. This phase replaces it with a **gateway we run ourselves**: a service that
holds the platform's provider keys, exposes clean streaming endpoints for LLM/STT/TTS,
authenticates callers with a **per-org gateway token** (never a raw provider key), and
reports exact usage against a **correlation id** so Phase 1's `BillingService` (or, for
now, the existing MPS-usage-reporting path) can account for it.

**Decision:** build a new, self-designed gateway API rather than reimplement MPS's
contract. This means rewriting the pipecat `dograh` clients to speak the new protocol —
a change that touches the `pipecat` submodule, not just `api/`.

### What already exists and is reused (not rebuilt)
- **Correlation-id concept** — MPS mints a `correlation_id` per run to tie streamed
  usage back to billing; the pipeline already threads it through
  `MPS_CORRELATION_ID_CONTEXT_KEY` / `get_mps_correlation_id()`
  (`api/services/managed_model_services.py:8-32`) and the pipecat services already
  accept/forward a `correlation_id` and read `mps_billing.py`'s
  `MPS_BILLING_VERSION_KEY` / `uses_mps_billing_v2()` /  `get_correlation_id()` helpers
  (`pipecat/src/pipecat/services/dograh/llm.py:16-21`, `stt.py:30-35`, `tts.py:30-35`).
  The gateway reuses this **pattern** (mint-once, thread-through, attach-to-every-call)
  but mints and validates its own correlation ids — it does not call out to MPS.
- **Effective-config resolution & `mode` selection** — `DograhManagedAIModelConfiguration`
  (`api/schemas/ai_model_configuration.py:49`) already distinguishes "dograh managed"
  (single platform-issued `api_key`) from BYOK; the gateway token replaces that
  `api_key`'s meaning but not its shape or plumbing.
- **Service-instantiation call sites** — `create_stt_service` (`api/services/pipecat/service_factory.py:106`,
  Dograh branch at line 192), `create_tts_service` (line 355, Dograh branch at line
  470), and the LLM builder (Dograh branch at line 736) already special-case
  `ServiceProviders.DOGRAH` and derive a `base_url` from `MPS_API_URL`. We reuse these
  branch points, only changing what URL/token they wire in.
- **Duration/usage metering & Phase 1 ledger** — `pipeline_metrics_aggregator.py` and
  `BillingService.debit_for_run` remain the billing source of truth; the gateway is a
  *usage-quality upgrade* (exact tokens/chars/audio-seconds instead of duration-only)
  that Phase 1 already anticipated ("real per-token/char cost capture at source → Phase
  2 gateway", see Phase 1's Open Questions).
- **Superuser admin surface pattern** — `api/routes/superuser.py` +
  `Depends(get_superuser)` is reused for gateway-token issuance/revocation endpoints.

### What's new
- A standalone **gateway service** (new FastAPI app, own deployable) holding platform
  provider keys and proxying LLM/STT/TTS.
- A **gateway token** primitive, scoped to an org, replacing the raw `api_key` in
  `DograhManagedAIModelConfiguration`.
- Rewritten pipecat `dograh` clients (`llm.py`, `stt.py`, `tts.py`) speaking the new
  gateway protocol instead of MPS's.
- Usage capture + reporting from the gateway into the local billing ledger's usage
  trail (informational in v1; Phase 1 already bills by duration regardless).

---

## Goals

1. Platform provider keys (OpenAI, Deepgram, ElevenLabs, etc.) live **only** in the
   gateway's secret store — never in `api/`, never in a client-visible config, never in
   pipecat process memory beyond a short-lived token.
2. Customers on "dograh managed" mode authenticate to the gateway with an **org-scoped
   gateway token**, not a provider key and not a shared platform secret.
3. The gateway captures **exact usage** (LLM tokens in/out, STT audio-seconds, TTS
   characters/audio-seconds) per request, tagged with a **correlation id**, and exposes
   it for billing/observability.
4. LLM/STT/TTS streaming semantics are preserved end-to-end (SSE-style chunks for LLM,
   full-duplex websocket framing for STT/TTS) with no added perceptible latency.
5. The pipecat `dograh` clients are rewritten against the new gateway contract; MPS
   billing-specific code (`mps_billing.py`, `MPS_BILLING_VERSION_*`) is retired from the
   managed-mode path.
6. Coexists behind a flag with BYOK and the legacy MPS path so rollout is incremental
   and reversible.

### Non-goals (later phases / explicitly out of scope)
- Changing Phase 1's billing *pricing model* (still duration × architecture rate). The
  gateway's per-token/char usage is captured and stored for future rate models and
  observability, not wired into `debit_for_run` in this phase.
- Self-serve Stripe top-ups (Phase 3).
- Non-superuser roles for gateway-token management (Phase 4).
- Multi-region / multi-cloud gateway deployment topology (operational concern, not
  covered by this spec).
- Provider-key rotation UI/automation beyond a manual admin endpoint.

---

## Key decisions

| Decision | Choice |
|---|---|
| Gateway API shape | New, self-designed contract. LLM: OpenAI-compatible `POST /v1/chat/completions` (SSE) so the client can keep using an OpenAI-SDK-style caller. STT/TTS: our own websocket framing (JSON control frames + binary audio frames), not a passthrough of any single provider's wire format. |
| Auth | Per-org **gateway token** (opaque, bearer), not the provider key. Gateway resolves token → org → provider-key set. |
| Correlation id | Minted by `api/` at authorize time (not by the gateway), passed through pipeline context and attached to every gateway request, exactly mirroring today's MPS correlation-id lifecycle. |
| Deployment | Separate FastAPI service (`gateway/`, new top-level component, own container), not a module inside `api/`. Reachable at `GATEWAY_URL`. |
| Usage reporting direction | Gateway → `api/` webhook/callback (push), not `api/` polling the gateway. Keeps the gateway stateless-ish and avoids `api/` needing gateway-side query capability. |
| Feature gate | `GATEWAY_ENABLED` (default off) selects gateway-backed Dograh-managed services; existing MPS-backed path remains the fallback until the gateway is proven. |
| Pipecat client rewrite | In place: `pipecat/src/pipecat/services/dograh/{llm,stt,tts}.py` keep their class names and call sites in `service_factory.py`, but their `__init__`/`create_client`/websocket-connect internals target the gateway protocol. `mps_billing.py` helpers are replaced by a `dograh_gateway.py` helper module with the same shape (`get_correlation_id`, a header/attach helper) so the diff at call sites is small. |

---

## Architecture

### Components

```
                         ┌───────────────────────────────┐
                         │              api/               │
                         │  - mints correlation_id at      │
                         │    authorize_workflow_run_start │
                         │  - issues/validates gateway     │
                         │    tokens (superuser + runtime) │
                         │  - receives usage callbacks      │
                         └───────────────┬─────────────────┘
                                         │ gateway_token, correlation_id
                                         │ (via effective AI model config)
                                         ▼
                         ┌───────────────────────────────┐
                         │        pipecat pipeline         │
                         │  DograhLLMService / STT / TTS   │
                         └───────────────┬─────────────────┘
                                         │ HTTPS / WSS, Bearer gateway_token,
                                         │ X-Correlation-Id header
                                         ▼
              ┌───────────────────────────────────────────────────┐
              │                 gateway service (new)                │
              │  ┌───────────────┐ ┌───────────────┐ ┌────────────┐ │
              │  │ token auth /   │ │ usage capture  │ │ upstream   │ │
              │  │ org resolution │ │ (per-request)  │ │ provider   │ │
              │  │                │ │                │ │ clients    │ │
              │  └───────┬────────┘ └───────┬────────┘ └─────┬──────┘ │
              │          └──────────┬────────┴────────┬───────┘        │
              │                     ▼                  ▼               │
              │            secret store (platform  usage sink /       │
              │            provider keys)          async queue         │
              └───────────────────────────┬──────────────────────────┘
                                          │ usage events, correlation_id
                                          ▼
                         ┌───────────────────────────────┐
                         │   api/ usage callback endpoint   │
                         │  → Phase 1 ledger usage trail    │
                         │    (informational in v1)         │
                         └───────────────────────────────┘
                                          │
                                          ▼
                         ┌───────────────────────────────┐
                         │  real upstream providers          │
                         │  OpenAI, Groq, Google, Azure,     │
                         │  Bedrock, OpenRouter, Sarvam       │
                         │  (LLM); Deepgram, Cartesia,        │
                         │  AssemblyAI, Gladia, Speechmatics, │
                         │  Azure, Google, OpenAI (STT);       │
                         │  ElevenLabs, Cartesia, Deepgram,    │
                         │  OpenAI, Google, Sarvam, Minimax,   │
                         │  Rime (TTS)                         │
                         └───────────────────────────────┘
```

### Why a separate service, not a module in `api/`

- **Blast-radius isolation**: a bug or credential leak in the gateway must not expose
  `api/`'s database, session auth, or other internal state, and vice versa. The gateway
  process holds a fundamentally more sensitive secret (every platform provider key) and
  should have the smallest possible surface area.
- **Independent scaling**: the gateway is on the hot path for every audio frame and LLM
  token of every managed call; `api/` is not. They have different scaling and latency
  profiles (the gateway wants many lightweight long-lived websocket connections; `api/`
  is mostly short REST requests).
- **Independent deploy cadence**: provider integrations churn faster (new models, new
  providers, provider outages needing failover) than core billing/workflow logic;
  decoupling deploys reduces blast radius of gateway-only changes.

---

## Correlation-id lifecycle

Mirrors the existing MPS pattern (`get_mps_correlation_id`, `ensure_mps_correlation_id`
in `api/services/managed_model_services.py`) but the mint and the validation both happen
inside our own stack instead of round-tripping to MPS.

```
1. Call authorize      quota_service.authorize_workflow_run_start()
   (api/)              └─ if effective config uses ServiceProviders.DOGRAH and
                            GATEWAY_ENABLED:
                             mint correlation_id = f"run:{workflow_run_id}:{uuid4()}"
                             stash on run context (same context key pattern as
                             MPS_CORRELATION_ID_CONTEXT_KEY, e.g. GATEWAY_CORRELATION_ID_CONTEXT_KEY)
                             pre-register it with the gateway (see below) so a request
                             bearing an unknown correlation_id is rejected

2. Pipeline start      run_pipeline.py builds EffectiveAIModelConfiguration and passes
   (api/)              initial_context (carries correlation_id) into the pipecat worker,
                        same as today's MPS flow.

3. Pipeline services   DograhLLMService / DograhSTTService / DograhTTSService read
   (pipecat)           correlation_id from constructor kwargs (wired by service_factory.py)
                        and attach it to every gateway request as `X-Correlation-Id`
                        (LLM: HTTP header on chat-completions call; STT/TTS: first
                        websocket control frame, since these are long-lived streams).

4. Gateway             validates the token → org, validates the correlation_id was
   (gateway)           pre-registered for that org, and tags every usage event it emits
                        with (org_id, correlation_id, provider, kind, quantity).

5. Usage reporting     gateway pushes usage events to api/'s callback endpoint (batched,
   (gateway → api/)    e.g. on stream completion or every N seconds for long streams);
                        api/ maps correlation_id → workflow_run_id and appends to the
                        run's usage trail (parallel to, not replacing, Phase 1's
                        duration-based debit).
```

**Pre-registration matters**: without it, a stolen gateway token could be replayed with
an arbitrary correlation_id to attribute usage to someone else's run, or requests could
arrive with no run backing them at all. Pre-registering the id (or at minimum validating
it's well-formed and org-scoped) at mint time closes that gap. This is a deliberate
divergence from — and hardening over — the MPS pattern, where the correlation_id is
opaque to the caller.

---

## Auth model

### Gateway tokens
- **Not** the platform provider keys. A gateway token authenticates "this pipeline
  session belongs to org X, dograh-managed mode" — nothing more.
- **Issuance**: two paths, both superuser-gated initially (Phase 4 opens this to
  non-superuser org admins):
  - `POST /superuser/orgs/{org_id}/gateway-tokens` — long-lived token, stored (hashed)
    against the org, used to populate `DograhManagedAIModelConfiguration.api_key`'s
    successor field. Mirrors the existing `mps_service_key_client.create_service_key`
    shape but is issued and validated locally, not via MPS.
  - Short-lived, per-run tokens are **not** introduced in v1 — reusing one org-scoped
    token across a run keeps the design simple; the correlation-id pre-registration step
    is what scopes a given gateway request to a specific run, not the token itself.
- **Validation**: gateway holds a local cache (hash → org_id, revoked flag, expiry),
  refreshed from `api/`'s token store via the same `WorkerSyncManager` Redis pub/sub
  pattern `api/`'s other cross-worker state uses, so a revocation propagates without a
  gateway restart. Token payload never contains a provider key.
- **Storage**: tokens hashed at rest (same treatment as passwords), only the hash is
  persisted; the plaintext is shown once at creation, matching the existing
  `service_key`/`key_prefix` display pattern in `mps_service_key_client.py`.

### Rate limiting / abuse protection
- Per-token request-rate and concurrent-stream caps at the gateway edge (e.g. token
  bucket keyed by org_id), independent of Phase 1's credit checks — this guards the
  gateway's own capacity and the platform's standing with upstream providers, not
  billing correctness.
- Per-provider concurrency caps (e.g. max concurrent OpenAI streams) to protect the
  platform's own upstream rate limits from being exhausted by one runaway org.
- Correlation-id pre-registration (above) doubles as replay/abuse protection: a token
  cannot be used to open gateway streams for correlation ids it didn't mint.

---

## Data flow (call lifecycle)

```
Call start
  └─ authorize_workflow_run_start()  [api/]
       ├─ resolve effective config → Dograh-managed + GATEWAY_ENABLED
       ├─ mint correlation_id, pre-register with gateway
       └─ stash correlation_id + gateway token on run context

Pipeline run  [pipecat, via DograhLLMService/STT/TTS]
  └─ open gateway request(s), Bearer <gateway_token>, X-Correlation-Id: <id>
       ├─ LLM: POST /v1/chat/completions (stream=true) → SSE chunks
       ├─ STT: WSS /v1/stt/stream → binary audio in, JSON transcript frames out
       └─ TTS: WSS /v1/tts/stream → JSON text in, binary audio frames out

Gateway  [new service]
  └─ auth: token → org_id; validate correlation_id belongs to org_id
       └─ proxy to real upstream provider using platform key for that org's
          configured provider/model (config resolved the same way service_factory.py
          resolves it today, just gateway-side)
            ├─ stream response back to caller unmodified (pass-through framing)
            └─ accumulate usage (tokens in/out; audio seconds in/out; chars) per
               request, tag with (org_id, correlation_id, provider, kind, quantity)
                 └─ on stream completion (or periodic flush for long streams):
                    push usage event → api/ usage-callback endpoint

Call complete
  └─ report_workflow_run_platform_usage()  [api/, existing Phase 1 hook]
       └─ debit_for_run() prices by duration × architecture rate, unchanged
       └─ (informational) gateway usage events already appended to the run's
          usage trail via the callback, available for reconciliation/observability
```

---

## Pipecat submodule changes

All changes are scoped to `pipecat/src/pipecat/services/dograh/` plus the call sites in
`api/services/pipecat/service_factory.py`. No other pipecat subsystem is touched.

### `pipecat/src/pipecat/services/dograh/llm.py`
- `__init__` signature keeps `api_key`, `base_url`, `correlation_id`, `settings` — only
  the **meaning** of `api_key` changes (gateway token, not MPS key) and the **default**
  `base_url` changes from `https://services.dograh.com/api/v1/llm` (line 43) to the new
  `{GATEWAY_URL}/v1/llm`, kept OpenAI-compatible so `create_client` — which already
  builds an `AsyncOpenAI`-style client — needs minimal changes.
- `create_client` continues to build an OpenAI-SDK-compatible client pointed at the
  gateway; add the `X-Correlation-Id` default header at construction time instead of
  per-request, since it's static for the life of the service instance.
- Replace the `from pipecat.services.dograh.mps_billing import (...)` block
  (`llm.py:16-21`) with the new `dograh_gateway` helper module (same function shapes:
  `get_correlation_id`, an `attach_correlation_id(client_kwargs, correlation_id)` or
  header-builder helper) so the diff is mechanical.

### `pipecat/src/pipecat/services/dograh/stt.py`
- `base_url` default moves from `wss://services.dograh.com` to `{GATEWAY_URL}` (ws
  scheme derived the same way `service_factory.py` already does today via
  `MPS_API_URL.replace("http://", "ws://")...`, line 193 — same pattern, new source env).
  `ws_path` likely changes from `/api/v1/stt/stream` to the gateway's STT path; keep it
  as a constructor default so it stays overridable.
- Auth: today's Bearer auth pattern for the websocket handshake is preserved; the value
  becomes the gateway token instead of the MPS key.
- Replace `mps_billing` import (`stt.py:30-35`) with `dograh_gateway`, same as LLM.
  `MPS_BILLING_VERSION_KEY`/`uses_mps_billing_v2()` branching is dropped entirely — the
  gateway protocol has one usage-reporting shape, no versioning needed for v1.

### `pipecat/src/pipecat/services/dograh/tts.py`
- Same treatment as STT: `base_url` default, Bearer auth value, `mps_billing` →
  `dograh_gateway` import swap (`tts.py:30-35`).

### `api/services/pipecat/service_factory.py`
- New env `GATEWAY_URL` (analogous to `MPS_API_URL` in `api/constants.py:34`), added to
  `api/constants.py`.
- The three Dograh branches (STT line 192, TTS line 470, LLM line 736) switch their
  `base_url = MPS_API_URL...` derivation to `base_url = GATEWAY_URL...` **only when**
  `GATEWAY_ENABLED` is true; otherwise they keep deriving from `MPS_API_URL` exactly as
  today, so the MPS path is untouched and selectable by flag.
- `api_key=user_config.stt.api_key` (etc.) now carries the gateway token instead of the
  MPS key when the gateway path is selected — sourced from wherever
  `DograhManagedAIModelConfiguration.api_key` is populated (Phase 3/4 concern: how the
  org's gateway token gets into that field is an admin/superuser action, not a schema
  change in this phase).

### What's explicitly *not* rewritten
- `pipecat/src/pipecat/services/dograh/mps_billing.py` stays in the tree (BYOK/legacy
  MPS path still uses it) but is no longer imported by the three Dograh service files
  once they're on the gateway path. It can be deleted once MPS is fully retired
  (out of scope here).
- No other provider service files (`services/openai/`, `services/deepgram/`, etc.)
  change — the gateway is a new upstream target for the `dograh` services only; BYOK
  customers keep talking to providers directly, unaffected.

---

## Streaming passthrough concerns

- **LLM (SSE)**: gateway must forward `data: {...}` chunks with the same cadence/shape
  OpenAI's own streaming API uses, since the pipecat `OpenAILLMService` base class
  (`DograhLLMService` extends it) parses `ChatCompletionChunk` — the gateway response
  must remain byte-for-byte compatible with that shape for providers that aren't
  natively OpenAI-shaped (Groq/Google/Azure/Bedrock/OpenRouter/Sarvam), meaning the
  gateway is responsible for **normalizing** every upstream LLM provider's stream into
  OpenAI chunk format before forwarding.
- **STT/TTS (websocket)**: framing is gateway-defined (JSON control + binary audio),
  which decouples the pipecat client from each upstream provider's idiosyncratic wire
  protocol (Deepgram's frame types differ from Google's differ from Cartesia's). The
  gateway does this normalization once, centrally, instead of it being duplicated across
  N provider-specific pipecat services (which already exist for BYOK) — for the
  Dograh-managed path there is now exactly one wire protocol pipecat needs to speak.
- **Timeouts**: gateway enforces its own idle/read timeouts per stream (e.g. no upstream
  bytes for N seconds → close with an error frame) independent of, and generally
  stricter than, upstream provider timeouts, so a hung upstream can't hang the whole
  pipecat call indefinitely.
- **Upstream failover**: v1 does **not** implement automatic cross-provider failover
  (e.g. Deepgram down → auto-switch to Google STT) — that changes the effective config
  the customer chose and has billing/quality implications outside this phase's scope.
  V1 failover is limited to **same-provider retry** (e.g. one reconnect attempt on a
  dropped websocket) before surfacing an `ErrorFrame` upstream, consistent with the
  existing `push_error(..., fatal=False)` convention pipecat services already follow.

---

## Error handling

- **Upstream provider error** (4xx/5xx from OpenAI/Deepgram/etc.): gateway maps it to a
  structured error response/frame (`{error: {code, message, provider}}`) rather than
  passing the raw upstream body through, so pipecat's `ErrorFrame` handling doesn't need
  per-provider parsing logic.
- **Partial usage on disconnect**: if a websocket (STT/TTS) or SSE stream (LLM) drops
  mid-flight, the gateway flushes whatever partial usage it accumulated so far as a
  usage event before closing — no usage is silently dropped, but also nothing is
  double-counted on reconnect (each stream/connection gets its own usage-event id).
- **Gateway down / unreachable**: pipecat's connection attempt fails fast (short
  connect-timeout) and surfaces a fatal `ErrorFrame`, ending the call gracefully rather
  than hanging. **Fail closed**, not silent fallback to BYOK or MPS mid-call — an org on
  gateway mode has no provider keys configured anywhere else, so there is nothing safe
  to fall back to once a call has started. (Contrast: the *decision* of whether to route
  a new call through the gateway vs. MPS vs. BYOK is made once, before the call starts,
  by config/flag — not a live fallback.)
- **Correlation id not pre-registered / unknown**: gateway rejects the request at
  connection time (401/403-equivalent) — this should only happen on a bug (authorize
  step didn't run) or an abuse attempt, and is logged loudly at the gateway.
- **Usage-callback delivery failure** (gateway → api/): retried with backoff, same shape
  as `mps_service_key_client.report_platform_usage`'s existing retry loop
  (`mps_service_key_client.py:559-626`, `max_attempts=3`); since usage reporting in v1 is
  informational (Phase 1 bills by duration, not gateway usage), a dropped usage event
  does not block or corrupt billing — it only degrades the usage trail's completeness,
  which is acceptable for v1 and revisited if/when usage-based pricing is adopted.

---

## Security

- **Platform keys never leave the gateway process boundary.** They are not present in
  `api/`, not in pipecat, not in any config object serialized to a client or a log line.
  This is the core property this phase buys over MPS-via-vendor: same isolation
  guarantee, but operated by us.
- **Secret storage**: platform provider keys held in a dedicated secret store (e.g. the
  cloud provider's secret manager, or an encrypted-at-rest table the gateway alone can
  read) — never in plaintext env vars checked into any repo or docker-compose file used
  outside local dev.
- **Token scoping**: a gateway token authenticates an org, not a user or a specific run
  — it cannot be used to read another org's usage or impersonate another org's calls.
  Correlation-id pre-registration further scopes a given *stream* to a given *run*.
- **Transport security**: gateway endpoints are TLS-only (`wss://`/`https://`) even in
  the local-dev compose setup where practical, since these carry live customer audio.
- **Least privilege for provider keys**: where a provider supports scoped/rate-limited
  API keys (many do), the platform's keys held by the gateway should be provisioned with
  the narrowest scope needed (no billing-management scope, no account-admin scope) so a
  gateway compromise can't escalate to provider-account takeover.
- **Audit trail**: token issuance/revocation and correlation-id minting are logged with
  actor (superuser) and org, mirroring the existing superuser-route audit expectations.

---

## Testing strategy

Gateway is a new codebase/component; tests split between the gateway repo/package itself
and the `api/`/`pipecat` integration points.

**Unit — gateway usage accounting**
- Per-provider usage extraction: given a captured upstream response/stream, assert the
  parsed usage (tokens in/out for LLM; audio-seconds for STT/TTS; characters for TTS)
  matches expected values for each supported provider's response shape.
- Partial-stream usage: simulate a mid-stream disconnect, assert the flushed usage event
  reflects only what was actually transferred, not the full expected length.
- Correlation-id validation: registered id + matching org → accept; unregistered,
  wrong-org, or malformed id → reject with the structured error.
- Token validation: valid/expired/revoked token → correct auth outcome; revocation
  propagation from the local cache-refresh mechanism.

**Integration — proxy roundtrip (mock upstream)**
- LLM: mock an upstream OpenAI-compatible endpoint, drive a full request through the
  gateway, assert the SSE chunks reaching the caller are well-formed and usage is
  captured correctly. Repeat with a non-OpenAI-shaped mock (e.g. Google-style) to verify
  normalization.
- STT/TTS: mock upstream websocket per provider, drive audio in/text out (or vice
  versa) through the gateway, assert framing correctness and usage capture.
- Auth: end-to-end request with a valid gateway token + registered correlation_id
  succeeds; with a bad token or unregistered id, fails as designed.

**Integration — pipecat client (`pipecat/tests/`)**
- `DograhLLMService`/`STT`/`TTS` against a mock gateway server, verifying the rewritten
  `create_client`/websocket-connect logic produces the expected requests (headers,
  correlation id, base_url resolution) and correctly parses gateway responses into
  pipecat frames — using pipecat's existing `run_test()` harness
  (`src/pipecat/tests/utils.py`) per pipecat's own testing conventions.

**End-to-end (staging)**
- A real outbound/inbound call routed through `GATEWAY_ENABLED=true` against real
  upstream providers (sandboxed/test keys), verifying audio quality, latency budget, and
  that usage events land in `api/`'s callback endpoint and are attributable to the
  correct `workflow_run_id`.

---

## Rollout

1. Ship the gateway service (new deployable), platform-key secret store, token
   issuance/revocation superuser endpoints, and the `dograh_gateway` pipecat helper
   module — all inert until `GATEWAY_ENABLED=true`.
2. Rewrite `pipecat/src/pipecat/services/dograh/{llm,stt,tts}.py` against the new
   contract, gated so the MPS-pointed code path remains reachable when
   `GATEWAY_ENABLED=false` (i.e. the `base_url`/`api_key` source branches on the flag,
   not a hard replacement).
3. Issue a gateway token for one internal/staging org, flip `GATEWAY_ENABLED` for that
   org only (org-level override, mirroring how Phase 1's `BILLING_ENGINE=local` flag is
   expected to roll out per-org before a global flip), and run outbound + inbound test
   calls across each provider family (LLM/STT/TTS) to validate quality and usage capture.
- **4. Compare usage-callback data against the existing duration-based billing** for the
   same calls, to sanity-check the two signals agree in magnitude before usage-based
   pricing is ever considered.
5. Flip `GATEWAY_ENABLED` on for all Dograh-managed orgs; keep the MPS path code intact
   (behind the flag, off) for a rollback window before deleting `mps_billing.py` and the
   MPS-derived `base_url` branches.

---

## Open questions deferred to their phases

- **Usage-based pricing** (charging per-token/char/audio-second instead of per-minute
  architecture rate) — the gateway captures the data needed for this, but wiring it into
  `BillingService.debit_for_run` is a pricing-model decision explicitly deferred; Phase 1
  already bills by duration regardless of what the gateway reports.
- **Self-serve gateway-token management for org admins** (currently superuser-only) →
  **Phase 4** roles/permissions work.
- **Cross-provider automatic failover** (e.g. STT provider outage triggers a switch to a
  different provider mid-deployment) — a product/quality decision, not addressed by this
  phase's same-provider-retry-only approach.
- **Full retirement of MPS and `mps_billing.py`** — depends on this phase proving out in
  production and Phase 3 (Stripe) also moving off MPS's billing-account APIs
  (`mps_service_key_client.py`'s `ensure_billing_account_v2`, `get_credit_ledger`, etc.);
  tracked as a follow-up cleanup once both are live.
- **Gateway multi-region / failover topology** — an operational/infra decision out of
  scope for this design doc.
