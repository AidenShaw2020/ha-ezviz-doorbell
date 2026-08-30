"""The EZVIZ Doorbell integration.

A doorbell that hibernates on battery never passes the built-in integration's
RTSP check, so it ends up with no camera and almost no entities. This
integration talks to the same cloud API the EZVIZ app uses instead, which needs
no RTSP server to exist: the ring arrives as its own event, motion as another,
and the live view is served out of Home Assistant itself.
"""

from __future__ import annotations

import secrets

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_STREAM_TOKEN, DOMAIN
from .coordinator import (
    EzvizDoorbellCoordinator,
    config_signature,
    report_library,
)
from .stream_view import EzvizStreamView, import_cloud_stream

type EzvizDoorbellConfigEntry = ConfigEntry[EzvizDoorbellCoordinator]

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SIREN,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: EzvizDoorbellConfigEntry
) -> bool:
    """Set up one EZVIZ account."""
    # Importing reads from disk, so it happens off the event loop, once.
    cloud_stream = await hass.async_add_executor_job(import_cloud_stream)
    report_library(cloud_stream is not None)

    _ensure_stream_token(hass, entry)
    _register_stream_view(hass)

    coordinator = EzvizDoorbellCoordinator(hass, entry)
    coordinator.cloud_stream = cloud_stream
    await coordinator.async_login()
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_listening()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EzvizDoorbellConfigEntry
) -> bool:
    """Tear down one EZVIZ account."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Setup may not have got as far as the coordinator, and Home Assistant
    # unloads the entry either way.
    if unloaded and (coordinator := getattr(entry, "runtime_data", None)):
        await coordinator.async_unload()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: EzvizDoorbellConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Let a camera this account no longer handles be deleted.

    Untick a camera in the options and its entities stop being built, but the
    device it left behind stays in the registry until somebody removes it. This
    is what makes the delete button on its page work.
    """
    handled = entry.runtime_data.data
    return not any(
        identifier[1] in handled
        for identifier in device_entry.identifiers
        if identifier[0] == DOMAIN
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: EzvizDoorbellConfigEntry
) -> None:
    """Reload when the options change; the intervals are read at setup.

    This listener fires for any change to the entry - a refreshed session
    written back at startup, or a camera's verification code added as a
    subentry. Reloading on the session would store a new one, fire the listener
    again and never stop, so only what the integration actually reads at setup
    counts as a change.
    """
    coordinator = entry.runtime_data
    if config_signature(entry) == coordinator.loaded_signature:
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _ensure_stream_token(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give this account a stream token if it has none yet.

    The view that serves live video cannot ask for a Home Assistant login -
    FFmpeg, which is what opens the stream, has no way to present one - so the
    URL carries a secret of its own instead. It is generated once and stays put
    for the life of the config entry.
    """
    if entry.data.get(CONF_STREAM_TOKEN):
        return
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_STREAM_TOKEN: secrets.token_urlsafe(16)},
    )


def _register_stream_view(hass: HomeAssistant) -> None:
    """Register the live video view once, however many accounts there are."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("stream_view_registered"):
        return
    hass.http.register_view(EzvizStreamView(hass))
    domain_data["stream_view_registered"] = True
