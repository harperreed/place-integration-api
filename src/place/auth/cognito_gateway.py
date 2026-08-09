# ABOUTME: The blocking-boto3 seam behind CognitoAuth — SRP login, token refresh, MFA,
# ABOUTME: and IoT-credential exchange, isolated so async auth logic can be faked in tests.
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from botocore.exceptions import BotoCoreError, ClientError

from ..config import PlaceConfig
from ..exceptions import PlaceAuthError
from ..models import Credentials
from . import srp_auth

_T = TypeVar("_T")


def _as_place_auth_error(action: str, call: Callable[[], _T]) -> _T:
    """Run a boto3-backed Cognito call, surfacing botocore failures as PlaceAuthError.

    The gateway is the SDK's boto3 seam; translating here keeps the error taxonomy
    (exceptions.py) the single contract a consumer — or PlaceConnection's reconnect
    loop — catches, instead of leaking raw botocore exceptions.
    """
    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        raise PlaceAuthError(f"{action} failed: {exc}") from exc


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
        return _as_place_auth_error(
            "srp login",
            lambda: srp_auth.get_tokens_via_srp(
                user_pool_id=self._config.cognito_user_pool_id,
                client_id=self._config.cognito_client_id,
                username=username,
                password=password,
                region=self._config.region,
            ),
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return _as_place_auth_error(
            "token refresh",
            lambda: srp_auth.refresh_tokens(
                refresh_token,
                region=self._config.region,
                client_id=self._config.cognito_client_id,
            ),
        )

    def respond_mfa(
        self, *, challenge_name: str, session: str, username: str, code: str
    ) -> dict[str, Any]:
        return _as_place_auth_error(
            "mfa response",
            lambda: srp_auth.respond_mfa(
                challenge_name=challenge_name,
                session=session,
                username=username,
                code=code,
                region=self._config.region,
                client_id=self._config.cognito_client_id,
            ),
        )

    def iot_credentials(self, id_token: str, access_token: str) -> Credentials:
        return _as_place_auth_error(
            "iot credential exchange",
            lambda: srp_auth.get_iot_credentials(
                id_token,
                access_token,
                region=self._config.region,
                user_pool_id=self._config.cognito_user_pool_id,
                identity_pool_id=self._config.cognito_identity_pool_id,
            ),
        )
