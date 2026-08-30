"""Firmware updates offered by the EZVIZ cloud."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity
from .helpers import as_int

FIRMWARE = UpdateEntityDescription(
    key="firmware",
    device_class=UpdateDeviceClass.FIRMWARE,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the firmware update entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizFirmwareUpdate(coordinator, serial) for serial in coordinator.data
    )


class EzvizFirmwareUpdate(EzvizDoorbellEntity, UpdateEntity):
    """Whether the camera has a firmware update waiting."""

    entity_description = FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, coordinator: EzvizDoorbellCoordinator, serial: str) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, serial, FIRMWARE.key)
        self.entity_description = FIRMWARE

    @property
    def installed_version(self) -> str | None:
        """Return the firmware the camera is running."""
        return self.device.version

    @property
    def latest_version(self) -> str | None:
        """Return the firmware EZVIZ is offering, if any."""
        info = self.status.get("latest_firmware_info")
        if isinstance(info, dict):
            version = info.get("version") or info.get("fullVersion")
            if version:
                return str(version)
        # Nothing on offer means the installed version is the latest one; a
        # blank here would only make the entity look broken.
        if not self.status.get("upgrade_available"):
            return self.installed_version
        return None

    @property
    def in_progress(self) -> bool:
        """Return whether an upgrade is running."""
        return bool(self.status.get("upgrade_in_progress"))

    @property
    def update_percentage(self) -> int | None:
        """Return how far an upgrade has got."""
        if not self.in_progress:
            return None
        return as_int(self.status.get("upgrade_percent"))

    @property
    def release_summary(self) -> str | None:
        """Return what EZVIZ says the update changes."""
        info = self.status.get("latest_firmware_info")
        if isinstance(info, dict) and info.get("desc"):
            return str(info["desc"])[:255]
        return None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Start the upgrade."""
        await self.coordinator.async_execute(
            lambda client: client.upgrade_device(self._serial)
        )
