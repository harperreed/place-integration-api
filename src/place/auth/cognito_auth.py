# ABOUTME: CognitoAuth — the self-refreshing AbstractAuth for Place: SRP login, MFA,
# ABOUTME: access-token refresh, and IoT-credential caching, all async via to_thread.
from __future__ import annotations

import asyncio
import time
from typing import Any

from aiohttp import ClientSession

from ..config import PlaceConfig
from ..exceptions import MfaRequired, PlaceAuthError
from .abstract_auth import AbstractAuth
from .cognito_gateway import CognitoGateway, RealCognitoGateway


class CognitoAuth(AbstractAuth):
    """Concrete AbstractAuth backed by Cognito SRP with self-refresh and MFA."""

    def __init__(
        self,
        config: PlaceConfig,
        websession: ClientSession,
        gateway: CognitoGateway | None = None,
    ) -> None:
        super().__init__(websession)
        self._config: PlaceConfig = config
        self._gateway: CognitoGateway = gateway or RealCognitoGateway(config)
        self._username: str | None = None
        self._access_token: str | None = None
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._access_token_expiry: float = 0.0
        self._mfa_challenge: str | None = None
        self._mfa_session: str | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    async def authenticate(self, username: str, password: str) -> None:
        self._username = username
        result = await asyncio.to_thread(self._gateway.srp_login, username, password)
        self._consume_auth_response(result)

    async def submit_mfa(self, code: str) -> None:
        if self._mfa_challenge is None or self._mfa_session is None:
            raise PlaceAuthError("no MFA challenge pending")
        result = await asyncio.to_thread(
            self._gateway.respond_mfa,
            challenge_name=self._mfa_challenge,
            session=self._mfa_session,
            username=self._username or "",
            code=code,
        )
        self._consume_auth_response(result)

    async def async_get_access_token(self) -> str:
        if self._access_token is None:
            raise PlaceAuthError("not authenticated; call authenticate() first")
        return self._access_token

    def _consume_auth_response(self, result: dict[str, Any]) -> None:
        challenge = result.get("ChallengeName")
        if challenge in ("SOFTWARE_TOKEN_MFA", "SMS_MFA"):
            self._mfa_challenge = challenge
            self._mfa_session = result["Session"]
            raise MfaRequired(
                challenge_name=challenge,
                session=result["Session"],
                username=self._username or "",
            )
        self._store_tokens(result["AuthenticationResult"])
        self._mfa_challenge = None
        self._mfa_session = None

    def _store_tokens(self, auth: dict[str, Any]) -> None:
        self._access_token = auth["AccessToken"]
        self._id_token = auth["IdToken"]
        if auth.get("RefreshToken"):
            self._refresh_token = auth["RefreshToken"]
        self._access_token_expiry = time.time() + float(auth.get("ExpiresIn", 3600))
