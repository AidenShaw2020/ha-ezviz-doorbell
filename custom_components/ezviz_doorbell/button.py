"""Buttons for an EZVIZ camera.

The one that matters is Wake: a battery doorbell sleeps between events and
answers nothing while it does, so anything you ask of it - a picture, a live
view - starts with getting it back on the network.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity


@dataclass(frozen=True, kw_only=True)
class EzvizButtonDescription(ButtonEntityDescription):
    """Describes one EZVIZ button."""

    press_fn: Callable[
        [EzvizDoorbellCoordinator, str], Coroutine[Any, Any, Any]
    ]


BUTTONS: tuple[EzvizButtonDescription, ...] = (
    EzvizButtonDescription(
        key="wake",
        translation_key="wake",
        press_fn=lambda coordinator, serial: coordinator.async_wake(serial),
    ),
    EzvizButtonDescription(
        key="snapshot",
        translation_key="snapshot",
        press_fn=lambda coordinator, serial: coordinator.async_capture(serial),
    ),
    EzvizButtonDescription(
        key="reboot",
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda coordinator, serial: coordinator.async_execute(
            lambda client: client.reboot_camera(serial)
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizButton(coordinator, serial, description)
        for serial in coordinator.data
        for description in BUTTONS
    )


class EzvizButton(EzvizDoorbellEntity, ButtonEntity):
    """One thing the camera can be asked to do."""

    entity_description: EzvizButtonDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizButtonDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        """Ask the camera to do it."""
        await self.entity_description.press_fn(self.coordinator, self._serial)
