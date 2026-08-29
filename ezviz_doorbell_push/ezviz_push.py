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

import hashlib
import paho.mqtt.client as mqtt
from pyezvizapi import client as ezviz_client_module
from pyezvizapi import constants as ezviz_constants
from pyezvizapi import mqtt as ezviz_mqtt_module
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError
from pyezvizapi.utils import decrypt_image
import requests

OPTIONS_PATH = Path("/data/options.json")
TOKEN_PATH = Path("/data/token.json")
FEATURE_CODE_PATH = Path("/data/feature_code")
SUPERVISOR_URL = "http://supervisor"
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "ezviz_push"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"

EVENT_RING = "ring"
EVENT_MOTION = "motion"
EVENT_ALARM = "alarm"
EVENT_TYPES = [EVENT_RING, EVENT_MOTION, EVENT_ALARM]

# Push and polled messages use two different code spaces, so they get two
# different maps. Anything unmapped is reported as a generic alarm with the raw
# code attached, so a new code can be identified from the event attributes.

# Push messages carry "alert_type_code": 0 is the doorbell button, 10000 the PIR.
PUSH_ALERT_TYPES: dict[int, str] = {
    0: EVENT_RING,
    10000: EVENT_MOTION,
}

# Polled messages carry "subType". A ring is 2701 - a *call*, not an alarm,
# which is why it never appears on the alarm push channel.
POLL_SUBTYPES: dict[int, str] = {
    2701: EVENT_RING,
}

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
        self._seen_messages: set[str] = set()
        self._poll_primed = False

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
        _LOGGER.info("Published a %d byte snapshot for %s", len(data), serial)

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
        event_type = PUSH_ALERT_TYPES.get(code, EVENT_ALARM)

        _LOGGER.info(
            "%s (%s): %s",
            self._names.get(serial, serial),
            serial,
            event_type,
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

        event_type = POLL_SUBTYPES.get(code, EVENT_ALARM)

        # A ring is a call rather than an alarm, and says so outright.
        if ext.get("callingStatus"):
            event_type = EVENT_RING
        elif event_type is EVENT_ALARM and ext.get("alarmType") is not None:
            try:
                event_type = PUSH_ALERT_TYPES.get(
                    int(ext["alarmType"]), EVENT_ALARM
                )
            except (TypeError, ValueError):
                pass

        if event_type is EVENT_ALARM:
            _LOGGER.info(
                "Polled subType %s is not mapped yet, reported as '%s'",
                raw_code,
                EVENT_ALARM,
            )

        self.announce(serial)
        self._publish(
            f"{BASE_TOPIC}/{serial}/event",
            json.dumps(
                {
                    "event_type": event_type,
                    "alert_type_code": raw_code,
                    "alert": item.get("title") or item.get("detail"),
                    "time": item.get("timeStr") or item.get("time"),
                    "msg_id": item.get("msgId"),
                    "source": "poll",
                }
            ),
        )

        if pic_url := item.get("pic"):
            self.publish_snapshot(serial, str(pic_url).split(";")[0])

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

            cycle_stop.wait(interval)

    def load_devices(self, client: EzvizClient) -> dict[str, Any]:
        """Fetch device info so entities are not named after a serial."""
        try:
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

            name = self._names.get(serial, serial)
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
                    " EZVIZ app, so a button press is never pushed. Turn"
                    " notifications back on for this device in the app.",
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

    def run(self) -> None:
        """Run until stopped, reconnecting to EZVIZ as needed."""
        self.connect_mqtt()

        while not self._stop.is_set():
            push_client = None
            # Bounds the poll thread to this connection cycle, so a reconnect
            # never leaves a second one running against a stale client.
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
                else:
                    _LOGGER.info("Message polling is off (poll_interval=0)")

                while not self._stop.is_set():
                    self._stop.wait(1)

            except (PyEzvizError, OSError, KeyError) as err:
                _LOGGER.error("EZVIZ connection failed (%s), retrying in 60s", err)
                self._stop.wait(60)
            finally:
                cycle_stop.set()
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

    # Echo the effective configuration, so a setting that did not take can be
    # spotted from the log rather than guessed at.
    _LOGGER.info(
        "Starting: log_level=%s region=%s serials=%s verification_codes_for=%s",
        options.get("log_level"),
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
