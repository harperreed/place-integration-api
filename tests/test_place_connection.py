# ABOUTME: Tests for PlaceConnection — connect/subscribe/publish/dispatch, the not-connected
# ABOUTME: publish guard, subscription dedup, add-order replay, and reconnect backoff on MqttError.
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import TracebackType

import pytest
from aiomqtt import MqttError

from place.config import PlaceConfig
from place.exceptions import PlaceConnectionError
from place.models import Credentials
from place.transport import MqttTransport, PlaceConnection, TransportFactory


def _creds() -> Credentials:
    return Credentials("AK", "secret", "tok", "idid")


class FakeAuth:
    def __init__(self, creds: Credentials) -> None:
        self._creds: Credentials = creds
        self.calls: int = 0

    async def async_get_iot_credentials(self) -> Credentials:
        self.calls += 1
        return self._creds


class ScriptedTransport:
    """A one-shot fake connection: replays scripted messages, then stops the loop.

    An optional ``hook`` runs after the scripted messages drain but before the
    loop stops, so a test can act (e.g. call ``conn.publish``) while the
    transport is still live.
    """

    def __init__(
        self,
        messages: list[tuple[str, bytes]],
        subs: list[str],
        published: list[tuple[str, bytes]],
        stop: Callable[[], None],
        hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._messages: list[tuple[str, bytes]] = messages
        self._subs: list[str] = subs
        self._published: list[tuple[str, bytes]] = published
        self._stop: Callable[[], None] = stop
        self._hook: Callable[[], Awaitable[None]] | None = hook

    async def __aenter__(self) -> "ScriptedTransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        _ = qos
        self._subs.append(topic)

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        _ = qos
        self._published.append((topic, payload))

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        for item in self._messages:
            yield item
        if self._hook is not None:
            await self._hook()
        self._stop()  # drain, then end the loop


async def test_connect_subscribes_publishes_and_dispatches() -> None:
    subs: list[str] = []
    published: list[tuple[str, bytes]] = []
    received: list[tuple[str, bytes]] = []
    states: list[bool] = []
    auth = FakeAuth(_creds())

    factory: TransportFactory = lambda cfg, creds: ScriptedTransport(
        [("$aws/things/T/shadow/get/accepted", b"{}")], subs, published, conn.stop
    )
    conn = PlaceConnection(
        PlaceConfig(),
        auth,
        transport_factory=factory,
        on_message=lambda t, p: received.append((t, p)),
        on_state=states.append,
    )
    conn.add_subscription("$aws/things/T/shadow/#")
    conn.add_connect_publish("$aws/things/T/shadow/get")

    await conn.run()

    assert subs == ["$aws/things/T/shadow/#"]
    assert published == [("$aws/things/T/shadow/get", b"")]
    assert received == [("$aws/things/T/shadow/get/accepted", b"{}")]
    assert states == [True, False]
    assert auth.calls == 1


async def test_publish_before_connect_raises() -> None:
    def _factory_should_not_be_called(cfg: PlaceConfig, creds: Credentials) -> MqttTransport:
        _ = cfg
        _ = creds
        raise AssertionError("transport_factory must not be called when run() is never invoked")

    conn = PlaceConnection(
        PlaceConfig(),
        FakeAuth(_creds()),
        transport_factory=_factory_should_not_be_called,
        on_message=lambda t, p: None,
    )

    with pytest.raises(PlaceConnectionError):
        await conn.publish("$aws/things/T/shadow/get")


async def test_add_subscription_dedup_guard() -> None:
    subs: list[str] = []
    published: list[tuple[str, bytes]] = []
    auth = FakeAuth(_creds())

    factory: TransportFactory = lambda cfg, creds: ScriptedTransport(
        [], subs, published, conn.stop
    )
    conn = PlaceConnection(
        PlaceConfig(),
        auth,
        transport_factory=factory,
        on_message=lambda t, p: None,
    )
    conn.add_subscription("$aws/things/T/shadow/#")
    conn.add_subscription("$aws/things/T/shadow/#")

    await conn.run()

    assert subs == ["$aws/things/T/shadow/#"]


async def test_replay_preserves_add_order() -> None:
    subs: list[str] = []
    published: list[tuple[str, bytes]] = []
    auth = FakeAuth(_creds())

    factory: TransportFactory = lambda cfg, creds: ScriptedTransport(
        [], subs, published, conn.stop
    )
    conn = PlaceConnection(
        PlaceConfig(),
        auth,
        transport_factory=factory,
        on_message=lambda t, p: None,
    )
    conn.add_subscription("a")
    conn.add_subscription("b")
    conn.add_subscription("c")
    conn.add_connect_publish("p1")
    conn.add_connect_publish("p2", b"x")

    await conn.run()

    assert subs == ["a", "b", "c"]
    assert published == [("p1", b""), ("p2", b"x")]


async def test_publish_while_connected_delegates_to_transport() -> None:
    subs: list[str] = []
    published: list[tuple[str, bytes]] = []
    auth = FakeAuth(_creds())

    async def _publish_mid_stream() -> None:
        await conn.publish("$aws/things/T/shadow/get", b"mid-stream")

    hook: Callable[[], Awaitable[None]] = _publish_mid_stream
    factory: TransportFactory = lambda cfg, creds: ScriptedTransport(
        [], subs, published, conn.stop, hook
    )
    conn = PlaceConnection(
        PlaceConfig(),
        auth,
        transport_factory=factory,
        on_message=lambda t, p: None,
    )

    await conn.run()

    assert published == [("$aws/things/T/shadow/get", b"mid-stream")]


class FlakyTransport:
    """Fails with MqttError for the first `fail_times` connects, then drains and stops."""

    def __init__(self, attempt: int, fail_times: int, stop: Callable[[], None]) -> None:
        self._attempt: int = attempt
        self._fail_times: int = fail_times
        self._stop: Callable[[], None] = stop

    async def __aenter__(self) -> "FlakyTransport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        _ = topic
        _ = qos
        return None

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        _ = topic
        _ = payload
        _ = qos
        return None

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        if self._attempt <= self._fail_times:
            raise MqttError("dropped")
        self._stop()
        for _ in ():  # never runs; the empty loop makes this an async generator
            yield ("", b"")


def _flaky_factory(
    fail_times: int, stop_getter: Callable[[], Callable[[], None]]
) -> TransportFactory:
    state = {"n": 0}

    def factory(cfg: PlaceConfig, creds: Credentials) -> MqttTransport:
        _ = cfg
        _ = creds
        state["n"] += 1
        return FlakyTransport(state["n"], fail_times, stop_getter())

    return factory


async def test_backoff_grows_then_connects() -> None:
    slept: list[float] = []
    sleep_fn: Callable[[float], Awaitable[None]] = lambda d: _noop_sleep(slept, d)
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=1.0, reconnect_max_sec=60.0),
        FakeAuth(_creds()),
        transport_factory=_flaky_factory(2, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=sleep_fn,
    )
    await conn.run()
    assert slept == [1.0, 2.0]  # 2 failures -> two backoff sleeps, then success


async def test_backoff_is_capped() -> None:
    slept: list[float] = []
    sleep_fn: Callable[[float], Awaitable[None]] = lambda d: _noop_sleep(slept, d)
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=1.0, reconnect_max_sec=1.5),
        FakeAuth(_creds()),
        transport_factory=_flaky_factory(3, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=sleep_fn,
    )
    await conn.run()
    assert slept == [1.0, 1.5, 1.5]  # 1.0, 2.0->cap 1.5, 4.0->cap 1.5


async def _noop_sleep(record: list[float], delay: float) -> None:
    record.append(delay)


def test_seconds_until_refresh_uses_margin() -> None:
    def _factory_unused(cfg: PlaceConfig, creds: Credentials) -> MqttTransport:
        _ = cfg
        _ = creds
        raise AssertionError("transport_factory must not be called in this test")

    conn = PlaceConnection(
        PlaceConfig(creds_refresh_margin_sec=600),
        FakeAuth(_creds()),
        transport_factory=_factory_unused,
        on_message=lambda t, p: None,
    )
    far = _creds()
    far.expiration = datetime.now(timezone.utc) + timedelta(seconds=3600)
    secs = conn._seconds_until_refresh(far)  # pyright: ignore[reportPrivateUsage]
    assert secs is not None and 2900 < secs <= 3000

    unknown = _creds()  # expiration is None
    assert conn._seconds_until_refresh(unknown) is None  # pyright: ignore[reportPrivateUsage]

    stale = _creds()
    stale.expiration = datetime.now(timezone.utc) - timedelta(seconds=10)
    # clamped, never negative
    assert conn._seconds_until_refresh(stale) == 0.0  # pyright: ignore[reportPrivateUsage]


async def test_each_connect_fetches_fresh_credentials() -> None:
    auth = FakeAuth(_creds())
    sleep_fn: Callable[[float], Awaitable[None]] = lambda d: _noop_sleep([], d)
    conn = PlaceConnection(
        PlaceConfig(reconnect_min_sec=0.0, reconnect_max_sec=0.0),
        auth,
        transport_factory=_flaky_factory(1, lambda: conn.stop),
        on_message=lambda t, p: None,
        sleep=sleep_fn,
    )
    await conn.run()
    assert auth.calls == 2  # one failed connect + one successful, each fetched creds
