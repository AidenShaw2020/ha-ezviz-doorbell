"""Serve the EZVIZ cloud live stream from inside Home Assistant.

The cameras this integration exists for have no RTSP server, so there is no URL
to hand the stream component. There is a cloud stream, but it only exists as
bytes produced by ``pyezvizapi`` and FFmpeg, so this view is what turns those
bytes back into a URL - which is all ``Camera.stream_source`` really needs.

FFmpeg opens that URL itself and cannot present a Home Assistant login, so the
view carries a token generated for the config entry instead of the usual auth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging
import secrets

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import CONF_STREAM_TOKEN, DOMAIN
from .vendor.pyezvizapi.exceptions import PyEzvizError

_LOGGER = logging.getLogger(__name__)


def import_cloud_stream() -> Callable[..., None] | None:
    """Return pyezvizapi's cloud stream copier, or None if it has none.

    The library is bundled with the integration, so this should always find
    it. It stays a question asked by importing rather than a plain import at
    the top of the module: this once had to survive a half-replaced library on
    the system, and there is no reason to make live video able to stop the
    integration loading again. Run it in an executor - it reads from disk.
    """
    try:
        from .vendor.pyezvizapi.cloud_stream import (  # noqa: PLC0415
            copy_cloud_stream_to_mpegts,
        )
    except ImportError as err:
        _LOGGER.debug("No usable cloud stream in the installed pyezvizapi: %s", err)
        return None
    return copy_cloud_stream_to_mpegts


# Enough to ride out a slow client without letting the stream run far ahead of
# what has actually been sent.
QUEUE_SIZE = 32
WRITE_TIMEOUT = 15.0


def stream_path(entry_id: str, serial: str) -> str:
    """Return the path the live stream for one camera is served on."""
    return f"/api/{DOMAIN}/{entry_id}/{serial}/live.ts"


class EzvizStreamView(HomeAssistantView):
    """Stream one camera as MPEG-TS."""

    url = "/api/" + DOMAIN + "/{entry_id}/{serial}/live.ts"
    name = f"api:{DOMAIN}:stream"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, serial: str
    ) -> web.StreamResponse:
        """Answer one live stream request."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            return web.Response(status=404, text="Unknown account")

        token = str(entry.data.get(CONF_STREAM_TOKEN) or "")
        given = request.query.get("token", "")
        if not token or not secrets.compare_digest(given, token):
            return web.Response(status=401, text="Missing or wrong token")

        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None or serial not in coordinator.data:
            return web.Response(status=404, text="Unknown camera")

        copy_cloud_stream_to_mpegts = coordinator.cloud_stream
        if copy_cloud_stream_to_mpegts is None:
            _LOGGER.warning(
                "The bundled pyezvizapi has no cloud stream, so there is no"
                " live video to serve. Reinstall the integration. Snapshots"
                " work either way."
            )
            return web.Response(status=501, text="No cloud stream in this build")

        # A sleeping battery camera answers nothing, so ask the cloud to keep
        # it awake before opening the stream.
        await coordinator.async_keep_awake(serial)

        encrypted = bool(coordinator.data[serial].raw.get("encrypted"))
        media_key = coordinator.verification_code(serial)
        if encrypted and media_key is None:
            # The stream is decrypted as it arrives, but only with a key, and
            # EZVIZ will not hand every account one over its API.
            _LOGGER.warning(
                "Video encryption is on for %s and there is no key for it, so"
                " there is no live video. Give the integration the code from"
                " the device's label under Reconfigure, or switch video"
                " encryption off for it in the EZVIZ app",
                serial,
            )
            return web.Response(status=501, text="Encrypted, and no key to use")

        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
        )
        await response.prepare(request)

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        writer = _QueueWriter(self.hass.loop, queue)

        def _produce() -> None:
            """Fill the queue from the cloud stream (executor thread)."""
            # Imported here for the same reason the copier is: a bundled
            # library that turned out to be missing something should cost the
            # live video, not the whole integration.
            from .decrypting_stream import (  # noqa: PLC0415
                copy_decrypted_cloud_stream,
            )

            try:
                if encrypted:
                    copy_decrypted_cloud_stream(
                        coordinator.client, serial, writer, media_key
                    )
                else:
                    copy_cloud_stream_to_mpegts(coordinator.client, serial, writer)
            except (BrokenPipeError, ConnectionResetError):
                _LOGGER.debug("Live stream for %s closed by the client", serial)
            except (PyEzvizError, OSError, ValueError, ImportError) as err:
                _LOGGER.warning("Live stream for %s failed: %s", serial, err)
            finally:
                writer.close()

        producer = self.hass.async_add_executor_job(_produce)
        try:
            while (chunk := await queue.get()) is not None:
                await response.write(chunk)
        except (ConnectionResetError, ConnectionError):
            _LOGGER.debug("Client went away during the live stream for %s", serial)
        finally:
            # Unblocks the producer if it is waiting on a queue nobody is
            # draining any more, so the executor thread cannot leak.
            writer.abort()
            with suppress(Exception):
                await producer

        return response


class _QueueWriter:
    """A file-like object that hands what is written to it to the event loop.

    ``copy_cloud_stream_to_mpegts`` writes to a plain binary file object from a
    worker thread, while the response has to be written from the event loop.
    Putting a bounded queue between the two bridges that, and the bound is what
    keeps a slow client from letting the stream run away in memory.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[bytes | None]
    ) -> None:
        """Initialize the writer."""
        self._loop = loop
        self._queue = queue
        self._aborted = False

    def write(self, data: bytes) -> int:
        """Queue a chunk, blocking while the queue is full.

        Raises:
            BrokenPipeError: If the reader has gone away.
        """
        if self._aborted:
            raise BrokenPipeError("Live stream reader has gone")
        future = asyncio.run_coroutine_threadsafe(self._queue.put(data), self._loop)
        try:
            future.result(timeout=WRITE_TIMEOUT)
        except TimeoutError as err:
            future.cancel()
            raise BrokenPipeError("Live stream reader stopped reading") from err
        return len(data)

    def flush(self) -> None:
        """Do nothing; every write is already handed over immediately."""

    def close(self) -> None:
        """Tell the reader the stream has ended."""
        self._put_nowait(None)

    def abort(self) -> None:
        """Stop accepting writes and unblock the writing thread."""
        self._aborted = True
        self._drain()

    def _drain(self) -> None:
        """Empty the queue so a blocked producer can move on and fail."""

        def _empty() -> None:
            while not self._queue.empty():
                self._queue.get_nowait()

        self._loop.call_soon_threadsafe(_empty)

    def _put_nowait(self, item: bytes | None) -> None:
        """Put an item on the queue from whichever thread we are on."""

        def _put() -> None:
            with suppress(asyncio.QueueFull):
                self._queue.put_nowait(item)

        self._loop.call_soon_threadsafe(_put)


def stream_url(
    hass: HomeAssistant, entry_id: str, serial: str, token: str
) -> str | None:
    """Return the absolute URL of one camera's live stream.

    FFmpeg opens this from inside Home Assistant, so the internal address is
    the right one even when Home Assistant is normally reached from outside.
    """
    try:
        base = get_url(hass, allow_external=False, allow_ip=True, require_ssl=False)
    except NoURLAvailableError:
        _LOGGER.warning(
            "Home Assistant has no internal URL configured, so the live stream"
            " cannot be handed to the stream component"
        )
        return None
    return f"{base}{stream_path(entry_id, serial)}?token={token}"
