from __future__ import annotations

from aiohttp import ClientError

from .auth.abstract_auth import AbstractAuth
from .exceptions import PlaceDiscoveryError
from .models.discover_device import DiscoverDevice
from .config import FULFILLMENT_URL

class Provider:
    def __init__(self, authorized_session: AbstractAuth) -> None:
        self.authorized_session = authorized_session


    async def discover(self) -> list[DiscoverDevice]:
        # Discovery failures surface as PlaceDiscoveryError so a consumer (a Home
        # Assistant config_flow) can catch the SDK taxonomy: a network blip on the
        # HTTPS fulfillment call and a success=false rejection both mean "discovery
        # failed". A PlaceAuthError from the token fetch inside request() is not a
        # ClientError, so it propagates untouched — config_flow needs that reauth
        # signal kept distinct. PlaceConnectionError stays reserved for MQTT.
        body = {"command": "DISCOVER", "data": {}}
        try:
            resp = await self.authorized_session.request("POST", FULFILLMENT_URL, json=body)
            data = await resp.json()
        except ClientError as err:
            raise PlaceDiscoveryError("could not reach PLACE discovery") from err
        if not data.get("success", True):
            raise PlaceDiscoveryError(f"PLACE discovery was rejected: {data.get('message', data)}")
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
