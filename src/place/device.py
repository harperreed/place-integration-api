# ABOUTME: PlaceDevice — the stateful, one-source-of-truth model for a single PLACE
# ABOUTME: device: identity + live shadow + last event, with change listeners.
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .models import DeviceEvent, DiscoverDevice, PlaceDeviceShadow

# Default clear-after window for motion(): a motionDetected pulse counts as
# "motion" for this many seconds after it arrives. Consumers (e.g. a Home
# Assistant binary_sensor) typically override it to match their own auto-off.
DEFAULT_MOTION_WINDOW_SEC = 30.0

Listener = Callable[["PlaceDevice"], None]


@dataclass
class PlaceDevice:
    """A device's live state. Mutated in place by shadow/event dispatch."""

    thing_name: str
    shadow: PlaceDeviceShadow
    device_id: str | None = None
    name: str | None = None
    model: str | None = None
    # Discovery-time metadata: firmware for a HA device registry sw_version,
    # location for its suggested_area. Static — set at discovery, not live.
    firmware_version: str | None = None
    location: str | None = None
    online: bool | None = None
    last_event: DeviceEvent | None = None
    # Monotonic timestamp of the last motionDetected pulse (see apply_event/motion).
    # Monotonic, not wall-clock: it exists for elapsed-time math, not display —
    # the event's own .timestamp carries the device-reported wall-clock instead.
    last_motion_at: float | None = None
    # Monotonic timestamp of the last shadow answer that carried reported state (a
    # shadow/get/accepted reply or a spontaneous update). This is the liveness
    # anchor a HA coordinator reads for per-device availability: a live device
    # answers shadow/get, a dead one does not. Monotonic for elapsed-time math, not
    # display. Starts None — discovery arrives over HTTPS, not as an MQTT answer.
    last_shadow_at: float | None = None
    _listeners: list[Listener] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_discovery(cls, discovered: DiscoverDevice) -> "PlaceDevice":
        if not discovered.thing_name:
            raise ValueError("cannot build a PlaceDevice without a thing_name")
        return cls(
            thing_name=discovered.thing_name,
            shadow=PlaceDeviceShadow.from_shadow(discovered.shadow),
            device_id=discovered.device_id,
            name=discovered.device_name,
            model=discovered.model_number,
            firmware_version=discovered.firmware_version,
            location=discovered.location,
            online=discovered.online,
        )

    def add_listener(self, callback: Listener) -> Callable[[], None]:
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsubscribe

    def apply_shadow(
        self, message: dict[str, object], *, now: float | None = None
    ) -> bool:
        """Merge a shadow message; notify (and report True) only if state changed.

        Like set_online, a no-op update is silent: an empty-payload shadow message
        echoed back on the shadow/# wildcard must not fire a listener.

        Independently, a message that carries reported state stamps last_shadow_at
        — even when it changes nothing — because a device answering at all proves
        it is alive. That liveness stamp is silent: availability is read by polling
        the timestamp, not pushed. ``now`` is injectable for tests.
        """
        if PlaceDeviceShadow.carries_reported_state(message):
            self.last_shadow_at = time.monotonic() if now is None else now
        changed = self.shadow.merge(message)
        if changed:
            self._notify()
        return changed

    def apply_event(self, event: DeviceEvent, *, now: float | None = None) -> None:
        self.last_event = event
        if event.is_motion:
            self.last_motion_at = time.monotonic() if now is None else now
        self._notify()

    def motion(
        self,
        within_seconds: float = DEFAULT_MOTION_WINDOW_SEC,
        *,
        now: float | None = None,
    ) -> bool:
        """Whether a motionDetected pulse arrived within the last ``within_seconds``.

        Motion is a fire-and-forget event, not shadow state, so "is there motion
        now?" is a freshness question: True while the last pulse is still within
        the window, False once it ages out. ``now`` is injectable for tests.
        """
        if self.last_motion_at is None:
            return False
        ref = time.monotonic() if now is None else now
        return (ref - self.last_motion_at) <= within_seconds

    def set_online(self, online: bool | None) -> None:
        if self.online != online:
            self.online = online
            self._notify()

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback(self)
