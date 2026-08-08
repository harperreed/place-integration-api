# ABOUTME: Tests for the Cognito identity-credential exchange — that get_iot_credentials
# ABOUTME: maps the identity response fields and captures the credential Expiration.
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from place.auth.srp_auth import get_iot_credentials, refresh_tokens, respond_mfa


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
