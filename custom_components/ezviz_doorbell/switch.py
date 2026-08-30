"""Switches for an EZVIZ camera.

Three of them are account-level settings that decide whether events reach Home
Assistant at all; the rest are the device's own switches, and which of those
exist is whatever the device itself reports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyezvizapi.client import EzvizClient

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .const import DIAGNOSTIC_SWITCHES, SWITCH_ICONS, SWITCH_NAMES
from .coordinator import DeviceData, EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity


@dataclass(frozen=True, kw_only=True)
class EzvizSwitchDescription(SwitchEntityDescription):
    """Describes one EZVIZ switch."""

    value_fn: Callable[[DeviceData], bool]
    set_fn: Callable[[EzvizClient, str, bool], Any]


SWITCHES: tuple[EzvizSwitchDescription, ...] = (
    EzvizSwitchDescription(
        key="motion_detection",
        translation_key="motion_detection",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda device: bool(device.raw.get("alarm_notify")),
        set_fn=lambda client, serial, on: client.set_camera_defence(
            serial, 1 if on else 0
        ),
    ),
    EzvizSwitchDescription(
        key="notify_alarm",
        translation_key="notify_alarm",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda device: bool(device.raw.get("push_notify_alarm")),
        # The cloud stores the inverse - a do-not-disturb flag - so switching
        # notifications on is sent as "do not disturb: off".
        set_fn=lambda client, serial, on: client.do_not_disturb(
            serial, 0 if on else 1
        ),
    ),
    EzvizSwitchDescription(
        key="notify_call",
        translation_key="notify_call",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda device: bool(device.raw.get("push_notify_call")),
        # Calls have their own do-not-disturb flag, and it is the one that
        # decides whether a button press is pushed at all.
        set_fn=lambda client, serial, on: client.set_answer_call(
            serial, 0 if on else 1
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for serial, device in coordinator.data.items():
        entities.extend(
            EzvizSwitch(coordinator, serial, description) for description in SWITCHES
        )
        entities.extend(
            EzvizDeviceSwitch(coordinator, serial, number)
            for number in sorted(device.switches)
        )
    async_add_entities(entities)


class EzvizSwitch(EzvizDoorbellEntity, SwitchEntity):
    """A cloud setting that can be switched on and off."""

    entity_description: EzvizSwitchDescription

    def __init__(
        self,
        coordinator: EzvizDoorbellCoordinator,
        serial: str,
        description: EzvizSwitchDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return whether the setting is on."""
        return self.entity_description.value_fn(self.device)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the setting off."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        """Send the new value to EZVIZ."""
        await self.coordinator.async_execute(
            lambda client: self.entity_description.set_fn(client, self._serial, on)
        )


class EzvizDeviceSwitch(EzvizDoorbellEntity, SwitchEntity):
    """One of the switches the device itself reports."""

    def __init__(
        self, coordinator: EzvizDoorbellCoordinator, serial: str, number: int
    ) -> None:
        """Initialize the switch from its EZVIZ switch type."""
        super().__init__(coordinator, serial, f"switch_{number}")
        self._number = number
        # Only the well known switch types have a translation; anything else
        # is named after its number rather than left blank.
        if number in SWITCH_NAMES:
            self._attr_translation_key = f"device_switch_{number}"
        else:
            self._attr_name = f"Switch {number}"
        self._attr_icon = SWITCH_ICONS.get(number)
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC
            if number in DIAGNOSTIC_SWITCHES
            else EntityCategory.CONFIG
        )

    @property
    def is_on(self) -> bool:
        """Return whether the device reports this switch as on."""
        return bool(self.device.switches.get(self._number))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch it on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch it off."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        """Send the new value to EZVIZ."""
        await self.coordinator.async_execute(
            lambda client: client.switch_status(self._serial, self._number, on)
        )
