# examples/shadow_snapshot.py
# ABOUTME: One-shot PLACE shadow snapshot from discovery — no MQTT. Throwaway script;
# ABOUTME: run against a real account (reads only). Creds from env or .env: PLACE_USERNAME / PLACE_PASSWORD.
import asyncio
from getpass import getpass

import aiohttp
import decouple

from place.auth.cognito_auth import CognitoAuth
from place.config import PlaceConfig
from place.exceptions import MfaRequired
from place.models import PlaceDeviceShadow
from place.provider import Provider


async def main() -> None:
    config = PlaceConfig.from_env()
    async with aiohttp.ClientSession() as session:
        auth = CognitoAuth(config, session)
        try:
            await auth.authenticate(
                str(decouple.config("PLACE_USERNAME")),
                str(decouple.config("PLACE_PASSWORD")),
            )
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))

        for device in await Provider(auth).discover():
            shadow = PlaceDeviceShadow.from_shadow(device.shadow)
            print(f"{device.thing_name} (online={device.online}): {shadow}")


if __name__ == "__main__":
    asyncio.run(main())
