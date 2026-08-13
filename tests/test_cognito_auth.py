# ABOUTME: Tests for CognitoAuth — SRP login token storage, MFA-required signaling,
# ABOUTME: and MFA-completion login, driven against a hand-written FakeGateway.
from __future__ import annotations

import asyncio
import dataclasses
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from place.auth.cognito_auth import CognitoAuth
from place.config import PlaceConfig
from place.exceptions import (
    MfaRequired,
    PlaceAuthError,
    PlaceError,
    PlaceInvalidAuthError,
    PlaceTransientAuthError,
)
from place.models import Credentials


class FakeGateway:
    """In-memory CognitoGateway: scripted login/refresh/mfa, records calls."""

    def __init__(
        self, *, login=None, mfa=None, refresh=None, creds=None, refresh_error=None
    ) -> None:
        self._login = login or {}
        self._mfa = mfa or {}
        self._refresh = refresh or {}
        self._creds = creds
        self._refresh_error = refresh_error
        self.login_calls = 0
        self.refresh_calls = 0
        self.iot_calls = 0
        self.login_args: list[tuple[str, str]] = []
        self.refresh_args: list[str] = []

    def srp_login(self, username: str, password: str) -> dict[str, Any]:
        self.login_calls += 1
        self.login_args.append((username, password))
        return self._login

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.refresh_calls += 1
        self.refresh_args.append(refresh_token)
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh

    def respond_mfa(self, *, challenge_name, session, username, code) -> dict[str, Any]:
        return self._mfa

    def iot_credentials(self, id_token: str, access_token: str):
        self.iot_calls += 1
        if self._creds is None:
            return None
        return dataclasses.replace(self._creds)


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
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert await auth.async_get_access_token() == "access-1"


async def test_authenticate_raises_mfa_required() -> None:
    gw = FakeGateway(login={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-9"})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(MfaRequired) as excinfo:
        await auth.authenticate("alice", "pw")
    assert excinfo.value.challenge_name == "SOFTWARE_TOKEN_MFA"
    assert excinfo.value.session == "sess-9"


async def test_submit_mfa_completes_login() -> None:
    gw = FakeGateway(
        login={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-9"},
        mfa={"AuthenticationResult": _auth_result(AccessToken="access-mfa")},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(MfaRequired):
        await auth.authenticate("alice", "pw")
    await auth.submit_mfa("123456")

    assert await auth.async_get_access_token() == "access-mfa"


async def test_authenticate_raises_mfa_required_for_sms_challenge() -> None:
    gw = FakeGateway(login={"ChallengeName": "SMS_MFA", "Session": "sess-sms"})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(MfaRequired) as excinfo:
        await auth.authenticate("alice", "pw")
    assert excinfo.value.challenge_name == "SMS_MFA"
    assert excinfo.value.session == "sess-sms"


async def test_authenticate_retains_refresh_token_when_response_omits_it() -> None:
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")
    assert auth._refresh_token == "refresh-1"

    gw._login = {
        "AuthenticationResult": _auth_result(AccessToken="access-2", RefreshToken=None)
    }
    await auth.authenticate("alice", "pw")

    assert auth._access_token == "access-2"
    assert auth._refresh_token == "refresh-1"


async def test_async_get_access_token_raises_when_not_authenticated() -> None:
    gw = FakeGateway()
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(PlaceAuthError):
        await auth.async_get_access_token()


async def test_submit_mfa_raises_when_no_challenge_pending() -> None:
    gw = FakeGateway()
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(PlaceAuthError):
        await auth.submit_mfa("123456")


async def test_access_token_refreshes_when_expired() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()},
        refresh={"AccessToken": "access-2", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    auth._access_token_expiry = 0.0  # force staleness
    assert await auth.async_get_access_token() == "access-2"
    assert gw.refresh_calls == 1


async def test_concurrent_refresh_is_single_flight() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()},
        refresh={"AccessToken": "access-2", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")
    auth._access_token_expiry = 0.0

    a, b = await asyncio.gather(
        auth.async_get_access_token(), auth.async_get_access_token()
    )
    assert a == b == "access-2"
    assert gw.refresh_calls == 1  # second caller saw the freshly-refreshed token


async def test_stale_token_without_refresh_token_returned_as_is() -> None:
    gw = FakeGateway(login={"AuthenticationResult": _auth_result(RefreshToken=None)})
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    auth._access_token_expiry = 0.0  # force staleness
    assert await auth.async_get_access_token() == "access-1"
    assert (
        gw.refresh_calls == 0
    )  # no refresh token → cannot refresh, returns the stale token


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
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    first = await auth.async_get_iot_credentials()
    second = await auth.async_get_iot_credentials()
    assert first is second  # served from cache


async def test_iot_credentials_refresh_when_expired() -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()}, creds=_creds(past))
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    first = await auth.async_get_iot_credentials()
    second = await auth.async_get_iot_credentials()
    assert first is not second  # stale creds forced a re-exchange each call


async def test_iot_credentials_single_flight_under_concurrency() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(login={"AuthenticationResult": _auth_result()}, creds=_creds(far))
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    a, b = await asyncio.gather(
        auth.async_get_iot_credentials(), auth.async_get_iot_credentials()
    )
    assert a is b  # single-flight: one exchange, both callers get the one cached object
    assert (
        gw.iot_calls == 1
    )  # second caller saw the freshly-cached creds, not a second exchange


async def test_iot_credentials_uses_expiry_fallback_when_response_has_none() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()},
        creds=Credentials(
            access_key_id="AKIA",
            secret_access_key="s",
            session_token="t",
            identity_id="idid",
        ),  # expiration=None → exercises the url_expire_sec fallback
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "pw")

    first = await auth.async_get_iot_credentials()
    second = await auth.async_get_iot_credentials()
    assert (
        first is second
    )  # fallback expiry (now + url_expire_sec) keeps creds fresh → served from cache
    assert gw.iot_calls == 1


class FakeCache:
    """In-memory TokenCache: scripted load, records saves/clears."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saves = 0
        self.clears = 0

    def load(self) -> dict[str, Any] | None:
        return self.data

    def save(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self.saves += 1

    def clear(self) -> None:
        self.data = None
        self.clears += 1


class FailingLoadCache(FakeCache):
    """TokenCache whose load failure contains a secret-like canary."""

    def load(self) -> dict[str, Any] | None:  # pyright: ignore[reportImplicitOverride, reportExplicitAny]
        raise RuntimeError("cache-canary-secret")


async def test_authenticate_from_cache_uses_refresh_token_without_srp() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")},
        refresh={"AccessToken": "access-cached", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    cache = FakeCache({"username": "alice", "refresh_token": "rt-cached"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate_from_cache("alice")

    assert await auth.async_get_access_token() == "access-cached"
    assert auth._refresh_token == "rt-cached"  # pyright: ignore[reportPrivateUsage]
    assert gw.refresh_calls == 1
    assert gw.refresh_args == ["rt-cached"]
    assert gw.login_calls == 0


async def test_authenticate_from_cache_failure_preserves_existing_account_state() -> (
    None
):
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    vars(auth)["_mfa_challenge"] = "SOFTWARE_TOKEN_MFA"
    vars(auth)["_mfa_session"] = "old-mfa-session"
    cache.data = {"username": "new-account", "refresh_token": "rt-new"}
    gw._refresh_error = PlaceTransientAuthError("refresh temporarily unavailable")
    old_state = vars(auth).copy()

    with pytest.raises(PlaceTransientAuthError):
        await auth.authenticate_from_cache("new-account")

    assert vars(auth) == old_state


async def test_authenticate_from_cache_switches_account_bound_state() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    vars(auth)["_mfa_challenge"] = "SOFTWARE_TOKEN_MFA"
    vars(auth)["_mfa_session"] = "old-mfa-session"
    cache.data = {"username": "new-account", "refresh_token": "rt-new"}
    gw._refresh = {
        "AccessToken": "access-new",
        "IdToken": "id-new",
        "ExpiresIn": 3600,
    }

    await auth.authenticate_from_cache("new-account")

    state = vars(auth)
    assert state["_username"] == "new-account"
    assert state["_access_token"] == "access-new"
    assert state["_id_token"] == "id-new"
    assert state["_refresh_token"] == "rt-new"
    assert state["_iot_creds"] is None
    assert state["_iot_creds_expiry"] is None
    assert state["_mfa_challenge"] is None
    assert state["_mfa_session"] is None


async def test_authenticate_from_cache_clears_same_account_mfa_state() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        refresh={
            "AccessToken": "access-new",
            "IdToken": "id-new",
            "ExpiresIn": 3600,
        },
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "old-password")
    old_iot_creds = await auth.async_get_iot_credentials()
    vars(auth)["_mfa_challenge"] = "SOFTWARE_TOKEN_MFA"
    vars(auth)["_mfa_session"] = "stale-mfa-session"
    cache.data = {"username": "alice", "refresh_token": "rt-cached"}

    await auth.authenticate_from_cache("alice")

    state = vars(auth)
    assert state["_mfa_challenge"] is None
    assert state["_mfa_session"] is None
    assert state["_iot_creds"] is old_iot_creds


@pytest.mark.parametrize(
    "cached",
    [
        None,
        {},
        {"username": "bob", "refresh_token": "rt-bob"},
        {"username": "alice"},
        {"username": "alice", "refresh_token": ""},
        {"username": "alice", "refresh_token": 123},
    ],
)
async def test_authenticate_from_cache_rejects_invalid_cache_without_gateway_calls(
    cached: dict[str, Any] | None,  # pyright: ignore[reportExplicitAny]
) -> None:
    gw = FakeGateway()
    auth = CognitoAuth(
        PlaceConfig(),
        websession=object(),  # pyright: ignore[reportArgumentType]
        gateway=gw,  # pyright: ignore[reportArgumentType]
        token_cache=FakeCache(cached),
    )

    with pytest.raises(PlaceInvalidAuthError):
        await auth.authenticate_from_cache("alice")

    assert gw.refresh_calls == 0
    assert gw.login_calls == 0


async def test_authenticate_from_cache_requires_a_configured_cache() -> None:
    gw = FakeGateway()
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]

    with pytest.raises(
        PlaceInvalidAuthError, match="no refresh-token cache configured"
    ):
        await auth.authenticate_from_cache("alice")

    assert gw.refresh_calls == 0
    assert gw.login_calls == 0


async def test_authenticate_from_cache_propagates_rejected_refresh_without_srp() -> (
    None
):
    error = PlaceInvalidAuthError("refresh rejected")
    gw = FakeGateway(refresh_error=error)
    cache = FakeCache({"username": "alice", "refresh_token": "rt-stale"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    with pytest.raises(PlaceInvalidAuthError) as caught:
        await auth.authenticate_from_cache("alice")

    assert caught.value is error
    assert gw.refresh_calls == 1
    assert gw.login_calls == 0


async def test_authenticate_from_cache_redacts_cache_load_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gw = FakeGateway()
    auth = CognitoAuth(
        PlaceConfig(),
        websession=object(),  # pyright: ignore[reportArgumentType]
        gateway=gw,  # pyright: ignore[reportArgumentType]
        token_cache=FailingLoadCache(),
    )

    with pytest.raises(PlaceTransientAuthError) as caught:
        await auth.authenticate_from_cache("alice")

    assert str(caught.value) == "refresh-token cache unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "cache-canary-secret" not in caplog.text
    assert gw.refresh_calls == 0
    assert gw.login_calls == 0


async def test_authenticate_from_cache_propagates_transient_refresh_without_srp() -> (
    None
):
    error = PlaceTransientAuthError("refresh temporarily unavailable")
    gw = FakeGateway(refresh_error=error)
    cache = FakeCache({"username": "alice", "refresh_token": "rt-cached"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    with pytest.raises(PlaceTransientAuthError) as caught:
        await auth.authenticate_from_cache("alice")

    assert caught.value is error
    assert gw.refresh_calls == 1
    assert gw.login_calls == 0


async def test_authenticate_uses_cached_refresh_token_and_skips_srp() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")},
        refresh={"AccessToken": "access-cached", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    cache = FakeCache({"username": "alice", "refresh_token": "rt-cached"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert await auth.async_get_access_token() == "access-cached"
    assert gw.refresh_calls == 1
    assert gw.login_calls == 0  # cache hit → SRP (and its MFA prompt) skipped entirely


async def test_cached_login_threads_refresh_token_into_memory() -> None:
    # REFRESH_TOKEN_AUTH responses omit the refresh token; the cached one must survive so
    # subsequent in-process refreshes still work.
    gw = FakeGateway(
        refresh={"AccessToken": "access-cached", "IdToken": "id-2", "ExpiresIn": 3600},
    )
    cache = FakeCache({"username": "alice", "refresh_token": "rt-cached"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert auth._refresh_token == "rt-cached"


async def test_authenticate_falls_back_to_srp_when_cached_refresh_rejected() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")},
        refresh_error=PlaceInvalidAuthError("token refresh failed: expired"),
    )
    cache = FakeCache({"username": "alice", "refresh_token": "rt-stale"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert await auth.async_get_access_token() == "access-srp"
    assert gw.refresh_calls == 1  # tried the cached token...
    assert gw.login_calls == 1  # ...then fell back to SRP


async def test_authenticate_propagates_transient_cached_refresh_without_srp() -> None:
    error = PlaceTransientAuthError("refresh temporarily unavailable")
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")},
        refresh_error=error,
    )
    cache = FakeCache({"username": "alice", "refresh_token": "rt-cached"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    with pytest.raises(PlaceTransientAuthError) as caught:
        await auth.authenticate("alice", "pw")

    assert caught.value is error
    assert gw.refresh_calls == 1
    assert gw.login_calls == 0


async def test_authenticate_cached_account_switch_commits_all_bound_state() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    auth._mfa_challenge = "SOFTWARE_TOKEN_MFA"  # pyright: ignore[reportPrivateUsage]
    auth._mfa_session = "old-session"  # pyright: ignore[reportPrivateUsage]
    cache.data = {"username": "new-account", "refresh_token": "rt-new"}
    gw._refresh = {
        "AccessToken": "access-new",
        "IdToken": "id-new",
        "ExpiresIn": 3600,
    }

    await auth.authenticate("new-account", "new-password")

    assert auth._username == "new-account"  # pyright: ignore[reportPrivateUsage]
    assert auth._access_token == "access-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._id_token == "id-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._refresh_token == "rt-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds is None  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds_expiry is None  # pyright: ignore[reportPrivateUsage]
    assert auth._mfa_challenge is None  # pyright: ignore[reportPrivateUsage]
    assert auth._mfa_session is None  # pyright: ignore[reportPrivateUsage]
    assert gw.login_calls == 1


async def test_authenticate_transient_cached_switch_preserves_all_state() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    auth._mfa_challenge = "SOFTWARE_TOKEN_MFA"  # pyright: ignore[reportPrivateUsage]
    auth._mfa_session = "old-session"  # pyright: ignore[reportPrivateUsage]
    cache.data = {"username": "new-account", "refresh_token": "rt-new"}
    gw._refresh_error = PlaceTransientAuthError("temporary")
    old_state = vars(auth).copy()

    with pytest.raises(PlaceTransientAuthError):
        await auth.authenticate("new-account", "new-password")

    assert vars(auth) == old_state
    assert gw.login_calls == 1


async def test_authenticate_invalid_cache_then_srp_switches_all_bound_state() -> None:
    far = datetime.now(timezone.utc) + timedelta(hours=5)
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(far),
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    auth._mfa_challenge = "SOFTWARE_TOKEN_MFA"  # pyright: ignore[reportPrivateUsage]
    auth._mfa_session = "old-session"  # pyright: ignore[reportPrivateUsage]
    cache.data = {"username": "new-account", "refresh_token": "rt-stale"}
    gw._refresh_error = PlaceInvalidAuthError("rejected")
    gw._login = {
        "AuthenticationResult": _auth_result(
            AccessToken="access-new", IdToken="id-new", RefreshToken="rt-new"
        )
    }

    await auth.authenticate("new-account", "new-password")

    assert auth._username == "new-account"  # pyright: ignore[reportPrivateUsage]
    assert auth._access_token == "access-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._id_token == "id-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._refresh_token == "rt-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds is None  # pyright: ignore[reportPrivateUsage]
    assert auth._mfa_challenge is None  # pyright: ignore[reportPrivateUsage]
    assert gw.login_calls == 2


async def test_malformed_srp_result_does_not_partially_switch_account() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(datetime.now(timezone.utc) + timedelta(hours=5)),
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    old_state = vars(auth).copy()
    gw._login = {"AuthenticationResult": {"AccessToken": "incomplete-new-token"}}

    with pytest.raises(KeyError):
        await auth.authenticate("new-account", "new-password")

    assert vars(auth) == old_state


async def test_malformed_mfa_challenge_does_not_partially_switch_account() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(datetime.now(timezone.utc) + timedelta(hours=5)),
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    old_state = vars(auth).copy()
    gw._login = {"ChallengeName": "SOFTWARE_TOKEN_MFA"}

    with pytest.raises(KeyError):
        await auth.authenticate("new-account", "new-password")

    assert vars(auth) == old_state


async def test_new_account_mfa_replaces_old_account_with_pending_state() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-old")},
        creds=_creds(datetime.now(timezone.utc) + timedelta(hours=5)),
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    await auth.async_get_iot_credentials()
    gw._login = {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "new-session"}

    with pytest.raises(MfaRequired) as caught:
        await auth.authenticate("new-account", "new-password")

    assert caught.value.username == "new-account"
    assert auth._username == "new-account"  # pyright: ignore[reportPrivateUsage]
    assert auth._access_token is None  # pyright: ignore[reportPrivateUsage]
    assert auth._id_token is None  # pyright: ignore[reportPrivateUsage]
    assert auth._refresh_token is None  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds is None  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds_expiry is None  # pyright: ignore[reportPrivateUsage]
    assert auth._mfa_challenge == "SOFTWARE_TOKEN_MFA"  # pyright: ignore[reportPrivateUsage]
    assert auth._mfa_session == "new-session"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(PlaceAuthError, match="not authenticated"):
        await auth.async_get_access_token()
    with pytest.raises(PlaceAuthError, match="not authenticated"):
        await auth.async_get_iot_credentials()


class BlockingCredentialGateway(FakeGateway):
    """Block one old-principal exchange while a replacement login commits."""

    def __init__(self) -> None:
        super().__init__(login={"AuthenticationResult": _auth_result()})
        self.started = threading.Event()
        self.release = threading.Event()

    def iot_credentials(self, id_token: str, access_token: str) -> Credentials:
        self.iot_calls += 1
        if id_token == "id-1":
            self.started.set()
            assert self.release.wait(timeout=5)
        return Credentials(
            access_key_id=f"key-{id_token}",
            secret_access_key="secret",
            session_token="session",
            identity_id=f"identity-{id_token}",
            expiration=datetime.now(timezone.utc) + timedelta(hours=5),
        )


async def test_old_iot_result_cannot_populate_after_account_switch() -> None:
    gw = BlockingCredentialGateway()
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")

    credential_task = asyncio.create_task(auth.async_get_iot_credentials())
    assert await asyncio.to_thread(gw.started.wait, 5)
    gw._login = {
        "AuthenticationResult": _auth_result(
            AccessToken="access-new", IdToken="id-new", RefreshToken="refresh-new"
        )
    }
    await auth.authenticate("new-account", "new-password")
    gw.release.set()

    credentials = await credential_task

    assert credentials.identity_id == "identity-id-new"
    assert auth._iot_creds is credentials  # pyright: ignore[reportPrivateUsage]
    assert gw.iot_calls == 2


class BlockingRefreshGateway(FakeGateway):
    """Block an old-principal refresh while a replacement login commits."""

    def __init__(self) -> None:
        super().__init__(login={"AuthenticationResult": _auth_result()})
        self.started = threading.Event()
        self.release = threading.Event()

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.refresh_calls += 1
        self.refresh_args.append(refresh_token)
        self.started.set()
        assert self.release.wait(timeout=5)
        return {
            "AccessToken": "access-refreshed-old",
            "IdToken": "id-refreshed-old",
            "ExpiresIn": 3600,
        }


async def test_old_refresh_result_cannot_overwrite_account_switch() -> None:
    gw = BlockingRefreshGateway()
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    auth._access_token_expiry = 0.0  # pyright: ignore[reportPrivateUsage]

    refresh_task = asyncio.create_task(auth.async_get_access_token())
    assert await asyncio.to_thread(gw.started.wait, 5)
    try:
        gw._login = {
            "AuthenticationResult": _auth_result(
                AccessToken="access-new",
                IdToken="id-new",
                RefreshToken="refresh-new",
            )
        }
        await auth.authenticate("new-account", "new-password")
    finally:
        gw.release.set()

    assert await refresh_task == "access-new"
    assert auth._access_token == "access-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._id_token == "id-new"  # pyright: ignore[reportPrivateUsage]


class BlockingRefreshErrorGateway(FakeGateway):
    """Block one old-principal refresh, then raise its scripted typed error."""

    def __init__(self, error: PlaceError) -> None:
        super().__init__(login={"AuthenticationResult": _auth_result()})
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        self.refresh_calls += 1
        self.refresh_args.append(refresh_token)
        self.started.set()
        assert self.release.wait(timeout=5)
        raise self.error


@pytest.mark.parametrize("error_type", (PlaceInvalidAuthError, PlaceTransientAuthError))
async def test_old_refresh_error_is_discarded_after_account_switch(
    error_type: type[PlaceError],
) -> None:
    stale_error = error_type("stale old-principal refresh")
    gw = BlockingRefreshErrorGateway(stale_error)
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")
    auth._access_token_expiry = 0.0  # pyright: ignore[reportPrivateUsage]

    refresh_task = asyncio.create_task(auth.async_get_access_token())
    assert await asyncio.to_thread(gw.started.wait, 5)
    try:
        gw._login = {
            "AuthenticationResult": _auth_result(
                AccessToken="access-new",
                IdToken="id-new",
                RefreshToken="refresh-new",
            )
        }
        await auth.authenticate("new-account", "new-password")
    finally:
        gw.release.set()

    assert await refresh_task == "access-new"
    assert auth._username == "new-account"  # pyright: ignore[reportPrivateUsage]
    assert auth._access_token == "access-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._id_token == "id-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._refresh_token == "refresh-new"  # pyright: ignore[reportPrivateUsage]
    assert gw.refresh_calls == 1


class BlockingCredentialErrorGateway(FakeGateway):
    """Block one old-principal IoT exchange, then raise its scripted typed error."""

    def __init__(self, error: PlaceError) -> None:
        super().__init__(login={"AuthenticationResult": _auth_result()})
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()

    def iot_credentials(self, id_token: str, access_token: str) -> Credentials:
        self.iot_calls += 1
        if id_token == "id-1":
            self.started.set()
            assert self.release.wait(timeout=5)
            raise self.error
        return Credentials(
            access_key_id=f"key-{id_token}",
            secret_access_key="secret",
            session_token="session",
            identity_id=f"identity-{id_token}",
            expiration=datetime.now(timezone.utc) + timedelta(hours=5),
        )


@pytest.mark.parametrize(
    "error_type", (PlaceAuthError, PlaceInvalidAuthError, PlaceTransientAuthError)
)
async def test_old_iot_error_is_discarded_after_account_switch(
    error_type: type[PlaceError],
) -> None:
    stale_error = error_type("stale old-principal credential exchange")
    gw = BlockingCredentialErrorGateway(stale_error)
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("old-account", "old-password")

    credential_task = asyncio.create_task(auth.async_get_iot_credentials())
    assert await asyncio.to_thread(gw.started.wait, 5)
    try:
        gw._login = {
            "AuthenticationResult": _auth_result(
                AccessToken="access-new",
                IdToken="id-new",
                RefreshToken="refresh-new",
            )
        }
        await auth.authenticate("new-account", "new-password")
    finally:
        gw.release.set()

    credentials = await credential_task

    assert credentials.identity_id == "identity-id-new"
    assert auth._username == "new-account"  # pyright: ignore[reportPrivateUsage]
    assert auth._access_token == "access-new"  # pyright: ignore[reportPrivateUsage]
    assert auth._iot_creds is credentials  # pyright: ignore[reportPrivateUsage]
    assert gw.iot_calls == 2


@pytest.mark.parametrize(
    "error",
    (
        PlaceInvalidAuthError("current refresh rejected"),
        PlaceTransientAuthError("current refresh unavailable"),
        RuntimeError("programmer error"),
    ),
)
async def test_current_refresh_error_propagates_as_same_object(
    error: Exception,
) -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result()}, refresh_error=error
    )
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "password")
    auth._access_token_expiry = 0.0  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(type(error)) as caught:
        await auth.async_get_access_token()

    assert caught.value is error
    assert gw.refresh_calls == 1


class CredentialFailureGateway(FakeGateway):
    """Raise a scripted IoT exchange exception without changing auth state."""

    def __init__(self, error: Exception) -> None:
        super().__init__(login={"AuthenticationResult": _auth_result()})
        self.error = error

    def iot_credentials(self, id_token: str, access_token: str) -> Credentials:
        self.iot_calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error", (PlaceAuthError("current IoT error"), RuntimeError("programmer error"))
)
async def test_current_iot_error_propagates_as_same_object(error: Exception) -> None:
    gw = CredentialFailureGateway(error)
    auth = CognitoAuth(PlaceConfig(), websession=object(), gateway=gw)  # pyright: ignore[reportArgumentType]
    await auth.authenticate("alice", "password")

    with pytest.raises(type(error)) as caught:
        await auth.async_get_iot_credentials()

    assert caught.value is error
    assert gw.iot_calls == 1


@pytest.mark.parametrize("refresh_token", ["", 123])
async def test_authenticate_uses_srp_for_malformed_cached_refresh_token(
    refresh_token: object,
) -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")},
    )
    cache = FakeCache({"username": "alice", "refresh_token": refresh_token})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "supplied-password")

    assert gw.refresh_calls == 0
    assert gw.login_calls == 1
    assert gw.login_args == [("alice", "supplied-password")]
    assert await auth.async_get_access_token() == "access-srp"


async def test_authenticate_persists_refresh_token_after_srp_login() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(RefreshToken="rt-fresh")}
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert cache.data == {"username": "alice", "refresh_token": "rt-fresh"}


async def test_authenticate_ignores_cache_for_a_different_user() -> None:
    gw = FakeGateway(
        login={"AuthenticationResult": _auth_result(AccessToken="access-srp")}
    )
    cache = FakeCache({"username": "bob", "refresh_token": "rt-bob"})
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    await auth.authenticate("alice", "pw")

    assert gw.refresh_calls == 0  # username mismatch → cache ignored
    assert gw.login_calls == 1
    assert await auth.async_get_access_token() == "access-srp"


async def test_mfa_login_persists_only_after_completion() -> None:
    gw = FakeGateway(
        login={"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-9"},
        mfa={
            "AuthenticationResult": _auth_result(
                AccessToken="access-mfa", RefreshToken="rt-mfa"
            )
        },
    )
    cache = FakeCache()
    auth = CognitoAuth(
        PlaceConfig(), websession=object(), gateway=gw, token_cache=cache
    )  # pyright: ignore[reportArgumentType]

    with pytest.raises(MfaRequired):
        await auth.authenticate("alice", "pw")
    assert cache.data is None  # nothing persisted while the MFA challenge is pending
    await auth.submit_mfa("123456")

    assert cache.data == {"username": "alice", "refresh_token": "rt-mfa"}
