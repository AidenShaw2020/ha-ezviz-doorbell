"""Constants and EZVIZ code tables for the EZVIZ Doorbell integration."""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "ezviz_doorbell"
MANUFACTURER: Final = "EZVIZ"

DEFAULT_REGION: Final = "apiieu.ezvizlife.com"

CONF_REGION: Final = "region"
CONF_MFA_CODE: Final = "mfa_code"
CONF_TOKEN: Final = "token"
CONF_STREAM_TOKEN: Final = "stream_token"

CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_STATUS_INTERVAL: Final = "status_interval"
CONF_SNAPSHOT_INTERVAL: Final = "snapshot_interval"
CONF_LIVE_STREAM: Final = "live_stream"
CONF_RING_CODES: Final = "ring_codes"
CONF_MOTION_CODES: Final = "motion_codes"
CONF_DEVICES: Final = "devices"
CONF_VERIFICATION_CODES: Final = "verification_codes"

DEFAULT_POLL_INTERVAL: Final = 5
DEFAULT_STATUS_INTERVAL: Final = 60
DEFAULT_SNAPSHOT_INTERVAL: Final = 3

# An encrypted cloud stream cannot be decrypted as it arrives, only in one
# piece afterwards, so for those devices a "stream" is really a short clip.
ENCRYPTED_CLIP_SECONDS: Final = 15.0

EVENT_RING: Final = "ring"
EVENT_MOTION: Final = "motion"
EVENT_ALARM: Final = "alarm"
EVENT_TYPES: Final = [EVENT_RING, EVENT_MOTION, EVENT_ALARM]

# After any push, poll hard for a short while. Someone reaching the button has
# almost always tripped motion first, and that push arrives instantly - so it
# is a reliable early warning that a ring may be seconds away.
BURST_SECONDS: Final = 30
BURST_INTERVAL: Final = 1.0

# Push and polled messages use two different code spaces, so they get two
# different maps. Anything unmapped is reported as a generic alarm with the raw
# code attached, so a new code can be identified from the event attributes.

# Push messages carry "alert_type_code": 0 is the doorbell button, 10000 the PIR.
PUSH_ALERT_TYPES: Final[dict[int, str]] = {
    0: EVENT_RING,
    10000: EVENT_MOTION,
    # "AI Human Detection" - what an EP8x actually reports for motion.
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

# Switches that describe the device rather than the picture it takes.
DIAGNOSTIC_SWITCHES: Final[frozenset[int]] = frozenset({5, 11, 12, 24, 26, 34, 35, 702})

# Selects. The value sent to EZVIZ is the enum number; the option shown in
# Home Assistant is the label, which is also its translation key.
WORK_MODES: Final[dict[str, int]] = {
    "power_save": 0,
    "high_performance": 1,
    "plugged_in": 2,
    "super_power_save": 3,
    "custom": 4,
    "always_on": 7,
}
NIGHT_VISION_MODES: Final[dict[str, int]] = {
    "black_and_white": 0,
    "colour": 1,
    "smart": 2,
}
DISPLAY_MODES: Final[dict[str, int]] = {
    "original": 1,
    "soft": 2,
    "vivid": 3,
}
ALARM_SOUND_MODES: Final[dict[str, int]] = {
    "soft": 0,
    "intense": 1,
    "silent": 2,
}
DETECTION_TYPES: Final[dict[str, int]] = {
    "human_shape": 1,
    "image_change": 3,
    "pir": 5,
}

# EZVIZ reports the alarm sound as a SoundMode *name* but takes a number when
# it is set, so the name has to be translated back to the option key.
SOUND_MODE_KEYS: Final[dict[str, str]] = {
    key.upper(): key for key in ALARM_SOUND_MODES
}


def signal_event(entry_id: str) -> str:
    """Return the dispatcher signal one config entry's events travel on."""
    return f"{DOMAIN}_{entry_id}_event"
