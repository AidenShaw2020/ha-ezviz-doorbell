"""Event entities: the doorbell press, motion, and everything else.

The press and motion get an entity each so an automation can trigger on the one
it means without filtering on an attribute - which is the whole point, because
EZVIZ delivers both down the same pipe and calls them both an alarm. The third
entity carries every event, unrecognised ones included, with the raw code in
its attributes for anyone cataloguing a new model.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .const import EVENT_MOTION, EVENT_RING, EVENT_TYPES, signal_event
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity

MAX_SEEN_EVENTS = 500


@dataclass(frozen=True, kw_only=True)
class EzvizEventDescription(EventEntityDescription):
    """Describes one EZVIZ event entity."""

    # None means every event type reaches this entity.
    only: str | None = None


EVENTS: tuple[EzvizEventDescription, ...] = (
    EzvizEventDescription(
        key="doorbell",
        translation_key="doorbell",
        device_class=EventDeviceClass.DOORBELL,
        event_types=[EVENT_RING],
        only=EVENT_RING,
    ),
    EzvizEventDescription(
        key="motion",
        translation_key="motion",
        device_class=EventDeviceClass.MOTION,
        event_types=[EVENT_MOTION],
        only=EVENT_MOTION,
    ),
    EzvizEventDescription(
        key="alerts",
        translation_key="alerts",
        event_types=EVENT_TYPES,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the event entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizEvent(coordinator, serial, description)
        for serial in coordinator.data
        for description in EVENTS
    )


class EzvizEvent(EzvizDoorbellEntity, EventEntity):
    """An event entity fed by the push and polling paths."""

    entity_description: EzvizEventDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizEventDescription,
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description
        # EZVIZ can replay the last MQTT message after the connection comes
        # back. Keep a bounded history here as a second line of defence beyond
        # the coordinator's short push-vs-poll dedupe window.
        self._seen_events: dict[str, None] = {}

    @property
    def available(self) -> bool:
        """Keep event entities independent from status-poll availability.

        Motion and rings arrive through push/message polling, not through the
        status request managed by DataUpdateCoordinator. A temporary failure of
        that unrelated request must therefore not turn the event entity into
        ``unavailable`` and create a false state transition for automations.
        """
        return self._serial in self.coordinator.data

    async def async_added_to_hass(self) -> None:
        """Start listening for events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_event(self.coordinator.config_entry.entry_id),
                self._handle_event,
            )
        )

    @callback
    def _handle_event(self, serial: str, event_type: str, attributes: dict) -> None:
        """Fire, if this event belongs to this camera and this entity."""
        if serial != self._serial:
            return
        only = self.entity_description.only
        if only is not None and event_type != only:
            return
        if self._is_replay(event_type, attributes):
            return

        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    def _is_replay(self, event_type: str, attributes: dict) -> bool:
        """Return True when EZVIZ re-sends an event already delivered here."""
        msg_id = attributes.get("msg_id")
        if msg_id:
            key = f"msg:{msg_id}"
        elif event_time := attributes.get("time"):
            # A few models omit msgId. Their cloud timestamp together with the
            # event path and code is the next most stable replay fingerprint.
            key = "|".join(
                (
                    "time",
                    event_type,
                    str(attributes.get("source") or ""),
                    str(attributes.get("alert_type_code") or ""),
                    str(event_time),
                )
            )
        else:
            # Without any stable identity, suppressing the event would risk
            # merging two genuine consecutive presses or motion detections.
            return False

        if key in self._seen_events:
            return True

        self._seen_events[key] = None
        if len(self._seen_events) > MAX_SEEN_EVENTS:
            self._seen_events.pop(next(iter(self._seen_events)))
        return False
