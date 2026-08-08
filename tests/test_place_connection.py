# ABOUTME: Tests for PlaceConnection's happy path — connect, subscribe, fire
# ABOUTME: on-connect publishes, and dispatch messages to a callback, against a fake transport.
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import TracebackType

from place.config import PlaceConfig
from place.models import Credentials
from place.transport import PlaceConnection, TransportFactory


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
    """A one-shot fake connection: replays scripted messages, then stops the loop."""

    def __init__(
        self,
        messages: list[tuple[str, bytes]],
        subs: list[str],
        published: list[tuple[str, bytes]],
        stop: Callable[[], None],
    ) -> None:
        self._messages: list[tuple[str, bytes]] = messages
        self._subs: list[str] = subs
        self._published: list[tuple[str, bytes]] = published
        self._stop: Callable[[], None] = stop

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
