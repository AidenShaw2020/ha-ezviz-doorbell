"""The camera's picture and power settings, as pick-one lists."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .const import (
    ALARM_SOUND_MODES,
    DETECTION_TYPES,
    DISPLAY_MODES,
    NIGHT_VISION_MODES,
    SOUND_MODE_KEYS,
    WORK_MODES,
)
from .coordinator import DeviceData, EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity
from .helpers import display_mode, night_vision_mode, option_key
from .vendor.pyezvizapi.client import EzvizClient


@dataclass(frozen=True, kw_only=True)
class EzvizSelectDescription(SelectEntityDescription):
    """Describes one EZVIZ select."""

    # Option key to the number EZVIZ knows it by.
    values: dict[str, int]
    current_fn: Callable[[DeviceData], str | None]
    set_fn: Callable[[EzvizClient, str, int], Any]


SELECTS: tuple[EzvizSelectDescription, ...] = (
    EzvizSelectDescription(
        key="work_mode",
        translation_key="work_mode",
        options=list(WORK_MODES),
        values=WORK_MODES,
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda device: option_key(
            WORK_MODES, device.raw.get("battery_camera_work_mode")
        ),
        set_fn=lambda client, serial, value: client.set_battery_camera_work_mode(
            serial, value
        ),
    ),
    EzvizSelectDescription(
        key="night_vision_mode",
        translation_key="night_vision_mode",
        options=list(NIGHT_VISION_MODES),
        values=NIGHT_VISION_MODES,
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda device: option_key(
            NIGHT_VISION_MODES, night_vision_mode(device.raw)
        ),
        set_fn=lambda client, serial, value: client.set_night_vision_mode(
            serial, value
        ),
    ),
    EzvizSelectDescription(
        key="display_mode",
        translation_key="display_mode",
        options=list(DISPLAY_MODES),
        values=DISPLAY_MODES,
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda device: option_key(DISPLAY_MODES, display_mode(device.raw)),
        set_fn=lambda client, serial, value: client.set_display_mode(serial, value),
    ),
    EzvizSelectDescription(
        key="alarm_sound_mode",
        translation_key="alarm_sound_mode",
        options=list(ALARM_SOUND_MODES),
        values=ALARM_SOUND_MODES,
        entity_category=EntityCategory.CONFIG,
        # This one is reported as a name rather than a number, so it takes its
        # own route back to the option key.
        current_fn=lambda device: SOUND_MODE_KEYS.get(
            str(device.raw.get("alarm_sound_mod") or "").upper()
        ),
        set_fn=lambda client, serial, value: client.alarm_sound(serial, value, 1),
    ),
    EzvizSelectDescription(
        key="detection_type",
        translation_key="detection_type",
        options=list(DETECTION_TYPES),
        values=DETECTION_TYPES,
        entity_category=EntityCategory.CONFIG,
        current_fn=lambda device: option_key(
            DETECTION_TYPES, device.raw.get("Alarm_DetectHumanCar")
        ),
        set_fn=lambda client, serial, value: client.set_alarm_detect_human_car(
            serial, value
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the selects."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizSelect(coordinator, serial, description)
        for serial in coordinator.data
        for description in SELECTS
    )


class EzvizSelect(EzvizDoorbellEntity, SelectEntity):
    """One setting with a handful of possible values."""

    entity_description: EzvizSelectDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizSelectDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def current_option(self) -> str | None:
        """Return the option the camera is set to."""
        return self.entity_description.current_fn(self.device)

    async def async_select_option(self, option: str) -> None:
        """Send a new setting to the camera."""
        value = self.entity_description.values[option]
        await self.coordinator.async_execute(
            lambda client: self.entity_description.set_fn(client, self._serial, value)
        )
