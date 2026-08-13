# examples/watch_live.py
# ABOUTME: Live PLACE watcher — streams shadow updates and household events, flagging
# ABOUTME: motionDetected pulses. Throwaway script; read-only. Households (and thus the
# ABOUTME: live motion firehose) are auto-derived from discovery — no extra config.
import asyncio
from getpass import getpass

import aiohttp
import decouple

from place.auth.cognito_auth import CognitoAuth
from place.auth.token_cache import FileTokenCache
from place.client import PlaceClient
from place.config import PlaceConfig
from place.exceptions import MfaRequired


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

        client = PlaceClient.create(config, auth)
        client.on_event(
            lambda e: print(f"[event] {e.event_type} device={e.device_id} seq={e.seq}")
        )
        async with client:
            print(f"Watching {len(client.devices)} device(s). Ctrl-C to stop.")
            async for device in client.updates():
                motion = "  <-- MOTION" if device.motion() else ""
                print(
                    f"[update] {device.name or device.thing_name} "
                    f"co_ppm={device.shadow.co_ppm} "
                    f"smoke={device.shadow.smoke_alarm_status.name}{motion}"
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
