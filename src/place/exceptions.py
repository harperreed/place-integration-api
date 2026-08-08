# ABOUTME: The SDK's typed error taxonomy — the contract a consumer (e.g. a Home
# ABOUTME: Assistant integration) catches to drive reauth, retry, and MFA prompts.
from __future__ import annotations


class PlaceError(Exception):
    """Base class for every error this SDK raises."""


class PlaceAuthError(PlaceError):
    """Authentication or token refresh failed."""


class MfaRequired(PlaceAuthError):
    """Login needs a second factor before it can complete."""

    def __init__(self, *, challenge_name: str, session: str, username: str) -> None:
        super().__init__(f"MFA required: {challenge_name}")
        self.challenge_name = challenge_name
        self.session = session
        self.username = username


class PlaceConnectionError(PlaceError):
    """The MQTT transport could not connect or stay connected."""


class PlaceDiscoveryError(PlaceError):
    """Device discovery failed or returned nothing usable."""


class PlaceTimeoutError(PlaceError):
    """An awaited operation exceeded its deadline."""
