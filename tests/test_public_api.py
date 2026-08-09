# ABOUTME: Tests the package's public API surface — that the names an SDK consumer
# ABOUTME: (e.g. a Home Assistant integration) imports from `place` are actually exported.
from __future__ import annotations


def test_top_level_exports_are_importable() -> None:
    from place import (
        AlarmStatus,
        CognitoAuth,
        Credentials,
        DeviceEvent,
        DiscoverDevice,
        MfaRequired,
        NightLight,
        PlaceAuthError,
        PlaceClient,
        PlaceConfig,
        PlaceConnectionError,
        PlaceDevice,
        PlaceDeviceShadow,
        PlaceDiscoveryError,
        PlaceError,
        PlaceTimeoutError,
    )

    # Spot-check the names resolve to the real objects, not placeholders.
    assert PlaceClient.__name__ == "PlaceClient"
    assert PlaceConfig.__name__ == "PlaceConfig"
    assert PlaceDevice.__name__ == "PlaceDevice"
    assert CognitoAuth.__name__ == "CognitoAuth"
    assert issubclass(PlaceAuthError, PlaceError)
    assert issubclass(MfaRequired, PlaceAuthError)
    for exc in (PlaceConnectionError, PlaceDiscoveryError, PlaceTimeoutError):
        assert issubclass(exc, PlaceError)
    # Model/event/shadow types are exposed for consumers mapping device state.
    assert AlarmStatus is not None
    assert NightLight is not None
    assert PlaceDeviceShadow.__name__ == "PlaceDeviceShadow"
    assert DeviceEvent.__name__ == "DeviceEvent"
    assert DiscoverDevice.__name__ == "DiscoverDevice"
    assert Credentials.__name__ == "Credentials"


def test_version_is_exposed() -> None:
    import place

    assert isinstance(place.__version__, str)
    assert place.__version__  # non-empty
