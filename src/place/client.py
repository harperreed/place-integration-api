# ABOUTME: PlaceClient — the async facade over discovery, the self-healing MQTT connection,
# ABOUTME: and the PlaceDevice registry; read-only (publishes shadow/get only, else subscribes).
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from .auth.cognito_auth import CognitoAuth
from .config import PlaceConfig
from .device import PlaceDevice
from .messages import (
    household_subscription_topic,
    shadow_get_topic,
    shadow_subscription_topic,
)
from .models import Credentials, DiscoverDevice
from .provider import Provider
from .transport import AiomqttTransport, MqttTransport, PlaceConnection

OnMessage = Callable[[str, bytes], None]
OnState = Callable[[bool], None]


class Discoverer(Protocol):
    """The discovery surface PlaceClient needs: list the account's devices."""

    async def discover(self) -> list[DiscoverDevice]: ...


class Connection(Protocol):
    """The connection surface PlaceClient drives: pre-register topics, then run/stop the loop."""

    def add_subscription(self, topic: str) -> None: ...
    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None: ...
    async def run(self) -> None: ...
    def stop(self) -> None: ...


ConnectionFactory = Callable[[OnMessage, OnState], Connection]


def _aiomqtt_transport_factory(cfg: PlaceConfig, creds: Credentials) -> MqttTransport:
    return AiomqttTransport(cfg, creds)


class PlaceClient:
    """Read-only async client for a PLACE account's devices."""

    def __init__(
        self,
        config: PlaceConfig,
        auth: CognitoAuth,
        *,
        provider: Discoverer,
        connection_factory: ConnectionFactory,
        household_ids: list[str] | None = None,
    ) -> None:
        self._config: PlaceConfig = config
        self._auth: CognitoAuth = auth
        self._provider: Discoverer = provider
        self._household_ids: list[str] = list(household_ids or [])
        self._devices: dict[str, PlaceDevice] = {}
        self._connected: bool = False
        self._connection: Connection = connection_factory(self._dispatch, self._set_connected)
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def create(
        cls,
        config: PlaceConfig,
        auth: CognitoAuth,
        *,
        household_ids: list[str] | None = None,
    ) -> "PlaceClient":
        provider = Provider(auth)

        def connection_factory(on_message: OnMessage, on_state: OnState) -> Connection:
            return PlaceConnection(
                config,
                auth,
                transport_factory=_aiomqtt_transport_factory,
                on_message=on_message,
                on_state=on_state,
            )

        return cls(
            config,
            auth,
            provider=provider,
            connection_factory=connection_factory,
            household_ids=household_ids,
        )

    @property
    def devices(self) -> dict[str, PlaceDevice]:
        return dict(self._devices)

    async def start(self) -> None:
        discovered = await self._provider.discover()
        for entry in discovered:
            if not entry.thing_name:
                continue
            device = PlaceDevice.from_discovery(entry)
            self._devices[device.thing_name] = device
            self._connection.add_subscription(shadow_subscription_topic(device.thing_name))
            self._connection.add_connect_publish(shadow_get_topic(device.thing_name))
        for household_id in self._household_ids:
            self._connection.add_subscription(household_subscription_topic(household_id))
        self._task = asyncio.create_task(self._connection.run())
        await asyncio.sleep(0)  # let the connection task take its first step before we return

    async def stop(self) -> None:
        self._connection.stop()
        if self._task is not None:
            _ = self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def __aenter__(self) -> "PlaceClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def _dispatch(self, topic: str, payload: bytes) -> None:
        _ = (topic, payload)  # routing added in Task 15

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
