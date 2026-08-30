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
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .const import (
    CAMERA_SUBENTRY,
    CONF_SERIAL,
    CONF_STREAM_TOKEN,
    CONF_VERIFICATION_CODE,
    CONF_VERIFICATION_CODES,
    DOMAIN,
)
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
    _migrate_codes_out_of_subentries(hass, entry)
    _register_stream_view(hass)

    coordinator = EzvizDoorbellCoordinator(hass, entry)
    coordinator.cloud_stream = cloud_stream
    await coordinator.async_login()
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_listening()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _review_live_video(hass, entry, coordinator)
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


def _review_live_video(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: EzvizDoorbellCoordinator,
) -> None:
    """Raise a repair for each camera that cannot offer live video.

    A camera with encryption on and no key is offered as stills, which looks
    from the outside exactly like a camera that is simply not streaming. Saying
    so in the log at info level tells nobody; a repair says it where somebody
    will see it, and goes away by itself once the code is given.
    """
    for serial, device in coordinator.data.items():
        issue_id = f"video_encryption_{serial}"
        blocked = bool(device.raw.get("encrypted")) and (
            coordinator.media_key(serial) is None
        )
        if not blocked:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue

        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="video_encryption",
            translation_placeholders={"name": device.name, "serial": serial},
            learn_more_url="https://github.com/AidenShaw2020/ha-ezviz-doorbell#video-encryption-and-the-key-that-gets-past-it",
        )


def _migrate_codes_out_of_subentries(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Move verification codes into the account's data.

    They were briefly a subentry per camera, which put them in the list of
    things this integration had set up - next to the cameras themselves, where
    a piece of configuration has no business being. Anyone who added one there
    keeps it without having to type it again.
    """
    subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == CAMERA_SUBENTRY
    ]
    if not subentries:
        return

    codes = dict(entry.data.get(CONF_VERIFICATION_CODES) or {})
    for subentry in subentries:
        serial = subentry.data.get(CONF_SERIAL)
        code = subentry.data.get(CONF_VERIFICATION_CODE)
        if serial and code:
            codes[str(serial)] = str(code)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_VERIFICATION_CODES: codes}
    )
    for subentry in subentries:
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)


def _register_stream_view(hass: HomeAssistant) -> None:
    """Register the live video view once, however many accounts there are."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("stream_view_registered"):
        return
    hass.http.register_view(EzvizStreamView(hass))
    domain_data["stream_view_registered"] = True
