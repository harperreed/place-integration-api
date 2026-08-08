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
        self._config: PlaceConfig = config

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
