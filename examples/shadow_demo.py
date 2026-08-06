# ABOUTME: Runnable demo of the Place device shadow — parses hazards + live telemetry
# ABOUTME: from a sample reported shadow and builds a desired-state write. No network.
"""Illustrative demo (no network, no device).

Run: uv run python examples/shadow_demo.py
"""

from __future__ import annotations

from place.messages import desired_shadow_update
from place.models import PlaceDeviceShadow

# A sample reported shadow of the shape AWS IoT delivers. Values are illustrative.
SAMPLE_SHADOW = {
    "state": {
        "reported": {
            "smokeAlarmStatus": 0,          # IDLE
            "coAlarmStatus": 3,             # ALARM
            "heatAlarmStatus": 0,           # IDLE
            "aqiAlarmStatus": 2,            # PRE_ALARM
            "vocAlarmStatus": 0,            # IDLE
            "explosiveGasAlarmStatus": 5,   # HUSHED
            "coPpm": 9,
            "temperatureC": 22.5,
            "humidity": 48,
            "wifiSignalStrength": -58,
            "motionSensitivity": 1,
            "nightLightSettings": {
                "on": True,
                "rgbaLed": {"red": 224, "green": 19, "blue": 4, "alpha": 255},
            },
        }
    }
}


def main() -> None:
    shadow = PlaceDeviceShadow.from_shadow(SAMPLE_SHADOW)

    print("Hazard statuses:")
    for name, value in [
        ("smoke", shadow.smoke_alarm_status),
        ("co", shadow.co_alarm_status),
        ("heat", shadow.heat_alarm_status),
        ("aqi", shadow.aqi_alarm_status),
        ("voc", shadow.voc_alarm_status),
        ("explosive_gas", shadow.explosive_gas_alarm_status),
    ]:
        print(f"  {name:<14} {value.name}")

    print("\nLive telemetry:")
    print(f"  co_ppm             {shadow.co_ppm}")
    print(f"  temperature_c      {shadow.temperature_c}")
    print(f"  humidity           {shadow.humidity}")
    print(f"  wifi_signal        {shadow.wifi_signal_strength} dBm")
    print(f"  motion_sensitivity {shadow.motion_sensitivity}")
    if shadow.night_light is not None:
        nl = shadow.night_light
        print(
            f"  night_light        on={nl.on} "
            f"rgba=({nl.red},{nl.green},{nl.blue},{nl.alpha})"
        )

    topic, payload = desired_shadow_update("thing-001", {"exampleDesiredField": 1})
    print("\nDesired-state write (envelope only; field name illustrative — not published):")
    print(f"  topic:   {topic}")
    print(f"  payload: {payload}")


if __name__ == "__main__":
    main()
