"""Turn a pyezvizapi camera status into the flat JSON the entities read.

``EzvizCamera.status()`` returns the cloud's own shape: nested optionals, codes
where Home Assistant wants labels, and a switch list keyed by integers that
cannot be addressed from a Jinja template. Everything here exists to flatten
that into one dictionary whose keys match the value templates in
:mod:`entities`.
"""

from __future__ import annotations

import json
from typing import Any

from const import (
    ALARM_SOUND_MODES,
    DETECTION_TYPES,
    DISPLAY_MODES,
    NIGHT_VISION_MODES,
    WORK_MODES,
)

# EZVIZ reports the alarm sound as a SoundMode *name* but takes a number when
# it is set, so the name has to be translated back to the label the select
# offers. Derived from that same table, which is what keeps the two in step.
SOUND_MODE_LABELS: dict[str, str] = {
    label.upper(): label for label in ALARM_SOUND_MODES
}


def _label(options: dict[str, int], value: Any) -> str | None:
    """Return the option label for an EZVIZ enum value."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    for label, candidate in options.items():
        if candidate == number:
            return label
    return None


def _nested(value: Any, key: str) -> Any:
    """Return ``key`` from a value that may be a dict or a JSON string.

    Several optionals arrive as a JSON document inside a string, and which of
    the two a device sends varies by model and firmware.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return value.get(key)
    return None


def _int(value: Any) -> int | None:
    """Return value as an int, or None when it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _disk_capacity(value: Any) -> int | None:
    """Return the card size in MB from any shape EZVIZ reports it in.

    It arrives as a bare number, as a comma separated string of one figure per
    card, or as that string already split into a list, depending on the model
    and on how far pyezvizapi got parsing it.
    """
    if isinstance(value, str):
        value = value.split(",")
    if isinstance(value, list):
        value = value[0] if value else None
    return _int(value)


def build_status(
    serial: str,
    status: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the flat status document published for one device."""
    wifi = status.get("WIFI") if isinstance(status.get("WIFI"), dict) else {}
    optionals = (
        status.get("optionals") if isinstance(status.get("optionals"), dict) else {}
    )
    disk = status.get("diskCapacity")
    night_vision = optionals.get("NightVision_Model")

    data: dict[str, Any] = {
        "serial": serial,
        "name": status.get("name"),
        "model": status.get("device_sub_category") or status.get("device_category"),
        "version": status.get("version"),
        "online": status.get("status") == 1,
        "mac_address": status.get("mac_address"),
        "local_ip": status.get("local_ip"),
        "wan_ip": status.get("wan_ip"),
        "wifi_ssid": wifi.get("ssid") or wifi.get("netName"),
        "wifi_signal": wifi.get("signal"),
        "battery_level": status.get("battery_level"),
        "encrypted": bool(status.get("encrypted")),
        "alarm_notify": bool(status.get("alarm_notify")),
        "alarm_schedules_enabled": bool(status.get("alarm_schedules_enabled")),
        "push_notify_alarm": bool(status.get("push_notify_alarm")),
        "push_notify_call": bool(status.get("push_notify_call")),
        "pir_status": status.get("PIR_Status"),
        "last_alarm_time": status.get("last_alarm_time"),
        "last_alarm_type_code": status.get("last_alarm_type_code"),
        "last_alarm_type_name": status.get("last_alarm_type_name"),
        "seconds_last_trigger": status.get("Seconds_Last_Trigger"),
        "alarm_sound_mode": SOUND_MODE_LABELS.get(
            str(status.get("alarm_sound_mod") or "").upper()
        ),
        "work_mode": _label(WORK_MODES, status.get("battery_camera_work_mode")),
        "night_vision_mode": _label(
            NIGHT_VISION_MODES,
            _nested(night_vision, "graphicType")
            if not isinstance(night_vision, int)
            else night_vision,
        ),
        "display_mode": _label(
            DISPLAY_MODES, _nested(optionals.get("display_mode"), "mode")
        ),
        "detection_type": _label(DETECTION_TYPES, status.get("Alarm_DetectHumanCar")),
        "disk_capacity_mb": _disk_capacity(disk),
        "upgrade_available": bool(status.get("upgrade_available")),
        "upgrade_in_progress": bool(status.get("upgrade_in_progress")),
        "upgrade_percent": status.get("upgrade_percent"),
        # Jinja cannot subscript a dict with an integer key, so the switch
        # numbers are prefixed and addressed as switches.s21 and friends.
        "switches": {
            f"s{number}": bool(state)
            for number, state in (status.get("switches") or {}).items()
        },
    }

    # Keys the bridge fills in rather than the cloud. They are seeded here so
    # that every value template finds its key, whether or not the feature that
    # writes it is switched on.
    for key in (
        "detection_sensitivity",
        "last_event",
        "last_event_time",
        "last_ring",
        "last_motion",
        "live_stream_url",
        "snapshot_url",
        "mjpeg_url",
    ):
        data.setdefault(key, None)

    data.update(extra or {})
    return data


def firmware_payload(status: dict[str, Any]) -> dict[str, Any]:
    """Return the JSON state for the update entity."""
    installed = status.get("version")
    latest_info = status.get("latest_firmware_info")
    latest = None
    if isinstance(latest_info, dict):
        latest = latest_info.get("version") or latest_info.get("fullVersion")
    if not latest:
        # No upgrade offered means the installed version *is* the latest; an
        # update entity with no latest version shows as unavailable instead.
        latest = installed if not status.get("upgrade_available") else None

    payload: dict[str, Any] = {
        "installed_version": installed,
        "latest_version": latest,
        "in_progress": bool(status.get("upgrade_in_progress")),
    }
    percent = _int(status.get("upgrade_percent"))
    if percent is not None and payload["in_progress"]:
        payload["update_percentage"] = percent
    if isinstance(latest_info, dict) and latest_info.get("desc"):
        payload["release_summary"] = str(latest_info["desc"])[:255]
    return payload
