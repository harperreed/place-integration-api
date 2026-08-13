# ABOUTME: place — the public API for the PLACE async SDK: the PlaceClient facade,
# ABOUTME: config, device + model types, auth, and the error taxonomy consumers catch.
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .auth.cognito_auth import CognitoAuth
from .auth.token_cache import FileTokenCache, TokenCache
from .client import PlaceClient
from .config import PlaceConfig
from .device import PlaceDevice
from .exceptions import (
    MfaRequired,
    PlaceAuthError,
    PlaceConnectionError,
    PlaceDiscoveryError,
    PlaceError,
    PlaceInvalidAuthError,
    PlaceTimeoutError,
    PlaceTransientAuthError,
)
from .models import (
    AlarmStatus,
    Credentials,
    DeviceEvent,
    DiscoverDevice,
    NightLight,
    PlaceDeviceShadow,
)

try:
    __version__ = version("place-integration-api")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0"

__all__ = [
    "CognitoAuth",
    "FileTokenCache",
    "TokenCache",
    "PlaceClient",
    "PlaceConfig",
    "PlaceDevice",
    "PlaceError",
    "PlaceAuthError",
    "PlaceInvalidAuthError",
    "PlaceTransientAuthError",
    "MfaRequired",
    "PlaceConnectionError",
    "PlaceDiscoveryError",
    "PlaceTimeoutError",
    "AlarmStatus",
    "Credentials",
    "DeviceEvent",
    "DiscoverDevice",
    "NightLight",
    "PlaceDeviceShadow",
    "__version__",
]
