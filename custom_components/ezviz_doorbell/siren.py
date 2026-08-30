"""The camera's own alarm sound."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.siren import (
    DOMAIN as SIREN_DOMAIN,
    SirenEntity,
    SirenEntityDescription,
    SirenEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .const import DOMAIN
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity
from .helpers import supports

_LOGGER = logging.getLogger(__name__)

SIREN = SirenEntityDescription(key="siren", translation_key="siren")

# Sounding the alarm is not something every device does, and one that does not
# answers with 设备异常 - "device error" - however the request is phrased. These
# are the capabilities that mean it can.
ALARM_CAPABILITIES = (
    7,  # SupportAlarmVoice
    96,  # SupportActiveDefense
    214,  # SupportSoundLightAlarm
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the siren, for the devices that have one."""
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    sirens: list[EzvizSiren] = []

    for serial, device in coordinator.data.items():
        if supports(device.raw, *ALARM_CAPABILITIES):
            sirens.append(EzvizSiren(coordinator, serial))
            continue

        # This device cannot sound an alarm. An earlier version gave every
        # device a siren regardless, and an entity that was registered once
        # stays registered - sitting there unavailable, with nothing to remove
        # it but this.
        entity_id = registry.async_get_entity_id(
            SIREN_DOMAIN, DOMAIN, f"{serial}_{SIREN.key}"
        )
        # Only ours to remove: another account could hold the same camera.
        existing = registry.async_get(entity_id) if entity_id else None
        if entity_id and existing and existing.config_entry_id == entry.entry_id:
            _LOGGER.debug(
                "Removing %s: %s does not sound an alarm", entity_id, serial
            )
            registry.async_remove(entity_id)

    async_add_entities(sirens)


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
