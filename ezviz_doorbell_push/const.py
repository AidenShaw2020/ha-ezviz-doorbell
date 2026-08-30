"""Topics, event types and EZVIZ code tables shared across the add-on."""

from __future__ import annotations

import re
from typing import Final

DISCOVERY_PREFIX: Final = "homeassistant"
BASE_TOPIC: Final = "ezviz_push"
AVAILABILITY_TOPIC: Final = f"{BASE_TOPIC}/status"

EVENT_RING: Final = "ring"
EVENT_MOTION: Final = "motion"
EVENT_ALARM: Final = "alarm"
EVENT_TYPES: Final = [EVENT_RING, EVENT_MOTION, EVENT_ALARM]

# Push and polled messages use two different code spaces, so they get two
# different maps. Anything unmapped is reported as a generic alarm with the raw
# code attached, so a new code can be identified from the event attributes.

# Push messages carry "alert_type_code": 0 is the doorbell button, 10000 the PIR.
PUSH_ALERT_TYPES: Final[dict[int, str]] = {
    0: EVENT_RING,
    10000: EVENT_MOTION,
    # "AI Human Detection" - what an EP8x actually reports for motion. The
    # descriptive title stays in the "alert" attribute.
    10120: EVENT_MOTION,
}

# Polled messages carry "subType". A ring is 2701 - a *call*, not an alarm,
# which is why it never appears on the alarm push channel.
POLL_SUBTYPES: Final[dict[int, str]] = {
    2701: EVENT_RING,
}

# Last line of defence when neither code space recognises the message: EZVIZ
# always sends a human readable title, and it says plainly which of the two
# happened. Whole words only - "ring" as a bare substring also appears in
# "monitoring" and "triggering", which would turn motion into a doorbell press.
RING_PATTERN: Final = re.compile(
    r"\b("
    r"doorbell|door\s?bell|ring\w*|call\w*|visitor|at the door|"
    r"zvon\w*|dve[rř]\w*|"  # cs
    r"klingel\w*|t[uü]rglocke|"  # de
    r"sonnette|"  # fr
    r"timbre"  # es
    r")\b",
    re.IGNORECASE,
)
MOTION_PATTERN: Final = re.compile(
    r"\b("
    r"motion|movement|moving|human|person|people|vehicle|detect\w*|"
    r"pohyb\w*|osob\w*|"  # cs
    r"bewegung\w*|"  # de
    r"mouvement|"  # fr
    r"movimiento"  # es
    r")\b",
    re.IGNORECASE,
)

# Switch types a device may report, named the way the EZVIZ app names them.
# Only the ones a given device actually reports get an entity, so this table
# can stay long without cluttering anyone's device page.
SWITCH_NAMES: Final[dict[int, str]] = {
    1: "Alarm tone",
    2: "Adaptive stream",
    3: "Status light",
    4: "Intelligent analysis",
    5: "Log upload",
    6: "Defence plan",
    7: "Privacy mode",
    8: "Sound localization",
    9: "Cruise",
    10: "Infrared light",
    11: "Wi-Fi",
    12: "Wi-Fi marketing",
    13: "Wi-Fi light",
    14: "Plug",
    21: "Sleep",
    22: "Audio",
    23: "Baby care",
    24: "Logo",
    25: "Motion tracking",
    26: "Channel offline alert",
    29: "All day recording",
    32: "Auto sleep",
    34: "Roaming status",
    35: "Mobile data",
    37: "Alarm reminder",
    39: "Outdoor ringing sound",
    40: "Intelligent picture quality",
    41: "Light on human detection",
    101: "Two way talk",
    200: "Human detection",
    301: "Flashing light",
    303: "Alarm light",
    305: "Alarm light linkage",
    306: "Tamper alarm",
    451: "Detection type",
    600: "Outlet recovery",
    604: "Wide dynamic range",
    611: "Chime indicator light",
    617: "Distortion correction",
    650: "Tracking",
    651: "Cruise tracking",
    700: "Partial image optimization",
    701: "Feature tracking",
    702: "Logo watermark",
}

# Icons for the switches worth recognising on sight in a long list.
SWITCH_ICONS: Final[dict[int, str]] = {
    3: "mdi:led-on",
    7: "mdi:eye-off",
    10: "mdi:led-outline",
    21: "mdi:sleep",
    22: "mdi:volume-high",
    29: "mdi:record-rec",
    32: "mdi:sleep",
    101: "mdi:account-voice",
    200: "mdi:human",
    303: "mdi:spotlight-beam",
    306: "mdi:shield-alert",
}

# Switches that describe the device rather than the picture it takes: shown
# under diagnostics so the main card stays about the doorbell.
DIAGNOSTIC_SWITCHES: Final[frozenset[int]] = frozenset({5, 11, 12, 24, 26, 34, 35, 702})

# Selects. The value sent to EZVIZ is the enum number; the option shown in
# Home Assistant is the label.
WORK_MODES: Final[dict[str, int]] = {
    "Power save": 0,
    "High performance": 1,
    "Plugged in": 2,
    "Super power save": 3,
    "Custom": 4,
    "Always on": 7,
}
NIGHT_VISION_MODES: Final[dict[str, int]] = {
    "Black and white": 0,
    "Colour": 1,
    "Smart": 2,
}
DISPLAY_MODES: Final[dict[str, int]] = {
    "Original": 1,
    "Soft": 2,
    "Vivid": 3,
}
ALARM_SOUND_MODES: Final[dict[str, int]] = {
    "Soft": 0,
    "Intense": 1,
    "Silent": 2,
}
DETECTION_TYPES: Final[dict[str, int]] = {
    "Human shape": 1,
    "Image change": 3,
    "PIR": 5,
}


def status_topic(serial: str) -> str:
    """Topic carrying the full device status as JSON."""
    return f"{BASE_TOPIC}/{serial}/status"


def update_topic(serial: str) -> str:
    """Topic carrying the firmware update entity's JSON state."""
    return f"{BASE_TOPIC}/{serial}/update"


def image_topic(serial: str) -> str:
    """Topic carrying the latest snapshot as base64 JPEG."""
    return f"{BASE_TOPIC}/{serial}/image"


def event_topic(serial: str, key: str) -> str:
    """Topic carrying one event entity's payload."""
    return f"{BASE_TOPIC}/{serial}/event" if key == "alerts" else (
        f"{BASE_TOPIC}/{serial}/event/{key}"
    )


def trigger_topic(serial: str, key: str) -> str:
    """Topic a momentary binary sensor is pulsed on."""
    return f"{BASE_TOPIC}/{serial}/trigger/{key}"


def command_topic(serial: str, key: str) -> str:
    """Topic Home Assistant sends commands for one entity on."""
    return f"{BASE_TOPIC}/{serial}/cmd/{key}"


COMMAND_SUBSCRIPTION: Final = f"{BASE_TOPIC}/+/cmd/+"
