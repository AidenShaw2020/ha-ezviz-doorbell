"""EZVIZ push -> MQTT bridge.

The Home Assistant EZVIZ integration polls the cloud every 30 seconds and has
no event platform, so a doorbell button press never reaches Home Assistant at
all: the ring travels over the EZVIZ push channel, which the polled alarm feed
never shows.

This add-on keeps that push channel open and republishes everything it hears
onto the local MQTT broker using MQTT discovery, so Home Assistant creates the
entities on its own. It runs beside the built-in integration and changes
nothing about it.

Alarm snapshots are AES encrypted by EZVIZ (the files start with the magic
bytes ``hikencodepicture``). Given the device verification code, this add-on
decrypts them and publishes the plain JPEG, which means image encryption can
stay switched on in the EZVIZ app.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError
from pyezvizapi.utils import decrypt_image
import requests

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_URL = "http://supervisor"
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "ezviz_push"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"

EVENT_RING = "ring"
EVENT_MOTION = "motion"
EVENT_ALARM = "alarm"
EVENT_TYPES = [EVENT_RING, EVENT_MOTION, EVENT_ALARM]

# Observed EZVIZ push alert codes: 0 is the doorbell button, 10000 the PIR.
# Anything else is reported as a generic alarm with the raw code attached, so
# an unmapped code can be identified from the event attributes.
ALERT_TYPE_MAP: dict[int, str] = {
    0: EVENT_RING,
    10000: EVENT_MOTION,
}

_LOGGER = logging.getLogger("ezviz_push")


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


class EzvizPushBridge:
    """Bridge EZVIZ cloud push messages onto the local MQTT broker."""

    def __init__(self, options: dict[str, Any]) -> None:
        """Initialize the bridge."""
        self._options = options
        self._region = options.get("ezviz_region") or "apiieu.ezvizlife.com"
        self._serial_filter = {s for s in options.get("serials") or [] if s}
        self._codes = {
            item["serial"]: item["code"]
            for item in options.get("verification_codes") or []
            if item.get("serial") and item.get("code")
        }
        self._names: dict[str, str] = {}
        self._announced: set[str] = set()
        self._mqtt: mqtt.Client | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Local MQTT
    # ------------------------------------------------------------------

    def connect_mqtt(self) -> None:
        """Connect to the local broker and publish an online birth message."""
        settings = discover_mqtt(self._options)

        # paho-mqtt 2.x demands an explicit callback API version, 1.x does not
        # accept the argument at all. No callbacks are registered here, so
        # either generation behaves identically.
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
        client.connect(settings["host"], settings["port"], keepalive=60)
        client.loop_start()
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        self._mqtt = client
        _LOGGER.info("Connected to MQTT broker at %s:%s",
                     settings["host"], settings["port"])

    def _publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        """Publish to the local broker."""
        if self._mqtt is None:
            return
        self._mqtt.publish(topic, payload, retain=retain)

    def _device_block(self, serial: str) -> dict[str, Any]:
        """Return the MQTT discovery device block for one camera."""
        return {
            "identifiers": [f"ezviz_push_{serial}"],
            "name": self._names.get(serial, serial),
            "manufacturer": "EZVIZ",
            "model": "EZVIZ camera (push)",
        }

    def announce(self, serial: str) -> None:
        """Publish MQTT discovery config for a device, once."""
        if serial in self._announced:
            return

        device = self._device_block(serial)

        self._publish(
            f"{DISCOVERY_PREFIX}/event/ezviz_push_{serial}/alerts/config",
            json.dumps(
                {
                    "name": "Alerts",
                    "unique_id": f"ezviz_push_{serial}_alerts",
                    "state_topic": f"{BASE_TOPIC}/{serial}/event",
                    "availability_topic": AVAILABILITY_TOPIC,
                    "device_class": "doorbell",
                    "event_types": EVENT_TYPES,
                    "device": device,
                }
            ),
            retain=True,
        )

        self._publish(
            f"{DISCOVERY_PREFIX}/image/ezviz_push_{serial}/snapshot/config",
            json.dumps(
                {
                    "name": "Last snapshot",
                    "unique_id": f"ezviz_push_{serial}_snapshot",
                    "image_topic": f"{BASE_TOPIC}/{serial}/image",
                    "image_encoding": "b64",
                    "content_type": "image/jpeg",
                    "availability_topic": AVAILABILITY_TOPIC,
                    "device": device,
                }
            ),
            retain=True,
        )

        self._announced.add(serial)
        _LOGGER.info("Announced %s (%s) to Home Assistant",
                     self._names.get(serial, serial), serial)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def publish_snapshot(self, serial: str, url: str) -> None:
        """Download, decrypt if needed, and publish an alarm snapshot."""
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as err:
            _LOGGER.warning("Could not download snapshot for %s: %s", serial, err)
            return

        data = resp.content
        if data[:16] == b"hikencodepicture":
            code = self._codes.get(serial)
            if not code:
                _LOGGER.warning(
                    "Snapshot for %s is encrypted but no verification code is"
                    " configured - skipping. Add it under verification_codes,"
                    " or turn off image encryption in the EZVIZ app",
                    serial,
                )
                return
            try:
                data = decrypt_image(data, code)
            except PyEzvizError as err:
                _LOGGER.warning(
                    "Could not decrypt snapshot for %s (%s) - is the"
                    " verification code correct?",
                    serial,
                    err,
                )
                return

        self._publish(
            f"{BASE_TOPIC}/{serial}/image",
            base64.b64encode(data).decode("ascii"),
            retain=True,
        )

    # ------------------------------------------------------------------
    # EZVIZ push
    # ------------------------------------------------------------------

    def handle_push(self, msg: dict[str, Any]) -> None:
        """Handle one decoded push message (paho network thread)."""
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}

        serial = ext.get("device_serial")
        if not serial:
            _LOGGER.debug("Ignoring push message without a serial: %s", msg)
            return
        if self._serial_filter and serial not in self._serial_filter:
            _LOGGER.debug("Ignoring push message for filtered device %s", serial)
            return

        raw_code = ext.get("alert_type_code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1
        event_type = ALERT_TYPE_MAP.get(code, EVENT_ALARM)

        _LOGGER.info(
            "%s (%s): %s (alert_type_code=%s)",
            self._names.get(serial, serial),
            serial,
            event_type,
            raw_code,
        )
        if event_type == EVENT_ALARM:
            _LOGGER.info(
                "Alert code %s is not mapped yet. If this was the doorbell"
                " button, add %s to ALERT_TYPE_MAP.",
                raw_code,
                raw_code,
            )

        self.announce(serial)
        self._publish(
            f"{BASE_TOPIC}/{serial}/event",
            json.dumps(
                {
                    "event_type": event_type,
                    "alert_type_code": raw_code,
                    "alert": msg.get("alert"),
                    "time": ext.get("time"),
                    "msg_id": ext.get("msgId"),
                }
            ),
        )

        if pic_url := ext.get("default_pic_url"):
            self.publish_snapshot(serial, pic_url)

    def load_names(self, client: EzvizClient) -> None:
        """Fetch device names so entities are not named after a serial."""
        try:
            devices = client.get_device_infos()
        except (PyEzvizError, OSError) as err:
            _LOGGER.warning("Could not fetch device names: %s", err)
            return

        for serial, info in (devices or {}).items():
            name = ((info or {}).get("deviceInfos") or {}).get("name")
            if name:
                self._names[serial] = name

    def run(self) -> None:
        """Run until stopped, reconnecting to EZVIZ as needed."""
        self.connect_mqtt()

        while not self._stop.is_set():
            push_client = None
            try:
                client = EzvizClient(
                    self._options["ezviz_username"],
                    self._options["ezviz_password"],
                    self._region,
                )
                try:
                    client.login()
                except EzvizAuthVerificationCode:
                    _LOGGER.error(
                        "EZVIZ demands a two factor code. Log in once in the"
                        " EZVIZ app, approve this login, then restart the"
                        " add-on."
                    )
                    self._stop.wait(300)
                    continue

                self.load_names(client)
                for serial in self._serial_filter:
                    self.announce(serial)

                push_client = client.get_mqtt_client(
                    on_message_callback=self.handle_push
                )
                push_client.connect()
                _LOGGER.info("Connected to EZVIZ push, waiting for events")

                while not self._stop.is_set():
                    self._stop.wait(1)

            except (PyEzvizError, OSError, KeyError) as err:
                _LOGGER.error("EZVIZ connection failed (%s), retrying in 60s", err)
                self._stop.wait(60)
            finally:
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
        if self._mqtt is None:
            return
        self._mqtt.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
        _LOGGER.info("Stopped")


def main() -> int:
    """Entry point."""
    options = load_options()

    logging.basicConfig(
        level=getattr(logging, str(options.get("log_level", "info")).upper(), 20),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not options.get("ezviz_username") or not options.get("ezviz_password"):
        _LOGGER.error("Set ezviz_username and ezviz_password in the add-on options")
        return 1

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
