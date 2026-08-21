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


class SlowCancelConnection(FakeConnection):
    """Expose the real cancellation boundary inside connection-task cleanup."""

    def __init__(
        self,
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> None:
        super().__init__(on_message, on_state, on_error)
        self.cancel_received = asyncio.Event()
        self.finish_cancel = asyncio.Event()

    @override
    async def run(self) -> None:
        """Hold the owned task after it receives its genuine cancellation."""
        self.run_calls += 1
        self.started = True
        try:
            _ = await self._gate.wait()
        except asyncio.CancelledError:
            self.cancel_received.set()
            await self.finish_cancel.wait()
            raise


def _discover(thing: str) -> DiscoverDevice:
    return DiscoverDevice.from_dict(
        {"thingName": thing, "deviceId": "dev-1", "shadow": {}}
    )


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


async def test_client_start_and_refresh_publish_only_empty_shadow_gets() -> None:
    thing_names = ("Place_PL1AS_A", "Place_PL1AS_B")
    client, conn = await _started_client(*thing_names)

    try:
        await client.async_refresh_shadow()

        expected = [(shadow_get_topic(name), b"") for name in thing_names]
        assert conn.connect_publishes == expected
        assert conn.published == expected
        assert all(
            topic.endswith("/shadow/get") and payload == b""
            for topic, payload in conn.connect_publishes + conn.published
        )
    finally:
        await client.stop()


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


async def _started_slow_cancel_client() -> tuple[PlaceClient, SlowCancelConnection]:
    created: list[SlowCancelConnection] = []

    def connection_factory(
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None],
        on_error: Callable[[PlaceError], None],
    ) -> SlowCancelConnection:
        connection = SlowCancelConnection(on_message, on_state, on_error)
        created.append(connection)
        return connection

    client = PlaceClient(
        PlaceConfig(),
        auth=cast(CognitoAuth, object()),
        provider=FakeProvider([_discover("Place_PL1AS_EXAMPLE")]),
        connection_factory=connection_factory,
    )
    await client.start()
    return client, created[0]


async def test_stop_consumes_owned_connection_task_cancellation() -> None:
    client, connection = await _started_slow_cancel_client()
    stop = asyncio.create_task(client.stop())
    await connection.cancel_received.wait()

    connection.finish_cancel.set()
    await stop
    await client.stop()

    assert stop.cancelled() is False
    assert connection.stopped is True


async def test_stop_propagates_caller_cancellation_and_remains_idempotent() -> None:
    client, connection = await _started_slow_cancel_client()
    stop = asyncio.create_task(client.stop())
    await connection.cancel_received.wait()

    stop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop
    await client.stop()

    assert stop.cancelled() is True
    assert connection.stopped is True


async def test_stop_completes_inside_handler_for_prior_caller_cancellation() -> None:
    client, connection = await _started_slow_cancel_client()
    handler_entered = asyncio.Event()
    cleanup_completed = asyncio.Event()
    wait_forever = asyncio.Event()

    async def cancel_then_clean_up() -> None:
        handler_entered.set()
        try:
            await wait_forever.wait()
        except asyncio.CancelledError:
            connection.finish_cancel.set()
            await client.stop()
            cleanup_completed.set()
            raise

    caller = asyncio.create_task(cancel_then_clean_up())
    await handler_entered.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    await client.stop()

    assert cleanup_completed.is_set()
    assert caller.cancelled() is True
    assert connection.stopped is True


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


async def test_identical_reported_shadow_advances_liveness_and_emits_update() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    device = client.devices["Place_PL1AS_EXAMPLE"]
    _ = device.apply_shadow({"state": {"reported": {"coPpm": 12}}}, now=-1.0)
    previous_shadow_at = device.last_shadow_at
    updates: list[PlaceDevice] = []
    _ = client.on_update(updates.append)

    conn.on_message(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":12}}}',
    )

    assert previous_shadow_at is not None
    assert device.last_shadow_at is not None
    assert device.last_shadow_at > previous_shadow_at
    assert updates == [device]
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


async def test_updates_iterator_yields_identical_reported_liveness_reply() -> None:
    client, conn = await _started_client("Place_PL1AS_EXAMPLE")
    conn.on_message(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":1}}}',
    )
    stream = client.updates()

    conn.on_message(
        "$aws/things/Place_PL1AS_EXAMPLE/shadow/get/accepted",
        b'{"state":{"reported":{"coPpm":1}}}',
    )
    device = await asyncio.wait_for(stream.__anext__(), timeout=0.1)

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
