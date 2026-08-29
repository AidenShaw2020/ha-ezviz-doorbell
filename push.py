"""EZVIZ cloud push (MQTT) support.

The upstream integration is pure 30 second cloud polling, which cannot see a
doorbell button press at all: the ring is delivered over the EZVIZ push
channel, never through the polled alarm feed.

This module keeps a long lived connection to that push channel open and
dispatches every decoded message to the entities interested in it.
"""

from __future__ import annotations

import logging
from typing import Any

from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import PyEzvizError

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

_LOGGER = logging.getLogger(__name__)

SIGNAL_PUSH_MESSAGE = "ezviz_push_message"


def signal_for_serial(serial: str) -> str:
    """Return the dispatcher signal carrying push messages for one device."""
    return f"{SIGNAL_PUSH_MESSAGE}_{serial}"


class EzvizPushManager:
    """Keep an EZVIZ MQTT push connection alive for a cloud account."""

    def __init__(self, hass: HomeAssistant, client: EzvizClient) -> None:
        """Initialize the push manager."""
        self.hass = hass
        self._client = client
        self._mqtt_client: Any = None

    @property
    def connected(self) -> bool:
        """Return True while the push connection is up."""
        return self._mqtt_client is not None

    async def async_start(self) -> None:
        """Connect to the EZVIZ push service.

        Push is a bonus on top of polling, so a failure here is logged and
        swallowed: the rest of the integration must keep working without it.
        """
        try:
            self._mqtt_client = await self.hass.async_add_executor_job(self._connect)
        except (PyEzvizError, OSError, KeyError) as err:
            _LOGGER.warning(
                "EZVIZ push unavailable, real time events are disabled: %s", err
            )
            self._mqtt_client = None
        else:
            _LOGGER.debug("EZVIZ push connected")

    def _connect(self) -> Any:
        """Create and connect the MQTT client (executor thread)."""
        mqtt_client = self._client.get_mqtt_client(
            on_message_callback=self._on_message
        )
        mqtt_client.connect()
        return mqtt_client

    def _on_message(self, msg: dict[str, Any]) -> None:
        """Handle a decoded push message.

        Called from the paho network thread, so hand the message over to the
        event loop before touching any Home Assistant API.
        """
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}
        serial = ext.get("device_serial")
        if not serial:
            _LOGGER.debug("Ignoring push message without a serial: %s", msg)
            return

        _LOGGER.debug("EZVIZ push for %s: %s", serial, msg)
        self.hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self.hass, signal_for_serial(serial), msg
        )

    async def async_stop(self) -> None:
        """Disconnect from the EZVIZ push service."""
        if self._mqtt_client is None:
            return
        mqtt_client, self._mqtt_client = self._mqtt_client, None
        try:
            await self.hass.async_add_executor_job(mqtt_client.stop)
        except (PyEzvizError, OSError) as err:
            _LOGGER.debug("Error while stopping EZVIZ push: %s", err)
