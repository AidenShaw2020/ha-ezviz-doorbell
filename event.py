"""EZVIZ real time events delivered over cloud push."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizEntity
from .push import signal_for_serial

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

EVENT_RING = "ring"
EVENT_MOTION = "motion"
EVENT_ALARM = "alarm"

# Observed EZVIZ push alert codes. 0 is the doorbell button, 10000 the PIR.
# Anything unknown is reported as a generic alarm, with the raw code kept in
# the event attributes so new codes can be identified and mapped here.
ALERT_TYPE_MAP: dict[int, str] = {
    0: EVENT_RING,
    10000: EVENT_MOTION,
}

EVENT_DESCRIPTION = EventEntityDescription(
    key="alerts",
    translation_key="alerts",
    device_class=EventDeviceClass.DOORBELL,
)


async def async_setup_entry(
    hass, entry: EzvizConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up EZVIZ event entities based on a config entry."""
    coordinator = entry.runtime_data

    async_add_entities(
        EzvizAlertEvent(coordinator, serial) for serial in coordinator.data
    )


class EzvizAlertEvent(EzvizEntity, EventEntity):
    """Fires when EZVIZ pushes an alert for this device."""

    _attr_event_types = [EVENT_RING, EVENT_MOTION, EVENT_ALARM]
    _attr_should_poll = False

    def __init__(
        self, coordinator: EzvizDataUpdateCoordinator, serial: str
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, serial)
        self.entity_description = EVENT_DESCRIPTION
        self._attr_unique_id = f"{serial}_{EVENT_DESCRIPTION.key}"

    @property
    def available(self) -> bool:
        """Return True at all times.

        A battery doorbell reports itself offline while it hibernates, which is
        exactly when a ring is most likely to arrive. Availability must not
        follow the polled device status here.
        """
        return True

    async def async_added_to_hass(self) -> None:
        """Subscribe to push messages for this device."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_for_serial(self._serial), self._handle_push
            )
        )

    @callback
    def _handle_push(self, msg: dict[str, Any]) -> None:
        """Fire an event from a decoded push message."""
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}

        raw_code = ext.get("alert_type_code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1

        event_type = ALERT_TYPE_MAP.get(code, EVENT_ALARM)
        if event_type is EVENT_ALARM:
            _LOGGER.debug(
                "Unmapped EZVIZ alert code %s for %s, reported as '%s'",
                raw_code,
                self._serial,
                EVENT_ALARM,
            )

        self._trigger_event(
            event_type,
            {
                "alert_type_code": raw_code,
                "alert": msg.get("alert"),
                "time": ext.get("time"),
                "pic_url": ext.get("default_pic_url"),
                "is_encrypted": ext.get("is_encrypted"),
                "msg_id": ext.get("msgId"),
            },
        )
        self.async_write_ha_state()
