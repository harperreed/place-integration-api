# ABOUTME: CognitoAuth — the self-refreshing AbstractAuth for Place: SRP login, MFA,
# ABOUTME: access-token refresh, and IoT-credential caching, all async via to_thread.
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import ClientSession

from ..config import PlaceConfig
from ..exceptions import MfaRequired, PlaceAuthError
from ..models import Credentials
from .abstract_auth import AbstractAuth
from .cognito_gateway import CognitoGateway, RealCognitoGateway
from .token_cache import TokenCache

logger = logging.getLogger(__name__)


class CognitoAuth(AbstractAuth):
    """Concrete AbstractAuth backed by Cognito SRP with self-refresh and MFA."""

    def __init__(
        self,
        config: PlaceConfig,
        websession: ClientSession,
        gateway: CognitoGateway | None = None,
        token_cache: TokenCache | None = None,
    ) -> None:
        super().__init__(websession)
        self._config: PlaceConfig = config
        self._gateway: CognitoGateway = gateway or RealCognitoGateway(config)
        self._token_cache: TokenCache | None = token_cache
        self._username: str | None = None
        self._access_token: str | None = None
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._access_token_expiry: float = 0.0
        self._mfa_challenge: str | None = None
        self._mfa_session: str | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._iot_creds: Credentials | None = None
        self._iot_creds_expiry: datetime | None = None
        self._iot_lock: asyncio.Lock = asyncio.Lock()

    async def authenticate(self, username: str, password: str) -> None:
        self._username = username
        if await self._try_cached_login(username):
            return
        result = await asyncio.to_thread(self._gateway.srp_login, username, password)
        self._consume_auth_response(result)

    async def _try_cached_login(self, username: str) -> bool:
        """Mint tokens from a cached refresh token, skipping SRP+MFA.

        Returns True only when a cached refresh token for this exact username minted a
        fresh access token. Any miss — no cache, wrong user, empty or rejected token, or a
        misbehaving cache — returns False so authenticate() falls back to SRP login.
        """
        if self._token_cache is None:
            return False
        try:
            cached = self._token_cache.load()
        except Exception:  # a broken cache must never block a real login
            logger.warning("token cache load failed; falling back to SRP login")
            return False
        if not cached or cached.get("username") != username:
            return False
        refresh_token = cached.get("refresh_token")
        if not refresh_token:
            return False
        try:
            auth = await asyncio.to_thread(self._gateway.refresh, refresh_token)
        except PlaceAuthError:
            logger.info("cached refresh token rejected; falling back to SRP login")
            return False
        # REFRESH_TOKEN_AUTH omits the refresh token; thread the cached one back in so the
        # in-memory session keeps it and re-persists it below.
        auth.setdefault("RefreshToken", refresh_token)
        self._store_tokens(auth)
        return True

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
        async with self._refresh_lock:
            if self._access_token is not None and time.time() < (
                self._access_token_expiry - self._config.token_refresh_margin_sec
            ):
                return self._access_token
            if self._refresh_token is None:
                if self._access_token is None:
                    raise PlaceAuthError("not authenticated; call authenticate() first")
                return self._access_token
            auth = await asyncio.to_thread(self._gateway.refresh, self._refresh_token)
            self._store_tokens(auth)
            assert self._access_token is not None
            return self._access_token

    async def async_get_iot_credentials(self) -> Credentials:
        async with self._iot_lock:
            if self._iot_creds is not None and self._iot_creds_expiry is not None:
                margin = timedelta(seconds=self._config.creds_refresh_margin_sec)
                if datetime.now(timezone.utc) < self._iot_creds_expiry - margin:
                    return self._iot_creds
            access_token = await self.async_get_access_token()
            assert self._id_token is not None
            creds = await asyncio.to_thread(
                self._gateway.iot_credentials, self._id_token, access_token
            )
            self._iot_creds = creds
            self._iot_creds_expiry = creds.expiration or (
                datetime.now(timezone.utc)
                + timedelta(seconds=self._config.url_expire_sec)
            )
            return creds

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
        self._persist_tokens()

    def _persist_tokens(self) -> None:
        """Best-effort write of the current refresh token to the cache (if configured)."""
        if self._token_cache is None or not self._username or not self._refresh_token:
            return
        try:
            self._token_cache.save(
                {"username": self._username, "refresh_token": self._refresh_token}
            )
        except Exception:  # caching is best-effort; never break auth on a save failure
            logger.warning("token cache save failed; continuing without persistence")
