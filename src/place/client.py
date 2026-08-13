# ABOUTME: PlaceClient — the async facade over discovery, the self-healing MQTT connection,
# ABOUTME: and the PlaceDevice registry; read-only (publishes shadow/get only, else subscribes).
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Protocol, TypeVar

from .auth.cognito_auth import CognitoAuth
from .config import PlaceConfig
from .device import PlaceDevice
from .exceptions import PlaceError
from .messages import (
    household_id_from_thing_name,
    household_subscription_topic,
    parse_payload,
    shadow_get_topic,
    shadow_subscription_topic,
    thing_name_from_topic,
)
from .models import Credentials, DeviceEvent, DiscoverDevice
from .models.device_event import EVENTS_SEGMENT
from .provider import Provider
from .transport import AiomqttTransport, MqttTransport, PlaceConnection

OnMessage = Callable[[str, bytes], None]
OnState = Callable[[bool], None]
OnError = Callable[[PlaceError], None]

logger = logging.getLogger(__name__)

_ListenerT = TypeVar("_ListenerT")


class Discoverer(Protocol):
    """The discovery surface PlaceClient needs: list the account's devices."""

    async def discover(self) -> list[DiscoverDevice]: ...


class Connection(Protocol):
    """The connection surface PlaceClient drives: pre-register topics, then run/stop the loop."""

    def add_subscription(self, topic: str) -> None: ...
    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None: ...
    async def publish(self, topic: str, payload: bytes = b"") -> None: ...
    async def run(self) -> None: ...
    def stop(self) -> None: ...


ConnectionFactory = Callable[[OnMessage, OnState, OnError], Connection]


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
        self._update_listeners: list[Callable[[PlaceDevice], None]] = []
        self._event_listeners: list[Callable[[DeviceEvent], None]] = []
        self._connection_listeners: list[Callable[[bool], None]] = []
        self._error_listeners: list[OnError] = []
        self._connection: Connection = connection_factory(
            self._dispatch, self._set_connected, self._emit_error
        )
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

        def connection_factory(
            on_message: OnMessage, on_state: OnState, on_error: OnError
        ) -> Connection:
            return PlaceConnection(
                config,
                auth,
                transport_factory=_aiomqtt_transport_factory,
                on_message=on_message,
                on_state=on_state,
                on_error=on_error,
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

    async def async_discover(self) -> list[DiscoverDevice]:
        """Return devices visible to the authenticated account without starting MQTT."""
        return await self._provider.discover()

    async def start(self) -> None:
        discovered = await self.async_discover()
        # Any explicitly configured households first, then one derived from each
        # device's thing name (the household subscription is what delivers live
        # motion events). dict.fromkeys dedupes while preserving order — devices
        # share a household, and a manual id may repeat a derived one.
        household_ids: list[str] = list(self._household_ids)
        for entry in discovered:
            if not entry.thing_name:
                continue
            device = PlaceDevice.from_discovery(entry)
            self._devices[device.thing_name] = device
            self._connection.add_subscription(
                shadow_subscription_topic(device.thing_name)
            )
            self._connection.add_connect_publish(shadow_get_topic(device.thing_name))
            household_ids.append(household_id_from_thing_name(device.thing_name))
        for household_id in dict.fromkeys(household_ids):
            self._connection.add_subscription(
                household_subscription_topic(household_id)
            )
        self._task = asyncio.create_task(self._connection.run())
        await asyncio.sleep(
            0
        )  # let the connection task take its first step before we return

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

    @property
    def connected(self) -> bool:
        return self._connected

    def on_update(self, callback: Callable[[PlaceDevice], None]) -> Callable[[], None]:
        return self._register(self._update_listeners, callback)

    def on_event(self, callback: Callable[[DeviceEvent], None]) -> Callable[[], None]:
        return self._register(self._event_listeners, callback)

    def on_connection_change(
        self, callback: Callable[[bool], None]
    ) -> Callable[[], None]:
        return self._register(self._connection_listeners, callback)

    def on_error(self, callback: OnError) -> Callable[[], None]:
        return self._register(self._error_listeners, callback)

    def updates(self) -> AsyncGenerator[PlaceDevice, None]:
        queue: asyncio.Queue[PlaceDevice] = asyncio.Queue()
        unsubscribe = self.on_update(queue.put_nowait)

        async def _generator() -> AsyncGenerator[PlaceDevice, None]:
            try:
                while True:
                    yield await queue.get()
            finally:
                unsubscribe()

        return _generator()

    async def async_refresh_shadow(self, thing_name: str | None = None) -> None:
        names = [thing_name] if thing_name is not None else list(self._devices)
        for name in names:
            await self._connection.publish(shadow_get_topic(name), b"")

    @staticmethod
    def _register(
        registry: list[_ListenerT], callback: _ListenerT
    ) -> Callable[[], None]:
        registry.append(callback)

        def _unsubscribe() -> None:
            if callback in registry:
                registry.remove(callback)

        return _unsubscribe

    def _dispatch(self, topic: str, raw: bytes) -> None:
        payload = parse_payload(raw)
        thing = thing_name_from_topic(topic)
        if thing is not None:
            device = self._devices.get(thing)
            if device is not None and device.apply_shadow(payload):
                self._emit_update(device)
            return
        if EVENTS_SEGMENT in topic:
            event = DeviceEvent.from_message(topic, payload)
            if event is None:
                return
            device = self._device_for_event(event)
            if device is not None:
                device.apply_event(event)
                self._emit_update(device)
            self._emit_event(event)

    def _device_for_event(self, event: DeviceEvent) -> PlaceDevice | None:
        if event.thing_name and event.thing_name in self._devices:
            return self._devices[event.thing_name]
        if event.device_id:
            for device in self._devices.values():
                if device.device_id == event.device_id:
                    return device
        return None

    def _emit_update(self, device: PlaceDevice) -> None:
        for callback in list(self._update_listeners):
            callback(device)

    def _emit_event(self, event: DeviceEvent) -> None:
        for callback in list(self._event_listeners):
            callback(event)

    def _emit_error(self, error: PlaceError) -> None:
        for callback in list(self._error_listeners):
            try:
                callback(error)
            except Exception as exc:
                logger.warning("Place error listener failed (%s)", type(exc).__name__)

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        for callback in list(self._connection_listeners):
            callback(connected)
