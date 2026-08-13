# ABOUTME: Tests PLACE provider discovery parsing, error safety, and legacy commands.
# ABOUTME: Uses hand-written auth and response fakes without live account calls.
from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp import ClientError

from place.auth.abstract_auth import AbstractAuth
from place.config import FULFILLMENT_URL
from place.exceptions import PlaceAuthError, PlaceDiscoveryError
from place.provider import Provider


def test_provider_discover_parses_response() -> None:
    payload = {
        "success": True,
        "data": {
            "devices": [
                {"thingName": "t1", "deviceId": "d1", "deviceName": "Device 1"},
                {"thingName": "t2", "deviceId": "d2", "deviceName": "Device 2"},
            ]
        },
    }

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def async_get_access_token(self) -> str:
            return "token"

        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))

            class DummyResponse:
                async def json(self_inner):
                    return payload

            return DummyResponse()

    auth = DummyAuth()
    provider = Provider(auth)
    devices = asyncio.run(provider.discover())

    assert auth.calls == [
        ("POST", FULFILLMENT_URL, {"json": {"command": "DISCOVER", "data": {}}})
    ]

    thing_names = sorted({d.thing_name for d in devices if d.thing_name is not None})

    assert thing_names == ["t1", "t2"]
    assert [d.device_id for d in devices] == ["d1", "d2"]
    assert [d.device_name for d in devices] == ["Device 1", "Device 2"]


def test_provider_enable_sends_enable_command() -> None:
    payload = {"success": True, "data": {"status": "enabled"}}

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def async_get_access_token(self) -> str:
            return "token"

        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))

            class DummyResponse:
                async def json(self_inner):
                    return payload

            return DummyResponse()

    auth = DummyAuth()
    provider = Provider(auth)
    result = asyncio.run(provider.enable())

    assert auth.calls == [
        ("POST", FULFILLMENT_URL, {"json": {"command": "ENABLE", "data": {}}})
    ]
    assert result == payload


def test_provider_disable_sends_disable_command() -> None:
    payload = {"success": True, "data": {"status": "disabled"}}

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        async def async_get_access_token(self) -> str:
            return "token"

        async def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))

            class DummyResponse:
                async def json(self_inner):
                    return payload

            return DummyResponse()

    auth = DummyAuth()
    provider = Provider(auth)
    result = asyncio.run(provider.disable())

    assert auth.calls == [
        ("POST", FULFILLMENT_URL, {"json": {"command": "DISABLE", "data": {}}})
    ]
    assert result == payload


def test_provider_discover_rejection_does_not_leak_service_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fulfillment response with success=false must surface as PlaceDiscoveryError.

    HA's config_flow catches the SDK taxonomy to tell invalid_auth from
    cannot_connect. A bare RuntimeError is uncatchable through that contract, and
    its "Home Assistant error" label is untrue — the rejection is PLACE's.
    """
    payload = {
        "success": False,
        "message": "SECRET-MESSAGE-CANARY",
        "data": {
            "account": "ACCOUNT-CANARY",
            "devices": [{"deviceId": "DEVICE-CANARY"}],
        },
    }

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            pass

        async def async_get_access_token(self) -> str:
            return "token"

        async def request(self, method, url, **kwargs):
            class DummyResponse:
                async def json(self_inner):
                    return payload

            return DummyResponse()

    provider = Provider(DummyAuth())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PlaceDiscoveryError) as excinfo:
            _ = asyncio.run(provider.discover())

    assert str(excinfo.value) == "PLACE discovery rejected"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert "CANARY" not in caplog.text


def test_provider_discover_sanitizes_transport_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A network blip reaching the discovery endpoint is a discovery failure.

    aiohttp raises ClientError on a connection failure; unwrapped it escapes as a
    non-PlaceError the config_flow can't catch. PlaceConnectionError is reserved
    for the MQTT transport, so an HTTPS discovery blip maps to PlaceDiscoveryError.
    """

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            pass

        async def async_get_access_token(self) -> str:
            return "token"

        async def request(self, method, url, **kwargs):
            raise ClientError("SECRET-CANARY for ACCOUNT-CANARY")

    provider = Provider(DummyAuth())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PlaceDiscoveryError) as excinfo:
            _ = asyncio.run(provider.discover())

    assert str(excinfo.value) == "could not reach PLACE discovery"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert "CANARY" not in caplog.text


def test_provider_discover_lets_place_auth_error_propagate() -> None:
    """A token/auth failure during discovery must keep its PlaceAuthError type.

    async_get_access_token raises PlaceAuthError through request(); discovery must
    not remap it to PlaceDiscoveryError, or config_flow loses the reauth signal.
    """

    auth_error = PlaceAuthError("typed auth failure")

    class DummyAuth(AbstractAuth):
        def __init__(self) -> None:
            pass

        async def async_get_access_token(self) -> str:
            raise auth_error

        async def request(self, method, url, **kwargs):
            _ = await self.async_get_access_token()
            raise AssertionError("unreachable: token fetch should have raised")

    provider = Provider(DummyAuth())
    with pytest.raises(PlaceAuthError) as caught:
        _ = asyncio.run(provider.discover())

    assert caught.value is auth_error
