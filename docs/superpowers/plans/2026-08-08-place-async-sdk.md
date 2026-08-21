# Place Async SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape `place-integration-api` into a robust, async-native SDK — a self-healing `PlaceClient` facade over AWS IoT MQTT — that a future Home Assistant integration can consume.

**Architecture:** A layered async library: injectable `PlaceConfig` → self-refreshing `CognitoAuth` (SRP + MFA + token/IoT-cred refresh) → self-healing `PlaceConnection` (aiomqtt behind an `MqttTransport` seam) → stateful `PlaceDevice` registry (one source of truth) → `PlaceClient` facade (callbacks + async-iterator). The library never imports Home Assistant; it raises a typed error taxonomy the integration maps to HA's.

**Tech Stack:** Python ≥3.11, asyncio, aiohttp, aiomqtt (new), boto3 (Cognito SRP/identity, wrapped in `asyncio.to_thread`), PyJWT, pytest + pytest-asyncio (`asyncio_mode=auto`), basedpyright.

**Design spec:** `docs/superpowers/specs/2026-08-08-place-async-sdk-design.md` (approved).

## Global Constraints

Every task's requirements implicitly include this section.

- **Python floor:** `requires-python >=3.11`. Use `X | None`, `list[...]`, `from __future__ import annotations`.
- **Read-only posture (hard invariant):** the SDK publishes only `shadow/get` (a read trigger) and SUBSCRIBEs. It NEVER publishes desired shadow state and NEVER sends device commands. `desired_shadow_update()` stays a pure `(topic, payload)` *builder* — nothing calls it. `Provider.enable()/disable()` are left untouched and are NOT surfaced on `PlaceClient`.
- **No real home identifiers** in committed code/tests/examples. Synthetic only: household `aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee`, device-uuid `11111111-2222-4333-8444-555555555555`, device-id `Place_PL1AS_EXAMPLE`.
- **Never `import homeassistant`** anywhere in `src/place/`.
- **`logging.getLogger(__name__)`**, never `print()`, in library code (examples may print).
- **We own reconnection.** aiomqtt's internal auto-reconnect must be off; the outer loop is the only reconnection path.
- **Fixed AWS SigV4 protocol strings** (`AWS4-HMAC-SHA256`, `iotdevicegateway`, `wss`, `/mqtt`) are protocol facts, not configuration — they stay module constants; only account/endpoint/tunable values live in `PlaceConfig`.
- **TDD:** write the test, watch it fail for the right reason, then minimal code. Fakes match the repo convention (`DummyAuth` subclass that skips `super().__init__()`, as in `tests/test_provider.py`). No mocks of mocks.
- **Git:** conventional commits; `git status` before staging; stage explicit paths (never `git add -A`; `uv.lock` stays untracked); never `--no-verify`.
- **Commands:** tests `uv run pytest <path> -v`; types `uv run basedpyright`.

---

## File Structure

**New files**
- `src/place/config.py` — REWORK: add frozen `PlaceConfig` dataclass + `from_env()` (keeps existing module constants as the default source).
- `src/place/exceptions.py` — typed error taxonomy.
- `src/place/auth/cognito_gateway.py` — `CognitoGateway` protocol + `RealCognitoGateway` (wraps blocking boto3/SRP calls; the seam tests fake).
- `src/place/auth/cognito_auth.py` — `CognitoAuth(AbstractAuth)`: authenticate/MFA/refresh/IoT-cred caching.
- `src/place/transport.py` — `get_signed_uri` (moved from `mqtt_client.py`), `MqttTransport` protocol, `AiomqttTransport`, `PlaceConnection` (self-healing loop).
- `src/place/device.py` — `PlaceDevice` stateful model + listeners.
- `src/place/client.py` — `PlaceClient` facade.
- `src/place/py.typed` — PEP 561 marker (empty file).
- Tests: `tests/test_config.py`, `test_exceptions.py`, `test_cognito_gateway.py`, `test_cognito_auth.py`, `test_transport_signing.py`, `test_place_connection.py`, `test_device.py`, `test_client.py`.
- Examples: `examples/quickstart.py`, `examples/shadow_snapshot.py`, `examples/watch_live.py`.

**Modified files**
- `src/place/models/credentials.py` — add `expiration: datetime | None`.
- `src/place/auth/srp_auth.py` — parameterize `get_iot_credentials` (inject client + config values; capture `Expiration`); add `refresh_tokens` + `respond_mfa` helpers.
- `src/place/models/__init__.py` — no change needed (Credentials still exported).
- `src/place/messages.py` — drop the `MqttClient`-bound `PlaceMessages` methods; keep topic-builder functions + `parse_payload` + `thing_name_from_topic`.
- `pyproject.toml` — add `aiomqtt`; drop direct `paho-mqtt` (pulled transitively by aiomqtt); ensure `py.typed` shipped.

**Deleted files (Task 18, after new code works)**
- `src/place/mqtt_client.py` (sync paho transport — `get_signed_uri` already moved).
- `src/place/events.py` + `tests/test_events.py` (`HouseholdEventListener` folds into `PlaceClient` dispatch).
- `examples/probe_real_device.py`, `examples/watch_events.py`, `examples/shadow_demo.py` (replaced by the three new examples).

---

## Phase 0 — Foundations

### Task 1: `PlaceConfig` (injectable config)

**Files:**
- Modify: `src/place/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing (leaf).
- Produces: `PlaceConfig` frozen dataclass with fields `region, iot_endpoint, cognito_user_pool_id, cognito_client_id, cognito_identity_pool_id, fulfillment_url, oauth2_token_url, keep_alive_sec: int, url_expire_sec: int, reconnect_min_sec: float, reconnect_max_sec: float, token_refresh_margin_sec: int, creds_refresh_margin_sec: int`; classmethod `PlaceConfig.from_env() -> PlaceConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import dataclasses

import pytest

from place.config import PlaceConfig


def test_defaults_are_the_known_public_constants() -> None:
    cfg = PlaceConfig()
    assert cfg.region == "us-east-2"
    assert cfg.iot_endpoint == "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
    assert cfg.cognito_user_pool_id == "us-east-2_LKSPO9tT6"
    assert cfg.cognito_client_id == "5blr1qf2evvj4ivircqbpqikev"
    assert cfg.cognito_identity_pool_id == "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1"
    assert cfg.keep_alive_sec == 30
    assert cfg.url_expire_sec == 86400


def test_config_is_frozen() -> None:
    cfg = PlaceConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.region = "eu-west-1"  # type: ignore[misc]


def test_from_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_KEEP_ALIVE_SEC", "45")
    cfg = PlaceConfig.from_env()
    assert cfg.region == "eu-west-1"
    assert cfg.keep_alive_sec == 45
    # Untouched values fall back to defaults.
    assert cfg.cognito_client_id == "5blr1qf2evvj4ivircqbpqikev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlaceConfig'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/place/config.py` (keep the existing module-level constants — they are the default source):

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlaceConfig:
    """Injectable Place configuration. Defaults are the public PLACE app constants."""

    region: str = REGION
    iot_endpoint: str = IOT_ENDPOINT
    cognito_user_pool_id: str = COGNITO_USER_POOL_ID
    cognito_client_id: str = COGNITO_CLIENT_ID
    cognito_identity_pool_id: str = COGNITO_IDENTITY_POOL_ID
    fulfillment_url: str = FULFILLMENT_URL
    oauth2_token_url: str = OAUTH2_TOKEN_URL
    keep_alive_sec: int = KEEP_ALIVE_SEC
    url_expire_sec: int = EXPIRE_SEC
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    token_refresh_margin_sec: int = 300
    creds_refresh_margin_sec: int = 600

    @classmethod
    def from_env(cls) -> "PlaceConfig":
        """Build a config, letting environment variables override defaults."""
        return cls(
            region=decouple.config("AWS_REGION", default=REGION),
            iot_endpoint=decouple.config("AWS_IOT_ENDPOINT", default=IOT_ENDPOINT),
            cognito_user_pool_id=decouple.config(
                "AWS_COGNITO_USER_POOL_ID", default=COGNITO_USER_POOL_ID
            ),
            cognito_client_id=decouple.config(
                "AWS_COGNITO_CLIENT_ID", default=COGNITO_CLIENT_ID
            ),
            cognito_identity_pool_id=decouple.config(
                "AWS_COGNITO_IDENTITY_POOL_ID", default=COGNITO_IDENTITY_POOL_ID
            ),
            fulfillment_url=decouple.config("AWS_FULFILLMENT_URL", default=FULFILLMENT_URL),
            oauth2_token_url=decouple.config("OAUTH2_TOKEN_URL", default=OAUTH2_TOKEN_URL),
            keep_alive_sec=decouple.config("AWS_KEEP_ALIVE_SEC", default=KEEP_ALIVE_SEC, cast=int),
            url_expire_sec=decouple.config("AWS_EXPIRE_SEC", default=EXPIRE_SEC, cast=int),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/config.py tests/test_config.py
git commit -m "feat: add injectable PlaceConfig with env overrides"
```

---

### Task 2: Error taxonomy

**Files:**
- Create: `src/place/exceptions.py`
- Test: `tests/test_exceptions.py`

**Interfaces:**
- Consumes: nothing (leaf).
- Produces: `PlaceError`, `PlaceAuthError(PlaceError)`, `MfaRequired(PlaceAuthError)` with `__init__(self, *, challenge_name: str, session: str, username: str)` storing those three attrs, `PlaceConnectionError(PlaceError)`, `PlaceDiscoveryError(PlaceError)`, `PlaceTimeoutError(PlaceError)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exceptions.py
from place.exceptions import (
    MfaRequired,
    PlaceAuthError,
    PlaceConnectionError,
    PlaceDiscoveryError,
    PlaceError,
    PlaceTimeoutError,
)


def test_hierarchy() -> None:
    for exc in (
        PlaceAuthError,
        PlaceConnectionError,
        PlaceDiscoveryError,
        PlaceTimeoutError,
    ):
        assert issubclass(exc, PlaceError)
    assert issubclass(MfaRequired, PlaceAuthError)


def test_mfa_required_carries_challenge() -> None:
    exc = MfaRequired(
        challenge_name="SOFTWARE_TOKEN_MFA", session="sess-123", username="alice"
    )
    assert exc.challenge_name == "SOFTWARE_TOKEN_MFA"
    assert exc.session == "sess-123"
    assert exc.username == "alice"
    assert isinstance(exc, PlaceError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exceptions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'place.exceptions'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/place/exceptions.py
# ABOUTME: The SDK's typed error taxonomy — the contract a consumer (e.g. a Home
# ABOUTME: Assistant integration) catches to drive reauth, retry, and MFA prompts.
from __future__ import annotations


class PlaceError(Exception):
    """Base class for every error this SDK raises."""


class PlaceAuthError(PlaceError):
    """Authentication or token refresh failed."""


class MfaRequired(PlaceAuthError):
    """Login needs a second factor before it can complete."""

    def __init__(self, *, challenge_name: str, session: str, username: str) -> None:
        super().__init__(f"MFA required: {challenge_name}")
        self.challenge_name = challenge_name
        self.session = session
        self.username = username


class PlaceConnectionError(PlaceError):
    """The MQTT transport could not connect or stay connected."""


class PlaceDiscoveryError(PlaceError):
    """Device discovery failed or returned nothing usable."""


class PlaceTimeoutError(PlaceError):
    """An awaited operation exceeded its deadline."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_exceptions.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/exceptions.py tests/test_exceptions.py
git commit -m "feat: add typed error taxonomy"
```

---

## Phase 1 — Auth

### Task 3: Capture IoT credential expiry

**Files:**
- Modify: `src/place/models/credentials.py`
- Modify: `src/place/auth/srp_auth.py` (`get_iot_credentials` — add an injectable client + config kwargs, capture `Expiration`)
- Test: `tests/test_cognito_gateway.py` (first test; file grows in Task 4)

**Interfaces:**
- Consumes: `Credentials` (existing).
- Produces: `Credentials.expiration: datetime | None = None`; `get_iot_credentials(id_token, access_token, *, region=REGION, user_pool_id=COGNITO_USER_POOL_ID, identity_pool_id=COGNITO_IDENTITY_POOL_ID, identity_client=None) -> Credentials` populating `expiration` from the identity response.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cognito_gateway.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from place.auth.srp_auth import get_iot_credentials


class _FakeIdentityClient:
    """Stands in for a boto3 cognito-identity client (records calls, returns canned creds)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_id(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_id", kwargs))
        return {"IdentityId": "id-region:abc"}

    def get_credentials_for_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_credentials_for_identity", kwargs))
        return {
            "Credentials": {
                "AccessKeyId": "AKIA",
                "SecretKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime(2030, 1, 1, tzinfo=timezone.utc),
            }
        }


def test_get_iot_credentials_maps_fields_and_expiration() -> None:
    client = _FakeIdentityClient()
    creds = get_iot_credentials("id-token", "access-token", identity_client=client)

    assert creds.access_key_id == "AKIA"
    assert creds.secret_access_key == "secret"
    assert creds.session_token == "token"
    assert creds.identity_id == "id-region:abc"
    assert creds.access_token == "access-token"
    assert creds.expiration == datetime(2030, 1, 1, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cognito_gateway.py -v`
Expected: FAIL — `TypeError: get_iot_credentials() got an unexpected keyword argument 'identity_client'`.

- [ ] **Step 3: Write minimal implementation**

Add the field to `src/place/models/credentials.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    identity_id: str
    access_token: str | None = None
    expiration: datetime | None = None
```

Replace `get_iot_credentials` in `src/place/auth/srp_auth.py` (add the kwargs + capture `Expiration`; body otherwise unchanged):

```python
def get_iot_credentials(
    id_token: str,
    access_token: str,
    *,
    region: str = REGION,
    user_pool_id: str = COGNITO_USER_POOL_ID,
    identity_pool_id: str = COGNITO_IDENTITY_POOL_ID,
    identity_client: Any | None = None,
) -> Credentials:
    """Exchange a Cognito ID token for AWS IoT credentials."""
    identity = identity_client or boto3.client(
        "cognito-identity",
        region_name=region,
        config=Config(signature_version=UNSIGNED),
    )
    provider_key = f"cognito-idp.{region}.amazonaws.com/{user_pool_id}"
    logins = {provider_key: id_token}

    identity_id = identity.get_id(
        IdentityPoolId=identity_pool_id, Logins=logins
    )["IdentityId"]

    creds = identity.get_credentials_for_identity(
        IdentityId=identity_id, Logins=logins
    )["Credentials"]

    return Credentials(
        access_key_id=creds["AccessKeyId"],
        secret_access_key=creds["SecretKey"],
        session_token=creds["SessionToken"],
        identity_id=identity_id,
        access_token=access_token,
        expiration=creds.get("Expiration"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cognito_gateway.py -v`
Expected: PASS (1 test). Also run `uv run pytest tests/ -v` — the existing suite still passes.

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/models/credentials.py src/place/auth/srp_auth.py tests/test_cognito_gateway.py
git commit -m "feat: capture IoT credential expiration and inject identity client"
```

---

### Task 4: Cognito gateway (refresh + MFA blocking calls)

**Files:**
- Create: `src/place/auth/cognito_gateway.py`
- Modify: `src/place/auth/srp_auth.py` (add `refresh_tokens` + `respond_mfa`)
- Test: `tests/test_cognito_gateway.py` (extend)

**Interfaces:**
- Consumes: `PlaceConfig`; `get_tokens_via_srp`, `get_iot_credentials`, `refresh_tokens`, `respond_mfa` (srp_auth); `Credentials`.
- Produces:
  - `refresh_tokens(refresh_token, *, region, client_id, cognito_idp_client=None) -> dict[str, Any]` (returns the `AuthenticationResult` dict).
  - `respond_mfa(*, challenge_name, session, username, code, region, client_id, cognito_idp_client=None) -> dict[str, Any]` (returns the full `respond_to_auth_challenge` response).
  - `CognitoGateway` Protocol with `srp_login(username, password) -> dict`, `refresh(refresh_token) -> dict`, `respond_mfa(*, challenge_name, session, username, code) -> dict`, `iot_credentials(id_token, access_token) -> Credentials`.
  - `RealCognitoGateway(config: PlaceConfig)` implementing it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cognito_gateway.py  (append)
from place.auth.srp_auth import refresh_tokens, respond_mfa


class _FakeIdpClient:
    def __init__(self, initiate=None, respond=None) -> None:
        self._initiate = initiate or {}
        self._respond = respond or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("initiate_auth", kwargs))
        return self._initiate

    def respond_to_auth_challenge(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("respond_to_auth_challenge", kwargs))
        return self._respond


def test_refresh_tokens_uses_refresh_token_auth_flow() -> None:
    client = _FakeIdpClient(initiate={"AuthenticationResult": {"AccessToken": "new-a"}})
    result = refresh_tokens(
        "refresh-abc", region="us-east-2", client_id="cid", cognito_idp_client=client
    )
    assert result == {"AccessToken": "new-a"}
    name, kwargs = client.calls[0]
    assert name == "initiate_auth"
    assert kwargs["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert kwargs["AuthParameters"] == {"REFRESH_TOKEN": "refresh-abc"}
    assert kwargs["ClientId"] == "cid"
    assert "SECRET_HASH" not in kwargs["AuthParameters"]  # this app client has no secret


def test_respond_mfa_uses_software_token_code_key() -> None:
    client = _FakeIdpClient(respond={"AuthenticationResult": {"AccessToken": "a"}})
    result = respond_mfa(
        challenge_name="SOFTWARE_TOKEN_MFA",
        session="sess",
        username="alice",
        code="123456",
        region="us-east-2",
        client_id="cid",
        cognito_idp_client=client,
    )
    name, kwargs = client.calls[0]
    assert name == "respond_to_auth_challenge"
    assert kwargs["ChallengeName"] == "SOFTWARE_TOKEN_MFA"
    assert kwargs["Session"] == "sess"
    assert kwargs["ChallengeResponses"] == {
        "USERNAME": "alice",
        "SOFTWARE_TOKEN_MFA_CODE": "123456",
    }
    assert result["AuthenticationResult"] == {"AccessToken": "a"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cognito_gateway.py -v`
Expected: FAIL — `ImportError: cannot import name 'refresh_tokens'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/place/auth/srp_auth.py`:

```python
def refresh_tokens(
    refresh_token: str,
    *,
    region: str = REGION,
    client_id: str = COGNITO_CLIENT_ID,
    cognito_idp_client: Any | None = None,
) -> Dict[str, Any]:
    """Exchange a refresh token for fresh access + id tokens (no client secret)."""
    client = cognito_idp_client or boto3.client(
        "cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED)
    )
    resp = client.initiate_auth(
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": refresh_token},
        ClientId=client_id,
    )
    return resp["AuthenticationResult"]


def respond_mfa(
    *,
    challenge_name: str,
    session: str,
    username: str,
    code: str,
    region: str = REGION,
    client_id: str = COGNITO_CLIENT_ID,
    cognito_idp_client: Any | None = None,
) -> Dict[str, Any]:
    """Answer an MFA challenge with the user's one-time code."""
    client = cognito_idp_client or boto3.client(
        "cognito-idp", region_name=region, config=Config(signature_version=UNSIGNED)
    )
    code_key = "SMS_MFA_CODE" if challenge_name == "SMS_MFA" else "SOFTWARE_TOKEN_MFA_CODE"
    return client.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName=challenge_name,
        Session=session,
        ChallengeResponses={"USERNAME": username, code_key: code},
    )
```

Create `src/place/auth/cognito_gateway.py`:

```python
# ABOUTME: The blocking-boto3 seam behind CognitoAuth — SRP login, token refresh, MFA,
# ABOUTME: and IoT-credential exchange, isolated so async auth logic can be faked in tests.
from __future__ import annotations

from typing import Any, Protocol

from ..config import PlaceConfig
from ..models import Credentials
from . import srp_auth


class CognitoGateway(Protocol):
    """Synchronous Cognito operations CognitoAuth drives via asyncio.to_thread."""

    def srp_login(self, username: str, password: str) -> dict[str, Any]: ...
    def refresh(self, refresh_token: str) -> dict[str, Any]: ...
    def respond_mfa(
        self, *, challenge_name: str, session: str, username: str, code: str
    ) -> dict[str, Any]: ...
    def iot_credentials(self, id_token: str, access_token: str) -> Credentials: ...


class RealCognitoGateway:
    """CognitoGateway backed by boto3/SRP, parameterized by PlaceConfig."""

    def __init__(self, config: PlaceConfig) -> None:
        self._config = config

    def srp_login(self, username: str, password: str) -> dict[str, Any]:
        return srp_auth.get_tokens_via_srp(
            user_pool_id=self._config.cognito_user_pool_id,
            client_id=self._config.cognito_client_id,
            username=username,
            password=password,
            region=self._config.region,
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return srp_auth.refresh_tokens(
            refresh_token,
            region=self._config.region,
            client_id=self._config.cognito_client_id,
        )

    def respond_mfa(
        self, *, challenge_name: str, session: str, username: str, code: str
    ) -> dict[str, Any]:
        return srp_auth.respond_mfa(
            challenge_name=challenge_name,
            session=session,
            username=username,
            code=code,
            region=self._config.region,
            client_id=self._config.cognito_client_id,
        )

    def iot_credentials(self, id_token: str, access_token: str) -> Credentials:
        return srp_auth.get_iot_credentials(
            id_token,
            access_token,
            region=self._config.region,
            user_pool_id=self._config.cognito_user_pool_id,
            identity_pool_id=self._config.cognito_identity_pool_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cognito_gateway.py -v`
Expected: PASS (3 tests total in file).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/auth/srp_auth.py src/place/auth/cognito_gateway.py tests/test_cognito_gateway.py
git commit -m "feat: add Cognito refresh/MFA calls and a fakeable gateway"
```

---

### Task 5: `CognitoAuth` — authenticate + MFA

**Files:**
- Create: `src/place/auth/cognito_auth.py`
- Test: `tests/test_cognito_auth.py`

**Interfaces:**
- Consumes: `AbstractAuth`, `PlaceConfig`, `CognitoGateway`, `MfaRequired`, `PlaceAuthError`, `aiohttp.ClientSession`.
- Produces: `CognitoAuth(config, websession, gateway=None)`; `async authenticate(username, password) -> None`; `async submit_mfa(code) -> None`. Stores tokens on the instance: `_access_token, _id_token, _refresh_token, _access_token_expiry`. On MFA challenge raises `MfaRequired` and stashes `_mfa_challenge/_mfa_session/_username`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cognito_auth.py
from __future__ import annotations

from typing import Any

import pytest

from place.auth.cognito_auth import CognitoAuth
from place.config import PlaceConfig
from place.exceptions import MfaRequired


class FakeGateway:
    """In-memory CognitoGateway: scripted login/refresh/mfa, records calls."""

    def __init__(self, *, login=None, mfa=None, refresh=None, creds=None) -> None:
        self._login = login or {}
        self._mfa = mfa or {}
        self._refresh = refresh or {}
        self._creds = creds
        self.refresh_calls = 0

    def srp_login(self, username: str, password: str) -> dict[str, Any]:
        return self._login

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.refresh_calls += 1
        return self._refresh

    def respond_mfa(self, *, challenge_name, session, username, code) -> dict[str, Any]:
        return self._mfa

    def iot_credentials(self, id_token: str, access_token: str):
        return self._creds


def _auth_result(**over: Any) -> dict[str, Any]:
    base = {
        "AccessToken": "access-1",
        "IdToken": "id-1",
        "RefreshToken": "refresh-1",
        "ExpiresIn": 3600,
    }
    base.update(over)
    return base


async def test_authenticate_stores_tokens() -> None:
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]

    await auth.authenticate("alice", "pw")

    assert await auth.async_get_access_token() == "access-1"


async def test_authenticate_raises_mfa_required() -> None:
    gw = FakeGateway(login={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-9"})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]

    with pytest.raises(MfaRequired) as excinfo:
        await auth.authenticate("alice", "pw")
    assert excinfo.value.challenge_name == "SOFTWARE_TOKEN_MFA"
    assert excinfo.value.session == "sess-9"


async def test_submit_mfa_completes_login() -> None:
    gw = FakeGateway(
        login={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-9"},
        mfa={"AuthenticationResult": _auth_result(AccessToken="access-mfa")},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]

    with pytest.raises(MfaRequired):
        await auth.authenticate("alice", "pw")
    await auth.submit_mfa("123456")

    assert await auth.async_get_access_token() == "access-mfa"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cognito_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'place.auth.cognito_auth'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/place/auth/cognito_auth.py
# ABOUTME: CognitoAuth — the self-refreshing AbstractAuth for Place: SRP login, MFA,
# ABOUTME: access-token refresh, and IoT-credential caching, all async via to_thread.
from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp import ClientSession

from ..config import PlaceConfig
from ..exceptions import MfaRequired, PlaceAuthError
from .abstract_auth import AbstractAuth
from .cognito_gateway import CognitoGateway, RealCognitoGateway


class CognitoAuth(AbstractAuth):
    """Concrete AbstractAuth backed by Cognito SRP with self-refresh and MFA."""

    def __init__(
        self,
        config: PlaceConfig,
        websession: ClientSession,
        gateway: CognitoGateway | None = None,
    ) -> None:
        super().__init__(websession)
        self._config = config
        self._gateway: CognitoGateway = gateway or RealCognitoGateway(config)
        self._username: str | None = None
        self._access_token: str | None = None
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._access_token_expiry: float = 0.0
        self._mfa_challenge: str | None = None
        self._mfa_session: str | None = None
        self._refresh_lock = asyncio.Lock()

    async def authenticate(self, username: str, password: str) -> None:
        self._username = username
        result = await asyncio.to_thread(self._gateway.srp_login, username, password)
        self._consume_auth_response(result)

    async def submit_mfa(self, code: str) -> None:
        if self._mfa_challenge is None or self._mfa_session is None:
            raise PlaceAuthError("no MFA challenge pending")
        result = await asyncio.to_thread(
            self._gateway.respond_mfa,
            challenge_name=self._mfa_challenge,
            session=self._mfa_session,
            username=self._username or "",
            code=code,
        )
        self._consume_auth_response(result)

    async def async_get_access_token(self) -> str:
        if self._access_token is None:
            raise PlaceAuthError("not authenticated; call authenticate() first")
        return self._access_token

    def _consume_auth_response(self, result: dict[str, Any]) -> None:
        challenge = result.get("ChallengeName")
        if challenge in ("SOFTWARE_TOKEN_MFA", "SMS_MFA"):
            self._mfa_challenge = challenge
            self._mfa_session = result["Session"]
            raise MfaRequired(
                challenge_name=challenge,
                session=result["Session"],
                username=self._username or "",
            )
        self._store_tokens(result["AuthenticationResult"])
        self._mfa_challenge = None
        self._mfa_session = None

    def _store_tokens(self, auth: dict[str, Any]) -> None:
        self._access_token = auth["AccessToken"]
        self._id_token = auth["IdToken"]
        if auth.get("RefreshToken"):
            self._refresh_token = auth["RefreshToken"]
        self._access_token_expiry = time.time() + float(auth.get("ExpiresIn", 3600))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cognito_auth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/auth/cognito_auth.py tests/test_cognito_auth.py
git commit -m "feat: add CognitoAuth login and MFA handling"
```

---

### Task 6: `CognitoAuth.async_get_access_token` — refresh on expiry

**Files:**
- Modify: `src/place/auth/cognito_auth.py`
- Test: `tests/test_cognito_auth.py` (extend)

**Interfaces:**
- Consumes: Task 5 state + `gateway.refresh`.
- Produces: `async_get_access_token()` returns the cached token while valid, else refreshes via `gateway.refresh` under `_refresh_lock` (single-flight) and re-stores.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cognito_auth.py  (append)
import asyncio


async def test_access_token_refreshes_when_expired() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()},
        refresh={"AccessToken": "access-2", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]
    await auth.authenticate("alice", "pw")

    auth._access_token_expiry = 0.0  # force staleness
    assert await auth.async_get_access_token() == "access-2"
    assert gw.refresh_calls == 1


async def test_concurrent_refresh_is_single_flight() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()},
        refresh={"AccessToken": "access-2", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]
    await auth.authenticate("alice", "pw")
    auth._access_token_expiry = 0.0

    a, b = await asyncio.gather(
        auth.async_get_access_token(), auth.async_get_access_token()
    )
    assert a == b == "access-2"
    assert gw.refresh_calls == 1  # second caller saw the freshly-refreshed token
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cognito_auth.py -k refresh -v`
Expected: FAIL — returns stale `access-1` / `refresh_calls == 0` (no refresh logic yet).

- [ ] **Step 3: Write minimal implementation**

Replace `async_get_access_token` in `src/place/auth/cognito_auth.py`:

```python
    async def async_get_access_token(self) -> str:
        async with self._refresh_lock:
            if self._access_token is not None and time.time() < (
                self._access_token_expiry - self._config.token_refresh_margin_sec
            ):
                return self._access_token
            if self._refresh_token is None:
                if self._access_token is None:
                    raise PlaceAuthError("not authenticated; call authenticate() first")
                return self._access_token
            auth = await asyncio.to_thread(self._gateway.refresh, self._refresh_token)
            self._store_tokens(auth)
            assert self._access_token is not None
            return self._access_token
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cognito_auth.py -v`
Expected: PASS (5 tests). The Task-5 `test_authenticate_stores_tokens` still passes (fresh token is within margin).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/auth/cognito_auth.py tests/test_cognito_auth.py
git commit -m "feat: refresh Cognito access token on expiry (single-flight)"
```

---

### Task 7: `CognitoAuth.async_get_iot_credentials` — cache + refresh

**Files:**
- Modify: `src/place/auth/cognito_auth.py`
- Test: `tests/test_cognito_auth.py` (extend)

**Interfaces:**
- Consumes: Task 6 + `gateway.iot_credentials`; `Credentials`.
- Produces: `async async_get_iot_credentials() -> Credentials` — returns cached creds while `now < expiration - creds_refresh_margin_sec`, else re-exchanges (ensuring a fresh id token first) under `_iot_lock`. Tracks `_iot_creds` + `_iot_creds_expiry`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cognito_auth.py  (append)
from datetime import datetime, timedelta, timezone

from place.models import Credentials


def _creds(exp: datetime) -> Credentials:
    return Credentials(
        access_key_id="AKIA",
        secret_access_key="s",
        session_token="t",
        identity_id="idid",
        access_token="access-1",
        expiration=exp,
    )


async def test_iot_credentials_cached_until_near_expiry() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()}, creds=_creds(far))
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]
    await auth.authenticate("alice", "pw")

    first = await auth.async_get_iot_credentials()
    second = await auth.async_get_iot_credentials()
    assert first is second  # served from cache


async def test_iot_credentials_refresh_when_expired() -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()}, creds=_creds(past))
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # type: ignore[arg-type]
    await auth.authenticate("alice", "pw")

    first = await auth.async_get_iot_credentials()
    second = await auth.async_get_iot_credentials()
    assert first is not second  # stale creds forced a re-exchange each call
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cognito_auth.py -k iot -v`
Expected: FAIL — `AttributeError: 'CognitoAuth' object has no attribute 'async_get_iot_credentials'`.

- [ ] **Step 3: Write minimal implementation**

Add to `__init__` (after `_refresh_lock`):

```python
        self._iot_creds = None
        self._iot_creds_expiry: datetime | None = None
        self._iot_lock = asyncio.Lock()
```

Add imports at the top: `from datetime import datetime, timedelta, timezone` and `from ..models import Credentials`. Add the method:

```python
    async def async_get_iot_credentials(self) -> Credentials:
        async with self._iot_lock:
            if self._iot_creds is not None and self._iot_creds_expiry is not None:
                margin = timedelta(seconds=self._config.creds_refresh_margin_sec)
                if datetime.now(timezone.utc) < self._iot_creds_expiry - margin:
                    return self._iot_creds
            access_token = await self.async_get_access_token()
            assert self._id_token is not None
            creds = await asyncio.to_thread(
                self._gateway.iot_credentials, self._id_token, access_token
            )
            self._iot_creds = creds
            self._iot_creds_expiry = creds.expiration or (
                datetime.now(timezone.utc)
                + timedelta(seconds=self._config.url_expire_sec)
            )
            return creds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cognito_auth.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/auth/cognito_auth.py tests/test_cognito_auth.py
git commit -m "feat: cache and refresh IoT credentials by expiry"
```

---

## Phase 2 — Transport

### Task 8: Move the SigV4 signer into `transport.py` (config-driven)

Moves the presigned-URL signer out of the doomed `mqtt_client.py` and makes it source `region`/`url_expire_sec` from `PlaceConfig`. The fixed AWS protocol strings stay imported from `config`. The old copy in `mqtt_client.py` is left in place (that whole file is deleted in Task 18); this is a deliberate, documented transient — nothing new consumes the old copy.

**Files:**
- Create: `src/place/transport.py`
- Test: `tests/test_transport_signing.py`

**Interfaces:**
- Consumes: `PlaceConfig` (`region, iot_endpoint, url_expire_sec`); `Credentials`; module constants `ALGORITHM, SERVICE, SCHEME, PATH` from `config`.
- Produces: `get_signed_uri(config: PlaceConfig, credentials: Credentials) -> str` returning `wss://{iot_endpoint}/mqtt?<sigv4 query>`; module helpers `_hmac_sha256`, `_sha256_hex`, `_sign`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transport_signing.py
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from place.config import PlaceConfig
from place.models import Credentials
from place.transport import get_signed_uri


def _creds() -> Credentials:
    return Credentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG",
        session_token="SESSIONTOKEN",
        identity_id="us-east-2:idid",
    )


def test_signed_uri_is_a_presigned_wss_url() -> None:
    cfg = PlaceConfig()
    uri = get_signed_uri(cfg, _creds())

    assert uri.startswith(f"wss://{cfg.iot_endpoint}/mqtt?")
    q = parse_qs(urlsplit(uri).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == [str(cfg.url_expire_sec)]
    assert q["X-Amz-Security-Token"] == ["SESSIONTOKEN"]
    assert q["X-Amz-SignedHeaders"] == ["host"]
    assert "X-Amz-Signature" in q
    cred = q["X-Amz-Credential"][0]
    assert cred.startswith("AKIDEXAMPLE/")
    assert f"{cfg.region}/iotdevicegateway/aws4_request" in cred


def test_region_flows_into_the_credential_scope() -> None:
    cfg = PlaceConfig(region="eu-west-1")
    q = parse_qs(urlsplit(get_signed_uri(cfg, _creds())).query)
    assert "eu-west-1/iotdevicegateway/aws4_request" in q["X-Amz-Credential"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_transport_signing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'place.transport'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/place/transport.py` (signer only — no aiomqtt yet). The signing body is the one from `mqtt_client.py:get_signed_uri`, changed only to read `config.region` / `config.url_expire_sec` / `config.iot_endpoint` instead of module globals:

```python
# src/place/transport.py
# ABOUTME: The async MQTT transport for Place — the SigV4 WebSocket presigner, an
# ABOUTME: MqttTransport seam over aiomqtt, and the self-healing PlaceConnection loop.
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

from .config import ALGORITHM, PATH, SCHEME, SERVICE, PlaceConfig
from .models import Credentials


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest().lower()


def _sign(key: bytes, msg: str) -> bytes:
    return _hmac_sha256(key, msg)


def get_signed_uri(config: PlaceConfig, credentials: Credentials) -> str:
    """Build a SigV4 presigned WSS URL for the AWS IoT MQTT WebSocket endpoint."""
    host = config.iot_endpoint
    region = config.region
    access_key_id = credentials.access_key_id
    secret_access_key = credentials.secret_access_key
    session_token = credentials.session_token

    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    credential_scope = f"{access_key_id}/{date_stamp}/{region}/{SERVICE}/aws4_request"
    signed_headers = "host"
    canonical_headers = f"host:{host}\n"
    payload_hash = _sha256_hex("")

    def enc(s: str) -> str:
        return quote(str(s), safe="")

    query = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": credential_scope,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(config.url_expire_sec),
        "X-Amz-SignedHeaders": signed_headers,
    }
    canonical_query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    canonical_request = "\n".join(
        ["GET", PATH, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    request_hash = _sha256_hex(canonical_request)
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, f"{date_stamp}/{region}/{SERVICE}/aws4_request", request_hash]
    )
    k_date = _sign(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, SERVICE)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    query["X-Amz-Signature"] = signature
    query["X-Amz-Security-Token"] = session_token
    query_string = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    return f"{SCHEME}://{host}{PATH}?{query_string}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_transport_signing.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/transport.py tests/test_transport_signing.py
git commit -m "feat: add config-driven SigV4 presigner in transport"
```

---

### Task 9: aiomqtt transport adapter (add dep + introspect + thin wrapper)

The one task with an **empirical step**: aiomqtt is not yet installed, so we add it and read its real constructor signature rather than guessing kwargs. The only unit-tested logic is the pure `websocket_options` helper; the thin `AiomqttTransport` adapter is validated live by the example scripts (Task 17) and, indirectly, by the fake-transport tests of `PlaceConnection`.

**Files:**
- Modify: `pyproject.toml` (via `uv add`)
- Modify: `src/place/transport.py` (add `websocket_options`, `MqttTransport`, `TransportFactory`, `AiomqttTransport`)
- Test: `tests/test_transport_signing.py` (extend)

**Interfaces:**
- Consumes: `get_signed_uri`, `PlaceConfig`, `Credentials`.
- Produces:
  - `websocket_options(signed_uri: str, host: str) -> tuple[str, dict[str, str]]`.
  - `MqttTransport` Protocol: async context manager + `async subscribe(topic, qos=1)`, `async publish(topic, payload=b"", qos=1)`, `messages() -> AsyncIterator[tuple[str, bytes]]`.
  - `TransportFactory = Callable[[PlaceConfig, Credentials], MqttTransport]`.
  - `AiomqttTransport(config, credentials)` implementing `MqttTransport`.

- [ ] **Step 1: Add the dependency**

Run: `uv add aiomqtt`
Expected: `pyproject.toml` gains `aiomqtt` under `[project].dependencies`; `uv.lock` updates (stays untracked).

- [ ] **Step 2: Introspect the real constructor (do not guess)**

Run: `uv run python -c "import aiomqtt, inspect; print(aiomqtt.__version__); print(inspect.signature(aiomqtt.Client.__init__))"`
Record the printed version and signature. Confirm the exact names for: the WebSocket transport selector, the websocket path kwarg, the websocket headers kwarg, the TLS kwarg, the client-identifier kwarg, and the keepalive kwarg. **Use those exact names in Step 4** — the names below reflect aiomqtt v2 and must be reconciled with what you just printed. Also confirm how incoming messages are consumed (v2: `async for message in client.messages`).

- [ ] **Step 3: Write the failing test (pure helper)**

```python
# tests/test_transport_signing.py  (append)
from place.transport import websocket_options


def test_websocket_options_extracts_path_and_host_header() -> None:
    signed = "wss://host.example/mqtt?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
    path, headers = websocket_options(signed, "host.example")
    assert path == "/mqtt?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
    assert headers == {"Host": "host.example"}
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_transport_signing.py -k websocket -v`
Expected: FAIL — `ImportError: cannot import name 'websocket_options'`.

- [ ] **Step 5: Write minimal implementation**

Append to `src/place/transport.py`:

```python
import uuid
from typing import AsyncIterator, Callable, Protocol

import aiomqtt


def websocket_options(signed_uri: str, host: str) -> tuple[str, dict[str, str]]:
    """Split a signed WSS URL into the aiomqtt websocket path (with query) + Host header."""
    path_with_query = PATH + signed_uri.split(PATH, 1)[1]
    return path_with_query, {"Host": host}


class MqttTransport(Protocol):
    """A single MQTT connection lifecycle: enter, (un)subscribe, publish, stream messages."""

    async def __aenter__(self) -> "MqttTransport": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def subscribe(self, topic: str, qos: int = 1) -> None: ...
    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None: ...
    def messages(self) -> AsyncIterator[tuple[str, bytes]]: ...


TransportFactory = Callable[[PlaceConfig, Credentials], MqttTransport]


def _as_bytes(payload: object) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return str(payload).encode("utf-8")


class AiomqttTransport:
    """MqttTransport backed by aiomqtt over a SigV4-presigned AWS IoT WebSocket."""

    def __init__(self, config: PlaceConfig, credentials: Credentials) -> None:
        signed_uri = get_signed_uri(config, credentials)
        path, headers = websocket_options(signed_uri, config.iot_endpoint)
        client_id = f"{credentials.identity_id}-{uuid.uuid4()}"
        # Kwarg names below are aiomqtt v2 — reconcile with Step 2's introspection.
        self._client = aiomqtt.Client(
            hostname=config.iot_endpoint,
            port=443,
            identifier=client_id,
            transport="websockets",
            websocket_path=path,
            websocket_headers=headers,
            tls_params=aiomqtt.TLSParameters(),
            keepalive=config.keep_alive_sec,
        )

    async def __aenter__(self) -> "AiomqttTransport":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        await self._client.subscribe(topic, qos=qos)

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        await self._client.publish(topic, payload=payload, qos=qos)

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        async for message in self._client.messages:
            yield str(message.topic), _as_bytes(message.payload)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_transport_signing.py -v`
Expected: PASS (3 tests). Also confirm import health: `uv run python -c "from place.transport import AiomqttTransport, MqttTransport"`.

- [ ] **Step 7: Commit**

```bash
git status
git add pyproject.toml src/place/transport.py tests/test_transport_signing.py
git commit -m "feat: add aiomqtt transport adapter over the presigned websocket"
```

---

### Task 10: `PlaceConnection` — connect, subscribe, dispatch

The self-healing loop's happy path: fetch IoT creds, open the transport, subscribe the desired set, fire the on-connect publishes (shadow/get), then pump messages to a callback. Reconnect/backoff and proactive refresh come in Tasks 11–12. Tested against a fake transport (no broker).

**Files:**
- Modify: `src/place/transport.py`
- Test: `tests/test_place_connection.py`

**Interfaces:**
- Consumes: `TransportFactory`, `PlaceConfig`, an auth object exposing `async async_get_iot_credentials() -> Credentials`, `PlaceConnectionError`.
- Produces: `PlaceConnection(config, auth, *, transport_factory, on_message, on_state=None, sleep=asyncio.sleep, jitter=None)`; methods `add_subscription(topic)`, `add_connect_publish(topic, payload=b"")`, `async run()`, `stop()`, `async publish(topic, payload=b"")`. `on_message` is `Callable[[str, bytes], None]`; `on_state` is `Callable[[bool], None] | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_place_connection.py
from __future__ import annotations

from place.config import PlaceConfig
from place.models import Credentials
from place.transport import PlaceConnection


def _creds() -> Credentials:
    return Credentials("AK", "secret", "tok", "idid")


class FakeAuth:
    def __init__(self, creds: Credentials) -> None:
        self._creds = creds
        self.calls = 0

    async def async_get_iot_credentials(self) -> Credentials:
        self.calls += 1
        return self._creds


class ScriptedTransport:
    """A one-shot fake connection: replays scripted messages, then stops the loop."""

    def __init__(self, messages, subs, published, stop) -> None:
        self._messages = messages
        self._subs = subs
        self._published = published
        self._stop = stop

    async def __aenter__(self) -> "ScriptedTransport":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        self._subs.append(topic)

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        self._published.append((topic, payload))

    async def messages(self):
        for item in self._messages:
            yield item
        self._stop()  # drain, then end the loop


async def test_connect_subscribes_publishes_and_dispatches() -> None:
    subs: list[str] = []
    published: list[tuple[str, bytes]] = []
    received: list[tuple[str, bytes]] = []
    states: list[bool] = []
    auth = FakeAuth(_creds())

    conn = PlaceConnection(
        PlaceConfig(),
        auth,
        transport_factory=lambda cfg, creds: ScriptedTransport(
            [("$aws/things/T/shadow/get/accepted", b"{}")], subs, published, conn.stop
        ),
        on_message=lambda t, p: received.append((t, p)),
        on_state=states.append,
    )
    conn.add_subscription("$aws/things/T/shadow/#")
    conn.add_connect_publish("$aws/things/T/shadow/get")

    await conn.run()

    assert subs == ["$aws/things/T/shadow/#"]
    assert published == [("$aws/things/T/shadow/get", b"")]
    assert received == [("$aws/things/T/shadow/get/accepted", b"{}")]
    assert states == [True, False]
    assert auth.calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_place_connection.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlaceConnection'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/place/transport.py` (add `import asyncio` at the top of the file, and `from .exceptions import PlaceConnectionError`):

```python
class PlaceConnection:
    """A self-healing MQTT session: (re)connects, subscribes, and pumps messages."""

    def __init__(
        self,
        config: PlaceConfig,
        auth,
        *,
        transport_factory: TransportFactory,
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None] | None = None,
        sleep: Callable[[float], "asyncio.Future[None]"] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._transport_factory = transport_factory
        self._on_message = on_message
        self._on_state = on_state
        self._sleep = sleep
        self._jitter = jitter or (lambda d: d)
        self._subscriptions: list[str] = []
        self._connect_publishes: list[tuple[str, bytes]] = []
        self._transport: MqttTransport | None = None
        self._stopped = False

    def add_subscription(self, topic: str) -> None:
        if topic not in self._subscriptions:
            self._subscriptions.append(topic)

    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None:
        self._connect_publishes.append((topic, payload))

    def stop(self) -> None:
        self._stopped = True

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        if self._transport is None:
            raise PlaceConnectionError("not connected")
        await self._transport.publish(topic, payload)

    async def run(self) -> None:
        while not self._stopped:
            creds = await self._auth.async_get_iot_credentials()
            async with self._transport_factory(self._config, creds) as transport:
                self._transport = transport
                try:
                    for topic in self._subscriptions:
                        await transport.subscribe(topic)
                    for topic, payload in self._connect_publishes:
                        await transport.publish(topic, payload)
                    if self._on_state:
                        self._on_state(True)
                    async for topic, payload in transport.messages():
                        self._on_message(topic, payload)
                finally:
                    self._transport = None
                    if self._on_state:
                        self._on_state(False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_place_connection.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/transport.py tests/test_place_connection.py
git commit -m "feat: add PlaceConnection connect/subscribe/dispatch loop"
```

---

### Task 11: `PlaceConnection` — reconnect with exponential backoff

Wrap each connection cycle so an `aiomqtt.MqttError` triggers a bounded exponential-backoff reconnect instead of killing the loop. `sleep` and `jitter` are injected so the schedule is deterministic under test.

**Files:**
- Modify: `src/place/transport.py` (replace `run`)
- Test: `tests/test_place_connection.py` (extend)

**Interfaces:**
- Consumes: Task 10 members + `config.reconnect_min_sec`, `config.reconnect_max_sec`, `aiomqtt.MqttError`.
- Produces: `run()` retries on `MqttError` with delay `min(reconnect_max_sec, reconnect_min_sec * 2**attempt)` (jittered), resetting `attempt` after a clean cycle; a clean stop during backoff breaks out.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_place_connection.py  (append)
from aiomqtt import MqttError


class FlakyTransport:
    """Fails with MqttError for the first `fail_times` connects, then drains and stops."""

    def __init__(self, attempt: int, fail_times: int, stop) -> None:
        self._attempt = attempt
        self._fail_times = fail_times
        self._stop = stop

    async def __aenter__(self) -> "FlakyTransport":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        return None

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        return None

    async def messages(self):
        if self._attempt <= self._fail_times:
            raise MqttError("dropped")
        self._stop()
        return
        yield  # pragma: no cover - marks this an async generator


def _flaky_factory(fail_times: int, stop_getter):
    state = {"n": 0}

    def factory(cfg, creds):
        state["n"] += 1
        return FlakyTransport(state["n"], fail_times, stop_getter())

    return factory


async def test_backoff_grows_then_connects() -> None:
    slept: list[float] = []
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=1.0, reconnect_max_sec=60.0),
        FakeAuth(_creds()),
        transport_factory=_flaky_factory(2, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=lambda d: _noop_sleep(slept, d),
    )
    await conn.run()
    assert slept == [1.0, 2.0]  # 2 failures -> two backoff sleeps, then success


async def test_backoff_is_capped() -> None:
    slept: list[float] = []
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=1.0, reconnect_max_sec=1.5),
        FakeAuth(_creds()),
        transport_factory=_flaky_factory(3, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=lambda d: _noop_sleep(slept, d),
    )
    await conn.run()
    assert slept == [1.0, 1.5, 1.5]  # 1.0, 2.0->cap 1.5, 4.0->cap 1.5


async def _noop_sleep(record: list[float], delay: float) -> None:
    record.append(delay)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_place_connection.py -k backoff -v`
Expected: FAIL — the un-guarded `run()` lets `MqttError` propagate, so `run()` raises instead of retrying.

- [ ] **Step 3: Write minimal implementation**

Add `from aiomqtt import MqttError` near the aiomqtt import, then replace `run` in `src/place/transport.py`:

```python
    async def run(self) -> None:
        attempt = 0
        while not self._stopped:
            try:
                creds = await self._auth.async_get_iot_credentials()
                async with self._transport_factory(self._config, creds) as transport:
                    self._transport = transport
                    try:
                        for topic in self._subscriptions:
                            await transport.subscribe(topic)
                        for topic, payload in self._connect_publishes:
                            await transport.publish(topic, payload)
                        if self._on_state:
                            self._on_state(True)
                        async for topic, payload in transport.messages():
                            self._on_message(topic, payload)
                    finally:
                        self._transport = None
                        if self._on_state:
                            self._on_state(False)
                attempt = 0
            except MqttError:
                if self._stopped:
                    break
                delay = min(
                    self._config.reconnect_max_sec,
                    self._config.reconnect_min_sec * (2**attempt),
                )
                attempt += 1
                await self._sleep(self._jitter(delay))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_place_connection.py -v`
Expected: PASS (3 tests — Task 10's still green).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/transport.py tests/test_place_connection.py
git commit -m "feat: reconnect PlaceConnection with capped exponential backoff"
```

---

### Task 12: `PlaceConnection` — fresh credentials + proactive refresh

Two related properties: every (re)connect fetches fresh IoT credentials from auth (already true — assert it), and the message pump is bounded by `asyncio.timeout(...)` so the connection proactively cycles *before* the credentials expire rather than waiting for AWS to drop it. The deadline math is a pure, unit-tested function; the `asyncio.timeout` wiring is exercised live.

**Files:**
- Modify: `src/place/transport.py` (add `_seconds_until_refresh`, wrap the pump)
- Test: `tests/test_place_connection.py` (extend)

**Interfaces:**
- Consumes: Task 11 + `Credentials.expiration`, `config.creds_refresh_margin_sec`.
- Produces: `_seconds_until_refresh(creds: Credentials) -> float | None` (None when expiry unknown; clamped ≥ 0); `run()` fetches fresh creds per cycle and treats a refresh-deadline `TimeoutError` as a clean reconnect.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_place_connection.py  (append)
from datetime import datetime, timedelta, timezone


def test_seconds_until_refresh_uses_margin() -> None:
    conn = PlaceConnection(
        PlaceConfig(creds_refresh_margin_sec=600),
        FakeAuth(_creds()),
        transport_factory=lambda cfg, creds: None,  # unused here
        on_message=lambda t, p: None,
    )
    far = _creds()
    far.expiration = datetime.now(timezone.utc) + timedelta(seconds=3600)
    secs = conn._seconds_until_refresh(far)
    assert secs is not None and 2900 < secs <= 3000

    unknown = _creds()  # expiration is None
    assert conn._seconds_until_refresh(unknown) is None

    stale = _creds()
    stale.expiration = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert conn._seconds_until_refresh(stale) == 0.0  # clamped, never negative


async def test_each_connect_fetches_fresh_credentials() -> None:
    auth = FakeAuth(_creds())
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=0.0, reconnect_max_sec=0.0),
        auth,
        transport_factory=_flaky_factory(1, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=lambda d: _noop_sleep([], d),
    )
    await conn.run()
    assert auth.calls == 2  # one failed connect + one successful, each fetched creds
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_place_connection.py -k "refresh or fresh_credentials" -v`
Expected: FAIL — `AttributeError: 'PlaceConnection' object has no attribute '_seconds_until_refresh'`.

- [ ] **Step 3: Write minimal implementation**

Add `from datetime import datetime, timezone` to the imports (already present from the signer). Add the method and wrap the pump in `run`:

```python
    def _seconds_until_refresh(self, creds: Credentials) -> float | None:
        if creds.expiration is None:
            return None
        remaining = (creds.expiration - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining - self._config.creds_refresh_margin_sec)
```

In `run`, replace the message-pump block:

```python
                        if self._on_state:
                            self._on_state(True)
                        async for topic, payload in transport.messages():
                            self._on_message(topic, payload)
```

with:

```python
                        if self._on_state:
                            self._on_state(True)
                        try:
                            async with asyncio.timeout(self._seconds_until_refresh(creds)):
                                async for topic, payload in transport.messages():
                                    self._on_message(topic, payload)
                        except TimeoutError:
                            pass  # proactive refresh: reconnect with fresh credentials
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_place_connection.py -v`
Expected: PASS (5 tests). `asyncio.timeout(None)` means "no deadline", so Tasks 10–11 (creds without expiration) are unaffected.

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/transport.py tests/test_place_connection.py
git commit -m "feat: proactively refresh credentials before expiry"
```

---

## Phase 3 — Domain

### Task 13: `PlaceDevice` — the one source of truth per device

A stateful device object built from discovery, updated in place by shadow messages and events, that notifies listeners on every change. This is the single source every consumer reads from.

**Files:**
- Create: `src/place/device.py`
- Test: `tests/test_device.py`

**Interfaces:**
- Consumes: `DiscoverDevice`, `PlaceDeviceShadow`, `DeviceEvent` (from `place.models`).
- Produces: `PlaceDevice(thing_name: str, shadow: PlaceDeviceShadow, device_id=None, name=None, model=None, online=None, last_event=None)`; classmethod `from_discovery(d: DiscoverDevice) -> PlaceDevice` (raises `ValueError` if `thing_name` is falsy); `add_listener(cb: Callable[[PlaceDevice], None]) -> Callable[[], None]`; `apply_shadow(message: dict) -> None`; `apply_event(event: DeviceEvent) -> None`; `set_online(online: bool | None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_device.py
from __future__ import annotations

from place.device import PlaceDevice
from place.models import AlarmStatus, DeviceEvent, DiscoverDevice


def _discover() -> DiscoverDevice:
    return DiscoverDevice.from_dict(
        {
            "thingName": "Place_PL1AS_EXAMPLE",
            "deviceId": "dev-1",
            "deviceName": "Hallway",
            "modelNumber": "PL1AS",
            "online": True,
            "shadow": {"state": {"reported": {"coPpm": 3, "smokeAlarmStatus": 0}}},
        }
    )


def test_from_discovery_maps_identity_and_shadow() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    assert dev.thing_name == "Place_PL1AS_EXAMPLE"
    assert dev.device_id == "dev-1"
    assert dev.name == "Hallway"
    assert dev.online is True
    assert dev.shadow.co_ppm == 3
    assert dev.shadow.smoke_alarm_status is AlarmStatus.IDLE


def test_apply_shadow_merges_and_notifies() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    dev.add_listener(seen.append)

    dev.apply_shadow({"state": {"reported": {"coPpm": 9}}})

    assert dev.shadow.co_ppm == 9
    assert dev.shadow.smoke_alarm_status is AlarmStatus.IDLE  # untouched key persists
    assert seen == [dev]


def test_apply_event_records_motion_and_notifies() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    dev.add_listener(seen.append)

    event = DeviceEvent(event_type="motionDetected", device_id="dev-1")
    dev.apply_event(event)

    assert dev.last_event is event
    assert dev.last_event.is_motion is True
    assert seen == [dev]


def test_unsubscribe_stops_notifications() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    unsubscribe = dev.add_listener(seen.append)

    unsubscribe()
    dev.apply_shadow({"state": {"reported": {"coPpm": 1}}})

    assert seen == []


def test_from_discovery_without_thing_name_is_rejected() -> None:
    import pytest

    bad = DiscoverDevice.from_dict({"deviceId": "dev-1"})
    with pytest.raises(ValueError):
        PlaceDevice.from_discovery(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_device.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'place.device'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/place/device.py
# ABOUTME: PlaceDevice — the stateful, one-source-of-truth model for a single PLACE
# ABOUTME: device: identity + live shadow + last event, with change listeners.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import DeviceEvent, DiscoverDevice, PlaceDeviceShadow

Listener = Callable[["PlaceDevice"], None]


@dataclass
class PlaceDevice:
    """A device's live state. Mutated in place by shadow/event dispatch."""

    thing_name: str
    shadow: PlaceDeviceShadow
    device_id: str | None = None
    name: str | None = None
    model: str | None = None
    online: bool | None = None
    last_event: DeviceEvent | None = None
    _listeners: list[Listener] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_discovery(cls, discovered: DiscoverDevice) -> "PlaceDevice":
        if not discovered.thing_name:
            raise ValueError("cannot build a PlaceDevice without a thing_name")
        return cls(
            thing_name=discovered.thing_name,
            shadow=PlaceDeviceShadow.from_shadow(discovered.shadow),
            device_id=discovered.device_id,
            name=discovered.device_name,
            model=discovered.model_number,
            online=discovered.online,
        )

    def add_listener(self, callback: Listener) -> Callable[[], None]:
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsubscribe

    def apply_shadow(self, message: dict) -> None:
        self.shadow.merge(message)
        self._notify()

    def apply_event(self, event: DeviceEvent) -> None:
        self.last_event = event
        self._notify()

    def set_online(self, online: bool | None) -> None:
        if self.online != online:
            self.online = online
            self._notify()

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback(self)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_device.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/device.py tests/test_device.py
git commit -m "feat: add stateful PlaceDevice with change listeners"
```

---

## Phase 4 — Facade

### Task 14: `PlaceClient` — construction, discovery, start/stop

The facade that ties discovery, the connection loop, and the device registry together. This task wires start/stop; message routing lands in Task 15 (`_dispatch` is a stub here). Tested with a fake provider and a fake connection.

**Files:**
- Create: `src/place/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `PlaceConfig`; `Provider`; `PlaceConnection` + `AiomqttTransport`; `PlaceDevice`; topic builders `shadow_subscription_topic`, `shadow_get_topic`, `household_subscription_topic`.
- Produces:
  - `PlaceClient(config, auth, *, provider, connection_factory, household_ids=None)` where `connection_factory(on_message: Callable[[str, bytes], None], on_state: Callable[[bool], None]) -> connection`.
  - classmethod `create(config, auth, *, household_ids=None) -> PlaceClient` (wires the real `Provider` + real `PlaceConnection`/`AiomqttTransport`).
  - `devices -> dict[str, PlaceDevice]` (copy); `async start()`; `async stop()`; `async __aenter__/__aexit__`.
  - Internal `_dispatch(topic, payload)` (stub) and `_set_connected(connected)` (stores `_connected`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client.py
from __future__ import annotations

import asyncio

from place.client import PlaceClient
from place.config import PlaceConfig
from place.messages import (
    household_subscription_topic,
    shadow_get_topic,
    shadow_subscription_topic,
)
from place.models import DiscoverDevice


class FakeProvider:
    def __init__(self, devices: list[DiscoverDevice]) -> None:
        self._devices = devices

    async def discover(self) -> list[DiscoverDevice]:
        return self._devices


class FakeConnection:
    def __init__(self, on_message, on_state) -> None:
        self.on_message = on_message
        self.on_state = on_state
        self.subscriptions: list[str] = []
        self.connect_publishes: list[tuple[str, bytes]] = []
        self.published: list[tuple[str, bytes]] = []
        self.started = False
        self.stopped = False
        self._gate = asyncio.Event()

    def add_subscription(self, topic: str) -> None:
        self.subscriptions.append(topic)

    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None:
        self.connect_publishes.append((topic, payload))

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        self.published.append((topic, payload))

    async def run(self) -> None:
        self.started = True
        await self._gate.wait()

    def stop(self) -> None:
        self.stopped = True
        self._gate.set()


def _discover(thing: str) -> DiscoverDevice:
    return DiscoverDevice.from_dict({"thingName": thing, "deviceId": "dev-1", "shadow": {}})


async def test_start_discovers_wires_subscriptions_and_launches() -> None:
    client = PlaceClient(
        PlaceConfig(),
        auth=object(),
        provider=FakeProvider([_discover("Place_PL1AS_EXAMPLE")]),
        connection_factory=FakeConnection,
        household_ids=["hh-1"],
    )

    await client.start()
    conn = client._connection

    assert "Place_PL1AS_EXAMPLE" in client.devices
    assert shadow_subscription_topic("Place_PL1AS_EXAMPLE") in conn.subscriptions
    assert household_subscription_topic("hh-1") in conn.subscriptions
    assert (shadow_get_topic("Place_PL1AS_EXAMPLE"), b"") in conn.connect_publishes
    assert conn.started is True

    await client.stop()
    assert conn.stopped is True


async def test_create_builds_a_client_with_empty_registry() -> None:
    client = PlaceClient.create(PlaceConfig(), auth=object())
    assert client.devices == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'place.client'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/place/client.py
# ABOUTME: PlaceClient — the async facade over discovery, the self-healing MQTT
# ABOUTME: connection, and the PlaceDevice registry; read-only (shadow/get + subscribe).
from __future__ import annotations

import asyncio
from typing import Callable

from .config import PlaceConfig
from .device import PlaceDevice
from .messages import (
    household_subscription_topic,
    shadow_get_topic,
    shadow_subscription_topic,
)
from .provider import Provider
from .transport import AiomqttTransport, PlaceConnection

ConnectionFactory = Callable[
    [Callable[[str, bytes], None], Callable[[bool], None]], object
]


class PlaceClient:
    """Read-only async client for a PLACE account's devices."""

    def __init__(
        self,
        config: PlaceConfig,
        auth,
        *,
        provider,
        connection_factory: ConnectionFactory,
        household_ids: list[str] | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._provider = provider
        self._household_ids = list(household_ids or [])
        self._devices: dict[str, PlaceDevice] = {}
        self._connected = False
        self._connection = connection_factory(self._dispatch, self._set_connected)
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def create(
        cls,
        config: PlaceConfig,
        auth,
        *,
        household_ids: list[str] | None = None,
    ) -> "PlaceClient":
        provider = Provider(auth)

        def connection_factory(on_message, on_state):
            return PlaceConnection(
                config,
                auth,
                transport_factory=lambda cfg, creds: AiomqttTransport(cfg, creds),
                on_message=on_message,
                on_state=on_state,
            )

        return cls(
            config,
            auth,
            provider=provider,
            connection_factory=connection_factory,
            household_ids=household_ids,
        )

    @property
    def devices(self) -> dict[str, PlaceDevice]:
        return dict(self._devices)

    async def start(self) -> None:
        discovered = await self._provider.discover()
        for entry in discovered:
            if not entry.thing_name:
                continue
            device = PlaceDevice.from_discovery(entry)
            self._devices[device.thing_name] = device
            self._connection.add_subscription(shadow_subscription_topic(device.thing_name))
            self._connection.add_connect_publish(shadow_get_topic(device.thing_name))
        for household_id in self._household_ids:
            self._connection.add_subscription(household_subscription_topic(household_id))
        self._task = asyncio.create_task(self._connection.run())
        await asyncio.sleep(0)  # let the connection task take its first step before we return

    async def stop(self) -> None:
        self._connection.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def __aenter__(self) -> "PlaceClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def _dispatch(self, topic: str, payload: bytes) -> None:
        pass  # routing added in Task 15

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/client.py tests/test_client.py
git commit -m "feat: add PlaceClient discovery and start/stop wiring"
```

---

### Task 15: `PlaceClient` — dispatch, callbacks, `updates()`, connection state

Route incoming messages to devices, expose `on_update`/`on_event`/`on_connection_change` callbacks and an `updates()` async-iterator, and surface connection state. Requires a module-level `thing_name_from_topic` in `messages.py` (extracted from the existing staticmethod so both share one implementation).

**Files:**
- Modify: `src/place/messages.py` (add module-level `thing_name_from_topic`; staticmethod delegates)
- Modify: `src/place/client.py` (real `_dispatch`, callbacks, `updates()`, connection notifications)
- Test: `tests/test_client.py` (extend)

**Interfaces:**
- Consumes: Task 14; `parse_payload`, `thing_name_from_topic` (messages); `DeviceEvent`, `EVENTS_SEGMENT` (models).
- Produces: `on_update(cb: Callable[[PlaceDevice], None]) -> unsub`; `on_event(cb: Callable[[DeviceEvent], None]) -> unsub`; `on_connection_change(cb: Callable[[bool], None]) -> unsub`; `updates() -> AsyncIterator[PlaceDevice]`; `connected -> bool`. `_dispatch` routes shadow topics (`$aws/things/{thing}/shadow/...`) to `device.apply_shadow` and household `.../events/{type}` topics to `device.apply_event` + event listeners. The current client contract emits `on_update`/`updates()` for every valid message carrying reported shadow state, including a value-identical liveness reply; empty MQTT wildcard echoes remain silent. This contract supersedes the historical implementation sketch below without changing `PlaceDevice`'s field-change-only listener semantics.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client.py  (append)
from place.messages import thing_name_from_topic


def test_thing_name_from_topic_module_function() -> None:
    assert (
        thing_name_from_topic("$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted")
        == "Place_PL1AS_EXAMPLE"
    )
    assert thing_name_from_topic("connectedsmoke/household/hh-1/x") is None


async def _started_client(*discover_args: str) -> PlaceClient:
    client = PlaceClient(
        PlaceConfig(),
        auth=object(),
        provider=FakeProvider([_discover(t) for t in discover_args]),
        connection_factory=FakeConnection,
    )
    await client.start()
    return client


async def test_shadow_message_updates_device_and_emits_update() -> None:
    client = await _started_client("Place_PL1AS_EXAMPLE")
    updates: list[PlaceDevice] = []
    client.on_update(updates.append)

    client._dispatch(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":12}}}',
    )

    assert client.devices["Place_PL1AS_EXAMPLE"].shadow.co_ppm == 12
    assert updates == [client.devices["Place_PL1AS_EXAMPLE"]]
    await client.stop()


async def test_event_message_routes_and_emits_event() -> None:
    client = await _started_client("Place_PL1AS_EXAMPLE")
    events = []
    client.on_event(events.append)

    client._dispatch(
        "connectedsmoke/household/hh-1/device/dev-1/events/motionDetected",
        b'{"deviceId":"dev-1","thingName":"Place_PL1AS_EXAMPLE","seq":5}',
    )

    assert len(events) == 1 and events[0].is_motion is True
    assert client.devices["Place_PL1AS_EXAMPLE"].last_event.seq == 5
    await client.stop()


async def test_updates_iterator_yields_changed_devices() -> None:
    client = await _started_client("Place_PL1AS_EXAMPLE")
    stream = client.updates()

    client._dispatch(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":1}}}',
    )
    device = await stream.__anext__()

    assert device.shadow.co_ppm == 1
    await stream.aclose()
    await client.stop()


async def test_connection_change_notifies_and_dedupes() -> None:
    client = await _started_client("Place_PL1AS_EXAMPLE")
    changes: list[bool] = []
    client.on_connection_change(changes.append)

    client._set_connected(True)
    client._set_connected(True)  # no-op: state unchanged
    client._set_connected(False)

    assert changes == [True, False]
    assert client.connected is False
    await client.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -k "thing_name or shadow_message or event_message or updates_iterator or connection_change" -v`
Expected: FAIL — `ImportError: cannot import name 'thing_name_from_topic' from 'place.messages'` (and, once that's added, `AttributeError` for `on_update`).

- [ ] **Step 3: Write minimal implementation**

In `src/place/messages.py`, add a module-level function (place it just above the `PlaceMessages` class) and make the staticmethod delegate to it:

```python
def thing_name_from_topic(topic: str) -> str | None:
    """Extract thing_name from an AWS IoT shadow topic ($aws/things/{name}/shadow/...)."""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "$aws" and parts[1] == "things":
        return parts[2]
    return None
```

Replace the body of `PlaceMessages.thing_name_from_topic` with a one-line delegation (keeps one implementation; the class is removed in Task 18):

```python
    @staticmethod
    def thing_name_from_topic(topic: str) -> str | None:
        """Extract thing_name from an AWS IoT shadow topic ($aws/things/{name}/shadow/...)."""
        return thing_name_from_topic(topic)
```

In `src/place/client.py`, add imports:

```python
from .messages import (
    household_subscription_topic,
    parse_payload,
    shadow_get_topic,
    shadow_subscription_topic,
    thing_name_from_topic,
)
from .models import DeviceEvent
from .models.device_event import EVENTS_SEGMENT
```

Add these to `__init__` (after `self._connected = False`):

```python
        self._update_listeners: list[Callable[[PlaceDevice], None]] = []
        self._event_listeners: list[Callable[[DeviceEvent], None]] = []
        self._connection_listeners: list[Callable[[bool], None]] = []
```

Add the callback registry, the iterator, and replace `_dispatch` / `_set_connected`:

```python
    @property
    def connected(self) -> bool:
        return self._connected

    def on_update(self, callback: Callable[[PlaceDevice], None]) -> Callable[[], None]:
        return self._register(self._update_listeners, callback)

    def on_event(self, callback: Callable[[DeviceEvent], None]) -> Callable[[], None]:
        return self._register(self._event_listeners, callback)

    def on_connection_change(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        return self._register(self._connection_listeners, callback)

    def updates(self):
        queue: asyncio.Queue[PlaceDevice] = asyncio.Queue()
        unsubscribe = self.on_update(queue.put_nowait)

        async def _generator():
            try:
                while True:
                    yield await queue.get()
            finally:
                unsubscribe()

        return _generator()

    @staticmethod
    def _register(registry: list, callback):
        registry.append(callback)

        def _unsubscribe() -> None:
            if callback in registry:
                registry.remove(callback)

        return _unsubscribe

    def _dispatch(self, topic: str, raw: bytes) -> None:
        payload = parse_payload(raw)
        thing = thing_name_from_topic(topic)
        if thing is not None:
            device = self._devices.get(thing)
            if device is not None:
                device.apply_shadow(payload)
                self._emit_update(device)
            return
        if EVENTS_SEGMENT in topic:
            event = DeviceEvent.from_message(topic, payload)
            if event is None:
                return
            device = self._device_for_event(event)
            if device is not None:
                device.apply_event(event)
                self._emit_update(device)
            self._emit_event(event)

    def _device_for_event(self, event: DeviceEvent) -> PlaceDevice | None:
        if event.thing_name and event.thing_name in self._devices:
            return self._devices[event.thing_name]
        if event.device_id:
            for device in self._devices.values():
                if device.device_id == event.device_id:
                    return device
        return None

    def _emit_update(self, device: PlaceDevice) -> None:
        for callback in list(self._update_listeners):
            callback(device)

    def _emit_event(self, event: DeviceEvent) -> None:
        for callback in list(self._event_listeners):
            callback(event)

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        for callback in list(self._connection_listeners):
            callback(connected)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py tests/test_messages.py -v`
Expected: PASS (all — Task 14's start/stop tests and `test_messages.py` stay green).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/messages.py src/place/client.py tests/test_client.py
git commit -m "feat: route messages to devices with callbacks and updates stream"
```

---

### Task 16: `PlaceClient.async_refresh_shadow` + on-demand publish

Let a consumer force a fresh shadow snapshot by publishing `shadow/get` on demand (a read trigger — still read-only). Adds the on-demand `publish` path to `PlaceConnection` too.

**Files:**
- Modify: `src/place/client.py` (add `async_refresh_shadow`)
- Test: `tests/test_client.py` (extend `FakeConnection` already has `publish`), `tests/test_place_connection.py` (publish guard)

**Interfaces:**
- Consumes: `PlaceConnection.publish` (Task 10), `shadow_get_topic`.
- Produces: `async async_refresh_shadow(thing_name: str | None = None) -> None` — publishes `shadow/get` for one device or all known devices.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client.py  (append)
async def test_async_refresh_shadow_publishes_get_for_all_devices() -> None:
    client = await _started_client("Place_PL1AS_EXAMPLE")

    await client.async_refresh_shadow()

    assert client._connection.published == [
        (shadow_get_topic("Place_PL1AS_EXAMPLE"), b"")
    ]
    await client.stop()
```

```python
# tests/test_place_connection.py  (append)
import pytest

from place.exceptions import PlaceConnectionError


async def test_publish_without_connection_raises() -> None:
    conn = PlaceConnection(
        PlaceConfig(),
        FakeAuth(_creds()),
        transport_factory=lambda cfg, creds: None,
        on_message=lambda t, p: None,
    )
    with pytest.raises(PlaceConnectionError):
        await conn.publish("$aws/things/T/shadow/get", b"")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -k refresh_shadow tests/test_place_connection.py -k publish -v`
Expected: FAIL — `AttributeError: 'PlaceClient' object has no attribute 'async_refresh_shadow'` (the publish-guard test passes already, since `PlaceConnection.publish` was implemented in Task 10 — keep it as a regression guard).

- [ ] **Step 3: Write minimal implementation**

Add to `src/place/client.py`:

```python
    async def async_refresh_shadow(self, thing_name: str | None = None) -> None:
        names = [thing_name] if thing_name is not None else list(self._devices)
        for name in names:
            await self._connection.publish(shadow_get_topic(name), b"")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py tests/test_place_connection.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git status
git add src/place/client.py tests/test_client.py tests/test_place_connection.py
git commit -m "feat: add on-demand shadow refresh (read-only shadow/get)"
```

---

## Phase 5 — Examples & cleanup

### Task 17: Async example scripts

Three runnable examples on `PlaceClient`. **These are throwaway/spike scripts per the TDD skill's exception — declared out loud: they have no unit tests and are validated by running against Doctor Biz's real account.** All read-only (discovery, `shadow/get`, subscribe). Credentials come from the environment at runtime; MFA is prompted interactively and never written to disk.

**Files:**
- Create: `examples/quickstart.py`, `examples/shadow_snapshot.py`, `examples/watch_live.py`

**Interfaces:**
- Consumes: `PlaceConfig.from_env`, `CognitoAuth`, `PlaceClient`, `Provider`, `PlaceDeviceShadow`, `MfaRequired`.
- Produces: nothing importable (scripts).

- [ ] **Step 1: Write `examples/shadow_snapshot.py` (discovery-only, no MQTT)**

```python
# examples/shadow_snapshot.py
# ABOUTME: One-shot PLACE shadow snapshot from discovery — no MQTT. Throwaway script;
# ABOUTME: run against a real account (reads only). Env: PLACE_USERNAME / PLACE_PASSWORD.
import asyncio
import os
from getpass import getpass

import aiohttp

from place.auth.cognito_auth import CognitoAuth
from place.config import PlaceConfig
from place.exceptions import MfaRequired
from place.models import PlaceDeviceShadow
from place.provider import Provider


async def main() -> None:
    config = PlaceConfig.from_env()
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(os.environ["PLACE_USERNAME"], os.environ["PLACE_PASSWORD"])
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        for device in await Provider(auth).discover():
            shadow = PlaceDeviceShadow.from_shadow(device.shadow)
            print(f"{device.thing_name} (online={device.online}): {shadow}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Write `examples/quickstart.py` (live shadow via MQTT)**

```python
# examples/quickstart.py
# ABOUTME: Connect to PLACE, discover devices, and print a live shadow snapshot after
# ABOUTME: the initial shadow/get lands. Throwaway script; read-only against a real account.
import asyncio
import os
from getpass import getpass

import aiohttp

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.exceptions import MfaRequired


async def main() -> None:
    config = PlaceConfig.from_env()
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(os.environ["PLACE_USERNAME"], os.environ["PLACE_PASSWORD"])
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        async with PlaceClient.create(config, auth) as client:
            await asyncio.sleep(3)  # let the initial shadow/get responses arrive
            for device in client.devices.values():
                s = device.shadow
                print(
                    f"{device.thing_name}: online={device.online} "
                    f"smoke={s.smoke_alarm_status.name} co={s.co_alarm_status.name} "
                    f"co_ppm={s.co_ppm} temp={s.temperature_c} humidity={s.humidity}"
                )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Write `examples/watch_live.py` (stream updates + motion)**

```python
# examples/watch_live.py
# ABOUTME: Live PLACE watcher — streams shadow updates and household events, flagging
# ABOUTME: motionDetected pulses. Throwaway script; read-only. Set PLACE_HOUSEHOLD_IDS
# ABOUTME: (comma-separated) to receive the live motion firehose.
import asyncio
import os
from getpass import getpass

import aiohttp

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.exceptions import MfaRequired


async def main() -> None:
    config = PlaceConfig.from_env()
    household_ids = [h for h in os.environ.get("PLACE_HOUSEHOLD_IDS", "").split(",") if h]
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(os.environ["PLACE_USERNAME"], os.environ["PLACE_PASSWORD"])
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        client = PlaceClient.create(config, auth, household_ids=household_ids)
        client.on_event(
            lambda e: print(f"[event] {e.event_type} device={e.device_id} seq={e.seq}")
        )
        async with client:
            print(f"Watching {len(client.devices)} device(s). Ctrl-C to stop.")
            async for device in client.updates():
                motion = (
                    "  <-- MOTION"
                    if device.last_event is not None and device.last_event.is_motion
                    else ""
                )
                print(
                    f"[update] {device.name or device.thing_name} "
                    f"co_ppm={device.shadow.co_ppm} "
                    f"smoke={device.shadow.smoke_alarm_status.name}{motion}"
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 4: Sanity-check the scripts import cleanly (no run)**

Run: `uv run python -c "import ast; [ast.parse(open(p).read()) for p in ['examples/quickstart.py','examples/shadow_snapshot.py','examples/watch_live.py']]; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git status
git add examples/quickstart.py examples/shadow_snapshot.py examples/watch_live.py
git commit -m "docs: add async example scripts on PlaceClient"
```

---

### Task 18: Retire the sync transport + finalize packaging

Delete the replaced sync-era modules, trim `messages.py` to the pure helpers, ship `py.typed`, and clean up dependencies. Do this last so nothing imports a module mid-flight. **This removes code — confirm no live importer remains before deleting (grep step below).**

**Files:**
- Delete: `src/place/mqtt_client.py`, `src/place/events.py`, `tests/test_mqtt_client.py`, `tests/test_events.py`, `examples/mqtt_flow.py`, `examples/probe_real_device.py`, `examples/watch_events.py`, `examples/shadow_demo.py`
- Modify: `src/place/messages.py` (remove `from .mqtt_client import MqttClient` and the `PlaceMessages` class)
- Create: `src/place/py.typed` (empty)
- Modify: `pyproject.toml` (add `aiomqtt` if not already; drop `paho-mqtt`; ship `py.typed`)

- [ ] **Step 1: Confirm the retirement set has no live importers**

Run: `grep -rln "mqtt_client\|PlaceMessages\|HouseholdEventListener\|import MqttClient" src tests examples`
Expected: only `src/place/messages.py` (the import + class we are about to remove) — no other `src/` module, and no `tests/` file except `test_mqtt_client.py` / `test_events.py` (both being deleted). If anything else appears, stop and reconcile before deleting.

- [ ] **Step 2: Delete the sync-era modules, tests, and examples**

```bash
git rm src/place/mqtt_client.py src/place/events.py \
       tests/test_mqtt_client.py tests/test_events.py \
       examples/mqtt_flow.py examples/probe_real_device.py \
       examples/watch_events.py examples/shadow_demo.py
```

- [ ] **Step 3: Trim `messages.py`**

Remove the import line `from .mqtt_client import MqttClient` and delete the entire `class PlaceMessages:` block (its methods are superseded: `thing_name_from_topic` is now a module function, `subscribe_*`/`publish_shadow_get` are replaced by `PlaceConnection`). Keep `parse_payload`, `message_kind`, the topic-builder functions, `desired_shadow_update`, `describe_message`, and the module-level `thing_name_from_topic`.

- [ ] **Step 4: Ship `py.typed`**

```bash
: > src/place/py.typed
```

- [ ] **Step 5: Update `pyproject.toml`**

Ensure `[project].dependencies` includes `aiomqtt` (added in Task 9) and **no longer** lists `paho-mqtt` (aiomqtt depends on it transitively). Add package data so the marker ships:

```toml
[tool.setuptools.package-data]
place = ["py.typed"]
```

- [ ] **Step 6: Full verification**

Run: `uv run pytest -q`
Expected: PASS, pristine output (no warnings/errors), covering config, exceptions, credentials/gateway, cognito-auth, transport-signing, place-connection, device, client, plus the retained `test_messages.py` / `test_models.py` / `test_device_event.py` / `test_provider.py` / `test_auth.py`.

Run: `uv run basedpyright`
Expected: 0 errors. (If aiomqtt lacks stubs, resolve narrowly — e.g. a targeted `# pyright: ignore` on the aiomqtt import in `transport.py` — never a blanket suppression.)

- [ ] **Step 7: Commit**

```bash
git status
git add -u
git add src/place/py.typed pyproject.toml
git commit -m "refactor: retire sync MQTT transport and ship py.typed"
```

---

## Self-Review

**1. Spec coverage** — every design component maps to a task:
- `PlaceConfig` (spec §4.1) → Task 1. Exceptions (§4.2) → Task 2. `CognitoAuth`/MFA/refresh (§4.3) → Tasks 3–7. Transport/signer/aiomqtt/self-healing loop (§4.4) → Tasks 8–12. `PlaceDevice` (§4.5) → Task 13. `PlaceClient` facade + callbacks + `updates()` (§4.6) → Tasks 14–16. Examples (§9) → Task 17. Packaging/`py.typed` + sync retirement (§10) → Task 18. Read-only invariant (§7) is enforced throughout (only `shadow/get` + subscribe; `desired_shadow_update` stays an unused builder; `Provider.enable/disable` never surfaced).

**2. Placeholder scan** — no `TBD`/`TODO`/"handle errors appropriately". The single deliberately-empirical point (Task 9, Step 2: introspect aiomqtt's constructor) is a required discovery step with an explicit command, not a placeholder — it exists precisely to avoid inventing kwarg names.

**3. Type consistency** — names verified across tasks: `async_get_iot_credentials`/`async_get_access_token` (Tasks 6–7) are what `PlaceConnection`/`CognitoAuth` consume; `TransportFactory = Callable[[PlaceConfig, Credentials], MqttTransport]` (Task 9) matches `transport_factory=lambda cfg, creds: ...` (Tasks 10–14); `connection_factory(on_message, on_state)` (Task 14) matches `FakeConnection.__init__(on_message, on_state)` and `create()`'s factory; `add_subscription`/`add_connect_publish`/`publish`/`stop`/`run` are used identically by client and tests; `thing_name_from_topic` is one module function after Task 15; `Credentials.expiration` (Task 3) is read by `_seconds_until_refresh` (Task 12) and `async_get_iot_credentials` (Task 7).

**4. Open item flagged for live validation** — the household id(s) for the live-motion firehose are supplied explicitly (`household_ids`, from `PLACE_HOUSEHOLD_IDS`), NOT derived from `thing_name` (which carries no household UUID). If a future capture shows discovery returns the household id, wire it in then; until then the SDK works for shadow/telemetry without it and consumes the event firehose only when ids are provided. This matches how the prior working watcher obtained the id and avoids inventing a derivation.
