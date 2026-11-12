from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, cast

import boto3
from botocore import UNSIGNED
from botocore.config import Config

from ..config import (
    COGNITO_CLIENT_ID,
    COGNITO_IDENTITY_POOL_ID,
    COGNITO_USER_POOL_ID,
    REGION,
)
from ..exceptions import PlaceAuthError, PlaceInvalidAuthError
from ..models import Credentials
from .aws_srp import AWSSRP


def _unexpected_response(detail: str) -> PlaceAuthError:
    """A well-formed-but-unexpected AWS response, reported as an SDK auth error."""
    return PlaceAuthError(f"unexpected Cognito response: {detail}")


def _response_object(response: Any, *, path: str) -> Mapping[str, Any]:
    """Narrow an AWS response to a mapping, naming the call when it is not one."""
    if not isinstance(response, Mapping):
        raise _unexpected_response(f"{path} is not an object")
    return cast(Mapping[str, Any], response)


def _require_field(response: Mapping[str, Any], key: str, *, path: str) -> Any:
    """Read a required field, naming the missing path instead of raising KeyError."""
    if key not in response:
        raise _unexpected_response(f"{path} missing")
    return response[key]


def _require_string(response: Mapping[str, Any], key: str, *, path: str) -> str:
    """Read a required non-empty string, naming the field and never its value."""
    value = _require_field(response, key, path=path)
    if not isinstance(value, str) or not value:
        raise _unexpected_response(f"{path} is not a non-empty string")
    return value


def _unanswered_challenge(challenge: str) -> PlaceInvalidAuthError:
    """A refresh-time challenge the SDK cannot answer — only the user can."""
    return PlaceInvalidAuthError(
        f"unexpected Cognito response: initiate_auth returned {challenge} challenge "
        "instead of AuthenticationResult"
    )


def login(username: str, password: str) -> Dict[str, Any]:
    return get_tokens_via_srp(
        user_pool_id=COGNITO_USER_POOL_ID,
        client_id=COGNITO_CLIENT_ID,
        username=username,
        password=password,
    )


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

    id_response = _response_object(
        identity.get_id(IdentityPoolId=identity_pool_id, Logins=logins),
        path="get_id response",
    )
    identity_id = _require_string(
        id_response, "IdentityId", path="get_id response.IdentityId"
    )

    creds_response = _response_object(
        identity.get_credentials_for_identity(IdentityId=identity_id, Logins=logins),
        path="get_credentials_for_identity response",
    )
    creds = _response_object(
        _require_field(
            creds_response,
            "Credentials",
            path="get_credentials_for_identity response.Credentials",
        ),
        path="Credentials",
    )

    return Credentials(
        access_key_id=_require_string(
            creds, "AccessKeyId", path="Credentials.AccessKeyId"
        ),
        secret_access_key=_require_string(
            creds, "SecretKey", path="Credentials.SecretKey"
        ),
        session_token=_require_string(
            creds, "SessionToken", path="Credentials.SessionToken"
        ),
        identity_id=identity_id,
        access_token=access_token,
        expiration=creds.get("Expiration"),
    )


def get_tokens_via_srp(
    *,
    user_pool_id: str,
    client_id: str,
    username: str,
    password: str,
    region: str = REGION,
    client_secret: str | None = None,
) -> Dict[str, Any]:
    aws = AWSSRP(
        username=username,
        password=password,
        pool_id=user_pool_id,
        client_id=client_id,
        pool_region=region,
        client_secret=client_secret,
    )
    return aws.authenticate_user()


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
    resp_object = _response_object(resp, path="initiate_auth response")
    if "AuthenticationResult" not in resp_object:
        # A challenge here means Cognito refused to mint tokens from the refresh token,
        # and refresh_tokens discards the Session that answering it would need. Retrying
        # the same refresh token can only reproduce the challenge, so this demands user
        # action: the SDK treats it as terminal so PlaceConnection stops instead of
        # looping, and cached login can fall back to SRP. The message names the challenge
        # without leaking the session.
        challenge = resp_object.get("ChallengeName")
        if isinstance(challenge, str) and challenge:
            raise _unanswered_challenge(challenge)
        raise _unexpected_response(
            "initiate_auth response missing AuthenticationResult"
        )
    auth_result = _response_object(
        resp_object["AuthenticationResult"],
        path="initiate_auth response.AuthenticationResult",
    )
    # CognitoAuth._stage_tokens indexes these two directly, so a truncated result must
    # fail here as a typed auth error instead of escaping the reconnect loop as KeyError.
    tokens = dict(auth_result)
    tokens["AccessToken"] = _require_string(
        auth_result,
        "AccessToken",
        path="initiate_auth response.AuthenticationResult.AccessToken",
    )
    tokens["IdToken"] = _require_string(
        auth_result,
        "IdToken",
        path="initiate_auth response.AuthenticationResult.IdToken",
    )
    return tokens


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
    code_key = (
        "SMS_MFA_CODE" if challenge_name == "SMS_MFA" else "SOFTWARE_TOKEN_MFA_CODE"
    )
    return client.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName=challenge_name,
        Session=session,
        ChallengeResponses={"USERNAME": username, code_key: code},
    )
