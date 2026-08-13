# ABOUTME: The blocking-boto3 seam behind CognitoAuth — SRP login, token refresh, MFA,
# ABOUTME: and IoT-credential exchange, isolated so async auth logic can be faked in tests.
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast

from botocore.exceptions import BotoCoreError, ClientError

from ..config import PlaceConfig
from ..exceptions import (
    PlaceAuthError,
    PlaceInvalidAuthError,
    PlaceTransientAuthError,
)
from ..models import Credentials
from . import srp_auth
from .aws_srp import ForceChangePasswordException, _UnsupportedChallengeException

_T = TypeVar("_T")

_TRANSIENT_AUTH_CODES = frozenset(
    {"TooManyRequestsException", "InternalErrorException", "ExternalServiceException"}
)
_NO_INVALID_AUTH_CODES: frozenset[str] = frozenset()
_NO_EXCEPTION_TYPES: tuple[type[Exception], ...] = ()


def _as_place_auth_error(
    action: str,
    call: Callable[[], _T],
    *,
    invalid_codes: frozenset[str] = _NO_INVALID_AUTH_CODES,
    invalid_exceptions: tuple[type[Exception], ...] = _NO_EXCEPTION_TYPES,
    unsupported_exceptions: tuple[type[Exception], ...] = _NO_EXCEPTION_TYPES,
) -> _T:
    """Run a Cognito call and expose a typed, secret-safe SDK error."""
    translated_error: PlaceAuthError
    try:
        return call()
    except ClientError as exc:
        response = cast(dict[str, object], exc.response)
        error = response.get("Error")
        error_details = (
            cast(dict[str, object], error) if isinstance(error, dict) else {}
        )
        code = str(error_details.get("Code", "UnknownClientError"))
        if code in invalid_codes:
            translated_error = PlaceInvalidAuthError(f"{action} rejected")
        elif code in _TRANSIENT_AUTH_CODES:
            translated_error = PlaceTransientAuthError(
                f"{action} temporarily failed ({code})"
            )
        else:
            translated_error = PlaceAuthError(f"{action} failed ({code})")
    except BotoCoreError as exc:
        translated_error = PlaceTransientAuthError(
            f"{action} temporarily failed ({type(exc).__name__})"
        )
    except invalid_exceptions:
        translated_error = PlaceInvalidAuthError(f"{action} rejected")
    except unsupported_exceptions:
        translated_error = PlaceAuthError(f"{action} failed (unsupported challenge)")
    raise translated_error


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
            invalid_codes=frozenset(
                {
                    "NotAuthorizedException",
                    "UserNotFoundException",
                    "PasswordResetRequiredException",
                    "UserNotConfirmedException",
                }
            ),
            invalid_exceptions=(ForceChangePasswordException,),
            unsupported_exceptions=(_UnsupportedChallengeException,),
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return _as_place_auth_error(
            "token refresh",
            lambda: srp_auth.refresh_tokens(
                refresh_token,
                region=self._config.region,
                client_id=self._config.cognito_client_id,
            ),
            invalid_codes=frozenset({"NotAuthorizedException"}),
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
            invalid_codes=frozenset(
                {
                    "CodeMismatchException",
                    "ExpiredCodeException",
                    "NotAuthorizedException",
                    "UserNotFoundException",
                }
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
