# ABOUTME: PlaceDevice — the stateful, one-source-of-truth model for a single PLACE
# ABOUTME: device: identity + live shadow + last event, with change listeners.
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import DeviceEvent, DiscoverDevice, PlaceDeviceShadow

Listener = Callable[["PlaceDevice"], None]


@dataclass
class PlaceDevice:
    """A device's live state. Mutated in place by shadow/event dispatch."""

    thing_name: str
    shadow: PlaceDeviceShadow
    device_id: str | None = None
    name: str | None = None
    model: str | None = None
    online: bool | None = None
    last_event: DeviceEvent | None = None
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
            online=discovered.online,
        )

    def add_listener(self, callback: Listener) -> Callable[[], None]:
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsubscribe

    def apply_shadow(self, message: dict[str, object]) -> None:
        self.shadow.merge(message)
        self._notify()

    def apply_event(self, event: DeviceEvent) -> None:
        self.last_event = event
        self._notify()

    def set_online(self, online: bool | None) -> None:
        if self.online != online:
            self.online = online
            self._notify()

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback(self)
