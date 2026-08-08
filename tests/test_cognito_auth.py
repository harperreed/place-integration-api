# ABOUTME: Tests for CognitoAuth — SRP login token storage, MFA-required signaling,
# ABOUTME: and MFA-completion login, driven against a hand-written FakeGateway.
from __future__ import annotations

from typing import Any

import pytest

from place.auth.cognito_auth import CognitoAuth
from place.config import PlaceConfig
from place.exceptions import MfaRequired, PlaceAuthError


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
