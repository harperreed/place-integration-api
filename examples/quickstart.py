# examples/quickstart.py
# ABOUTME: Connect to PLACE, discover devices, and print a live shadow snapshot after
# ABOUTME: the initial shadow/get lands. Throwaway script; read-only against a real account.
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
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(
                str(decouple.config("PLACE_USERNAME")), str(decouple.config("PLACE_PASSWORD"))
            )
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        async with PlaceClient.create(config, auth) as client:
            await asyncio.sleep(3)  # let the initial shadow/get responses arrive
            for device in client.devices.values():
                s = device.shadow
                print(
                    f"{device.thing_name}: online={device.online} "
                    f"smoke={s.smoke_alarm_status.name} co={s.co_alarm_status.name} "
                    f"co_ppm={s.co_ppm} temp={s.temperature_c} humidity={s.humidity}"
                )


if __name__ == "__main__":
    asyncio.run(main())
