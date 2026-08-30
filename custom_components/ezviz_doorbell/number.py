"""How sensitive the camera's detection is."""

from __future__ import annotations

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EzvizDoorbellConfigEntry
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity

SENSITIVITY = NumberEntityDescription(
    key="detection_sensitivity",
    translation_key="detection_sensitivity",
    native_min_value=1,
    native_max_value=6,
    native_step=1,
    mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the detection sensitivity."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizSensitivityNumber(coordinator, serial) for serial in coordinator.data
    )


class EzvizSensitivityNumber(EzvizDoorbellEntity, NumberEntity):
    """The detection sensitivity, 1 for the least and 6 for the most."""

    entity_description = SENSITIVITY

    def __init__(self, coordinator: EzvizDoorbellCoordinator, serial: str) -> None:
        """Initialize the number."""
        super().__init__(coordinator, serial, SENSITIVITY.key)
        self.entity_description = SENSITIVITY

    @property
    def native_value(self) -> float | None:
        """Return the sensitivity the cloud reports."""
        value = self.device.detection_sensitivity
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Send a new sensitivity to the camera."""
        wanted = int(value)

        def _set(client: EzvizClient) -> None:
            # Battery cameras keep sensitivity under algorithm type 3 and mains
            # powered ones under type 0; a device rejects the type it does not
            # use, so the other one is tried before giving up.
            try:
                client.detection_sensibility(self._serial, wanted, 3)
            except PyEzvizError:
                client.detection_sensibility(self._serial, wanted, 0)

        await self.coordinator.async_execute(_set)
        # The cloud is only asked for this once, so the new value is recorded
        # here rather than waiting for a refresh that will not fetch it.
        self.device.detection_sensitivity = wanted
        self.async_write_ha_state()
