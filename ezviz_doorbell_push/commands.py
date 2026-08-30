"""Commands Home Assistant can send back to a camera.

Every controllable entity in :mod:`entities` names a handler here. Handlers run
on the bridge's command worker thread, never on the MQTT network thread, so a
slow cloud call cannot stall the push connection.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from const import (
    ALARM_SOUND_MODES,
    DETECTION_TYPES,
    DISPLAY_MODES,
    NIGHT_VISION_MODES,
    WORK_MODES,
)
from pyezvizapi.constants import DeviceSwitchType
from pyezvizapi.exceptions import PyEzvizError

_LOGGER = logging.getLogger("ezviz_push.commands")

Handler = Callable[[Any, str, str], None]


def _is_on(payload: str) -> bool:
    """Return whether an MQTT on/off payload means on."""
    return payload.strip().upper() in {"ON", "1", "TRUE", "OPEN"}


def _option(options: dict[str, int], payload: str) -> int:
    """Return the EZVIZ enum value for a select option label."""
    try:
        return options[payload]
    except KeyError:
        raise PyEzvizError(
            f"{payload!r} is not one of {', '.join(options)}"
        ) from None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _motion_detection(bridge: Any, serial: str, payload: str) -> None:
    """Arm or disarm motion detection."""
    bridge.client.set_camera_defence(serial, 1 if _is_on(payload) else 0)


def _notify_alarm(bridge: Any, serial: str, payload: str) -> None:
    """Switch EZVIZ alarm push notifications on or off.

    The cloud stores the inverse - a do-not-disturb flag - so the entity's ON
    is sent as "do not disturb: off".
    """
    bridge.client.do_not_disturb(serial, 0 if _is_on(payload) else 1)


def _notify_call(bridge: Any, serial: str, payload: str) -> None:
    """Switch doorbell call notifications on or off.

    Calls have their own do-not-disturb flag, and it is the one that decides
    whether a button press is pushed at all, so it is inverted the same way.
    """
    bridge.client.set_answer_call(serial, 0 if _is_on(payload) else 1)


def _detection_sensitivity(bridge: Any, serial: str, payload: str) -> None:
    """Set detection sensitivity, 1 (least) to 6 (most) sensitive."""
    value = int(float(payload))
    # Battery cameras keep sensitivity under algorithm type 3, mains powered
    # ones under type 0, and a device rejects the type it does not use.
    try:
        bridge.client.detection_sensibility(serial, value, 3)
    except PyEzvizError:
        bridge.client.detection_sensibility(serial, value, 0)


def _work_mode(bridge: Any, serial: str, payload: str) -> None:
    """Set the battery work mode."""
    bridge.client.set_battery_camera_work_mode(serial, _option(WORK_MODES, payload))


def _night_vision_mode(bridge: Any, serial: str, payload: str) -> None:
    """Set the night vision mode."""
    bridge.client.set_night_vision_mode(serial, _option(NIGHT_VISION_MODES, payload))


def _display_mode(bridge: Any, serial: str, payload: str) -> None:
    """Set the image style."""
    bridge.client.set_display_mode(serial, _option(DISPLAY_MODES, payload))


def _alarm_sound_mode(bridge: Any, serial: str, payload: str) -> None:
    """Set the alarm sound mode."""
    bridge.client.alarm_sound(serial, _option(ALARM_SOUND_MODES, payload), 1)


def _detection_type(bridge: Any, serial: str, payload: str) -> None:
    """Set what the camera looks for when it detects."""
    bridge.client.set_alarm_detect_human_car(serial, _option(DETECTION_TYPES, payload))


def _reboot(bridge: Any, serial: str, payload: str) -> None:
    """Reboot the camera."""
    bridge.client.reboot_camera(serial)


def _siren(bridge: Any, serial: str, payload: str) -> None:
    """Sound or silence the camera's own alarm."""
    bridge.client.sound_alarm(serial, 1 if _is_on(payload) else 0)


def _firmware(bridge: Any, serial: str, payload: str) -> None:
    """Start a firmware upgrade."""
    bridge.client.upgrade_device(serial)


def _snapshot(bridge: Any, serial: str, payload: str) -> None:
    """Make the camera take a fresh picture and publish it."""
    bridge.capture_snapshot(serial)


def _refresh(bridge: Any, serial: str, payload: str) -> None:
    """Fetch the device status again straight away."""
    bridge.request_status_refresh()


def _wake(bridge: Any, serial: str, payload: str) -> None:
    """Wake a hibernating battery camera.

    A battery doorbell sleeps between events, and while it sleeps it answers
    nothing - no live view, no fresh snapshot. The EZVIZ app wakes it by
    opening the live view, which is three separate requests underneath. All
    three are tried in turn, and each is allowed to fail: which of them a given
    model honours depends on its firmware, and one success is enough.
    """
    woke = False

    try:
        bridge.client.switch_status(serial, DeviceSwitchType.SLEEP.value, 0)
        _LOGGER.info("Wake %s: sleep switch turned off", serial)
        woke = True
    except (PyEzvizError, OSError, KeyError) as err:
        _LOGGER.debug("Wake %s: sleep switch not accepted (%s)", serial, err)

    try:
        bridge.client.delay_battery_device_sleep(serial, 1, 1)
        _LOGGER.info("Wake %s: asked the cloud to keep it awake", serial)
        woke = True
    except (PyEzvizError, OSError, KeyError) as err:
        _LOGGER.debug("Wake %s: sleep delay not accepted (%s)", serial, err)

    # Capturing a picture is what actually pulls a sleeping device onto the
    # network, and it leaves a fresh image behind as proof that it worked.
    try:
        bridge.capture_snapshot(serial)
        woke = True
    except (PyEzvizError, OSError, KeyError) as err:
        _LOGGER.debug("Wake %s: capture failed (%s)", serial, err)

    if woke:
        _LOGGER.info("Wake %s: done", serial)
    else:
        _LOGGER.warning(
            "Wake %s: every wake request was refused - the device may be out of"
            " battery, offline, or shared to this account without control rights",
            serial,
        )

    bridge.request_status_refresh()


HANDLERS: dict[str, Handler] = {
    "motion_detection": _motion_detection,
    "notify_alarm": _notify_alarm,
    "notify_call": _notify_call,
    "detection_sensitivity": _detection_sensitivity,
    "work_mode": _work_mode,
    "night_vision_mode": _night_vision_mode,
    "display_mode": _display_mode,
    "alarm_sound_mode": _alarm_sound_mode,
    "detection_type": _detection_type,
    "reboot": _reboot,
    "siren": _siren,
    "firmware": _firmware,
    "snapshot": _snapshot,
    "refresh": _refresh,
    "wake": _wake,
}


def dispatch(bridge: Any, serial: str, key: str, payload: str) -> None:
    """Run the handler for one command topic.

    Raises:
        PyEzvizError: If the command is unknown or the cloud refuses it.
    """
    if key.startswith("switch_"):
        number = int(key.removeprefix("switch_"))
        bridge.client.switch_status(serial, number, _is_on(payload))
        return

    handler = HANDLERS.get(key)
    if handler is None:
        raise PyEzvizError(f"No handler for command {key!r}")
    handler(bridge, serial, payload)
