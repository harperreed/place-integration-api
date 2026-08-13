# ABOUTME: Tests for the Cognito identity-credential exchange — that get_iot_credentials
# ABOUTME: maps the identity response fields and captures the credential Expiration.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from place.auth import aws_srp, cognito_gateway, srp_auth
from place.auth.cognito_gateway import RealCognitoGateway
from place.auth.srp_auth import get_iot_credentials, refresh_tokens, respond_mfa
from place.config import PlaceConfig
from place.exceptions import (
    PlaceAuthError,
    PlaceInvalidAuthError,
    PlaceTransientAuthError,
)
from place.models import Credentials


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


def _client_error(code: str, message: str = "TOKEN-CANARY") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "InitiateAuth",
    )


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
    assert (
        "SECRET_HASH" not in kwargs["AuthParameters"]
    )  # this app client has no secret


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


def test_real_gateway_passes_config_fields_to_srp_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RealCognitoGateway must forward the correct PlaceConfig field to each srp_auth call."""
    calls: dict[str, dict[str, Any]] = {}

    def _record(name: str, retval: Any):
        def _fn(*args: Any, **kwargs: Any) -> Any:
            calls[name] = {"args": args, "kwargs": kwargs}
            return retval

        return _fn

    monkeypatch.setattr(
        cognito_gateway.srp_auth, "get_tokens_via_srp", _record("srp_login", {"ok": 1})
    )
    monkeypatch.setattr(
        cognito_gateway.srp_auth, "refresh_tokens", _record("refresh", {"ok": 2})
    )
    monkeypatch.setattr(
        cognito_gateway.srp_auth, "respond_mfa", _record("mfa", {"ok": 3})
    )
    monkeypatch.setattr(
        cognito_gateway.srp_auth, "get_iot_credentials", _record("iot", "CREDS")
    )

    config = PlaceConfig(
        region="R-test",
        cognito_user_pool_id="UP-test",
        cognito_client_id="CID-test",
        cognito_identity_pool_id="IP-test",
    )
    gw = RealCognitoGateway(config)

    gw.srp_login("alice", "pw")
    assert calls["srp_login"]["kwargs"] == {
        "user_pool_id": "UP-test",
        "client_id": "CID-test",
        "username": "alice",
        "password": "pw",
        "region": "R-test",
    }

    gw.refresh("refresh-xyz")
    assert calls["refresh"]["args"] == ("refresh-xyz",)
    assert calls["refresh"]["kwargs"] == {"region": "R-test", "client_id": "CID-test"}

    gw.respond_mfa(challenge_name="SMS_MFA", session="s", username="alice", code="000")
    assert calls["mfa"]["kwargs"] == {
        "challenge_name": "SMS_MFA",
        "session": "s",
        "username": "alice",
        "code": "000",
        "region": "R-test",
        "client_id": "CID-test",
    }

    creds = gw.iot_credentials("id-tok", "acc-tok")
    assert creds == "CREDS"
    assert calls["iot"]["args"] == ("id-tok", "acc-tok")
    assert calls["iot"]["kwargs"] == {
        "region": "R-test",
        "user_pool_id": "UP-test",
        "identity_pool_id": "IP-test",
    }


def test_respond_mfa_uses_sms_code_key_for_sms_challenge() -> None:
    client = _FakeIdpClient(respond={"AuthenticationResult": {"AccessToken": "a"}})
    respond_mfa(
        challenge_name="SMS_MFA",
        session="sess",
        username="bob",
        code="654321",
        region="us-east-2",
        client_id="cid",
        cognito_idp_client=client,
    )
    name, kwargs = client.calls[0]
    assert name == "respond_to_auth_challenge"
    assert kwargs["ChallengeName"] == "SMS_MFA"
    assert kwargs["ChallengeResponses"] == {"USERNAME": "bob", "SMS_MFA_CODE": "654321"}


def test_iot_credentials_translates_botocore_to_place_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> Credentials:
        raise ClientError(
            {"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}},
            "GetCredentialsForIdentity",
        )

    monkeypatch.setattr(srp_auth, "get_iot_credentials", _boom)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError):
        gateway.iot_credentials("id-token", "access-token")


def test_refresh_translates_botocore_to_place_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "expired"}},
            "InitiateAuth",
        )

    monkeypatch.setattr(srp_auth, "refresh_tokens", _boom)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError):
        gateway.refresh("some-refresh-token")


def test_srp_login_translates_botocore_to_place_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "bad password"}},
            "InitiateAuth",
        )

    monkeypatch.setattr(srp_auth, "get_tokens_via_srp", _boom)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError):
        gateway.srp_login("alice", "pw")


def test_respond_mfa_translates_botocore_to_place_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise ClientError(
            {"Error": {"Code": "CodeMismatchException", "Message": "wrong code"}},
            "RespondToAuthChallenge",
        )

    monkeypatch.setattr(srp_auth, "respond_mfa", _boom)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError):
        gateway.respond_mfa(
            challenge_name="SOFTWARE_TOKEN_MFA",
            session="s",
            username="alice",
            code="123456",
        )


def test_refresh_classifies_rejected_token_without_leaking_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _client_error("NotAuthorizedException")

    monkeypatch.setattr(srp_auth, "refresh_tokens", _reject)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceInvalidAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert str(caught.value) == "token refresh rejected"
    assert "TOKEN-CANARY" not in str(caught.value)
    assert "REFRESH-TOKEN-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "code",
    (
        "TooManyRequestsException",
        "InternalErrorException",
        "ExternalServiceException",
    ),
)
def test_refresh_classifies_transient_service_failures_without_leaking_response(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _client_error(code)

    monkeypatch.setattr(srp_auth, "refresh_tokens", _fail)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceTransientAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert str(caught.value) == f"token refresh temporarily failed ({code})"
    assert "TOKEN-CANARY" not in str(caught.value)
    assert "REFRESH-TOKEN-CANARY" not in str(caught.value)


@pytest.mark.parametrize(
    "code",
    (
        "NotAuthorizedException",
        "UserNotFoundException",
        "PasswordResetRequiredException",
        "UserNotConfirmedException",
    ),
)
def test_srp_login_classifies_credential_rejections(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    def _reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _client_error(code)

    monkeypatch.setattr(srp_auth, "get_tokens_via_srp", _reject)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceInvalidAuthError) as caught:
        _ = gateway.srp_login("alice", "PASSWORD-CANARY")

    assert str(caught.value) == "srp login rejected"
    assert "TOKEN-CANARY" not in str(caught.value)
    assert "PASSWORD-CANARY" not in str(caught.value)


def test_srp_login_classifies_forced_password_change_without_secret_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _force_change(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise aws_srp.ForceChangePasswordException("SECRET-CANARY ACCOUNT-CANARY")

    monkeypatch.setattr(srp_auth, "get_tokens_via_srp", _force_change)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceInvalidAuthError) as caught:
        _ = gateway.srp_login("ACCOUNT-CANARY", "PASSWORD-CANARY")

    assert str(caught.value) == "srp login rejected"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CANARY" not in caplog.text


def test_srp_login_classifies_unsupported_challenge_without_secret_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _unsupported(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise NotImplementedError("SECRET-CANARY ACCOUNT-CANARY")

    monkeypatch.setattr(srp_auth, "get_tokens_via_srp", _unsupported)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError) as caught:
        _ = gateway.srp_login("ACCOUNT-CANARY", "PASSWORD-CANARY")

    assert type(caught.value) is PlaceAuthError
    assert str(caught.value) == "srp login failed (unsupported challenge)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "CANARY" not in caplog.text


def test_srp_login_does_not_translate_unknown_programmer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ValueError("programmer contract violation")

    def _bug(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise source

    monkeypatch.setattr(srp_auth, "get_tokens_via_srp", _bug)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(ValueError) as caught:
        _ = gateway.srp_login("alice", "password")

    assert caught.value is source


@pytest.mark.parametrize(
    "code",
    (
        "CodeMismatchException",
        "ExpiredCodeException",
        "NotAuthorizedException",
        "UserNotFoundException",
    ),
)
def test_mfa_classifies_challenge_rejections(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    def _reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _client_error(code)

    monkeypatch.setattr(srp_auth, "respond_mfa", _reject)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceInvalidAuthError) as caught:
        _ = gateway.respond_mfa(
            challenge_name="SOFTWARE_TOKEN_MFA",
            session="SESSION-CANARY",
            username="alice",
            code="123456",
        )

    assert str(caught.value) == "mfa response rejected"
    assert "TOKEN-CANARY" not in str(caught.value)
    assert "SESSION-CANARY" not in str(caught.value)


def test_iot_not_authorized_remains_retryable_plain_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _reject(*_args: object, **_kwargs: object) -> Credentials:
        raise _client_error("NotAuthorizedException")

    monkeypatch.setattr(srp_auth, "get_iot_credentials", _reject)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError) as caught:
        _ = gateway.iot_credentials("ID-TOKEN-CANARY", "ACCESS-TOKEN-CANARY")

    assert type(caught.value) is PlaceAuthError
    assert (
        str(caught.value) == "iot credential exchange failed (NotAuthorizedException)"
    )
    assert "TOKEN-CANARY" not in str(caught.value)


def test_unknown_client_error_remains_retryable_plain_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise _client_error("FutureServiceException")

    monkeypatch.setattr(srp_auth, "refresh_tokens", _fail)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert type(caught.value) is PlaceAuthError
    assert str(caught.value) == "token refresh failed (FutureServiceException)"
    assert "TOKEN-CANARY" not in str(caught.value)


def test_botocore_failure_is_transient_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise BotoCoreError()

    monkeypatch.setattr(srp_auth, "refresh_tokens", _fail)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceTransientAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert str(caught.value) == "token refresh temporarily failed (BotoCoreError)"
    assert "REFRESH-TOKEN-CANARY" not in str(caught.value)


def test_client_error_translation_drops_secret_bearing_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _client_error("NotAuthorizedException", "SECRET-CANARY")

    def _reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise source

    monkeypatch.setattr(srp_auth, "refresh_tokens", _reject)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceInvalidAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert "SECRET-CANARY" in str(source)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_botocore_translation_drops_secret_bearing_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = BotoCoreError()
    source.args = ("SECRET-CANARY",)

    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise source

    monkeypatch.setattr(srp_auth, "refresh_tokens", _fail)
    gateway = RealCognitoGateway(PlaceConfig())

    with pytest.raises(PlaceTransientAuthError) as caught:
        _ = gateway.refresh("REFRESH-TOKEN-CANARY")

    assert "SECRET-CANARY" in str(source)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
