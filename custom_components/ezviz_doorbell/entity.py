"""The base every EZVIZ Doorbell entity is built on."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import DeviceData, EzvizDoorbellCoordinator


class EzvizDoorbellEntity(CoordinatorEntity[EzvizDoorbellCoordinator]):
    """One entity belonging to one camera."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EzvizDoorbellCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the entity and the device it belongs to."""
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"

        device = coordinator.data[serial]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.model,
            sw_version=device.version,
            serial_number=serial,
        )

    @property
    def device(self) -> DeviceData:
        """Return the camera this entity belongs to."""
        return self.coordinator.data[self._serial]

    @property
    def status(self) -> dict[str, Any]:
        """Return the camera's raw cloud status."""
        return self.device.raw

    @property
    def available(self) -> bool:
        """Return whether the camera is still on the account."""
        return super().available and self._serial in self.coordinator.data
