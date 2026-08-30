"""Small readers for the shapes the EZVIZ cloud reports values in.

``EzvizCamera.status()`` hands back the cloud's own structure: enums as bare
numbers, several optionals as a JSON document inside a string, and figures that
may arrive as a number, a string, or a list depending on the model. Every
entity that reads one of those goes through a function here rather than
repeating the guesswork.
"""

from __future__ import annotations

import json
from typing import Any


def option_key(options: dict[str, int], value: Any) -> str | None:
    """Return the select option whose EZVIZ value this is."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    for key, candidate in options.items():
        if candidate == number:
            return key
    return None


def nested(value: Any, key: str) -> Any:
    """Return ``key`` from a value that may be a dict or a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return value.get(key)
    return None


def as_int(value: Any) -> int | None:
    """Return value as an int, or None when it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def disk_capacity(value: Any) -> int | None:
    """Return the card size in MB from any shape EZVIZ reports it in.

    It arrives as a bare number, as a comma separated string of one figure per
    card, or as that string already split into a list.
    """
    if isinstance(value, str):
        value = value.split(",")
    if isinstance(value, list):
        value = value[0] if value else None
    return as_int(value)


def optionals(status: dict[str, Any]) -> dict[str, Any]:
    """Return the device's STATUS.optionals block, whatever is there."""
    value = status.get("optionals")
    return value if isinstance(value, dict) else {}


def wifi(status: dict[str, Any]) -> dict[str, Any]:
    """Return the device's WIFI block, whatever is there."""
    value = status.get("WIFI")
    return value if isinstance(value, dict) else {}


def night_vision_mode(status: dict[str, Any]) -> Any:
    """Return the night vision mode number, however it is wrapped."""
    value = optionals(status).get("NightVision_Model")
    if isinstance(value, int):
        return value
    return nested(value, "graphicType")


def display_mode(status: dict[str, Any]) -> Any:
    """Return the image style number, however it is wrapped."""
    value = optionals(status).get("display_mode")
    if isinstance(value, int):
        return value
    return nested(value, "mode")


def first_image_url(value: Any) -> str | None:
    """Return the first HTTP(S) image URL anywhere in an EZVIZ response.

    Which key holds the picture depends on the endpoint and the model, and
    several of them pack more than one URL into a semicolon separated string.
    """
    if isinstance(value, str):
        for part in value.split(";"):
            text = part.strip()
            if text.startswith(("http://", "https://")):
                return text
        return None
    if isinstance(value, dict):
        candidates: Any = value.values()
    elif isinstance(value, list):
        candidates = value
    else:
        return None
    for item in candidates:
        if found := first_image_url(item):
            return found
    return None
