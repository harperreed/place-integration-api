# ABOUTME: Tests for PlaceClient — the async facade wiring discovery, the MQTT connection,
# ABOUTME: and the PlaceDevice registry (read-only: shadow/get + subscribe, routing in Task 15).
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.device import PlaceDevice
from place.messages import (
    household_subscription_topic,
    shadow_get_topic,
    shadow_subscription_topic,
    thing_name_from_topic,
)
from place.models import DeviceEvent, DiscoverDevice


class FakeProvider:
    def __init__(self, devices: list[DiscoverDevice]) -> None:
        self._devices: list[DiscoverDevice] = devices

    async def discover(self) -> list[DiscoverDevice]:
        return self._devices


class FakeConnection:
    def __init__(
        self,
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
    ) -> None:
        self.on_message: Callable[[str, bytes], None] = on_message
        self.on_state: Callable[[bool], None] = on_state
        self.subscriptions: list[str] = []
        self.connect_publishes: list[tuple[str, bytes]] = []
        self.published: list[tuple[str, bytes]] = []
        self.started: bool = False
        self.stopped: bool = False
        self._gate: asyncio.Event = asyncio.Event()

    def add_subscription(self, topic: str) -> None:
        self.subscriptions.append(topic)

    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None:
        self.connect_publishes.append((topic, payload))

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        self.published.append((topic, payload))

    async def run(self) -> None:
        self.started = True
        _ = await self._gate.wait()

    def stop(self) -> None:
        self.stopped = True
        self._gate.set()


def _discover(thing: str) -> DiscoverDevice:
    return DiscoverDevice.from_dict({"thingName": thing, "deviceId": "dev-1", "shadow": {}})


async def test_start_discovers_wires_subscriptions_and_launches() -> None:
    created: list[FakeConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
    ) -> FakeConnection:
        conn = FakeConnection(on_message, on_state)
        created.append(conn)
        return conn

    client = PlaceClient(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=FakeProvider([_discover("Place_PL1AS_EXAMPLE")]),
        connection_factory=connection_factory,
        household_ids=["hh-1"],
    )

    await client.start()
    conn = created[0]

    assert "Place_PL1AS_EXAMPLE" in client.devices
    assert shadow_subscription_topic("Place_PL1AS_EXAMPLE") in conn.subscriptions
    assert household_subscription_topic("hh-1") in conn.subscriptions
    assert (shadow_get_topic("Place_PL1AS_EXAMPLE"), b"") in conn.connect_publishes
    assert conn.started is True

    await client.stop()
    assert conn.stopped is True


async def test_create_builds_a_client_with_empty_registry() -> None:
    client = PlaceClient.create(PlaceConfig(), auth=cast(CognitoAuth, object()))
    assert client.devices == {}


def test_thing_name_from_topic_module_function() -> None:
    assert (
        thing_name_from_topic("$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted")
        == "Place_PL1AS_EXAMPLE"
    )
    assert thing_name_from_topic("connectedsmoke/household/hh-1/x") is None


async def _started_client(
    *discover_args: str,
) -> tuple[PlaceClient, FakeConnection]:
    created: list[FakeConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
    ) -> FakeConnection:
        conn = FakeConnection(on_message, on_state)
        created.append(conn)
        return conn

    client = PlaceClient(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=FakeProvider([_discover(t) for t in discover_args]),
        connection_factory=connection_factory,
    )
    await client.start()
    return client, created[0]


async def test_shadow_message_updates_device_and_emits_update() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    updates: list[PlaceDevice] = []
    _ = client.on_update(updates.append)

    conn.on_message(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":12}}}',
    )

    assert client.devices["Place_PL1AS_EXAMPLE"].shadow.co_ppm == 12
    assert updates == [client.devices["Place_PL1AS_EXAMPLE"]]
    await client.stop()


async def test_event_message_routes_and_emits_event() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    events: list[DeviceEvent] = []
    _ = client.on_event(events.append)

    conn.on_message(
        "connectedsmoke/household/hh-1/device/dev-1/events/motionDetected",
        b'{"deviceId":"dev-1","thingName":"Place_PL1AS_EXAMPLE","seq":5}',
    )

    assert len(events) == 1 and events[0].is_motion is True
    last = client.devices["Place_PL1AS_EXAMPLE"].last_event
    assert last is not None
    assert last.seq == 5
    await client.stop()


async def test_updates_iterator_yields_changed_devices() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    stream = client.updates()

    conn.on_message(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":1}}}',
    )
    device = await stream.__anext__()

    assert device.shadow.co_ppm == 1
    await stream.aclose()
    await client.stop()


async def test_connection_change_notifies_and_dedupes() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    changes: list[bool] = []
    _ = client.on_connection_change(changes.append)

    conn.on_state(True)
    conn.on_state(True)  # no-op: state unchanged
    conn.on_state(False)

    assert changes == [True, False]
    assert client.connected is False
    await client.stop()
