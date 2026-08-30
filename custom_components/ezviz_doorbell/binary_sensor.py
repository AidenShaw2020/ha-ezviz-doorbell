"""Binary sensors for an EZVIZ camera.

Two kinds live here. The momentary ones pulse when an event arrives and clear
themselves afterwards - the doorbell press and motion, for dashboards and
blueprints that expect a binary sensor rather than an event entity - and the
rest simply report the device's own state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import EzvizDoorbellConfigEntry
from .const import EVENT_MOTION, EVENT_RING, signal_event
from .coordinator import DeviceData, EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity

# How long a press or a detection stays showing after it arrives.
RING_SECONDS = 5
MOTION_SECONDS = 30


@dataclass(frozen=True, kw_only=True)
class EzvizBinarySensorDescription(BinarySensorEntityDescription):
    """Describes one EZVIZ binary sensor."""

    value_fn: Callable[[DeviceData], bool]


@dataclass(frozen=True, kw_only=True)
class EzvizTriggerDescription(BinarySensorEntityDescription):
    """Describes a binary sensor that pulses when an event arrives."""

    event_type: str
    off_delay: int


BINARY_SENSORS: tuple[EzvizBinarySensorDescription, ...] = (
    EzvizBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.online,
    ),
    EzvizBinarySensorDescription(
        key="encrypted",
        translation_key="encrypted",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: bool(device.raw.get("encrypted")),
    ),
    EzvizBinarySensorDescription(
        key="alarm_schedule",
        translation_key="alarm_schedule",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: bool(device.raw.get("alarm_schedules_enabled")),
    ),
)

TRIGGERS: tuple[EzvizTriggerDescription, ...] = (
    EzvizTriggerDescription(
        key="ring",
        translation_key="ring",
        event_type=EVENT_RING,
        off_delay=RING_SECONDS,
    ),
    EzvizTriggerDescription(
        key="motion_detected",
        translation_key="motion_detected",
        device_class=BinarySensorDeviceClass.MOTION,
        event_type=EVENT_MOTION,
        off_delay=MOTION_SECONDS,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for serial in coordinator.data:
        entities.extend(
            EzvizBinarySensor(coordinator, serial, description)
            for description in BINARY_SENSORS
        )
        entities.extend(
            EzvizTriggerSensor(coordinator, serial, description)
            for description in TRIGGERS
        )
    async_add_entities(entities)


class EzvizBinarySensor(EzvizDoorbellEntity, BinarySensorEntity):
    """A yes or no the camera reports about itself."""

    entity_description: EzvizBinarySensorDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return the state this sensor reads."""
        return self.entity_description.value_fn(self.device)


class EzvizTriggerSensor(EzvizDoorbellEntity, BinarySensorEntity):
    """A binary sensor that goes on for a moment when an event arrives."""

    entity_description: EzvizTriggerDescription
    _attr_is_on = False

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizTriggerDescription,
    ) -> None:
        """Initialize the trigger sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description
        self._cancel_off: Callable[[], None] | None = None

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
        self.async_on_remove(self._cancel_pending)

    @callback
    def _handle_event(self, serial: str, event_type: str, attributes: dict) -> None:
        """Switch on for this camera's own kind of event."""
        if serial != self._serial or event_type != self.entity_description.event_type:
            return

        self._cancel_pending()
        self._attr_is_on = True
        self.async_write_ha_state()
        self._cancel_off = async_call_later(
            self.hass, self.entity_description.off_delay, self._switch_off
        )

    @callback
    def _switch_off(self, _now: datetime) -> None:
        """Clear the sensor once its moment has passed."""
        self._cancel_off = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _cancel_pending(self) -> None:
        """Drop a scheduled switch off, if there is one."""
        if self._cancel_off is not None:
            self._cancel_off()
            self._cancel_off = None
