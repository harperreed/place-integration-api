# ABOUTME: Read-only probe against a real Place account — authenticate, DISCOVER, then
# ABOUTME: MQTT shadow/get each device and print its full reported state + the six-hazard parse. No writes.
"""One-off integration probe (THROWAWAY, not covered by unit tests).

It answers one question: which fields does *your* real device actually report?

Flow (all reads): Cognito SRP login (completing an MFA one-time code if the
account requires one) -> temporary IoT credentials -> Provider.discover()
(a single HTTPS POST) -> MQTT shadow/get for each thing (its live reported
state) -> print shadows.

It only reads. An MQTT publish to `shadow/get` is a *read* request — it asks
the cloud for the current shadow. It never writes desired state
(`shadow/update`) or calls any device command (enable/disable, hush, test), so
it cannot change your device.

Credentials (COGNITO_USERNAME, COGNITO_PASSWORD) come from a .env file in the
project root, read via python-decouple — the same mechanism place.config uses.
They are never printed.

Run it in a real terminal. If the account has MFA, it prompts for a one-time
code after the password step — freshest possible, and the only way SMS codes
work (they're texted during that step):
    uv run python examples/probe_real_device.py

Only for a non-interactive context (no TTY, e.g. piped) pass the code inline:
    MFA_CODE=123456 uv run python examples/probe_real_device.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import boto3
import decouple
from aiohttp import ClientSession
from botocore import UNSIGNED
from botocore.config import Config

from place.auth import login, get_iot_credentials
from place.auth.abstract_auth import AbstractAuth
from place.config import COGNITO_CLIENT_ID, IOT_ENDPOINT, REGION
from place.messages import PlaceMessages, parse_payload
from place.mqtt_client import MqttClient
from place.provider import Provider
from place.models import PlaceDeviceShadow

HAZARD_KEYS = [
    "smokeAlarmStatus",
    "coAlarmStatus",
    "heatAlarmStatus",
    "aqiAlarmStatus",
    "vocAlarmStatus",
    "explosiveGasAlarmStatus",
]

# Cognito MFA challenges and the ChallengeResponses key that carries the code.
# SRP login returns one of these instead of tokens when the account has MFA on.
_MFA_CODE_KEYS = {
    "SOFTWARE_TOKEN_MFA": "SOFTWARE_TOKEN_MFA_CODE",  # authenticator app (TOTP)
    "SMS_MFA": "SMS_MFA_CODE",                        # texted code
}

# Grace period to let SUBACKs settle after we subscribe, before we ask for the
# shadow. shadow/get's reply lands on `.../shadow/get/accepted`, so the
# subscription must be live first or we'd miss it. A short wait beats racing the
# broker (this is a throwaway probe — just re-run if a device ever times out).
_SUBSCRIBE_GRACE_SEC = 0.75


class _StaticAuth(AbstractAuth):
    """Minimal AbstractAuth that returns a fixed access token."""

    def __init__(self, session: ClientSession, access_token: str) -> None:
        super().__init__(session)
        self._token = access_token

    async def async_get_access_token(self) -> str:
        return self._token


def _reported(shadow: dict) -> dict:
    """Pull the reported block from a shadow/get payload (state.reported).

    Tolerates a bare reported dict (no envelope) but never treats an AWS error
    payload (code/message) as reported fields.
    """
    if not isinstance(shadow, dict):
        return {}
    state = shadow.get("state")
    if isinstance(state, dict):
        reported = state.get("reported")
        return reported if isinstance(reported, dict) else {}
    if "code" in shadow and "message" in shadow:
        return {}
    return shadow


def _fmt(value: object) -> str:
    """Compact repr for a shadow value; truncate long strings/blobs."""
    text = repr(value)
    return text if len(text) <= 70 else text[:67] + "…"


def _get_mfa_code(challenge: str, params: dict) -> str:
    """Prompt for the one-time code; an MFA_CODE env var overrides for non-TTY runs."""
    code = os.environ.get("MFA_CODE", "").strip()
    if code:
        return code
    dest = params.get("CODE_DELIVERY_DESTINATION")
    where = f" (sent to {dest})" if dest else ""
    try:
        return input(f"Enter your {challenge} one-time code{where}: ").strip()
    except EOFError:
        sys.exit(
            "MFA required but no code was supplied. Re-run with a FRESH code, e.g.:\n"
            "  MFA_CODE=123456 uv run python examples/probe_real_device.py"
        )


def _authenticate(username: str, password: str) -> dict:
    """SRP login, completing an MFA challenge if the account requires one.

    Returns the AuthenticationResult (with IdToken/AccessToken). The MFA step
    mirrors the SDK's own SRP client: an unsigned cognito-idp call to
    respond_to_auth_challenge, carrying the Session from the SRP step.
    """
    tokens = login(username, password)
    if "AuthenticationResult" in tokens:
        return tokens["AuthenticationResult"]

    challenge = tokens.get("ChallengeName")
    if challenge not in _MFA_CODE_KEYS:
        sys.exit(
            f"Login returned a '{challenge}' challenge this probe can't complete "
            "(only SOFTWARE_TOKEN_MFA / SMS_MFA are handled)."
        )

    params = tokens.get("ChallengeParameters", {})
    code = _get_mfa_code(challenge, params)
    # Echo back the identifier Cognito used for this SRP session when present;
    # fall back to the username we logged in with.
    mfa_username = params.get("USER_ID_FOR_SRP") or username

    idp = boto3.client(
        "cognito-idp",
        region_name=REGION,
        config=Config(signature_version=UNSIGNED),
    )
    result = idp.respond_to_auth_challenge(
        ClientId=COGNITO_CLIENT_ID,
        ChallengeName=challenge,
        Session=tokens["Session"],
        ChallengeResponses={
            "USERNAME": mfa_username,
            _MFA_CODE_KEYS[challenge]: code,
        },
    )
    if "AuthenticationResult" not in result:
        sys.exit(
            f"MFA response returned '{result.get('ChallengeName')}', not tokens — "
            "the code was likely wrong or expired. Try again with a fresh one."
        )
    print("MFA accepted.\n")
    return result["AuthenticationResult"]


def fetch_shadows(
    creds, thing_names: list[str], timeout: float = 12.0
) -> dict[str, dict]:
    """Read each thing's live shadow via MQTT shadow/get. READ-ONLY.

    Subscribes to `$aws/things/{thing}/shadow/#`, then publishes an empty
    message to `$aws/things/{thing}/shadow/get` — an AWS IoT *read* that makes
    the cloud reply with the current shadow on `.../shadow/get/accepted`. No
    desired state is ever written.

    Returns {thing_name: parsed get/accepted (or /rejected) payload}, waiting up
    to `timeout` seconds for every thing to answer.
    """
    results: dict[str, dict] = {}
    expected = set(thing_names)
    connected = threading.Event()
    done = threading.Event()

    client = MqttClient(endpoint=IOT_ENDPOINT, credentials=creds)
    messages = PlaceMessages(client)

    def on_connect() -> None:
        for name in thing_names:
            messages.subscribe_shadow(name)
        connected.set()

    def on_message(topic: str, payload: bytes) -> None:
        if not (
            topic.endswith("/shadow/get/accepted")
            or topic.endswith("/shadow/get/rejected")
        ):
            return
        name = PlaceMessages.thing_name_from_topic(topic)
        if name and name not in results:
            results[name] = parse_payload(payload)
            if expected.issubset(results):
                done.set()

    client.connect(on_message=on_message, on_connect=on_connect)
    client.loop_start()
    try:
        if not connected.wait(timeout):
            return results
        time.sleep(_SUBSCRIBE_GRACE_SEC)
        for name in thing_names:
            messages.publish_shadow_get(name)  # publish to shadow/get == read request
        done.wait(timeout)
    finally:
        client.disconnect()
        client.loop_stop()
    return results


async def main() -> None:
    username = decouple.config("COGNITO_USERNAME", default="").strip()
    password = decouple.config("COGNITO_PASSWORD", default="")
    if not username or not password:
        sys.exit(
            "Set COGNITO_USERNAME and COGNITO_PASSWORD in place-integration-api/.env"
        )

    masked = username[:2] + "***" + ("@" + username.split("@", 1)[1] if "@" in username else "")
    print(f"Authenticating as {masked} (Cognito SRP)...")
    auth_result = _authenticate(username, password)
    id_token = auth_result["IdToken"]
    access_token = auth_result["AccessToken"]

    creds = get_iot_credentials(id_token, access_token)
    print(f"Exchanged for temporary IoT credentials (identity {creds.identity_id[:18]}…).\n")

    async with ClientSession() as session:
        provider = Provider(_StaticAuth(session, access_token))
        devices = await provider.discover()

    print(f"DISCOVER returned {len(devices)} device(s).")
    thing_names = [d.thing_name for d in devices]
    print(f"Fetching live shadows via MQTT shadow/get for {len(thing_names)} thing(s)...\n")
    shadows = await asyncio.to_thread(fetch_shadows, creds, thing_names)

    for i, d in enumerate(devices, 1):
        shadow = shadows.get(d.thing_name, {})
        reported = _reported(shadow)
        print(
            f"── Device {i}: {d.device_name or '?'}  "
            f"(thing={d.thing_name}, model={d.model_number}, online={d.online})"
        )
        if not shadow:
            print("   ⚠ no shadow/get reply within the timeout (device offline, or slow).")
        elif "state" not in shadow and "message" in shadow:
            print(f"   ⚠ shadow/get rejected: {shadow.get('message')!r}")

        print("   hazard fields actually present:")
        for key in HAZARD_KEYS:
            present = key in reported
            print(f"     {'✓' if present else '·'} {key:<24} {reported.get(key, '—')}")

        print("   full reported status (every field the device sent — motion / gas / temp / etc.):")
        if reported:
            for key in sorted(reported):
                print(f"     {key:<30} {_fmt(reported[key])}")
        else:
            print("     (nothing reported)")

        parsed = PlaceDeviceShadow.from_shadow(shadow)
        print(
            "   parsed → "
            f"smoke={parsed.smoke_alarm_status.name} "
            f"co={parsed.co_alarm_status.name} "
            f"heat={parsed.heat_alarm_status.name} | "
            f"aqi={parsed.aqi_alarm_status.name} "
            f"voc={parsed.voc_alarm_status.name} "
            f"gas={parsed.explosive_gas_alarm_status.name}\n"
        )

    print("Done. (Read-only: shadow/get only — no desired-state writes, no device commands.)")


if __name__ == "__main__":
    asyncio.run(main())
