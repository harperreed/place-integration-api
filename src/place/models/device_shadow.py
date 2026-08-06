# ABOUTME: Public data model for the Place device shadow — hazards (smoke/CO/heat plus
# ABOUTME: AQI/VOC/gas) and the live telemetry a real PL1AS reports (CO ppm, temp, humidity, …).
"""Public data models for the Place device shadow.

A device's *reported* shadow carries its hazards and live telemetry, and this
model parses the whole of it:

* The smoke / CO / heat hazards the integration has always consumed.
* The extra hazards larger models add — AQI, VOC, explosive gas (absent hardware
  parses to ``NOT_PRESENT``).
* The live status a real PL1AS emits over shadow/get — CO ppm, CO accumulation,
  room/board temperature, humidity, the raw dual-wavelength smoke optics, wifi
  signal, battery, motion sensitivity, and the nightlight.

Each added field is ``None`` (telemetry) or ``NOT_PRESENT`` (hazards) when a
given model doesn't emit it, so absence stays distinct from a real zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class AlarmStatus(IntEnum):
    """Alarm status values."""

    IDLE = 0
    TEST = 1
    PRE_ALARM = 2
    ALARM = 3
    CRITICAL_ALARM = 4
    HUSHED = 5
    NOT_PRESENT = 6


def _parse_alarm(value: Any) -> AlarmStatus:
    """Convert a raw shadow value to an AlarmStatus."""
    if value is None:
        return AlarmStatus.NOT_PRESENT
    try:
        return AlarmStatus(int(value))
    except (ValueError, TypeError):
        return AlarmStatus.NOT_PRESENT


def _num(value: Any) -> float | None:
    """Return a numeric shadow value unchanged, or None if absent/non-numeric.

    bool is rejected on purpose: JSON booleans are flags, not telemetry, and
    bool is a subclass of int in Python.
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


# Live status fields a real PL1AS reports, mapped snake_case attr -> shadow key.
# Grounded in an actual device's shadow/get (see tests). Extend as other models
# are observed; unknown keys simply stay None.
_TELEMETRY = {
    "co_ppm": "coPpm",
    "co_accumulation": "coAccumulation",
    "temperature_c": "temperatureC",
    "board_temp_c": "boardTempC",
    "humidity": "humidity",
    "blue_front_scatter": "blueFrontScatter",
    "blue_back_scatter": "blueBackScatter",
    "ir_front_scatter": "irFrontScatter",
    "ir_back_scatter": "irBackScatter",
    "wifi_signal_strength": "wifiSignalStrength",
    "battery_status": "batteryStatus",
    "motion_sensitivity": "motionSensitivity",
}


@dataclass
class NightLight:
    """The nightlight's reported state (on/off + rgba; alpha is brightness).

    This is the closest thing the hardware exposes to a light level — an
    *output* setting, not an ambient-light reading (the device has no lux sensor).
    """

    on: bool | None = None
    red: float | None = None
    green: float | None = None
    blue: float | None = None
    alpha: float | None = None


def _parse_night_light(value: Any) -> NightLight | None:
    """Parse a ``nightLightSettings`` block into a NightLight, or None if absent."""
    if not isinstance(value, dict):
        return None
    rgba = value.get("rgbaLed")
    if not isinstance(rgba, dict):
        rgba = {}
    on = value.get("on")
    return NightLight(
        on=on if isinstance(on, bool) else None,
        red=_num(rgba.get("red")),
        green=_num(rgba.get("green")),
        blue=_num(rgba.get("blue")),
        alpha=_num(rgba.get("alpha")),
    )


@dataclass
class PlaceDeviceShadow:
    """The Place device shadow: hazards plus the live telemetry a device reports."""

    co_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT
    heat_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT
    smoke_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT
    aqi_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT
    voc_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT
    explosive_gas_alarm_status: AlarmStatus = AlarmStatus.NOT_PRESENT

    # Live telemetry the device reports (None when a given model doesn't emit it).
    # Typed float, not int: these are JSON numbers passed through unchanged (see
    # _num) — a device may send an integer or a decimal, and float admits both.
    co_ppm: float | None = None
    co_accumulation: float | None = None
    temperature_c: float | None = None
    board_temp_c: float | None = None
    humidity: float | None = None
    blue_front_scatter: float | None = None
    blue_back_scatter: float | None = None
    ir_front_scatter: float | None = None
    ir_back_scatter: float | None = None
    wifi_signal_strength: float | None = None
    battery_status: float | None = None
    motion_sensitivity: float | None = None
    night_light: NightLight | None = None

    @staticmethod
    def from_shadow(shadow: dict[str, Any]) -> "PlaceDeviceShadow":
        """Parse a full shadow from a raw dict."""
        reported = shadow.get("state", shadow).get("reported", shadow)
        return PlaceDeviceShadow(
            co_alarm_status=_parse_alarm(reported.get("coAlarmStatus")),
            heat_alarm_status=_parse_alarm(reported.get("heatAlarmStatus")),
            smoke_alarm_status=_parse_alarm(reported.get("smokeAlarmStatus")),
            aqi_alarm_status=_parse_alarm(reported.get("aqiAlarmStatus")),
            voc_alarm_status=_parse_alarm(reported.get("vocAlarmStatus")),
            explosive_gas_alarm_status=_parse_alarm(
                reported.get("explosiveGasAlarmStatus")
            ),
            night_light=_parse_night_light(reported.get("nightLightSettings")),
            **{attr: _num(reported.get(key)) for attr, key in _TELEMETRY.items()},
        )

    def merge(self, partial: dict[str, Any]) -> None:
        """Merge a sparse shadow update into the current state."""
        reported = partial.get("state", partial).get("reported", partial)
        if "coAlarmStatus" in reported:
            self.co_alarm_status = _parse_alarm(reported["coAlarmStatus"])
        if "heatAlarmStatus" in reported:
            self.heat_alarm_status = _parse_alarm(reported["heatAlarmStatus"])
        if "smokeAlarmStatus" in reported:
            self.smoke_alarm_status = _parse_alarm(reported["smokeAlarmStatus"])
        if "aqiAlarmStatus" in reported:
            self.aqi_alarm_status = _parse_alarm(reported["aqiAlarmStatus"])
        if "vocAlarmStatus" in reported:
            self.voc_alarm_status = _parse_alarm(reported["vocAlarmStatus"])
        if "explosiveGasAlarmStatus" in reported:
            self.explosive_gas_alarm_status = _parse_alarm(
                reported["explosiveGasAlarmStatus"]
            )
        for attr, key in _TELEMETRY.items():
            if key in reported:
                setattr(self, attr, _num(reported[key]))
        if "nightLightSettings" in reported:
            self.night_light = _parse_night_light(reported["nightLightSettings"])
