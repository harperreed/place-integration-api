# ABOUTME: Tests for the Cognito identity-credential exchange — that get_iot_credentials
# ABOUTME: maps the identity response fields and captures the credential Expiration.
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
