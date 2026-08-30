"""The last picture the camera sent with an event.

EZVIZ encrypts these, which is why the built-in integration so often shows a
broken image: it can only decrypt them when a camera config entry exists to
hold the verification code, and a doorbell with no RTSP server never gets one.
The coordinator decrypts them with the key the cloud hands its own account, so
image encryption can stay switched on in the EZVIZ app.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity

LAST_SNAPSHOT = ImageEntityDescription(
    key="last_snapshot", translation_key="last_snapshot"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the snapshot images."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizSnapshotImage(coordinator, serial) for serial in coordinator.data
    )


class EzvizSnapshotImage(EzvizDoorbellEntity, ImageEntity):
    """The decrypted picture from the most recent event."""

    entity_description = LAST_SNAPSHOT
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: EzvizDoorbellCoordinator, serial: str) -> None:
        """Initialize the image."""
        super().__init__(coordinator, serial, LAST_SNAPSHOT.key)
        ImageEntity.__init__(self, coordinator.hass)
        self.entity_description = LAST_SNAPSHOT
        self._attr_image_last_updated = self.device.snapshot_time

    @callback
    def _handle_coordinator_update(self) -> None:
        """Tell Home Assistant when a newer picture has arrived."""
        updated: datetime | None = self.device.snapshot_time
        if updated != self._attr_image_last_updated:
            self._attr_image_last_updated = updated
            self._cached_image = None
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Return the picture itself."""
        return self.device.snapshot
