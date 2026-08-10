# ABOUTME: Tests for PlaceDevice, the stateful one-source-of-truth per device —
# ABOUTME: built from discovery, mutated by shadow/event dispatch, notifying listeners.
from __future__ import annotations

import pytest

from place.device import PlaceDevice
from place.models import AlarmStatus, DeviceEvent, DiscoverDevice


def _discover() -> DiscoverDevice:
    return DiscoverDevice.from_dict(
        {
            "thingName": "Place_PL1AS_EXAMPLE",
            "deviceId": "dev-1",
            "deviceName": "Hallway",
            "modelNumber": "PL1AS",
            "firmwareVersion": "1.2.3",
            "location": "Upstairs",
            "online": True,
            "shadow": {"state": {"reported": {"coPpm": 3, "smokeAlarmStatus": 0}}},
        }
    )


def test_from_discovery_maps_identity_and_shadow() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    assert dev.thing_name == "Place_PL1AS_EXAMPLE"
    assert dev.device_id == "dev-1"
    assert dev.name == "Hallway"
    assert dev.model == "PL1AS"
    assert dev.firmware_version == "1.2.3"  # HA device registry sw_version
    assert dev.location == "Upstairs"  # HA suggested_area
    assert dev.online is True
    assert dev.shadow.co_ppm == 3
    assert dev.shadow.smoke_alarm_status is AlarmStatus.IDLE


def test_apply_shadow_merges_and_notifies() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    _ = dev.add_listener(seen.append)

    _ = dev.apply_shadow({"state": {"reported": {"coPpm": 9}}})

    assert dev.shadow.co_ppm == 9
    assert dev.shadow.smoke_alarm_status is AlarmStatus.IDLE  # untouched key persists
    assert seen == [dev]


def test_apply_event_records_motion_and_notifies() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    _ = dev.add_listener(seen.append)

    event = DeviceEvent(event_type="motionDetected", device_id="dev-1")
    dev.apply_event(event)

    ev = dev.last_event
    assert ev is not None
    assert ev is event
    assert ev.is_motion is True
    assert seen == [dev]


def test_motion_state_is_false_before_any_event() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    assert dev.last_motion_at is None
    assert dev.motion() is False


def test_apply_motion_event_stamps_last_motion_at() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    dev.apply_event(DeviceEvent(event_type="motionDetected"), now=100.0)
    assert dev.last_motion_at == 100.0


def test_motion_is_true_within_the_window_and_false_after() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    dev.apply_event(DeviceEvent(event_type="motionDetected"), now=100.0)
    assert dev.motion(within_seconds=30, now=120.0) is True  # 20s since motion
    assert dev.motion(within_seconds=30, now=140.0) is False  # 40s since motion


def test_non_motion_event_does_not_stamp_motion() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    dev.apply_event(DeviceEvent(event_type="somethingElse"), now=100.0)
    assert dev.last_motion_at is None
    assert dev.motion(now=100.0) is False
    assert dev.last_event is not None and dev.last_event.event_type == "somethingElse"


def test_unsubscribe_stops_notifications() -> None:
    dev = PlaceDevice.from_discovery(_discover())
    seen: list[PlaceDevice] = []
    unsubscribe = dev.add_listener(seen.append)

    unsubscribe()
    _ = dev.apply_shadow({"state": {"reported": {"coPpm": 1}}})

    assert seen == []


def test_from_discovery_without_thing_name_is_rejected() -> None:
    bad = DiscoverDevice.from_dict({"deviceId": "dev-1"})
    with pytest.raises(ValueError):
        _ = PlaceDevice.from_discovery(bad)


def test_apply_shadow_notifies_only_on_change() -> None:
    """A no-op merge (empty or value-identical shadow message) must not notify.

    Mirrors set_online: notify on real change only. Prevents the spurious update
    an empty-payload shadow message — our own shadow/get echoed back on the
    shadow/# wildcard — would otherwise fire on every connect.
    """
    dev = PlaceDevice.from_discovery(_discover())  # _discover() → co_ppm=3
    seen: list[PlaceDevice] = []
    _ = dev.add_listener(seen.append)

    assert dev.apply_shadow({}) is False  # empty payload → no change → no notify
    assert seen == []

    assert dev.apply_shadow({"state": {"reported": {"coPpm": 9}}}) is True  # changed
    assert seen == [dev]

    assert dev.apply_shadow({"state": {"reported": {"coPpm": 9}}}) is False  # same value
    assert seen == [dev]  # still only the one notification


def test_set_online_notifies_only_on_change() -> None:
    dev = PlaceDevice.from_discovery(_discover())  # _discover() → online=True
    seen: list[PlaceDevice] = []
    _ = dev.add_listener(seen.append)

    dev.set_online(True)  # no change (already True) → must NOT notify
    assert dev.online is True
    assert seen == []

    dev.set_online(False)  # changed → notifies once
    assert dev.online is False
    assert seen == [dev]

    dev.set_online(False)  # no change → must NOT notify again
    assert dev.online is False
    assert seen == [dev]
