"""Sensors describing an EZVIZ camera."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import EzvizDoorbellConfigEntry
from .const import EVENT_TYPES
from .coordinator import DeviceData, EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity
from .helpers import disk_capacity, wifi


@dataclass(frozen=True, kw_only=True)
class EzvizSensorDescription(SensorEntityDescription):
    """Describes one EZVIZ sensor."""

    value_fn: Callable[[DeviceData], StateType | datetime]


SENSORS: tuple[EzvizSensorDescription, ...] = (
    EzvizSensorDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.raw.get("battery_level"),
    ),
    EzvizSensorDescription(
        key="last_event",
        translation_key="last_event",
        device_class=SensorDeviceClass.ENUM,
        options=EVENT_TYPES,
        value_fn=lambda device: device.last_event,
    ),
    EzvizSensorDescription(
        key="last_ring",
        translation_key="last_ring",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda device: device.last_ring,
    ),
    EzvizSensorDescription(
        key="last_motion",
        translation_key="last_motion",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda device: device.last_motion,
    ),
    EzvizSensorDescription(
        key="last_alarm_type",
        translation_key="last_alarm_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("last_alarm_type_name"),
    ),
    EzvizSensorDescription(
        key="last_alarm_time",
        translation_key="last_alarm_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("last_alarm_time"),
    ),
    EzvizSensorDescription(
        key="seconds_last_trigger",
        translation_key="seconds_last_trigger",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("Seconds_Last_Trigger"),
    ),
    EzvizSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: wifi(device.raw).get("signal"),
    ),
    EzvizSensorDescription(
        key="wifi_ssid",
        translation_key="wifi_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: wifi(device.raw).get("ssid")
        or wifi(device.raw).get("netName"),
    ),
    EzvizSensorDescription(
        key="local_ip",
        translation_key="local_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("local_ip"),
    ),
    EzvizSensorDescription(
        key="wan_ip",
        translation_key="wan_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("wan_ip"),
    ),
    EzvizSensorDescription(
        key="pir_status",
        translation_key="pir_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.raw.get("PIR_Status"),
    ),
    EzvizSensorDescription(
        key="storage_capacity",
        translation_key="storage_capacity",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: disk_capacity(device.raw.get("diskCapacity")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizSensor(coordinator, serial, description)
        for serial in coordinator.data
        for description in SENSORS
    )


class EzvizSensor(EzvizDoorbellEntity, SensorEntity):
    """A single value read from the camera's cloud status."""

    entity_description: EzvizSensorDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Return the value this sensor reads."""
        return self.entity_description.value_fn(self.device)
