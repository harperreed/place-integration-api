# ABOUTME: The async MQTT transport for Place — the SigV4 WebSocket presigner, an
# ABOUTME: MqttTransport seam over aiomqtt, and the self-healing PlaceConnection loop.
from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from types import TracebackType
from typing import Protocol
from urllib.parse import quote

import aiomqtt

from .config import ALGORITHM, PATH, SCHEME, SERVICE, PlaceConfig
from .exceptions import PlaceConnectionError
from .models import Credentials


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest().lower()


def _sign(key: bytes, msg: str) -> bytes:
    return _hmac_sha256(key, msg)


def get_signed_uri(config: PlaceConfig, credentials: Credentials) -> str:
    """Build a SigV4 presigned WSS URL for the AWS IoT MQTT WebSocket endpoint."""
    host = config.iot_endpoint
    region = config.region
    access_key_id = credentials.access_key_id
    secret_access_key = credentials.secret_access_key
    session_token = credentials.session_token

    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    credential_scope = f"{access_key_id}/{date_stamp}/{region}/{SERVICE}/aws4_request"
    signed_headers = "host"
    canonical_headers = f"host:{host}\n"
    payload_hash = _sha256_hex("")

    def enc(s: str) -> str:
        return quote(str(s), safe="")

    query = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": credential_scope,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(config.url_expire_sec),
        "X-Amz-SignedHeaders": signed_headers,
    }
    canonical_query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    canonical_request = "\n".join(
        ["GET", PATH, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    request_hash = _sha256_hex(canonical_request)
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, f"{date_stamp}/{region}/{SERVICE}/aws4_request", request_hash]
    )
    k_date = _sign(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, SERVICE)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    query["X-Amz-Signature"] = signature
    query["X-Amz-Security-Token"] = session_token
    query_string = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    return f"{SCHEME}://{host}{PATH}?{query_string}"


def websocket_options(signed_uri: str, host: str) -> tuple[str, dict[str, str]]:
    """Split a signed WSS URL into the aiomqtt websocket path (with query) + Host header."""
    path_with_query = PATH + signed_uri.split(PATH, 1)[1]
    return path_with_query, {"Host": host}


class MqttTransport(Protocol):
    """A single MQTT connection lifecycle: enter, (un)subscribe, publish, stream messages."""

    async def __aenter__(self) -> "MqttTransport": ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def subscribe(self, topic: str, qos: int = 1) -> None: ...
    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None: ...
    def messages(self) -> AsyncIterator[tuple[str, bytes]]: ...


TransportFactory = Callable[[PlaceConfig, Credentials], MqttTransport]


class AiomqttTransport:
    """MqttTransport backed by aiomqtt over a SigV4-presigned AWS IoT WebSocket."""

    def __init__(self, config: PlaceConfig, credentials: Credentials) -> None:
        signed_uri = get_signed_uri(config, credentials)
        path, headers = websocket_options(signed_uri, config.iot_endpoint)
        client_id = f"{credentials.identity_id}-{uuid.uuid4()}"
        self._client: aiomqtt.Client = aiomqtt.Client(
            hostname=config.iot_endpoint,
            port=443,
            identifier=client_id,
            transport="websockets",
            websocket_path=path,
            websocket_headers=headers,
            tls_params=aiomqtt.TLSParameters(),
            keepalive=config.keep_alive_sec,
        )

    async def __aenter__(self) -> "AiomqttTransport":
        _ = await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        _ = await self._client.subscribe(topic, qos=qos)

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 1) -> None:
        await self._client.publish(topic, payload=payload, qos=qos)

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        async for message in self._client.messages:
            yield str(message.topic), message.payload


class IotCredentialsProvider(Protocol):
    """The auth surface PlaceConnection needs: mint IoT credentials on demand."""

    async def async_get_iot_credentials(self) -> Credentials: ...


class PlaceConnection:
    """A self-healing MQTT session: (re)connects, subscribes, and pumps messages."""

    def __init__(
        self,
        config: PlaceConfig,
        auth: IotCredentialsProvider,
        *,
        transport_factory: TransportFactory,
        on_message: Callable[[str, bytes], None],
        on_state: Callable[[bool], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._config: PlaceConfig = config
        self._auth: IotCredentialsProvider = auth
        self._transport_factory: TransportFactory = transport_factory
        self._on_message: Callable[[str, bytes], None] = on_message
        self._on_state: Callable[[bool], None] | None = on_state
        self._sleep: Callable[[float], Awaitable[None]] = sleep
        self._jitter: Callable[[float], float] = jitter or (lambda d: d)
        self._subscriptions: list[str] = []
        self._connect_publishes: list[tuple[str, bytes]] = []
        self._transport: MqttTransport | None = None
        self._stopped: bool = False

    def add_subscription(self, topic: str) -> None:
        if topic not in self._subscriptions:
            self._subscriptions.append(topic)

    def add_connect_publish(self, topic: str, payload: bytes = b"") -> None:
        self._connect_publishes.append((topic, payload))

    def stop(self) -> None:
        self._stopped = True

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        if self._transport is None:
            raise PlaceConnectionError("not connected")
        await self._transport.publish(topic, payload)

    async def run(self) -> None:
        while not self._stopped:
            creds = await self._auth.async_get_iot_credentials()
            async with self._transport_factory(self._config, creds) as transport:
                self._transport = transport
                try:
                    for topic in self._subscriptions:
                        await transport.subscribe(topic)
                    for topic, payload in self._connect_publishes:
                        await transport.publish(topic, payload)
                    if self._on_state:
                        self._on_state(True)
                    async for topic, payload in transport.messages():
                        self._on_message(topic, payload)
                finally:
                    self._transport = None
                    if self._on_state:
                        self._on_state(False)
