# ABOUTME: HouseholdEventListener — subscribes a household's MQTT tree and delivers
# ABOUTME: parsed DeviceEvents (chiefly live motion) to a callback. Read-only: subscribe only.
"""High-level consumer for live household device events.

A device's discrete events — most usefully live ``motionDetected`` — arrive on
its household topic tree, not in its shadow. This listener wraps that: give it a
:class:`~place.messages.PlaceMessages`, the household id(s) to watch, and a
callback, then hand its two methods to :meth:`~place.mqtt_client.MqttClient.connect`::

    listener = HouseholdEventListener(
        messages, household_ids, on_motion, motion_only=True
    )
    client.connect(on_message=listener.on_message, on_connect=listener.on_connect)
    client.loop_start()

``on_connect`` subscribes every household; ``on_message`` parses each message and
delivers a :class:`~place.models.device_event.DeviceEvent` to the callback
(optionally motion only). It is READ-ONLY — it only subscribes, never publishes.
Because MqttClient binds its handlers once at connect time, construct the
listener before calling ``connect``.
"""

from __future__ import annotations

from typing import Callable, Iterable

from .messages import PlaceMessages, parse_payload
from .models.device_event import DeviceEvent


class HouseholdEventListener:
    """Turns a household's raw MQTT firehose into typed DeviceEvent callbacks."""

    def __init__(
        self,
        messages: PlaceMessages,
        household_ids: Iterable[str],
        on_event: Callable[[DeviceEvent], None],
        *,
        motion_only: bool = False,
    ) -> None:
        self._messages = messages
        self._household_ids = list(household_ids)
        self._on_event = on_event
        self._motion_only = motion_only

    def on_connect(self) -> None:
        """Subscribe every household tree. Pass to ``MqttClient.connect(on_connect=)``."""
        for household_id in self._household_ids:
            self._messages.subscribe_household(household_id)

    def on_message(self, topic: str, payload: bytes) -> None:
        """Deliver a DeviceEvent to the callback. Pass to ``connect(on_message=)``."""
        event = DeviceEvent.from_message(topic, parse_payload(payload))
        if event is None:
            return
        if self._motion_only and not event.is_motion:
            return
        self._on_event(event)
