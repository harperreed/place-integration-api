"""Tests for the device-event model (the household 'events' topic).

Grounded in a live capture: walking under a real PL1AS published a discrete
``motionDetected`` event. Motion is NOT in the device shadow — it is an event on
``connectedsmoke/household/{householdId}/device/{deviceId}/events/{type}``.

The fixture below reproduces that event's real *schema* (every field the device
sent, and their types) but uses synthetic household/device identifiers, so the
test carries no real home's IDs while still pinning the parse to the wire format.
"""

from place.models.device_event import DeviceEvent

# Real motion-event schema with SYNTHETIC identifiers (household UUID, device
# UUID, and device id are fabricated; the field set/types match a live capture).
SYNTHETIC_HOUSEHOLD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SYNTHETIC_DEVICE_UUID = "11111111-2222-4333-8444-555555555555"
SYNTHETIC_DEVICE_ID = "Place_PL1AS_EXAMPLE"

MOTION_TOPIC = (
    f"connectedsmoke/household/{SYNTHETIC_HOUSEHOLD}"
    f"/device/{SYNTHETIC_DEVICE_ID}/events/motionDetected"
)
MOTION_PAYLOAD = {
    "timestamp": "2026-08-06T13:41:54Z",
    "deviceId": SYNTHETIC_DEVICE_ID,
    "model": "base",
    "fwPackageId": "40510",
    "seq": 87,
    "thingName": f"{SYNTHETIC_HOUSEHOLD}_{SYNTHETIC_DEVICE_UUID}_{SYNTHETIC_DEVICE_ID}",
}


def test_parses_real_motion_event() -> None:
    """A motion message parses into every field it carried."""
    event = DeviceEvent.from_message(MOTION_TOPIC, MOTION_PAYLOAD)

    assert event is not None
    assert event.event_type == "motionDetected"
    assert event.is_motion is True
    assert event.device_id == SYNTHETIC_DEVICE_ID
    assert event.seq == 87
    assert event.timestamp == "2026-08-06T13:41:54Z"
    assert event.model == "base"
    assert event.fw_package_id == "40510"
    assert event.thing_name == (
        f"{SYNTHETIC_HOUSEHOLD}_{SYNTHETIC_DEVICE_UUID}_{SYNTHETIC_DEVICE_ID}"
    )


def test_event_type_comes_from_topic_not_payload() -> None:
    """event_type is read off the topic, so any event family parses.

    Only 'motionDetected' has been observed live; this uses a synthetic type to
    prove the parser is generic and that is_motion is scoped to motion alone.
    """
    topic = "connectedsmoke/household/h/device/Place_PL1AS_x/events/testEvent"
    event = DeviceEvent.from_message(topic, {"deviceId": "Place_PL1AS_x", "seq": 1})

    assert event is not None
    assert event.event_type == "testEvent"
    assert event.is_motion is False
    assert event.device_id == "Place_PL1AS_x"


def test_non_event_topics_return_none() -> None:
    """Shadow topics are not device events."""
    assert DeviceEvent.from_message("$aws/things/x/shadow/update/documents", {}) is None
    assert (
        DeviceEvent.from_message(
            "connectedsmoke/household/h/device/d/shadow/update/accepted", {}
        )
        is None
    )


def test_missing_optional_fields_stay_none() -> None:
    """A sparse event still parses; absent fields stay None, not invented."""
    topic = "connectedsmoke/household/h/device/d/events/motionDetected"
    event = DeviceEvent.from_message(topic, {"deviceId": "d"})

    assert event is not None
    assert event.is_motion is True
    assert event.device_id == "d"
    assert event.seq is None
    assert event.timestamp is None
    assert event.model is None
    assert event.fw_package_id is None
    assert event.thing_name is None
