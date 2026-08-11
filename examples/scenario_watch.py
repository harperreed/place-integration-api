# examples/scenario_watch.py
# ABOUTME: Bounded, read-only SDK scenario run — snapshots every device's live shadow,
# ABOUTME: then tracks shadow updates and motion pulses for a window and prints a summary.
import asyncio
import io
import sys
from collections import Counter
from getpass import getpass

import aiohttp
import decouple

from place.auth.cognito_auth import CognitoAuth
from place.auth.token_cache import FileTokenCache
from place.client import PlaceClient
from place.config import PlaceConfig
from place.device import PlaceDevice
from place.exceptions import MfaRequired
from place.models import DeviceEvent

# Seconds to let shadow/get replies land before snapshotting. The watch window
# afterward defaults to 70s; override via SCENARIO_WINDOW_SEC for a longer walk.
SNAPSHOT_DELAY_SEC = 6.0


def _fmt(value: object, suffix: str = "") -> str:
    """Compact display: dash for absent, one decimal for floats."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def _snapshot(devices: dict[str, PlaceDevice]) -> None:
    print(f"\n=== Snapshot: {len(devices)} device(s) ===")
    for dev in devices.values():
        s = dev.shadow
        print(
            f"  {dev.name or dev.thing_name}  model={dev.model or '—'} "
            f"fw={dev.firmware_version or '—'} loc={dev.location or '—'} "
            f"online={dev.online}"
        )
        print(
            f"      temp={_fmt(s.temperature_c, '°C')} rh={_fmt(s.humidity, '%')} "
            f"co={_fmt(s.co_ppm, 'ppm')} smoke={s.smoke_alarm_status.name} "
            f"co_alarm={s.co_alarm_status.name} "
            # faults is a dict of per-sensor bits; a fault is active only if one is set.
            f"faults={'ACTIVE' if (s.faults and any(s.faults.values())) else 'clear'}"
        )


async def main() -> None:
    # Stream each line as it prints even when stdout is redirected to a file,
    # so a live `> log` run shows events as they fire instead of buffering to exit.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    window = decouple.config("SCENARIO_WINDOW_SEC", default=70.0, cast=float)
    config = PlaceConfig.from_env()
    async with aiohttp.ClientSession() as session:
        cache = FileTokenCache.default()
        print(f"Token cache: {cache.path}")
        auth = CognitoAuth(config, session, token_cache=cache)
        try:
            await auth.authenticate(
                str(decouple.config("PLACE_USERNAME")),
                str(decouple.config("PLACE_PASSWORD")),
            )
        except MfaRequired as mfa:
            await auth.submit_mfa(getpass(f"MFA code ({mfa.challenge_name}): "))
        print("Authenticated.")

        updates: Counter[str] = Counter()
        motion: Counter[str] = Counter()
        events: list[DeviceEvent] = []

        client = PlaceClient.create(config, auth)

        def on_update(dev: PlaceDevice) -> None:
            updates[dev.name or dev.thing_name] += 1
            print(
                f"[update] {dev.name or dev.thing_name} "
                f"temp={_fmt(dev.shadow.temperature_c, '°C')} "
                f"co={_fmt(dev.shadow.co_ppm, 'ppm')} "
                f"smoke={dev.shadow.smoke_alarm_status.name}"
            )

        def on_event(e: DeviceEvent) -> None:
            events.append(e)
            key = e.thing_name or e.device_id or "?"
            if e.is_motion:
                motion[key] += 1
            tag = "  <-- MOTION" if e.is_motion else ""
            print(f"[event] {e.event_type} device={e.device_id} seq={e.seq}{tag}")

        def on_conn(up: bool) -> None:
            print(f"[conn] {'up' if up else 'down'}")

        _ = client.on_update(on_update)
        _ = client.on_event(on_event)
        _ = client.on_connection_change(on_conn)

        async with client:
            await asyncio.sleep(SNAPSHOT_DELAY_SEC)
            _snapshot(client.devices)
            print(
                f"\nWatching {len(client.devices)} device(s) for {window:.0f}s — "
                "walk past a detector to fire a motion pulse.\n"
            )
            await asyncio.sleep(window)

        print("\n=== Summary ===")
        print(f"  shadow updates: {dict(updates) or 'none'}")
        print(f"  motion pulses:  {dict(motion) or 'none'}")
        print(f"  total events:   {len(events)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
