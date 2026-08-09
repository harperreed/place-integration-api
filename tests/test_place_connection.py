# ABOUTME: Tests for PlaceConnection — the happy-path connect/subscribe/publish/dispatch
# ABOUTME: cycle, the not-connected publish guard, subscription dedup, and add-order replay.
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType

import pytest

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
