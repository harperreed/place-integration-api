# ABOUTME: Calls the PLACE fulfillment API for account device discovery.
# ABOUTME: Keeps discovery errors typed and safe while retaining legacy enable/disable calls.
from __future__ import annotations

from typing import Any

from aiohttp import ClientError

from .auth.abstract_auth import AbstractAuth
from .config import FULFILLMENT_URL
from .exceptions import PlaceDiscoveryError, PlaceTimeoutError
from .models.discover_device import DiscoverDevice


class Provider:
    def __init__(self, authorized_session: AbstractAuth) -> None:
        self.authorized_session = authorized_session

    async def discover(self) -> list[DiscoverDevice]:
        body = {"command": "DISCOVER", "data": {}}
        transport_error: PlaceDiscoveryError | PlaceTimeoutError | None = None
        data: dict[str, Any] | None = None
        try:
            resp = await self.authorized_session.request(
                "POST", FULFILLMENT_URL, json=body
            )
            data = await resp.json()
        except TimeoutError:
            transport_error = PlaceTimeoutError("PLACE discovery timed out")
        except ClientError:
            transport_error = PlaceDiscoveryError("could not reach PLACE discovery")
        if transport_error is not None:
            raise transport_error
        assert data is not None
        if not data.get("success", True):
            raise PlaceDiscoveryError("PLACE discovery rejected")
        devices_raw = (data.get("data") or {}).get("devices") or []
        devices: list[DiscoverDevice] = []
        for raw in devices_raw:
            devices.append(DiscoverDevice.from_dict(raw))
        return devices

    async def enable(self):
        body = {"command": "ENABLE", "data": {}}
        resp = await self.authorized_session.request("POST", FULFILLMENT_URL, json=body)
        data = await resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"Home Assistant error: {data.get('message', data)}")
        return data

    async def disable(self):
        body = {"command": "DISABLE", "data": {}}
        resp = await self.authorized_session.request("POST", FULFILLMENT_URL, json=body)
        data = await resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"Home Assistant error: {data.get('message', data)}")
        return data
