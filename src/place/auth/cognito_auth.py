# ABOUTME: CognitoAuth — the self-refreshing AbstractAuth for Place: SRP login, MFA,
# ABOUTME: access-token refresh, and IoT-credential caching, all async via to_thread.
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import ClientSession

from ..config import PlaceConfig
from ..exceptions import (
    MfaRequired,
    PlaceAuthError,
    PlaceInvalidAuthError,
    PlaceTransientAuthError,
)
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
        self._principal_generation: int = 0
        self._refresh_lock: asyncio.Lock = asyncio.Lock()
        self._iot_creds: Credentials | None = None
        self._iot_creds_expiry: datetime | None = None
        self._iot_lock: asyncio.Lock = asyncio.Lock()

    async def authenticate(self, username: str, password: str) -> None:
        generation = self._principal_generation
        cached_auth = await self._try_cached_login(username)
        if cached_auth is not None:
            self._commit_authenticated(username, cached_auth, generation)
            return
        result = await asyncio.to_thread(self._gateway.srp_login, username, password)
        self._commit_auth_response(username, result, generation)

    async def authenticate_from_cache(self, username: str) -> None:
        """Authenticate with the configured refresh-token cache and never use SRP."""
        generation = self._principal_generation
        if self._token_cache is None:
            raise PlaceInvalidAuthError("no refresh-token cache configured")

        cached: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        cache_failed = False
        try:
            cached = self._token_cache.load()
        except Exception:
            logger.warning("token cache load failed")
            cache_failed = True
        if cache_failed:
            raise PlaceTransientAuthError("refresh-token cache unavailable") from None

        refresh_token = self._parse_cached_refresh_token(cached, username)
        if refresh_token is None:
            raise PlaceInvalidAuthError("no refresh token for username")

        auth = dict(await asyncio.to_thread(self._gateway.refresh, refresh_token))
        auth.setdefault("RefreshToken", refresh_token)
        self._commit_authenticated(username, auth, generation)

    async def _try_cached_login(self, username: str) -> dict[str, Any] | None:
        """Mint tokens from a cached refresh token, skipping SRP+MFA.

        Returns tokens only when a cached refresh token for this exact username minted a
        fresh access token. Any miss — no cache, wrong user, empty or rejected token, or a
        misbehaving cache — returns None so authenticate() falls back to SRP login.
        """
        if self._token_cache is None:
            return None
        try:
            cached = self._token_cache.load()
        except Exception:  # a broken cache must never block a real login
            logger.warning("token cache load failed; falling back to SRP login")
            return None
        refresh_token = self._parse_cached_refresh_token(cached, username)
        if refresh_token is None:
            return None
        try:
            auth = dict(await asyncio.to_thread(self._gateway.refresh, refresh_token))
        except PlaceInvalidAuthError:
            logger.info("cached refresh token rejected; falling back to SRP login")
            return None
        # REFRESH_TOKEN_AUTH omits the refresh token; thread the cached one back in so the
        # in-memory session keeps it and re-persists it below.
        auth.setdefault("RefreshToken", refresh_token)
        return auth

    @staticmethod
    def _parse_cached_refresh_token(
        cached: Mapping[str, object] | None, username: str
    ) -> str | None:
        """Return a non-empty refresh token only for the requested account."""
        if not cached or cached.get("username") != username:
            return None
        refresh_token = cached.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return None
        return refresh_token

    async def submit_mfa(self, code: str) -> None:
        if self._mfa_challenge is None or self._mfa_session is None:
            raise PlaceAuthError("no MFA challenge pending")
        generation = self._principal_generation
        username = self._username or ""
        result = await asyncio.to_thread(
            self._gateway.respond_mfa,
            challenge_name=self._mfa_challenge,
            session=self._mfa_session,
            username=username,
            code=code,
        )
        self._commit_auth_response(username, result, generation)

    async def async_get_access_token(self) -> str:
        async with self._refresh_lock:
            while True:
                if self._access_token is not None and time.time() < (
                    self._access_token_expiry - self._config.token_refresh_margin_sec
                ):
                    return self._access_token
                if self._refresh_token is None:
                    if self._access_token is None:
                        raise PlaceAuthError(
                            "not authenticated; call authenticate() first"
                        )
                    return self._access_token
                generation = self._principal_generation
                refresh_token = self._refresh_token
                auth = dict(
                    await asyncio.to_thread(self._gateway.refresh, refresh_token)
                )
                if generation != self._principal_generation:
                    continue
                self._install_tokens(auth)
                assert self._access_token is not None
                return self._access_token

    async def async_get_iot_credentials(self) -> Credentials:
        async with self._iot_lock:
            while True:
                if self._iot_creds is not None and self._iot_creds_expiry is not None:
                    margin = timedelta(seconds=self._config.creds_refresh_margin_sec)
                    if datetime.now(timezone.utc) < self._iot_creds_expiry - margin:
                        return self._iot_creds
                access_token = await self.async_get_access_token()
                generation = self._principal_generation
                id_token = self._id_token
                assert id_token is not None
                creds = await asyncio.to_thread(
                    self._gateway.iot_credentials, id_token, access_token
                )
                if generation != self._principal_generation:
                    continue
                self._iot_creds = creds
                self._iot_creds_expiry = creds.expiration or (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._config.url_expire_sec)
                )
                return creds

    def _commit_auth_response(
        self, username: str, result: dict[str, Any], generation: int
    ) -> None:
        challenge = result.get("ChallengeName")
        if challenge in ("SOFTWARE_TOKEN_MFA", "SMS_MFA"):
            session = result["Session"]
            self._ensure_current_generation(generation)
            self._username = username
            self._access_token = None
            self._id_token = None
            self._refresh_token = None
            self._access_token_expiry = 0.0
            self._iot_creds = None
            self._iot_creds_expiry = None
            self._mfa_challenge = challenge
            self._mfa_session = session
            self._principal_generation += 1
            raise MfaRequired(
                challenge_name=challenge,
                session=session,
                username=username,
            )
        self._commit_authenticated(username, result["AuthenticationResult"], generation)

    def _commit_authenticated(
        self, username: str, auth: dict[str, Any], generation: int
    ) -> None:
        switching_principal = self._username != username
        retained_refresh_token = None if switching_principal else self._refresh_token
        tokens = self._stage_tokens(auth, retained_refresh_token)
        self._ensure_current_generation(generation)
        self._username = username
        if switching_principal:
            self._iot_creds = None
            self._iot_creds_expiry = None
        self._apply_tokens(tokens)
        self._mfa_challenge = None
        self._mfa_session = None
        self._principal_generation += 1

    def _install_tokens(self, auth: dict[str, Any]) -> None:
        self._apply_tokens(self._stage_tokens(auth, self._refresh_token))

    @staticmethod
    def _stage_tokens(
        auth: dict[str, Any], retained_refresh_token: str | None
    ) -> tuple[str, str, str | None, float]:
        refresh_token = auth.get("RefreshToken") or retained_refresh_token
        return (
            auth["AccessToken"],
            auth["IdToken"],
            refresh_token,
            time.time() + float(auth.get("ExpiresIn", 3600)),
        )

    def _apply_tokens(self, tokens: tuple[str, str, str | None, float]) -> None:
        (
            self._access_token,
            self._id_token,
            self._refresh_token,
            self._access_token_expiry,
        ) = tokens
        self._persist_tokens()

    def _ensure_current_generation(self, generation: int) -> None:
        if generation != self._principal_generation:
            raise PlaceAuthError("authentication superseded by a newer request")

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
