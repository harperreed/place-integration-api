from __future__ import annotations

from typing import Any, Dict

import boto3
from botocore import UNSIGNED
from botocore.config import Config

from ..config import (
    COGNITO_CLIENT_ID,
    COGNITO_IDENTITY_POOL_ID,
    COGNITO_USER_POOL_ID,
    REGION,
)
from ..models import Credentials
from .aws_srp import AWSSRP


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

    identity_id = identity.get_id(IdentityPoolId=identity_pool_id, Logins=logins)[
        "IdentityId"
    ]

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
    code_key = (
        "SMS_MFA_CODE" if challenge_name == "SMS_MFA" else "SOFTWARE_TOKEN_MFA_CODE"
    )
    return client.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName=challenge_name,
        Session=session,
        ChallengeResponses={"USERNAME": username, code_key: code},
    )
