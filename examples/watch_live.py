# examples/watch_live.py
# ABOUTME: Live PLACE watcher — streams shadow updates and household events, flagging
# ABOUTME: motionDetected pulses. Throwaway script; read-only. Set PLACE_HOUSEHOLD_IDS
# ABOUTME: (comma-separated) to receive the live motion firehose.
import asyncio
from getpass import getpass

import aiohttp
import decouple

from place.auth.cognito_auth import CognitoAuth
from place.client import PlaceClient
from place.config import PlaceConfig
from place.exceptions import MfaRequired


async def main() -> None:
    config = PlaceConfig.from_env()
    household_ids = [h for h in str(decouple.config("PLACE_HOUSEHOLD_IDS", default="")).split(",") if h]
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(
                str(decouple.config("PLACE_USERNAME")), str(decouple.config("PLACE_PASSWORD"))
            )
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        client = PlaceClient.create(config, auth, household_ids=household_ids)
        client.on_event(
            lambda e: print(f"[event] {e.event_type} device={e.device_id} seq={e.seq}")
        )
        async with client:
            print(f"Watching {len(client.devices)} device(s). Ctrl-C to stop.")
            async for device in client.updates():
                motion = (
                    "  <-- MOTION"
                    if device.last_event is not None and device.last_event.is_motion
                    else ""
                )
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
