# ABOUTME: Tests for PlaceClient — the async facade wiring discovery, the MQTT connection,
# ABOUTME: and the PlaceDevice registry (read-only: shadow/get + subscribe, plus message routing).
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from typing import override
else:

    def override(method: object) -> object:
        """Backport the type-checking-only override marker for Python 3.11."""
        return method

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.device import PlaceDevice
from place.exceptions import PlaceError, PlaceInvalidAuthError, PlaceTransientAuthError
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
        self.discover_calls: int = 0

    async def discover(self) -> list[DiscoverDevice]:
        self.discover_calls += 1
        return self._devices


class FakeConnection:
    def __init__(
        self,
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> None:
        self.on_message: Callable[[str, bytes], None] = on_message
        self.on_state: Callable[[bool], None] = on_state
        self.on_error: Callable[[PlaceError], None] = on_error
        self.subscriptions: list[str] = []
        self.connect_publishes: list[tuple[str, bytes]] = []
        self.published: list[tuple[str, bytes]] = []
        self.run_calls: int = 0
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
        self.run_calls += 1
        self.started = True
        _ = await self._gate.wait()

    def stop(self) -> None:
        self.stopped = True
        self._gate.set()


def _discover(thing: str) -> DiscoverDevice:
    return DiscoverDevice.from_dict({"thingName": thing, "deviceId": "dev-1", "shadow": {}})


async def test_async_discover_returns_devices_without_starting_connection() -> None:
    discovered = [_discover("thing-a")]
    provider = FakeProvider(discovered)
    created: list[FakeConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> FakeConnection:
        connection = FakeConnection(on_message, on_state, on_error)
        created.append(connection)
        return connection

    client = PlaceClient(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=provider,
        connection_factory=connection_factory,
    )

    result = await client.async_discover()

    assert result == discovered
    assert provider.discover_calls == 1
    assert created[0].run_calls == 0


async def test_start_discovers_wires_subscriptions_and_launches() -> None:
    created: list[FakeConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> FakeConnection:
        conn = FakeConnection(on_message, on_state, on_error)
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


async def test_start_uses_public_discovery_contract() -> None:
    public_discovered = [_discover("public-device")]
    provider = FakeProvider([_discover("provider-device")])
    discovery_calls = 0

    class ClientWithPublicDiscovery(PlaceClient):
        @override
        async def async_discover(self) -> list[DiscoverDevice]:
            nonlocal discovery_calls
            discovery_calls += 1
            return public_discovered

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> FakeConnection:
        return FakeConnection(on_message, on_state, on_error)

    client = ClientWithPublicDiscovery(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=provider,
        connection_factory=connection_factory,
    )

    await client.start()
    try:
        assert discovery_calls == 1
        assert provider.discover_calls == 0
        assert "public-device" in client.devices
        assert "provider-device" not in client.devices
    finally:
        await client.stop()


async def test_start_auto_derives_household_from_thing_name() -> None:
    # No household_ids passed: the household subscription (which carries live
    # motion events) is derived from the discovered thing name's leading token.
    client, conn = await _started_client("hh-1_reg-1_Place_PL1AS_EXAMPLE")

    assert household_subscription_topic("hh-1") in conn.subscriptions
    await client.stop()


async def test_start_unions_manual_household_with_derived() -> None:
    created: list[FakeConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> FakeConnection:
        conn = FakeConnection(on_message, on_state, on_error)
        created.append(conn)
        return conn

    client = PlaceClient(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=FakeProvider([_discover("hh-1_reg-1_Place_PL1AS_EXAMPLE")]),
        connection_factory=connection_factory,
        household_ids=["extra-hh"],
    )

    await client.start()
    conn = created[0]

    assert household_subscription_topic("extra-hh") in conn.subscriptions  # manual
    assert household_subscription_topic("hh-1") in conn.subscriptions  # derived
    await client.stop()


async def test_start_dedupes_household_shared_by_multiple_devices() -> None:
    client, conn = await _started_client(
        "hh-1_reg-1_Place_PL1AS_A",
        "hh-1_reg-2_Place_PL1AS_B",
    )

    assert conn.subscriptions.count(household_subscription_topic("hh-1")) == 1
    await client.stop()


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
        on_error: Callable[[PlaceError], None],
    ) -> FakeConnection:
        conn = FakeConnection(on_message, on_state, on_error)
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


async def test_noop_shadow_message_does_not_emit_update() -> None:
    # An empty-payload shadow message — our own shadow/get echoed back on the
    # shadow/# wildcard — merges to a no-op and must NOT emit a spurious update.
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    updates: list[PlaceDevice] = []
    _ = client.on_update(updates.append)

    conn.on_message("$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted", b"")

    assert updates == []
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


async def test_error_listener_receives_errors_until_unsubscribed() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    seen: list[PlaceError] = []
    unsubscribe = client.on_error(seen.append)
    transient = PlaceTransientAuthError("temporary")
    invalid = PlaceInvalidAuthError("token rejected")

    conn.on_error(transient)
    unsubscribe()
    conn.on_error(invalid)

    assert seen == [transient]
    assert seen[0] is transient
    await client.stop()


async def test_error_listener_mutation_and_failure_do_not_block_other_listeners(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    seen: list[PlaceError] = []
    self_seen: list[PlaceError] = []
    unsubscribe_self: Callable[[], None]
    canary = "consumer-callback-secret-canary"

    def remove_self(error: PlaceError) -> None:
        self_seen.append(error)
        unsubscribe_self()

    def broken_listener(error: PlaceError) -> None:
        _ = error
        raise RuntimeError(canary)

    unsubscribe_self = client.on_error(remove_self)
    _ = client.on_error(broken_listener)
    _ = client.on_error(seen.append)
    first = PlaceTransientAuthError("first")
    second = PlaceTransientAuthError("second")

    with caplog.at_level(logging.WARNING, logger="place.client"):
        conn.on_error(first)
        conn.on_error(second)

    assert seen == [first, second]
    assert self_seen == [first]
    assert canary not in caplog.text
    await client.stop()


async def test_async_refresh_shadow_publishes_get_for_all_devices() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")

    await client.async_refresh_shadow()

    assert conn.published == [(shadow_get_topic("Place_PL1AS_EXAMPLE"), b"")]
    await client.stop()


async def test_async_refresh_shadow_publishes_get_for_one_device() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE", "Place_PL1AS_OTHER")

    await client.async_refresh_shadow("Place_PL1AS_EXAMPLE")

    assert conn.published == [(shadow_get_topic("Place_PL1AS_EXAMPLE"), b"")]
    await client.stop()
