"""One EZVIZ cloud session, shared by every platform.

The coordinator owns three things at once: the polled device status behind the
usual refresh interval, the push connection that carries motion, and the
message poll that carries the doorbell ring. The last two are what make a
doorbell usable, and neither fits the coordinator's own timer, so both run
alongside it and hand their results to entities over the dispatcher.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import inspect
import logging
import threading
import time
from typing import Any

import requests

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BURST_INTERVAL,
    BURST_SECONDS,
    CAPTURE_ALWAYS,
    CAPTURE_RING,
    CAMERA_SUBENTRY,
    CONF_CAPTURE_WHEN_MISSING,
    CONF_DEVICES,
    CONF_MOTION_CODES,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_RING_CODES,
    CONF_SERIAL,
    CONF_STATUS_INTERVAL,
    CONF_TOKEN,
    CONF_VERIFICATION_CODE,
    CONF_VERIFICATION_CODES,
    DOORBELL_CATEGORIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_REGION,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
    EVENT_ALARM,
    EVENT_MOTION,
    EVENT_RING,
    MOTION_PATTERN,
    POLL_SUBTYPES,
    PUSH_ALERT_TYPES,
    PICTURE_FRESH_SECONDS,
    RING_PATTERN,
    SETTLE_SECONDS,
    signal_event,
)
from .helpers import first_image_url
from .vendor.pyezvizapi.camera import EzvizCamera
from .vendor.pyezvizapi.client import EzvizClient
from .vendor.pyezvizapi.constants import DeviceSwitchType
from .vendor.pyezvizapi.exceptions import EzvizAuthVerificationCode, PyEzvizError
from .vendor.pyezvizapi.utils import decrypt_image

_LOGGER = logging.getLogger(__name__)

HIK_ENCRYPTION_HEADER = b"hikencodepicture"

# The library ships with this integration (see vendor/), so these checks should
# never find anything missing. They stay as a safety net: if the bundled copy is
# ever replaced by a thinner one, the integration says what it has lost and
# carries on with the rest rather than failing to load - and the doorbell ring
# survives almost anything, because it arrives by polling.
_STATUS_ACCEPTS_REFRESH = "refresh" in inspect.signature(EzvizCamera.status).parameters

# Method on EzvizClient -> what is lost without it.
OPTIONAL_METHODS: dict[str, str] = {
    "get_mqtt_client": "instant motion over push (polling still delivers events)",
    "capture_picture": "snapshots and live stills taken on demand",
    "delay_battery_device_sleep": "asking a sleeping camera to stay awake",
    "set_alarm_detect_human_car": "the detection type setting",
    "export_token": "keeping the session across restarts",
}


def library_gaps(can_stream: bool) -> list[str]:
    """Return what the installed pyezvizapi cannot do."""
    gaps = [
        description
        for name, description in OPTIONAL_METHODS.items()
        if not hasattr(EzvizClient, name)
    ]
    if not can_stream:
        gaps.append("live video from the cloud stream")
    return gaps


def report_library(can_stream: bool) -> None:
    """Say plainly which pyezvizapi is in use and what it costs."""
    gaps = library_gaps(can_stream)
    if not gaps:
        return
    _LOGGER.warning(
        "The copy of pyezvizapi bundled with this integration is not the one it"
        " expects, so these are unavailable: %s. Reinstall the integration to"
        " restore them.",
        "; ".join(gaps),
    )


@dataclass
class DeviceData:
    """Everything known about one camera.

    Refreshes update ``raw`` and ``switches`` in place, so what the push and
    poll paths record here - the last ring, the last snapshot - is not thrown
    away every time the status is fetched again.
    """

    serial: str
    raw: dict[str, Any] = field(default_factory=dict)
    switches: dict[int, bool] = field(default_factory=dict)
    detection_sensitivity: int | None = None

    last_event: str | None = None
    last_event_time: datetime | None = None
    last_ring: datetime | None = None
    last_motion: datetime | None = None

    snapshot: bytes | None = None
    snapshot_time: datetime | None = None

    # What decrypts this camera: the code from its label if one was given, or
    # the key EZVIZ hands the account that owns it.
    media_key: str | None = None

    @property
    def name(self) -> str:
        """Return the device's name, falling back to its serial."""
        return str(self.raw.get("name") or self.serial)

    @property
    def model(self) -> str | None:
        """Return the model EZVIZ reports."""
        value = self.raw.get("device_sub_category") or self.raw.get("device_category")
        return str(value) if value else None

    @property
    def version(self) -> str | None:
        """Return the installed firmware version."""
        value = self.raw.get("version")
        return str(value) if value else None

    @property
    def online(self) -> bool:
        """Return whether the cloud considers the device reachable."""
        return self.raw.get("status") == 1


class EzvizDoorbellCoordinator(DataUpdateCoordinator[dict[str, DeviceData]]):
    """Keep one EZVIZ session and everything it feeds."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator without touching the network."""
        options = entry.options
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)
            ),
        )
        self.client: EzvizClient | None = None
        self.data = {}
        # pyezvizapi's cloud stream copier, when the installed version has one.
        self.cloud_stream: Callable[..., None] | None = None
        # What the options said when this coordinator was built, so a later
        # entry update can be told from a real options change.
        self.loaded_signature = config_signature(entry)

        # pyezvizapi is synchronous and is called from executor threads by the
        # push handler, the message poll and every entity command, so its
        # session takes turns.
        self._api_lock = threading.Lock()
        self._push_client: Any = None
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_now = asyncio.Event()
        self._burst_until = 0.0
        self._seen_messages: set[str] = set()
        self._poll_primed = False
        self._recent: dict[str, float] = {}
        self._sensitivity_broken: set[str] = set()
        self._warned_about_categories = False
        self._keyless: set[str] = set()
        # Nothing that arrives in the first moments of listening is news: the
        # cloud hands over what it has been holding as soon as it is asked.
        self._settle_until = 0.0

        poll_interval = options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        # Wide enough to cover a poll arriving after a push for the same event,
        # but no wider, so two genuine presses are not merged into one.
        self._dedupe_window = max(15, poll_interval + 10)
        self._capture_when_missing = options.get(
            CONF_CAPTURE_WHEN_MISSING, CAPTURE_RING
        )
        self._extra_ring = _codes(options.get(CONF_RING_CODES))
        self._extra_motion = _codes(options.get(CONF_MOTION_CODES))

        # Empty means every device on the account.
        self._only = {str(serial) for serial in options.get(CONF_DEVICES) or []}
        # The code from the device label. EZVIZ will normally hand this account
        # the same key over the API, but not for every device and not for every
        # account, and without it an encrypted picture or stream is noise.
        self._verification_codes = verification_codes(entry)

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _create_client(self) -> EzvizClient:
        """Build a client from the stored credentials (executor thread)."""
        entry = self.config_entry
        return EzvizClient(
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data.get(CONF_REGION, DEFAULT_REGION),
            token=entry.data.get(CONF_TOKEN),
        )

    def _login(self) -> tuple[EzvizClient, dict[str, Any]]:
        """Log in, reusing the stored session when there is one."""
        client = self._create_client()
        # login() returns the session in every version of the library;
        # export_token() only exists in the newer ones.
        token = client.login()
        if not token and hasattr(client, "export_token"):
            token = client.export_token()
        return client, dict(token or {})

    async def async_login(self) -> None:
        """Log in and remember the refreshed session.

        Raises:
            ConfigEntryAuthFailed: If EZVIZ will not accept the credentials.
        """
        try:
            client, token = await self.hass.async_add_executor_job(self._login)
        except EzvizAuthVerificationCode as err:
            raise ConfigEntryAuthFailed(
                "EZVIZ wants a two factor code for this account again"
            ) from err
        except PyEzvizError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        self.client = client
        self._store_token(token)

    def _store_token(self, token: dict[str, Any]) -> None:
        """Persist a refreshed session, so a restart needs no new code."""
        if not token or token == self.config_entry.data.get(CONF_TOKEN):
            return
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_TOKEN: token},
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _fetch(self) -> dict[str, dict[str, Any]]:
        """Fetch every device's status (executor thread)."""
        assert self.client is not None
        with self._api_lock:
            devices = self.client.get_device_infos()

        wanted = self._wanted(devices or {})
        result: dict[str, dict[str, Any]] = {}
        for serial, info in (devices or {}).items():
            if serial not in wanted:
                continue
            try:
                camera = EzvizCamera(self.client, serial, info)
                with self._api_lock:
                    result[serial] = (
                        camera.status(refresh=False)
                        if _STATUS_ACCEPTS_REFRESH
                        else camera.status()
                    )
            except (PyEzvizError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Could not read the status of %s: %s", serial, err)
        return result

    def _wanted(self, devices: dict[str, Any]) -> set[str]:
        """Return the serials to build entities for.

        An EZVIZ account usually holds cameras that have nothing to do with a
        doorbell, and forty entities each is no use to anyone. So unless the
        options name particular devices, only what EZVIZ calls a doorbell is
        taken - and if that is nothing at all, the account is presumably all
        cameras and the choice is handed back rather than showing an empty
        integration.
        """
        if self._only:
            return {serial for serial in devices if serial in self._only}

        doorbells = {
            serial
            for serial, info in devices.items()
            if str(((info or {}).get("deviceInfos") or {}).get("deviceCategory") or "")
            in DOORBELL_CATEGORIES
        }
        if doorbells:
            return doorbells

        if not self._warned_about_categories:
            self._warned_about_categories = True
            _LOGGER.info(
                "No device on this account calls itself a doorbell, so all %d"
                " of them are being used. Pick the ones you want under the"
                " integration's options",
                len(devices),
            )
        return set(devices)

    async def _async_update_data(self) -> dict[str, DeviceData]:
        """Refresh every device's status."""
        if self.client is None:
            await self.async_login()

        try:
            statuses = await self.hass.async_add_executor_job(self._fetch)
        except EzvizAuthVerificationCode as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (PyEzvizError, OSError) as err:
            raise UpdateFailed(f"Could not reach EZVIZ: {err}") from err

        for serial, raw in statuses.items():
            device = self.data.get(serial)
            if device is None:
                device = DeviceData(serial=serial)
                self.data[serial] = device
            device.raw = raw
            device.switches = {
                int(number): bool(state)
                for number, state in (raw.get("switches") or {}).items()
            }
            if device.detection_sensitivity is None:
                # A setting rather than a reading: worth one request when the
                # device first appears, not one a minute for ever after.
                device.detection_sensitivity = (
                    await self._async_detection_sensitivity(serial)
                )

            if (
                device.media_key is None
                and raw.get("encrypted")
                and not self._verification_codes.get(serial)
            ):
                # Asked for before the entities are built, because whether the
                # camera can offer live video depends on the answer.
                device.media_key = await self.hass.async_add_executor_job(
                    self._fetch_media_key, serial
                )

        return self.data

    async def _async_detection_sensitivity(self, serial: str) -> int | None:
        """Return the detection sensitivity, if this device reports one."""
        if serial in self._sensitivity_broken:
            return None

        def _read() -> Any:
            assert self.client is not None
            for type_value in ("3", "0"):
                try:
                    with self._api_lock:
                        value = self.client.get_detection_sensibility(
                            serial, type_value
                        )
                except (PyEzvizError, OSError):
                    continue
                if value is not None:
                    return value
            return None

        value = await self.hass.async_add_executor_job(_read)
        if value is None:
            # Asking every minute for something this device does not report is
            # just noise, so ask once and then stop.
            self._sensitivity_broken.add(serial)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Push and message polling
    # ------------------------------------------------------------------

    async def async_start_listening(self) -> None:
        """Open the push connection and start polling for messages."""
        self._settle_until = time.monotonic() + SETTLE_SECONDS
        await self.hass.async_add_executor_job(self._connect_push)
        self._poll_task = self.config_entry.async_create_background_task(
            self.hass, self._message_poll(), f"{DOMAIN}_message_poll"
        )

    def _connect_push(self) -> None:
        """Connect to the EZVIZ push channel (executor thread)."""
        assert self.client is not None
        if not hasattr(self.client, "get_mqtt_client"):
            # Reported once already, in full, by report_library(); saying it
            # again at warning level only makes the same news look like two
            # separate problems.
            _LOGGER.info(
                "The bundled pyezvizapi cannot open the push channel, so motion"
                " will arrive by polling rather than instantly. The doorbell"
                " ring is unaffected - it only ever arrives by polling"
            )
            return
        try:
            with self._api_lock:
                push_client = self.client.get_mqtt_client(
                    on_message_callback=self._on_push
                )
                # A clean session forces the library's own subscribe to run,
                # rather than trusting a resumed session to still hold it.
                push_client.connect(clean_session=True)
        except (PyEzvizError, OSError) as err:
            _LOGGER.warning(
                "Could not open the EZVIZ push channel (%s); motion will still"
                " arrive by polling, just more slowly",
                err,
            )
            return

        self._push_client = push_client
        self._resubscribe(push_client)
        _LOGGER.debug("Connected to EZVIZ push")

    def _resubscribe(self, push_client: Any) -> None:
        """Subscribe again, in case a resumed session dropped the topic.

        pyezvizapi only subscribes when the broker reports a fresh session, so
        a resumed one whose subscriptions did not survive leaves the client
        connected and permanently deaf. Subscribing again is idempotent.
        """
        paho_client = getattr(push_client, "mqtt_client", None)
        topic = getattr(push_client, "_topic", None)
        if paho_client is None or not topic:
            return
        try:
            paho_client.subscribe(topic, qos=2)
        except (OSError, ValueError) as err:
            _LOGGER.debug("Could not re-subscribe to %s: %s", topic, err)

    def _on_push(self, msg: dict[str, Any]) -> None:
        """Handle a push message (paho network thread)."""
        # The coroutine has to be built on the event loop, not on this one, so
        # it is the lambda rather than the call that crosses over.
        self.hass.loop.call_soon_threadsafe(
            lambda: self.config_entry.async_create_background_task(
                self.hass, self._async_handle_push(msg), f"{DOMAIN}_push"
            )
        )

    async def _async_handle_push(self, msg: dict[str, Any]) -> None:
        """Turn a push message into an event."""
        ext = msg.get("ext")
        ext = ext if isinstance(ext, dict) else {}
        serial = ext.get("device_serial")

        _LOGGER.debug("Push received: %s", msg)

        if self._settling():
            # EZVIZ delivers what it was holding as soon as the connection
            # opens, so the first thing heard after a restart or a reload is
            # usually the last thing that happened - hours ago, and already
            # dealt with.
            _LOGGER.debug("Ignoring a push that arrived while starting up")
            return

        # A ring usually comes by polling rather than push. Motion does come
        # over push, and usually just before the button is pressed, so treat
        # any push as a cue to start polling hard.
        self._burst_until = time.monotonic() + BURST_SECONDS
        self._poll_now.set()

        if not serial or serial not in self.data:
            return

        code = _int(ext.get("alert_type_code"))
        text = str(msg.get("alert") or "")
        event_type, why = self.classify(code, text, False, "push")

        await self._async_emit(
            serial,
            event_type,
            why,
            {
                "alert_type_code": ext.get("alert_type_code"),
                "alert": msg.get("alert"),
                "time": ext.get("time"),
                "msg_id": ext.get("msgId"),
                "source": "push",
            },
            ext.get("default_pic_url"),
        )

    async def _message_poll(self) -> None:
        """Poll the message feed the official app reads.

        The push channel can report itself connected and subscribed and still
        deliver nothing, and a ring is not on it at all - EZVIZ files a
        doorbell press as a call. Polling is therefore the path that actually
        delivers the press.
        """
        interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        if interval <= 0:
            _LOGGER.info("Message polling is switched off; rings will not arrive")
            return

        while True:
            try:
                await self._async_poll_messages()
            except (PyEzvizError, OSError) as err:
                _LOGGER.warning("Message poll failed: %s", err)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in the message poll")

            await self._async_wait_for_next_poll(interval)

    async def _async_wait_for_next_poll(self, interval: int) -> None:
        """Wait out the poll interval, cut short by a push."""
        delay = BURST_INTERVAL if time.monotonic() < self._burst_until else interval
        try:
            await asyncio.wait_for(self._poll_now.wait(), timeout=delay)
        except TimeoutError:
            return
        self._poll_now.clear()

    async def _async_poll_messages(self) -> None:
        """Emit an event for every message that is new since the last poll."""

        def _read() -> list[dict[str, Any]]:
            assert self.client is not None
            with self._api_lock:
                response = self.client.get_device_messages_list(
                    serials=None, limit=10, date="", end_time=""
                )
            items = response.get("message") or response.get("messages") or []
            return items if isinstance(items, list) else []

        items = await self.hass.async_add_executor_job(_read)

        for item in reversed(items):
            msg_id = item.get("msgId")
            if not msg_id or msg_id in self._seen_messages:
                continue
            self._seen_messages.add(msg_id)
            # The first sweep only records what already exists, so history is
            # not replayed as fresh events on startup - and neither is anything
            # that turns up in the moments after, which the first sweep misses
            # when the cloud answers it with an empty list.
            if self._poll_primed and not self._settling():
                await self._async_handle_polled(item)

        if not self._poll_primed:
            _LOGGER.debug(
                "Message poll primed with %d existing message(s)",
                len(self._seen_messages),
            )
            self._poll_primed = True

        if len(self._seen_messages) > 500:
            self._seen_messages.clear()
            self._poll_primed = False

    async def _async_handle_polled(self, item: dict[str, Any]) -> None:
        """Turn a polled message into an event."""
        serial = item.get("deviceSerial")
        if not serial or serial not in self.data:
            return

        _LOGGER.debug("Polled message for %s: %s", serial, item)

        ext = item.get("ext")
        ext = ext if isinstance(ext, dict) else {}
        code = _int(item.get("subType"))
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
        if event_type == EVENT_ALARM and ext.get("alarmType") is not None:
            mapped = PUSH_ALERT_TYPES.get(_int(ext.get("alarmType")))
            if mapped is not None:
                event_type, why = mapped, f"alarmType {ext.get('alarmType')}"

        pic = item.get("pic")
        await self._async_emit(
            serial,
            event_type,
            why,
            {
                "alert_type_code": item.get("subType"),
                "alert": item.get("title") or item.get("detail"),
                "time": item.get("timeStr") or item.get("time"),
                "msg_id": item.get("msgId"),
                "source": "poll",
            },
            str(pic).split(";")[0] if pic else None,
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _settling(self) -> bool:
        """Return whether listening only just started."""
        return time.monotonic() < self._settle_until

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
            return EVENT_RING, f"code {code} from the ring codes option"
        if code in self._extra_motion:
            return EVENT_MOTION, f"code {code} from the motion codes option"
        if (event_type := secondary.get(code)) is not None:
            return event_type, f"code {code}, known from the other message path"

        if RING_PATTERN.search(text):
            return EVENT_RING, f"the wording of {text!r}"
        if MOTION_PATTERN.search(text):
            return EVENT_MOTION, f"the wording of {text!r}"

        return EVENT_ALARM, f"nothing recognised code {code} or {text!r}"

    def _should_emit(self, serial: str, event_type: str, msg_id: Any) -> bool:
        """Return False when this event already went out by the other path."""
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

    async def _async_emit(
        self,
        serial: str,
        event_type: str,
        why: str,
        attributes: dict[str, Any],
        picture_url: str | None,
    ) -> None:
        """Record an event and tell the entities about it."""
        if not self._should_emit(serial, event_type, attributes.get("msg_id")):
            _LOGGER.debug(
                "Skipping duplicate %s for %s, already delivered via the other path",
                event_type,
                serial,
            )
            # The copy that arrives second often carries the picture the first
            # one did not: a push announces the ring with nothing attached, and
            # the polled copy that follows has the picture. Dropping the
            # duplicate whole dropped that with it, which is why a notification
            # sometimes had a photo and sometimes did not.
            if picture_url and not self._has_fresh_picture(serial):
                await self.async_download_snapshot(serial, picture_url)
            return

        device = self.data[serial]
        _LOGGER.info("%s: %s, from %s", device.name, event_type, why)

        now = dt_util.utcnow()
        device.last_event = event_type
        device.last_event_time = now
        if event_type == EVENT_RING:
            device.last_ring = now
        elif event_type == EVENT_MOTION:
            device.last_motion = now

        async_dispatcher_send(
            self.hass,
            signal_event(self.config_entry.entry_id),
            serial,
            event_type,
            attributes,
        )
        self.async_update_listeners()

        picture = None
        if picture_url:
            picture = await self.async_download_snapshot(serial, picture_url)

        if (
            picture is None
            and self._should_capture(event_type)
            and not self._has_fresh_picture(serial)
        ):
            # Either nothing came with the message or what came was no use -
            # an address that answers with nothing, a picture that will not
            # decrypt. The camera refreshes its own picture only when something
            # makes it, so this is what makes it.
            _LOGGER.debug(
                "%s for %s brought no usable picture; taking one",
                event_type,
                serial,
            )
            try:
                await self.async_capture(serial)
            except (PyEzvizError, OSError, HomeAssistantError) as err:
                _LOGGER.info("Could not take a picture for %s: %s", serial, err)

    def _should_capture(self, event_type: str) -> bool:
        """Return whether to take a picture for an event that brought none."""
        if self._capture_when_missing == CAPTURE_ALWAYS:
            return True
        return (
            self._capture_when_missing == CAPTURE_RING and event_type == EVENT_RING
        )

    def _has_fresh_picture(self, serial: str) -> bool:
        """Return whether this camera's picture belongs to what just happened."""
        device = self.data.get(serial)
        if device is None or device.snapshot_time is None:
            return False
        age = (dt_util.utcnow() - device.snapshot_time).total_seconds()
        return age < PICTURE_FRESH_SECONDS

    # ------------------------------------------------------------------
    # Pictures
    # ------------------------------------------------------------------

    def _decrypt(self, serial: str, data: bytes) -> bytes | None:
        """Return a plain JPEG, decrypting an EZVIZ encrypted one if needed."""
        if data[: len(HIK_ENCRYPTION_HEADER)] != HIK_ENCRYPTION_HEADER:
            return data

        assert self.client is not None
        key = self.media_key(serial) or self._fetch_media_key(serial)
        if key is None:
            return None
        try:
            return decrypt_image(data, key)
        except (PyEzvizError, OSError) as err:
            _LOGGER.warning(
                "Could not decrypt a picture from %s (%s). Put the"
                " device's verification code - the letters on its label - into"
                " the integration's options, or turn image encryption off in"
                " the EZVIZ app",
                serial,
                err,
            )
            return None

    def _download(self, serial: str, url: str) -> bytes | None:
        """Download and decrypt one picture (executor thread)."""
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException as err:
            _LOGGER.warning("Could not download a picture for %s: %s", serial, err)
            return None
        return self._decrypt(serial, response.content)

    async def async_download_snapshot(self, serial: str, url: str) -> bytes | None:
        """Download an alarm snapshot and keep it for the image entity."""
        image = await self.hass.async_add_executor_job(self._download, serial, url)
        if image:
            self._store_snapshot(serial, image)
        return image

    def _store_snapshot(self, serial: str, image: bytes) -> None:
        """Remember a picture and tell the entities it is there."""
        device = self.data[serial]
        device.snapshot = image
        device.snapshot_time = dt_util.utcnow()
        self.async_update_listeners()

    async def async_capture(self, serial: str) -> bytes | None:
        """Make the camera take a picture now and return it.

        This is also what wakes a sleeping battery camera: the cloud has to
        reach the device to get a fresh frame out of it.

        Raises:
            HomeAssistantError: If the installed pyezvizapi cannot capture.
        """

        if not hasattr(self.client, "capture_picture"):
            # Pressing a button and having nothing happen, with nothing in the
            # log either, is worse than being told why.
            raise HomeAssistantError(
                "The bundled pyezvizapi cannot take a picture on demand;"
                " reinstall the integration."
            )

        def _capture() -> bytes | None:
            assert self.client is not None
            with self._api_lock:
                response = self.client.capture_picture(serial, 1)
            url = first_image_url(response)
            if not url:
                _LOGGER.warning(
                    "Capture for %s returned no image URL: %s", serial, response
                )
                return None
            return self._download(serial, url)

        image = await self.hass.async_add_executor_job(_capture)
        if image:
            self._store_snapshot(serial, image)
        return image

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def async_execute(
        self, action: Callable[[EzvizClient], Any], *, refresh: bool = True
    ) -> Any:
        """Run one cloud call off the event loop and refresh afterwards.

        Raises:
            HomeAssistantError: If EZVIZ refuses the call.
        """

        def _run() -> Any:
            if self.client is None:
                raise HomeAssistantError("Not connected to EZVIZ")
            with self._api_lock:
                return action(self.client)

        try:
            result = await self.hass.async_add_executor_job(_run)
        except AttributeError as err:
            raise HomeAssistantError(
                f"The installed pyezvizapi cannot do this: {err}"
            ) from err
        except (PyEzvizError, OSError, ValueError, KeyError) as err:
            raise HomeAssistantError(f"EZVIZ refused the request: {err}") from err

        if refresh:
            # The cloud needs a moment before it reports the new value.
            await asyncio.sleep(2)
            await self.async_request_refresh()
        return result

    def verification_code(self, serial: str) -> str | None:
        """Return the code configured by hand for one device, if any."""
        return self._verification_codes.get(serial)

    def media_key(self, serial: str) -> str | None:
        """Return what decrypts this camera, without going to the cloud.

        Either the code from its label, or the key EZVIZ handed out when the
        device was last read. Both decrypt the pictures and the video; the code
        only has to be typed in when EZVIZ will not give this account the key.
        """
        if configured := self._verification_codes.get(serial):
            return configured
        device = self.data.get(serial)
        return device.media_key if device else None

    def _fetch_media_key(self, serial: str) -> str | None:
        """Ask EZVIZ for a camera's key (executor thread)."""
        if serial in self._keyless:
            return None
        try:
            with self._api_lock:
                key = self.client.get_cam_key(serial)
        except (PyEzvizError, OSError, AttributeError) as err:
            # Not every account is given one - a shared device, most often -
            # and an older library has no way to ask at all.
            self._keyless.add(serial)
            _LOGGER.info(
                "EZVIZ will not give this account the key for %s (%s). Its"
                " pictures and video stay encrypted unless the code from its"
                " label is given to the integration under Reconfigure",
                serial,
                err,
            )
            return None
        return str(key) if key else None

    async def async_keep_awake(self, serial: str) -> bool:
        """Ask the cloud to keep a battery camera awake a while longer."""
        try:
            await self.async_execute(
                lambda client: client.delay_battery_device_sleep(serial, 1, 1),
                refresh=False,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Could not delay sleep for %s: %s", serial, err)
            return False
        return True

    async def async_wake(self, serial: str) -> None:
        """Wake a hibernating battery camera.

        A battery doorbell sleeps between events, and while it sleeps it
        answers nothing. The EZVIZ app wakes it by opening the live view, which
        is three separate requests underneath. All three are tried in turn, and
        each is allowed to fail: which of them a given model honours depends on
        its firmware, and one success is enough.
        """
        woke = False

        try:
            await self.async_execute(
                lambda client: client.switch_status(
                    serial, DeviceSwitchType.SLEEP.value, 0
                ),
                refresh=False,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Wake %s: the sleep switch was refused (%s)", serial, err)
        else:
            _LOGGER.debug("Wake %s: sleep switch turned off", serial)
            woke = True

        if await self.async_keep_awake(serial):
            _LOGGER.debug("Wake %s: the cloud was asked to keep it awake", serial)
            woke = True

        # Capturing a picture is what actually pulls a sleeping device onto the
        # network, and it leaves a fresh image behind as proof that it worked.
        try:
            woke = bool(await self.async_capture(serial)) or woke
        except (PyEzvizError, OSError, HomeAssistantError) as err:
            _LOGGER.info("Wake %s: could not take a picture (%s)", serial, err)

        if not woke:
            _LOGGER.warning(
                "Wake %s: every wake request was refused - the device may be"
                " out of battery, offline, or shared to this account without"
                " control rights",
                serial,
            )
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def async_unload(self) -> None:
        """Close the push connection and stop polling."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

        def _close() -> None:
            if self._push_client is not None:
                try:
                    self._push_client.stop()
                except (PyEzvizError, OSError) as err:
                    _LOGGER.debug("Error stopping EZVIZ push: %s", err)
            if self.client is not None:
                try:
                    self.client.close_session()
                except (PyEzvizError, OSError) as err:
                    _LOGGER.debug("Error closing the EZVIZ session: %s", err)

        await self.hass.async_add_executor_job(_close)
        self._push_client = None
        self.client = None


def _int(value: Any) -> int:
    """Return an alert code as an int, or -1 when there is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def verification_codes(entry: ConfigEntry) -> dict[str, str]:
    """Return the verification code configured for each camera.

    They are part of the account's own data, edited through Reconfigure. The
    two places they used to live - a text option, then a subentry per camera -
    are still read first, so an upgrade carries an existing code across.
    """
    codes = _codes_from_text(entry.options.get(CONF_VERIFICATION_CODES))
    stored = entry.data.get(CONF_VERIFICATION_CODES)
    if isinstance(stored, dict):
        codes.update({str(k): str(v) for k, v in stored.items() if k and v})
    for subentry in entry.subentries.values():
        if subentry.subentry_type != CAMERA_SUBENTRY:
            continue
        serial = subentry.data.get(CONF_SERIAL)
        code = subentry.data.get(CONF_VERIFICATION_CODE)
        if serial and code:
            codes[str(serial)] = str(code)
    return codes


def config_signature(entry: ConfigEntry) -> tuple[Any, ...]:
    """Return what a reload depends on.

    An entry is updated for reasons that are none of our business - a refreshed
    session, most often - and reloading on those would be an endless loop. Only
    the options and the per camera codes should bring the integration back up.
    """
    return (
        tuple(sorted((key, repr(value)) for key, value in entry.options.items())),
        tuple(sorted(verification_codes(entry).items())),
    )


def _codes_from_text(value: Any) -> dict[str, str]:
    """Return SERIAL=CODE pairs from the old single text option."""
    codes: dict[str, str] = {}
    separators = str(value or "").replace(";", ",").replace(chr(10), ",")
    for pair in separators.split(","):
        serial, marker, code = pair.partition("=")
        if marker and serial.strip() and code.strip():
            codes[serial.strip()] = code.strip()
    return codes


def _codes(values: Any) -> set[int]:
    """Return a set of integer alert codes from an option value."""
    if isinstance(values, str):
        values = values.replace(";", ",").split(",")
    codes: set[int] = set()
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        try:
            codes.add(int(text))
        except ValueError:
            _LOGGER.warning("Ignoring the non-numeric alert code %r", text)
    return codes
