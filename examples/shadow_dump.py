# examples/shadow_dump.py
# ABOUTME: One-shot read-only dump of each device's RAW reported shadow over MQTT
# ABOUTME: (shadow/get). Throwaway diagnostic — reveals keys the model doesn't parse yet.
import asyncio
import json
from getpass import getpass

import aiohttp
import decouple

from place.auth.cognito_auth import CognitoAuth
from place.auth.token_cache import FileTokenCache
from place.config import PlaceConfig
from place.exceptions import MfaRequired
from place.messages import (
    parse_payload,
    shadow_get_topic,
    shadow_subscription_topic,
    thing_name_from_topic,
)
from place.provider import Provider
from place.transport import AiomqttTransport, PlaceConnection

# How long to wait for shadow/get replies before giving up (offline devices
# never answer, so waiting longer than this buys nothing).
CAPTURE_TIMEOUT_SEC = 15.0


async def main() -> None:
    config = PlaceConfig.from_env()
    async with aiohttp.ClientSession() as session:
        cache = FileTokenCache.default()
        print(f"Token cache: {cache.path} (first run does MFA; later runs reuse it)")
        auth = CognitoAuth(config, session, token_cache=cache)
        try:
            await auth.authenticate(
                str(decouple.config("PLACE_USERNAME")),
                str(decouple.config("PLACE_PASSWORD")),
            )
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))
        print("Authenticated.")

        devices = [d for d in await Provider(auth).discover() if d.thing_name]
        print(f"Discovered {len(devices)} device(s). Requesting shadows...")

        seen: dict[str, dict[str, object]] = {}

        def on_message(topic: str, raw: bytes) -> None:
            thing = thing_name_from_topic(topic)
            if thing is None or thing in seen:
                return
            payload = parse_payload(raw)
            reported = payload.get("state", payload).get("reported", payload)
            seen[thing] = reported
            print(f"\n=== RAW reported shadow: {thing} ===")
            print(json.dumps(reported, indent=2, sort_keys=True))

        connection = PlaceConnection(
            config,
            auth,
            transport_factory=lambda cfg, creds: AiomqttTransport(cfg, creds),
            on_message=on_message,
            on_state=lambda up: print("[connection]", "up" if up else "down"),
        )
        for device in devices:
            assert device.thing_name is not None  # filtered above
            connection.add_subscription(shadow_subscription_topic(device.thing_name))
            connection.add_connect_publish(shadow_get_topic(device.thing_name))

        task = asyncio.create_task(connection.run())
        waited = 0.0
        while len(seen) < len(devices) and waited < CAPTURE_TIMEOUT_SEC:
            await asyncio.sleep(0.25)
            waited += 0.25
        connection.stop()
        _ = task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        missing = [d.thing_name for d in devices if d.thing_name not in seen]
        print(f"\nCaptured {len(seen)}/{len(devices)} shadow(s).")
        if missing:
            print(f"No reply (offline?): {', '.join(str(m) for m in missing)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
