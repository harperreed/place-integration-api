# ABOUTME: Read-only live watcher — subscribes to your household MQTT tree + each shadow and
# ABOUTME: prints every message as it arrives, so you can see live motion. Never publishes/writes.
"""Live event watcher (THROWAWAY, not covered by unit tests).

It subscribes to your household topic tree and each device's shadow, then prints
messages as they arrive — so you can walk past a unit and SEE what it emits
(motion, shadow updates, presence). READ-ONLY: it only subscribes. It never
publishes, writes desired state, or sends a device command, so it cannot change
your hardware.

Topic shapes (confirmed from the app binary):
    connectedsmoke/household/{householdId}/#                      household-level
    connectedsmoke/household/{householdId}/device/{deviceId}/#    device-level
    $aws/things/{thing}/shadow/#                                  AWS IoT shadow

The household id is the leading UUID of a thing name (``{household}_{device}_…``);
it's derived from DISCOVER, not hard-coded. Each shadow update prints as a
field-level diff (what changed since the previous version), so you can see
exactly what a walk-by moves; a changed motion/presence field flags loud
(`<<< MOTION`). Non-shadow messages (a dedicated event topic, if one exists)
print whole and untruncated.

Run it in a real terminal (prompts for an MFA code after the password step):
    uv run python examples/watch_events.py

Stop with Ctrl-C. Optional auto-stop:
    WATCH_SECONDS=120 uv run python examples/watch_events.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import decouple
from aiohttp import ClientSession

from place.auth import get_iot_credentials
from place.config import IOT_ENDPOINT
from place.messages import PlaceMessages, parse_payload
from place.models.device_event import DeviceEvent
from place.mqtt_client import MqttClient
from place.provider import Provider

# Reuse the snapshot probe's auth flow (same directory) rather than duplicating
# the SRP + MFA dance. When run as a script examples/ is already on sys.path;
# insert it explicitly so other launchers (importlib) resolve it too.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_real_device import _StaticAuth, _authenticate  # noqa: E402


def _household_ids(thing_names: list[str]) -> list[str]:
    """Household id is the leading UUID of a thing name: {household}_{device}_…"""
    return sorted({t.split("_", 1)[0] for t in thing_names if "_" in t})


def _looks_like_motion(topic: str, raw: bytes) -> bool:
    """Loose flag for unrecognized topics: does this message mention motion at all?"""
    blob = topic.lower().encode("utf-8") + b" " + (raw or b"").lower()
    return b"motion" in blob


# On one shadow write AWS echoes the change three ways: the delta on
# `.../shadow/update`, plus an `.../update/accepted` on both the $aws and
# household topics. `.../shadow/update/documents` carries BOTH previous and
# current, so we diff off that one and fold the redundant echoes away.
_SHADOW_ECHO_SUFFIXES = (
    "/shadow/update",
    "/shadow/update/accepted",
    "/shadow/get/accepted",
)

# Changed-key names that signal a live motion/presence event. Matched only on
# the DIFF (keys that actually changed), so the static faults.motionSensor and
# the motionSensitivity *setting* never trip it — only a value that moved.
_MOTION_HINTS = ("motion", "occup", "presence", "pir")


def _reported_of(doc: object) -> dict:
    """Pull state.reported out of one side (previous/current) of a shadow document."""
    if isinstance(doc, dict):
        state = doc.get("state")
        if isinstance(state, dict) and isinstance(state.get("reported"), dict):
            return state["reported"]
    return {}


def _flatten(value: dict, prefix: str = "") -> dict:
    """Flatten nested dicts to dotted keys so a nested change reads as one key."""
    flat: dict = {}
    for key, val in value.items():
        dotted = f"{prefix}{key}"
        if isinstance(val, dict):
            flat.update(_flatten(val, dotted + "."))
        else:
            flat[dotted] = val
    return flat


def _diff_reported(previous: dict, current: dict) -> dict:
    """Keys whose reported value changed between two shadow versions: {key: (old, new)}."""
    prev_flat, curr_flat = _flatten(previous), _flatten(current)
    changed = {}
    for key in sorted(set(prev_flat) | set(curr_flat)):
        old, new = prev_flat.get(key, "∅"), curr_flat.get(key, "∅")
        if old != new:
            changed[key] = (old, new)
    changed.pop("lastUpdated", None)  # bookkeeping; ticks on every write
    return changed


async def _discover(access_token: str) -> list:
    async with ClientSession() as session:
        return await Provider(_StaticAuth(session, access_token)).discover()


def watch(
    creds, household_ids: list[str], thing_names: list[str], seconds: float | None
) -> None:
    """Subscribe to the household tree + each shadow and print messages. READ-ONLY."""
    client = MqttClient(endpoint=IOT_ENDPOINT, credentials=creds)
    messages = PlaceMessages(client)

    def on_connect() -> None:
        for hid in household_ids:
            messages.subscribe_household(hid)
            print(f"  subscribed: connectedsmoke/household/{hid}/#")
        for name in thing_names:
            messages.subscribe_shadow(name)
        print(f"  subscribed: $aws/things/<thing>/shadow/#  (x{len(thing_names)})")
        limit = f"for {seconds:.0f}s" if seconds else "until Ctrl-C"
        print(f"\nWatching {limit} — walk past a unit to trigger motion.\n")

    def on_message(topic: str, payload: bytes) -> None:
        stamp = time.strftime("%H:%M:%S")

        # (1) Discrete device event — this is where live motion lands. Parsed by
        # the same keepable model the SDK ships, so there's one source of truth.
        event = DeviceEvent.from_message(topic, parse_payload(payload))
        if event is not None:
            flag = "   <<< MOTION" if event.is_motion else ""
            print(
                f"[{stamp}] EVENT {event.event_type}{flag}  device={event.device_id} "
                f"seq={event.seq} {event.timestamp or ''}".rstrip()
            )
            return

        # (2) Shadow document — diff previous→current so we see exactly what moved.
        if topic.endswith("/shadow/update/documents"):
            doc = parse_payload(payload)
            changed = _diff_reported(
                _reported_of(doc.get("previous")), _reported_of(doc.get("current"))
            )
            if not changed:
                return
            reported = _reported_of(doc.get("current")) or _reported_of(doc.get("previous"))
            dev = reported.get("deviceId") or PlaceMessages.thing_name_from_topic(topic) or "?"
            motion = any(
                any(hint in key.lower() for hint in _MOTION_HINTS) for key in changed
            )
            shown = list(changed.items())[:15]
            pretty = ", ".join(f"{key} {old}→{new}" for key, (old, new) in shown)
            if len(changed) > len(shown):
                pretty += f"  (+{len(changed) - len(shown)} more)"
            flag = "   <<< MOTION" if motion else ""
            print(f"[{stamp}] {dev}  changed:{flag}\n           {pretty}")
            return

        # (3) The other two echoes of that same shadow write — fold them away.
        if topic.endswith(_SHADOW_ECHO_SUFFIXES):
            return

        # (4) Anything else — print whole, never truncated, loosely flagged.
        text = (payload or b"").decode("utf-8", "replace")
        flag = "   <<< MOTION?" if _looks_like_motion(topic, payload) else ""
        print(f"[{stamp}] {topic}{flag}\n           {text or '(empty)'}")

    client.connect(on_message=on_message, on_connect=on_connect)
    client.loop_start()
    try:
        if seconds:
            time.sleep(seconds)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping (Ctrl-C).")
    finally:
        client.disconnect()
        client.loop_stop()


def main() -> None:
    username = decouple.config("COGNITO_USERNAME", default="").strip()
    password = decouple.config("COGNITO_PASSWORD", default="")
    if not username or not password:
        sys.exit("Set COGNITO_USERNAME and COGNITO_PASSWORD in place-integration-api/.env")

    masked = username[:2] + "***" + ("@" + username.split("@", 1)[1] if "@" in username else "")
    print(f"Authenticating as {masked} (Cognito SRP)...")
    auth_result = _authenticate(username, password)
    access_token = auth_result["AccessToken"]
    creds = get_iot_credentials(auth_result["IdToken"], access_token)
    print(f"Exchanged for temporary IoT credentials (identity {creds.identity_id[:18]}…).\n")

    devices = asyncio.run(_discover(access_token))
    thing_names = [d.thing_name for d in devices]
    household_ids = _household_ids(thing_names)
    print(
        f"DISCOVER: {len(thing_names)} device(s) across "
        f"household(s): {', '.join(household_ids) or '?'}"
    )

    try:
        seconds = float(os.environ.get("WATCH_SECONDS", "")) or None
    except ValueError:
        seconds = None

    watch(creds, household_ids, thing_names, seconds)
    print("\nDone. (Read-only: subscribe/watch only — no publishes, no writes, no commands.)")


if __name__ == "__main__":
    main()
