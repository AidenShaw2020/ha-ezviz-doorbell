"""The camera entity these doorbells never used to get.

There is no RTSP server on a battery doorbell, so the usual route to a camera
entity is closed. What EZVIZ has instead is a cloud stream, which this
integration serves back to Home Assistant as a URL of its own - see
:mod:`stream_view` - and hands to the stream component here. Still pictures are
taken on demand, which is also what wakes a sleeping device.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EzvizDoorbellConfigEntry
from .const import (
    CONF_LIVE_STREAM,
    CONF_SNAPSHOT_INTERVAL,
    CONF_STREAM_TOKEN,
    DEFAULT_SNAPSHOT_INTERVAL,
)
from .coordinator import EzvizDoorbellCoordinator
from .entity import EzvizDoorbellEntity
from .stream_view import stream_url

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cameras."""
    coordinator = entry.runtime_data
    async_add_entities(
        EzvizDoorbellCamera(coordinator, serial) for serial in coordinator.data
    )


class EzvizDoorbellCamera(EzvizDoorbellEntity, Camera):
    """Live video and stills for one EZVIZ camera."""

    # The camera is what the device is for, so it carries the device's name
    # rather than a name of its own.
    _attr_name = None

    def __init__(self, coordinator: EzvizDoorbellCoordinator, serial: str) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, serial, "camera")
        Camera.__init__(self)

        options = coordinator.config_entry.options
        self._live_stream = options.get(CONF_LIVE_STREAM, True)
        self._snapshot_interval = float(
            options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
        )
        # A play button that can only ever fail is worse than no play
        # button: Home Assistant retries a broken stream over and over, and
        # fills the log doing it. So it is only offered when there is really
        # something to play.
        self._can_stream = (
            self._live_stream
            and coordinator.cloud_stream is not None
            and self._stream_is_decodable(coordinator, serial)
        )
        if self._can_stream:
            self._attr_supported_features = CameraEntityFeature.STREAM
        self._capture_lock = asyncio.Lock()
        self._capture_refused = False

    @staticmethod
    def _stream_is_decodable(
        coordinator: EzvizDoorbellCoordinator, serial: str
    ) -> bool:
        """Return whether this camera's video can actually be played.

        An encrypted stream is decrypted as it arrives, which needs the
        camera's key - either the one EZVIZ hands the account or the code from
        the device's label. Without either there is nothing to decode with, and
        a play button that can only fail makes Home Assistant retry it for ever.
        """
        if not coordinator.data[serial].raw.get("encrypted"):
            return True
        if coordinator.media_key(serial) is not None:
            return True
        _LOGGER.info(
            "%s has video encryption on and no key configured, so it is"
            " offered as stills. Give the integration the code from its label"
            " under Reconfigure, or switch video encryption off for it in the"
            " EZVIZ app",
            serial,
        )
        return False

    @property
    def frame_interval(self) -> float:
        """Return how often the still image stream may fetch a new picture.

        Every frame is a round trip to the cloud and a wake-up for the camera,
        so this is deliberately slow.
        """
        return self._snapshot_interval

    async def stream_source(self) -> str | None:
        """Return the URL the stream component should open."""
        if not self._can_stream:
            return None
        token = self.coordinator.config_entry.data.get(CONF_STREAM_TOKEN)
        if not token:
            return None
        return stream_url(
            self.hass,
            self.coordinator.config_entry.entry_id,
            self._serial,
            str(token),
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a picture, taking a new one if the last is stale."""
        device = self.device
        if device.snapshot and device.snapshot_time:
            age = (dt_util.utcnow() - device.snapshot_time).total_seconds()
            if age < self._snapshot_interval:
                return device.snapshot

        if self._capture_lock.locked():
            # A capture is already on its way; a second one would only wake the
            # camera twice for the same picture.
            async with self._capture_lock:
                return self.device.snapshot

        async with self._capture_lock:
            try:
                fresh = await self.coordinator.async_capture(self._serial)
            except HomeAssistantError as err:
                # The card asks for a picture on its own schedule, so this
                # cannot be raised at it - but it should be said once.
                if not self._capture_refused:
                    self._capture_refused = True
                    _LOGGER.warning("Cannot take a picture of %s: %s", self.name, err)
                return device.snapshot
            self._capture_refused = False
            return fresh or device.snapshot
