# ABOUTME: Device events on the household 'events' topic — the discrete signals a
# ABOUTME: PLACE device emits that never touch the shadow, chiefly live motionDetected.
"""Device events published on the household topic tree.

Some things a PLACE device reports are not state in its shadow but discrete
*events*. A live capture confirmed motion is one: walking under a PL1AS
published

    connectedsmoke/household/{householdId}/device/{deviceId}/events/motionDetected
    {"timestamp": ..., "deviceId": ..., "model": ..., "fwPackageId": ...,
     "seq": <monotonic>, "thingName": ...}

one second before its shadow's optics even twitched. This models that event
envelope. The event *type* is taken from the topic, so new event families parse
without guessing their payloads; ``motionDetected`` is the one observed so far.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# A device event lives at .../device/{deviceId}/events/{eventType}.
EVENTS_SEGMENT = "/events/"

# The one event type observed live. Others may exist; the parser stays generic.
MOTION_EVENT = "motionDetected"


def _event_type_from_topic(topic: str) -> str | None:
    """The trailing event type of a ``.../events/{eventType}`` topic, else None."""
    index = topic.find(EVENTS_SEGMENT)
    if index == -1:
        return None
    return topic[index + len(EVENTS_SEGMENT) :].split("/")[0] or None


@dataclass
class DeviceEvent:
    """A discrete event a device published on its household events topic.

    ``event_type`` comes from the topic; the rest from the payload. Motion is the
    observed case (``event_type == "motionDetected"``) — a fire-and-forget pulse,
    a timestamped "it happened" with no on/off state, so presence is inferred
    from the arrival of the event, not from a field value.
    """

    event_type: str
    device_id: str | None = None
    thing_name: str | None = None
    seq: int | None = None
    timestamp: str | None = None
    model: str | None = None
    fw_package_id: str | None = None

    @property
    def is_motion(self) -> bool:
        return self.event_type == MOTION_EVENT

    @staticmethod
    def from_message(topic: str, payload: Mapping[str, Any]) -> DeviceEvent | None:
        """Parse an events-topic message into a DeviceEvent, or None if not one."""
        event_type = _event_type_from_topic(topic)
        if event_type is None:
            return None
        data = payload or {}
        return DeviceEvent(
            event_type=event_type,
            device_id=data.get("deviceId"),
            thing_name=data.get("thingName"),
            seq=data.get("seq"),
            timestamp=data.get("timestamp"),
            model=data.get("model"),
            fw_package_id=data.get("fwPackageId"),
        )
