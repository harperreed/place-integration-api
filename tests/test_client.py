# ABOUTME: Tests for PlaceClient — the async facade wiring discovery, the MQTT connection,
# ABOUTME: and the PlaceDevice registry (read-only: shadow/get + subscribe, routing in Task 15).
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.messages import (
    household_subscription_topic,
    shadow_get_topic,
    shadow_subscription_topic,
)
from place.models import DiscoverDevice


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
