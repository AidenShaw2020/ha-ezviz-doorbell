"""The camera's own alarm sound."""

from __future__ import annotations

from typing import Any

from homeassistant.components.siren import (
    SirenEntity,
    SirenEntityDescription,
    SirenEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity

SIREN = SirenEntityDescription(key="siren", translation_key="siren")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the siren."""
    coordinator = entry.runtime_data
    async_add_entities(EzvizSiren(coordinator, serial) for serial in coordinator.data)


class EzvizSiren(EzvizDoorbellEntity, SirenEntity):
    """Sound the camera's alarm.

    EZVIZ does not report whether the alarm is currently sounding, so the state
    here is only what was last asked for.
    """

    entity_description = SIREN
    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    _attr_is_on = False

    def __init__(self, coordinator: EzvizDoorbellCoordinator, serial: str) -> None:
        """Initialize the siren."""
        super().__init__(coordinator, serial, SIREN.key)
        self.entity_description = SIREN

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound the alarm."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Silence the alarm."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        """Tell the camera to start or stop."""
        await self.coordinator.async_execute(
            lambda client: client.sound_alarm(self._serial, 1 if on else 0),
            refresh=False,
        )
        self._attr_is_on = on
        self.async_write_ha_state()
