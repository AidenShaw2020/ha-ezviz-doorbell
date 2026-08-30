"""Entity definitions and the MQTT discovery payloads they turn into.

Every read-only entity reads the same retained JSON status topic through a
value template, so one status publish updates the whole device at once and the
broker holds a single retained copy of it. Controllable entities additionally
get a command topic, which :mod:`commands` dispatches on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from const import (
    ALARM_SOUND_MODES,
    AVAILABILITY_TOPIC,
    DETECTION_TYPES,
    DIAGNOSTIC_SWITCHES,
    DISCOVERY_PREFIX,
    DISPLAY_MODES,
    EVENT_TYPES,
    NIGHT_VISION_MODES,
    SWITCH_ICONS,
    SWITCH_NAMES,
    WORK_MODES,
    command_topic,
    event_topic,
    image_topic,
    status_topic,
    trigger_topic,
    update_topic,
)


@dataclass(frozen=True)
class Entity:
    """One Home Assistant entity, and how its state and commands are wired."""

    component: str
    key: str
    name: str
    # Jinja expression evaluated against the status topic. Left out for
    # entities that carry their own topic or hold no state at all.
    template: str | None = None
    # Command handler name, see commands.HANDLERS. Its presence is what adds a
    # command topic to the discovery payload.
    command: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


def _value(expr: str) -> str:
    """Render a status field, leaving the state untouched when it is missing.

    An empty string tells the MQTT integration to ignore the update, which is
    what should happen for a field this particular device never reports. It has
    to be spelled out rather than left to Jinja's default filter, which would
    also blank a legitimate zero.
    """
    return (
        "{{ value_json." + expr + " if value_json." + expr
        + " is not none else '' }}"
    )


def _bool(expr: str) -> str:
    """Render a status field as ON/OFF."""
    return "{{ 'ON' if value_json." + expr + " else 'OFF' }}"


# ---------------------------------------------------------------------------
# Static entities: the same set for every device
# ---------------------------------------------------------------------------

EVENT_ENTITIES: list[Entity] = [
    Entity(
        "event",
        "alerts",
        "Alerts",
        config={
            "device_class": "doorbell",
            "event_types": EVENT_TYPES,
            "icon": "mdi:bell-alert",
        },
    ),
    Entity(
        "event",
        "doorbell",
        "Doorbell",
        config={
            "device_class": "doorbell",
            "event_types": ["ring"],
            "icon": "mdi:doorbell",
        },
    ),
    Entity(
        "event",
        "motion",
        "Motion",
        config={
            "device_class": "motion",
            "event_types": ["motion"],
            "icon": "mdi:motion-sensor",
        },
    ),
]

# Momentary binary sensors, pulsed ON and switched back off by Home Assistant
# after off_delay. They exist because most dashboard cards and many automation
# blueprints still expect a binary sensor rather than an event entity.
TRIGGER_ENTITIES: list[Entity] = [
    Entity(
        "binary_sensor",
        "ring",
        "Doorbell button",
        config={"icon": "mdi:doorbell", "off_delay": 5},
    ),
    Entity(
        "binary_sensor",
        "motion_detected",
        "Motion detected",
        config={"device_class": "motion", "off_delay": 30},
    ),
]

STATUS_ENTITIES: list[Entity] = [
    # --- binary sensors -------------------------------------------------
    Entity(
        "binary_sensor",
        "online",
        "Online",
        template=_bool("online"),
        config={"device_class": "connectivity", "entity_category": "diagnostic"},
    ),
    Entity(
        "binary_sensor",
        "encrypted",
        "Video encryption",
        template=_bool("encrypted"),
        config={"icon": "mdi:lock", "entity_category": "diagnostic"},
    ),
    Entity(
        "binary_sensor",
        "alarm_schedules_enabled",
        "Alarm schedule",
        template=_bool("alarm_schedules_enabled"),
        config={"icon": "mdi:calendar-clock", "entity_category": "diagnostic"},
    ),
    # --- sensors --------------------------------------------------------
    Entity(
        "sensor",
        "battery_level",
        "Battery",
        template=_value("battery_level"),
        config={
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
        },
    ),
    Entity(
        "sensor",
        "last_event",
        "Last event",
        template=_value("last_event"),
        config={"icon": "mdi:bell-outline"},
    ),
    Entity(
        "sensor",
        "last_ring",
        "Last ring",
        template=_value("last_ring"),
        config={"device_class": "timestamp", "icon": "mdi:doorbell"},
    ),
    Entity(
        "sensor",
        "last_motion",
        "Last motion",
        template=_value("last_motion"),
        config={"device_class": "timestamp", "icon": "mdi:motion-sensor"},
    ),
    Entity(
        "sensor",
        "last_alarm_type_name",
        "Last alarm type",
        template=_value("last_alarm_type_name"),
        config={"icon": "mdi:shield-alert", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "last_alarm_time",
        "Last alarm time",
        template=_value("last_alarm_time"),
        config={"icon": "mdi:clock-outline", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "seconds_last_trigger",
        "Seconds since last trigger",
        template=_value("seconds_last_trigger"),
        config={
            "device_class": "duration",
            "unit_of_measurement": "s",
            "state_class": "measurement",
            "entity_category": "diagnostic",
        },
    ),
    Entity(
        "sensor",
        "wifi_signal",
        "Wi-Fi signal",
        template=_value("wifi_signal"),
        config={
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "icon": "mdi:wifi",
            "entity_category": "diagnostic",
        },
    ),
    Entity(
        "sensor",
        "wifi_ssid",
        "Wi-Fi network",
        template=_value("wifi_ssid"),
        config={"icon": "mdi:wifi-settings", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "local_ip",
        "Local IP",
        template=_value("local_ip"),
        config={"icon": "mdi:ip-network", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "wan_ip",
        "WAN IP",
        template=_value("wan_ip"),
        config={"icon": "mdi:ip-network-outline", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "version",
        "Firmware",
        template=_value("version"),
        config={"icon": "mdi:chip", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "pir_status",
        "PIR status",
        template=_value("pir_status"),
        config={"icon": "mdi:motion-sensor", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "disk_capacity_mb",
        "Storage capacity",
        template=_value("disk_capacity_mb"),
        config={
            "device_class": "data_size",
            "unit_of_measurement": "MB",
            "icon": "mdi:sd",
            "entity_category": "diagnostic",
        },
    ),
    Entity(
        "sensor",
        "live_stream_url",
        "Live stream URL",
        template=_value("live_stream_url"),
        config={"icon": "mdi:video-wireless", "entity_category": "diagnostic"},
    ),
    Entity(
        "sensor",
        "snapshot_url",
        "Snapshot URL",
        template=_value("snapshot_url"),
        config={"icon": "mdi:camera-iris", "entity_category": "diagnostic"},
    ),
    # --- controls -------------------------------------------------------
    Entity(
        "switch",
        "motion_detection",
        "Motion detection",
        template=_bool("alarm_notify"),
        command="motion_detection",
        config={"icon": "mdi:motion-sensor", "entity_category": "config"},
    ),
    Entity(
        "switch",
        "notify_alarm",
        "Alarm notifications",
        template=_bool("push_notify_alarm"),
        command="notify_alarm",
        config={"icon": "mdi:bell-ring", "entity_category": "config"},
    ),
    Entity(
        "switch",
        "notify_call",
        "Doorbell notifications",
        template=_bool("push_notify_call"),
        command="notify_call",
        config={"icon": "mdi:bell-badge", "entity_category": "config"},
    ),
    Entity(
        "number",
        "detection_sensitivity",
        "Detection sensitivity",
        template=_value("detection_sensitivity"),
        command="detection_sensitivity",
        config={
            "min": 1,
            "max": 6,
            "step": 1,
            "mode": "slider",
            "icon": "mdi:tune",
            "entity_category": "config",
        },
    ),
    Entity(
        "select",
        "work_mode",
        "Work mode",
        template=_value("work_mode"),
        command="work_mode",
        config={
            "options": list(WORK_MODES),
            "icon": "mdi:battery-clock",
            "entity_category": "config",
        },
    ),
    Entity(
        "select",
        "night_vision_mode",
        "Night vision",
        template=_value("night_vision_mode"),
        command="night_vision_mode",
        config={
            "options": list(NIGHT_VISION_MODES),
            "icon": "mdi:weather-night",
            "entity_category": "config",
        },
    ),
    Entity(
        "select",
        "display_mode",
        "Image style",
        template=_value("display_mode"),
        command="display_mode",
        config={
            "options": list(DISPLAY_MODES),
            "icon": "mdi:image-filter-vintage",
            "entity_category": "config",
        },
    ),
    Entity(
        "select",
        "alarm_sound_mode",
        "Alarm sound",
        template=_value("alarm_sound_mode"),
        command="alarm_sound_mode",
        config={
            "options": list(ALARM_SOUND_MODES),
            "icon": "mdi:music-note",
            "entity_category": "config",
        },
    ),
    Entity(
        "select",
        "detection_type",
        "Detection type",
        template=_value("detection_type"),
        command="detection_type",
        config={
            "options": list(DETECTION_TYPES),
            "icon": "mdi:human-greeting",
            "entity_category": "config",
        },
    ),
    Entity(
        "button",
        "wake",
        "Wake camera",
        command="wake",
        config={"icon": "mdi:sleep-off"},
    ),
    Entity(
        "button",
        "snapshot",
        "Take snapshot",
        command="snapshot",
        config={"icon": "mdi:camera"},
    ),
    Entity(
        "button",
        "refresh",
        "Refresh status",
        command="refresh",
        config={"icon": "mdi:refresh", "entity_category": "diagnostic"},
    ),
    Entity(
        "button",
        "reboot",
        "Reboot",
        command="reboot",
        config={"device_class": "restart", "entity_category": "config"},
    ),
    Entity(
        "siren",
        "siren",
        "Siren",
        command="siren",
        config={"icon": "mdi:alarm-light", "optimistic": True},
    ),
    Entity(
        "image",
        "snapshot_image",
        "Last snapshot",
        config={"image_encoding": "b64", "content_type": "image/jpeg"},
    ),
    Entity(
        "update",
        "firmware",
        "Firmware",
        command="firmware",
        config={"device_class": "firmware", "entity_category": "config"},
    ),
]

TRIGGER_KEYS = frozenset(entity.key for entity in TRIGGER_ENTITIES)


def device_block(serial: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the MQTT discovery device block for one camera."""
    status = status or {}
    device: dict[str, Any] = {
        "identifiers": [f"ezviz_push_{serial}"],
        "name": status.get("name") or serial,
        "manufacturer": "EZVIZ",
        "model": status.get("model") or "EZVIZ camera (push)",
        "serial_number": serial,
    }
    if status.get("version"):
        device["sw_version"] = status["version"]
    return device


def switch_entities(switches: dict[str, Any]) -> list[Entity]:
    """Return a switch entity for every switch type the device reports."""
    entities: list[Entity] = []
    for name in sorted(switches, key=lambda item: int(item[1:])):
        number = int(name[1:])
        config: dict[str, Any] = {
            "entity_category": "diagnostic"
            if number in DIAGNOSTIC_SWITCHES
            else "config"
        }
        if icon := SWITCH_ICONS.get(number):
            config["icon"] = icon
        entities.append(
            Entity(
                "switch",
                f"switch_{number}",
                SWITCH_NAMES.get(number, f"Switch {number}"),
                template=_bool(f"switches.{name}"),
                command=f"switch_{number}",
                config=config,
            )
        )
    return entities


def _state_topic(serial: str, entity: Entity) -> str | None:
    """Return the topic an entity reads its state from, if it has one."""
    if entity.component == "event":
        return event_topic(serial, entity.key)
    if entity.component == "update":
        return update_topic(serial)
    if entity.component == "image":
        return None
    if entity.key in TRIGGER_KEYS:
        return trigger_topic(serial, entity.key)
    return status_topic(serial) if entity.template else None


def discovery_message(
    serial: str, entity: Entity, device: dict[str, Any]
) -> tuple[str, str]:
    """Return the (topic, payload) discovery message for one entity."""
    config: dict[str, Any] = {
        "name": entity.name,
        "unique_id": f"ezviz_push_{serial}_{entity.key}",
        "availability_topic": AVAILABILITY_TOPIC,
        "device": device,
    }

    if entity.component == "image":
        config["image_topic"] = image_topic(serial)
    elif topic := _state_topic(serial, entity):
        config["state_topic"] = topic
        if entity.template:
            config["value_template"] = entity.template

    if entity.command:
        config["command_topic"] = command_topic(serial, entity.command)

    config.update(entity.config)

    topic = (
        f"{DISCOVERY_PREFIX}/{entity.component}/ezviz_push_{serial}"
        f"/{entity.key}/config"
    )
    return topic, json.dumps(config)


def all_entities(status: dict[str, Any] | None = None) -> list[Entity]:
    """Return every entity for a device, given what its status reports."""
    entities = [*EVENT_ENTITIES, *TRIGGER_ENTITIES, *STATUS_ENTITIES]
    if status:
        entities += switch_entities(status.get("switches") or {})
    return entities
