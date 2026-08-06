"""Tests for the Place device shadow data model.

The model parses the whole reported shadow: the smoke / CO / heat hazards the
integration has always consumed, the extra hazards larger models add (AQI, VOC,
explosive gas), and the live telemetry a real PL1AS emits over shadow/get. The
telemetry fixture below is grounded in an actual device's reported values (the
field shapes and magnitudes the hardware really sends); it carries no device or
household identifiers.
"""

from place.models.device_shadow import AlarmStatus, PlaceDeviceShadow

FULL_SHADOW = {
    "state": {
        "reported": {
            "deviceId": "device-001",
            "model": "MODEL-X",
            "fwPackageId": "fw-1.0",
            "autoUpdate": True,
            "secureBuild": True,
            "coAlarmStatus": 0,
            "heatAlarmStatus": 3,
            "smokeAlarmStatus": 5,
        }
    }
}

SIX_HAZARD_SHADOW = {
    "state": {
        "reported": {
            "smokeAlarmStatus": 3,          # ALARM
            "coAlarmStatus": 0,             # IDLE
            "heatAlarmStatus": 0,           # IDLE
            "aqiAlarmStatus": 2,            # PRE_ALARM
            "vocAlarmStatus": 5,            # HUSHED
            "explosiveGasAlarmStatus": 4,   # CRITICAL_ALARM
        }
    }
}

# A real PL1AS "base" reported shadow (trimmed), captured live via shadow/get.
# The values are what the hardware actually emits — smoke+CO hazards, live CO
# ppm, temp/humidity, the raw dual-wavelength smoke optics, wifi, battery, motion
# sensitivity, nightlight. It carries no device/household identifiers.
REAL_PL1AS_SHADOW = {
    "state": {
        "reported": {
            "smokeAlarmStatus": 0,
            "coAlarmStatus": 0,
            "coPpm": 3,
            "coAccumulation": 0,
            "temperatureC": 23.9185791015625,
            "boardTempC": 27.541046142578125,
            "humidity": 53,
            "blueFrontScatter": 9,
            "blueBackScatter": 0,
            "irFrontScatter": 0,
            "irBackScatter": 0,
            "wifiSignalStrength": -60,
            "batteryStatus": 0,
            "motionSensitivity": 1,
            "nightLightSettings": {
                "on": True,
                "rgbaLed": {"red": 224, "green": 19, "blue": 4, "alpha": 255},
            },
        }
    }
}


# --- The three hazards the integration has always consumed -------------------

def test_from_shadow_full() -> None:
    """Test parsing a shadow payload."""
    shadow = PlaceDeviceShadow.from_shadow(FULL_SHADOW)

    # Alarm statuses
    assert shadow.co_alarm_status is AlarmStatus.IDLE
    assert shadow.heat_alarm_status is AlarmStatus.ALARM
    assert shadow.smoke_alarm_status is AlarmStatus.HUSHED


def test_from_shadow_without_state_wrapper() -> None:
    """Test parsing a flat reported dict (no state.reported wrapper)."""
    reported = {"coAlarmStatus": 3, "heatAlarmStatus": 0, "smokeAlarmStatus": 5}
    shadow = PlaceDeviceShadow.from_shadow(reported)

    assert shadow.co_alarm_status is AlarmStatus.ALARM
    assert shadow.heat_alarm_status is AlarmStatus.IDLE
    assert shadow.smoke_alarm_status is AlarmStatus.HUSHED


def test_from_shadow_empty() -> None:
    """Test parsing an empty shadow returns defaults."""
    shadow = PlaceDeviceShadow.from_shadow({})

    assert shadow.co_alarm_status is AlarmStatus.NOT_PRESENT
    assert shadow.heat_alarm_status is AlarmStatus.NOT_PRESENT
    assert shadow.smoke_alarm_status is AlarmStatus.NOT_PRESENT


def test_from_shadow_invalid_alarm_value() -> None:
    """Test that out-of-range alarm values default to NOT_PRESENT."""
    shadow = PlaceDeviceShadow.from_shadow({"coAlarmStatus": 99})

    assert shadow.co_alarm_status is AlarmStatus.NOT_PRESENT


def test_from_shadow_null_alarm_value() -> None:
    """Test that null alarm values default to NOT_PRESENT."""
    shadow = PlaceDeviceShadow.from_shadow({"coAlarmStatus": None})

    assert shadow.co_alarm_status is AlarmStatus.NOT_PRESENT


def test_merge_sparse_update() -> None:
    """Test that a sparse update only changes provided fields."""
    shadow = PlaceDeviceShadow.from_shadow(FULL_SHADOW)

    assert shadow.co_alarm_status is AlarmStatus.IDLE

    shadow.merge({"state": {"reported": {"coAlarmStatus": 3}}})

    # Updated fields
    assert shadow.co_alarm_status is AlarmStatus.ALARM

    # Unchanged fields
    assert shadow.heat_alarm_status is AlarmStatus.ALARM
    assert shadow.smoke_alarm_status is AlarmStatus.HUSHED


def test_alarm_status_enum_values() -> None:
    """Test AlarmStatus enum has correct integer mappings."""
    assert AlarmStatus.IDLE == 0
    assert AlarmStatus.TEST == 1
    assert AlarmStatus.PRE_ALARM == 2
    assert AlarmStatus.ALARM == 3
    assert AlarmStatus.CRITICAL_ALARM == 4
    assert AlarmStatus.HUSHED == 5
    assert AlarmStatus.NOT_PRESENT == 6


# --- The extra hazards larger models add -------------------------------------

def test_from_shadow_parses_all_six_hazards() -> None:
    """All six hazard fields parse off the same reported shadow."""
    shadow = PlaceDeviceShadow.from_shadow(SIX_HAZARD_SHADOW)

    # The three the integration already handled
    assert shadow.smoke_alarm_status is AlarmStatus.ALARM
    assert shadow.co_alarm_status is AlarmStatus.IDLE
    assert shadow.heat_alarm_status is AlarmStatus.IDLE

    # The three larger models add
    assert shadow.aqi_alarm_status is AlarmStatus.PRE_ALARM
    assert shadow.voc_alarm_status is AlarmStatus.HUSHED
    assert shadow.explosive_gas_alarm_status is AlarmStatus.CRITICAL_ALARM


def test_legacy_shadow_defaults_new_hazards_to_not_present() -> None:
    """A device that only reports smoke/CO/heat still parses cleanly."""
    legacy = {"state": {"reported": {"smokeAlarmStatus": 0, "coAlarmStatus": 3}}}

    shadow = PlaceDeviceShadow.from_shadow(legacy)

    assert shadow.smoke_alarm_status is AlarmStatus.IDLE
    assert shadow.co_alarm_status is AlarmStatus.ALARM
    assert shadow.aqi_alarm_status is AlarmStatus.NOT_PRESENT
    assert shadow.voc_alarm_status is AlarmStatus.NOT_PRESENT
    assert shadow.explosive_gas_alarm_status is AlarmStatus.NOT_PRESENT


def test_merge_updates_a_new_hazard_in_place() -> None:
    """A sparse update to a new hazard changes only that field."""
    shadow = PlaceDeviceShadow.from_shadow(SIX_HAZARD_SHADOW)

    shadow.merge({"state": {"reported": {"vocAlarmStatus": 3}}})

    assert shadow.voc_alarm_status is AlarmStatus.ALARM          # updated
    assert shadow.aqi_alarm_status is AlarmStatus.PRE_ALARM      # unchanged
    assert shadow.explosive_gas_alarm_status is AlarmStatus.CRITICAL_ALARM
    assert shadow.smoke_alarm_status is AlarmStatus.ALARM


# --- Live telemetry ----------------------------------------------------------

def test_from_shadow_parses_live_telemetry() -> None:
    """The model surfaces the live status a real PL1AS reports."""
    s = PlaceDeviceShadow.from_shadow(REAL_PL1AS_SHADOW)

    assert s.co_ppm == 3
    assert s.co_accumulation == 0
    assert s.temperature_c == 23.9185791015625
    assert s.board_temp_c == 27.541046142578125
    assert s.humidity == 53
    assert s.blue_front_scatter == 9
    assert s.blue_back_scatter == 0
    assert s.ir_front_scatter == 0
    assert s.ir_back_scatter == 0
    assert s.wifi_signal_strength == -60
    assert s.battery_status == 0
    assert s.motion_sensitivity == 1


def test_telemetry_absent_fields_are_none_not_zero() -> None:
    """A shadow that omits a live field leaves it None, distinct from a real 0."""
    s = PlaceDeviceShadow.from_shadow(
        {"state": {"reported": {"smokeAlarmStatus": 0}}}
    )

    assert s.co_ppm is None
    assert s.temperature_c is None
    assert s.humidity is None
    assert s.motion_sensitivity is None
    assert s.night_light is None


def test_from_shadow_parses_nightlight() -> None:
    """nightLightSettings becomes a typed NightLight (on + rgba brightness)."""
    s = PlaceDeviceShadow.from_shadow(REAL_PL1AS_SHADOW)

    assert s.night_light is not None
    assert s.night_light.on is True
    assert s.night_light.red == 224
    assert s.night_light.green == 19
    assert s.night_light.blue == 4
    assert s.night_light.alpha == 255


def test_merge_updates_telemetry_in_place() -> None:
    """A sparse update to one live value changes only that field."""
    s = PlaceDeviceShadow.from_shadow(REAL_PL1AS_SHADOW)

    s.merge({"state": {"reported": {"coPpm": 42}}})

    assert s.co_ppm == 42                       # updated
    assert s.humidity == 53                     # unchanged
    assert s.temperature_c == 23.9185791015625  # unchanged
