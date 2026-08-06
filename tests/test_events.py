"""Tests for HouseholdEventListener — the high-level live-event consumer.

The listener subscribes a household's MQTT tree and delivers parsed DeviceEvents
(chiefly live motion) to a callback. These tests use a fake PlaceMessages (no
network, no broker) and event payloads shaped like a live capture but carrying
synthetic identifiers.
"""

import json

from place.events import HouseholdEventListener
from place.messages import PlaceMessages

HOUSEHOLD_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOUSEHOLD_B = "11111111-2222-4333-8444-555555555555"
DEVICE_ID = "Place_PL1AS_EXAMPLE"


class _FakeMessages(PlaceMessages):
    """Records subscribe_household calls — the only surface the listener touches.

    Subclasses PlaceMessages (so it satisfies the type) but overrides __init__ to
    skip the MqttClient, mirroring the DummyAuth pattern in test_provider.py.
    """

    def __init__(self) -> None:
        self.subscribed: list[str] = []

    def subscribe_household(self, household_id: str, qos: int = 1) -> str:
        self.subscribed.append(household_id)
        return household_id


def _motion_topic(household_id: str, device_id: str) -> str:
    return (
        f"connectedsmoke/household/{household_id}"
        f"/device/{device_id}/events/motionDetected"
    )


def _payload(**fields: object) -> bytes:
    return json.dumps(fields).encode("utf-8")


def test_on_connect_subscribes_every_household() -> None:
    """on_connect subscribes each household tree, in order."""
    messages = _FakeMessages()
    listener = HouseholdEventListener(
        messages, [HOUSEHOLD_A, HOUSEHOLD_B], lambda event: None
    )

    listener.on_connect()

    assert messages.subscribed == [HOUSEHOLD_A, HOUSEHOLD_B]


def test_on_message_delivers_motion_event() -> None:
    """A motion message is parsed and handed to the callback as a DeviceEvent."""
    seen = []
    listener = HouseholdEventListener(_FakeMessages(), [HOUSEHOLD_A], seen.append)

    listener.on_message(
        _motion_topic(HOUSEHOLD_A, DEVICE_ID),
        _payload(deviceId=DEVICE_ID, seq=87, timestamp="2026-08-06T13:41:54Z"),
    )

    assert len(seen) == 1
    assert seen[0].is_motion is True
    assert seen[0].device_id == DEVICE_ID
    assert seen[0].seq == 87


def test_non_event_messages_are_ignored() -> None:
    """Shadow traffic on the household tree never reaches the callback."""
    seen = []
    listener = HouseholdEventListener(_FakeMessages(), [HOUSEHOLD_A], seen.append)

    listener.on_message(
        f"$aws/things/{DEVICE_ID}/shadow/update/documents",
        _payload(state={"reported": {"coPpm": 3}}),
    )

    assert seen == []


def test_motion_only_filters_other_event_types() -> None:
    """With motion_only, non-motion events are dropped but motion still passes."""
    seen = []
    listener = HouseholdEventListener(
        _FakeMessages(), [HOUSEHOLD_A], seen.append, motion_only=True
    )

    other = (
        f"connectedsmoke/household/{HOUSEHOLD_A}"
        f"/device/{DEVICE_ID}/events/testEvent"
    )
    listener.on_message(other, _payload(deviceId=DEVICE_ID))
    assert seen == []

    listener.on_message(
        _motion_topic(HOUSEHOLD_A, DEVICE_ID), _payload(deviceId=DEVICE_ID)
    )
    assert len(seen) == 1
    assert seen[0].is_motion is True


def test_all_events_delivered_when_not_motion_only() -> None:
    """The default delivers every event family, not just motion."""
    seen = []
    listener = HouseholdEventListener(_FakeMessages(), [HOUSEHOLD_A], seen.append)

    other = (
        f"connectedsmoke/household/{HOUSEHOLD_A}"
        f"/device/{DEVICE_ID}/events/testEvent"
    )
    listener.on_message(other, _payload(deviceId=DEVICE_ID))

    assert len(seen) == 1
    assert seen[0].event_type == "testEvent"
    assert seen[0].is_motion is False
