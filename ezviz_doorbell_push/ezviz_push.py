"""EZVIZ push -> MQTT bridge.

The Home Assistant EZVIZ integration polls the cloud every 30 seconds and has
no event platform, so a doorbell button press never reaches Home Assistant at
all: the ring travels over the EZVIZ push channel, which the polled alarm feed
never shows.

This add-on keeps that push channel open and republishes everything it hears
onto the local MQTT broker using MQTT discovery, so Home Assistant creates the
entities on its own. It runs beside the built-in integration and changes
nothing about it.

Alongside the events it mirrors the device itself - battery, switches, work
mode, firmware and the rest - as the entities the built-in integration would
create, and it serves live video over HTTP for doorbells that have no RTSP
server of their own.

Alarm snapshots are AES encrypted by EZVIZ (the files start with the magic
bytes ``hikencodepicture``). Given the device verification code, this add-on
decrypts them and publishes the plain JPEG, which means image encryption can
stay switched on in the EZVIZ app.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import signal
import socket
import sys
import threading
import time
from typing import Any

import hashlib
import paho.mqtt.client as mqtt
from pyezvizapi import client as ezviz_client_module
from pyezvizapi import constants as ezviz_constants
from pyezvizapi import mqtt as ezviz_mqtt_module
from pyezvizapi.camera import EzvizCamera
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError
from pyezvizapi.utils import decrypt_image
import requests

import commands
from const import (
    AVAILABILITY_TOPIC,
    COMMAND_SUBSCRIPTION,
    EVENT_ALARM,
    EVENT_MOTION,
    EVENT_RING,
    MOTION_PATTERN,
    POLL_SUBTYPES,
    PUSH_ALERT_TYPES,
    RING_PATTERN,
    event_topic,
    image_topic,
    status_topic,
    trigger_topic,
    update_topic,
)
from devicestate import build_status, firmware_payload
import entities
import liveview

OPTIONS_PATH = Path("/data/options.json")
TOKEN_PATH = Path("/data/token.json")
FEATURE_CODE_PATH = Path("/data/feature_code")
LIVE_TOKEN_PATH = Path("/data/live_token")
SUPERVISOR_URL = "http://supervisor"

# After any push, poll hard for a short while. Someone reaching the button has
# almost always tripped motion first, and that push arrives instantly - so it
# is a reliable early warning that a ring may be seconds away.
BURST_SECONDS = 30
BURST_INTERVAL = 1.0
POLL_TICK = 0.25

_LOGGER = logging.getLogger("ezviz_push")


def pin_feature_code() -> None:
    """Give this add-on a stable EZVIZ device identity.

    pyezvizapi derives its ``featureCode`` from the host MAC address, and a
    container gets a fresh MAC whenever it is recreated - which happens on
    every add-on update. EZVIZ ties the saved session to that code, so without
    pinning it the stored token is rejected and two factor authentication is
    demanded all over again. Generate the code once and keep it in /data.
    """
    try:
        if FEATURE_CODE_PATH.exists():
            code = FEATURE_CODE_PATH.read_text(encoding="utf-8").strip()
        else:
            code = hashlib.md5(os.urandom(16)).hexdigest()
            FEATURE_CODE_PATH.write_text(code, encoding="utf-8")
    except OSError as err:
        _LOGGER.warning(
            "Could not persist a stable feature code (%s); two factor auth may"
            " be requested again after an update",
            err,
        )
        return

    # client.py and mqtt.py import the constant by value, so each module needs
    # patching, as does the shared request header built from it at import time.
    for module in (ezviz_constants, ezviz_client_module, ezviz_mqtt_module):
        if hasattr(module, "FEATURE_CODE"):
            module.FEATURE_CODE = code
    header = getattr(ezviz_constants, "REQUEST_HEADER", None)
    if isinstance(header, dict):
        header["featureCode"] = code


def load_options() -> dict[str, Any]:
    """Read the add-on options written by the Supervisor."""
    if not OPTIONS_PATH.exists():
        _LOGGER.error("Missing %s - is this running as a Home Assistant add-on?",
                      OPTIONS_PATH)
        sys.exit(1)
    return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))


def discover_mqtt(options: dict[str, Any]) -> dict[str, Any]:
    """Return local broker settings, preferring explicit options.

    The Supervisor hands out the configured broker over its services API, so a
    normal install needs no MQTT settings at all.
    """
    if options.get("mqtt_host"):
        return {
            "host": options["mqtt_host"],
            "port": int(options.get("mqtt_port") or 1883),
            "username": options.get("mqtt_username") or None,
            "password": options.get("mqtt_password") or None,
        }

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        _LOGGER.error(
            "No mqtt_host configured and no Supervisor token available."
        )
        sys.exit(1)

    resp = requests.get(
        f"{SUPERVISOR_URL}/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    _LOGGER.info("Using the MQTT broker provided by the Supervisor")
    return {
        "host": data["host"],
        "port": int(data["port"]),
        "username": data.get("username") or None,
        "password": data.get("password") or None,
    }


def _now() -> str:
    """Return the current time as an ISO 8601 timestamp entities accept."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _codes(values: Any) -> set[int]:
    """Return a set of integer alert codes from an option list."""
    codes: set[int] = set()
    for value in values or []:
        try:
            codes.add(int(value))
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring non-numeric alert code %r in the options", value)
    return codes


class EzvizPushBridge:
    """Bridge EZVIZ cloud push messages onto the local MQTT broker."""

    def __init__(self, options: dict[str, Any]) -> None:
        """Initialize the bridge."""
        self._options = options
        self._region = options.get("ezviz_region") or "apiieu.ezvizlife.com"
        self._serial_filter = {s for s in options.get("serials") or [] if s}
        self._verification_codes = {
            item["serial"]: item["code"]
            for item in options.get("verification_codes") or []
            if item.get("serial") and item.get("code")
        }
        self._names: dict[str, str] = {}
        self._announced: set[str] = set()
        self._announced_full: set[str] = set()
        self._announced_signature: dict[str, tuple[str, tuple[str, ...]]] = {}
        self._mqtt: mqtt.Client | None = None
        self._client: EzvizClient | None = None
        self._stop = threading.Event()
        self._seen_messages: set[str] = set()
        self._poll_primed = False
        self._recent: dict[str, float] = {}
        self._poll_now = threading.Event()
        self._status_now = threading.Event()
        self._burst_until = 0.0
        # Wide enough to cover a poll arriving after a push for the same event,
        # but no wider, so two genuine presses are not merged into one.
        self._dedupe_window = max(15, int(options.get("poll_interval") or 0) + 10)

        # Extra codes the user has identified for their own model, on top of
        # the ones this add-on already knows.
        self._extra_ring = _codes(options.get("ring_codes"))
        self._extra_motion = _codes(options.get("motion_codes"))

        # Everything the status entities read, kept per device so a command or
        # an event can republish without refetching the whole device.
        self._status: dict[str, dict[str, Any]] = {}
        self._event_state: dict[str, dict[str, Any]] = {}
        self._snapshots: dict[str, bytes] = {}
        self._sensitivity_broken: set[str] = set()

        # EZVIZ calls are made from the poll thread, the command worker and the
        # live view server, so they take turns.
        self._api_lock = threading.RLock()
        self._commands: queue.Queue[tuple[str, str, str]] = queue.Queue()

        self._liveview: liveview.LiveViewServer | None = None

    # ------------------------------------------------------------------
    # Accessors used by commands and the live view server
    # ------------------------------------------------------------------

    @property
    def client(self) -> EzvizClient:
        """Return the logged in EZVIZ client.

        Raises:
            PyEzvizError: If the bridge is not connected to EZVIZ yet.
        """
        if self._client is None:
            raise PyEzvizError("Not connected to EZVIZ yet")
        return self._client

    def serials(self) -> list[str]:
        """Return every serial this add-on handles."""
        if self._serial_filter:
            return sorted(self._serial_filter)
        return sorted(self._status or self._names)

    def device_name(self, serial: str) -> str:
        """Return the device's name, falling back to its serial."""
        return self._names.get(serial, serial)

    def device_status(self, serial: str) -> dict[str, Any]:
        """Return the last published status for a device."""
        return self._status.get(serial, {})

    def last_snapshot(self, serial: str) -> bytes | None:
        """Return the last snapshot published for a device."""
        return self._snapshots.get(serial)

    def request_status_refresh(self) -> None:
        """Ask the status poller to fetch again straight away."""
        self._status_now.set()

    def keep_awake(self, serial: str) -> None:
        """Ask the cloud to keep a battery camera awake a while longer."""
        try:
            with self._api_lock:
                self.client.delay_battery_device_sleep(serial, 1, 1)
        except (PyEzvizError, OSError, KeyError) as err:
            _LOGGER.debug("Could not delay sleep for %s: %s", serial, err)

    # ------------------------------------------------------------------
    # Local MQTT
    # ------------------------------------------------------------------

    def connect_mqtt(self) -> None:
        """Connect to the local broker and publish an online birth message."""
        settings = discover_mqtt(self._options)

        # paho-mqtt 2.x demands an explicit callback API version, 1.x does not
        # accept the argument at all.
        callback_api_version = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_api_version is not None:
            client = mqtt.Client(
                callback_api_version.VERSION2, client_id="ezviz_push_addon"
            )
        else:
            client = mqtt.Client(client_id="ezviz_push_addon")

        if settings["username"]:
            client.username_pw_set(settings["username"], settings["password"])
        client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        client.on_message = self._on_mqtt_message
        client.connect(settings["host"], settings["port"], keepalive=60)
        client.loop_start()
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        client.subscribe(COMMAND_SUBSCRIPTION, qos=1)
        self._mqtt = client
        _LOGGER.info("Connected to MQTT broker at %s:%s",
                     settings["host"], settings["port"])

    def _publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        """Publish to the local broker."""
        if self._mqtt is None:
            return
        self._mqtt.publish(topic, payload, retain=retain)

    def _on_mqtt_message(self, client: Any, userdata: Any, message: Any) -> None:
        """Queue a command sent by Home Assistant (paho network thread)."""
        parts = message.topic.split("/")
        if len(parts) != 4:
            return
        _, serial, _, key = parts
        payload = message.payload.decode("utf-8", "replace").strip()
        _LOGGER.info("Command for %s: %s = %s", serial, key, payload)
        self._commands.put((serial, key, payload))

    def _command_worker(self) -> None:
        """Run queued commands one at a time, off the MQTT network thread."""
        while not self._stop.is_set():
            try:
                serial, key, payload = self._commands.get(timeout=1)
            except queue.Empty:
                continue

            try:
                with self._api_lock:
                    commands.dispatch(self, serial, key, payload)
            except (PyEzvizError, OSError, ValueError, KeyError) as err:
                _LOGGER.error("Command %s for %s failed: %s", key, serial, err)
            else:
                _LOGGER.info("Command %s for %s accepted", key, serial)
                # The cloud needs a moment before it reports the new value.
                self._stop.wait(2)
                self.request_status_refresh()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def announce(self, serial: str) -> None:
        """Publish discovery for the entities that need no device status.

        Events must exist from the first second, well before the first status
        poll has returned, or the very first ring has nowhere to land.
        """
        if serial in self._announced:
            return

        device = entities.device_block(serial, self._status.get(serial))
        for entity in (*entities.EVENT_ENTITIES, *entities.TRIGGER_ENTITIES):
            topic, payload = entities.discovery_message(serial, entity, device)
            self._publish(topic, payload, retain=True)

        self._announced.add(serial)
        _LOGGER.info("Announced %s (%s) to Home Assistant",
                     self.device_name(serial), serial)

    def announce_full(self, serial: str) -> None:
        """Publish discovery for every entity, once the status is known.

        The status is refreshed on a timer, but discovery is not: republishing
        forty retained messages a minute would be pure churn. It goes out again
        only when there is something new to say - a renamed device, a firmware
        upgrade, or a switch the device did not report before.
        """
        status = self._status.get(serial) or {}
        device = entities.device_block(serial, status)
        all_entities = entities.all_entities(status)

        signature = (
            json.dumps(device, sort_keys=True),
            tuple(entity.key for entity in all_entities),
        )
        if self._announced_signature.get(serial) == signature:
            return

        for entity in all_entities:
            topic, payload = entities.discovery_message(serial, entity, device)
            self._publish(topic, payload, retain=True)

        if serial not in self._announced_full:
            switches = len(entities.switch_entities(status.get("switches") or {}))
            _LOGGER.info(
                "Announced the full entity set for %s (%s), including %d device"
                " switch(es)",
                self.device_name(serial),
                serial,
                switches,
            )
        else:
            _LOGGER.info("Re-announced %s: its entities or its name changed", serial)

        self._announced.add(serial)
        self._announced_full.add(serial)
        self._announced_signature[serial] = signature

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _decrypt_image(self, serial: str, data: bytes) -> bytes | None:
        """Return a plain JPEG, decrypting an EZVIZ encrypted one if needed."""
        if data[:16] != b"hikencodepicture":
            return data

        code = self._verification_codes.get(serial)
        if not code:
            # No code in the options: the cloud will hand out the camera's key
            # to its own account, which is how the EZVIZ app does it.
            try:
                with self._api_lock:
                    code = self.client.get_cam_key(serial)
            except (PyEzvizError, OSError) as err:
                _LOGGER.warning(
                    "Snapshot for %s is encrypted and its key could not be"
                    " fetched (%s). Add the device verification code under"
                    " verification_codes, or turn off image encryption in the"
                    " EZVIZ app",
                    serial,
                    err,
                )
                return None

        try:
            return decrypt_image(data, code)
        except PyEzvizError as err:
            _LOGGER.warning(
                "Could not decrypt the snapshot for %s (%s) - is the"
                " verification code correct?",
                serial,
                err,
            )
            return None

    def _publish_image(self, serial: str, data: bytes) -> None:
        """Cache and publish a JPEG for the image entity."""
        self._snapshots[serial] = data
        self._publish(
            image_topic(serial),
            base64.b64encode(data).decode("ascii"),
            retain=True,
        )
        _LOGGER.info("Published a %d byte snapshot for %s", len(data), serial)

    def publish_snapshot(self, serial: str, url: str) -> None:
        """Download, decrypt if needed, and publish an alarm snapshot."""
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as err:
            _LOGGER.warning("Could not download snapshot for %s: %s", serial, err)
            return

        data = self._decrypt_image(serial, resp.content)
        if data:
            self._publish_image(serial, data)

    def capture_snapshot(self, serial: str) -> bytes | None:
        """Make the camera take a picture now, publish it and return it.

        This is also what wakes a sleeping battery camera: the cloud has to
        reach the device to get a fresh frame out of it.
        """
        with self._api_lock:
            response = self.client.capture_picture(serial, 1)

        url = _first_image_url(response)
        if not url:
            _LOGGER.warning(
                "Capture for %s returned no image URL: %s", serial, response
            )
            return None

        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as err:
            _LOGGER.warning("Could not download the capture for %s: %s", serial, err)
            return None

        data = self._decrypt_image(serial, resp.content)
        if data:
            self._publish_image(serial, data)
        return data

    # ------------------------------------------------------------------
    # Event classification
    # ------------------------------------------------------------------

    def classify(
        self, code: int, text: str, calling: bool, source: str
    ) -> tuple[str, str]:
        """Return the event type for one message, and how it was decided.

        Push messages and polled messages number their events differently, so
        each is looked up in its own table first. What follows is shared: the
        codes the user has added in the options, then the other table, and
        finally the message text, which says in words what the codes say in
        numbers and is the only signal a brand new model is guaranteed to send.
        """
        if calling:
            return EVENT_RING, "the message is a call"

        primary = PUSH_ALERT_TYPES if source == "push" else POLL_SUBTYPES
        secondary = POLL_SUBTYPES if source == "push" else PUSH_ALERT_TYPES

        if (event_type := primary.get(code)) is not None:
            return event_type, f"{source} code {code}"
        if code in self._extra_ring:
            return EVENT_RING, f"code {code} from the ring_codes option"
        if code in self._extra_motion:
            return EVENT_MOTION, f"code {code} from the motion_codes option"
        if (event_type := secondary.get(code)) is not None:
            return event_type, f"code {code}, known from the other message path"

        if RING_PATTERN.search(text):
            return EVENT_RING, f"the wording of {text!r}"
        if MOTION_PATTERN.search(text):
            return EVENT_MOTION, f"the wording of {text!r}"

        return EVENT_ALARM, f"nothing recognised code {code} or {text!r}"

    # ------------------------------------------------------------------
    # EZVIZ push
    # ------------------------------------------------------------------

    def handle_push(self, msg: dict[str, Any]) -> None:
        """Handle one decoded push message (paho network thread)."""
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}

        serial = ext.get("device_serial")
        raw_code = ext.get("alert_type_code")

        # Logged before any filtering, so "did anything arrive at all?" can
        # always be answered from the default log level.
        _LOGGER.info(
            "Push received: serial=%s alert_type_code=%s alert=%s",
            serial,
            raw_code,
            msg.get("alert"),
        )
        _LOGGER.debug("Full push message: %s", msg)

        # A ring usually comes by polling rather than push. Motion does come
        # over push, and usually just before the button is pressed, so treat
        # any push as a cue to start polling hard.
        self._burst_until = time.monotonic() + BURST_SECONDS
        self._poll_now.set()

        if not serial:
            _LOGGER.info("Ignoring push message: it carries no device serial")
            return
        if self._serial_filter and serial not in self._serial_filter:
            _LOGGER.info(
                "Ignoring push message for %s: not in the configured serials %s",
                serial,
                sorted(self._serial_filter),
            )
            return

        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1

        text = str(msg.get("alert") or "")
        event_type, why = self.classify(code, text, False, "push")

        _LOGGER.info(
            "%s (%s): %s, from %s",
            self.device_name(serial),
            serial,
            event_type,
            why,
        )
        if event_type == EVENT_ALARM:
            _LOGGER.info(
                "Push alert code %s is not mapped yet; add it to the"
                " ring_codes or motion_codes option to classify it.",
                raw_code,
            )

        self.emit_event(
            serial,
            event_type,
            {
                "event_type": event_type,
                "alert_type_code": raw_code,
                "alert": msg.get("alert"),
                "time": ext.get("time"),
                "msg_id": ext.get("msgId"),
                "source": "push",
            },
            ext.get("default_pic_url"),
        )

    def _should_emit(self, serial: str, event_type: str, msg_id: Any) -> bool:
        """Return False when this event already went out by the other path.

        Push and polling overlap: motion shows up on both within a second or
        two. Without this the event entity fires twice for one detection.
        """
        now = time.monotonic()
        self._recent = {
            key: seen
            for key, seen in self._recent.items()
            if now - seen < self._dedupe_window
        }

        keys = [f"{serial}|{event_type}"]
        if msg_id:
            keys.append(f"msg|{msg_id}")

        if any(key in self._recent for key in keys):
            return False

        for key in keys:
            self._recent[key] = now
        return True

    def emit_event(
        self,
        serial: str,
        event_type: str,
        payload: dict[str, Any],
        pic_url: str | None = None,
    ) -> None:
        """Publish one event, unless the other path already delivered it."""
        if not self._should_emit(serial, event_type, payload.get("msg_id")):
            _LOGGER.info(
                "Skipping duplicate %s for %s, already delivered via %s",
                event_type,
                serial,
                "poll" if payload.get("source") == "poll" else "push",
            )
            return

        self.announce(serial)

        # The catch-all entity carries every event; the doorbell and motion
        # entities carry one kind each, so an automation can subscribe to the
        # press alone without filtering on an attribute.
        self._publish(event_topic(serial, "alerts"), json.dumps(payload))
        if event_type == EVENT_RING:
            self._publish(event_topic(serial, "doorbell"), json.dumps(payload))
            self._publish(trigger_topic(serial, "ring"), "ON")
        elif event_type == EVENT_MOTION:
            self._publish(event_topic(serial, "motion"), json.dumps(payload))
            self._publish(trigger_topic(serial, "motion_detected"), "ON")

        state = self._event_state.setdefault(serial, {})
        state["last_event"] = event_type
        state["last_event_time"] = _now()
        if event_type == EVENT_RING:
            state["last_ring"] = state["last_event_time"]
        elif event_type == EVENT_MOTION:
            state["last_motion"] = state["last_event_time"]
        self.publish_status(serial)

        if pic_url:
            self.publish_snapshot(serial, pic_url)

    def handle_polled(self, item: dict[str, Any]) -> None:
        """Emit an event from a message found by polling."""
        serial = item.get("deviceSerial")
        if self._serial_filter and serial not in self._serial_filter:
            return

        _LOGGER.info("Polled message for %s: %s", serial, item)

        ext = item.get("ext")
        ext = ext if isinstance(ext, dict) else {}
        raw_code = item.get("subType")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1

        text = " ".join(
            str(value)
            for value in (item.get("title"), item.get("detail"), ext.get("text"))
            if value
        )
        event_type, why = self.classify(
            code, text, bool(ext.get("callingStatus")), "poll"
        )

        # A polled alarm can also name the push code it came from, which is
        # worth a look before giving up and calling it a generic alarm.
        if event_type is EVENT_ALARM and ext.get("alarmType") is not None:
            try:
                alarm_type = int(ext["alarmType"])
            except (TypeError, ValueError):
                alarm_type = -1
            mapped = PUSH_ALERT_TYPES.get(alarm_type)
            if mapped is not None:
                event_type, why = mapped, f"alarmType {alarm_type}"

        _LOGGER.info(
            "%s (%s): %s, from %s",
            self.device_name(serial or ""),
            serial,
            event_type,
            why,
        )
        if event_type is EVENT_ALARM:
            _LOGGER.info(
                "Polled subType %s is not mapped yet, reported as '%s'."
                " Add it to the ring_codes or motion_codes option to classify"
                " it.",
                raw_code,
                EVENT_ALARM,
            )

        pic_url = item.get("pic")
        self.emit_event(
            serial,
            event_type,
            {
                "event_type": event_type,
                "alert_type_code": raw_code,
                "alert": item.get("title") or item.get("detail"),
                "time": item.get("timeStr") or item.get("time"),
                "msg_id": item.get("msgId"),
                "source": "poll",
            },
            str(pic_url).split(";")[0] if pic_url else None,
        )

    def poll_messages(
        self, client: EzvizClient, interval: int, cycle_stop: threading.Event
    ) -> None:
        """Poll the cloud message list as a fallback for missing pushes.

        The push channel can report itself connected and subscribed and still
        deliver nothing. Polling the same feed the official app reads answers
        whether the event was recorded at all, and doubles as a working - if
        delayed - path to Home Assistant when push stays silent.
        """
        serials = ",".join(sorted(self._serial_filter)) or None

        while not self._stop.is_set() and not cycle_stop.is_set():
            try:
                with self._api_lock:
                    response = client.get_device_messages_list(
                        serials=serials, limit=10, date="", end_time=""
                    )
                items = response.get("message") or response.get("messages") or []
                if not isinstance(items, list):
                    items = []

                for item in reversed(items):
                    msg_id = item.get("msgId")
                    if not msg_id or msg_id in self._seen_messages:
                        continue
                    self._seen_messages.add(msg_id)
                    # The first sweep only records what already exists, so
                    # history is not replayed as fresh events on startup.
                    if self._poll_primed:
                        self.handle_polled(item)

                if not self._poll_primed:
                    _LOGGER.info(
                        "Message poll primed with %d existing message(s),"
                        " watching for new ones every %ss",
                        len(self._seen_messages),
                        interval,
                    )
                    self._poll_primed = True

                if len(self._seen_messages) > 500:
                    self._seen_messages.clear()
                    self._poll_primed = False

            except (PyEzvizError, OSError) as err:
                _LOGGER.warning("Message poll failed: %s", err)

            self._wait_before_next_poll(interval, cycle_stop)

    def _wait_before_next_poll(
        self, interval: int, cycle_stop: threading.Event
    ) -> None:
        """Sleep until the next poll, cut short by a push or a shutdown."""
        in_burst = time.monotonic() < self._burst_until
        deadline = time.monotonic() + (BURST_INTERVAL if in_burst else interval)

        while time.monotonic() < deadline:
            if cycle_stop.wait(POLL_TICK) or self._stop.is_set():
                return
            if self._poll_now.is_set():
                self._poll_now.clear()
                _LOGGER.debug("Poll woken early by a push")
                return

    # ------------------------------------------------------------------
    # Device status
    # ------------------------------------------------------------------

    def _detection_sensitivity(self, client: EzvizClient, serial: str) -> Any:
        """Return the current detection sensitivity, if the device has one."""
        if serial in self._sensitivity_broken:
            return None
        for type_value in ("3", "0"):
            try:
                with self._api_lock:
                    value = client.get_detection_sensibility(serial, type_value)
            except (PyEzvizError, OSError) as err:
                _LOGGER.debug(
                    "Detection sensitivity type %s unavailable for %s: %s",
                    type_value,
                    serial,
                    err,
                )
                continue
            if value is not None:
                return value

        # Asking every minute for something this device does not report is
        # just noise, so ask once and then stop.
        self._sensitivity_broken.add(serial)
        _LOGGER.info(
            "%s does not report a detection sensitivity; its number entity"
            " stays empty",
            self.device_name(serial),
        )
        return None

    def publish_status(self, serial: str) -> None:
        """Publish the status document every read-only entity reads."""
        status = self._status.get(serial)
        if status is None:
            # Before the first status poll there is nothing to report but the
            # events themselves, and those should not have to wait for it.
            status = build_status(serial, {}, self._event_state.get(serial, {}))
            self._status[serial] = status
        else:
            status.update(self._event_state.get(serial, {}))
        self._publish(status_topic(serial), json.dumps(status), retain=True)

    def refresh_status(self, client: EzvizClient) -> None:
        """Fetch every device's status and publish it."""
        with self._api_lock:
            devices = client.get_device_infos()

        for serial, info in (devices or {}).items():
            if self._serial_filter and serial not in self._serial_filter:
                continue

            try:
                raw = EzvizCamera(client, serial, info).status(refresh=False)
            except (PyEzvizError, OSError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not read the status of %s: %s", serial, err)
                continue

            if raw.get("name"):
                self._names[serial] = str(raw["name"])

            extra: dict[str, Any] = {
                "detection_sensitivity": self._detection_sensitivity(client, serial),
                **self._event_state.get(serial, {}),
            }
            if self._liveview is not None:
                extra.update(self._liveview.urls(serial))

            self._status[serial] = build_status(serial, raw, extra)
            self.announce_full(serial)
            self.publish_status(serial)
            self._publish(
                update_topic(serial), json.dumps(firmware_payload(raw)), retain=True
            )

    def poll_status(
        self, client: EzvizClient, interval: int, cycle_stop: threading.Event
    ) -> None:
        """Keep the status entities up to date until the cycle ends."""
        while not self._stop.is_set() and not cycle_stop.is_set():
            try:
                self.refresh_status(client)
            except (PyEzvizError, OSError) as err:
                _LOGGER.warning("Status poll failed: %s", err)

            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                if cycle_stop.wait(POLL_TICK) or self._stop.is_set():
                    return
                if self._status_now.is_set():
                    self._status_now.clear()
                    break

    # ------------------------------------------------------------------
    # EZVIZ connection
    # ------------------------------------------------------------------

    def load_devices(self, client: EzvizClient) -> dict[str, Any]:
        """Fetch device info so entities are not named after a serial."""
        try:
            with self._api_lock:
                devices = client.get_device_infos()
        except (PyEzvizError, OSError) as err:
            _LOGGER.warning("Could not fetch device info: %s", err)
            return {}

        for serial, info in (devices or {}).items():
            name = ((info or {}).get("deviceInfos") or {}).get("name")
            if name:
                self._names[serial] = name

        # Knowing what else is on the account matters: if no other device can
        # raise an alarm, "no push arrived" says nothing about the channel.
        _LOGGER.info("Account has %d device(s):", len(devices or {}))
        for serial, info in (devices or {}).items():
            dev = (info or {}).get("deviceInfos") or {}
            _LOGGER.info(
                "  %s  %-24s category=%s status=%s",
                serial,
                dev.get("name") or "?",
                dev.get("deviceCategory"),
                dev.get("status"),
            )

        return devices or {}

    def check_notifications(self, devices: dict[str, Any]) -> None:
        """Warn when EZVIZ is set not to push the things we listen for.

        EZVIZ suppresses pushes server side per device. If the do-not-disturb
        flags are set, nothing ever reaches this add-on however healthy the
        connection looks, so say so plainly instead of waiting in silence.
        """
        for serial, info in devices.items():
            if self._serial_filter and serial not in self._serial_filter:
                continue

            name = self.device_name(serial)
            nodisturb = (info or {}).get("NODISTURB") or {}
            if not nodisturb:
                _LOGGER.info(
                    "%s: no NODISTURB block reported, cannot tell whether"
                    " EZVIZ is suppressing its notifications",
                    name,
                )
                continue

            _LOGGER.info("%s NODISTURB: %s", name, nodisturb)

            if nodisturb.get("callingEnable"):
                _LOGGER.warning(
                    "%s: doorbell call notifications are switched OFF in the"
                    " EZVIZ app, so a button press is never pushed. Turn them"
                    " back on in the app, or with this add-on's 'Doorbell"
                    " notifications' switch.",
                    name,
                )
            if nodisturb.get("alarmEnable"):
                _LOGGER.warning(
                    "%s: alarm notifications are switched OFF in the EZVIZ"
                    " app, so motion is never pushed.",
                    name,
                )

    def instrument_push(self, push_client: Any) -> None:
        """Guarantee a subscription and report it at INFO.

        pyezvizapi only subscribes when the broker reports a fresh session::

            if rc == 0 and not session_present:
                client.subscribe(self._topic, qos=2)

        A resumed session whose subscriptions did not survive therefore leaves
        the client connected and permanently deaf, with nothing said above
        debug level. Subscribe again explicitly - it is idempotent - and log
        the outcome, so "nothing was sent" can be told apart from "we were
        never listening".
        """
        paho_client = getattr(push_client, "mqtt_client", None)
        if paho_client is None:
            _LOGGER.warning("Push client exposes no MQTT client to instrument")
            return

        topic = getattr(push_client, "_topic", None)
        original_subscribe = paho_client.on_subscribe
        original_message = paho_client.on_message

        def on_subscribe(
            client: Any, userdata: Any, mid: Any, granted_qos: Any, *args: Any
        ) -> None:
            _LOGGER.info(
                "Subscribed to EZVIZ push topic %s (qos=%s)", topic, granted_qos
            )
            if original_subscribe:
                original_subscribe(client, userdata, mid, granted_qos, *args)

        def on_message(client: Any, userdata: Any, message: Any) -> None:
            _LOGGER.debug(
                "Raw push payload on %s: %r",
                getattr(message, "topic", "?"),
                getattr(message, "payload", b""),
            )
            if original_message:
                original_message(client, userdata, message)

        paho_client.on_subscribe = on_subscribe
        paho_client.on_message = on_message

        if not topic:
            _LOGGER.warning("Could not determine the EZVIZ push topic")
            return

        result, mid = paho_client.subscribe(topic, qos=2)
        if result == mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.info("Subscription to %s requested (mid=%s)", topic, mid)
        else:
            _LOGGER.error(
                "Could not subscribe to %s: paho error %s", topic, result
            )

    def _load_token(self) -> dict[str, Any] | None:
        """Return the saved session token, if there is a usable one."""
        if not TOKEN_PATH.exists():
            return None
        try:
            return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Saved session is unreadable (%s), logging in again", err)
            return None

    def _save_token(self, client: EzvizClient) -> None:
        """Persist the session so the next start needs no two factor code."""
        try:
            TOKEN_PATH.write_text(
                json.dumps(client.export_token()), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as err:
            _LOGGER.warning("Could not save the session: %s", err)

    def authenticate(self) -> EzvizClient:
        """Log in, reusing a saved session when one exists.

        A stored token is refreshed through its refresh session id, which EZVIZ
        allows without a second factor. The two factor code is therefore only
        ever needed for the very first login.
        """
        token = self._load_token()
        client = EzvizClient(
            self._options["ezviz_username"],
            self._options["ezviz_password"],
            self._region,
            token=token,
        )

        mfa_code = str(self._options.get("mfa_code") or "").strip()

        if token:
            _LOGGER.info("Reusing the saved EZVIZ session")
            client.login()
        elif mfa_code:
            if not mfa_code.isdigit():
                raise PyEzvizError(
                    f"mfa_code {mfa_code!r} is not numeric - copy the digits"
                    " from the EZVIZ email"
                )
            client.login(sms_code=int(mfa_code))
            _LOGGER.info(
                "Logged in with the two factor code. You can clear 'mfa_code'"
                " in the add-on options now - it is single use and the session"
                " has been saved."
            )
        else:
            client.login()

        self._save_token(client)
        return client

    # ------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------

    def start_liveview(self) -> None:
        """Start the HTTP server that serves live video, if it is enabled."""
        if not self._options.get("live_stream", True):
            _LOGGER.info("Live view is switched off (live_stream=false)")
            return

        token = liveview.load_token(
            LIVE_TOKEN_PATH, str(self._options.get("live_stream_token") or "")
        )
        base_url = str(self._options.get("live_stream_url_base") or "").strip()
        if not base_url:
            # Add-ons resolve each other by container hostname on the
            # Supervisor's network, which is how Home Assistant will reach us.
            host = os.environ.get("HOSTNAME") or socket.gethostname()
            base_url = f"http://{host}:{liveview.PORT}"

        server = liveview.LiveViewServer(
            self,
            liveview.PORT,
            token,
            base_url,
            mjpeg_interval=float(self._options.get("snapshot_interval") or 3),
        )
        try:
            server.start()
        except OSError as err:
            _LOGGER.error("Could not start the live view server: %s", err)
            return
        self._liveview = server

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run until stopped, reconnecting to EZVIZ as needed."""
        self.connect_mqtt()
        self.start_liveview()
        threading.Thread(
            target=self._command_worker, name="ezviz-commands", daemon=True
        ).start()

        while not self._stop.is_set():
            push_client = None
            # Bounds the worker threads to this connection cycle, so a
            # reconnect never leaves a second one running against a stale
            # client.
            cycle_stop = threading.Event()
            try:
                try:
                    client = self.authenticate()
                except EzvizAuthVerificationCode:
                    TOKEN_PATH.unlink(missing_ok=True)
                    _LOGGER.error(
                        "EZVIZ requires a two factor code, which it has just"
                        " sent to your email. Paste it into the 'mfa_code'"
                        " option in the add-on Configuration tab, save, and"
                        " restart the add-on. The session is stored afterwards,"
                        " so this is a one time step."
                    )
                    self._stop.wait(300)
                    continue

                self._client = client
                devices = self.load_devices(client)
                self.check_notifications(devices)
                for serial in self._serial_filter:
                    self.announce(serial)

                push_client = client.get_mqtt_client(
                    on_message_callback=self.handle_push
                )
                # A clean session forces the library's own subscribe to run,
                # rather than trusting a resumed session to still hold it.
                push_client.connect(clean_session=True)
                self.instrument_push(push_client)
                _LOGGER.info("Connected to EZVIZ push, waiting for events")

                poll_interval = int(self._options.get("poll_interval") or 0)
                if poll_interval > 0:
                    threading.Thread(
                        target=self.poll_messages,
                        args=(client, poll_interval, cycle_stop),
                        name="ezviz-poll",
                        daemon=True,
                    ).start()
                    _LOGGER.info(
                        "Polling every %ss, dropping to %ss for %ss after any"
                        " push. A ring is usually only seen by polling, so this"
                        " interval is its worst case latency.",
                        poll_interval,
                        BURST_INTERVAL,
                        BURST_SECONDS,
                    )
                else:
                    _LOGGER.info("Message polling is off (poll_interval=0)")

                status_interval = int(self._options.get("status_interval") or 0)
                if status_interval > 0:
                    threading.Thread(
                        target=self.poll_status,
                        args=(client, status_interval, cycle_stop),
                        name="ezviz-status",
                        daemon=True,
                    ).start()
                    _LOGGER.info(
                        "Refreshing device status every %ss", status_interval
                    )
                else:
                    _LOGGER.info(
                        "Status refresh is off (status_interval=0); only the"
                        " event entities will update"
                    )

                while not self._stop.is_set():
                    self._stop.wait(1)

            except (PyEzvizError, OSError, KeyError) as err:
                _LOGGER.error("EZVIZ connection failed (%s), retrying in 60s", err)
                self._stop.wait(60)
            finally:
                cycle_stop.set()
                self._client = None
                if push_client is not None:
                    try:
                        push_client.stop()
                    except (PyEzvizError, OSError) as err:
                        _LOGGER.debug("Error stopping EZVIZ push: %s", err)

        self.shutdown()

    def stop(self) -> None:
        """Signal the bridge to stop."""
        self._stop.set()

    def shutdown(self) -> None:
        """Publish an offline message and disconnect from MQTT."""
        if self._liveview is not None:
            self._liveview.stop()
        if self._mqtt is None:
            return
        self._mqtt.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
        _LOGGER.info("Stopped")


def _first_image_url(value: Any) -> str | None:
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
        values: Any = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    for item in values:
        if found := _first_image_url(item):
            return found
    return None


def main() -> int:
    """Entry point."""
    options = load_options()

    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), 20),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Echo the effective configuration, so a setting that did not take can be
    # spotted from the log rather than guessed at.
    _LOGGER.info(
        "Starting: log_level=%s poll_interval=%ss status_interval=%ss region=%s"
        " serials=%s verification_codes_for=%s",
        options.get("log_level"),
        options.get("poll_interval"),
        options.get("status_interval"),
        options.get("ezviz_region"),
        options.get("serials") or "(all devices)",
        [
            item.get("serial")
            for item in options.get("verification_codes") or []
        ]
        or "(none)",
    )
    _LOGGER.debug("Debug logging is enabled")

    if not options.get("ezviz_username") or not options.get("ezviz_password"):
        _LOGGER.error("Set ezviz_username and ezviz_password in the add-on options")
        return 1

    pin_feature_code()

    bridge = EzvizPushBridge(options)

    def _handle_signal(signum: int, frame: Any) -> None:
        _LOGGER.info("Received signal %s, shutting down", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
