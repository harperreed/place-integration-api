# Place async SDK — design

- **Date:** 2026-08-08
- **Status:** approved (design); pending spec review
- **Branch:** `feat/robust-async-sdk`
- **Supersedes:** the synchronous `MqttClient` transport and the import-time-configured `config.py`

## 1. Goal & context

Reshape `place-integration-api` into a robust, async-native SDK that a future Home
Assistant custom component can sit on top of. The SDK is the standalone library half
of the canonical HA split: the integration stays a thin adapter (config_flow,
coordinator, entities) and knows about HA; **this library knows about the wire and
never imports `homeassistant`.**

The device is `cloud_push`: state arrives over AWS IoT MQTT (WebSocket) as shadow
documents plus discrete household events (live `motionDetected`). "Robust" means the
SDK owns its whole lifecycle and self-heals — it reconnects on drop and refreshes
both Cognito tokens and IoT credentials before they expire, without the consumer
orchestrating any of it.

This document is the design contract. The phased TDD implementation plan is produced
separately (writing-plans) from this spec.

## 2. Non-goals

- **No HA custom component.** Deferred until the SDK is proven. The SDK is built to be
  HA-*friendly*, not HA-*coupled*.
- **No backward-compatibility shims.** The sync transport and import-time config are
  replaced outright, not kept alongside. (Explicitly authorized.)
- **No write path to devices.** No publishing of desired shadow state, no device
  commands (hush/test/enable/disable). See §7.
- **No new device capabilities invented.** Field, topic, and event names come only from
  empirical captures already grounded in the models; nothing is fabricated.

## 3. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  PlaceClient         ← the one entry point (async ctx mgr)  │
│  • devices{}   • on_update / on_event   • updates() stream  │
└─────────┬────────────────────────────────────┬─────────────┘
          │                                     │
   ┌──────▼───────┐                     ┌───────▼────────┐
   │ PlaceDevice  │   one source        │  CognitoAuth   │  tokens +
   │ shadow+events│   of truth          │ (AbstractAuth) │  IoT creds
   └──────▲───────┘                     └───────┬────────┘  (refresh)
          │ apply()                             │ fresh creds
   ┌──────┴─────────────────────────────────────▼──────────┐
   │  PlaceConnection   (aiomqtt, self-healing outer loop)  │
   │  reconnect · re-subscribe · refresh-creds-on-cycle     │
   └──────────────────────┬─────────────────────────────────┘
                          │  SigV4 presigned WS URL (reused as-is)
                    AWS IoT  (MQTT / WSS)
```

### Module map

| Disposition | Modules |
|---|---|
| **Kept as-is** | `models/` (device_shadow, device_event, discover_device, credentials, mqtt_message, models); the SigV4 signer logic (moved verbatim); the topic-builder functions in `messages.py` |
| **Reworked** | `config.py` → injectable `PlaceConfig` (+ `from_env()`); `auth/` → add refresh-token flow + in-SDK MFA + expiry-aware caching, async-wrapped |
| **Replaced** | `mqtt_client.py` (sync paho) → `transport.py` (async aiomqtt); the sync-client-bound methods of `PlaceMessages`; `HouseholdEventListener` folded into `PlaceClient` dispatch |
| **New** | `client.py`, `transport.py`, `device.py`, `exceptions.py`, `auth/cognito_auth.py` |

## 4. Components

Each unit has one purpose, a typed interface, and named dependencies. Signatures below
are the intended public API; exact third-party kwargs flagged "(verify)" are pinned at
implementation against the installed library version — not guessed here.

### 4.1 `PlaceConfig` (reworks `config.py`)

A frozen dataclass carrying every knob currently read at import time via `decouple`:
region, cognito user-pool / client / identity-pool ids, IoT endpoint, fulfillment URL,
OAuth2 token URL, and MQTT tunables (`keep_alive_sec`, `creds_expire_sec`,
reconnect backoff bounds).

- Public PLACE constants are **baked in as field defaults** (they already ship publicly
  upstream and are app-level, not home identifiers), so examples construct
  `PlaceConfig()` with zero arguments.
- `PlaceConfig.from_env()` classmethod reads overrides from the environment for script
  convenience. **The library core never reads env implicitly** — it only ever receives a
  `PlaceConfig` object. This kills the import-time coupling that blocked injection.

Depends on: nothing (leaf).

### 4.2 `exceptions.py` (new)

The typed hierarchy that is the integration's contract:

```
PlaceError                     # base
├─ PlaceAuthError              # bad creds / refresh failed → HA ConfigEntryAuthFailed
│  └─ MfaRequired              # carries the challenge/session needed to continue
├─ PlaceConnectionError        # transport down / handshake failed → ConfigEntryNotReady
├─ PlaceDiscoveryError         # DISCOVER failed / empty
└─ PlaceTimeoutError           # an awaited operation exceeded its deadline
```

Depends on: nothing (leaf).

### 4.3 Auth (`auth/`)

**Kept:** `AbstractAuth` (the `async_get_access_token()` + `request()` bearer seam — this
is exactly the standard HA library auth pattern; the future integration subclasses it
against HA's OAuth session). `tokens.decode_sub`. The SRP mechanics in `aws_srp.py` and
the `get_iot_credentials` / SRP-login logic in `srp_auth.py` are **reused**, not rewritten.

**New:** `CognitoAuth(AbstractAuth)` — the concrete, self-refreshing implementation that
orchestrates those existing functions and adds what is currently missing:

- `async authenticate(username, password) -> None` — SRP login via the existing helper,
  wrapped in `asyncio.to_thread` (boto3/SRP are blocking). On an MFA challenge it raises
  `MfaRequired(challenge)` instead of returning.
- `async submit_mfa(challenge, code) -> None` — completes `RespondToAuthChallenge` for
  `SOFTWARE_TOKEN_MFA` / `SMS_MFA` (challenge names are Cognito's, verified against the
  existing probe). Two-step by design: maps cleanly onto both an interactive script and a
  future HA config_flow form.
- `async async_get_access_token() -> str` (override) — returns a cached access token if
  still valid; otherwise refreshes via Cognito `InitiateAuth` `REFRESH_TOKEN_AUTH`
  (`SECRET_HASH` included only if the app client requires one — verify) and re-caches.
- `async async_get_iot_credentials() -> Credentials` — returns cached IoT creds if not
  near expiry; otherwise re-exchanges via the existing `get_iot_credentials` path
  (to_thread) and re-caches with an expiry timestamp (from the credentials' own
  expiration when the exchange provides one, else `creds_expire_sec`).
- Token/credential state (access, id, refresh, expiries, IoT creds) held on the instance;
  all blocking work behind `to_thread`; a refresh lock prevents thundering-herd refreshes.

Depends on: `PlaceConfig`, existing SRP/`get_iot_credentials` functions, `exceptions`,
the injected `aiohttp.ClientSession` (via AbstractAuth).

### 4.4 Transport (`transport.py`, replaces `mqtt_client.py`)

**`get_signed_uri(...)`** — the SigV4 presigned WSS URL builder, moved verbatim from the
sync client (proven, transport-agnostic). Reused unchanged.

**`MqttTransport` (Protocol)** — a tiny seam so domain logic never imports
`aiomqtt.Client` directly: `connect()`, `subscribe(topic)`, `publish(topic, payload)`,
and an async-iterator of `(topic, payload)` messages. Real impl wraps aiomqtt; tests
inject a fake that yields scripted messages and records subscribes/publishes. **This is
the key testability move** — the self-healing/dispatch logic is testable with no broker.

**`PlaceConnection`** — owns the aiomqtt lifecycle as a self-healing outer loop:

- `async run()` — `while not stopped:` → ask `CognitoAuth` for fresh IoT creds → build a
  freshly-signed WSS URL → open the aiomqtt client (`transport="websockets"`,
  websocket path/headers carrying the presigned query + `Host`, TLS on 443 — exact
  kwargs verify) → re-apply the full desired-subscription set → consume messages,
  handing each `(topic, payload)` to a dispatch callback → until `MqttError` **or** a
  proactive deadline computed from creds expiry. On exit: exponential backoff + jitter,
  capped; then loop. `logging`, never `print`.
- `subscribe(topic)` / `unsubscribe(topic)` — maintain a declarative desired set; apply
  immediately if connected, and always re-applied on (re)connect so nothing is lost.
- `publish(topic, payload)` — used only for `shadow/get` reads (§7).
- `connected` property + `on_connection_change(cb)` — so a consumer can mark entities
  unavailable/available.

Depends on: `PlaceConfig`, `CognitoAuth`, `aiomqtt`, `exceptions`.

### 4.5 Domain (`device.py`, new)

**`PlaceDevice`** — the single source of truth per device:

- Identity from `DiscoverDevice`; current `PlaceDeviceShadow`; last event(s); `online`.
- `_apply_shadow(doc)` — merges a shadow document (via existing `PlaceDeviceShadow.merge`).
- `_apply_event(DeviceEvent)` — records a discrete event (live motion, etc.).
- Stamps `last_shadow_at` for every message carrying reported state, even when the
  cached fields do not change. Its own registered listeners remain field-change only.

Reuses `PlaceDeviceShadow`, `DeviceEvent`, `DiscoverDevice`. This is what HA entities
would read; callbacks/iterator are just *views* over it (one source of truth).

Depends on: `models/`.

### 4.6 Facade (`client.py`, new)

**`PlaceClient`** — the single entry point; async context manager.

- Construction: `PlaceClient(config, websession, auth)` for full injection (HA passes its
  own session + auth). Convenience `PlaceClient.create(config, websession, username,
  password)` builds a `CognitoAuth` for scripts.
- `async start()` / `async stop()` / `async with` — `start()` ensures auth, runs
  `Provider.discover()`, builds the `PlaceDevice` registry, launches
  `PlaceConnection.run()` as a background task, subscribes each household events topic +
  each device shadow topic, and issues an initial `shadow/get` per device (read).
- `devices: Mapping[str, PlaceDevice]` — by thing_name/device id.
- `on_update(cb) -> unsubscribe` / `on_event(cb) -> unsubscribe` — register listeners
  (device-state change or reported-shadow liveness reply; discrete event). Returns an
  unsubscribe handle.
- `updates()` — thin async-iterator over the same notifications, for scripts.
- `async async_refresh_shadow(device)` — force a `shadow/get` read.
- `connected` + `on_connection_change(cb)` — surfaced from `PlaceConnection`.
- **Dispatch** (the folded-in `HouseholdEventListener` logic): route each incoming
  `(topic, payload)` — shadow docs → `device._apply_shadow`; events → `DeviceEvent` →
  `device._apply_event` — then fire listeners. Every valid shadow message carrying
  reported state emits a client update, including value-identical replies; empty MQTT
  wildcard echoes neither stamp liveness nor emit.

Depends on: everything above + `Provider`.

## 5. Data flow

**Startup (`start`)**: auth → `discover()` → build registry → start connection task →
subscribe(household events, each device shadow) → `shadow/get` per device.

**Steady state**: message arrives on the single aiomqtt stream → dispatch classifies by
topic → `PlaceDevice.apply_*` updates the one source of truth → client listeners fire
for field changes, events, and reported-shadow liveness replies → consumer reads
`client.devices[id]` (or receives the callback / iterator item).

**Reconnect / refresh**: connection drops or hits the proactive creds deadline → outer
loop backs off → re-acquires fresh IoT creds (refreshing the Cognito token first if
near expiry) → re-signs → reconnects → re-applies the full subscription set. Consumers
see an `on_connection_change(False→True)` transition; no state is lost.

## 6. Error handling & consumer mapping

- `authenticate()` bad creds → `PlaceAuthError`; MFA needed → `MfaRequired` (consumer
  prompts, calls `submit_mfa`). Future integration maps `PlaceAuthError` →
  `ConfigEntryAuthFailed` (reauth).
- `discover()` failure/empty → `PlaceDiscoveryError`.
- Transport never up within setup window → `PlaceConnectionError` →
  `ConfigEntryNotReady`. After startup, drops are handled *inside* the self-healing loop
  and surfaced only as connection-state transitions, never as raised exceptions to the
  consumer.
- Awaited one-shot ops (e.g. a bounded `shadow/get` wait) exceeding their deadline →
  `PlaceTimeoutError`.

## 7. Read-only security posture (must hold)

This is an authorized read-only audit SDK. Invariants:

- **No desired-state writes.** `desired_shadow_update()` stays a pure `(topic, payload)`
  *builder*; nothing in the SDK publishes it. No `publish_shadow_update` method exists.
- **No device commands** (hush/test/enable/disable) wired into the client. The pre-existing
  `Provider.enable()/disable()` fulfillment directives are left untouched and deliberately
  **not** surfaced on `PlaceClient`.
- **Only reads on the wire**: `shadow/get` publishes (a read trigger) and household
  SUBSCRIBEs. Nothing else is published.
- **No real home identifiers** in committed code/tests/examples — synthetic IDs only.
- Credentials `.env` is never read or logged by the SDK; MFA codes are passed at runtime,
  never persisted.

## 8. Testing strategy (TDD)

Iron law: no production code without a failing test first. Fakes match the repo's existing
convention (fake auth/`DummyAuth`, fakes that skip `super().__init__()` like
`test_provider.py`).

- **Config**: defaults present; `from_env()` overrides; immutability.
- **Exceptions**: hierarchy (each subclass is a `PlaceError`; `MfaRequired` is a
  `PlaceAuthError` and carries its challenge).
- **Auth**: token-cache hit vs. refresh-on-expiry; `REFRESH_TOKEN_AUTH` path; MFA
  challenge → `MfaRequired` → `submit_mfa` success; IoT-cred cache/refresh. Cognito/boto3
  calls behind a seam (a small gateway we can fake) with realistic response shapes — no
  mocks-of-mocks.
- **Transport**: drive `PlaceConnection` with a fake `MqttTransport` — assert
  re-subscribe on reconnect, backoff growth + cap, proactive refresh triggers a
  cred re-fetch, dispatch routes each topic class correctly.
- **Device**: shadow merge + event apply + listener notification (absent fields stay
  `None`, not invented).
- **Client**: startup wiring (discover → subscribe set → initial `shadow/get`),
  `on_update`/`on_event`/`updates()` delivery for field changes and reported-shadow
  liveness replies, silent empty MQTT echoes, unsubscribe handles, connection-state
  propagation.
- **End-to-end stays manual**: the real-device example scripts exercise the live account
  (real data, no mocks) — not unit tests, consistent with the current repo.

## 9. Example scripts (`examples/`, async, read-only)

All built on `PlaceClient`, creds via `PlaceConfig.from_env()`:

- `quickstart.py` — authenticate (prompt for MFA if raised) → `async with PlaceClient` →
  print discovered devices, then stream live motion/telemetry as it arrives.
- `shadow_snapshot.py` — one-shot: start, `shadow/get`, print each device's shadow, exit.
- `watch_live.py` — subscribe and stream discrete events (walk past a unit, see motion).

These replace the current `probe_real_device.py` / `watch_events.py` throwaways, now far
smaller because the facade owns the wiring.

## 10. Packaging & hygiene

- Add `aiomqtt` dependency (pin the version we validate the WS/TLS kwargs against). Keep
  `aiohttp`, `boto3`, `PyJWT`. `python-decouple` becomes used only by `from_env()`.
- Ship a `py.typed` marker (PEP 561) so consumers get types.
- `logging.getLogger(__name__)` throughout; no `print` in library code.
- `pytest-asyncio` is already configured (`asyncio_mode=auto`).

## 11. Open questions / risks (resolve at implementation, don't guess)

1. **aiomqtt WS kwargs** — exact names for websocket path/headers + TLS (`tls_params` vs
   `tls_context`) and client id (`identifier` vs `client_id`) vary by aiomqtt version.
   Pin against the installed version; validate the SigV4-presigned path actually connects.
2. **aiomqtt reconnect vs. our loop** — confirm the library does not double-reconnect
   under our outer loop (we own reconnection; disable/avoid any internal auto-reconnect).
3. **Cognito refresh `SECRET_HASH`** — needed only if the app client has a secret;
   determine from the existing client config, don't assume.
4. **MFA wiring** — confirm how the existing `aws_srp` helper surfaces the challenge
   (return value vs. exception) so `authenticate()` raises `MfaRequired` faithfully.
5. **Committed-test churn** — folding `HouseholdEventListener` into the client means its
   tests move to client-dispatch tests; update them rather than delete coverage.

## 12. Rough size

~1.5–2k LoC net: core ~800 (client/transport/device/cognito_auth/exceptions/config),
examples ~250, tests ~600+.
