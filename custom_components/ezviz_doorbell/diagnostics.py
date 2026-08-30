"""What this integration made of the account, for when something looks wrong.

Whether a camera offers live video is decided from three things - the library,
the encryption flag and whether there is a key - and none of them is visible
from the outside. This puts all three in one place, with nothing secret in it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import EzvizDoorbellConfigEntry
from .const import CONF_DEVICES, CONF_LIVE_STREAM


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EzvizDoorbellConfigEntry
) -> dict[str, Any]:
    """Return what the integration knows, without the credentials."""
    coordinator = entry.runtime_data

    return {
        "options": {
            key: value
            for key, value in entry.options.items()
            if key in (CONF_LIVE_STREAM, CONF_DEVICES)
        },
        "library": {
            "cloud_stream_available": coordinator.cloud_stream is not None,
            "missing": coordinator_gaps(coordinator),
        },
        "devices": [
            {
                "serial": serial,
                "name": device.name,
                "model": device.model,
                "category": device.raw.get("device_category"),
                "online": device.online,
                "video_encryption": bool(device.raw.get("encrypted")),
                "verification_code_configured": (
                    coordinator.verification_code(serial) is not None
                ),
                "live_video_offered": _live_video_offered(coordinator, device, serial),
                "switches": sorted(device.switches),
                "last_event": device.last_event,
                "last_ring": device.last_ring,
                "last_motion": device.last_motion,
                "snapshot_taken": device.snapshot is not None,
            }
            for serial, device in coordinator.data.items()
        ],
    }


def coordinator_gaps(coordinator: Any) -> list[str]:
    """Return what the bundled library cannot do, if anything."""
    from .coordinator import library_gaps  # noqa: PLC0415 - avoids a cycle

    return library_gaps(coordinator.cloud_stream is not None)


def _live_video_offered(coordinator: Any, device: Any, serial: str) -> bool:
    """Return whether this camera's entity offers a stream at all."""
    if coordinator.cloud_stream is None:
        return False
    if not coordinator.config_entry.options.get(CONF_LIVE_STREAM, True):
        return False
    if not device.raw.get("encrypted"):
        return True
    return coordinator.verification_code(serial) is not None
